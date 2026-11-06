"""Bounded SFTP v3 over system OpenSSH, with no third-party dependencies.

Wire format: https://www.ietf.org/archive/id/draft-ietf-secsh-filexfer-02.txt.
One session serializes operations; transfers pipeline at most eight requests.
stdin/stdout carry only SFTP packets. ssh_watchdog cannot be reused because it
replaces both streams with DEVNULL. We instead own the process group, kill it
on close/error/timeout, and register exit cleanup. Unlike the tunnel watchdog,
this does not guarantee cleanup following SIGKILL of the Python parent.

Password mode uses a single-use askpass file, strict host-key verification,
and no password in argv or environment. OpenSSH 8.4+ is required for forced
askpass. Windows script-based askpass is unverified and fails closed with
SftpAuthError; keys/agent still uses the Windows kill-on-close job. The POSIX
askpass helper uses /bin/sh, including in the packaged app.
"""
import atexit
import collections
import os
import posixpath
import queue
import re
import secrets
import shlex
import shutil
import signal
import stat as statmod
import struct
import subprocess
import tempfile
import threading
import time

from . import dbops, ssh_tunnel
from .connector import ConnectorError, terminal_text

INIT, VERSION, OPEN, CLOSE, READ, WRITE, LSTAT = 1, 2, 3, 4, 5, 6, 7
EXTENDED, EXTENDED_REPLY = 200, 201
SETSTAT, FSETSTAT, OPENDIR, READDIR, REMOVE, MKDIR, RMDIR, REALPATH, STAT, RENAME = 9, 10, 11, 12, 13, 14, 15, 16, 17, 18
STATUS, HANDLE, DATA, NAME, ATTRS = 101, 102, 103, 104, 105
CHUNK_SIZE = 256 * 1024
# Every SFTP server must accept 32 KiB of data per READ or WRITE. A larger
# chunk is used only up to the limits a server states (limits@openssh.com):
# OpenSSH's sftp-server closes the session on any message over 256 KiB,
# headers included, so a 256 KiB WRITE is never sent as is.
SAFE_CHUNK = 32 * 1024
PIPELINE = 8
PACKET_TIMEOUT = 30.0
MAX_PACKET = 1024 * 1024
MAX_ENTRIES = 100000
_STATUS_NAMES = {1: 'End of file', 2: 'No such file', 3: 'Permission denied', 4: 'Failure',
                 5: 'Bad message', 6: 'No connection', 7: 'Connection lost', 8: 'Operation unsupported'}
_SESSIONS = set()
_SESSIONS_LOCK = threading.Lock()


