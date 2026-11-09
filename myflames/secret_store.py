"""Saved connection passwords, kept out of the workspace database.

Stdlib only. The OS credential store is preferred:

- macOS: the login Keychain through ``/usr/bin/security``. The secret travels on
  the stdin of ``security -i`` (hex encoded), never on argv.
- Linux: the Secret Service through ``secret-tool``, secret on stdin.
- Windows: Credential Manager through ``CredWriteW``/``CredReadW``/``CredDeleteW``.

When none of those works (or ``MYFLAMES_SECRET_BACKEND=vault``), passwords go to
an encrypted file, ``~/.myflames/credentials.vault``. Its 32-byte master key is
``~/.myflames/vault.key`` (mode 0600), or is derived from
``MYFLAMES_VAULT_PASSPHRASE`` with PBKDF2-SHA256. Each entry is encrypted with
an HMAC-SHA256 keystream and authenticated with HMAC-SHA256 (encrypt-then-MAC).
The vault protects copies of the vault or workspace database made without the
key file (backups, synced folders). It does not protect against malware or
anyone running as your user account, who can read the key file too.

``MYFLAMES_SECRET_BACKEND=keychain|vault|none`` overrides auto-detection;
``none`` disables remembering passwords.
"""
import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

SERVICE = 'myflames'
KEY_PATTERN = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}')  # used with fullmatch: no trailing newline
MAX_SECRET = 4096
MAC_COMMAND_LIMIT = 3800  # bytes per `security -i` command line, below its ~4 KB read size
PBKDF2_ITERATIONS = 600000
VAULT_FORMAT = 'myflames-vault-1'
ENTRY_VERSION = b'\x01'
_ENCODED_PREFIX = 'mf1:'

LABELS = {
    'macos-keychain': 'macOS Keychain',
    'secret-service': 'Secret Service',
    'windows-credential': 'Windows Credential Manager',
    'vault': 'encrypted file',
    'none': 'not saved',
}

_LOCK = threading.Lock()


class _VaultLock(object):
    """Thread lock plus an advisory file lock on ``vault.lock``, so a UI save
    and a CLI ``profiles forget`` in another process cannot interleave their
    read-modify-write of the vault."""

    def __init__(self, directory):
        self.path = Path(directory) / 'vault.lock'
        self.handle = None

    def __enter__(self):
        _LOCK.acquire()
        try:
            _ensure_private_directory(self.path.parent)
            fd, _created = _open_private_file(self.path, os.O_RDWR, create=True)
            try:
                self.handle = os.fdopen(fd, 'r+b')
            except BaseException:
                os.close(fd)
                raise
            if os.name == 'nt':
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        except BaseException:
            if self.handle is not None:
                self.handle.close()
                self.handle = None
            _LOCK.release()
            raise
        return self

    def __exit__(self, *exc):
        try:
            if os.name == 'nt':
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None
            _LOCK.release()
        return False


class SecretStoreError(ValueError):
    """A credential store failed. Messages never contain the secret."""


def _storage_stat(path, directory=False):
    """Check a storage entry without following links or changing its target."""
    try:
        info = os.lstat(str(path))
    except FileNotFoundError:
        return None
    _validate_storage_stat(path, info, directory)
    return info


def _validate_storage_stat(path, info, directory=False):
    if stat.S_ISLNK(info.st_mode):
        raise SecretStoreError('Refusing a symbolic link for private storage: %s' % path)
    if os.name == 'posix' and info.st_uid != os.geteuid():
        raise SecretStoreError('Private storage must be owned by the current user: %s' % path)
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode):
        raise SecretStoreError('Private storage has the wrong file type: %s' % path)


def _storage_fd(path, fd, directory=False):
    info = os.fstat(fd)
    _validate_storage_stat(path, info, directory)
    current = _storage_stat(path, directory)
    if current is None or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
        raise SecretStoreError('Private storage changed while it was being opened: %s' % path)
    return info


