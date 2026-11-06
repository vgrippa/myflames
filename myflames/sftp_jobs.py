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
import re
import secrets
import stat
import threading
import time

from . import dbops
from .sftp import SftpCancelled, SftpError, remote_path

TERMINAL = ('finished', 'failed', 'cancelled')
JOB_TTL = 3600.0
MAX_JOBS = 256
MAX_ITEMS = 10000
MAX_DEPTH = 64
PUBLIC_ITEMS = 200


def _local_path(path):
    if (not isinstance(path, str) or not path or len(path) > 4096 or not os.path.isabs(path)
            or any(ord(c) < 32 or ord(c) == 127 for c in path)):
        raise ValueError('Enter an absolute local path without control characters.')
    # Do not normalize away a symlink followed by '..' before checking it.
    if '..' in path.replace('\\', '/').split('/'):
        raise ValueError('Local paths must not contain parent-directory traversal (..).')
    return os.path.abspath(path)


MAX_LINK_HOPS = 40


def _trusted_realpath(path):
    """realpath() that follows a link only when its owner is root or this user.

    macOS folders such as /tmp and /var are root's links; a link another user
    owns (in /tmp, /Users/Shared) could point the transfer at a folder of
    that user's choosing, so it is refused. A missing component ends the walk
    and the rest is kept as written (the transfer then reports it missing).
    """
    if not hasattr(os, 'getuid'):  # Windows: the dir-fd walk fails closed anyway.
        raise ValueError('Safe local file transfer is unavailable on this platform.')
    pending = [part for part in path.split('/') if part]
    resolved, hops, me = '/', 0, os.getuid()
    while pending:
        part = pending.pop(0)
        if part in ('', '.'):
            continue
        if part == '..':
            resolved = os.path.dirname(resolved) or '/'
            continue
        candidate = os.path.join(resolved, part)
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            return os.path.join(candidate, *pending)
        if stat.S_ISLNK(info.st_mode):
            if info.st_uid not in (0, me):
                raise ValueError('The chosen folder goes through a symbolic link owned by another user (%s). '
                                 'Choose the folder it points to instead.' % candidate)
            hops += 1
            if hops > MAX_LINK_HOPS:
                raise ValueError('The chosen folder goes through too many symbolic links.')
            target = os.readlink(candidate)
            again = os.lstat(candidate)
            if (again.st_dev, again.st_ino, again.st_uid) != (info.st_dev, info.st_ino, info.st_uid):
                raise ValueError('A symbolic link in the chosen folder changed while it was read. Try again.')
            if target.startswith('/'):
                resolved = '/'
            pending = [p for p in target.split('/') if p] + pending
        else:
            resolved = candidate
    return resolved


def _resolved(path, leaf=True):
    """The folder the person chose, with links in it resolved once, now.

    macOS folders such as /var and /tmp are links (to /private/...), and a
    chosen folder may sit under one. Resolving at the request is what the
    person picked (links owned by root or this user only, _trusted_realpath);
    every later open walks the resolved path without following links, so a
    link swapped in afterwards is refused. With ``leaf`` the last component
    is kept as given: a selected link is skipped, never followed.
    """
    path = _local_path(path)
    if not leaf:
        return _trusted_realpath(path)
    return os.path.join(_trusted_realpath(os.path.dirname(path)), os.path.basename(path))


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
                raise ValueError('Already exists.')

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


def local_transfers_available():
    """Whether uploads and downloads can run here: they need POSIX directory
    descriptors and O_NOFOLLOW (Windows fails closed; the UI says so)."""
    return os.name == 'posix' and hasattr(os, 'O_NOFOLLOW') and os.open in os.supports_dir_fd


def _check_paths(paths, label):
    if not isinstance(paths, list) or not paths or any(not isinstance(p, str) for p in paths):
        raise ValueError('%s must be a non-empty list of file paths.' % label)


def _item(source, target, size=0, state='queued', error=None):
    return {'source': source, 'target': target, 'bytes': size, 'done': 0,
            'state': state, 'error': error}


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class _ScanLimit(ValueError):
    pass


