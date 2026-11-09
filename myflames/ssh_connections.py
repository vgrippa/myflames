"""Saved SSH connections, endpoint-bound secrets and explicit host-key trust.

Only public projections leave this module. Secret references bind the record
ID, host, user, port, authentication method and key path before any store read.
Host keys stay in the workspace directory, never in the user's known_hosts.
Scan tokens retain exact captured lines in memory, expire and are single-use.
"""
import hashlib
import hmac
import os
import posixpath
import re
import secrets
import stat
import subprocess
import tempfile
import threading
import time

from . import secret_store, sftp, ssh_tunnel
from .connector import terminal_text

KIND = 'ssh_connection'
_FIELDS = ('name', 'host', 'port', 'user', 'auth', 'key_path', 'remote_dir', 'local_dir')
_REF = re.compile(r'sshconn\.([0-9a-f]{32})\.([0-9a-f]{24})\.([0-9a-f]{16})\Z')
_LOCK = threading.RLock()
_TOKENS = {}
TOKEN_SECONDS = 300
MAX_TOKENS = 256
MAX_KEY_BYTES = 65536


def identifier(value):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{32}', value) is None:
        raise ValueError('The SSH connection ID must be a 32-character hexadecimal string.')
    return value


def _text(value, field, limit=4096):
    if (not isinstance(value, str) or len(value) > limit
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError('%s must be text without control characters, at most %d characters.' % (field, limit))
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise ValueError('%s must contain valid Unicode characters.' % field) from None
    return value


def validate(value):
    if not isinstance(value, dict) or set(value) - set(_FIELDS + ('id',)):
        raise ValueError('An SSH connection must be an object containing only connection fields.')
    if 'id' in value:
        identifier(value['id'])
    name = _text(value.get('name'), 'Name', 80).strip()
    if not name:
        raise ValueError('Enter an SSH connection name.')
    host = _text(value.get('host'), 'Host')
    user = _text(value.get('user', ''), 'User')
    port = value.get('port')
    if port is not None and type(port) is not int:
        raise ValueError('SSH port must be an integer or null.')
    if port is not None and not 1 <= port <= 65535:
        raise ValueError('SSH port must be between 1 and 65535.')
    fields = ssh_tunnel.ssh_fields({'ssh_host': host, 'ssh_user': user, 'ssh_port': port})
    if not fields:
        raise ValueError('Enter an SSH host name or alias.')
    auth = value.get('auth')
    if auth not in ('agent', 'key', 'password'):
        raise ValueError('Choose agent, key or password for SSH authentication.')
    key = _text(value.get('key_path', ''), 'Key file')
    if auth == 'key':
        sftp.quoted_path(key)
        if not os.path.isfile(key):
            raise ValueError('The SSH key file must be an existing regular file.')
    elif key:
        raise ValueError('A key file is only used with key authentication.')
    remote = _text(value.get('remote_dir', ''), 'Remote folder')
    local = _text(value.get('local_dir', ''), 'Local folder')
    if remote and not posixpath.isabs(remote):
        raise ValueError('The remote folder must be an absolute POSIX path.')
    if local and not os.path.isabs(local):
        raise ValueError('The local folder must be an absolute path.')
    return dict(name=name, host=fields['ssh_host'], user=fields.get('ssh_user', ''),
                port=fields.get('ssh_port'), auth=auth, key_path=key, remote_dir=remote, local_dir=local)


def _fingerprint(record):
    values = (record['host'], record['user'], record['port'] or 0, record['auth'], record['key_path'])
    return hashlib.sha256('\x00'.join(str(v) for v in values).encode('utf-8')).hexdigest()[:24]


def _bound(record):
    ref = record.get('secret_ref')
    match = _REF.fullmatch(ref) if isinstance(ref, str) else None
    return bool(match and hmac.compare_digest(match.group(1), record['id'])
                and hmac.compare_digest(match.group(2), _fingerprint(record)))


def _public(record):
    out = {key: record[key] for key in _FIELDS}
    out.update(id=record['id'], updated=record['updated_at'], has_secret=bool(record.get('secret_ref')))
    return out


def known_hosts_file(store):
    directory = os.path.dirname(os.path.abspath(str(store.path)))
    path = os.path.join(directory, 'ssh_known_hosts')
    sftp.quoted_path(path)
    secret_store._ensure_private_directory(directory)
    fd, _ = secret_store._open_private_file(path, os.O_RDWR, create=True)
    os.close(fd)
    return path


def get(store, key):
    record = store.get(KIND, identifier(key))
    if record is None:
        raise ValueError('No SSH connection has that ID.')
    record['_known_hosts'] = known_hosts_file(store)
    return record


def list_connections(store):
    with _LOCK:
        records = sorted(store.list(KIND), key=lambda row: (row['name'].casefold(), row['id']))
        return {'connections': [_public(row) for row in records], 'known_hosts_file': known_hosts_file(store)}


def _delete_secret(record):
    if record and record.get('secret_ref'):
        if not _bound(record):
            raise ValueError('The saved SSH secret belongs to different connection details.')
        try:
            secret_store.delete_secret(record['secret_ref'], record.get('secret_backend'))
        except Exception:
            raise ValueError('The saved SSH secret could not be removed from the credential store.') from None


def save(store, payload):
    if set(payload) - {'connection', 'secret', 'forget_secret'}:
        raise ValueError('The SSH save request contains an unknown field.')
    value = validate(payload.get('connection'))
    forget = payload.get('forget_secret', False)
    if type(forget) is not bool:
        raise ValueError('Forget secret must be a boolean.')
    secret = sftp.check_password(payload['secret']) if 'secret' in payload else None
    if secret is not None and (forget or value['auth'] == 'agent'):
        raise ValueError('A secret needs password or key authentication, with Forget secret turned off.')
    with _LOCK:
        key = payload['connection'].get('id') or secrets.token_hex(16)
        old = get(store, key) if 'id' in payload['connection'] else None
        if any(row['id'] != key and row['name'].casefold() == value['name'].casefold() for row in store.list(KIND)):
            raise ValueError('An SSH connection with this name already exists.')
        if old and old.get('secret_ref') and not _bound(old):
            raise ValueError('The saved SSH secret belongs to different connection details.')
        keep = old and not forget and secret is None and _fingerprint(old) == _fingerprint(value)
        if keep and old.get('secret_ref'):
            value.update(secret_ref=old['secret_ref'], secret_backend=old['secret_backend'])
        new = None
        if secret is not None:
            ref = 'sshconn.%s.%s.%s' % (key, _fingerprint(value), secrets.token_hex(8))
            try:
                backend = secret_store.save_secret(ref, secret)
            except Exception:
                raise ValueError('The SSH secret could not be saved in the credential store.') from None
            value.update(secret_ref=ref, secret_backend=backend)
            new = dict(value, id=key)
        try:
            # The generic writer accepts a kind in the existing records table;
            # public save() validates only database profiles and investigations.
            record = store._write(KIND, value, key)
        except BaseException:
            # The old record still points at the old secret, which is intact.
            if new:
                _delete_secret(new)
            raise
        # Retire the old secret only once the record no longer refers to it, so
        # a failed write never leaves a record whose secret is gone.
        if not keep and old and old.get('secret_ref') and old['secret_ref'] != value.get('secret_ref'):
            try:
                _delete_secret(old)
            except ValueError:
                pass  # An orphaned entry in the store is safer than a lost one.
    from . import sftp_api
    sftp_api.drop(key)
    return {'connection': _public(record)}


def delete(store, key):
    with _LOCK:
        record = get(store, key)
        if record.get('secret_ref') and not _bound(record):
            raise ValueError('The saved SSH secret belongs to different connection details.')
        # The record goes first: a store failure afterwards leaves an orphaned
        # secret, never a connection that says it has one and does not.
        store.delete(KIND, key)
        try:
            _delete_secret(record)
        except ValueError:
            from . import sftp_api
            sftp_api.drop(key)
            raise ValueError('The SSH connection was deleted, but its saved password or passphrase could not be '
                             'removed from the credential store.') from None
    from . import sftp_api
    sftp_api.drop(key)
    return {'deleted': True}


def session_spec(record):
    """Comparable transport settings, without secret values or references."""
    return {'spec': {'ssh_host': record['host'], 'ssh_user': record['user'], 'ssh_port': record['port']},
            'auth': record['auth'], 'key_path': record['key_path'], 'known_hosts': record['_known_hosts']}


def open_session(record_or_draft, secret=None):
    record = record_or_draft
    if secret is None and record.get('secret_ref'):
        if not _bound(record):
            raise ValueError('The saved SSH secret belongs to different connection details.')
        try:
            secret = secret_store.get_secret(record['secret_ref'], record.get('secret_backend'))
        except Exception:
            raise ValueError('The saved SSH secret could not be read from the credential store.') from None
        if secret is None:
            raise ValueError('The saved SSH secret is missing. Save it again or forget the saved secret.')
    return sftp.SftpSession(secret=secret, **session_spec(record))


def _target(store, payload):
    if 'connection' in payload:
        if 'id' in payload:
            raise ValueError('Choose a saved SSH connection or a draft, not both.')
        record = validate(payload['connection'])
        record['_known_hosts'] = known_hosts_file(store)
        return record
    return get(store, payload.get('id'))


def test(store, payload):
    if set(payload) - {'id', 'connection', 'secret'} or ('secret' in payload and 'connection' not in payload):
        raise ValueError('Only a draft SSH test accepts a secret.')
    record = _target(store, payload)
    secret = sftp.check_password(payload['secret']) if 'secret' in payload else None
    started = time.monotonic()
    with open_session(record, secret) as session:
        home = session.safe(session.realpath('.'))
        if not posixpath.isabs(home):
            raise sftp.SftpError('The SSH server returned a home folder that is not absolute.')
    return {'ok': True, 'home': home, 'elapsed_ms': max(0, int((time.monotonic() - started) * 1000))}


def _prune_tokens():
    now = time.monotonic()
    for key, record in list(_TOKENS.items()):
        if record['expires'] <= now:
            del _TOKENS[key]


def scan(store, payload):
    if set(payload) - {'id', 'connection'}:
        raise ValueError('The host key scan accepts only an SSH connection.')
    record = _target(store, payload)
    binary = sftp.find_ssh()
    with tempfile.TemporaryDirectory(prefix='myflames-hostkey-') as directory:
        os.chmod(directory, 0o700)
        path = os.path.join(directory, 'known_hosts')
        fd, _ = secret_store._open_private_file(path, os.O_RDWR, create=True)
        os.close(fd)
        argv = [binary, '-T', '-o', 'StrictHostKeyChecking=accept-new',
                '-o', 'UserKnownHostsFile=' + sftp.quoted_path(path), '-o', 'GlobalKnownHostsFile=/dev/null',
                '-o', 'HashKnownHosts=no', '-o', 'BatchMode=yes', '-o', 'UpdateHostKeys=no',
                '-o', 'PermitLocalCommand=no', '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
                '-o', 'ClearAllForwardings=yes', '-o', 'ForwardAgent=no', '-o', 'ForwardX11=no',
                '-o', 'ConnectTimeout=10', '-o', 'LogLevel=ERROR']
        for method in ('Pubkey', 'Password', 'KbdInteractive', 'GSSAPI', 'Hostbased'):
            argv += ['-o', method + 'Authentication=no']
        if record['port']:
            argv += ['-p', str(record['port'])]
        if record['user']:
            argv += ['-l', record['user']]
        argv += ['--', record['host'], 'true']
        env = ssh_tunnel.ssh_environment()
        try:
            probe = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, env=env, timeout=15)
            fd, _ = secret_store._open_private_file(path, os.O_RDONLY)
            with os.fdopen(fd, 'rb') as stream:
                raw = stream.read(MAX_KEY_BYTES + 1)
            lines = raw.decode('ascii').splitlines()
            if not lines:
                # Nothing was recorded: ssh never reached key exchange. Its
                # first error line says why (refused, unknown host, timeout).
                said = [line.strip() for line in probe.stderr.decode('utf-8', 'replace').replace('\r', '\n').splitlines()
                        if line.strip() and 'permission denied' not in line.lower()]
                raise ValueError('The SSH host key could not be read from %s%s' % (
                    record['host'], (': ' + terminal_text(said[0])[:300]) if said else '.'))
            if len(raw) > MAX_KEY_BYTES or len(lines) > 256:
                raise ValueError('No usable SSH host key was captured.')
            # Known-host lines contain only a host list, key type and base64 key.
            # Retain the exact line, but refuse comments, markers and controls.
            if any(re.fullmatch(r'[^\s#@]+ [A-Za-z0-9@._+-]+ [A-Za-z0-9+/]+={0,3}', line) is None for line in lines):
                raise ValueError('The SSH host key capture was malformed.')
            keygen = os.path.join(os.path.dirname(binary), 'ssh-keygen.exe' if os.name == 'nt' else 'ssh-keygen')
            result = subprocess.run([keygen, '-l', '-E', 'sha256', '-f', path], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=10)
        except subprocess.TimeoutExpired:
            raise ValueError('The SSH host key could not be read from %s: no answer in 15 seconds.' % record['host']) from None
        except (OSError, UnicodeError, subprocess.SubprocessError):
            raise ValueError('The SSH host key could not be captured or fingerprinted.') from None
        output = result.stdout.decode('utf-8', 'replace')
        matches = [re.fullmatch(r'(\d+) (SHA256:[A-Za-z0-9+/=]+) .* \(([^()\s]+)\)', line)
                   for line in output.splitlines()]
        if result.returncode or len(matches) != len(lines) or not all(matches):
            raise ValueError('The SSH host key fingerprint could not be read.')
        keys = [{'type': m.group(3), 'bits': int(m.group(1)), 'fingerprint': m.group(2)} for m in matches]
        _refuse_other_trusted_key(binary, keygen, record, lines, env)
    with _LOCK:
        _prune_tokens()
        if len(_TOKENS) >= MAX_TOKENS:
            raise ValueError('Too many pending host key scans. Try again in five minutes.')
        token = secrets.token_hex(16)
        _TOKENS[token] = dict(host=lines[0].split(' ', 1)[0], lines=lines, path=record['_known_hosts'],
                              expires=time.monotonic() + TOKEN_SECONDS)
    # The first field of a captured line is the name ssh checks, with its port
    # ("[db.example]:2222") or a HostKeyAlias.
    return {'host': lines[0].split(' ', 1)[0], 'keys': keys, 'token': token}


