"""Authenticated Files operations using saved SSH connections.

Browsing sessions are bounded and idle-expiring; transfers get private sessions.
Remote removal counts its complete lstat-based tree before the first deletion,
and never descends through links. No database passwords or SQL policy apply.
"""
import atexit
import collections
import contextlib
import os
import posixpath
import re
import stat as statmod
import subprocess
import sys
import threading
import time

from . import dbops, sftp, sftp_jobs, ssh_connections, ssh_tunnel

IDLE_SECONDS = 120.0
MAX_SESSIONS = 8
MAX_REMOVE_ENTRIES = 10000
MAX_REMOVE_DEPTH = 64
LOCAL_LIST_MAX_ENTRIES = 100000
_CACHE_LOCK = threading.RLock()
_SESSIONS = collections.OrderedDict()


def _cache_key(record):
    spec = ssh_connections.session_spec(record)
    return (record['id'], tuple(sorted(spec['spec'].items())), spec['auth'], spec['key_path'],
            spec['known_hosts'], record.get('secret_ref'), record.get('updated_at'))


def _close_entry(entry):
    if entry.get('timer') is not None:
        entry['timer'].cancel()
    entry['session'].close()


def drop(key):
    with _CACHE_LOCK:
        for cache_key, entry in list(_SESSIONS.items()):
            if cache_key[0] == key:
                del _SESSIONS[cache_key]
                _close_entry(entry)


def _expire(key, entry):
    with _CACHE_LOCK:
        if _SESSIONS.get(key) is entry and time.monotonic() - entry['idle'] >= IDLE_SECONDS:
            del _SESSIONS[key]
            _close_entry(entry)


@contextlib.contextmanager
def _session(store, key):
    # Fetch under the cache lock so an edit's drop cannot miss an older opener.
    with _CACHE_LOCK:
        record = ssh_connections.get(store, key)
        cache_key, now = _cache_key(record), time.monotonic()
        for old_key, old in list(_SESSIONS.items()):
            if (now - old['idle'] >= IDLE_SECONDS or not old['session'].alive()
                    or (old_key[0] == key and old_key != cache_key)):
                del _SESSIONS[old_key]
                _close_entry(old)
        entry = _SESSIONS.get(cache_key)
        if entry is None:
            if len(_SESSIONS) >= MAX_SESSIONS:
                _, old = _SESSIONS.popitem(last=False)
                _close_entry(old)
            entry = {'session': ssh_connections.open_session(record), 'idle': now, 'timer': None}
            _SESSIONS[cache_key] = entry
        if entry['timer'] is not None:
            entry['timer'].cancel()
        _SESSIONS.move_to_end(cache_key)
        try:
            yield entry['session']
        except BaseException:
            _SESSIONS.pop(cache_key, None)
            _close_entry(entry)
            raise
        else:
            entry['idle'] = time.monotonic()
            timer = threading.Timer(IDLE_SECONDS, _expire, (cache_key, entry))
            timer.daemon = True
            entry['timer'] = timer
            timer.start()


def _remote(path, mutation=False):
    ssh_connections._text(path, 'Remote path')
    if not path or not posixpath.isabs(path):
        raise ValueError('Enter an absolute remote path.')
    if mutation and posixpath.basename(path.rstrip('/')) in ('', '.', '..'):
        raise ValueError('Choose a remote file or folder name other than . or ...')
    result = sftp.remote_path(path)
    if mutation and result == '/':
        raise ValueError('The remote root folder cannot be changed.')
    return result


def _local(path, mutation=False):
    ssh_connections._text(path, 'Local path')
    if not path or not os.path.isabs(path):
        raise ValueError('Enter an absolute local path.')
    if mutation and os.path.basename(path.rstrip(os.sep)) in ('', '.', '..'):
        raise ValueError('Choose a local file or folder name other than . or ...')
    return path


def _kind(mode):
    if mode is None:
        return 'other'
    for predicate, name in ((statmod.S_ISDIR, 'dir'), (statmod.S_ISREG, 'file'), (statmod.S_ISLNK, 'link')):
        if predicate(mode):
            return name
    return 'other'


def _entry(name, attrs, is_dir=None):
    mode = attrs.get('mode')
    kind = _kind(mode)
    return dict(name=name, kind=kind, is_dir=kind == 'dir' if is_dir is None else bool(is_dir),
                size=attrs.get('size'), mtime=attrs.get('mtime'), mode=mode)


