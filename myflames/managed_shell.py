"""On-demand Oracle MySQL Shell installer, with no HTTP or discovery side effects.

The shipped lock is the trust anchor. Its SHA-256 is checked on the descriptor
subsequently handed to the archive reader, before parsing any archive headers.
Only start_install writes or contacts Oracle. Callers may pass the workspace
store's directory; the desktop supplies MYFLAMES_TOOLS_DIR instead.
"""
import contextlib
import hashlib
import http.client
import json
import ntpath
import os
import platform
import re
import shutil
import signal
import socket
import ssl
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import unicodedata
import zipfile

from . import connector, tls

MAX_SHELL_ARCHIVE_BYTES = 250 * 1024 * 1024
MAX_EXTRACTED_FILES = 40000  # Entries of every type, including directories.
MAX_EXTRACTED_BYTES = 1536 * 1024 * 1024
MAX_PATH_DEPTH = 32
MIN_FREE_BYTES = 1300000000
DOWNLOAD_DEADLINE = 900.0
REQUEST_TIMEOUT = 15.0
CHUNK = 65536
SIGNING_KEY = 'BCA43417C3B485DD128EC6D4B7B3B788A8D3785C'
DOWNLOAD_PREFIX = 'https://cdn.mysql.com/Downloads/MySQL-Shell/'
PLATFORMS = ('darwin-arm64', 'darwin-x64', 'win32-x64')
VC_RUNTIME = ('msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll')
_ACTIVE = ('downloading', 'verifying', 'installing')
_LOCK = threading.RLock()
_JOB = None
_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')
_VERSION = re.compile(r'^[0-9]+\.[0-9]+\.[0-9]+$')
_RESERVED = re.compile(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', re.I)
_TEMP = re.compile(r'^(?:\.mysqlsh-(?:download|stage|manifest|probe)-[A-Za-z0-9_]+(?:\.part)?|\.retire-[0-9a-f]{32})$')
_INSTALL_LOCK = '.install.lock'


class Refused(ValueError):
    """A fixed, plain-language explanation suitable for the user."""


class _Cancelled(Exception):
    pass


def _validate_url(url):
    # Reject encoded separators/dot segments too. Oracle's archive URLs need
    # neither escaping nor query strings, credentials or explicit ports.
    if (not isinstance(url, str) or not url.startswith(DOWNLOAD_PREFIX + 'mysql-shell-')
            or any(c in url for c in ('?', '#', '\\', '%', '..'))
            or any(ord(c) <= 32 or ord(c) >= 127 for c in url)):
        raise Refused('The MySQL Shell download address is not allowed.')
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.netloc != 'cdn.mysql.com'
            or '/' in url[len(DOWNLOAD_PREFIX):]):
        raise Refused('The MySQL Shell download address is not allowed.')
    return url


def validate_lock(lock):
    """Validate all fields, including unused host entries, before any I/O."""
    try:
        if (not isinstance(lock, dict) or set(lock) != {
                'schema_version', 'product', 'version', 'license', 'license_url',
                'downloads_url', 'signing_key', 'archives'}
                or lock['schema_version'] != 'mysql-shell-lock-1.0'
                or lock['product'] != 'MySQL Shell'
                or not isinstance(lock['version'], str) or not _VERSION.fullmatch(lock['version'])
                or lock['license'] != 'GPL-2.0-only WITH Universal-FOSS-exception-1.0'
                or lock['license_url'] != 'https://www.mysql.com/about/legal/licensing/oem/'
                or lock['signing_key'] != SIGNING_KEY
                or not isinstance(lock['downloads_url'], str)
                or not re.fullmatch(r'https://dev\.mysql\.com/downloads/shell/(?:[0-9]+\.[0-9]+\.html)?',
                                    lock['downloads_url'])
                or not isinstance(lock['archives'], dict) or set(lock['archives']) != set(PLATFORMS)):
            raise ValueError()
        for key, entry in lock['archives'].items():
            if not isinstance(entry, dict) or set(entry) not in (
                    {'url', 'size', 'md5', 'sha256', 'root'},
                    {'version', 'url', 'size', 'md5', 'sha256', 'root'}):
                raise ValueError()
            version = entry.get('version', lock['version'])
            if not isinstance(version, str) or not _VERSION.fullmatch(version):
                raise ValueError()
            _validate_url(entry['url'])
            if (type(entry['size']) is not int or not 0 < entry['size'] <= MAX_SHELL_ARCHIVE_BYTES
                    or not isinstance(entry['sha256'], str)
                    or not re.fullmatch(r'[0-9a-f]{64}', entry['sha256'])
                    or not isinstance(entry['md5'], str) or not re.fullmatch(r'[0-9a-f]{32}', entry['md5'])
                    or not isinstance(entry['root'], str) or not _NAME.fullmatch(entry['root'])
                    or '..' in entry['root']):
                raise ValueError()
            suffix = '.zip' if key == 'win32-x64' else '.tar.gz'
            ending = {'darwin-arm64': r'macos[0-9]+-arm64',
                      'darwin-x64': r'macos[0-9]+-x86-64bit',
                      'win32-x64': r'windows-x86-64bit'}[key]
            if (not re.fullmatch('mysql-shell-' + re.escape(version) + '-' + ending, entry['root'])
                    or entry['url'] != DOWNLOAD_PREFIX + entry['root'] + suffix):
                raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise Refused('The MySQL Shell download information is invalid.') from None
    return lock