def _protect_private_path(path, mode, directory=False):
    if _storage_stat(path, directory) is None:
        return
    if sys.platform == 'win32':
        _windows_owner_only(path, directory=directory)
        return
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    if directory:
        flags |= getattr(os, 'O_DIRECTORY', 0)
    try:
        fd = os.open(str(path), flags)
    except FileNotFoundError:
        # SQLite can remove a sidecar between the check and the open.
        return
    try:
        info = _storage_fd(path, fd, directory)
        if os.name == 'posix' and stat.S_IMODE(info.st_mode) != mode:
            os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _ensure_private_directory(path):
    if _storage_stat(path, directory=True) is None:
        Path(path).mkdir(mode=0o700, parents=True, exist_ok=True)
    _protect_private_path(path, 0o700, directory=True)


def _open_private_file(path, flags, create=False, protect=True):
    """Open a checked regular file, creating exclusively when requested.

    Check the opened descriptor again before chmod or reading. Existing files
    are reopened without O_CREAT, so a removal race cannot create a new file.
    """
    _storage_stat(path)
    flags |= getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    created = False
    if create:
        try:
            fd = os.open(str(path), flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            _storage_stat(path)
            fd = os.open(str(path), flags)
    else:
        fd = os.open(str(path), flags)
    opened = None
    try:
        opened = os.fstat(fd)
        info = _storage_fd(path, fd)
        if protect:
            if sys.platform == 'win32':
                _windows_owner_only(path)
            elif os.name == 'posix' and stat.S_IMODE(info.st_mode) != 0o600:
                os.fchmod(fd, 0o600)
    except BaseException:
        os.close(fd)
        if created and opened is not None:
            # Do not remove a replacement installed after our failed check.
            try:
                current = os.lstat(str(path))
                if (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino):
                    os.unlink(str(path))
            except OSError:
                pass
        raise
    return fd, created


def _windows_owner_only(path, directory=False):
    """Replace the DACL with one protected ACE for the process token's user.

    Do not trust USERNAME or inherited ACLs. Directory ACEs propagate to new
    files (including SQLite journals); existing sensitive files are protected
    explicitly by Vault. A failed Win32 call stops the operation.
    """
    import ctypes
    from ctypes import wintypes as w

    class TokenUser(ctypes.Structure):
        _fields_ = [('sid', w.LPVOID), ('attributes', w.DWORD)]

    try:
        advapi = ctypes.WinDLL('advapi32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    except OSError:
        raise SecretStoreError('Cannot enforce owner-only Windows permissions for %s; Windows security APIs '
                               'are unavailable.' % path) from None
    signatures = (
        (kernel.GetCurrentProcess, [], w.HANDLE),
        (kernel.CloseHandle, [w.HANDLE], w.BOOL),
        (kernel.LocalFree, [w.LPVOID], w.LPVOID),
        (advapi.OpenProcessToken, [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)], w.BOOL),
        (advapi.GetTokenInformation, [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD,
                                     ctypes.POINTER(w.DWORD)], w.BOOL),
        (advapi.ConvertSidToStringSidW, [w.LPVOID, ctypes.POINTER(w.LPWSTR)], w.BOOL),
        (advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW,
         [w.LPCWSTR, w.DWORD, ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.DWORD)], w.BOOL),
        (advapi.GetSecurityDescriptorDacl, [w.LPVOID, ctypes.POINTER(w.BOOL),
                                          ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.BOOL)], w.BOOL),
        (advapi.SetNamedSecurityInfoW, [w.LPWSTR, ctypes.c_int, w.DWORD, w.LPVOID, w.LPVOID,
                                      w.LPVOID, w.LPVOID], w.DWORD),
    )
    for function, args, result in signatures:
        function.argtypes, function.restype = args, result
    token, sid_text, descriptor = w.HANDLE(), w.LPWSTR(), w.LPVOID()

    def check(ok):
        if not ok:
            raise OSError(ctypes.get_last_error(), 'Windows ACL operation failed')

    try:
        check(advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)))  # TOKEN_QUERY
        size = w.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser
        check(size.value > 0)
        buffer = ctypes.create_string_buffer(size.value)
        check(advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)))
        user = ctypes.cast(buffer, ctypes.POINTER(TokenUser)).contents
        check(advapi.ConvertSidToStringSidW(user.sid, ctypes.byref(sid_text)))
        # Protected DACL; one full-control ACE, inherited by new children only
        # when this is the vault directory. No other explicit grants survive.
        sddl = 'D:P(A;%s;FA;;;%s)' % ('OICI' if directory else '', sid_text.value)
        check(advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None))
        present, defaulted, dacl = w.BOOL(), w.BOOL(), w.LPVOID()
        check(advapi.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl),
                                             ctypes.byref(defaulted)))
        check(present.value and dacl.value)  # Never install a NULL (allow-all) ACL.
        error = advapi.SetNamedSecurityInfoW(str(path), 1, 0x80000004, None, None, dacl, None)
        if error:
            raise OSError(error, 'Windows ACL operation failed')
    except OSError:
        raise SecretStoreError('Cannot enforce owner-only Windows permissions for %s; nothing was read or saved.'
                               % path) from None
    finally:
        if descriptor:
            kernel.LocalFree(descriptor)
        if sid_text:
            kernel.LocalFree(ctypes.cast(sid_text, w.LPVOID))
        if token:
            kernel.CloseHandle(token)