def api_listdir(store, ssh, path):
    if path != '':
        _remote(path)
    with _session(store, ssh) as session:
        home = _remote(session.realpath('.'))
        resolved = home if path == '' else _remote(session.realpath(path))
        entries, links = [], 0
        for attrs in session.listdir(resolved):
            name = sftp.entry_name(attrs['name'])
            if name == '.':
                continue
            entry = _entry(name, attrs)
            if entry['kind'] == 'link' and links < 200:
                links += 1
                try:
                    entry['is_dir'] = _kind(session.stat(posixpath.join(resolved, name)).get('mode')) == 'dir'
                except sftp.SftpError as exc:
                    if exc.code not in (2, 3, 4):
                        raise
            entries.append(entry)
        return dbops.redact_value({'path': resolved, 'home': home, 'entries': entries}, session._password)


def api_mkdir(store, ssh, path):
    path = _remote(path, mutation=True)
    with _session(store, ssh) as session:
        session.mkdir(path)
        return {'path': session.safe(path)}


def api_rename(store, ssh, source, target):
    source, target = _remote(source, True), _remote(target, True)
    with _session(store, ssh) as session:
        session.rename(source, target)
        return {'path': session.safe(target)}


def _paths(paths, label, check):
    if not isinstance(paths, list) or not paths or len(paths) > 10000:
        raise ValueError('%s must be a non-empty list of at most 10000 paths.' % label)
    return [check(path) for path in paths]


def _directory_chain(session, path, verified):
    # SFTP v3 has no dir-fd. Check every ancestor, including after the scan,
    # to refuse a directory replaced by a link before the destructive phase.
    # ``verified`` holds the ancestors already checked: the read-only phases
    # share one set (one lstat per folder); every deletion passes a new one.
    parts = path.split('/')[1:-1]
    parent = ''
    for part in parts:
        parent += '/' + part
        if parent in verified:
            continue
        if _kind(session.lstat(parent).get('mode')) != 'dir':
            raise ValueError('A remote parent folder is no longer a directory. Nothing below it was removed.')
        verified.add(parent)


def api_remove(store, ssh, paths):
    paths = _paths(paths, 'Remote paths', lambda p: _remote(p, True))
    with _session(store, ssh) as session:
        plan, seen = [], set()

        def count(path, depth):
            if path in seen:
                return
            if depth > MAX_REMOVE_DEPTH:
                raise ValueError('The remote folders exceed the removal depth limit of 64. Nothing was removed.')
            seen.add(path)
            if len(seen) > MAX_REMOVE_ENTRIES:
                raise ValueError('The remote selection exceeds 10000 entries. Nothing was removed.')
            attrs = session.lstat(path)
            kind = _kind(attrs.get('mode'))
            if attrs.get('mode') is None:
                raise ValueError('The server did not report a file type. Nothing was removed.')
            if kind == 'dir':
                for item in session.listdir(path):
                    name = sftp.entry_name(item['name'])
                    if name != '.':
                        count(posixpath.join(path, name), depth + 1)
            plan.append((path, kind))

        verified = set()
        for path in paths:
            _directory_chain(session, path, verified)
            count(path, 0)
        # Preflight every planned type before deleting anything.
        verified = set()
        for path, kind in plan:
            _directory_chain(session, path, verified)
            if _kind(session.lstat(path).get('mode')) != kind:
                raise ValueError('The remote selection changed during scanning. Nothing was removed.')
        # Children come before their folder in the plan. Every ancestor is
        # checked again right before each deletion: a folder swapped for a
        # link between two deletions must stop the removal (a shared check
        # let /chosen/b resolve into the link's target).
        for path, kind in plan:
            _directory_chain(session, path, set())
            if _kind(session.lstat(path).get('mode')) != kind:
                raise ValueError('The remote selection changed. Removal stopped.')
            if kind == 'dir':
                session.rmdir(path)
            else:
                session.remove(path)
    return {'removed': len(plan)}


def _local_realpath(path):
    """realpath() under the transfers' rule: a link is followed only when root
    or this user owns it (sftp_jobs._trusted_realpath). The listing answers
    the resolved path and transfers start from it, so the listing must not
    resolve a link the transfer would refuse. Windows has no owner rule here
    (its transfers fail closed)."""
    if not hasattr(os, 'getuid'):
        return os.path.realpath(path)
    return sftp_jobs._trusted_realpath(path)