def load_lock():
    try:
        with open(os.path.join(os.path.dirname(__file__), 'mysql-shell.lock.json'), encoding='utf-8') as handle:
            return validate_lock(json.load(handle))
    except (OSError, ValueError):
        raise Refused('The MySQL Shell download information is invalid.') from None


def host_platform():
    machine = platform.machine().lower()
    if sys.platform == 'darwin':
        return {'arm64': 'darwin-arm64', 'aarch64': 'darwin-arm64',
                'x86_64': 'darwin-x64', 'amd64': 'darwin-x64'}.get(machine)
    if sys.platform == 'win32' and machine in ('amd64', 'x86_64', 'x64'):
        return 'win32-x64'
    return None


def _tools_directory(workspace_directory=None):
    value = os.environ.get('MYFLAMES_TOOLS_DIR')
    if not value:
        directory = workspace_directory or os.path.join(os.path.expanduser('~'), '.myflames')
        value = os.path.join(os.fspath(directory), 'tools')
    return os.path.abspath(value)


def _windows_guard(directory):
    if sys.platform != 'win32':
        return
    if os.environ.get('MYFLAMES_DESKTOP_DATA'):
        raise Refused('MySQL Shell cannot be installed when MYFLAMES_DESKTOP_DATA chooses the data folder.')
    if directory.replace('/', '\\').startswith('\\\\'):
        raise Refused('MySQL Shell needs a local tools folder in your user profile.')
    # lstat from the drive root down before resolving anything. A junction
    # below the profile can otherwise make realpath contact a UNC target.
    _windows_no_reparse_ancestors(directory)
    resolved = ntpath.normcase(os.path.realpath(directory))
    for key in ('LOCALAPPDATA', 'MYFLAMES_USER_PROFILE', 'USERPROFILE'):
        root = os.environ.get(key)
        if not root or not ntpath.isabs(root) or root.replace('/', '\\').startswith('\\\\'):
            continue
        try:
            _windows_no_reparse_ancestors(root)
        except (Refused, OSError):
            continue  # An unusable environment root must not veto a clean one.
        root = ntpath.normcase(os.path.realpath(root))
        try:
            if ntpath.commonpath((resolved, root)) == root:
                return
        except ValueError:
            pass
    raise Refused('MySQL Shell needs a local tools folder in your user profile.')


def _windows_no_reparse_ancestors(path):
    drive, tail = ntpath.splitdrive(ntpath.normpath(path))
    if not re.fullmatch(r'[A-Za-z]:', drive):
        raise Refused('MySQL Shell needs a local tools folder in your user profile.')
    current = drive + '\\'
    for component in tail.strip('\\/').split('\\'):
        if not component:
            continue
        current = ntpath.join(current, component)
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            break
        if _is_link(info):
            raise Refused('The MySQL Shell tools folder must be a real private folder, not a link.')


def _trusted_directory(path):
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or _is_link(info):
        raise Refused('The MySQL Shell tools folder must be a real private folder, not a link.')
    if os.name != 'nt' and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise Refused('The MySQL Shell tools folder must be private to your user account.')
    if os.name != 'nt':
        try:
            connector._check_client_trust(path)
        except ValueError:
            raise Refused('A folder above the MySQL Shell tools folder can be changed by other users.') from None


def _is_link(info):
    # Windows junctions and other reparse points must not bypass lstat checks.
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _check_existing_ancestors(path):
    existing = path
    while not os.path.lexists(existing):
        parent = os.path.dirname(existing)
        if parent == existing:
            raise Refused('The MySQL Shell tools folder is unavailable.')
        existing = parent
    if os.name != 'nt':
        try:
            connector._check_client_trust(existing)
        except ValueError:
            raise Refused('A folder above the MySQL Shell tools folder can be changed by other users.') from None


def _directories(workspace_directory=None, create=False):
    tools = _tools_directory(workspace_directory)
    _windows_guard(tools)  # Before realpath, mkdir or network.
    parent = os.path.join(tools, 'mysqlsh')
    # Inspect both before creating or cleaning anything; never chmod an
    # untrusted existing directory into apparent trust.
    for directory in (tools, parent):
        if os.path.lexists(directory):
            _trusted_directory(directory)
    _check_existing_ancestors(tools)
    if create:
        missing = []
        current = tools
        while not os.path.lexists(current):
            missing.append(current)
            current = os.path.dirname(current)
        for directory in reversed(missing):
            os.mkdir(directory, 0o700)
            _trusted_directory(directory)
        if not os.path.lexists(parent):
            os.mkdir(parent, 0o700)
        _trusted_directory(tools)
        _trusted_directory(parent)
    return parent


def _remove(path):
    if path is None:
        return
    try:
        info = os.lstat(path)
        if stat.S_ISDIR(info.st_mode) and not _is_link(info):
            shutil.rmtree(path)
        else:
            os.unlink(path)
    except FileNotFoundError:
        pass