def _known_hosts_files(binary, record, env):
    """Exactly the known_hosts files a session for this connection consults
    (sftp.known_hosts_config: the user's files, the global ones, and the
    myflames file), so the scan and the session judge trust alike."""
    user, global_files = sftp.known_hosts_config(binary, session_spec(record)['spec'])
    return user + global_files + [record['_known_hosts']]


def _refuse_other_trusted_key(binary, keygen, record, lines, env):
    """A host already trusted with other keys is a changed host, not a first
    contact. ssh treats a key of a type it has not stored for the host as
    unknown (an impersonator offering RSA where ED25519 is known), and trusting
    it would add the impersonator's key beside the real one."""
    captured = set(tuple(line.split(' ')[1:3]) for line in lines)
    names = set(name for line in lines for name in line.split(' ', 1)[0].split(','))
    for path in _known_hosts_files(binary, record, env):
        if not os.path.isfile(path):
            continue
        for name in names:
            # A file that cannot be searched is not "not known": refuse to issue
            # a token rather than miss a key trusted there (exit 1: no match).
            try:
                found = subprocess.run([keygen, '-F', name, '-f', path], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, timeout=10)
            except (OSError, subprocess.SubprocessError):
                found = None
            if found is None or found.returncode not in (0, 1):
                raise ValueError('The trusted host keys in %s could not be checked, so %s is not offered for '
                                 'trust. Check that the file is readable.' % (path, name))
            where, known = None, set()
            for line in found.stdout.decode('utf-8', 'replace').splitlines():
                mark = re.match(r'# Host .* found: line (\d+)', line)
                if mark:
                    where = mark.group(1)
                elif line and not line.startswith('#'):
                    parts = line.split()
                    if parts and parts[0].startswith('@'):
                        parts = parts[1:]  # @cert-authority or @revoked marker
                    if len(parts) >= 3:
                        known.add((parts[1], parts[2]))
            if known and not known & captured:
                raise sftp.SftpHostKeyChanged(
                    '%s is already trusted with a different host key (%s, line %s). The server now offers another '
                    'key: verify the change with the host administrator before trusting it.' % (name, path, where or '?'))