class SftpError(ConnectorError):
    """Protocol/transport error. ``code`` is the SFTP status, when supplied."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class SftpAuthError(SftpError):
    """SSH could not authenticate, or cannot safely deliver a saved password."""

    reason = 'auth_failed'


class SftpHostKeyUnknown(SftpError):
    reason = 'host_key_unknown'


class SftpHostKeyChanged(SftpError):
    reason = 'host_key_changed'


class SftpSshMissing(SftpError):
    reason = 'ssh_missing'


class SftpCancelled(SftpError):
    """The caller cancelled a transfer between chunks."""


class SftpLocalError(SftpError):
    """A problem with the local file (it shrank, the disk refused a write).

    It fails one file, not the session: ``code`` is 0, never the None that
    marks a transport or protocol failure (sftp_jobs._session_failure).
    """

    def __init__(self, message):
        super().__init__(message, code=0)


def remote_path(path):
    if not isinstance(path, str) or not path or len(path) > 4096 or '\x00' in path:
        raise ValueError('Enter a remote path of 1 to 4096 characters without a null character.')
    return posixpath.normpath(path)


def entry_name(name):
    """A READDIR name is one component, never a server-selected path."""
    if not isinstance(name, str) or not name or name == '..' or '/' in name or '\x00' in name:
        raise SftpError('The SFTP server returned an unsafe directory entry name.')
    return name


def _u32(number):
    return struct.pack('>I', number)


def _string(value):
    raw = value.encode('utf-8') if isinstance(value, str) else value
    return _u32(len(raw)) + raw


class _Cursor(object):
    def __init__(self, data):
        self.data, self.at = data, 0

    def take(self, count):
        if count < 0 or count > len(self.data) - self.at:
            raise SftpError('The SFTP server returned an incomplete packet.')
        result = self.data[self.at:self.at + count]
        self.at += count
        return result

    def uint(self):
        return struct.unpack('>I', self.take(4))[0]

    def uint64(self):
        return struct.unpack('>Q', self.take(8))[0]

    def string(self):
        return self.take(self.uint())

    def text(self):
        try:
            return self.string().decode('utf-8')
        except UnicodeError:
            raise SftpError('The SFTP server returned a name that is not UTF-8.') from None

    def end(self):
        if self.at != len(self.data):
            raise SftpError('The SFTP server returned unexpected packet data.')

    def attrs(self):
        flags = self.uint()
        if flags & ~0x8000000f:
            raise SftpError('The SFTP server returned unknown file attribute flags.')
        size = self.uint64() if flags & 1 else None
        if flags & 2:
            self.uint(), self.uint()  # uid, gid are not part of our public view.
        mode = self.uint() if flags & 4 else None
        mtime = None
        if flags & 8:
            self.uint()
            mtime = self.uint()
        if flags & 0x80000000:
            count = self.uint()
            if count > len(self.data) // 8:
                raise SftpError('The SFTP server returned too many extended attributes.')
            for _ in range(count):
                self.string(), self.string()
        return {'size': size, 'mtime': mtime, 'is_dir': bool(mode is not None and statmod.S_ISDIR(mode)),
                'mode': mode}


class _Askpass(object):
    """A private executable wrapper and a one-use, exclusively created file.

    The helper answers only the ssh process myflames started: ssh runs askpass
    as its own child, while a ProxyJump or ProxyCommand ssh (a grandchild that
    inherits SSH_ASKPASS) would hand the secret to the bastion. ``bind(pid)``
    records that ssh's pid right after it starts; the helper waits up to 5 s
    for it and refuses any other parent. Absolute /bin tools: no PATH lookup.
    """

    def __init__(self, password):
        self.directory = tempfile.mkdtemp(prefix='myflames-ssh-')
        self.helper = os.path.join(self.directory, 'askpass')
        self.secret_file = os.path.join(self.directory, secrets.token_hex(16))
        self.pid_file = os.path.join(self.directory, 'parent')
        try:
            os.chmod(self.directory, 0o700)
            self._write(self.secret_file, (password + '\n').encode('utf-8'), 0o600)
            # Unlink before writing stdout, including on a broken output pipe.
            quoted, pid = shlex.quote(self.secret_file), shlex.quote(self.pid_file)
            script = ('#!/bin/sh\n'
                      'i=0\n'
                      'while [ ! -s %s ] && [ $i -lt 100 ]; do /bin/sleep 0.05; i=$((i+1)); done\n'
                      '[ -s %s ] && [ "$PPID" = "$(/bin/cat %s)" ] || exit 1\n'
                      'exec 3<%s || exit 1\n'
                      '/bin/rm -f -- %s\n'
                      '/bin/cat <&3\n') % (pid, pid, pid, quoted, quoted)
            self._write(self.helper, script.encode('utf-8'), 0o700)
        except BaseException:
            self.close()
            raise

    def bind(self, pid):
        """The pid of the ssh this helper may answer (written once, 0600)."""
        self._write(self.pid_file, str(int(pid)).encode('ascii'), 0o600)

    @staticmethod
    def _write(path, data, mode):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), mode)
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)

    def close(self):
        shutil.rmtree(self.directory, ignore_errors=True)


def check_password(password):
    if not isinstance(password, str) or not 1 <= len(password) <= 1024:
        raise ValueError('The SSH password must be a string of 1 to 1024 characters.')
    if any(c in password for c in ('\x00', '\r', '\n')):
        raise ValueError('The SSH password cannot contain a null character or a line break.')
    try:
        password.encode('utf-8')
    except UnicodeError:
        raise ValueError('The SSH password must contain valid Unicode characters.') from None
    return password


def quoted_path(path):
    # ssh expands %-tokens and ${VAR} in IdentityFile and known-hosts paths,
    # so a path with % or $ would name another file: refused, not escaped.
    if (not isinstance(path, str) or not os.path.isabs(path) or any(c in path for c in '"%$')
            or any(ord(c) < 32 or ord(c) == 127 for c in path)):
        raise ValueError('SSH file paths must be absolute and contain no quotes, %, $ or control characters.')
    return '"' + path + '"'


_KEYCHAIN_OPTION = {}
_DEFAULT_GLOBAL = ('/etc/ssh/ssh_known_hosts', '/etc/ssh/ssh_known_hosts2')


def _paths_of(value):
    """The paths of one ssh -G list. ssh prints them unquoted and separated by
    spaces, so a path may contain spaces (/Users/John Smith/.ssh/known_hosts,
    "/team /known_hosts"). A missing file trusts nothing, so the filesystem
    settles each boundary: cut after a name that is an existing file, or before
    a word that starts a new path when joining both names nothing that exists.
    Relative paths are resolved like ssh does, from this process's folder
    (ssh inherits it)."""
    words = value.strip().split(' ')
    paths, current = [], ''
    for word in words:
        if not current:
            current = word
            continue
        joined = current + ' ' + word
        starts = word.startswith(('/', '~')) or re.match(r'[A-Za-z]:[\\/]', word) is not None
        if os.path.isfile(os.path.expanduser(current)) or (starts and not os.path.exists(os.path.expanduser(joined))):
            paths.append(current)
            current = word
        else:
            current = joined
    if current:
        paths.append(current)
    return [os.path.abspath(os.path.expanduser(path)) for path in paths if path]


def known_hosts_config(binary, spec):
    """(user files, global files) ssh consults for this endpoint, from
    ``ssh -G``; the OpenSSH defaults when it cannot say. Sessions keep the
    user files and use the global list plus myflames' file; the host-key scan
    searches exactly those, so both judge trust from the same files."""
    argv = [binary, '-G']
    if spec.get('ssh_port'):
        argv += ['-p', str(spec['ssh_port'])]
    if spec.get('ssh_user'):
        argv += ['-l', spec['ssh_user']]
    found = {}
    try:
        out = subprocess.run(argv + ['--', spec['ssh_host']], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, env=ssh_tunnel.ssh_environment(), timeout=10)
        for line in out.stdout.decode('utf-8', 'replace').splitlines():
            key, _, value = line.partition(' ')
            if key in ('userknownhostsfile', 'globalknownhostsfile') and value.strip():
                found[key] = _paths_of(value)
    except (OSError, subprocess.SubprocessError):
        pass
    home = os.path.join(ssh_tunnel.ssh_home(), '.ssh')
    user = found.get('userknownhostsfile') or [os.path.join(home, 'known_hosts'), os.path.join(home, 'known_hosts2')]
    global_files = found.get('globalknownhostsfile') or (list(_DEFAULT_GLOBAL) if os.name == 'posix' else [])
    return user, global_files


def _knows_use_keychain(binary):
    """Whether this ssh is Apple's, which accepts UseKeychain (others reject it)."""
    if binary not in _KEYCHAIN_OPTION:
        try:
            known = subprocess.run([binary, '-o', 'UseKeychain=no', '-G', 'localhost'],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   env=ssh_tunnel.ssh_environment(), timeout=5).returncode == 0
        except (OSError, subprocess.SubprocessError):
            known = False
        _KEYCHAIN_OPTION[binary] = known
    return _KEYCHAIN_OPTION[binary]