def _clean_stale(directory):
    # Only the install worker holding .install.lock may clean these entries.
    for name in os.listdir(directory):
        if _TEMP.fullmatch(name):
            _remove(os.path.join(directory, name))


def _acquire_install_lock(directory):
    """Hold a kernel lock until close, including cleanup and atomic publish.

    A finishing job removes its file. Recheck the pathname after locking so
    a process that opened the old inode cannot install alongside its successor.
    Kernel locks expire on process exit, so a stale file can be reused safely.
    """
    path = os.path.join(directory, _INSTALL_LOCK)
    message = 'The MySQL Shell install lock must be a private regular file.'
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0)
    if os.name == 'nt':
        # The CRT shares delete access and deletes on the final close. Closing
        # then unlinking on Windows would race a new owner of the same file.
        flags |= os.O_TEMPORARY
    for unused in range(8):
        if os.path.lexists(path) and _is_link(os.lstat(path)):
            raise Refused(message)
        handle = os.fdopen(os.open(path, flags, 0o600), 'r+b')
        try:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink > 1
                    or (os.name != 'nt' and (info.st_uid != os.geteuid() or info.st_mode & 0o077))):
                raise Refused(message)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise Refused('A MySQL Shell installation is already running in another myflames process. Try again when it finishes.') from None
            try:
                current = os.lstat(path)
            except FileNotFoundError:
                continue  # Its previous owner removed it before we locked it.
            if not os.path.samestat(info, current):
                continue  # Retry against the replacement, never the old inode.
            if _is_link(current) or info.st_nlink != 1:
                raise Refused(message)
            handle.write(('%d\n' % os.getpid()).encode('ascii'))
            handle.truncate()
            handle.flush()
            result, handle = handle, None
            return result
        finally:
            if handle is not None:
                handle.close()
    raise Refused('The MySQL Shell install folder could not be locked.')


def _release_install_lock(job):
    """Remove only our lock after all install artifacts have been cleaned."""
    handle, job.install_lock = job.install_lock, None
    if handle is None:
        return
    try:
        if os.name != 'nt':
            path = os.path.join(job.directory, _INSTALL_LOCK)
            try:
                current = os.lstat(path)
            except FileNotFoundError:
                return
            if os.path.samestat(os.fstat(handle.fileno()), current):
                # No install work follows this unlink. A waiter must recheck
                # its inode before doing any work on the newly unlocked file.
                os.unlink(path)
    finally:
        # On Windows O_TEMPORARY removes the file without an unlock/unlink gap.
        handle.close()


class InstallJob:
    """One process-wide flight, with an OS lock for the shared install folder."""
    def __init__(self, key, entry, directory):
        self.id = uuid.uuid4().hex
        self.platform = key
        self.entry = dict(entry)
        self.directory = directory
        self.state = 'downloading'
        self.received = 0
        self.total = entry['size']
        self.error = None
        self.started = time.monotonic()
        self.finished = None
        self.cancel_event = threading.Event()
        self.watchdog = None
        self.thread = None
        self.install_lock = None

    def snapshot(self):
        with _LOCK:
            return {'id': self.id, 'state': self.state, 'version': self.entry['version'],
                    'received': self.received, 'total': self.total,
                    'elapsed_s': round((self.finished or time.monotonic()) - self.started, 3),
                    'error': self.error,
                    'cleaning': bool(self.state in ('cancelled', 'failed') and
                                     self.thread and self.thread.is_alive())}

    def check_cancel(self):
        if self.cancel_event.is_set():
            raise _Cancelled()

    def update(self, **changes):
        with _LOCK:
            for key, value in changes.items():
                setattr(self, key, value)


class _ShellRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reuse the updater's bounded redirect implementation with our allowlist."""
    def http_error_302(self, req, fp, code, msg, headers):
        from .app_update import _ReleaseRedirectHandler
        location = headers.get('location') or headers.get('uri')
        try:
            # Reject unsafe raw Location values before urljoin can erase dot
            # segments. No pinned CDN address requires escapes or parameters.
            if location is not None and (any(value in location for value in ('..', '\\', '%', '?', '#'))
                    or any(ord(c) <= 32 or ord(c) >= 127 for c in location)
                    or '.' in urllib.parse.urlsplit(location).path.split('/')):
                raise Refused('The MySQL Shell download address is not allowed.')
            handler = _ReleaseRedirectHandler(_validate_url)
            handler.parent = self.parent
            return handler.http_error_302(req, fp, code, msg, headers)
        finally:
            fp.close()

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_url(newurl)
        return super(_ShellRedirectHandler, self).redirect_request(req, fp, code, msg, headers, newurl)


def _network_check(job, deadline):
    job.check_cancel()
    if job.watchdog.expired or time.monotonic() >= deadline:
        raise Refused('The MySQL Shell download took too long.')


