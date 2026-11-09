"""Fetch the MySQL command-line client the desktop app bundles.

Downloads the MySQL Community Server archive pinned in
apps/desktop/mysql-client.lock.json into a cache, refuses it unless its size
and SHA-256 match the lock, and extracts only the client programs (mysql and
mysqldump) with exactly the shared libraries they load, plus the archive's
LICENSE and README, into apps/desktop/build/mysql-client/:

    bin/mysql, bin/mysqldump            macOS: the libraries go in lib/, where
    lib/libssl.3.dylib, ...             @loader_path/../lib finds them
    bin/mysql.exe, bin/mysqldump.exe    Windows: the DLLs sit beside the
    bin/libssl-3-x64.dll, ...           programs, the first place Windows looks
    LICENSE, README, manifest.json

The dependencies are read from the files themselves (Mach-O load commands, PE
import and delay-import tables), never from a list, and anything unexpected is
refused: a library outside the archive and the operating system, an @rpath
reference, a symlink, a fat or foreign-architecture binary. On Windows the
programs also load the Visual C++ runtime, which the archive does not include;
its DLLs are copied from the Visual Studio redistributable folder
(VC/Redist/MSVC/<version>/x64/Microsoft.VC*.CRT, found with vswhere, or
--vc-runtime) so the client runs on a PC without the runtime installed.

Standard library only. Usage (from anywhere):

    python3 apps/desktop/scripts/fetch-mysql-client.py [--platform darwin-arm64|win32-x64]
        [--cache DIR] [--out DIR] [--vc-runtime DIR] [--no-check]
"""
import argparse
import glob
import hashlib
import json
import os
import posixpath
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

DESKTOP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK = os.path.join(DESKTOP, 'mysql-client.lock.json')
CACHE = os.path.join(DESKTOP, 'build', 'mysql-client-cache')
OUT = os.path.join(DESKTOP, 'build', 'mysql-client')
PLATFORMS = ('darwin-arm64', 'win32-x64')
DOWNLOAD_HOST = 'https://cdn.mysql.com/'
CHUNK = 1 << 20

# Libraries every supported macOS has; anything else must come from the archive.
MACOS_SYSTEM_PREFIXES = ('/usr/lib/', '/System/Library/')
# DLLs Windows 10 and 11 always provide. The Universal CRT is part of the OS
# there (api-ms-win-crt-*); the Visual C++ runtime is not and ships app-local.
WINDOWS_SYSTEM_DLLS = frozenset(name.lower() for name in (
    'KERNEL32.dll', 'ADVAPI32.dll', 'USER32.dll', 'SHELL32.dll', 'WS2_32.dll', 'CRYPT32.dll',
    'Secur32.dll', 'DNSAPI.dll', 'ole32.dll', 'OLEAUT32.dll', 'bcrypt.dll', 'ntdll.dll',
    'RPCRT4.dll', 'IPHLPAPI.DLL', 'SHLWAPI.dll', 'WLDAP32.dll', 'NETAPI32.dll', 'VERSION.dll'))
WINDOWS_SYSTEM_PREFIXES = ('api-ms-win-', 'ext-ms-win-')
# A DLL is named by a plain file name; anything else (separators, a drive, "..")
# is refused before it can become an output path.
PLAIN_DLL = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+-]*\.dll$', re.I)
PLAIN_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+-]*$')
VC_RUNTIME = re.compile(r'^(?:msvcp140(?:_[a-z0-9_]+)?|vcruntime140(?:_\d+)?|concrt140|vccorlib140|vcomp140)\.dll$', re.I)


class Refused(Exception):
    """The archive, a file in it or the build host is not what the lock allows."""


def die(message):
    sys.stderr.write('fetch-mysql-client: %s\n' % message)
    raise SystemExit(2)