def api_local_list(path):
    home = _local_realpath(ssh_tunnel.ssh_home())
    resolved = home if path == '' else _local_realpath(_local(path))
    if not os.path.isdir(resolved):
        raise ValueError('There is no local folder at this path.')
    entries, links = [], 0
    try:
        with os.scandir(resolved) as scan:
            for entry in scan:
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                item = _entry(entry.name, {'mode': info.st_mode, 'size': info.st_size, 'mtime': int(info.st_mtime)})
                if item['kind'] == 'link' and links < 200:
                    links += 1
                    try:
                        item['is_dir'] = statmod.S_ISDIR(os.stat(entry.path).st_mode)
                    except OSError:
                        pass
                entries.append(item)
                if len(entries) >= LOCAL_LIST_MAX_ENTRIES:
                    break
    except OSError:
        raise ValueError('This local folder cannot be read.') from None
    return {'path': resolved, 'home': home, 'entries': entries, 'transfers': sftp_jobs.local_transfers_available()}


def _local_target(path):
    path = _local(path, True)
    # Resolve the parent only: rename operates on a link itself, not its target.
    parent = _local_realpath(os.path.dirname(path))
    if not os.path.isdir(parent):
        raise ValueError('The local parent folder must exist.')
    return os.path.join(parent, os.path.basename(path))


def _absent(path):
    try:
        os.lstat(path)
    except FileNotFoundError:
        return
    raise ValueError('The local target already exists. Choose another name.')


def api_local_mkdir(path):
    target = _local_target(path)
    try:
        _absent(target)
        os.mkdir(target, 0o777)  # The user's umask decides, as in the Finder.
    except OSError:
        raise ValueError('The local folder could not be created.') from None
    return {'path': target}


def _rename_noreplace(source, target):
    """Rename that fails if the target exists, atomically, or not at all.

    POSIX rename() replaces a target created after a check, so the kernel's
    no-replace rename is used: renamex_np(RENAME_EXCL) on macOS and
    renameat2(RENAME_NOREPLACE) on Linux; Windows' rename never replaces.
    Without one, a regular file falls back to link() then unlink(), which also
    refuses an existing target; a folder is refused rather than risked.
    """
    if os.name == 'nt':
        os.rename(source, target)
        return
    import ctypes
    import ctypes.util
    import errno
    libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
    encode = os.fsencode
    if sys.platform == 'darwin' and hasattr(libc, 'renamex_np'):
        result = libc.renamex_np(encode(source), encode(target), ctypes.c_uint(0x00000004))  # RENAME_EXCL
    elif hasattr(libc, 'renameat2'):
        at_fdcwd = -100
        result = libc.renameat2(at_fdcwd, encode(source), at_fdcwd, encode(target), ctypes.c_uint(1))  # RENAME_NOREPLACE
    else:
        if not statmod.S_ISREG(os.lstat(source).st_mode):
            raise ValueError('Renaming a folder safely is not supported on this system.')
        os.link(source, target)
        os.unlink(source)
        return
    if result != 0:
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise FileExistsError(code, os.strerror(code), target)
        raise OSError(code, os.strerror(code), source)


def _reveal_argv(path):
    """The file manager command that shows *path* selected in its folder:
    Finder (open -R), File Explorer (/select,) or, on Linux, the folder opened
    with xdg-open. Absolute programs only, never found through PATH."""
    if sys.platform == 'darwin':
        return ['/usr/bin/open', '-R', path]
    if os.name == 'nt':
        root = os.environ.get('SystemRoot') or r'C:\Windows'
        return [os.path.join(root, 'explorer.exe'), '/select,' + path]
    for program in ('/usr/bin/xdg-open', '/usr/local/bin/xdg-open'):
        if os.path.isfile(program) and os.access(program, os.X_OK):
            return [program, os.path.dirname(path) or '/']
    raise ValueError('No file manager was found on this computer (xdg-open is missing).')


def _trim_separators(path, module=os.path):
    """A folder given with a trailing separator is that folder, not a child;
    a root (``/``, ``C:\\``, ``\\\\server\\share\\``) stays a root, never the
    drive-relative ``C:``."""
    drive, tail = module.splitdrive(path)
    stripped = tail.rstrip(module.sep + (module.altsep or ''))
    return drive + stripped if stripped else path