def _download(job):
    deadline = time.monotonic() + DOWNLOAD_DEADLINE
    watchdog = tls.Watchdog(deadline)
    job.update(watchdog=watchdog)
    temporary = None
    try:
        _network_check(job, deadline)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            tls.WatchedHTTPSHandler(watchdog, tls.https_context()), _ShellRedirectHandler())
        request = urllib.request.Request(_validate_url(job.entry['url']), headers={'Accept-Encoding': 'identity'})
        with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
            _network_check(job, deadline)
            _validate_url(response.geturl())
            if getattr(response, 'status', 200) != 200:
                raise Refused('Oracle did not return the MySQL Shell archive.')
            declared = response.headers.get_all('Content-Length', [])
            if declared:
                if len(declared) != 1 or not re.fullmatch(r'[0-9]+', declared[0]):
                    raise Refused('The MySQL Shell download has an invalid size.')
                length = int(declared[0])
                if length > MAX_SHELL_ARCHIVE_BYTES or length != job.total:
                    raise Refused('The MySQL Shell download does not match the pinned size.')
            fd, temporary = tempfile.mkstemp(prefix='.mysqlsh-download-', suffix='.part', dir=job.directory)
            with os.fdopen(fd, 'wb') as output:
                received = 0
                while True:
                    _network_check(job, deadline)
                    chunk = response.read(min(CHUNK, job.total - received + 1))
                    _network_check(job, deadline)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > job.total or received > MAX_SHELL_ARCHIVE_BYTES:
                        raise Refused('The MySQL Shell download is larger than allowed.')
                    output.write(chunk)
                    job.update(received=received)
                if received != job.total or (getattr(response, 'length', None) or 0) > 0:
                    raise Refused('The download ended early.')
                output.flush()
                os.fsync(output.fileno())
        _network_check(job, deadline)
        result, temporary = temporary, None
        return result
    except (http.client.IncompleteRead, ssl.SSLEOFError):
        raise Refused('The download ended early.') from None
    except (socket.timeout, urllib.error.URLError, OSError):
        job.check_cancel()
        if watchdog.expired or time.monotonic() >= deadline:
            raise Refused('The MySQL Shell download took too long.') from None
        raise Refused('cdn.mysql.com could not be reached or the download could not be saved.') from None
    finally:
        watchdog.stop()
        job.update(watchdog=None)
        _remove(temporary)


@contextlib.contextmanager
def open_verified(path, entry, job):
    """Keep this exact descriptor open through hashing and archive extraction."""
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0)
    if _is_link(os.lstat(path)):
        raise Refused('The downloaded MySQL Shell archive is not a private regular file.')
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_size != entry['size']
                or (os.name != 'nt' and (info.st_uid != os.geteuid() or info.st_mode & 0o077))):
            raise Refused('The downloaded MySQL Shell archive is not a private regular file.')
        digest = hashlib.sha256()
        received = 0
        while True:
            job.check_cancel()
            chunk = handle.read(min(CHUNK, entry['size'] - received + 1))
            if not chunk:
                break
            received += len(chunk)
            if received > entry['size']:
                raise Refused('The downloaded archive does not match the pinned size.')
            digest.update(chunk)
        if received != entry['size'] or digest.hexdigest() != entry['sha256']:
            raise Refused('The downloaded archive did not match the pinned SHA-256 checksum.')
        handle.seek(0)
        yield handle


def _member_path(name, root, zip_name=False):
    if (not isinstance(name, str) or not name or name.startswith(('/', '\\'))
            or ':' in name or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in name)
            or ('\\' in name and not zip_name)):
        raise Refused('The MySQL Shell archive contains an unsafe file path.')
    if zip_name:
        name = name.replace('\\', '/')
    # A single trailing separator denotes a directory; empty interior parts
    # and aliases such as '.', trailing dots/spaces and devices are refused.
    parts = name[:-1].split('/') if name.endswith('/') else name.split('/')
    if (len(parts) > MAX_PATH_DEPTH or parts[0] != root or any(
            not p or p in ('.', '..') or p.endswith(('.', ' ')) or _RESERVED.match(p) for p in parts)):
        raise Refused('The MySQL Shell archive contains an unsafe file path.')
    return parts[1:]


def _parents(stage, parts, seen):
    current = stage
    for depth, part in enumerate(parts, 1):
        relative = '/'.join(parts[:depth])
        identity = unicodedata.normalize('NFC', relative).casefold()
        if identity in seen and seen[identity] != relative:
            raise Refused('The MySQL Shell archive contains conflicting file paths.')
        seen[identity] = relative  # Implicit ancestors share the same namespace.
        current = os.path.join(current, part)
        if not os.path.lexists(current):
            os.mkdir(current, 0o755)
        info = os.lstat(current)
        if not stat.S_ISDIR(info.st_mode) or _is_link(info):
            raise Refused('The MySQL Shell archive contains conflicting file paths.')
    return current