def _check_key(key):
    if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
        raise SecretStoreError('Saved passwords need a profile ID of 1-200 letters, digits, or ._:- characters, starting with a letter or digit.')
    return key


def _check_secret(password):
    if not isinstance(password, str) or not password:
        raise SecretStoreError('Enter a password to save.')
    if len(password) > MAX_SECRET:
        raise SecretStoreError('Passwords are limited to 4096 characters.')
    return password


def _redact(text, *secrets):
    text = str(text or '')
    for secret in secrets:
        if secret:
            text = text.replace(secret, '[redacted]')
    return ' '.join(text.split())[:500]


def _encode(password):
    # Printable ASCII avoids the keychain's "hex if unprintable" output ambiguity
    # and newline handling differences in secret-tool.
    return _ENCODED_PREFIX + password.encode('utf-8').hex()


def _decode(text):
    text = text.strip()
    if text.startswith(_ENCODED_PREFIX):
        try:
            return bytes.fromhex(text[len(_ENCODED_PREFIX):]).decode('utf-8')
        except ValueError:
            raise SecretStoreError('The saved password is not in the format myflames writes. Forget it and save it again.')
    return text


# ---------------------------------------------------------------------------
# macOS Keychain
# ---------------------------------------------------------------------------

class MacKeychain(object):
    name = 'macos-keychain'
    tool = '/usr/bin/security'

    @classmethod
    def available(cls):
        return sys.platform == 'darwin' and os.path.exists(cls.tool)

    def _run(self, argv, stdin=None):
        try:
            return subprocess.run(argv, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SecretStoreError('macOS Keychain is unavailable: %s' % type(exc).__name__)

    def save(self, key, password):
        _check_key(key)  # the key is part of a stdin command line: no spaces or newlines
        data = _encode(password).encode('ascii').hex()
        # Interactive mode reads the command from stdin, so the secret is not
        # visible to other processes through argv. -U updates an existing item.
        command = 'add-generic-password -U -a %s -s %s -l "myflames connection profile" -X %s\n' % (key, SERVICE, data)
        # `security -i` reads commands in chunks of about 4 KB; a longer line
        # would be split and the remainder executed as a separate "command".
        if len(command) > MAC_COMMAND_LIMIT:
            raise SecretStoreError('This password is too long for the macOS Keychain command interface.')
        result = self._run([self.tool, '-i'], stdin=command.encode('ascii'))
        if result.returncode != 0:
            # Never echo security's stderr: it can quote parts of the command.
            raise SecretStoreError('macOS Keychain could not save the password (exit code %d).'
                                   % result.returncode)

    def get(self, key):
        result = self._run([self.tool, 'find-generic-password', '-a', key, '-s', SERVICE, '-w'])
        if result.returncode == 44:
            return None
        if result.returncode != 0:
            raise SecretStoreError('macOS Keychain did not return the password (access denied or keychain locked).')
        return _decode(result.stdout.decode('utf-8', 'replace'))

    def delete(self, key):
        result = self._run([self.tool, 'delete-generic-password', '-a', key, '-s', SERVICE])
        if result.returncode not in (0, 44):
            raise SecretStoreError('macOS Keychain could not delete the password.')


# ---------------------------------------------------------------------------
# Linux Secret Service (secret-tool)
# ---------------------------------------------------------------------------

class SecretService(object):
    name = 'secret-service'

    @classmethod
    def available(cls):
        return sys.platform.startswith('linux') and bool(shutil.which('secret-tool'))

    def _run(self, args, stdin=None):
        tool = shutil.which('secret-tool') or 'secret-tool'
        try:
            return subprocess.run([tool] + args, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SecretStoreError('Secret Service is unavailable: %s' % type(exc).__name__)

    def save(self, key, password):
        encoded = _encode(password)
        result = self._run(['store', '--label', 'myflames connection profile ' + key,
                            'service', SERVICE, 'account', key], stdin=encoded.encode('ascii'))
        if result.returncode != 0:
            raise SecretStoreError('Secret Service could not save the password (exit code %d; '
                                   'is the keyring unlocked?).' % result.returncode)

    def get(self, key):
        result = self._run(['lookup', 'service', SERVICE, 'account', key])
        if result.returncode != 0:
            if not result.stderr.strip():
                return None
            raise SecretStoreError('Secret Service did not return the password (is the keyring unlocked?).')
        if not result.stdout.strip():
            return None
        return _decode(result.stdout.decode('utf-8', 'replace'))

    def delete(self, key):
        result = self._run(['clear', 'service', SERVICE, 'account', key])
        if result.returncode != 0 and result.stderr.strip():
            raise SecretStoreError('Secret Service could not delete the password.')


# ---------------------------------------------------------------------------
# Windows Credential Manager
# ---------------------------------------------------------------------------

class WindowsCredential(object):
    name = 'windows-credential'
    ERROR_NOT_FOUND = 1168

    @classmethod
    def available(cls):
        return sys.platform == 'win32'

    def _api(self):
        import ctypes
        from ctypes import wintypes

        class CREDENTIALW(ctypes.Structure):
            _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD),
                        ('TargetName', wintypes.LPWSTR), ('Comment', wintypes.LPWSTR),
                        ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
                        ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)), ('Persist', wintypes.DWORD),
                        ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p),
                        ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR)]
        advapi32 = ctypes.WinDLL('advapi32', use_last_error=True)
        advapi32.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
        advapi32.CredWriteW.restype = wintypes.BOOL
        advapi32.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                       ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
        advapi32.CredReadW.restype = wintypes.BOOL
        advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        advapi32.CredDeleteW.restype = wintypes.BOOL
        advapi32.CredFree.argtypes = [ctypes.c_void_p]
        advapi32.CredFree.restype = None
        return ctypes, CREDENTIALW, advapi32

    @staticmethod
    def _target(key):
        return SERVICE + ':' + key

    #: CRED_MAX_CREDENTIAL_BLOB_SIZE (wincred.h): 5 * 512 bytes.
    MAX_BLOB = 2560

    def save(self, key, password):
        blob = password.encode('utf-16-le')
        if len(blob) > self.MAX_BLOB:
            # Refuse before calling CredWriteW; in auto mode save_secret then
            # uses the encrypted file instead (long cloud auth tokens).
            raise SecretStoreError('This password is too long for Windows Credential Manager '
                                   '(2,560 bytes as UTF-16).')
        ctypes, CREDENTIALW, advapi32 = self._api()
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        cred = CREDENTIALW()
        cred.Type = 1  # CRED_TYPE_GENERIC
        cred.TargetName = self._target(key)
        cred.Comment = 'myflames connection profile'
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        cred.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
        cred.UserName = key
        if not advapi32.CredWriteW(ctypes.byref(cred), 0):
            raise SecretStoreError('Windows Credential Manager could not save the password (error %d).'
                                   % ctypes.get_last_error())

    def get(self, key):
        ctypes, CREDENTIALW, advapi32 = self._api()
        pointer = ctypes.POINTER(CREDENTIALW)()
        if not advapi32.CredReadW(self._target(key), 1, 0, ctypes.byref(pointer)):
            code = ctypes.get_last_error()
            if code == self.ERROR_NOT_FOUND:
                return None
            raise SecretStoreError('Windows Credential Manager did not return the password (error %d).' % code)
        try:
            cred = pointer.contents
            blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
            return blob.decode('utf-16-le')
        finally:
            advapi32.CredFree(pointer)

    def delete(self, key):
        ctypes, _, advapi32 = self._api()
        if not advapi32.CredDeleteW(self._target(key), 1, 0):
            code = ctypes.get_last_error()
            if code != self.ERROR_NOT_FOUND:
                raise SecretStoreError('Windows Credential Manager could not delete the password (error %d).' % code)


