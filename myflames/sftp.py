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
SftpAuthError; keys/agent still uses the Windows kill-on-close job. A frozen
POSIX build needs a system python3 to execute its askpass helper.
"""
import atexit
import collections
import os
import posixpath
import queue
import secrets
import shlex
import shutil
import signal
import stat as statmod
import struct
import subprocess
import sys
import tempfile
import threading
import time

from . import dbops, ssh_tunnel
from .connector import ConnectorError, terminal_text

INIT, VERSION, OPEN, CLOSE, READ, WRITE, LSTAT = 1, 2, 3, 4, 5, 6, 7
SETSTAT, OPENDIR, READDIR, REMOVE, MKDIR, RMDIR, REALPATH, STAT, RENAME = 9, 11, 12, 13, 14, 15, 16, 17, 18
STATUS, HANDLE, DATA, NAME, ATTRS = 101, 102, 103, 104, 105
CHUNK_SIZE = 256 * 1024
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


class SftpCancelled(SftpError):
    """The caller cancelled a transfer between chunks."""


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
    """A private executable wrapper and a one-use, exclusively created file."""

    def __init__(self, password, executable=None):
        self.directory = tempfile.mkdtemp(prefix='myflames-ssh-')
        self.helper = os.path.join(self.directory, 'askpass')
        self.secret_file = os.path.join(self.directory, secrets.token_hex(16))
        try:
            os.chmod(self.directory, 0o700)
            self._write(self.secret_file, password.encode('utf-8'), 0o600)
            # Delete before writing stdout, including on a broken output pipe.
            code = ("import os,sys\np=%r\n"
                    "fd=os.open(p,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))\n"
                    "os.unlink(p)\n"
                    "with os.fdopen(fd,'rb') as f: data=f.read(4097)\n"
                    "sys.stdout.buffer.write(data+b'\\n')\n") % self.secret_file
            # Quoted absolute interpreter: spaces in an installation path work.
            script = '#!/bin/sh\nexec %s -I -c %s\n' % (shlex.quote(executable or sys.executable), shlex.quote(code))
            self._write(self.helper, script.encode('utf-8'), 0o700)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _write(path, data, mode):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), mode)
        with os.fdopen(fd, 'wb') as stream:
            os.chmod(path, mode)
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


def _askpass_python():
    if os.name == 'nt':
        raise SftpAuthError('SSH password sign-in is unavailable on Windows in this build. Use SSH keys or an agent.')
    if not getattr(sys, 'frozen', False):
        return sys.executable
    # A packaged backend cannot interpret -I -c. Search only absolute folders,
    # like find_ssh; never execute a python3 from the current working directory.
    folders = list(ssh_tunnel._path_folders(os.environ.get('PATH', '')))
    folders += [folder for folder in ssh_tunnel._POSIX_FOLDERS if folder not in folders]
    for folder in folders:
        candidate = os.path.join(folder, 'python3')
        if ssh_tunnel._is_program(candidate):
            return candidate
    raise SftpAuthError('SSH password sign-in needs a system Python 3 for its private helper. '
                        'Install Python 3 or use SSH keys or an agent.')


class SftpSession(object):
    """One SSH subprocess. ``_process`` is the pipe-based test transport seam."""

    def __init__(self, spec, password=None, _process=None, packet_timeout=PACKET_TIMEOUT):
        self.spec = ssh_tunnel.ssh_fields(spec)
        if not self.spec:
            raise ValueError('An SSH host is required for file transfer.')
        self._password = check_password(password) if password is not None else None
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
            while packet.at < len(packet.data):
                packet.string(), packet.string()  # Bounded extension name/value pairs.
            if self._askpass:
                self._askpass.close()  # Authentication is finished.
                self._askpass = None
        except BaseException:
            self.close()
            raise

    def safe(self, text):
        return terminal_text(dbops.redact(text, self._password))

    def _spawn(self):
        interpreter = _askpass_python() if self._password is not None else None
        argv = [ssh_tunnel.find_ssh(), '-T', '-s', '-o', 'LogLevel=ERROR',
                '-o', 'StrictHostKeyChecking=yes', '-o', 'UpdateHostKeys=no', '-o', 'PermitLocalCommand=no',
                '-o', 'ControlMaster=no', '-o', 'ControlPath=none', '-o', 'ClearAllForwardings=yes',
                '-o', 'ForwardAgent=no', '-o', 'ForwardX11=no', '-o', 'ConnectTimeout=10',
                '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2']
        env = ssh_tunnel.ssh_environment()
        if self._password is None:
            argv += ['-o', 'BatchMode=yes']
        else:
            try:
                self._askpass = _Askpass(self._password, executable=interpreter)
            except Exception:
                raise SftpAuthError('The private SSH password helper could not be created.') from None
            env.update(SSH_ASKPASS=self._askpass.helper, SSH_ASKPASS_REQUIRE='force', DISPLAY='myflames:0')
            # Avoid consuming the single-use file for a key passphrase or a
            # keyboard-interactive challenge. This mode is password auth only.
            argv += ['-o', 'PreferredAuthentications=password', '-o', 'PubkeyAuthentication=no',
                     '-o', 'NumberOfPasswordPrompts=1']
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
            detail = self.safe(bytes(self._stderr).decode('utf-8', 'replace'))
        if self._askpass is not None:
            detail = detail.replace(self._askpass.directory, '[SSH credential helper]')
        target = ssh_tunnel.target_text(self.spec)
        if 'permission denied' in detail.lower() or 'authentication failed' in detail.lower():
            if self._password is None:
                return SftpAuthError('SSH sign-in failed for %s: the server did not accept your keys or agent. '
                                     'Save a password or fix your SSH keys.' % target)
            return SftpAuthError('SSH sign-in failed for %s: the server did not accept the saved password.' % target)
        return SftpError('SSH file transfer stopped.' + (' ' + detail[:400] if detail else ''))

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
                        name = entry_name(packet.text())
                        packet.string()  # longname is display data, never parsed.
                        attrs = packet.attrs()
                        if name != '.':
                            attrs['name'] = name
                            result.append(attrs)
                    packet.end()
            finally:
                self._finish_handle(handle)

    def mkdir(self, path):
        with self.lock:
            self._request(MKDIR, _string(remote_path(path)) + _u32(4) + _u32(0o700))

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
                        amount = min(CHUNK_SIZE, size - offset)
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

    def put(self, local_fd, remote, size, progress_cb, cancel_event, overwrite=False):
        """Create exclusively; an authorized replacement first removes the old
        regular file. v3 has no O_NOFOLLOW, so TRUNC after LSTAT would follow a
        symlink swapped in by another remote user. REMOVE plus EXCL refuses
        that race, but cannot restore the previous contents on interruption.
        Remove only a file we successfully opened on transfer failure.
        """
        if type(size) is not int or size < 0 or type(overwrite) is not bool:
            raise ValueError('The file size must be a non-negative integer and overwrite must be a boolean.')
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
                        amount = min(CHUNK_SIZE, size - offset)
                        chunk = _read_local(local_fd, amount)
                        if not chunk:
                            raise SftpError('The local file became shorter during upload.')
                        rid = self._send(WRITE, _string(handle) + struct.pack('>Q', offset) + _string(chunk))
                        pending.append((rid, len(chunk)))
                        offset += len(chunk)
                    rid, amount = pending.popleft()
                    self._response(rid, STATUS)
                    done += amount
                    progress_cb(done)
                self._cancel(cancel_event)
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
            raise SftpError('The local file could not accept the downloaded data.')
        view = view[written:]


def close_all():
    with _SESSIONS_LOCK:
        sessions = list(_SESSIONS)
    for session in sessions:
        session.close()


atexit.register(close_all)