def _extract(handle, stage, entry, key, job):
    """Write regular files/dirs, create sibling links, then validate all targets."""
    stage = os.path.realpath(stage)
    seen, explicit = {}, set()
    links = []
    count, total = 0, 0
    archive = zipfile.ZipFile(handle) if key == 'win32-x64' else tarfile.open(fileobj=handle, mode='r:gz')
    with archive:
        members = archive.infolist() if key == 'win32-x64' else archive
        for member in members:
            job.check_cancel()
            count += 1
            if count > MAX_EXTRACTED_FILES:
                raise Refused('The MySQL Shell archive contains too many files.')
            zipped = key == 'win32-x64'
            name = member.orig_filename if zipped else member.name
            parts = _member_path(name, entry['root'], zipped)
            relative = '/'.join(parts)
            directory = member.is_dir() if zipped else member.isdir()
            # Case-fold on every host: the tarballs also install onto commonly
            # case-insensitive APFS volumes. No archive entry may alias another.
            identity = unicodedata.normalize('NFC', relative).casefold()
            if (identity in explicit or (identity in seen and
                    (seen[identity] != relative or not directory))):
                raise Refused('The MySQL Shell archive contains conflicting file paths.')
            seen[identity] = relative
            explicit.add(identity)
            mode = (member.external_attr >> 16) if zipped else member.mode
            symlink = stat.S_ISLNK(mode) if zipped else member.issym()
            regular = stat.S_IFMT(mode) in (0, stat.S_IFREG) if zipped else member.isfile()
            if zipped and (symlink or member.flag_bits & 1):
                raise Refused('The MySQL Shell archive contains an unsupported file type.')
            if not (directory or symlink or regular) or (not parts and not directory):
                raise Refused('The MySQL Shell archive contains an unsupported file type.')
            if not parts:
                continue
            destination = os.path.join(stage, *parts)
            _parents(stage, parts[:-1], seen)
            if symlink:
                target = member.linkname
                # Only a bare sibling name. Oracle uses forward sibling chains.
                if (not target or '/' in target or '\\' in target or ':' in target
                        or target in ('.', '..') or target.endswith(('.', ' '))
                        or _RESERVED.match(target) or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in target)):
                    raise Refused('The MySQL Shell archive contains an unsafe symbolic link.')
                links.append((parts, target))
                continue
            if directory:
                _parents(stage, parts, seen)
                continue
            size = member.file_size if zipped else member.size
            if size < 0 or total + size > MAX_EXTRACTED_BYTES:
                raise Refused('The MySQL Shell archive expands beyond the allowed size.')
            source = archive.open(member) if zipped else archive.extractfile(member)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
            try:
                fd = os.open(destination, flags, 0o600)
            except FileExistsError:
                source.close()
                raise Refused('The MySQL Shell archive contains conflicting file paths.') from None
            with source, os.fdopen(fd, 'wb') as output:
                written = 0
                while True:
                    job.check_cancel()
                    chunk = source.read(min(CHUNK, size - written + 1))
                    if not chunk:
                        break
                    written += len(chunk)
                    total += len(chunk)
                    if written > size or total > MAX_EXTRACTED_BYTES:
                        raise Refused('The MySQL Shell archive expands beyond the allowed size.')
                    output.write(chunk)
                if written != size:
                    raise Refused('The MySQL Shell archive contains an incomplete file.')
            os.chmod(destination, 0o755 if (mode & 0o111 or relative == 'bin/mysqlsh.exe') else 0o644)
        # Files first, then ALL links. A forward chain is valid only once its
        # final sibling exists. Parents always remain real directories.
        for parts, target in links:
            job.check_cancel()
            folder = _parents(stage, parts[:-1], seen)
            destination = os.path.join(folder, parts[-1])
            try:
                os.symlink(target, destination)
            except FileExistsError:
                raise Refused('The MySQL Shell archive contains conflicting file paths.') from None
        for parts, target in links:
            job.check_cancel()
            destination = os.path.join(stage, *parts)
            resolved = os.path.realpath(destination)
            try:
                info = os.stat(destination)  # Also rejects dangling and cyclic chains.
            except OSError:
                raise Refused('The MySQL Shell archive contains an unsafe symbolic link.') from None
            if (os.path.commonpath((stage, resolved)) != stage or
                    not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))):
                raise Refused('The MySQL Shell archive contains an unsafe symbolic link.')


def _structure(stage, key):
    binary = os.path.join(stage, 'bin', 'mysqlsh.exe' if key == 'win32-x64' else 'mysqlsh')
    # Oracle puts the Windows license beside bin/, but the macOS license
    # inside share/mysqlsh/. Keep these platform layouts distinct.
    license_file = (os.path.join(stage, 'LICENSE.txt') if key == 'win32-x64' else
                    os.path.join(stage, 'share', 'mysqlsh', 'LICENSE'))
    required = [(os.path.join(stage, 'bin'), True), (binary, False),
                (os.path.join(stage, 'lib', 'mysqlsh'), True),
                (os.path.join(stage, 'share', 'mysqlsh'), True),
                (license_file, False)]
    for path, directory in required:
        try:
            info = os.lstat(path)
            if _is_link(info) or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
                raise OSError()
        except OSError:
            raise Refused('The MySQL Shell archive is missing required program files.') from None
    return binary


def _check_pe(path):
    with open(path, 'rb') as handle:
        _check_pe_handle(handle)


def _check_pe_handle(handle):
    """Inspect the same descriptor that will be hashed and copied."""
    handle.seek(0)
    header = handle.read(64)
    if len(header) < 64 or header[:2] != b'MZ':
        raise Refused('The MySQL Shell program is not a Windows x64 program.')
    offset = struct.unpack_from('<I', header, 60)[0]
    if offset < 64 or offset > os.fstat(handle.fileno()).st_size - 26:
        raise Refused('The MySQL Shell program is not a Windows x64 program.')
    handle.seek(offset)
    pe = handle.read(26)
    if (len(pe) != 26 or pe[:4] != b'PE\0\0' or struct.unpack_from('<H', pe, 4)[0] != 0x8664
            or struct.unpack_from('<H', pe, 24)[0] != 0x20b):
        raise Refused('The MySQL Shell program is not a Windows x64 program.')


