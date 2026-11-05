"""SFTP transfer jobs with two active workers, bounded progress and safe files.

Local file operations use directory descriptors and O_NOFOLLOW on POSIX.
Platforms without those primitives fail closed for local transfers; native
Windows handle-based traversal still needs implementation and live validation.
Uploads replace a remote file only when explicitly requested. An interrupted
replacement deletes the partial file; SFTP v3 does not offer rollback of the
previous contents. Downloads publish only after the complete file is received.
"""
import atexit
import collections
import copy
import datetime
import os
import posixpath
import secrets
import stat
import threading
import time

from . import dbops, ssh_tunnel
from .sftp import SftpCancelled, SftpError, remote_path

TERMINAL = ('finished', 'failed', 'cancelled')
JOB_TTL = 3600.0
MAX_JOBS = 256
MAX_ITEMS = 10000


def _local_path(path):
    if (not isinstance(path, str) or not path or len(path) > 4096 or not os.path.isabs(path)
            or any(ord(c) < 32 or ord(c) == 127 for c in path)):
        raise ValueError('Enter an absolute local path without control characters.')
    # Do not normalize away a symlink followed by '..' before checking it.
    if '..' in path.replace('\\', '/').split('/'):
        raise ValueError('Local paths must not contain parent-directory traversal (..).')
    return os.path.abspath(path)


