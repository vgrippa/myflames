"""apps/desktop/scripts/fetch-mysql-client.py: the bundled MySQL client.

Synthetic Mach-O and PE files in synthetic archives stand in for Oracle's
archives, so these tests need no network: the archive is placed in the cache
under the name and digest a test lock pins.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import struct
import tarfile
import tempfile
import unittest
import zipfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, 'apps', 'desktop', 'scripts', 'fetch-mysql-client.py')
LOCK = os.path.join(ROOT, 'apps', 'desktop', 'mysql-client.lock.json')
_spec = importlib.util.spec_from_file_location('fetch_mysql_client', SCRIPT)
fetcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fetcher)


def macho(dylibs=(), cputype=0x0100000c, magic=0xfeedfacf, rpath=None):
    """A thin Mach-O header with one LC_LOAD_DYLIB per install name."""
    commands = b''
    for name in dylibs:
        raw = name.encode() + b'\0'
        size = (24 + len(raw) + 7) // 8 * 8
        commands += struct.pack('<IIIIII', 0xc, size, 24, 2, 0x10000, 0x10000) + raw.ljust(size - 24, b'\0')
    count = len(dylibs)
    if rpath:
        raw = rpath.encode() + b'\0'
        size = (12 + len(raw) + 7) // 8 * 8
        commands += struct.pack('<III', 0x8000001c, size, 12) + raw.ljust(size - 12, b'\0')
        count += 1
    return struct.pack('<IiiIIIII', magic, cputype, 0, 2, count, len(commands), 0, 0) + commands


def pe(imports=(), delayed=(), machine=0x8664, version=None):
    """A minimal PE32+ file whose import and delay-import tables name *imports* and *delayed*."""
    va, raw = 0x1000, 0x200
    names = b''
    offsets = []
    for name in list(imports) + list(delayed):
        offsets.append(len(names))
        names += name.encode() + b'\0'
    table_size = 20 * (len(imports) + 1)
    delay_size = 32 * (len(delayed) + 1) if delayed else 0
    names_at = va + table_size + delay_size
    section = b''
    for i in range(len(imports)):
        section += struct.pack('<IIIII', 0, 0, 0, names_at + offsets[i], 0)
    section += b'\0' * 20
    for j in range(len(delayed)):
        section += struct.pack('<IIIIIIII', 1, names_at + offsets[len(imports) + j], 0, 0, 0, 0, 0, 0)
    if delayed:
        section += b'\0' * 32
    section += names
    if version:
        ms, ls = (version[0] << 16) | version[1], (version[2] << 16) | version[3]
        section += struct.pack('<IIII', 0xfeef04bd, 0x10000, ms, ls)
    dos = bytearray(0x40)
    dos[0:2] = b'MZ'
    struct.pack_into('<I', dos, 0x3c, 0x40)
    optional = bytearray(240)
    struct.pack_into('<H', optional, 0, 0x20b)
    struct.pack_into('<I', optional, 108, 16)
    struct.pack_into('<II', optional, 112 + 8, va, table_size)
    if delayed:
        struct.pack_into('<II', optional, 112 + 8 * 13, va + table_size, delay_size)
    coff = struct.pack('<HHIIIHH', machine, 1, 0, 0, 0, len(optional), 0x22)
    header = struct.pack('<8sIIIIIIHHI', b'.idata', len(section), va, len(section), raw, 0, 0, 0, 0, 0)
    head = bytes(dos) + b'PE\0\0' + coff + bytes(optional) + header
    return head.ljust(raw, b'\0') + section


class Lab(object):
    """A temporary cache, output folder and lock for one synthetic archive."""

    def __init__(self, test, platform, files, links=()):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        base = self.tmp.name
        self.cache, self.out = os.path.join(base, 'cache'), os.path.join(base, 'build', 'mysql-client')
        os.makedirs(self.cache)
        self.root = 'mysql-8.4.99-' + ('macos15-arm64' if platform == 'darwin-arm64' else 'winx64')
        name = self.root + ('.tar.gz' if platform == 'darwin-arm64' else '.zip')
        self.archive = os.path.join(self.cache, name)
        buffer = io.BytesIO()
        if platform == 'darwin-arm64':
            with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
                for path, data in files.items():
                    info = tarfile.TarInfo(self.root + '/' + path)
                    info.size, info.mode = len(data), 0o755
                    tar.addfile(info, io.BytesIO(data))
                for path, target in links:
                    info = tarfile.TarInfo(self.root + '/' + path)
                    info.type, info.linkname = tarfile.SYMTYPE, target
                    tar.addfile(info)
        else:
            with zipfile.ZipFile(buffer, 'w') as archive:
                for path, data in files.items():
                    archive.writestr(self.root + '/' + path, data)
                for path, target in links:
                    info = zipfile.ZipInfo(self.root + '/' + path)
                    info.create_system, info.external_attr = 3, 0o120777 << 16
                    archive.writestr(info, target)
        self.data = buffer.getvalue()
        with open(self.archive, 'wb') as handle:
            handle.write(self.data)
        lock = json.load(open(LOCK))
        lock['archives'] = {platform: {
            'url': 'https://cdn.mysql.com/Downloads/MySQL-8.4/' + name, 'size': len(self.data),
            'md5': hashlib.md5(self.data).hexdigest(), 'sha256': hashlib.sha256(self.data).hexdigest(),
            'root': self.root}}
        self.lock = os.path.join(base, 'lock.json')
        with open(self.lock, 'w') as handle:
            json.dump(lock, handle)
        self.platform = platform

    def fetch(self, **extra):
        with contextlib.redirect_stdout(io.StringIO()):
            return fetcher.fetch(self.platform, self.lock, self.cache, self.out, run_check=False, **extra)

    def tree(self):
        found = []
        for folder, _, names in os.walk(self.out):
            found += [os.path.relpath(os.path.join(folder, name), self.out).replace(os.sep, '/') for name in names]
        return sorted(found)


SYSTEM = '/usr/lib/libSystem.B.dylib'


def mac_files(**override):
    files = {
        'bin/mysql': macho(['@loader_path/../lib/libssl.3.dylib', '@loader_path/../lib/libcrypto.3.dylib', SYSTEM]),
        'bin/mysqldump': macho(['@loader_path/../lib/libssl.3.dylib', SYSTEM]),
        'bin/mysqld': macho([SYSTEM]),
        'lib/libssl.3.dylib': macho(['@loader_path/libcrypto.3.dylib', SYSTEM]),
        'lib/libcrypto.3.dylib': macho([SYSTEM]),
        'lib/libprotobuf.24.4.0.dylib': macho([SYSTEM]),
        'LICENSE': b'GPLv2 with the Universal FOSS Exception',
        'README': b'MySQL',
    }
    files.update(override)
    return files


CRT = ['KERNEL32.dll', 'api-ms-win-crt-runtime-l1-1-0.dll']


def win_files(**override):
    files = {
        'bin/mysql.exe': pe(['libssl-3-x64.dll', 'libcrypto-3-x64.dll', 'MSVCP140.dll', 'VCRUNTIME140.dll',
                             'VCRUNTIME140_1.dll', 'DNSAPI.dll'] + CRT, version=(8, 4, 99, 0)),
        'bin/mysqldump.exe': pe(['libssl-3-x64.dll', 'MSVCP140.dll', 'Secur32.dll'] + CRT, delayed=['WS2_32.dll']),
        'bin/mysqld.exe': pe(CRT),
        'bin/libssl-3-x64.dll': pe(['libcrypto-3-x64.dll', 'VCRUNTIME140.dll'] + CRT),
        'bin/libcrypto-3-x64.dll': pe(['CRYPT32.dll', 'VCRUNTIME140.dll'] + CRT),
        'bin/libprotobuf.dll': pe(CRT),
        'LICENSE': b'GPLv2 with the Universal FOSS Exception',
        'README': b'MySQL',
    }
    files.update(override)
    return files


def vc_runtime(test, names=('msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140_2.dll')):
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    for name in names:
        imports = ['vcruntime140.dll'] + CRT if name.startswith('msvcp') else CRT
        with open(os.path.join(folder.name, name), 'wb') as handle:
            handle.write(pe(imports, version=(14, 44, 35211, 0)))
    return folder.name


class TestMacOS(unittest.TestCase):
    def test_extracts_the_programs_and_exactly_the_libraries_they_load(self):
        lab = Lab(self, 'darwin-arm64', mac_files())
        lab.fetch()
        # mysqld and the unused protobuf library stay out; the bin/lib layout
        # keeps @loader_path/../lib working.
        self.assertEqual(lab.tree(), ['LICENSE', 'README', 'bin/mysql', 'bin/mysqldump',
                                      'lib/libcrypto.3.dylib', 'lib/libssl.3.dylib', 'manifest.json'])
        with open(os.path.join(lab.out, 'bin', 'mysql'), 'rb') as handle:
            self.assertEqual(handle.read(), mac_files()['bin/mysql'])
        if os.name != 'nt':
            self.assertTrue(os.access(os.path.join(lab.out, 'bin', 'mysql'), os.X_OK))
        manifest = json.load(open(os.path.join(lab.out, 'manifest.json')))
        self.assertEqual(manifest['archive']['sha256'], hashlib.sha256(lab.data).hexdigest())
        self.assertEqual({item['path']: item['from'] for item in manifest['files']}['lib/libssl.3.dylib'],
                         lab.root + '/lib/libssl.3.dylib')

    def test_a_rerun_replaces_the_previous_output(self):
        lab = Lab(self, 'darwin-arm64', mac_files())
        lab.fetch()
        with open(os.path.join(lab.out, 'stale'), 'w') as handle:
            handle.write('x')
        lab.fetch()
        self.assertNotIn('stale', lab.tree())

    def test_refuses_references_outside_the_archive_and_the_system(self):
        for dylib in ('@rpath/libssl.3.dylib', '/opt/homebrew/lib/libssl.3.dylib', '@executable_path/../lib/x.dylib',
                      '@loader_path/../../etc/x.dylib', '@loader_path/../share/x.dylib'):
            lab = Lab(self, 'darwin-arm64', mac_files(**{'bin/mysql': macho([dylib, SYSTEM])}))
            with self.assertRaises(fetcher.Refused, msg=dylib):
                lab.fetch()
            self.assertFalse(os.path.exists(lab.out), dylib)

    def test_refuses_rpath_fat_and_foreign_architecture_files(self):
        for bad in (macho([SYSTEM], rpath='@loader_path/../lib'), macho([SYSTEM], cputype=0x01000007),
                    macho([SYSTEM], magic=0xcafebabe), b'#!/bin/sh\n'):
            lab = Lab(self, 'darwin-arm64', mac_files(**{'bin/mysqldump': bad}))
            with self.assertRaises(fetcher.Refused):
                lab.fetch()

    def test_refuses_a_symlink_where_a_program_or_library_should_be(self):
        files = mac_files()
        del files['lib/libcrypto.3.dylib']
        lab = Lab(self, 'darwin-arm64', files, links=[('lib/libcrypto.3.dylib', '/etc/passwd')])
        with self.assertRaisesRegex(fetcher.Refused, 'not a regular file'):
            lab.fetch()

    def test_refuses_an_archive_that_lacks_a_program(self):
        files = mac_files()
        del files['bin/mysqldump']
        with self.assertRaisesRegex(fetcher.Refused, 'no .*bin/mysqldump'):
            Lab(self, 'darwin-arm64', files).fetch()


class TestDigest(unittest.TestCase):
    def test_a_cached_archive_that_does_not_match_is_downloaded_again_and_refused(self):
        lab = Lab(self, 'darwin-arm64', mac_files())
        with open(lab.archive, 'r+b') as handle:
            handle.seek(100)
            handle.write(b'tampered')

        def download(url, size, target):
            fd, partial = tempfile.mkstemp(dir=os.path.dirname(target))
            with os.fdopen(fd, 'wb') as handle:
                handle.write(b'x' * size)  # right size, wrong content
            return partial

        with mock.patch.object(fetcher, 'download', download):
            with self.assertRaisesRegex(fetcher.Refused, 'refusing .*SHA-256'):
                lab.fetch()
        self.assertEqual(os.listdir(lab.cache), [])
        self.assertFalse(os.path.exists(lab.out))

    @unittest.skipIf(os.name == 'nt', 'Windows does not rename over a file that is open')
    def test_the_verified_bytes_are_the_ones_extracted_even_if_the_file_is_swapped(self):
        # Abuse case: another writer replaces the cached archive right after
        # its digest was checked. Extraction reads the handle that was hashed.
        lab = Lab(self, 'darwin-arm64', mac_files())
        evil = Lab(self, 'darwin-arm64', mac_files(**{'bin/mysql': macho([SYSTEM]) + b'evil'}))
        real_hash = fetcher._hash

        def hash_then_swap(handle, limit):
            result = real_hash(handle, limit)
            os.replace(evil.archive, lab.archive)
            return result

        with mock.patch.object(fetcher, '_hash', hash_then_swap):
            lab.fetch()
        with open(os.path.join(lab.out, 'bin', 'mysql'), 'rb') as handle:
            self.assertEqual(handle.read(), mac_files()['bin/mysql'])

    @unittest.skipUnless(hasattr(os, 'symlink') and os.name != 'nt', 'POSIX symlinks')
    def test_a_symlink_in_the_cache_is_refused(self):
        lab = Lab(self, 'darwin-arm64', mac_files())
        elsewhere = os.path.join(lab.tmp.name, 'elsewhere.tar.gz')
        os.replace(lab.archive, elsewhere)
        os.symlink(elsewhere, lab.archive)
        with self.assertRaisesRegex(fetcher.Refused, 'symbolic link'):
            lab.fetch()
        self.assertFalse(os.path.exists(lab.out))

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'POSIX FIFOs')
    def test_a_fifo_in_the_cache_is_refused_without_hanging(self):
        lab = Lab(self, 'darwin-arm64', mac_files())
        os.unlink(lab.archive)
        os.mkfifo(lab.archive)
        with self.assertRaisesRegex(fetcher.Refused, 'not a regular file'):
            lab.fetch()

    @unittest.skipUnless(hasattr(os, 'getuid'), 'POSIX ownership')
    def test_a_cached_archive_others_can_write_is_refused(self):
        lab = Lab(self, 'darwin-arm64', mac_files())
        os.chmod(lab.archive, 0o666)
        with self.assertRaisesRegex(fetcher.Refused, 'others can write'):
            lab.fetch()

    def test_a_matching_cached_archive_is_used_without_a_download(self):
        lab = Lab(self, 'darwin-arm64', mac_files())
        with mock.patch.object(fetcher, 'download', side_effect=AssertionError('no download')):
            lab.fetch()

    def test_a_download_larger_than_the_lock_is_stopped(self):
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class Opener(object):
            def open(self, url, timeout):
                return Response(b'y' * 100)

        cache = tempfile.TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        with mock.patch.object(fetcher.urllib.request, 'build_opener', return_value=Opener()):
            with self.assertRaisesRegex(fetcher.Refused, 'larger than the 10 bytes'):
                fetcher.download('https://cdn.mysql.com/x.zip', 10, os.path.join(cache.name, 'x.zip'))
        self.assertEqual(os.listdir(cache.name), [])

    def test_a_redirect_away_from_https_is_refused(self):
        handler = fetcher._HttpsOnly()
        with self.assertRaises(fetcher.Refused):
            handler.redirect_request(None, None, 302, 'Found', {}, 'http://cdn.mysql.com/x.zip')


class TestLock(unittest.TestCase):
    def test_the_shipped_lock_is_valid(self):
        lock = fetcher.load_lock(LOCK)
        self.assertEqual(sorted(lock['archives']), ['darwin-arm64', 'win32-x64'])

    def test_refuses_a_lock_that_is_not_strict(self):
        good = json.load(open(LOCK))
        for platform, field, value in (('darwin-arm64', 'url', 'http://cdn.mysql.com/Downloads/x.tar.gz'),
                                       ('darwin-arm64', 'url', 'https://example.com/x.tar.gz'),
                                       ('win32-x64', 'sha256', 'ABC'),
                                       ('win32-x64', 'size', True),
                                       ('win32-x64', 'root', '../evil')):
            lock = json.loads(json.dumps(good))
            lock['archives'][platform][field] = value
            with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as handle:
                json.dump(lock, handle)
            self.addCleanup(os.unlink, handle.name)
            with self.assertRaises(fetcher.Refused, msg=(field, value)):
                fetcher.load_lock(handle.name)


class TestWindows(unittest.TestCase):
    def test_extracts_the_programs_their_dlls_and_the_imported_vc_runtime(self):
        lab = Lab(self, 'win32-x64', win_files())
        lab.fetch(vc_runtime=vc_runtime(self))
        # msvcp140_2.dll is in the runtime folder but nothing imports it.
        self.assertEqual(lab.tree(), ['LICENSE', 'README', 'bin/libcrypto-3-x64.dll', 'bin/libssl-3-x64.dll',
                                      'bin/msvcp140.dll', 'bin/mysql.exe', 'bin/mysqldump.exe',
                                      'bin/vcruntime140.dll', 'bin/vcruntime140_1.dll', 'manifest.json'])
        manifest = json.load(open(os.path.join(lab.out, 'manifest.json')))
        versions = {item['path']: item.get('file_version') for item in manifest['files']}
        self.assertEqual(versions['bin/mysql.exe'], '8.4.99.0')
        self.assertEqual(versions['bin/msvcp140.dll'], '14.44.35211.0')
        self.assertTrue({item['path']: item['from'] for item in manifest['files']}['bin/msvcp140.dll']
                        .startswith('vc-runtime:'))

    def test_follows_delay_loaded_imports(self):
        files = win_files(**{'bin/mysqldump.exe': pe(['MSVCP140.dll'] + CRT, delayed=['libcrypto-3-x64.dll'])})
        files['bin/mysql.exe'] = pe(['MSVCP140.dll'] + CRT)
        lab = Lab(self, 'win32-x64', files)
        lab.fetch(vc_runtime=vc_runtime(self))
        self.assertIn('bin/libcrypto-3-x64.dll', lab.tree())
        self.assertNotIn('bin/libssl-3-x64.dll', lab.tree())

    def test_refuses_an_unknown_dll(self):
        lab = Lab(self, 'win32-x64', win_files(**{'bin/mysql.exe': pe(['libmysterious.dll'] + CRT)}))
        with self.assertRaisesRegex(fetcher.Refused, 'libmysterious.dll'):
            lab.fetch(vc_runtime=vc_runtime(self))
        self.assertFalse(os.path.exists(lab.out))

    def test_refuses_a_missing_vc_runtime_dll(self):
        lab = Lab(self, 'win32-x64', win_files())
        with self.assertRaisesRegex(fetcher.Refused, 'VCRUNTIME140_1.dll'):
            lab.fetch(vc_runtime=vc_runtime(self, names=('msvcp140.dll', 'vcruntime140.dll')))

    def test_requires_a_vc_runtime_folder(self):
        lab = Lab(self, 'win32-x64', win_files())
        with mock.patch.object(fetcher, 'find_vc_runtime', return_value=None):
            with self.assertRaisesRegex(fetcher.Refused, 'Visual C\\+\\+ runtime'):
                lab.fetch()

    def test_refuses_dll_names_that_are_not_plain_file_names(self):
        # Abuse case: a member named bin/..\\..\\outside.dll, imported by that
        # name, must never become a path outside the output folder.
        for name in ('..\\..\\outside.dll', 'C:outside.dll', 'sub/outside.dll'):
            files = win_files(**{'bin/mysql.exe': pe([name] + CRT)})
            files['bin/' + name] = pe(CRT)
            lab = Lab(self, 'win32-x64', files)
            with self.assertRaisesRegex(fetcher.Refused, 'plain DLL file name', msg=name):
                lab.fetch(vc_runtime=vc_runtime(self))
            self.assertFalse(os.path.exists(lab.out), name)
            self.assertFalse(os.path.exists(os.path.join(lab.tmp.name, 'outside.dll')), name)

    def test_refuses_a_symlink_in_the_zip(self):
        files = win_files()
        del files['bin/libssl-3-x64.dll']
        lab = Lab(self, 'win32-x64', files, links=[('bin/libssl-3-x64.dll', 'C:/Windows/System32/evil.dll')])
        with self.assertRaisesRegex(fetcher.Refused, 'not a regular file'):
            lab.fetch(vc_runtime=vc_runtime(self))

    def test_refuses_foreign_architecture_programs(self):
        lab = Lab(self, 'win32-x64', win_files(**{'bin/mysql.exe': pe(CRT, machine=0x14c)}))
        with self.assertRaisesRegex(fetcher.Refused, 'x64'):
            lab.fetch(vc_runtime=vc_runtime(self))

    def test_refuses_to_empty_an_unrelated_folder(self):
        lab = Lab(self, 'win32-x64', win_files())
        lab.out = os.path.join(lab.tmp.name, 'Documents')
        os.makedirs(lab.out)
        with open(os.path.join(lab.out, 'keep.txt'), 'w') as handle:
            handle.write('mine')
        with self.assertRaisesRegex(fetcher.Refused, 'refusing to empty'):
            lab.fetch(vc_runtime=vc_runtime(self))
        self.assertTrue(os.path.exists(os.path.join(lab.out, 'keep.txt')))


if __name__ == '__main__':
    unittest.main()