def _check_binary(binary, key):
    """Always enabled in production; tests replace this function, never a flag."""
    if key == 'win32-x64':
        for name in os.listdir(os.path.dirname(binary)):
            if name.lower().endswith('.dll') or name.lower() == 'mysqlsh.exe':
                path = os.path.join(os.path.dirname(binary), name)
                if not stat.S_ISREG(os.lstat(path).st_mode):
                    raise Refused('The MySQL Shell archive contains an invalid Windows library.')
                _check_pe(path)
        return
    with open(binary, 'rb') as handle:
        header = handle.read(32)
    expected = 0x0100000c if key == 'darwin-arm64' else 0x01000007
    if len(header) < 32 or struct.unpack_from('<II', header) != (0xfeedfacf, expected):
        raise Refused('The MySQL Shell program does not match this Mac’s processor.')


def _copy_windows_runtime(binary, key):
    if key != 'win32-x64':
        return
    bundle = connector.bundled_client_dir()
    if not bundle:
        return  # A dev machine may have the system redistributable already.
    try:
        bundle = os.path.realpath(bundle)
        # The client packager records paths relative to the bundle root; the
        # desktop supplies its bin folder. Never trust a DLL merely by name.
        manifest = _read_record(os.path.join(os.path.dirname(bundle), 'manifest.json'), max_bytes=1024 * 1024)
        if not isinstance(manifest, dict) or manifest.get('schema_version') != 'mysql-client-manifest-1.0':
            raise ValueError()
        files = manifest.get('files')
        if not isinstance(files, list) or any(not isinstance(item, dict) for item in files):
            raise ValueError()
        with contextlib.ExitStack() as stack:
            verified = []
            for name in VC_RUNTIME:
                destination = os.path.join(os.path.dirname(binary), name)
                if os.path.lexists(destination):
                    continue  # A pinned archive may already carry the runtime.
                records = [item for item in files if item.get('path') == 'bin/' + name]
                if (len(records) != 1 or not isinstance(records[0].get('sha256'), str) or
                        not re.fullmatch(r'[0-9a-f]{64}', records[0]['sha256'])):
                    raise ValueError()
                source = os.path.join(bundle, name)
                resolved = os.path.realpath(source)
                if (os.path.commonpath((bundle, resolved)) != bundle or _is_link(os.lstat(source))):
                    raise ValueError()
                flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0)
                handle = stack.enter_context(os.fdopen(os.open(source, flags), 'rb'))
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ValueError()
                _check_pe_handle(handle)
                handle.seek(0)
                digest = hashlib.sha256()
                for chunk in iter(lambda: handle.read(CHUNK), b''):
                    digest.update(chunk)
                if digest.hexdigest() != records[0]['sha256']:
                    raise ValueError()
                handle.seek(0)
                verified.append((handle, destination))
            # Verify every source before writing the first DLL.
            for handle, destination in verified:
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o644)
                with os.fdopen(fd, 'wb') as output:
                    shutil.copyfileobj(handle, output, CHUNK)
    except (OSError, ValueError, connector.ConnectorError):
        raise Refused('MySQL Shell needs the Microsoft Visual C++ Redistributable (x64). Reinstall myflames or install the redistributable.') from None


def _kill_probe(proc, env):
    if os.name == 'nt':
        command = [ntpath.join(os.environ.get('SYSTEMROOT', r'C:\Windows'), 'System32', 'taskkill.exe'),
                   '/PID', str(proc.pid), '/T', '/F']
        try:
            subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=1, env=env)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if proc.poll() is None:
        proc.kill()


def _probe_version(binary, entry, job):
    """Bounded, cancellable probe with an isolated home and process group."""
    from .client_tools import _banner_version
    from .lifecycle import _clean_env
    job.check_cancel()
    try:
        binary = connector.checked_shell_program(binary)
    except connector.ConnectorError:
        raise Refused('The MySQL Shell program folder is not trusted.') from None
    with tempfile.TemporaryDirectory(prefix='.mysqlsh-probe-', dir=job.directory) as home:
        env = _clean_env(home, shell=True)
        env['LC_ALL'] = 'C'
        options = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
        proc = subprocess.Popen([binary, '--no-defaults', '--disable-plugins', '--disable-builtin-plugins', '--version'], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=home, **options)
        buffers = [bytearray(), bytearray()]
        overflow = threading.Event()
        readers = []

        def drain(pipe, index):
            try:
                while True:
                    chunk = pipe.read(4096)
                    if not chunk:
                        break
                    if len(buffers[index]) + len(chunk) > CHUNK:
                        overflow.set()
                        break
                    buffers[index].extend(chunk)
            except OSError:
                overflow.set()
            finally:
                pipe.close()

        try:
            for index, pipe in enumerate((proc.stdout, proc.stderr)):
                reader = threading.Thread(target=drain, args=(pipe, index))
                reader.start()
                readers.append(reader)
            deadline = time.monotonic() + REQUEST_TIMEOUT
            while proc.poll() is None or any(r.is_alive() for r in readers):
                job.check_cancel()
                if overflow.is_set() or time.monotonic() >= deadline:
                    raise Refused('MySQL Shell did not answer its version check in time.')
                job.cancel_event.wait(0.025)
            banner = bytes(buffers[0] + buffers[1]).decode('utf-8', 'replace')
            if proc.returncode and job.platform == 'win32-x64' and (proc.returncode & 0xffffffff) == 0xc0000135:
                raise Refused('MySQL Shell needs the Microsoft Visual C++ Redistributable (x64).')
            if proc.returncode or _banner_version(banner, 'mysqlsh') != tuple(int(n) for n in entry['version'].split('.')):
                raise Refused('MySQL Shell did not pass its version check.')
        finally:
            _kill_probe(proc, env)
            proc.wait()
            for reader in readers:
                reader.join()