def _target_name(name):
    # A remote POSIX name is also checked against Windows separators, devices,
    # streams and drive syntax before it can become a local filename.
    stem = name.split('.')[0].upper() if isinstance(name, str) else ''
    devices = ('CON', 'PRN', 'AUX', 'NUL') + tuple('COM%d' % i for i in range(1, 10)) + tuple(
        'LPT%d' % i for i in range(1, 10))
    if (not isinstance(name, str) or not name or name in ('.', '..') or '/' in name or '\\' in name
            or ':' in name or name.endswith((' ', '.')) or stem in devices
            or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise ValueError('The transfer target must be a single safe file name without directory traversal.')
    return name


class _LocalDirectory(object):
    """Pin every ancestor without following links; use the final fd for I/O."""

    def __init__(self, path):
        self.path = _local_path(path)
        self.fd = None
        if not (os.name == 'posix' and hasattr(os, 'O_NOFOLLOW') and os.open in os.supports_dir_fd):
            raise ValueError('Safe local file transfer is unavailable on this platform.')
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open('/', flags)
        try:
            for component in self.path.split('/'):
                if not component:
                    continue
                try:
                    following = os.open(component, flags, dir_fd=fd)
                except OSError:
                    raise ValueError('The local folder must exist and no path component may be a symbolic link.') from None
                os.close(fd)
                fd = following
            self.fd = fd
        except BaseException:
            os.close(fd)
            raise

    def info(self, name):
        try:
            return os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def check_final(self, name, overwrite):
        info = self.info(name)
        if info is not None:
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('The local target must be a regular file, not a symbolic link or folder.')
            if not overwrite:
                raise ValueError('The local target already exists. Enable overwrite to replace it.')

    def open_read(self, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('The local source must be a regular file, not a symbolic link, device or pipe.')
            return fd, info
        except BaseException:
            os.close(fd)
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


def _check_paths(paths, label):
    if not isinstance(paths, list) or not paths or len(paths) > MAX_ITEMS or any(not isinstance(p, str) for p in paths):
        raise ValueError('%s must be a non-empty list of at most %d file paths.' % (label, MAX_ITEMS))


class _Job(object):
    def __init__(self, direction, spec, items, overwrite, now):
        self.spec, self.overwrite = spec, overwrite
        self.cancel = threading.Event()
        self.finished = None
        self.samples = collections.deque()
        self.started = None
        self.thread = None
        self.session = None
        self.public = {'id': secrets.token_hex(16), 'direction': direction, 'items': items,
                       'state': 'queued', 'bytes_done': 0, 'bytes_total': sum(i['bytes'] for i in items),
                       'rate_bps': 0.0, 'eta_s': None, 'started_at': None}
        self.created = now

    def metrics(self, now):
        rate = 0.0
        if self.samples and self.started is not None:
            cutoff = max(self.started, now - 5.0)
            while len(self.samples) > 1 and self.samples[1][0] <= cutoff:
                self.samples.popleft()
            at, base = self.samples[0]
            if at < cutoff and len(self.samples) > 1:
                following, value = self.samples[1]
                if following > at:
                    base += (value - base) * (cutoff - at) / (following - at)
            elapsed = now - cutoff
            if elapsed > 0:
                rate = max(0.0, (self.public['bytes_done'] - base) / elapsed)
        self.public['rate_bps'] = rate
        remaining = max(0, self.public['bytes_total'] - self.public['bytes_done'])
        self.public['eta_s'] = remaining / rate if rate > 0 else None
        if self.public['state'] in TERMINAL:
            self.public['eta_s'] = 0.0 if self.public['state'] == 'finished' else None


class SftpJobManager(object):
    """One thread per active job; the remaining jobs stay queued without threads.

    ``session_factory(spec)`` returns a private SftpSession, never a browsing
    cache lease. ``clock`` is monotonic and injectable for deterministic tests.
    ``store`` is an optional WorkspaceStore-compatible audit sink.
    """

    def __init__(self, store=None, session_factory=None, clock=time.monotonic):
        self.store, self.session_factory, self.clock = store, session_factory, clock
        self._lock = threading.RLock()
        self._jobs = collections.OrderedDict()
        self._running = 0
        self._closing = False

    def _prune(self):
        now = self.clock()
        for key, job in list(self._jobs.items()):
            if job.finished is not None and now - job.finished >= JOB_TTL:
                del self._jobs[key]

    def start_upload(self, spec, local_paths, remote_dir, overwrite=False):
        _check_paths(local_paths, 'Local paths')
        directory = remote_path(remote_dir)
        items, targets = [], set()
        for raw in local_paths:
            path = _local_path(raw)
            name = _target_name(os.path.basename(path))
            with _LocalDirectory(os.path.dirname(path)) as parent:
                try:
                    fd, info = parent.open_read(name)
                except OSError:
                    raise ValueError('The local source cannot be opened without following symbolic links.') from None
                os.close(fd)
            target = posixpath.join(directory, name)
            if target in targets:
                raise ValueError('Two selected files have the same transfer target.')
            targets.add(target)
            items.append({'source': path, 'target': target, 'bytes': info.st_size, 'done': 0,
                          'state': 'queued', 'error': None})
        return self._start('upload', spec, items, overwrite)

    def start_download(self, spec, remote_paths, local_dir, overwrite=False):
        _check_paths(remote_paths, 'Remote paths')
        if type(overwrite) is not bool:
            raise ValueError('Overwrite must be a boolean.')
        items, targets = [], set()
        with _LocalDirectory(local_dir) as parent:
            for raw in remote_paths:
                # Validate the raw basename before posixpath can erase '..'.
                name = _target_name(posixpath.basename(raw))
                source = remote_path(raw)
                parent.check_final(name, overwrite)
                target = os.path.join(parent.path, name)
                if target in targets:
                    raise ValueError('Two selected files have the same transfer target.')
                targets.add(target)
                items.append({'source': source, 'target': target, 'bytes': 0, 'done': 0,
                              'state': 'queued', 'error': None})
        # A target cannot be another item's staging path.
        if any(target + '.myflames-part' in targets for target in targets):
            raise ValueError('A selected file conflicts with another transfer staging file.')
        return self._start('download', spec, items, overwrite)

    def _start(self, direction, spec, items, overwrite):
        if type(overwrite) is not bool:
            raise ValueError('Overwrite must be a boolean.')
        fields = ssh_tunnel.ssh_fields(spec)
        if not fields:
            raise ValueError('An SSH host is required for file transfer.')
        with self._lock:
            self._prune()
            if self._closing:
                raise ValueError('The file transfer manager is shutting down.')
            if len(self._jobs) >= MAX_JOBS:
                raise ValueError('Too many file transfer jobs. Try again after older jobs expire.')
            job = _Job(direction, fields, items, overwrite, self.clock())
            self._jobs[job.public['id']] = job
            self._schedule()
            return copy.deepcopy(job.public)

    def _schedule(self):
        for job in self._jobs.values():
            if self._closing or self._running >= 2:
                break
            if job.public['state'] != 'queued':
                continue
            self._running += 1
            job.public['state'] = 'running'
            job.started = self.clock()
            job.public['started_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            job.samples.append((job.started, 0))
            job.thread = threading.Thread(target=self._run, args=(job,), name='sftp-job', daemon=True)
            job.thread.start()

    def _progress(self, job, item, done):
        with self._lock:
            previous = item['done']
            item['done'] = done
            job.public['bytes_done'] += done - previous
            at = self.clock()
            if job.samples and job.samples[-1][0] == at:
                # Preserve the initial zero sample until time has advanced.
                if len(job.samples) > 1:
                    job.samples[-1] = (at, job.public['bytes_done'])
            else:
                job.samples.append((at, job.public['bytes_done']))
            job.metrics(at)

    def _audit(self, job):
        if self.store is None:
            return None
        return dbops.AuditTrail(self.store, 'sftp_' + job.public['direction'], 'ui', connection=job.spec)

    @staticmethod
    def _check_cancel(job):
        if job.cancel.is_set():
            raise SftpCancelled('File transfer cancelled.')

    def _run(self, job):
        session, audit = None, self._audit(job)
        state, error = 'finished', None
        try:
            if audit is not None:
                audit.attempt([], target={'direction': job.public['direction'], 'files': len(job.public['items'])})
            self._check_cancel(job)
            if self.session_factory is None:
                from .sftp_api import open_session
                session = open_session(job.spec)
            else:
                session = self.session_factory(job.spec)
            with self._lock:
                job.session = session
            self._check_cancel(job)
            if job.public['direction'] == 'download':
                # Read all sizes first so ETA refers to the entire job.
                for item in job.public['items']:
                    self._check_cancel(job)
                    info = session.stat(item['source'])
                    if (type(info.get('size')) is not int or info['size'] < 0
                            or info.get('mode') is None or not stat.S_ISREG(info['mode'])):
                        raise ValueError('Only remote regular files with a known size can be downloaded.')
                    with self._lock:
                        item['bytes'] = info['size']
                        job.public['bytes_total'] += info['size']
            for item in job.public['items']:
                self._check_cancel(job)
                with self._lock:
                    item['state'] = 'running'
                if job.public['direction'] == 'upload':
                    self._upload(job, item, session)
                else:
                    self._download(job, item, session)
                with self._lock:
                    item['state'] = 'finished'
        except Exception as exc:
            state = 'cancelled' if isinstance(exc, SftpCancelled) else 'failed'
            error = getattr(session, 'safe', dbops.redact)(str(exc))
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    if state == 'finished':
                        state, error = 'failed', 'SSH could not be closed after the transfer.'
            if audit is not None:
                if audit.id is not None:
                    audit.complete('ok' if state == 'finished' else 'error', outcome=state, error=error,
                                   message='File transfer ' + state + '.')
                else:
                    audit.outcome([], 'error', outcome=state, error=error)
            with self._lock:
                for item in job.public['items']:
                    if item['state'] != 'finished':
                        item['state'], item['error'] = state if state != 'finished' else 'failed', error
                job.public['state'] = state
                job.finished = self.clock()
                job.metrics(job.finished)
                job.session = None
                self._running -= 1
                self._schedule()

    def _upload(self, job, item, session):
        with _LocalDirectory(os.path.dirname(item['source'])) as parent:
            fd, info = parent.open_read(os.path.basename(item['source']))
            try:
                with self._lock:
                    job.public['bytes_total'] += info.st_size - item['bytes']
                    item['bytes'] = info.st_size
                # Never knowingly truncate a remote symlink or special file.
                try:
                    previous = session.lstat(item['target'])
                except SftpError as exc:
                    if exc.code != 2:
                        raise
                    previous = None
                if previous is not None:
                    if not job.overwrite:
                        raise ValueError('The remote target already exists. Enable overwrite to replace it.')
                    if previous.get('mode') is None or not stat.S_ISREG(previous['mode']):
                        raise ValueError('The remote target must be a regular file, not a symbolic link or folder.')
                session.put(fd, item['target'], info.st_size,
                            lambda done: self._progress(job, item, done), job.cancel, overwrite=job.overwrite)
                try:
                    self._check_cancel(job)
                    # Drop special bits. Local file permissions do not grant
                    # a remote setuid/setgid/sticky executable capability.
                    session.setstat(item['target'], stat.S_IMODE(info.st_mode) & 0o777)
                except Exception:
                    session.remove(item['target'])
                    raise
            finally:
                os.close(fd)

    def _download(self, job, item, session):
        name = _target_name(os.path.basename(item['target']))
        part = name + '.myflames-part'
        with _LocalDirectory(os.path.dirname(item['target'])) as parent:
            parent.check_final(name, job.overwrite)
            fd = os.open(part, os.O_WRONLY | os.O_EXCL | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent.fd)
            try:
                session.get(item['source'], fd, item['bytes'], lambda done: self._progress(job, item, done), job.cancel)
                self._check_cancel(job)
                os.fsync(fd)
                written = os.fstat(fd)
                if written.st_size != item['bytes']:
                    raise SftpError('The downloaded file size does not match the remote file size.')
                current = parent.info(part)
                if current is None or (current.st_dev, current.st_ino) != (written.st_dev, written.st_ino):
                    raise ValueError('The download staging file changed during transfer.')
                parent.check_final(name, job.overwrite)
                self._check_cancel(job)
                if job.overwrite:
                    os.replace(part, name, src_dir_fd=parent.fd, dst_dir_fd=parent.fd)
                else:
                    # rename() would silently replace a racing final file on
                    # POSIX. link() publishes atomically and refuses EEXIST.
                    os.link(part, name, src_dir_fd=parent.fd, dst_dir_fd=parent.fd, follow_symlinks=False)
                    os.unlink(part, dir_fd=parent.fd)
            finally:
                os.close(fd)
                try:
                    os.unlink(part, dir_fd=parent.fd)
                except FileNotFoundError:
                    pass

    def jobs(self):
        with self._lock:
            self._prune()
            now = self.clock()
            for job in self._jobs.values():
                if job.public['state'] not in TERMINAL:
                    job.metrics(now)
            return [copy.deepcopy(job.public) for job in self._jobs.values()]

    def cancel(self, job_id):
        with self._lock:
            self._prune()
            job = self._jobs.get(job_id)
            if job is None:
                raise ValueError('No file transfer job has that ID.')
            job.cancel.set()
            if job.public['state'] == 'queued':
                job.public['state'] = 'cancelled'
                job.finished = self.clock()
                for item in job.public['items']:
                    item['state'], item['error'] = 'cancelled', 'File transfer cancelled.'
                audit = self._audit(job)
                if audit is not None:
                    audit.outcome([], 'error', outcome='cancelled', message='Queued file transfer cancelled.')
            return copy.deepcopy(job.public)

    def shutdown(self):
        with self._lock:
            self._closing = True
            jobs = list(self._jobs.values())
        for job in jobs:
            try:
                self.cancel(job.public['id'])
            except ValueError:
                # cancel() prunes terminal jobs first, including this snapshot.
                pass
        # First allow CLOSE/REMOVE cleanup; force-close transport if a peer
        # stalls. No job or ssh process is left behind on an orderly shutdown.
        deadline = time.monotonic() + 2
        for job in jobs:
            if job.thread is not None:
                job.thread.join(max(0, deadline - time.monotonic()))
        for job in jobs:
            if job.thread is not None and job.thread.is_alive() and job.session is not None:
                job.session.close()
        for job in jobs:
            if job.thread is not None:
                job.thread.join(35)


MANAGER = SftpJobManager()
start_upload = MANAGER.start_upload
start_download = MANAGER.start_download
jobs = MANAGER.jobs
cancel = MANAGER.cancel
shutdown = MANAGER.shutdown
atexit.register(shutdown)
