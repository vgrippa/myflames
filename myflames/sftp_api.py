"""Validated file-transfer calls for a future authenticated HTTP POST layer.

No routes are installed here. Upload/download payloads contain ``connection``,
``local_paths``/``remote_paths``, ``remote_dir``/``local_dir`` and optional
boolean ``overwrite``. Browsing calls accept connection and path separately.

WorkspaceStore has only database-password references; it has no SSH reference
field. SSH passwords use secret_store, with endpoint-bound references kept in
this module's memory, as allowed by the backend contract. References therefore
last only for this backend lifetime. A persisted SSH-reference schema and a
migration are future integration work, not a reuse of DB password keys.
The HTTP layer must keep these calls behind the existing authenticated POST
checks and must not expose the reference registry, session objects or secrets.
"""
import atexit
import collections
import contextlib
import hashlib
import hmac
import os
import re
import secrets
import stat as statmod
import threading
import time

from . import dbops, secret_store, sftp_jobs, ssh_tunnel
from .sftp import SftpSession, check_password, remote_path

IDLE_SECONDS = 120.0
MAX_SESSIONS = 8
_NO_SSH = 'This connection has no SSH details. Add an SSH host to the saved connection to transfer files.'
_REF_PATTERN = re.compile(r'sshpw\.([0-9a-f]{24})\.([0-9a-f]{16})\Z')
_AUTH_LOCK = threading.RLock()
_AUTH_REFS = {}
_CACHE_LOCK = threading.RLock()
_SESSIONS = collections.OrderedDict()


def _connection(value):
    if not isinstance(value, dict):
        raise ValueError('Connection must be an object with SSH details.')
    for field in ('ssh_host', 'ssh_user'):
        if field in value and not isinstance(value[field], str):
            raise ValueError('%s must be a string.' % field)
    if 'ssh_port' in value:
        port = value['ssh_port']
        if type(port) is not int and not isinstance(port, str):
            raise ValueError('SSH port must be an integer or a string of digits.')
        if isinstance(port, str) and port:
            digits = port.strip()
            if not digits or not digits.isascii() or not digits.isdigit():
                raise ValueError('SSH port must contain only digits.')
    if not value.get('ssh_host', '').strip():
        raise ValueError(_NO_SSH)
    fields = ssh_tunnel.ssh_fields({name: value[name] for name in ssh_tunnel.SSH_FIELDS if name in value})
    if not fields:
        raise ValueError(_NO_SSH)
    return fields


def _endpoint(spec):
    # Keep omitted port distinct from 22: ~/.ssh/config may select another port.
    # Host aliases and user names retain case, exactly as ssh_fields validates.
    return (spec['ssh_host'], spec.get('ssh_user', ''), spec.get('ssh_port', 0))


def _fingerprint(spec):
    # Match WorkspaceStore's normalized, NUL-delimited SHA-256 style, with a
    # separate namespace and only SSH endpoint fields. Never a DB secret key.
    return hashlib.sha256('\x00'.join(str(v) for v in _endpoint(spec)).encode('utf-8')).hexdigest()[:24]


def _matches(reference, spec):
    match = _REF_PATTERN.fullmatch(reference) if isinstance(reference, str) else None
    return bool(match and hmac.compare_digest(match.group(1), _fingerprint(spec)))


def _password_for(spec):
    with _AUTH_LOCK:
        record = _AUTH_REFS.get(_endpoint(spec))
        if record is None:
            return None
        key, backend = record
        if not _matches(key, spec):
            raise ValueError('The saved SSH password belongs to different SSH details. Save it again for this endpoint.')
        try:
            password = secret_store.get_secret(key, backend)
        except Exception:
            raise ValueError('The saved SSH password could not be read from the credential store.') from None
        if password is None:
            raise ValueError('The saved SSH password is missing. Save it again or clear saved SSH authentication.')
        return check_password(password)


def open_session(spec):
    """A private session for a worker; credentials are released only after binding."""
    fields = _connection(spec)
    return SftpSession(fields, password=_password_for(fields))


def _close_entry(entry):
    if entry.get('timer') is not None:
        entry['timer'].cancel()
    entry['session'].close()


def _drop(spec):
    with _CACHE_LOCK:
        entry = _SESSIONS.pop(_endpoint(spec), None)
        if entry is not None:
            _close_entry(entry)


def _expire(key, entry):
    with _CACHE_LOCK:
        if _SESSIONS.get(key) is entry and time.monotonic() - entry['idle'] >= IDLE_SECONDS:
            del _SESSIONS[key]
            _close_entry(entry)


@contextlib.contextmanager
def _session(spec):
    # Serialize small browsing operations, including construction and secret
    # invalidation. Transfers own separate sessions and never take this lock.
    with _CACHE_LOCK:
        key, now = _endpoint(spec), time.monotonic()
        for old_key, old in list(_SESSIONS.items()):
            if now - old['idle'] >= IDLE_SECONDS or not old['session'].alive():
                del _SESSIONS[old_key]
                _close_entry(old)
        entry = _SESSIONS.get(key)
        if entry is None:
            if len(_SESSIONS) >= MAX_SESSIONS:
                _, old = _SESSIONS.popitem(last=False)
                _close_entry(old)
            entry = {'session': open_session(spec), 'idle': now, 'timer': None}
            _SESSIONS[key] = entry
        if entry['timer'] is not None:
            entry['timer'].cancel()
        _SESSIONS.move_to_end(key)
        try:
            yield entry['session']
        except BaseException:
            _SESSIONS.pop(key, None)
            _close_entry(entry)
            raise
        else:
            entry['idle'] = time.monotonic()
            timer = threading.Timer(IDLE_SECONDS, _expire, (key, entry))
            timer.daemon = True
            entry['timer'] = timer
            timer.start()