def _write_json(path, value, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), mode)
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        json.dump(value, output, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())


def _record(job, folder):
    return {'schema_version': 'managed-shell-1.0', 'folder': folder,
            'version': job.entry['version'], 'platform': job.platform,
            'sha256': job.entry['sha256'], 'size': job.entry['size']}


def _publish(stage, job):
    folder = 'mysqlsh-%s-%s' % (job.entry['version'], job.platform)
    target = os.path.join(job.directory, folder)
    retired = os.path.join(job.directory, '.retire-' + uuid.uuid4().hex)
    temporary = None
    moved_old, moved_new = False, False
    _write_json(os.path.join(stage, 'myflames-shell.json'), _record(job, folder), 0o644)
    try:
        fd, temporary = tempfile.mkstemp(prefix='.mysqlsh-manifest-', suffix='.part', dir=job.directory)
        os.close(fd)
        os.unlink(temporary)
        _write_json(temporary, _record(job, folder))
        # Cancellation linearizes before or after the entire publish. A cancel
        # cannot report success halfway through moving directories/pointer.
        with _LOCK:
            job.check_cancel()
            if os.path.lexists(target):
                _trusted_directory(target)
                os.replace(target, retired)
                moved_old = True
            try:
                os.replace(stage, target)
                moved_new = True
                os.replace(temporary, os.path.join(job.directory, 'installed.json'))
                temporary = None
            except BaseException:
                if moved_new:
                    _remove(target)
                if moved_old:
                    os.replace(retired, target)
                raise
            job.state = 'done'
            job.finished = time.monotonic()
        if moved_old:
            _remove(retired)
    finally:
        _remove(temporary)


def _error_text(exc):
    if isinstance(exc, Refused):
        return str(exc)
    if isinstance(exc, (tarfile.TarError, zipfile.BadZipFile, EOFError, RuntimeError)):
        return 'The MySQL Shell archive could not be read safely.'
    return 'MySQL Shell could not be installed. Check the tools folder and available disk space.'


def _install(job):
    partial, stage = None, None
    try:
        directory = job.directory
        # Recheck at worker entry, before cleanup and before opening the network.
        _windows_guard(os.path.dirname(directory))
        _trusted_directory(os.path.dirname(directory))
        _trusted_directory(directory)
        job.check_cancel()
        if job.install_lock is None:
            raise Refused('The MySQL Shell install folder could not be locked.')
        _clean_stale(directory)
        if shutil.disk_usage(directory).free < MIN_FREE_BYTES:
            raise Refused('MySQL Shell needs at least 1.3 GB of free disk space in the tools folder.')
        partial = _download(job)
        job.update(state='verifying')
        with open_verified(partial, job.entry, job) as handle:
            stage = tempfile.mkdtemp(prefix='.mysqlsh-stage-', dir=directory)
            _extract(handle, stage, job.entry, job.platform, job)
        _remove(partial)
        partial = None
        binary = _structure(stage, job.platform)
        _copy_windows_runtime(binary, job.platform)
        _check_binary(binary, job.platform)
        job.check_cancel()
        job.update(state='installing')
        _probe_version(binary, job.entry, job)
        job.check_cancel()
        _trusted_directory(os.path.dirname(directory))
        _trusted_directory(directory)
        _publish(stage, job)
        stage = None
    except BaseException as exc:
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()
        with _LOCK:
            if job.state != 'done':
                job.state = 'cancelled' if isinstance(exc, _Cancelled) or job.cancel_event.is_set() else 'failed'
                job.error = None if job.state == 'cancelled' else _error_text(exc)
                job.finished = time.monotonic()
    finally:
        try:
            _remove(partial)
            _remove(stage)
        finally:
            _release_install_lock(job)


def _read_record(path, max_bytes=16384):
    if _is_link(os.lstat(path)):
        raise ValueError()
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, 'r', encoding='utf-8') as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes
                or (os.name != 'nt' and (info.st_uid != os.geteuid() or info.st_mode & 0o022))):
            raise ValueError()
        return json.load(handle)


def installed_bin_dir(workspace_directory=None, cache=False):
    """Return the current pinned install's bin folder, locally and without exec.

    cache=True is for read-only inventory polling only; it reuses the
    connector's five-second trust walk while pointer and metadata are still
    read afresh. Installs and launches always pass the default.
    """
    return _installed_bin_dir(workspace_directory, cache=cache)