def load_lock(path):
    with open(path, 'rb') as handle:
        lock = json.loads(handle.read().decode('utf-8'))
    if lock.get('schema_version') != 'mysql-client-lock-1.0':
        raise Refused('%s: unknown schema_version' % path)
    if lock.get('programs') != ['mysql', 'mysqldump']:
        raise Refused('%s: programs must be exactly mysql and mysqldump' % path)
    for platform, entry in sorted(lock.get('archives', {}).items()):
        if platform not in PLATFORMS:
            raise Refused('%s: unknown platform %s' % (path, platform))
        url, size, digest, root = entry.get('url'), entry.get('size'), entry.get('sha256'), entry.get('root')
        if not isinstance(url, str) or not url.startswith(DOWNLOAD_HOST) or '?' in url or '#' in url:
            raise Refused('%s: %s url must be a plain %s address' % (path, platform, DOWNLOAD_HOST))
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise Refused('%s: %s size must be a positive byte count' % (path, platform))
        if not isinstance(digest, str) or not re.match(r'^[0-9a-f]{64}$', digest):
            raise Refused('%s: %s sha256 must be 64 lowercase hex digits' % (path, platform))
        if not isinstance(root, str) or not re.match(r'^[A-Za-z0-9][A-Za-z0-9._-]*$', root):
            raise Refused('%s: %s root must be one plain folder name' % (path, platform))
    return lock


def host_platform():
    machine = (os.environ.get('PROCESSOR_ARCHITECTURE') if os.name == 'nt' else os.uname().machine) or ''
    if sys.platform == 'darwin' and machine == 'arm64':
        return 'darwin-arm64'
    if os.name == 'nt' and machine.upper() in ('AMD64', 'X86_64'):
        return 'win32-x64'
    return None


def _hash(handle, limit):
    """SHA-256 and length of an open file, stopping once it passes *limit* bytes."""
    digest, total = hashlib.sha256(), 0
    while True:
        block = handle.read(CHUNK)
        if not block:
            break
        total += len(block)
        if total > limit:
            return None, total
        digest.update(block)
    return digest.hexdigest(), total


def open_verified(path, entry):
    """Open *path* once; return (handle, sha256, size), the handle only when it matches *entry*.

    The handle that was hashed is the one extracted, so swapping the file
    after the check changes nothing. A symlink, anything but a regular file
    and, on POSIX, a file another user owns are refused.
    """
    if os.path.islink(path):
        raise Refused('%s is a symbolic link' % path)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    handle = os.fdopen(fd, 'rb')
    try:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise Refused('%s is not a regular file' % path)
        if hasattr(os, 'getuid') and info.st_uid != os.getuid():
            raise Refused('%s belongs to another user' % path)
        digest, total = _hash(handle, entry['size'])
        if total == entry['size'] and digest == entry['sha256']:
            handle.seek(0)
            return handle, digest, total
    except BaseException:
        handle.close()
        raise
    handle.close()
    return None, digest, total


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith('https://'):
            raise Refused('refused a redirect away from HTTPS: %s' % newurl)
        return urllib.request.HTTPRedirectHandler.redirect_request(self, req, fp, code, msg, headers, newurl)


def download(url, size, target):
    """Stream *url* to a temporary file beside *target*; never more than *size* bytes."""
    opener = urllib.request.build_opener(_HttpsOnly)
    fd, partial = tempfile.mkstemp(prefix='.download-', dir=os.path.dirname(target))
    try:
        with os.fdopen(fd, 'wb') as out, opener.open(url, timeout=60) as response:
            total = 0
            while True:
                block = response.read(CHUNK)
                if not block:
                    break
                total += len(block)
                if total > size:
                    raise Refused('%s is larger than the %d bytes the lock allows' % (url, size))
                out.write(block)
        return partial
    except BaseException:
        os.unlink(partial)
        raise


def cached_archive(entry, cache):
    """Return an open handle to the cached archive, verified against *entry*.

    The caller extracts from this same handle and closes it.
    """
    os.makedirs(cache, exist_ok=True)
    target = os.path.join(cache, posixpath.basename(entry['url']))
    if os.path.lexists(target):
        handle = open_verified(target, entry)[0]
        if handle:
            return handle
        sys.stderr.write('fetch-mysql-client: the cached %s does not match the lock; downloading it again\n'
                         % os.path.basename(target))
        os.unlink(target)
    print('Downloading %s (%d bytes)' % (entry['url'], entry['size']))
    partial = download(entry['url'], entry['size'], target)
    handle, digest, total = open_verified(partial, entry)
    if handle is None:
        os.unlink(partial)
        raise Refused('refusing %s: got %d bytes with SHA-256 %s, the lock pins %d bytes with SHA-256 %s'
                      % (entry['url'], total, digest, entry['size'], entry['sha256']))
    handle.close()
    os.replace(partial, target)
    handle = open_verified(target, entry)[0]
    if handle is None:
        raise Refused('%s changed after it was verified' % target)
    return handle