def find_ssh():
    try:
        return ssh_tunnel.find_ssh()
    except ssh_tunnel.TunnelError:
        raise SftpSshMissing('ssh was not found. Install the OpenSSH client and try again.') from None


class SftpSession(object):
    """One SSH subprocess. ``_process`` is the pipe-based test transport seam."""

    def __init__(self, spec, auth='agent', key_path='', secret=None, known_hosts=None,
                 _process=None, packet_timeout=PACKET_TIMEOUT):
        self.spec = ssh_tunnel.ssh_fields(spec)
        if not self.spec:
            raise ValueError('An SSH host is required for file transfer.')
        if auth not in ('agent', 'key', 'password'):
            raise ValueError('Choose agent, key or password for SSH authentication.')
        if auth == 'agent' and secret is not None:
            raise ValueError('Agent authentication does not use a saved secret.')
        if auth == 'password' and secret is None:
            raise SftpAuthError('Enter an SSH password for password sign-in.')
        if auth == 'key':
            quoted_path(key_path)
        elif key_path:
            raise ValueError('A key file is only used with key authentication.')
        if known_hosts is not None:
            quoted_path(known_hosts)
        self.auth, self.key_path, self.known_hosts = auth, key_path, known_hosts
        self._password = check_password(secret) if secret is not None else None
        if self._password is not None and os.name == 'nt':
            raise SftpAuthError('SSH password and key passphrase sign-in are unavailable on Windows in this build. '
                                'Use SSH keys without a passphrase or an agent.')
        self.packet_timeout = packet_timeout
        self.lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._closed = threading.Event()
        self._incoming = queue.Queue(maxsize=PIPELINE + 2)
        self._outgoing = queue.Queue(maxsize=1)
        self._threads = []
        self._stderr = bytearray()
        self._stderr_lock = threading.Lock()
        self._pending, self._waiting = {}, set()
        self._next_id = 0
        self._askpass = None
        self._ready = False  # True once the server answered INIT (signed in).
        self.process = None
        self._owns_group = _process is None
        try:
            self.process = _process if _process is not None else self._spawn()
            with _SESSIONS_LOCK:
                _SESSIONS.add(self)
            for target, name in ((self._reader, 'sftp-read'), (self._writer, 'sftp-write'),
                                 (self._read_stderr, 'sftp-stderr')):
                thread = threading.Thread(target=target, name=name, daemon=True)
                self._threads.append(thread)
                thread.start()
            self._send_packet(bytes([INIT]) + _u32(3))
            packet = _Cursor(self._packet())
            if packet.take(1) != bytes([VERSION]) or packet.uint() != 3:
                raise SftpError('The server does not support SFTP version 3.')
            self._ready = True
            extensions = set()
            while packet.at < len(packet.data):
                name, _value = packet.string(), packet.string()  # Bounded name/value pairs.
                extensions.add(name)
            self.read_chunk = self.write_chunk = SAFE_CHUNK
            if b'limits@openssh.com' in extensions:
                self._limits()
            if self._askpass:
                self._askpass.close()  # Authentication is finished.
                self._askpass = None
        except BaseException:
            self.close()
            raise

    def _limits(self):
        """Chunk sizes from the server's stated limits, never above CHUNK_SIZE."""
        reply = self._request(EXTENDED, _string('limits@openssh.com'), EXTENDED_REPLY)
        _packet_length, read_length, write_length = reply.uint64(), reply.uint64(), reply.uint64()
        reply.uint64()  # Open handles: one per transfer here.
        reply.end()
        if read_length:
            self.read_chunk = min(CHUNK_SIZE, read_length)
        if write_length:
            self.write_chunk = min(CHUNK_SIZE, write_length)

    def safe(self, text):
        return terminal_text(dbops.redact(text, self._password))

    def _spawn(self):
        argv = [find_ssh(), '-T', '-s', '-o', 'LogLevel=ERROR',
                '-o', 'StrictHostKeyChecking=yes', '-o', 'UpdateHostKeys=no', '-o', 'PermitLocalCommand=no',
                '-o', 'ControlMaster=no', '-o', 'ControlPath=none', '-o', 'ClearAllForwardings=yes',
                '-o', 'ForwardAgent=no', '-o', 'ForwardX11=no', '-o', 'ConnectTimeout=10',
                '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2']
        if self.known_hosts is not None:
            # The user's own global files stay (ssh -G), with myflames' added;
            # a global path ssh could misread (a quote, %, $) is left out.
            paths = []
            for path in known_hosts_config(argv[0], self.spec)[1]:
                try:
                    paths.append(quoted_path(path))
                except ValueError:
                    pass
            argv += ['-o', 'GlobalKnownHostsFile=' + ' '.join(paths + [quoted_path(self.known_hosts)])]
        if self.auth == 'key':
            argv += ['-o', 'IdentityFile=' + quoted_path(self.key_path), '-o', 'IdentitiesOnly=yes',
                     '-o', 'PreferredAuthentications=publickey']
        env = ssh_tunnel.ssh_environment()
        if self._password is None:
            argv += ['-o', 'BatchMode=yes']
        else:
            try:
                self._askpass = _Askpass(self._password)
            except Exception:
                raise SftpAuthError('The private SSH password helper could not be created.') from None
            env.update(SSH_ASKPASS=self._askpass.helper, SSH_ASKPASS_REQUIRE='force', DISPLAY='myflames:0')
            argv += ['-o', 'BatchMode=no', '-o', 'NumberOfPasswordPrompts=1']
            if self.auth == 'key':
                # A saved passphrase stays in myflames' store: the user's
                # AddKeysToAgent or (Apple) UseKeychain must not copy the key
                # into the agent or the passphrase into the Keychain, where
                # Forget in myflames could not remove them.
                argv += ['-o', 'AddKeysToAgent=no']
                if _knows_use_keychain(argv[0]):
                    argv += ['-o', 'UseKeychain=no']
            if self.auth == 'password':
                argv += ['-o', 'PreferredAuthentications=password', '-o', 'PubkeyAuthentication=no']
        if self.spec.get('ssh_port'):
            argv += ['-p', str(self.spec['ssh_port'])]
        if self.spec.get('ssh_user'):
            argv += ['-l', self.spec['ssh_user']]
        argv += ['--', self.spec['ssh_host'], 'sftp']
        kwargs = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, bufsize=0)
        if os.name == 'posix':
            kwargs['start_new_session'] = True
        elif os.name == 'nt':
            from .win_job import CREATE_SUSPENDED
            kwargs['creationflags'] = (getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
                                       | getattr(subprocess, 'CREATE_NO_WINDOW', 0) | CREATE_SUSPENDED)
        try:
            process = subprocess.Popen(argv, **kwargs)
            self.process = process
            if self._askpass is not None:
                self._askpass.bind(process.pid)
            if os.name == 'nt':
                ssh_tunnel._join_job(process)
            return process
        except Exception as exc:
            raise SftpError(self.safe('Could not start SSH for file transfer: ' + str(exc))) from None

    def _enqueue(self, item):
        while not self._closed.is_set():
            try:
                self._incoming.put(item, timeout=0.1)
                return
            except queue.Full:
                pass

    def _read_exact(self, size):
        out = bytearray()
        while len(out) < size:
            part = self.process.stdout.read(size - len(out))
            if not part:
                raise EOFError()
            out.extend(part)
        return bytes(out)

    def _reader(self):
        try:
            while not self._closed.is_set():
                length = struct.unpack('>I', self._read_exact(4))[0]
                if not 1 <= length <= MAX_PACKET:
                    raise SftpError('The SFTP server returned an oversized or empty packet.')
                self._enqueue(self._read_exact(length))
        except (OSError, ValueError, EOFError, SftpError) as exc:
            self._enqueue(exc)

    def _writer(self):
        while not self._closed.is_set():
            try:
                data, done, result = self._outgoing.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                view = memoryview(data)
                while view:
                    written = self.process.stdin.write(view)
                    if not written:
                        raise OSError('SSH closed its input stream.')
                    view = view[written:]
                self.process.stdin.flush()
            except (OSError, ValueError) as exc:
                result.append(exc)
            finally:
                done.set()

    def _read_stderr(self):
        try:
            while not self._closed.is_set():
                data = self.process.stderr.read(4096)
                if not data:
                    break
                with self._stderr_lock:
                    self._stderr.extend(data)
                    del self._stderr[:-16384]
        except (OSError, ValueError):
            pass

    def _failure(self):
        # EOF on stdout can precede the stderr drainer by a scheduling slice.
        if len(self._threads) > 2:
            self._threads[2].join(0.1)
        with self._stderr_lock:
            raw = bytes(self._stderr).decode('utf-8', 'replace').replace('\r\n', '\n').replace('\r', '\n')
            detail = self.safe(raw)
        if self._askpass is not None:
            detail = detail.replace(self._askpass.directory, '[SSH credential helper]')
        target = ssh_tunnel.target_text(self.spec)
        lowered = detail.lower()
        if 'remote host identification has changed' in lowered or re.search(r'host key for .* has changed', lowered):
            offending = re.search(r'offending \S+ key in (.+?):(\d+)', detail, re.IGNORECASE)
            where = (' The trusted key is in %s, line %s: remove that line only after the administrator confirms '
                     'the new key, then connect again to trust it.' % (offending.group(1), offending.group(2))
                     if offending else '')
            return SftpHostKeyChanged('%s has a different host key from the trusted one. '
                                      'Verify the change with the host administrator.%s' % (target, where))
        if re.search(r'no .* host key is known for', lowered) or 'host key verification failed' in lowered:
            return SftpHostKeyUnknown('%s is not a trusted host yet.' % target)
        if 'permission denied' in lowered or 'authentication failed' in lowered:
            method = {'agent': 'your keys or agent', 'key': 'the selected key or its passphrase',
                      'password': 'the saved password'}[self.auth]
            return SftpAuthError('SSH sign-in failed for %s: the server did not accept %s.' % (target, method))
        if detail:
            return SftpError('SSH file transfer stopped. ' + detail[:400])
        if self._ready:
            return SftpError('The SSH connection to %s was lost.' % target)
        # ssh at LogLevel=ERROR says nothing when the server drops the
        # connection before sign-in, which OpenSSH 9.8+ does for a while after
        # failed sign-ins from the same address (PerSourcePenalties).
        return SftpError('%s closed the connection before sign-in. After failed sign-ins a server may refuse '
                         'connections from this computer for a short time: wait a moment and try again.' % target)

    def _timeout(self):
        self.close()
        raise SftpError('SSH file transfer timed out waiting for an SFTP packet; SSH was stopped.')

    def _send_packet(self, packet):
        if self._closed.is_set():
            raise SftpError('The SFTP session is closed.')
        done, result = threading.Event(), []
        self._outgoing.put((_u32(len(packet)) + packet, done, result))
        if not done.wait(self.packet_timeout):
            self._timeout()
        if result:
            error = self._failure()
            self.close()
            raise error

    def _packet(self, timeout=None):
        try:
            packet = self._incoming.get(timeout=self.packet_timeout if timeout is None else max(0, timeout))
        except queue.Empty:
            self._timeout()
        if isinstance(packet, BaseException):
            error = packet if isinstance(packet, SftpError) else self._failure()
            self.close()
            raise error
        return packet

    def _send(self, kind, payload=b''):
        self._next_id = self._next_id % 0xffffffff + 1
        rid = self._next_id
        self._waiting.add(rid)
        self._send_packet(bytes([kind]) + _u32(rid) + payload)
        return rid

    def _response(self, rid, expected):
        deadline = time.monotonic() + self.packet_timeout
        while rid not in self._pending:
            packet = _Cursor(self._packet(deadline - time.monotonic()))
            kind, received = packet.take(1)[0], packet.uint()
            if received not in self._waiting or received in self._pending:
                self.close()
                raise SftpError('The SFTP server returned an unknown or duplicate request ID.')
            self._pending[received] = (kind, packet)
        kind, packet = self._pending.pop(rid)
        self._waiting.remove(rid)
        if kind == STATUS:
            code = packet.uint()
            message = self.safe(packet.string().decode('utf-8', 'replace'))
            packet.string()  # language tag
            packet.end()
            if code:
                raise SftpError('%s: %s' % (_STATUS_NAMES.get(code, 'SFTP error %d' % code), message), code)
            if expected != STATUS:
                raise SftpError('The SFTP server returned success without the requested data.')
        elif kind != expected:
            raise SftpError('The SFTP server returned an unexpected reply type.')
        return packet

    def _request(self, kind, payload, expected=STATUS):
        return self._response(self._send(kind, payload), expected)

    def _handle(self, kind, payload):
        packet = self._request(kind, payload, HANDLE)
        handle = packet.string()
        packet.end()
        if len(handle) > 256 or not handle:
            raise SftpError('The SFTP server returned an invalid file handle.')
        return handle

    def _finish_handle(self, handle):
        # Drain outstanding pipeline replies before CLOSE. Cancellation must
        # not leave replies to poison the next operation on a cached session.
        if self._closed.is_set():
            return
        for rid in list(self._waiting):
            try:
                self._response(rid, self._pending.get(rid, (STATUS,))[0])
            except SftpError:
                if self._closed.is_set():
                    return
        self._request(CLOSE, _string(handle))

    def stat(self, path, follow_symlinks=True):
        with self.lock:
            packet = self._request(STAT if follow_symlinks else LSTAT, _string(remote_path(path)), ATTRS)
            attrs = packet.attrs()
            packet.end()
            return attrs

    def lstat(self, path):
        return self.stat(path, follow_symlinks=False)

    def realpath(self, path):
        with self.lock:
            packet = self._request(REALPATH, _string(remote_path(path)), NAME)
            if packet.uint() != 1:
                raise SftpError('The SFTP server returned more than one canonical path.')
            result = remote_path(packet.text())
            packet.string(), packet.attrs()
            packet.end()
            return result

    def listdir(self, path):
        with self.lock:
            handle = self._handle(OPENDIR, _string(remote_path(path)))
            result = []
            seen = 0
            try:
                while True:
                    try:
                        packet = self._request(READDIR, _string(handle), NAME)
                    except SftpError as exc:
                        if exc.code == 1:
                            return result
                        raise
                    count = packet.uint()
                    if not count or count > MAX_ENTRIES - seen or count > len(packet.data) // 12:
                        raise SftpError('The SFTP directory listing is empty or too large.')
                    seen += count
                    for _ in range(count):
                        name = packet.text()
                        packet.string()  # longname is display data, never parsed.
                        attrs = packet.attrs()
                        # OpenSSH lists "." and ".." in every folder: skip them,
                        # and refuse any other name that could act as a path.
                        if name not in ('.', '..'):
                            attrs['name'] = entry_name(name)
                            result.append(attrs)
                    packet.end()
            finally:
                self._finish_handle(handle)

    def mkdir(self, path):
        with self.lock:
            # No permissions attribute: the server applies its umask, as an
            # interactive mkdir or FileZilla's New folder would.
            self._request(MKDIR, _string(remote_path(path)) + _u32(0))

    def rmdir(self, path):
        with self.lock:
            self._request(RMDIR, _string(remote_path(path)))

    def remove(self, path):
        with self.lock:
            self._request(REMOVE, _string(remote_path(path)))

    def rename(self, source, target):
        with self.lock:
            self._request(RENAME, _string(remote_path(source)) + _string(remote_path(target)))

    def setstat(self, path, mode):
        if type(mode) is not int or not 0 <= mode <= 0o777:
            raise ValueError('File permissions must be an integer between 0 and 0777.')
        with self.lock:
            self._request(SETSTAT, _string(remote_path(path)) + _u32(4) + _u32(mode))

    @staticmethod
    def _cancel(event):
        if event.is_set():
            raise SftpCancelled('File transfer cancelled.')

    def get(self, remote, local_fd, size, progress_cb, cancel_event):
        """Read exactly *size* bytes, repairing short DATA replies without gaps."""
        if type(size) is not int or size < 0:
            raise ValueError('The file size must be a non-negative integer.')
        with self.lock:
            self._cancel(cancel_event)
            handle = self._handle(OPEN, _string(remote_path(remote)) + _u32(1) + _u32(0))
            pending, offset, done = collections.deque(), 0, 0
            try:
                while offset < size or pending:
                    self._cancel(cancel_event)
                    while offset < size and len(pending) < PIPELINE:
                        self._cancel(cancel_event)
                        amount = min(self.read_chunk, size - offset)
                        rid = self._send(READ, _string(handle) + struct.pack('>QI', offset, amount))
                        pending.append((rid, offset, amount))
                        offset += amount
                    rid, at, amount = pending.popleft()
                    packet = self._response(rid, DATA)
                    chunk = packet.string()
                    packet.end()
                    if not chunk or len(chunk) > amount:
                        raise SftpError('The SFTP server returned an invalid file chunk.')
                    self._cancel(cancel_event)
                    _write_local(local_fd, chunk)
                    done += len(chunk)
                    progress_cb(done)
                    if len(chunk) < amount:
                        self._cancel(cancel_event)
                        rid = self._send(READ, _string(handle) + struct.pack('>QI', at + len(chunk), amount - len(chunk)))
                        pending.appendleft((rid, at + len(chunk), amount - len(chunk)))
                self._cancel(cancel_event)
                return done
            finally:
                self._finish_handle(handle)

    def put(self, local_fd, remote, size, progress_cb, cancel_event, overwrite=False, mode=None):
        """Create exclusively; an authorized replacement first removes the old
        regular file. v3 has no O_NOFOLLOW, so TRUNC after LSTAT would follow a
        symlink swapped in by another remote user. REMOVE plus EXCL refuses
        that race, but cannot restore the previous contents on interruption.
        Remove only a file we successfully opened on transfer failure.

        ``mode`` is set on the open handle (FSETSTAT) before CLOSE: a SETSTAT
        by path after CLOSE would follow a link swapped in meanwhile and
        chmod another file (a 0600 ~/.my.cnf made 0644).
        """
        if type(size) is not int or size < 0 or type(overwrite) is not bool:
            raise ValueError('The file size must be a non-negative integer and overwrite must be a boolean.')
        if mode is not None and (type(mode) is not int or not 0 <= mode <= 0o777):
            raise ValueError('File permissions must be an integer between 0 and 0777.')
        path = remote_path(remote)
        with self.lock:
            self._cancel(cancel_event)
            if overwrite:
                try:
                    previous = self.lstat(path)
                except SftpError as exc:
                    if exc.code != 2:
                        raise
                    previous = None
                if previous is not None:
                    if previous['mode'] is None or not statmod.S_ISREG(previous['mode']):
                        raise SftpError('The remote target must be a regular file, not a symbolic link or folder.')
                    self._cancel(cancel_event)
                    self.remove(path)
            self._cancel(cancel_event)
            flags = 2 | 8 | 32  # WRITE | CREAT | EXCL, including replacements.
            handle = self._handle(OPEN, _string(path) + _u32(flags) + _u32(4) + _u32(0o600))
            pending, offset, done = collections.deque(), 0, 0
            success = False
            try:
                while offset < size or pending:
                    self._cancel(cancel_event)
                    while offset < size and len(pending) < PIPELINE:
                        self._cancel(cancel_event)
                        amount = min(self.write_chunk, size - offset)
                        chunk = _read_local(local_fd, amount)
                        if not chunk:
                            raise SftpLocalError('The local file became shorter during upload.')
                        rid = self._send(WRITE, _string(handle) + struct.pack('>Q', offset) + _string(chunk))
                        pending.append((rid, len(chunk)))
                        offset += len(chunk)
                    rid, amount = pending.popleft()
                    self._response(rid, STATUS)
                    done += amount
                    progress_cb(done)
                self._cancel(cancel_event)
                if mode is not None:
                    self._request(FSETSTAT, _string(handle) + _u32(4) + _u32(mode))
                self._finish_handle(handle)
                success = True
                return done
            finally:
                if not success:
                    try:
                        self._finish_handle(handle)
                    finally:
                        if not self._closed.is_set():
                            try:
                                self.remove(path)
                            except SftpError:
                                raise SftpError('Upload stopped, but the partial remote file could not be removed.') from None

    def alive(self):
        return not self._closed.is_set() and self.process is not None and self.process.poll() is None

    def close(self):
        with self._close_lock:
            if self._closed.is_set():
                return
            self._closed.set()
            process = self.process
            if process is not None:
                try:
                    if self._owns_group and os.name == 'posix':
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except OSError:
                            pass
                    elif process.poll() is None:
                        process.kill()
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                for thread in self._threads:
                    if thread is not threading.current_thread():
                        thread.join(2)
                for stream in (process.stdin, process.stdout, process.stderr):
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
            if self._askpass is not None:
                self._askpass.close()
                self._askpass = None
            with _SESSIONS_LOCK:
                _SESSIONS.discard(self)

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


def _read_local(fd, count):
    return os.read(fd, count) if isinstance(fd, int) else fd.read(count)


def _write_local(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view) if isinstance(fd, int) else fd.write(view)
        if not written:
            raise SftpLocalError('The local file could not accept the downloaded data.')
        view = view[written:]


def close_all():
    with _SESSIONS_LOCK:
        sessions = list(_SESSIONS)
    for session in sessions:
        session.close()


atexit.register(close_all)