def _installed_bin_dir(workspace_directory, cache=False):
    """Only read-only polling may cache trust; metadata is always read afresh."""
    try:
        key = host_platform()
        if key is None:
            return None
        lock = load_lock()
        entry = dict(lock['archives'][key])
        entry.setdefault('version', lock['version'])
        directory = _directories(workspace_directory)
        record = _read_record(os.path.join(directory, 'installed.json'))
        folder = 'mysqlsh-%s-%s' % (entry['version'], key)
        expected = {'schema_version': 'managed-shell-1.0', 'folder': folder,
                    'version': entry['version'], 'platform': key, 'sha256': entry['sha256'], 'size': entry['size']}
        if record != expected:
            return None
        target = os.path.join(directory, folder)
        parent_real, target_real = os.path.realpath(directory), os.path.realpath(target)
        if (target_real == parent_real or os.path.commonpath((parent_real, target_real)) != parent_real):
            return None
        _trusted_directory(target)
        if _read_record(os.path.join(target, 'myflames-shell.json')) != expected:
            return None
        binary = _structure(target, key)
        _check_binary(binary, key)
        connector.checked_shell_program(binary, cache=cache)  # Metadata never grants an exemption.
        return os.path.dirname(binary)
    except (OSError, ValueError, KeyError, TypeError, connector.ConnectorError):
        return None


def status(workspace_directory=None):
    """Read-only status for a future authenticated endpoint; never starts a job."""
    key = host_platform()
    reason = None if key else 'MySQL Shell downloads are available for macOS arm64, macOS x64 and Windows x64.'
    pinned = None
    try:
        lock = load_lock()
        _directories(workspace_directory)  # Surface storage refusals without writes.
        if key:
            entry = lock['archives'][key]
            pinned = {name: lock[name] for name in ('license', 'license_url', 'downloads_url')}
            pinned.update(version=entry.get('version', lock['version']), size=entry['size'])
    except Refused as exc:
        reason = str(exc)
    except OSError:
        reason = 'The MySQL Shell tools folder is unavailable.'
    bin_dir = _installed_bin_dir(workspace_directory, cache=True) if reason is None else None
    with _LOCK:
        snapshot = _JOB.snapshot() if _JOB else None
    return {'schema_version': 'managed-shell-status-1.0', 'supported': reason is None,
            'unsupported_reason': reason, 'platform': key, 'pinned': pinned,
            'installed': {'path': os.path.join(bin_dir, 'mysqlsh.exe' if key == 'win32-x64' else 'mysqlsh'),
                          'version': pinned['version'], 'origin': 'managed',
                          'origin_label': 'Downloaded by myflames'} if bin_dir else None, 'job': snapshot}


def start_install(workspace_directory=None):
    """Start one flight, reusing it until its worker has finished cleanup."""
    global _JOB
    with _LOCK:
        if _JOB and (_JOB.state in _ACTIVE or (_JOB.thread and _JOB.thread.is_alive())):
            return _JOB.snapshot()
        key = host_platform()
        if key is None:
            raise Refused('MySQL Shell downloads are not available for this operating system and processor.')
        lock = load_lock()
        entry = dict(lock['archives'][key])
        entry.setdefault('version', lock['version'])
        try:
            directory = _directories(workspace_directory, create=True)
        except OSError:
            raise Refused('The MySQL Shell tools folder could not be created or read.') from None
        job = InstallJob(key, entry, directory)
        try:
            job.install_lock = _acquire_install_lock(directory)
        except OSError:
            raise Refused('The MySQL Shell install folder could not be locked.') from None
        _JOB = job
        try:
            if installed_bin_dir(workspace_directory):
                job.state, job.finished = 'done', time.monotonic()
                _release_install_lock(job)
                return job.snapshot()
            job.thread = threading.Thread(target=_install, args=(job,), name='myflames-mysqlsh-install', daemon=True)
            job.thread.start()
        except BaseException:
            _release_install_lock(job)
            _JOB = None
            raise
        return job.snapshot()


def cancel(job_id):
    """Request cleanup and close download sockets immediately. Done stays done."""
    with _LOCK:
        job = _JOB
        if job is None:
            raise Refused('No MySQL Shell download is running.')
        if not isinstance(job_id, str) or not job_id or job.id != job_id:
            raise Refused('That MySQL Shell download is no longer active.')
        if job.state in _ACTIVE:
            job.cancel_event.set()
            if job.watchdog:
                job.watchdog._expire()
        return job.snapshot()


def shutdown(timeout):
    """Request cancellation and give the worker at most timeout seconds to end.

    Like cancel, expiring the job's watchdog ends a blocked network read;
    filesystem cleanup may still outlive this wait. The next installer holding
    the OS lock removes any stale staging/retire trees.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    with _LOCK:
        job = _JOB
        if job is None:
            return
        if job.state in _ACTIVE:
            job.cancel_event.set()
            if job.watchdog:
                job.watchdog._expire()
        thread = job.thread
    # Cleanup needs _LOCK too. Never hold it while waiting for the worker.
    if thread is not None and thread is not threading.current_thread():
        thread.join(max(0.0, deadline - time.monotonic()))