def api_local_reveal(path):
    """Reveal in Finder / Show in File Explorer: the item is shown selected in
    its folder. Nothing is opened or run; the path resolves under the same
    link rule as listings and must exist."""
    path = _local(path)
    trimmed = _trim_separators(path)
    resolved = os.path.join(_local_realpath(os.path.dirname(trimmed)), os.path.basename(trimmed))
    if not os.path.lexists(resolved):
        raise ValueError('There is nothing at this path on this computer any more.')
    try:
        subprocess.run(_reveal_argv(resolved), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        raise ValueError('The file manager could not be opened.') from None
    return {'revealed': resolved}


def api_local_rename(source, target):
    source, target = _local_target(source), _local_target(target)
    try:
        os.lstat(source)
        _absent(target)
        _rename_noreplace(source, target)
    except FileExistsError:
        raise ValueError('The local target already exists. Choose another name.') from None
    except OSError:
        raise ValueError('The local file or folder could not be renamed.') from None
    return {'path': target}


def _transfer(store, payload, direction):
    paths_key, dir_key = ('local_paths', 'remote_dir') if direction == 'upload' else ('remote_paths', 'local_dir')
    if set(payload) - {'ssh', paths_key, dir_key, 'on_exists'}:
        raise ValueError('The file transfer request contains an unknown field.')
    on_exists = payload.get('on_exists')
    if on_exists not in ('fail', 'skip', 'overwrite'):
        raise ValueError('Choose fail, skip or overwrite for existing files.')
    check = _local if direction == 'upload' else _remote
    paths = _paths(payload.get(paths_key), paths_key, lambda p: check(p, True))
    directory = (_remote if direction == 'upload' else _local)(payload.get(dir_key))
    record = ssh_connections.get(store, payload.get('ssh'))
    label = {'ssh': record['id'], 'name': record['name']}
    spec = ssh_connections.session_spec(record)

    def opener():
        # Re-read before a queued job starts: never send a stale secret, or
        # silently redirect the queued paths to an edited connection endpoint.
        current = ssh_connections.get(store, record['id'])
        if ssh_connections.session_spec(current) != spec:
            raise ValueError('The SSH connection changed. Start the transfer again.')
        return ssh_connections.open_session(current)

    start = sftp_jobs.start_upload if direction == 'upload' else sftp_jobs.start_download
    return start(opener, label, paths, directory, on_exists)


def api_start_upload(store, payload):
    return _transfer(store, payload, 'upload')


def api_start_download(store, payload):
    return _transfer(store, payload, 'download')


def _job_id(job_id):
    if not isinstance(job_id, str) or re.fullmatch(r'[0-9a-f]{32}', job_id) is None:
        raise ValueError('The file transfer job ID must be a 32-character hexadecimal string.')
    return job_id


def api_jobs(ssh=None):
    if ssh is not None:
        ssh_connections.identifier(ssh)
    return sftp_jobs.jobs(ssh=ssh)


def api_cancel(job_id):
    return sftp_jobs.cancel(_job_id(job_id))


def response(path, payload, store):
    if path == '/api/files/remote/list':
        return api_listdir(store, payload.get('ssh'), payload.get('path'))
    if path == '/api/files/remote/mkdir':
        return api_mkdir(store, payload.get('ssh'), payload.get('path'))
    if path == '/api/files/remote/rename':
        return api_rename(store, payload.get('ssh'), payload.get('from'), payload.get('to'))
    if path == '/api/files/remote/remove':
        return api_remove(store, payload.get('ssh'), payload.get('paths'))
    if path == '/api/files/local/list':
        return api_local_list(payload.get('path'))
    if path == '/api/files/local/mkdir':
        return api_local_mkdir(payload.get('path'))
    if path == '/api/files/local/rename':
        return api_local_rename(payload.get('from'), payload.get('to'))
    if path == '/api/files/local/reveal':
        return api_local_reveal(payload.get('path'))
    if path == '/api/files/upload':
        return {'job': api_start_upload(store, payload)}
    if path == '/api/files/download':
        return {'job': api_start_download(store, payload)}
    if path == '/api/files/jobs':
        return {'jobs': api_jobs(payload.get('ssh'))}
    if path == '/api/files/cancel':
        return {'job': api_cancel(payload.get('job_id'))}
    if path == '/api/files/retry':
        return {'job': sftp_jobs.retry(_job_id(payload.get('job_id')))}
    if path == '/api/files/clear':
        ssh = payload.get('ssh')
        if ssh is not None:
            ssh_connections.identifier(ssh)
        return {'cleared': sftp_jobs.clear(ssh=ssh)}
    raise ValueError('Unknown file transfer operation.')


def close_sessions():
    with _CACHE_LOCK:
        entries = list(_SESSIONS.values())
        _SESSIONS.clear()
        for entry in entries:
            _close_entry(entry)


atexit.register(close_sessions)