class Archive(object):
    """Read regular files by exact name from a verified tar.gz or zip archive."""

    def __init__(self, fileobj, is_zip):
        self.zip, self.file = is_zip, fileobj
        self.handle = zipfile.ZipFile(fileobj) if is_zip else tarfile.open(fileobj=fileobj, mode='r:gz')
        if self.zip:
            self.members = {info.filename: info for info in self.handle.infolist()}
        else:
            self.members = {member.name: member for member in self.handle.getmembers()}

    def names(self):
        return list(self.members)

    def read(self, name):
        member = self.members.get(name)
        if member is None:
            raise Refused('the archive has no %s' % name)
        if self.zip:
            # Oracle's zip is made on Windows (no Unix mode); a Unix type, when
            # present, must be a regular file, never a symlink.
            kind = stat.S_IFMT(member.external_attr >> 16)
            if member.is_dir() or (kind and kind != stat.S_IFREG):
                raise Refused('%s in the archive is not a regular file' % name)
            return self.handle.read(member)
        if not member.isreg():
            raise Refused('%s in the archive is not a regular file' % name)
        return self.handle.extractfile(member).read()

    def close(self):
        self.handle.close()
        self.file.close()


# Mach-O: 64-bit little-endian headers and the load commands that name dylibs.
MH_MAGIC_64 = 0xfeedfacf
CPU_TYPE_ARM64 = 0x0100000c
LC_DYLIB_COMMANDS = {0xc: 'LC_LOAD_DYLIB', 0x80000018: 'LC_LOAD_WEAK_DYLIB',
                     0x8000001f: 'LC_REEXPORT_DYLIB', 0x20: 'LC_LAZY_LOAD_DYLIB',
                     0x80000023: 'LC_LOAD_UPWARD_DYLIB'}
LC_RPATH = 0x8000001c


def macho_dylibs(data, name):
    """Return the install names a thin arm64 Mach-O file loads."""
    if len(data) < 32:
        raise Refused('%s is not a Mach-O file' % name)
    magic, cputype, _, _, ncmds, sizeofcmds, _, _ = struct.unpack_from('<IiiIIIII', data, 0)
    if magic != MH_MAGIC_64:
        raise Refused('%s is not a thin 64-bit Mach-O file (magic %#x)' % (name, magic))
    if cputype != CPU_TYPE_ARM64:
        raise Refused('%s is not built for arm64 (cputype %#x)' % (name, cputype))
    offset, end, found = 32, 32 + sizeofcmds, []
    for _ in range(ncmds):
        if offset + 8 > min(end, len(data)):
            raise Refused('%s has a truncated load command' % name)
        cmd, cmdsize = struct.unpack_from('<II', data, offset)
        if cmdsize < 8 or offset + cmdsize > end:
            raise Refused('%s has a malformed load command' % name)
        if cmd in LC_DYLIB_COMMANDS or cmd == LC_RPATH:
            text = struct.unpack_from('<I', data, offset + 8)[0]
            raw = data[offset + text:offset + cmdsize].split(b'\0', 1)[0]
            if cmd == LC_RPATH:
                raise Refused('%s sets an LC_RPATH (%s); only @loader_path references are accepted'
                              % (name, raw.decode('utf-8', 'replace')))
            found.append(raw.decode('utf-8'))
        offset += cmdsize
    return found