# ---------------------------------------------------------------------------
# Encrypted file vault (fallback)
# ---------------------------------------------------------------------------

def hkdf_sha256(ikm, salt, info, length=32):
    """RFC 5869 HKDF with HMAC-SHA256."""
    prk = hmac.new(salt or b'\x00' * 32, ikm, hashlib.sha256).digest()
    output, block, counter = b'', b'', 1
    while len(output) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        output += block
        counter += 1
    return output[:length]


def _keystream(enc_key, nonce, length):
    blocks = []
    for counter in range((length + 31) // 32):
        blocks.append(hmac.new(enc_key, nonce + struct.pack('>Q', counter), hashlib.sha256).digest())
    return b''.join(blocks)[:length]


def _mac_input(key, nonce, ciphertext):
    name = key.encode('utf-8')
    return ENTRY_VERSION + struct.pack('>I', len(name)) + name + nonce + ciphertext


class Vault(object):
    name = 'vault'

    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else Path.home() / '.myflames'
        self.path = self.directory / 'credentials.vault'
        self.key_path = self.directory / 'vault.key'

    @classmethod
    def available(cls):
        return True

    # -- files ---------------------------------------------------------------
    def _ensure_dir(self):
        _ensure_private_directory(self.directory)
        for name in ('vault.key', 'credentials.vault', 'vault.lock', 'workspace.sqlite3',
                     'workspace.sqlite3-wal', 'workspace.sqlite3-shm', 'workspace.sqlite3-journal'):
            path = self.directory / name
            if _storage_stat(path) is not None and sys.platform == 'win32':
                # Explicit grants on existing files need their own ACL update.
                _windows_owner_only(path)

    def _read(self):
        self._ensure_dir()
        if _storage_stat(self.path) is None:
            return None
        try:
            fd, _created = _open_private_file(self.path, os.O_RDONLY)
            try:
                with os.fdopen(fd, 'r', encoding='utf-8') as handle:
                    fd = None
                    data = json.load(handle)
            finally:
                if fd is not None:
                    os.close(fd)
        except (OSError, ValueError) as exc:
            if isinstance(exc, SecretStoreError):
                raise
            raise SecretStoreError('The credential vault %s is unreadable or corrupt.' % self.path)
        if not isinstance(data, dict) or data.get('format') != VAULT_FORMAT or not isinstance(data.get('entries'), dict):
            raise SecretStoreError('The credential vault %s is not a myflames vault.' % self.path)
        return data

    def _write(self, data):
        self._ensure_dir()
        fd, temp = tempfile.mkstemp(prefix='.credentials.', suffix='.tmp', dir=str(self.directory))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                if sys.platform == 'win32':
                    _windows_owner_only(temp)
                elif os.name == 'posix':
                    os.fchmod(handle.fileno(), 0o600)
                json.dump(data, handle, indent=1, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            _storage_stat(self.path)
            os.replace(temp, str(self.path))
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise

    def _key_file(self, create):
        self._ensure_dir()
        info = _storage_stat(self.key_path)
        if info is not None:
            fd, _created = _open_private_file(self.key_path, os.O_RDONLY, protect=False)
            try:
                info = os.fstat(fd)
                if os.name == 'posix' and stat.S_IMODE(info.st_mode) & 0o077:
                    raise SecretStoreError('%s is readable by other users. Run: chmod 600 %s'
                                           % (self.key_path, self.key_path))
                with os.fdopen(fd, 'rb') as handle:
                    fd = None
                    raw = handle.read(33)
            finally:
                if fd is not None:
                    os.close(fd)
            if len(raw) != 32:
                raise SecretStoreError('%s is not a 32-byte myflames vault key.' % self.key_path)
            return raw
        if not create:
            raise SecretStoreError('The vault key %s is missing, so saved passwords cannot be decrypted. '
                                   'Forget them and save them again.' % self.key_path)
        raw = os.urandom(32)
        fd, created = _open_private_file(self.key_path, os.O_WRONLY, create=True, protect=False)
        if not created:
            os.close(fd)
            return self._key_file(False)
        created_info = os.fstat(fd)
        try:
            with os.fdopen(fd, 'wb') as handle:
                fd = None
                if sys.platform == 'win32':
                    _windows_owner_only(self.key_path)
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                current = os.lstat(str(self.key_path))
                if (current.st_dev, current.st_ino) == (created_info.st_dev, created_info.st_ino):
                    self.key_path.unlink()
            except OSError:
                pass
            raise
        finally:
            if fd is not None:
                os.close(fd)
        return raw

    # -- keys ----------------------------------------------------------------
    @staticmethod
    def _passphrase():
        value = os.environ.get('MYFLAMES_VAULT_PASSPHRASE')
        return value if value else None

    @staticmethod
    def _verifier(master):
        return hmac.new(hkdf_sha256(master, b'myflames-vault-1', b'verify'),
                        b'myflames vault check', hashlib.sha256).hexdigest()

    def _new_header(self):
        passphrase = self._passphrase()
        if passphrase is None:
            master = self._key_file(True)
            return {'format': VAULT_FORMAT, 'kdf': {'type': 'keyfile'}, 'entries': {}}, master
        salt = os.urandom(16)
        master = hashlib.pbkdf2_hmac('sha256', passphrase.encode('utf-8'), salt, PBKDF2_ITERATIONS, 32)
        kdf = {'type': 'pbkdf2-sha256', 'salt': salt.hex(), 'iterations': PBKDF2_ITERATIONS,
               'check': self._verifier(master)}
        return {'format': VAULT_FORMAT, 'kdf': kdf, 'entries': {}}, master

    def _master(self, data):
        kdf = data.get('kdf') or {}
        if kdf.get('type') == 'keyfile':
            if self._passphrase() is not None:
                raise SecretStoreError('The credential vault uses the key file %s; unset MYFLAMES_VAULT_PASSPHRASE '
                                       'or forget the saved passwords to re-create it.' % self.key_path)
            return self._key_file(False)
        if kdf.get('type') == 'pbkdf2-sha256':
            passphrase = self._passphrase()
            if passphrase is None:
                raise SecretStoreError('The credential vault is protected by a passphrase. Set MYFLAMES_VAULT_PASSPHRASE.')
            try:
                salt = bytes.fromhex(kdf['salt'])
                iterations = int(kdf['iterations'])
            except (KeyError, TypeError, ValueError):
                raise SecretStoreError('The credential vault header is corrupt.')
            if not 1000 <= iterations <= 10000000 or len(salt) < 16:
                raise SecretStoreError('The credential vault header is corrupt.')
            master = hashlib.pbkdf2_hmac('sha256', passphrase.encode('utf-8'), salt, iterations, 32)
            if not hmac.compare_digest(self._verifier(master), str(kdf.get('check', ''))):
                raise SecretStoreError('Wrong MYFLAMES_VAULT_PASSPHRASE for the credential vault.')
            return master
        raise SecretStoreError('The credential vault uses an unknown key scheme.')

    @staticmethod
    def _keys(master):
        salt = b'myflames-vault-1'
        return hkdf_sha256(master, salt, b'encryption'), hkdf_sha256(master, salt, b'authentication')

    def _load_for_write(self):
        data = self._read()
        if data is None or not data['entries']:
            # An empty vault can be re-keyed to match the current settings.
            return self._new_header()
        return data, self._master(data)

    # -- operations ----------------------------------------------------------
    def save(self, key, password):
        with _VaultLock(self.directory):
            data, master = self._load_for_write()
            enc_key, mac_key = self._keys(master)
            plaintext = password.encode('utf-8')
            nonce = os.urandom(16)
            ciphertext = bytes(a ^ b for a, b in zip(plaintext, _keystream(enc_key, nonce, len(plaintext))))
            tag = hmac.new(mac_key, _mac_input(key, nonce, ciphertext), hashlib.sha256).digest()
            data['entries'][key] = {'nonce': nonce.hex(), 'ciphertext': ciphertext.hex(), 'tag': tag.hex()}
            self._write(data)

    def get(self, key):
        with _VaultLock(self.directory):
            data = self._read()
            if data is None or key not in data['entries']:
                return None
            entry = data['entries'][key]
            try:
                nonce = bytes.fromhex(entry['nonce'])
                ciphertext = bytes.fromhex(entry['ciphertext'])
                tag = bytes.fromhex(entry['tag'])
            except (KeyError, TypeError, ValueError, AttributeError):
                raise SecretStoreError('The saved password failed its integrity check: the vault entry is corrupt.')
            enc_key, mac_key = self._keys(self._master(data))
            expected = hmac.new(mac_key, _mac_input(key, nonce, ciphertext), hashlib.sha256).digest()
            if len(nonce) != 16 or not hmac.compare_digest(expected, tag):
                raise SecretStoreError('The saved password failed its integrity check: the vault was modified or '
                                       'its key changed. Forget the saved password and save it again.')
            plaintext = bytes(a ^ b for a, b in zip(ciphertext, _keystream(enc_key, nonce, len(ciphertext))))
            try:
                return plaintext.decode('utf-8')
            except UnicodeDecodeError:
                raise SecretStoreError('The saved password could not be decoded.')

    def delete(self, key):
        with _VaultLock(self.directory):
            data = self._read()
            if data is None or key not in data['entries']:
                return
            del data['entries'][key]
            self._write(data)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

_NATIVE = (MacKeychain, SecretService, WindowsCredential)
_BY_NAME = {cls.name: cls for cls in _NATIVE + (Vault,)}


def _configured():
    value = (os.environ.get('MYFLAMES_SECRET_BACKEND') or 'auto').strip().lower()
    if value not in ('auto', 'keychain', 'vault', 'none'):
        raise SecretStoreError('MYFLAMES_SECRET_BACKEND must be keychain, vault, or none.')
    return value


def _native():
    for cls in _NATIVE:
        if cls.available():
            return cls
    return None


def backend_name():
    """The backend that a new saved password goes to, or ``'none'``."""
    choice = _configured()
    if choice == 'none':
        return 'none'
    if choice == 'vault':
        return Vault.name
    native = _native()
    if native is not None:
        return native.name
    return 'none' if choice == 'keychain' else Vault.name


def backend_label(name=None):
    return LABELS.get(name if name is not None else backend_name(), name or 'unknown')


def describe():
    """UI/CLI summary of where saved passwords go. Never contains secrets."""
    try:
        name = backend_name()
    except SecretStoreError as exc:
        return {'backend': 'none', 'label': LABELS['none'], 'available': False, 'error': str(exc)}
    return {'backend': name, 'label': LABELS.get(name, name), 'available': name != 'none'}


def _instance(name):
    cls = _BY_NAME.get(name)
    if cls is None:
        raise SecretStoreError('Unknown password store: %s' % name)
    return cls()


def save_secret(key, password):
    """Store *password* under *key*. Returns the backend name actually used."""
    _check_key(key)
    _check_secret(password)
    choice = _configured()
    name = backend_name()
    if name == 'none':
        if choice == 'keychain':
            raise SecretStoreError('No OS credential store is available. Set MYFLAMES_SECRET_BACKEND=vault to use an encrypted file.')
        raise SecretStoreError('Saving passwords is disabled (MYFLAMES_SECRET_BACKEND=none).')
    try:
        _instance(name).save(key, password)
        return name
    except SecretStoreError:
        if choice != 'auto' or name == Vault.name:
            raise
    # Auto mode: the OS store exists but did not work (no GUI session, no D-Bus).
    Vault().save(key, password)
    return Vault.name


def get_secret(key, backend=None):
    """Return the saved password for *key*, or None. *backend* is where it was saved."""
    _check_key(key)
    if _configured() == 'none':
        return None
    return _instance(backend or backend_name()).get(key)


def delete_secret(key, backend=None):
    """Delete the saved password for *key* (missing entries are fine)."""
    _check_key(key)
    names = [backend] if backend else []
    if not backend:
        native = _native()
        if native is not None:
            names.append(native.name)
        names.append(Vault.name)
    for name in names:
        if name == Vault.name and not Vault().path.exists():
            continue
        _instance(name).delete(key)
    return True