def _path(value):
    return remote_path(value)


def api_listdir(connection_payload, path):
    spec, path = _connection(connection_payload), _path(path)
    with _session(spec) as session:
        return dbops.redact_value(session.listdir(path), session._password)


def api_realpath(connection_payload, path):
    spec, path = _connection(connection_payload), _path(path)
    with _session(spec) as session:
        return session.safe(session.realpath(path))


def api_mkdir(connection_payload, path):
    spec, path = _connection(connection_payload), _path(path)
    with _session(spec) as session:
        session.mkdir(path)
    return {'created': True}


def api_remove(connection_payload, path):
    spec, path = _connection(connection_payload), _path(path)
    with _session(spec) as session:
        # An explicit directory action uses RMDIR; symlinks use REMOVE.
        if session.lstat(path)['is_dir']:
            session.rmdir(path)
        else:
            session.remove(path)
    return {'removed': True}


def _transfer(payload, direction):
    if not isinstance(payload, dict):
        raise ValueError('The file transfer request must be an object.')
    paths_key, dir_key = ('local_paths', 'remote_dir') if direction == 'upload' else ('remote_paths', 'local_dir')
    if set(payload) - {'connection', paths_key, dir_key, 'overwrite'}:
        raise ValueError('The file transfer request contains an unknown field.')
    spec = _connection(payload.get('connection'))
    paths = payload.get(paths_key)
    sftp_jobs._check_paths(paths, paths_key)
    directory = payload.get(dir_key)
    if not isinstance(directory, str) or not directory:
        raise ValueError('%s must be a non-empty string.' % dir_key)
    overwrite = payload.get('overwrite', False)
    if type(overwrite) is not bool:
        raise ValueError('Overwrite must be a boolean.')
    start = sftp_jobs.start_upload if direction == 'upload' else sftp_jobs.start_download
    return start(spec, list(paths), directory, overwrite)


def api_start_upload(payload):
    return _transfer(payload, 'upload')


def api_start_download(payload):
    return _transfer(payload, 'download')


def api_jobs():
    return sftp_jobs.jobs()


LOCAL_LIST_MAX_ENTRIES = 100000


def api_local_list(path):
    """List a folder on this computer for the local pane (no SSH involved).

    The path is resolved (``~`` and symlinks) so the pane shows where it
    actually is. Entries are ``lstat``-based: a symlink is reported as a
    file, never descended into. Unreadable entries are skipped; an
    unreadable folder is an error.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError('Path must be a non-empty string.')
    if '\x00' in path:
        raise ValueError('Path must not contain NUL bytes.')
    resolved = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(resolved):
        raise ValueError('There is no local folder at this path.')
    entries = []
    try:
        with os.scandir(resolved) as scan:
            for entry in scan:
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                entries.append({'name': entry.name, 'size': info.st_size,
                                'mtime': info.st_mtime,
                                'is_dir': entry.is_dir(follow_symlinks=False),
                                'mode': statmod.S_IMODE(info.st_mode)})
                if len(entries) >= LOCAL_LIST_MAX_ENTRIES:
                    break
    except OSError:
        raise ValueError('This local folder cannot be read.') from None
    return {'path': resolved, 'entries': entries}


def api_cancel(job_id):
    if not isinstance(job_id, str) or re.fullmatch(r'[0-9a-f]{32}', job_id) is None:
        raise ValueError('The file transfer job ID must be a 32-character hexadecimal string.')
    return sftp_jobs.cancel(job_id)


def api_ssh_auth_status(connection):
    spec = _connection(connection)
    return {'has_password': _password_for(spec) is not None}


def api_ssh_auth_save(connection, password):
    spec, password = _connection(connection), check_password(password)
    key = 'sshpw.' + _fingerprint(spec) + '.' + secrets.token_hex(8)
    with _AUTH_LOCK:
        try:
            backend = secret_store.save_secret(key, password)
        except Exception:
            raise ValueError('The SSH password could not be saved in the credential store.') from None
        old = _AUTH_REFS.get(_endpoint(spec))
        _AUTH_REFS[_endpoint(spec)] = (key, backend)
        if old is not None and _matches(old[0], spec):
            try:
                secret_store.delete_secret(*old)
            except Exception:
                # The new reference is authoritative. Never restore the old
                # reference after its replacement has been saved successfully.
                pass
    _drop(spec)
    return {'saved': True}


def api_ssh_auth_clear(connection):
    spec = _connection(connection)
    try:
        with _AUTH_LOCK:
            old = _AUTH_REFS.get(_endpoint(spec))
            if old is not None:
                if not _matches(old[0], spec):
                    raise ValueError('The saved SSH password belongs to different SSH details.')
                try:
                    secret_store.delete_secret(*old)
                except Exception:
                    raise ValueError('The SSH password could not be removed from the credential store.') from None
                del _AUTH_REFS[_endpoint(spec)]
    finally:
        _drop(spec)
    return {'cleared': True}


def close_sessions():
    with _CACHE_LOCK:
        entries = list(_SESSIONS.values())
        _SESSIONS.clear()
        for entry in entries:
            _close_entry(entry)


atexit.register(close_sessions)