def pe_imports(data, name):
    """Return the DLL names an x64 PE file imports, including delay-loaded ones."""
    if data[:2] != b'MZ' or len(data) < 0x40:
        raise Refused('%s is not a PE file' % name)
    pe = struct.unpack_from('<I', data, 0x3c)[0]
    if data[pe:pe + 4] != b'PE\0\0':
        raise Refused('%s is not a PE file' % name)
    machine, nsections, _, _, _, optsize, _ = struct.unpack_from('<HHIIIHH', data, pe + 4)
    if machine != 0x8664:
        raise Refused('%s is not built for x64 (machine %#x)' % (name, machine))
    optional = pe + 24
    if struct.unpack_from('<H', data, optional)[0] != 0x20b:
        raise Refused('%s is not a PE32+ file' % name)
    ndirs = struct.unpack_from('<I', data, optional + 108)[0]
    dirs = [struct.unpack_from('<II', data, optional + 112 + 8 * i) for i in range(min(ndirs, 16))]
    sections = []
    for i in range(nsections):
        vsize, va, rsize, raw = struct.unpack_from('<IIII', data, optional + optsize + 40 * i + 8)
        sections.append((va, max(vsize, rsize), raw))

    def offset(rva):
        for va, size, raw in sections:
            if va <= rva < va + size:
                return rva - va + raw
        raise Refused('%s has an import entry outside its sections' % name)

    def text(rva):
        start = offset(rva)
        return data[start:data.index(b'\0', start)].decode('ascii')

    found = []
    # Import directory (1): 20-byte descriptors, the name RVA at +12.
    # Delay-load directory (13): 32-byte descriptors, the name RVA at +4.
    for index, size, name_at in ((1, 20, 12), (13, 32, 4)):
        if index >= len(dirs) or not dirs[index][0]:
            continue
        at = offset(dirs[index][0])
        while True:
            entry = data[at:at + size]
            if len(entry) < size:
                raise Refused('%s has a truncated import table' % name)
            if not any(entry):
                break
            found.append(text(struct.unpack_from('<I', entry, name_at)[0]))
            at += size
    return found


def pe_file_version(data):
    """The VS_FIXEDFILEINFO file version of a PE file, or None."""
    at = data.find(struct.pack('<I', 0xfeef04bd))
    if at < 0 or at + 16 > len(data):
        return None
    ms, ls = struct.unpack_from('<II', data, at + 8)
    return '%d.%d.%d.%d' % (ms >> 16, ms & 0xffff, ls >> 16, ls & 0xffff)


def windows_system_dll(name):
    lower = name.lower()
    return lower in WINDOWS_SYSTEM_DLLS or lower.startswith(WINDOWS_SYSTEM_PREFIXES)