class _Job(object):
    def __init__(self, direction, opener, label, sources, target, on_exists, now):
        self.opener, self.label = opener, dict(label)
        self.cancel = threading.Event()
        self.finished = self.started = self.thread = self.session = None
        self.created = now
        self.samples = collections.deque()
        self.items, self.directories = [], []
        self.nonfiles = set()
        self.retry_items = None
        self.scanned = False  # The scan finished: items list every file.
        self.public = {
            'id': secrets.token_hex(16), 'ssh': label['ssh'], 'ssh_name': label['name'],
            'direction': direction, 'state': 'queued', 'phase': None,
            'on_exists': on_exists, 'target_dir': target, 'sources': list(sources),
            'files_total': 0, 'files_done': 0, 'files_skipped': 0, 'files_failed': 0,
            'bytes_total': 0, 'bytes_done': 0, 'rate_bps': 0.0, 'eta_s': None,
            'started_at': None, 'finished_at': None, 'error': None, 'error_code': None,
            'current': None, 'items': [], 'items_more': 0, 'failed_items': []}

    def snapshot(self):
        result = dict(self.public)
        result.update(files_total=len(self.items),
                      files_done=sum(i['state'] == 'finished' for i in self.items),
                      files_skipped=sum(i['state'] == 'skipped' for i in self.items),
                      files_failed=sum(i['state'] == 'failed' for i in self.items),
                      items=self.items[:PUBLIC_ITEMS],
                      items_more=max(0, len(self.items) - PUBLIC_ITEMS),
                      failed_items=[i for i in self.items if i['state'] in ('failed', 'cancelled')][:PUBLIC_ITEMS])
        return copy.deepcopy(result)

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
    """Two private sessions at once; queued jobs do not own threads or secrets.

    The caller supplies a zero-argument opener and a public connection label.
    ``clock`` is monotonic and injectable. ``store`` is an optional audit sink.
    """

    def __init__(self, store=None, clock=time.monotonic):
        self.store, self.clock = store, clock
        self._lock = threading.RLock()
        self._jobs = collections.OrderedDict()
        self._running = 0
        self._closing = False

    def _prune(self):
        now = self.clock()
        for key, job in list(self._jobs.items()):
            if job.finished is not None and now - job.finished >= JOB_TTL:
                del self._jobs[key]

    @staticmethod
    def _options(opener, label, on_exists):
        if on_exists not in ('fail', 'skip', 'overwrite'):
            raise ValueError('Choose fail, skip or overwrite for existing files.')
        if not callable(opener):
            raise ValueError('A private SSH session opener is required.')
        if (not isinstance(label, dict) or not isinstance(label.get('ssh'), str)
                or re.fullmatch('[0-9a-fA-F]{32}', label['ssh']) is None
                or not isinstance(label.get('name'), str)):
            raise ValueError('An SSH connection ID and display name are required.')

    @staticmethod
    def _targets(paths, directory, local=False):
        targets = set()
        for raw in paths:
            name = _target_name(posixpath.basename(raw))
            target = os.path.join(directory, name) if local else posixpath.join(directory, name)
            if target in targets:
                raise ValueError('Two selected files have the same transfer target.')
            targets.add(target)
        if local and any(target + '.myflames-part' in targets for target in targets):
            raise ValueError('A selected file conflicts with another transfer staging file.')

    def start_upload(self, opener, label, local_paths, remote_dir, on_exists):
        self._options(opener, label, on_exists)
        _check_paths(local_paths, 'Local paths')
        directory = remote_path(remote_dir)
        normalized = []
        for raw in local_paths:
            path = _resolved(raw)
            name = _target_name(os.path.basename(path))
            with _LocalDirectory(os.path.dirname(path)) as parent:
                info = parent.info(name)
                if info is not None and stat.S_ISDIR(info.st_mode):
                    try:
                        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent.fd)
                    except OSError:
                        raise ValueError('The local source cannot be opened without following symbolic links.') from None
                else:
                    try:
                        fd, info = parent.open_read(name)
                    except OSError:
                        raise ValueError('The local source cannot be opened without following symbolic links.') from None
                os.close(fd)
            normalized.append(path)
        self._targets(normalized, directory)
        return self._start('upload', opener, label, normalized, directory, on_exists)

    def start_download(self, opener, label, remote_paths, local_dir, on_exists):
        self._options(opener, label, on_exists)
        _check_paths(remote_paths, 'Remote paths')
        with _LocalDirectory(_resolved(local_dir, leaf=False)) as parent:
            self._targets(remote_paths, parent.path, local=True)
            for raw in remote_paths:
                remote_path(raw)
            directory = parent.path
        return self._start('download', opener, label, remote_paths, directory, on_exists)

    def _start(self, direction, opener, label, sources, target, on_exists, retry_items=None, directories=None):
        with self._lock:
            self._prune()
            if self._closing:
                raise ValueError('The file transfer manager is shutting down.')
            if len(self._jobs) >= MAX_JOBS:
                raise ValueError('Too many file transfer jobs. Try again after older jobs expire.')
            job = _Job(direction, opener, label, sources, target, on_exists, self.clock())
            job.retry_items = retry_items
            if retry_items is not None:
                job.items = [_item(i['source'], i['target'], i['bytes']) for i in retry_items]
                job.directories = list(directories or [])
                job.public['bytes_total'] = sum(i['bytes'] for i in job.items)
            self._jobs[job.public['id']] = job
            self._schedule()
            return job.snapshot()

    def _schedule(self):
        for job in self._jobs.values():
            if self._closing or self._running >= 2:
                break
            if job.public['state'] != 'queued':
                continue
            self._running += 1
            job.public.update(state='running', phase='scanning', started_at=_utcnow())
            job.started = self.clock()
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
                if len(job.samples) > 1:
                    job.samples[-1] = (at, job.public['bytes_done'])
            else:
                job.samples.append((at, job.public['bytes_done']))
            job.metrics(at)

    def _audit(self, job):
        if self.store is None:
            return None
        return dbops.AuditTrail(self.store, 'sftp_' + job.public['direction'], 'ui', connection=job.label)

    @staticmethod
    def _check_cancel(job):
        if job.cancel.is_set():
            raise SftpCancelled('File transfer cancelled.')

    @staticmethod
    def _session_failure(session, exc):
        if isinstance(exc, SftpCancelled):
            return False
        return ((isinstance(exc, SftpError) and exc.code is None)
                or (hasattr(session, 'alive') and not session.alive())
                or getattr(session, 'closed', False) is True)

    @staticmethod
    def _error(session, exc):
        return getattr(session, 'safe', dbops.redact)(str(exc))

    def _add(self, job, source, target, size=0, state='queued', error=None, nonfile=False):
        with self._lock:
            if len(job.items) >= MAX_ITEMS:
                raise _ScanLimit('This transfer has more than 10000 files. Transfer fewer files at a time.')
            item = _item(source, target, size, state, error)
            job.items.append(item)
            if nonfile:
                job.nonfiles.add(id(item))
            job.public['bytes_total'] += size
            return item

    def _scan_error(self, job, session, source, target, exc, nonfile=False):
        if isinstance(exc, (SftpCancelled, _ScanLimit)) or self._session_failure(session, exc):
            raise exc
        self._add(job, source, target, state='failed', error=self._error(session, exc), nonfile=nonfile)

    def _depth(self, job, depth):
        self._check_cancel(job)
        if depth > MAX_DEPTH:
            raise _ScanLimit('This transfer has folders deeper than 64 levels. Transfer fewer folder levels at a time.')

    def _scan_local(self, job, session, parent_fd, name, source, target, depth):
        self._check_cancel(job)
        is_directory = False
        try:
            _target_name(name)
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                self._add(job, source, target, state='skipped', error='Symbolic link not transferred.', nonfile=True)
            elif stat.S_ISDIR(info.st_mode):
                is_directory = True
                self._depth(job, depth)
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                try:
                    job.directories.append(target)
                    for child in sorted(os.listdir(fd)):
                        self._scan_local(job, session, fd, child, os.path.join(source, child),
                                         posixpath.join(target, child), depth + 1)
                finally:
                    os.close(fd)
            elif stat.S_ISREG(info.st_mode):
                self._add(job, source, target, info.st_size)
            else:
                self._add(job, source, target, state='skipped', error='Not a regular file.', nonfile=True)
        except Exception as exc:
            self._scan_error(job, session, source, target, exc, nonfile=is_directory)

    def _scan_remote(self, job, session, source, target, depth, info=None, top=False):
        self._check_cancel(job)
        is_directory = False
        try:
            if info is None:
                info = session.lstat(source)
            mode = info.get('mode')
            if mode is not None and stat.S_ISLNK(mode):
                if top:
                    followed = session.stat(source)
                    if followed.get('mode') is not None and stat.S_ISDIR(followed['mode']):
                        info, mode = followed, followed['mode']
                if stat.S_ISLNK(mode):
                    self._add(job, source, target, state='skipped', error='Symbolic link not transferred.', nonfile=True)
                    return
            if mode is not None and stat.S_ISDIR(mode):
                is_directory = True
                self._depth(job, depth)
                job.directories.append(target)
                for entry in session.listdir(source):
                    self._check_cancel(job)
                    name = entry.get('name')
                    try:
                        _target_name(name)
                    except ValueError as exc:
                        # Unsafe READDIR names are visible failures, never I/O paths.
                        self._add(job, source + '/' + str(name), target + '/' + str(name),
                                  state='failed', error=str(exc), nonfile=True)
                        continue
                    self._scan_remote(job, session, posixpath.join(source, name),
                                      os.path.join(target, name), depth + 1, info=entry)
            elif mode is not None and stat.S_ISREG(mode):
                size = info.get('size')
                if type(size) is not int or size < 0:
                    raise ValueError('Only remote regular files with a known size can be downloaded.')
                self._add(job, source, target, size)
            else:
                self._add(job, source, target, state='skipped', error='Not a regular file.', nonfile=True)
        except Exception as exc:
            self._scan_error(job, session, source, target, exc, nonfile=is_directory)

    def _scan(self, job, session):
        if job.retry_items is not None:
            # Retry exact file targets without expanding their parent folders
            # again. Refresh sizes before transferring any of the retry items.
            if job.public['direction'] == 'download':
                for item in job.items:
                    self._check_cancel(job)
                    try:
                        info = session.lstat(item['source'])
                        if (info.get('mode') is None or not stat.S_ISREG(info['mode'])
                                or type(info.get('size')) is not int or info['size'] < 0):
                            raise ValueError('Only remote regular files with a known size can be downloaded.')
                        with self._lock:
                            job.public['bytes_total'] += info['size'] - item['bytes']
                            item['bytes'] = info['size']
                    except Exception as exc:
                        if isinstance(exc, SftpCancelled) or self._session_failure(session, exc):
                            raise
                        with self._lock:
                            item.update(state='failed', error=self._error(session, exc))
            return
        for raw in job.public['sources']:
            self._check_cancel(job)
            if job.public['direction'] == 'upload':
                path = _local_path(raw)
                name = _target_name(os.path.basename(path))
                target = posixpath.join(job.public['target_dir'], name)
                try:
                    with _LocalDirectory(os.path.dirname(path)) as parent:
                        self._scan_local(job, session, parent.fd, name, path, target, 0)
                except Exception as exc:
                    self._scan_error(job, session, path, target, exc)
            else:
                path = remote_path(raw)
                target = os.path.join(job.public['target_dir'], _target_name(posixpath.basename(raw)))
                self._scan_remote(job, session, path, target, 0, top=True)
        # Detect nested staging collisions before creating any folders or bytes.
        if job.public['direction'] == 'download':
            with self._lock:
                targets = set(i['target'] for i in job.items if i['state'] == 'queued') | set(job.directories)
                for item in job.items:
                    if item['state'] == 'queued' and item['target'] + '.myflames-part' in targets:
                        item.update(state='failed', error='A selected file conflicts with another transfer staging file.')

    @staticmethod
    def _blocked(target, failures):
        for directory, error in failures:
            if target == directory or target.startswith(directory.rstrip('/') + '/'):
                return error
        return None

    def _folders(self, job, session):
        failures = []
        for target in job.directories:
            self._check_cancel(job)
            if self._blocked(target, failures):
                continue
            try:
                if job.public['direction'] == 'upload':
                    try:
                        info = session.lstat(target)
                    except SftpError as exc:
                        if exc.code != 2:
                            raise
                        info = None
                    if info is None:
                        try:
                            session.mkdir(target)
                        except SftpError as exc:
                            if self._session_failure(session, exc):
                                raise
                            info = session.lstat(target)
                            if info.get('mode') is None or not stat.S_ISDIR(info['mode']):
                                raise ValueError('The destination folder is not a folder.') from None
                    elif info.get('mode') is None or not stat.S_ISDIR(info['mode']):
                        raise ValueError('The destination folder is not a folder.')
                else:
                    with _LocalDirectory(os.path.dirname(target)) as parent:
                        name = _target_name(os.path.basename(target))
                        try:
                            # The user's umask decides, as for a folder made in the Finder.
                            os.mkdir(name, 0o777, dir_fd=parent.fd)
                        except FileExistsError:
                            pass
                        try:
                            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent.fd)
                        except OSError:
                            raise ValueError('The destination folder is not a folder or is a symbolic link.') from None
                        os.close(fd)
            except Exception as exc:
                if isinstance(exc, SftpCancelled) or self._session_failure(session, exc):
                    raise
                error = self._error(session, exc)
                failures.append((target, error))
        with self._lock:
            for item in job.items:
                if item['state'] == 'queued':
                    error = self._blocked(item['target'], failures)
                    if error is not None:
                        item.update(state='failed', error=error)
        return failures

    def _run(self, job):
        session, audit = None, None
        state, error = 'finished', None
        try:
            audit = self._audit(job)
            if audit is not None:
                audit.attempt([], target={'direction': job.public['direction'], 'files': len(job.public['sources'])})
            self._check_cancel(job)
            session = job.opener()
            with self._lock:
                job.session = session
            self._scan(job, session)
            job.scanned = True
            self._check_cancel(job)
            with self._lock:
                job.public['phase'] = 'transferring'
            failures = self._folders(job, session)
            for item in job.items:
                self._check_cancel(job)
                if item['state'] != 'queued':
                    continue
                with self._lock:
                    item['state'] = 'running'
                    job.public['current'] = item
                try:
                    if job.public['direction'] == 'upload':
                        self._upload(job, item, session)
                    else:
                        self._download(job, item, session)
                    with self._lock:
                        if item['state'] == 'running':
                            item['state'] = 'finished'
                except Exception as exc:
                    if isinstance(exc, SftpCancelled) or self._session_failure(session, exc):
                        raise
                    with self._lock:
                        item.update(state='failed', error=self._error(session, exc))
                finally:
                    with self._lock:
                        job.public['current'] = None
            errors = [i['error'] for i in job.items if i['state'] == 'failed']
            if errors or failures:
                state, error = 'failed', (errors[0] if errors else failures[0][1])
        except Exception as exc:
            state = 'cancelled' if isinstance(exc, SftpCancelled) else 'failed'
            error = self._error(session, exc)
            # The SSH reason (auth_failed, host_key_unknown...) lets the job row
            # offer Edit connection or the host-key prompt.
            reason = getattr(exc, 'reason', None) if isinstance(exc, SftpError) else None
            job.public['error_code'] = reason if isinstance(reason, str) and reason else None
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    if state == 'finished':
                        state, error = 'failed', 'SSH could not be closed after the transfer.'
            try:
                if audit is not None:
                    if audit.id is not None:
                        audit.complete('ok' if state == 'finished' else 'error', outcome=state, error=error,
                                       message='File transfer ' + state + '.')
                    else:
                        audit.outcome([], 'error', outcome=state, error=error)
            finally:
                with self._lock:
                    for item in job.items:
                        if item['state'] in ('queued', 'running'):
                            item.update(state=state if state != 'finished' else 'failed', error=error)
                    job.public.update(state=state, error=error, phase=None, current=None, finished_at=_utcnow())
                    job.finished = self.clock()
                    job.metrics(job.finished)
                    job.session = None
                    self._running -= 1
                    self._schedule()

    def _exists(self, job, item, previous, remote=False):
        if previous is None:
            return False
        policy = job.public['on_exists']
        if policy == 'skip':
            with self._lock:
                item['state'] = 'skipped'
            return True
        if policy == 'fail':
            raise ValueError('Already exists.')
        mode = previous.get('mode') if remote else previous.st_mode
        if mode is None or not stat.S_ISREG(mode):
            where = 'remote' if remote else 'local'
            raise ValueError('The %s target must be a regular file, not a symbolic link or folder.' % where)
        return False

    def _upload(self, job, item, session):
        with _LocalDirectory(os.path.dirname(item['source'])) as parent:
            name = _target_name(os.path.basename(item['source']))
            _target_name(posixpath.basename(item['target']))
            fd, info = parent.open_read(name)
            try:
                with self._lock:
                    job.public['bytes_total'] += info.st_size - item['bytes']
                    item['bytes'] = info.st_size
                try:
                    previous = session.lstat(item['target'])
                except SftpError as exc:
                    if exc.code != 2:
                        raise
                    previous = None
                if self._exists(job, item, previous, remote=True):
                    return
                # The mode is set on the open handle before CLOSE (never by path
                # afterwards); setuid, setgid and sticky bits are dropped.
                session.put(fd, item['target'], info.st_size,
                            lambda done: self._progress(job, item, done), job.cancel,
                            overwrite=job.public['on_exists'] == 'overwrite',
                            mode=stat.S_IMODE(info.st_mode) & 0o777)
            finally:
                os.close(fd)

    def _download(self, job, item, session):
        name = _target_name(os.path.basename(item['target']))
        part = _target_name(name + '.myflames-part')
        overwrite = job.public['on_exists'] == 'overwrite'
        with _LocalDirectory(os.path.dirname(item['target'])) as parent:
            if self._exists(job, item, parent.info(name)):
                return
            parent.check_final(name, overwrite)
            fd = os.open(part, os.O_WRONLY | os.O_EXCL | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent.fd)
            try:
                session.get(item['source'], fd, item['bytes'], lambda done: self._progress(job, item, done), job.cancel)
                self._check_cancel(job)
                os.fsync(fd)
                written = os.fstat(fd)
                if written.st_size != item['bytes']:
                    raise ValueError('The downloaded file size does not match the remote file size.')
                current = parent.info(part)
                if current is None or (current.st_dev, current.st_ino) != (written.st_dev, written.st_ino):
                    raise ValueError('The download staging file changed during transfer.')
                parent.check_final(name, overwrite)
                self._check_cancel(job)
                if overwrite:
                    os.replace(part, name, src_dir_fd=parent.fd, dst_dir_fd=parent.fd)
                else:
                    # link() refuses a racing final file instead of replacing it.
                    os.link(part, name, src_dir_fd=parent.fd, dst_dir_fd=parent.fd, follow_symlinks=False)
                    os.unlink(part, dir_fd=parent.fd)
            finally:
                os.close(fd)
                try:
                    os.unlink(part, dir_fd=parent.fd)
                except FileNotFoundError:
                    pass

    def jobs(self, ssh=None):
        with self._lock:
            self._prune()
            now = self.clock()
            result = []
            for job in reversed(list(self._jobs.values())):
                if ssh is None or job.public['ssh'] == ssh:
                    if job.public['state'] not in TERMINAL:
                        job.metrics(now)
                    result.append(job.snapshot())
            return result

    def _find(self, job_id):
        self._prune()
        job = self._jobs.get(job_id)
        if job is None:
            raise ValueError('No file transfer job has that ID.')
        return job

    def cancel(self, job_id):
        with self._lock:
            job = self._find(job_id)
            if job.public['state'] in TERMINAL:
                return job.snapshot()
            job.cancel.set()
            if job.public['state'] == 'queued':
                job.public.update(state='cancelled', phase=None, error='File transfer cancelled.', finished_at=_utcnow())
                job.finished = self.clock()
                for item in job.items:
                    item.update(state='cancelled', error='File transfer cancelled.')
                job.metrics(job.finished)
                audit = self._audit(job)
                if audit is not None:
                    audit.outcome([], 'error', outcome='cancelled', message='Queued file transfer cancelled.')
            return job.snapshot()

    def retry(self, job_id):
        with self._lock:
            job = self._find(job_id)
            if job.public['state'] not in TERMINAL:
                raise ValueError('Wait for the file transfer job to finish before retrying.')
            if not job.scanned and job.retry_items is None:
                # Stopped while scanning: nothing was transferred and the
                # files below the selection were never all listed.
                return self._start(job.public['direction'], job.opener, job.label, job.public['sources'],
                                   job.public['target_dir'], job.public['on_exists'])
            items = [i for i in job.items if i['state'] in ('failed', 'cancelled') and id(i) not in job.nonfiles]
            if not items:
                raise ValueError('This transfer has no failed or cancelled files to retry.')
            directories = [d for d in job.directories if any(i['target'].startswith(d.rstrip('/') + '/') for i in items)]
            return self._start(job.public['direction'], job.opener, job.label,
                               [i['source'] for i in items], job.public['target_dir'],
                               job.public['on_exists'], retry_items=items, directories=directories)

    def clear(self, ssh=None):
        # Failed and cancelled jobs stay listed until they expire, so their
        # errors can be read and their files retried.
        with self._lock:
            self._prune()
            keys = [key for key, job in self._jobs.items() if job.public['state'] == 'finished'
                    and (ssh is None or job.public['ssh'] == ssh)]
            for key in keys:
                del self._jobs[key]
            return len(keys)

    def shutdown(self):
        with self._lock:
            self._closing = True
            jobs = list(self._jobs.values())
        for job in jobs:
            try:
                self.cancel(job.public['id'])
            except ValueError:
                pass
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
retry = MANAGER.retry
clear = MANAGER.clear
shutdown = MANAGER.shutdown
atexit.register(shutdown)