def trust(store, token):
    identifier(token)
    path = known_hosts_file(store)
    with _LOCK:
        _prune_tokens()
        record = _TOKENS.get(token)
        if record is None or record['path'] != path:
            raise ValueError('The host key scan expired or was already used. Scan the host again.')
        fd, _ = secret_store._open_private_file(path, os.O_RDWR)
        with os.fdopen(fd, 'r+b') as stream:
            existing = stream.read()
            seen = set(existing.splitlines())
            added = []
            for line in record['lines']:
                encoded = line.encode('ascii')
                if encoded not in seen:
                    added.append(encoded)
                    seen.add(encoded)
            if added:
                stream.seek(0, os.SEEK_END)
                stream.write((b'\n' if existing and not existing.endswith(b'\n') else b'') + b'\n'.join(added) + b'\n')
                stream.flush()
                os.fsync(stream.fileno())
        del _TOKENS[token]
    return {'trusted': True, 'host': record['host']}


def keys():
    base = os.path.join(ssh_tunnel.ssh_home(), '.ssh')
    result = []
    try:
        with os.scandir(base) as entries:
            for entry in entries:
                if len(result) >= 500:
                    break
                if entry.name.endswith('.pub') or not entry.is_file(follow_symlinks=False):
                    continue
                public = entry.path + '.pub'
                try:
                    if entry.name.endswith('.pem') and not os.path.lexists(public):
                        # Cloud key pairs (an EC2 .pem) come without a .pub: read
                        # only the first line, the PEM header, never the key.
                        # 64 bytes, unbuffered: the key itself is never read.
                        fd = os.open(entry.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
                        try:
                            header = os.read(fd, 64).split(b'\n', 1)[0]
                        finally:
                            os.close(fd)
                        if re.fullmatch(rb'-----BEGIN [A-Z ]*PRIVATE KEY-----\r?', header):
                            sftp.quoted_path(entry.path)
                            result.append({'path': entry.path, 'type': 'PEM', 'comment': ''})
                        continue
                    if not stat.S_ISREG(os.lstat(public).st_mode):
                        continue
                    with open(public, 'rb') as stream:
                        data = stream.read(16385)
                    if len(data) > 16384:
                        continue
                    parts = data.decode('utf-8').strip().split(None, 2)
                    if len(parts) < 2 or not re.fullmatch(r'(?:ssh-|ecdsa-|sk-)[A-Za-z0-9@._+-]+', parts[0]):
                        continue
                    sftp.quoted_path(entry.path)
                    result.append({'path': entry.path, 'type': parts[0],
                                   'comment': terminal_text(parts[2]) if len(parts) > 2 else ''})
                except (OSError, UnicodeError, ValueError):
                    continue
    except OSError:
        pass
    return {'keys': sorted(result, key=lambda key: key['path'])}


def response(path, payload, store):
    if path == '/api/ssh/connections/list':
        return list_connections(store)
    if path == '/api/ssh/connections/save':
        return save(store, payload)
    if path == '/api/ssh/connections/delete':
        return delete(store, payload.get('id'))
    if path == '/api/ssh/connections/test':
        return test(store, payload)
    if path == '/api/ssh/hostkey/scan':
        return scan(store, payload)
    if path == '/api/ssh/hostkey/trust':
        return trust(store, payload.get('token'))
    if path == '/api/ssh/keys':
        return keys()
    raise ValueError('Unknown SSH connection operation.')