def find_vc_runtime():
    """The newest x64 Microsoft.VC*.CRT folder of the newest Visual Studio, or None."""
    roots = []
    vswhere = os.path.join(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
                           'Microsoft Visual Studio', 'Installer', 'vswhere.exe')
    if os.path.isfile(vswhere):
        result = subprocess.run([vswhere, '-latest', '-products', '*', '-property', 'installationPath'],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
        roots += [os.path.join(line.strip(), 'VC', 'Redist', 'MSVC')
                  for line in result.stdout.decode('utf-8', 'replace').splitlines() if line.strip()]
    if os.environ.get('VCToolsRedistDir'):
        roots.append(os.path.dirname(os.environ['VCToolsRedistDir'].rstrip('\\/')))
    best = None
    for root in roots:
        for folder in glob.glob(os.path.join(root, '*', 'x64', 'Microsoft.VC*.CRT')):
            version = os.path.basename(os.path.dirname(os.path.dirname(folder)))
            if not re.match(r'^\d+(\.\d+)+$', version):
                continue
            key = tuple(int(part) for part in version.split('.'))
            if best is None or key > best[0]:
                best = (key, folder)
    return best[1] if best else None


def plan_macos(archive, root):
    """Map output paths to (archive member, data) for the programs and the dylibs they load."""
    files, queue = {}, ['bin/mysql', 'bin/mysqldump']
    while queue:
        relative = queue.pop(0)
        if relative in files:
            continue
        data = archive.read(root + '/' + relative)
        files[relative] = (root + '/' + relative, data)
        for install_name in macho_dylibs(data, relative):
            if install_name.startswith(MACOS_SYSTEM_PREFIXES):
                continue
            if not install_name.startswith('@loader_path/'):
                raise Refused('%s loads %s, which is neither a system library nor beside it in the archive'
                              % (relative, install_name))
            target = posixpath.normpath(posixpath.join(posixpath.dirname(relative),
                                                       install_name[len('@loader_path/'):]))
            if target.startswith('../') or target.startswith('/') or not target.startswith('lib/'):
                raise Refused('%s loads %s, outside the archive\'s lib folder' % (relative, install_name))
            queue.append(target)
    return files


def plan_windows(archive, root, vc_runtime):
    """Map output paths to (origin, data) for the programs, their DLLs and the VC++ runtime."""
    in_bin = {}
    for name in archive.names():
        if name.startswith(root + '/bin/') and name.count('/') == 2 and PLAIN_DLL.match(posixpath.basename(name)):
            in_bin[posixpath.basename(name).lower()] = name
    runtime = {}
    if vc_runtime:
        for path in glob.glob(os.path.join(vc_runtime, '*.dll')):
            if PLAIN_DLL.match(os.path.basename(path)):
                runtime[os.path.basename(path).lower()] = path
    files, queue = {}, ['mysql.exe', 'mysqldump.exe']
    while queue:
        name = queue.pop(0)
        relative = 'bin/' + name
        if relative.lower() in {key.lower() for key in files}:
            continue
        if name in ('mysql.exe', 'mysqldump.exe'):
            origin = root + '/bin/' + name
            data = archive.read(origin)
        elif name.lower() in in_bin:
            origin = in_bin[name.lower()]
            data = archive.read(origin)
            name = posixpath.basename(origin)
            relative = 'bin/' + name
        else:
            source = runtime.get(name.lower())
            if source is None:
                raise Refused('%s is part of the Visual C++ runtime; pass --vc-runtime with the folder '
                              'that holds it (VC\\Redist\\MSVC\\<version>\\x64\\Microsoft.VC*.CRT)' % name)
            if os.path.islink(source) or not os.path.isfile(source):
                raise Refused('%s is not a regular file' % source)
            with open(source, 'rb') as handle:
                data = handle.read()
            origin = 'vc-runtime:' + source
            name = os.path.basename(source)
            relative = 'bin/' + name
        files[relative] = (origin, data)
        for dll in pe_imports(data, relative):
            if not PLAIN_DLL.match(dll):
                raise Refused('%s imports %r, which is not a plain DLL file name' % (relative, dll))
            if windows_system_dll(dll):
                continue
            if dll.lower() in in_bin or VC_RUNTIME.match(dll):
                queue.append(dll)
                continue
            raise Refused('%s imports %s, which is neither in the archive, the Visual C++ runtime nor a '
                          'Windows system DLL; review it before bundling' % (relative, dll))
    return files


def write_tree(out, files, extra, manifest):
    """Write the planned files into a fresh *out* folder; output names never come from the archive."""
    if os.path.lexists(out):
        if os.path.islink(out) or not os.path.isdir(out):
            raise Refused('%s is not a folder' % out)
        if os.path.basename(os.path.normpath(out)) != 'mysql-client' and os.listdir(out):
            raise Refused('refusing to empty %s: the output folder must be named mysql-client or be empty' % out)
        shutil.rmtree(out)
    os.makedirs(out)
    root = os.path.realpath(out)

    def inside(relative):
        parts = relative.split('/')
        if not all(PLAIN_NAME.match(part) for part in parts):
            raise Refused('refusing to write %r: not a plain path inside the output folder' % relative)
        target = os.path.join(out, *parts)
        if os.path.commonpath([root, os.path.realpath(os.path.dirname(target))]) != root:
            raise Refused('refusing to write %r outside %s' % (relative, out))
        return target

    entries = []
    for relative in sorted(files):
        origin, data = files[relative]
        target = inside(relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'xb') as handle:
            handle.write(data)
        os.chmod(target, 0o755)
        entry = {'path': relative, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'from': origin}
        if relative.lower().endswith(('.exe', '.dll')):
            entry['file_version'] = pe_file_version(data)
        entries.append(entry)
    for relative, (origin, data) in sorted(extra.items()):
        target = inside(relative)
        with open(target, 'xb') as handle:
            handle.write(data)
        os.chmod(target, 0o644)
        entries.append({'path': relative, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'from': origin})
    manifest['files'] = entries
    with open(os.path.join(out, 'manifest.json'), 'x') as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write('\n')
    return entries


def check(out, platform):
    """Run the bundled mysql --version when the build host can run it."""
    if host_platform() != platform:
        return None
    program = os.path.join(out, 'bin', 'mysql.exe' if platform == 'win32-x64' else 'mysql')
    # An empty environment apart from the system folders: nothing installed helps it start.
    env = {'PATH': os.environ.get('SystemRoot', r'C:\Windows') + r'\System32'} if os.name == 'nt' else {'PATH': '/usr/bin:/bin'}
    for key in ('SystemRoot', 'SYSTEMROOT', 'WINDIR'):
        if key in os.environ:
            env[key] = os.environ[key]
    result = subprocess.run([program, '--version'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=env, timeout=60)
    output = result.stdout.decode('utf-8', 'replace').strip()
    if result.returncode != 0:
        raise Refused('the bundled client does not start: %s' % output)
    return output


def fetch(platform, lock_path=LOCK, cache=CACHE, out=OUT, vc_runtime=None, run_check=True):
    lock = load_lock(lock_path)
    entry = lock.get('archives', {}).get(platform)
    if entry is None:
        raise Refused('%s pins no archive for %s' % (lock_path, platform))
    if platform == 'win32-x64':
        vc_runtime = vc_runtime or (find_vc_runtime() if os.name == 'nt' else None)
        if not vc_runtime or not os.path.isdir(vc_runtime):
            raise Refused('the Visual C++ runtime folder was not found; install Visual Studio with the C++ '
                          'build tools, or pass --vc-runtime VC\\Redist\\MSVC\\<version>\\x64\\Microsoft.VC*.CRT')
    archive = Archive(cached_archive(entry, cache), entry['url'].endswith('.zip'))
    try:
        root = entry['root']
        files = plan_macos(archive, root) if platform == 'darwin-arm64' else plan_windows(archive, root, vc_runtime)
        extra = {name: (root + '/' + name, archive.read(root + '/' + name)) for name in ('LICENSE', 'README')}
    finally:
        archive.close()
    manifest = {'schema_version': 'mysql-client-manifest-1.0', 'product': lock['product'],
                'version': lock['version'], 'license': lock['license'], 'source': lock['source'],
                'platform': platform, 'archive': {'url': entry['url'], 'sha256': entry['sha256']}}
    if platform == 'win32-x64':
        manifest['vc_runtime'] = os.path.abspath(vc_runtime)
    entries = write_tree(out, files, extra, manifest)
    for item in entries:
        print('  %-28s %10d  %s' % (item['path'], item['size'], item.get('file_version') or ''))
    print('Bundled %d files, %d bytes, in %s' % (len(entries), sum(item['size'] for item in entries), out))
    version = check(out, platform) if run_check else None
    if version:
        print(version)
    return entries


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--platform', choices=PLATFORMS, help='the app platform (default: this machine)')
    parser.add_argument('--lock', default=LOCK, help=argparse.SUPPRESS)
    parser.add_argument('--cache', default=os.environ.get('MYFLAMES_MYSQL_CLIENT_CACHE') or CACHE,
                        help='where downloaded archives are kept (default: build/mysql-client-cache)')
    parser.add_argument('--out', default=OUT, help='output folder (default: build/mysql-client)')
    parser.add_argument('--vc-runtime', help='Windows: the x64 Microsoft.VC*.CRT folder to copy the '
                                             'Visual C++ runtime from (default: found with vswhere)')
    parser.add_argument('--no-check', action='store_true', help='do not run the bundled mysql --version')
    args = parser.parse_args(argv)
    platform = args.platform or host_platform()
    if platform is None:
        die('the desktop app bundles a client for Apple Silicon macOS and Windows x64 only; pass --platform')
    try:
        fetch(platform, args.lock, args.cache, args.out, args.vc_runtime, not args.no_check)
    except Refused as exc:
        die(str(exc))
    except (OSError, ValueError, zipfile.BadZipFile, tarfile.TarError) as exc:
        die('%s: %s' % (type(exc).__name__, exc))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
