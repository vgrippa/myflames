"""Installer trust and confinement gates with synthetic, checksum-pinned archives.

All transports are patched build_opener transports, as in test_app_update:
no sockets, Oracle downloads, gpg, or archive executables are needed.
"""
import copy
from email.message import Message
import hashlib
import io
import json
import ntpath
import os
from pathlib import Path
import ssl
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request
import zipfile

from myflames import managed_shell as shell

_REAL_CHECK_BINARY = shell._check_binary

# Measured Oracle 9.7.0 archive roots, independent of the installer's checks.
REAL_ARCHIVE_ROOTS = {
    'darwin-arm64': 'mysql-shell-9.7.0-macos15-arm64',
    'darwin-x64': 'mysql-shell-9.7.0-macos15-x86-64bit',
    'win32-x64': 'mysql-shell-9.7.0-windows-x86-64bit',
}


class Response(io.BytesIO):
    def __init__(self, body, url, declared=None, remaining=None):
        super().__init__(body)
        self.url, self.length, self.status = url, remaining, 200
        self.headers = Message()
        self.read_count = 0
        if declared is not None:
            self.headers['Content-Length'] = str(declared)

    def geturl(self):
        return self.url

    def read(self, amount=-1):
        self.read_count += 1
        return super(Response, self).read(amount)


def archive_bytes(key, root, extras=(), omit=()):
    """Small representatives of the measured layouts, not full vendor payloads."""
    zipped = key == 'win32-x64'
    # kind, relative name, data/target, optional mode
    entries = [('dir', '', b''), ('dir', 'bin', b''), ('dir', 'lib', b''),
               ('dir', 'lib/mysqlsh', b''), ('dir', 'libexec', b''),
               ('dir', 'libexec/mysqlsh', b''), ('dir', 'share', b''),
               ('dir', 'share/mysqlsh', b'')]
    if zipped:
        entries += [('file', 'LICENSE.txt', b'license'), ('file', 'README.txt', b'readme'),
                    ('file', 'bin/mysqlsh.exe', b'program', 0o755),
                    ('file', 'bin/python313.dll', b'library'),
                    ('file', 'bin/python3.dll', b'library'),
                    ('file', 'bin/mysql-secret-store-windows-credential.exe', b'helper', 0o755),
                    ('dir', 'bin/sasl2', b''), ('dir', 'lib/Python3.13', b''),
                    ('dir', 'lib/mysql', b'')]
    else:
        entries += [('file', 'bin/mysqlsh', b'program', 0o755),
                    ('file', 'bin/mysql-secret-store-login-path', b'helper', 0o755),
                    ('file', 'bin/mysql-secret-store-keychain', b'helper', 0o755),
                    ('file', 'libexec/mysqlsh/mysqlbinlog', b'helper', 0o755),
                    ('file', 'share/mysqlsh/LICENSE', b'license'),
                    ('dir', 'lib/mysqlsh/plugins', b''),
                    ('dir', 'lib/mysqlsh/ossl-modules', b''),
                    ('file', 'lib/mysqlsh/libssl.3.dylib', b'library'),
                    # Forward sibling chains must work in the default fixture,
                    # including when both links precede the regular target.
                    ('sym', 'lib/mysqlsh/libfido2.dylib', 'libfido2.1.dylib'),
                    ('sym', 'lib/mysqlsh/libfido2.1.dylib', 'libfido2.1.15.0.dylib'),
                    ('file', 'lib/mysqlsh/libfido2.1.15.0.dylib', b'library')]
    # Omitting a directory removes its subtree too, so extraction cannot
    # implicitly recreate a supposedly missing required directory.
    entries = [e for e in entries if not any(
        e[1] == name or e[1].startswith(name + '/') for name in omit)] + list(extras)
    stream = io.BytesIO()
    if zipped:
        with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as contents:
            for item in entries:
                kind, name, data = item[:3]
                # '@' is an explicit full archive path for malicious names.
                path = name[1:] if name.startswith('@') else root + ('/' + name if name else '')
                info = zipfile.ZipInfo(path + ('/' if kind == 'dir' else ''))
                info.create_system = 3
                mode = (item[3] if len(item) > 3 else 0o644)
                info.external_attr = ((stat.S_IFDIR if kind == 'dir' else stat.S_IFLNK if kind == 'sym' else stat.S_IFREG) | mode) << 16
                contents.writestr(info, data.encode() if isinstance(data, str) else data)
    else:
        with tarfile.open(fileobj=stream, mode='w:gz') as contents:
            for item in entries:
                kind, name, data = item[:3]
                path = name[1:] if name.startswith('@') else root + ('/' + name if name else '')
                info = tarfile.TarInfo(path)
                info.mode = item[3] if len(item) > 3 else 0o755 if kind == 'dir' else 0o644
                if kind == 'dir':
                    info.type = tarfile.DIRTYPE
                elif kind in ('sym', 'hard'):
                    info.type = tarfile.SYMTYPE if kind == 'sym' else tarfile.LNKTYPE
                    info.linkname = data
                elif kind == 'fifo':
                    info.type = tarfile.FIFOTYPE
                else:
                    info.size = len(data)
                contents.addfile(info, io.BytesIO(data) if kind == 'file' else None)
    return stream.getvalue()


class ArchiveLayoutTests(unittest.TestCase):
    """Exercise the unpatched verifier, extractor and structure gate together."""

    def setUp(self):
        self.lock = shell.load_lock()

    def unpack(self, key, omit=(), extras=()):
        temp = tempfile.TemporaryDirectory(prefix='myflames-shell-layout-')
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        entry = dict(self.lock['archives'][key])
        self.assertEqual(entry['root'], REAL_ARCHIVE_ROOTS[key])
        body = archive_bytes(key, REAL_ARCHIVE_ROOTS[key], extras=extras, omit=omit)
        entry['size'] = len(body)
        entry['sha256'] = hashlib.sha256(body).hexdigest()
        archive = root / 'archive.part'
        archive.write_bytes(body)
        archive.chmod(0o600)
        stage = root / 'stage'
        stage.mkdir(mode=0o700)
        job = shell.InstallJob(key, entry, str(root))
        with shell.open_verified(str(archive), entry, job) as handle:
            shell._extract(handle, str(stage), entry, key, job)
        return stage

    def test_each_measured_platform_layout_passes_structure(self):
        for key in REAL_ARCHIVE_ROOTS:
            with self.subTest(platform=key):
                stage = self.unpack(key)
                binary = 'mysqlsh.exe' if key == 'win32-x64' else 'mysqlsh'
                self.assertEqual(shell._structure(str(stage), key), str(stage / 'bin' / binary))
                top_level = {'bin', 'lib', 'libexec', 'share'}
                if key == 'win32-x64':
                    top_level.update(('LICENSE.txt', 'README.txt'))
                    for relative in ('bin/sasl2', 'lib/Python3.13', 'lib/mysql',
                                     'lib/mysqlsh', 'libexec/mysqlsh', 'share/mysqlsh'):
                        self.assertTrue((stage / relative).is_dir(), relative)
                    self.assertFalse((stage / 'share/mysqlsh/LICENSE.txt').exists())
                    self.assertFalse(any(path.is_symlink() for path in stage.rglob('*')))
                else:
                    self.assertEqual((stage / 'lib/mysqlsh/libfido2.dylib').read_bytes(), b'library')
                    self.assertTrue((stage / 'lib/mysqlsh/libfido2.dylib').is_symlink())
                    self.assertTrue((stage / 'lib/mysqlsh/libfido2.1.dylib').is_symlink())
                    self.assertTrue((stage / 'share/mysqlsh/LICENSE').is_file())
                self.assertEqual({path.name for path in stage.iterdir()}, top_level)

    def test_each_layout_refuses_each_missing_required_item(self):
        for key in REAL_ARCHIVE_ROOTS:
            binary = 'bin/mysqlsh.exe' if key == 'win32-x64' else 'bin/mysqlsh'
            license_file = 'LICENSE.txt' if key == 'win32-x64' else 'share/mysqlsh/LICENSE'
            for missing in ('bin', binary, 'lib/mysqlsh', 'share/mysqlsh', license_file):
                with self.subTest(platform=key, missing=missing):
                    stage = self.unpack(key, omit=(missing,))
                    self.assertFalse((stage / missing).exists())
                    with self.assertRaisesRegex(shell.Refused, 'missing required program files'):
                        shell._structure(str(stage), key)

    def test_windows_license_in_macos_location_does_not_replace_root_license(self):
        stage = self.unpack('win32-x64', omit=('LICENSE.txt',),
                            extras=[('file', 'share/mysqlsh/LICENSE.txt', b'license')])
        with self.assertRaisesRegex(shell.Refused, 'missing required program files'):
            shell._structure(str(stage), 'win32-x64')

    def test_fixtures_preserve_zip_directory_slashes_and_tar_forward_links(self):
        for key, root in REAL_ARCHIVE_ROOTS.items():
            with self.subTest(platform=key):
                body = io.BytesIO(archive_bytes(key, root))
                if key == 'win32-x64':
                    with zipfile.ZipFile(body) as archive:
                        directories = [member for member in archive.infolist() if member.is_dir()]
                        self.assertTrue(directories)
                        self.assertTrue(all(member.filename.endswith('/') for member in directories))
                        self.assertIn(root + '/lib/Python3.13/', archive.namelist())
                        self.assertIn(root + '/LICENSE.txt', archive.namelist())
                else:
                    with tarfile.open(fileobj=body, mode='r:gz') as archive:
                        names = archive.getnames()
                        first = archive.getmember(root + '/lib/mysqlsh/libfido2.dylib')
                        second = archive.getmember(root + '/lib/mysqlsh/' + first.linkname)
                        target = root + '/lib/mysqlsh/' + second.linkname
                        self.assertTrue(first.issym())
                        self.assertTrue(second.issym())
                        self.assertLess(names.index(first.name), names.index(second.name))
                        self.assertLess(names.index(second.name), names.index(target))


class ManagedShellTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='myflames-shell-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tools = self.root / 'tools'
        self.tools.mkdir(mode=0o700)
        self.directory = self.tools / 'mysqlsh'
        self.base_lock = shell.load_lock()
        self.lock = copy.deepcopy(self.base_lock)
        self.start_patch(patch.dict(os.environ, {'MYFLAMES_TOOLS_DIR': str(self.tools),
            'MYFLAMES_DESKTOP_DATA': '', 'MYFLAMES_BUNDLED_CLIENT_DIR': ''}))
        self.start_patch(patch.object(shell, '_JOB', None))
        self.start_patch(patch.object(shell.connector, 'MYSQLSH_SYSTEM_CONFIG_DIR', str(self.root / 'etc-mysql')))
        self.start_patch(patch.object(shell, 'host_platform', return_value='darwin-arm64'))
        self.start_patch(patch.object(shell, 'load_lock', side_effect=lambda: shell.validate_lock(copy.deepcopy(self.lock))))
        self.start_patch(patch.object(shell.tls, 'https_context', return_value=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)))
        self.network = self.start_patch(patch.object(shell.urllib.request, 'build_opener'))
        self.opener = self.network.return_value
        self.opener.open.side_effect = AssertionError('Unexpected network request')
        self.binary_check = self.start_patch(patch.object(shell, '_check_binary'))
        self.probe = self.start_patch(patch.object(shell, '_probe_version'))
        self.exec = self.start_patch(patch.object(shell.subprocess, 'Popen', side_effect=AssertionError('No executable may run')))
        self.addCleanup(self.join)

    def start_patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def join(self):
        job = shell._JOB
        if job and job.thread:
            job.thread.join(5)
            self.assertFalse(job.thread.is_alive(), 'Installer worker did not stop')

    def configure(self, key='darwin-arm64', extras=(), omit=(), body=None, declared='exact', remaining=None):
        shell.host_platform.return_value = key
        root = self.lock['archives'][key]['root']
        self.body = archive_bytes(key, root, extras, omit) if body is None else body
        entry = self.lock['archives'][key]
        entry['size'] = len(self.body)
        entry['sha256'] = hashlib.sha256(self.body).hexdigest()
        self.response = Response(self.body, entry['url'], len(self.body) if declared == 'exact' else declared, remaining)
        self.opener.open.side_effect = None
        self.opener.open.return_value = self.response
        return entry

    def finish(self):
        shell.start_install()
        self.join()
        return shell.status()['job']

    def assert_refused(self, body=None):
        try:
            result = self.finish()
            self.assertEqual(result['state'], 'failed', result)
            self.assertTrue(result['error'].endswith('.'))
        except shell.Refused:
            pass  # Pre-network path/lock validation refuses synchronously.
        self.probe.assert_not_called()
        self.exec.assert_not_called()
        self.assertFalse((self.directory / 'installed.json').exists())
        self.assertFalse(any(self.directory.glob('mysqlsh-*')))
        self.assertFalse(any(self.directory.glob('.mysqlsh-*')))
        self.assertFalse((self.directory / shell._INSTALL_LOCK).exists())
        return shell._JOB.snapshot() if shell._JOB else None

    def test_tar_happy_path_and_sibling_symlink(self):
        self.configure(extras=[('sym', 'lib/mysqlsh/libssl.dylib', 'libssl.3.dylib')])
        result = self.finish()
        self.assertEqual(result['state'], 'done', result)
        self.assertEqual(result['received'], result['total'])
        target = Path(shell.installed_bin_dir()).parent
        self.assertEqual((target / 'lib/mysqlsh/libssl.dylib').read_bytes(), b'library')
        self.assertEqual((target / 'bin/mysqlsh').stat().st_mode & 0o777, 0o755)
        self.assertEqual((target / 'share/mysqlsh/LICENSE').stat().st_mode & 0o777, 0o644)
        self.assertEqual((self.directory / 'installed.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads((target / 'myflames-shell.json').read_text())['sha256'], self.lock['archives']['darwin-arm64']['sha256'])
        self.assertFalse(any(self.directory.glob('.mysqlsh-*')))
        self.assertFalse((self.directory / shell._INSTALL_LOCK).exists())
        self.probe.assert_called_once()
        self.exec.assert_not_called()
        self.assertEqual(shell.start_install()['state'], 'done')
        self.assertEqual(self.opener.open.call_count, 1, 'Already installed does not download again')
        handlers = self.network.call_args[0]
        self.assertTrue(any(isinstance(h, shell.tls.WatchedHTTPSHandler) for h in handlers))
        self.assertTrue(any(isinstance(h, shell._ShellRedirectHandler) for h in handlers))
        self.assertEqual(next(h for h in handlers if isinstance(h, urllib.request.ProxyHandler)).proxies, {})

    def test_zip_happy_path(self):
        self.configure('win32-x64')
        self.assertEqual(self.finish()['state'], 'done')
        target = Path(shell.installed_bin_dir()).parent
        self.assertEqual((target / 'bin/mysqlsh.exe').read_bytes(), b'program')
        self.assertTrue((target / 'LICENSE.txt').is_file())
        self.assertTrue((target / 'README.txt').is_file())
        self.assertTrue((target / 'lib/Python3.13').is_dir())
        self.assertFalse((target / 'share/mysqlsh/LICENSE.txt').exists())
        self.probe.assert_called_once()
        self.exec.assert_not_called()

    def test_forward_sibling_chain_is_valid_after_every_link_exists(self):
        self.configure()
        self.assertEqual(self.finish()['state'], 'done')
        target = Path(shell.installed_bin_dir()).parent
        self.assertEqual((target / 'lib/mysqlsh/libfido2.dylib').read_bytes(), b'library')

    def test_zip_directory_trailing_slash_strips_exactly_one_separator(self):
        self.configure('win32-x64', extras=[('dir', 'lib/mysqlsh/extra', b''),
                                          ('file', 'lib/mysqlsh/extra/data', b'data')])
        self.assertEqual(self.finish()['state'], 'done')
        with self.assertRaises(shell.Refused):
            shell._member_path(self.lock['archives']['win32-x64']['root'] + '/lib//',
                               self.lock['archives']['win32-x64']['root'], True)

    def test_wrong_hash_before_archive_reader_or_staging(self):
        for key in ('darwin-arm64', 'win32-x64'):
            with self.subTest(key=key):
                self.configure(key)
                self.lock['archives'][key]['sha256'] = '0' * 64
                with patch.object(shell.tarfile, 'open') as tar, patch.object(shell.zipfile, 'ZipFile') as zip_reader, patch.object(shell.tempfile, 'mkdtemp') as stage:
                    self.assertIn('SHA-256', self.assert_refused()['error'])
                    tar.assert_not_called()
                    zip_reader.assert_not_called()
                    stage.assert_not_called()

    def test_both_truncated_forms(self):
        for key in ('darwin-arm64', 'win32-x64'):
            for declared in ('exact', None):
                with self.subTest(key=key, content_length=declared):
                    entry = self.configure(key, declared=declared)
                    self.response = Response(self.body[:-1], entry['url'], len(self.body) if declared else None,
                                             1 if declared else None)
                    self.opener.open.return_value = self.response
                    self.assertEqual(self.assert_refused()['error'], 'The download ended early.')

    def test_ragged_tls_eof(self):
        self.configure()
        self.response.read = Mock(side_effect=__import__('http.client', fromlist=['IncompleteRead']).IncompleteRead(b'part', 3))
        self.assertEqual(self.assert_refused()['error'], 'The download ended early.')
        self.configure()
        self.response.read = Mock(side_effect=ssl.SSLEOFError())
        self.assertEqual(self.assert_refused()['error'], 'The download ended early.')

    def test_path_traversal_absolute_drive_and_wrong_root(self):
        names = ['@../escape.sh', '../../escape.sh', '@/tmp/abs.sh', '@\\evil.dll',
                 '@C:evil.exe', '@other-root/bin/mysqlsh', 'bin/../evil', 'bin//evil',
                 'bin/CON.txt', 'bin/NUL', 'bin/evil.', 'bin/evil ', 'bin/COM1.log',
                 'bin/evil\x01', 'bin/evil\x7f', 'bin/x:stream', 'bin/./evil']
        for key in ('darwin-arm64', 'win32-x64'):
            for name in names:
                with self.subTest(key=key, name=name):
                    self.configure(key, [('file', name, b'evil')])
                    self.assert_refused()
                    self.assertFalse((self.root / 'escape.sh').exists())

    def test_tar_backslashes_and_depth(self):
        for name in ('bin\\evil', '/'.join(['nested'] * 33) + '/evil'):
            with self.subTest(name=name):
                self.configure(extras=[('file', name, b'evil')])
                self.assert_refused()

    def test_tar_link_and_special_file_abuse(self):
        cases = [ [('sym', 'lib/x.dylib', '../../../outside')],
                  [('sym', 'share/mysqlsh/mysqlshrc.js', '/etc/passwd')],
                  [('sym', 'lib/sub', '..'), ('sym', 'lib/sub/out', '../../outside')],
                  [('hard', 'lib/x.dylib', 'lib/mysqlsh/libssl.3.dylib')],
                  [('sym', 'lib/mysqlsh/non-sibling', '../mysqlsh/libssl.3.dylib')],
                  [('sym', 'lib/mysqlsh/first', 'second'), ('sym', 'lib/mysqlsh/second', 'first')],
                  [('sym', 'lib/mysqlsh/dangling', 'missing')],
                  [('sym', 'lib/mysqlsh/nul', '')],
                  [('fifo', 'lib/fifo', b'')] ]
        for extras in cases:
            with self.subTest(extras=extras):
                self.configure(extras=extras)
                self.assert_refused()

    def test_zip_symlink(self):
        self.configure('win32-x64', [('sym', 'bin/evil.dll', 'python313.dll')])
        self.assert_refused()

    def test_private_tools_and_mysqlsh_before_network(self):
        for which in (self.tools, self.directory):
            for mode in (0o755, 0o777):
                with self.subTest(folder=which.name, mode=mode):
                    self.configure()
                    which.mkdir(mode=0o700, exist_ok=True)
                    which.chmod(mode)
                    try:
                        report = shell.status()
                        self.assertIsNone(report['installed'])
                        self.assertIn('private to your user account', report['unsupported_reason'])
                        self.assert_refused()
                        self.network.assert_not_called()
                    finally:
                        which.chmod(0o700)

    def test_symlink_tools_and_mysqlsh_before_network(self):
        for which in (self.directory, self.tools):
            with self.subTest(folder=which.name):
                self.configure()
                target = self.root / ('target-' + which.name)
                target.mkdir(mode=0o700)
                if which == self.tools and self.directory.exists():
                    self.directory.rmdir()
                if which.exists():
                    which.rmdir()
                which.symlink_to(target, target_is_directory=True)
                try:
                    report = shell.status()
                    self.assertIsNone(report['installed'])
                    self.assertIn('real private folder, not a link', report['unsupported_reason'])
                    self.assert_refused()
                    self.network.assert_not_called()
                    self.assertEqual(list(target.iterdir()), [])
                finally:
                    which.unlink()
                    which.mkdir(mode=0o700)

    def test_writable_ancestor_before_network(self):
        self.configure()
        self.root.chmod(0o777)
        try:
            self.assert_refused()
            self.network.assert_not_called()
        finally:
            self.root.chmod(0o700)

    def test_foreign_owned_tools_refused_before_transport_is_built(self):
        self.configure()
        original = os.lstat

        def lstat(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if str(path) == str(self.tools):
                values = list(info)
                values[4] = os.geteuid() + 1
                return os.stat_result(values)
            return info
        with patch.object(shell.os, 'lstat', side_effect=lstat):
            self.assert_refused()
        self.network.assert_not_called()

    def test_windows_custom_data_guard_before_network(self):
        self.configure('win32-x64')
        with patch.object(shell.sys, 'platform', 'win32'), patch.dict(os.environ, {'MYFLAMES_DESKTOP_DATA': str(self.root)}):
            self.assert_refused()
        self.network.assert_not_called()

    def test_not_enough_disk_before_network(self):
        self.configure()
        with patch.object(shell.shutil, 'disk_usage', return_value=Mock(free=1)):
            self.assertIn('1.3 GB', self.assert_refused()['error'])
        self.network.assert_not_called()

    def test_header_limits_rejected_without_reading_body(self):
        for value in ('+1', 'invalid', -1, '262144001', 0, 99999):
            with self.subTest(content_length=value):
                self.configure(declared=value)
                self.assert_refused()
                self.assertEqual(self.response.read_count, 0)

    def test_duplicate_content_lengths_rejected(self):
        self.configure()
        self.response.headers['Content-Length'] = str(len(self.body))
        self.assert_refused()
        self.assertEqual(self.response.read_count, 0)

    def test_running_size_over_pin_and_over_global_cap(self):
        for cap in (shell.MAX_SHELL_ARCHIVE_BYTES, 10):
            with self.subTest(cap=cap):
                entry = self.configure(declared=None)
                self.opener.open.return_value = Response(self.body + b'excess', entry['url'])
                with patch.object(shell, 'MAX_SHELL_ARCHIVE_BYTES', cap):
                    # For the tiny-cap case keep the lock valid and reach the
                    # transport using a small pinned opaque body.
                    if cap == 10:
                        entry = self.configure(body=b'1234567890', declared=None)
                        for other in self.lock['archives'].values():
                            other['size'] = 10
                        self.opener.open.return_value = Response(b'12345678901', entry['url'])
                    self.assert_refused()

    def test_entry_and_expanded_byte_caps(self):
        for limit, value in (('MAX_EXTRACTED_FILES', 3), ('MAX_EXTRACTED_BYTES', 5)):
            for key in ('darwin-arm64', 'win32-x64'):
                with self.subTest(limit=limit, key=key):
                    self.configure(key)
                    with patch.object(shell, limit, value):
                        self.assert_refused()

    def test_header_exact_pin_plus_one_rejected_without_body_read(self):
        self.configure()
        self.response.headers.replace_header('Content-Length', str(len(self.body) + 1))
        self.assert_refused()
        self.assertEqual(self.response.read_count, 0)

    def test_binary_gate_refuses_wrong_architecture_before_probe(self):
        for key in ('darwin-arm64', 'win32-x64'):
            with self.subTest(key=key):
                self.configure(key)
                self.binary_check.side_effect = _REAL_CHECK_BINARY
                self.assert_refused()

    def test_safe_sibling_directory_symlink(self):
        self.configure(extras=[('dir', 'lib/mysqlsh/real', b''),
                               ('file', 'lib/mysqlsh/real/data', b'data'),
                               ('sym', 'lib/mysqlsh/alias', 'real')])
        self.assertEqual(self.finish()['state'], 'done')
        target = Path(shell.installed_bin_dir()).parent
        self.assertEqual((target / 'lib/mysqlsh/alias/data').read_bytes(), b'data')

    def test_symlink_archive_descriptor_refused_without_parsing(self):
        self.configure()
        downloaded = self.root / 'archive.part'
        downloaded.write_bytes(self.body)
        downloaded.chmod(0o600)
        link = self.root / 'archive-link.part'
        link.symlink_to(downloaded)
        job = shell.InstallJob('darwin-arm64', self.lock['archives']['darwin-arm64'], str(self.directory))
        with self.assertRaises(shell.Refused):
            with shell.open_verified(str(link), job.entry, job):
                self.fail('A symlink must not be verified')
        self.probe.assert_not_called()
        self.exec.assert_not_called()
        self.assertFalse((self.directory / 'installed.json').exists())
        self.assertFalse(any(self.directory.glob('mysqlsh-*')))

    def test_verified_descriptor_is_used_even_if_path_replaced(self):
        entry = self.configure()
        archive = self.root / 'archive.part'
        archive.write_bytes(self.body)
        archive.chmod(0o600)
        job = shell.InstallJob('darwin-arm64', entry, str(self.directory))
        with shell.open_verified(str(archive), entry, job) as handle:
            archive.unlink()
            archive.write_bytes(b'not the verified archive')
            self.assertEqual(handle.read(), self.body)
        self.exec.assert_not_called()

    def test_cancel_during_verification_leaves_no_artifacts(self):
        self.configure()
        original = shell._extract

        def extract(handle, stage, entry, key, job):
            job.cancel_event.set()
            return original(handle, stage, entry, key, job)
        with patch.object(shell, '_extract', side_effect=extract):
            self.assertEqual(self.finish()['state'], 'cancelled')
        self.assertEqual(list(self.directory.iterdir()), [])
        self.probe.assert_not_called()
        self.exec.assert_not_called()

    def runtime_bundle(self):
        bundle = self.root / 'bundle' / 'bin'
        bundle.mkdir(parents=True)
        header = bytearray(128)
        header[:2] = b'MZ'
        struct.pack_into('<I', header, 60, 64)
        header[64:68] = b'PE\0\0'
        struct.pack_into('<H', header, 68, 0x8664)
        struct.pack_into('<H', header, 88, 0x20b)
        for name in shell.VC_RUNTIME:
            (bundle / name).write_bytes(header)
        manifest = {'schema_version': 'mysql-client-manifest-1.0', 'files': [
            {'path': 'bin/' + name, 'sha256': hashlib.sha256(header).hexdigest()}
            for name in shell.VC_RUNTIME]}
        (bundle.parent / 'manifest.json').write_text(json.dumps(manifest))
        return bundle, header

    def test_windows_runtime_is_copied_only_from_checked_bundle(self):
        self.configure('win32-x64')
        bundle, header = self.runtime_bundle()
        with patch.dict(os.environ, {'MYFLAMES_BUNDLED_CLIENT_DIR': str(bundle)}):
            self.assertEqual(self.finish()['state'], 'done')
        target = Path(shell.installed_bin_dir())
        for name in shell.VC_RUNTIME:
            self.assertEqual((target / name).read_bytes(), header)
        self.exec.assert_not_called()

    def test_windows_runtime_digest_mismatch_fails_without_copy_or_probe(self):
        self.configure('win32-x64')
        bundle, header = self.runtime_bundle()
        (bundle / shell.VC_RUNTIME[-1]).write_bytes(header + b'planted code')
        with patch.dict(os.environ, {'MYFLAMES_BUNDLED_CLIENT_DIR': str(bundle)}), \
                patch.object(shell.shutil, 'copyfileobj') as copy_file:
            result = self.assert_refused()
        self.assertIn('Visual C++ Redistributable', result['error'])
        copy_file.assert_not_called()

    def test_windows_runtime_link_outside_bundle_is_never_copied(self):
        self.configure('win32-x64')
        bundle, header = self.runtime_bundle()
        source = bundle / shell.VC_RUNTIME[0]
        source.unlink()
        outside = self.root / 'outside.dll'
        outside.write_bytes(header)  # Even the pinned digest cannot authorize this path.
        source.symlink_to(outside)
        with patch.dict(os.environ, {'MYFLAMES_BUNDLED_CLIENT_DIR': str(bundle)}):
            self.assertIn('Visual C++ Redistributable', self.assert_refused()['error'])

    def test_untrusted_windows_runtime_fails_before_probe(self):
        self.configure('win32-x64')
        with patch.dict(os.environ, {'MYFLAMES_BUNDLED_CLIENT_DIR': str(self.root / 'missing-bundle')}):
            result = self.assert_refused()
        self.assertIn('Microsoft Visual C++ Redistributable (x64)', result['error'])

    def test_conflicting_duplicate_paths(self):
        self.configure(extras=[('file', 'bin/mysqlsh', b'evil')])
        self.assert_refused()

    def test_case_and_unicode_aliases_refuse_and_remove_stage(self):
        for key, extras in (
                ('darwin-arm64', [('file', 'lib/A', b'a'), ('sym', 'lib/a', 'A')]),
                ('win32-x64', [('file', 'bin/X.dll', b'a'), ('file', 'bin/x.dll', b'b')]),
                ('darwin-arm64', [('file', 'lib/\u00e9', b'a'), ('file', 'lib/e\u0301', b'b')])):
            with self.subTest(key=key, extras=extras):
                self.configure(key, extras=extras)
                self.assert_refused()

    def test_implicit_ancestor_aliases_refuse_and_remove_stage(self):
        for key in ('darwin-arm64', 'win32-x64'):
            for first, alias in (('Foo', 'foo'), ('\u00e9', 'e\u0301')):
                for explicit_directory in (False, True):
                    extras = [('file', 'lib/' + first + '/a', b'a')]
                    if explicit_directory:
                        extras.append(('dir', 'lib/' + alias, b''))
                    extras.append(('file', 'lib/' + alias + '/b', b'b'))
                    with self.subTest(key=key, first=first, explicit=explicit_directory):
                        self.configure(key, extras=extras)
                        self.assertIn('conflicting file paths', self.assert_refused()['error'])

    def test_implicit_directory_can_be_declared_later_with_the_same_spelling(self):
        self.configure(extras=[('file', 'lib/Foo/a', b'a'), ('dir', 'lib/Foo', b''),
                               ('file', 'lib/Foo/b', b'b')])
        self.assertEqual(self.finish()['state'], 'done')
        target = Path(shell.installed_bin_dir()).parent
        self.assertEqual((target / 'lib/Foo/a').read_bytes(), b'a')
        self.assertEqual((target / 'lib/Foo/b').read_bytes(), b'b')

    def test_filesystem_eexist_never_merges_files_or_links(self):
        # Simulate filesystem aliases not detected by Unicode/case folding.
        self.configure(extras=[('file', 'lib/twin', b'evil')])
        original = os.open

        def open_file(path, *args, **kwargs):
            if str(path).endswith('/lib/twin'):
                raise FileExistsError('filesystem alias')
            return original(path, *args, **kwargs)
        with patch.object(shell.os, 'open', side_effect=open_file):
            self.assertIn('conflicting', self.assert_refused()['error'])
        self.configure(extras=[('sym', 'lib/mysqlsh/alias', 'libssl.3.dylib')])
        with patch.object(shell.os, 'symlink', side_effect=FileExistsError('filesystem alias')):
            self.assertIn('conflicting', self.assert_refused()['error'])

    def test_missing_structure(self):
        self.configure(omit=('share/mysqlsh/LICENSE',))
        self.assert_refused()

    def test_bad_archive_after_valid_checksum(self):
        self.configure(body=b'this is not a tarball')
        self.assert_refused()

    def test_setuid_and_writable_modes_are_stripped(self):
        self.configure(extras=[('file', 'bin/helper', b'helper', 0o7777)])
        self.assertEqual(self.finish()['state'], 'done')
        target = Path(shell.installed_bin_dir()).parent
        self.assertEqual((target / 'bin/helper').stat().st_mode & 0o7777, 0o755)

    def test_single_flight_and_cancel_download_clean_up(self):
        self.configure()
        entered, release = threading.Event(), threading.Event()
        original = self.response.read

        def read(amount=-1):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(amount)
        self.response.read = read
        first = shell.start_install()
        self.assertTrue(entered.wait(2))
        self.assertEqual(shell.start_install()['id'], first['id'])
        watchdog = shell._JOB.watchdog
        try:
            with patch.object(watchdog, '_expire', wraps=watchdog._expire) as stop:
                shell.cancel(first['id'])
                stop.assert_called_once()
        finally:
            release.set()
        self.join()
        self.assertEqual(shell.status()['job']['state'], 'cancelled')
        self.probe.assert_not_called()
        self.exec.assert_not_called()
        self.assertEqual(list(self.directory.iterdir()), [])
        with shell._acquire_install_lock(str(self.directory)):
            pass  # Cancellation released the kernel lock after cleanup.

    def test_lock_is_held_through_publish_and_released_after_failure(self):
        self.configure()
        original = shell._publish
        entered, release = threading.Event(), threading.Event()

        def publish(stage, job):
            entered.set()
            if not release.wait(3):
                raise AssertionError('Test did not release publish')
            return original(stage, job)
        with patch.object(shell, '_publish', side_effect=publish):
            shell.start_install()
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaisesRegex(shell.Refused, 'already running'):
                    shell._acquire_install_lock(str(self.directory))
            finally:
                release.set()
                self.join()
        self.assertEqual(shell._JOB.state, 'done')
        self.assertFalse((self.directory / shell._INSTALL_LOCK).exists())
        with shell._acquire_install_lock(str(self.directory)):
            pass
        self.configure(body=b'bad archive')
        self.assertEqual(self.finish()['state'], 'failed')
        self.assertFalse((self.directory / shell._INSTALL_LOCK).exists())
        with shell._acquire_install_lock(str(self.directory)):
            pass

    def test_thread_start_failure_releases_install_lock(self):
        self.configure()
        with patch.object(shell.threading.Thread, 'start', side_effect=RuntimeError('thread unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'thread unavailable'):
                shell.start_install()
        self.assertIsNone(shell._JOB)
        self.assertFalse((self.directory / shell._INSTALL_LOCK).exists())
        with shell._acquire_install_lock(str(self.directory)):
            pass
        self.network.assert_not_called()

    def test_cancel_requires_exact_active_job_id_and_stale_id_cannot_stop_new_job(self):
        entry = self.configure()
        older = shell.InstallJob('darwin-arm64', dict(entry, version=self.lock['version']), str(self.directory))
        newer = shell.InstallJob('darwin-arm64', older.entry, str(self.directory))
        shell._JOB = newer
        for bad_id in (None, '', older.id, 123):
            with self.subTest(job_id=bad_id), self.assertRaises(shell.Refused):
                shell.cancel(bad_id)
            self.assertFalse(newer.cancel_event.is_set())
        shell.cancel(newer.id)
        self.assertTrue(newer.cancel_event.is_set())

    def test_deadline_closes_transport_and_cleans(self):
        self.configure()
        with patch.object(shell, 'DOWNLOAD_DEADLINE', -1):
            self.assertIn('too long', self.assert_refused()['error'])
        self.opener.open.assert_not_called()

    def test_no_server_or_exception_text_in_error(self):
        self.configure()
        self.opener.open.side_effect = urllib.error.URLError('SECRET raw server error')
        result = self.assert_refused()
        self.assertNotIn('SECRET', result['error'])

    def test_status_is_read_only(self):
        with patch.object(shell.os, 'mkdir', side_effect=AssertionError('Status must not mkdir')):
            self.assertIsNone(shell.status()['installed'])
        self.network.assert_not_called()
        self.probe.assert_not_called()
        self.exec.assert_not_called()

    def test_invalid_pointer_or_record_never_probed_or_preferred(self):
        self.configure()
        self.assertEqual(self.finish()['state'], 'done')
        target = Path(shell.installed_bin_dir()).parent
        pointer_path = self.directory / 'installed.json'
        record_path = target / 'myflames-shell.json'
        pointer = pointer_path.read_bytes()
        record = record_path.read_bytes()
        self.probe.reset_mock()
        self.network.reset_mock()
        for invalid in ('escape', 'wrong_hash', 'missing_record', 'outside_folder', 'untrusted_code'):
            with self.subTest(invalid=invalid):
                pointer_path.write_bytes(pointer)
                record_path.write_bytes(record)
                outside = self.root / 'outside-install'
                if invalid == 'escape':
                    value = json.loads(pointer)
                    value['folder'] = '../../tmp/x'
                    pointer_path.write_text(json.dumps(value))
                elif invalid == 'wrong_hash':
                    value = json.loads(record)
                    value['sha256'] = '0' * 64
                    record_path.write_text(json.dumps(value))
                elif invalid == 'missing_record':
                    record_path.unlink()
                elif invalid == 'outside_folder':
                    target.rename(outside)
                    target.symlink_to(outside, target_is_directory=True)
                else:
                    (target / 'lib/mysqlsh/libssl.3.dylib').chmod(0o777)
                try:
                    self.assertIsNone(shell.installed_bin_dir())
                    self.assertIsNone(shell.status()['installed'])
                    self.probe.assert_not_called()
                    self.network.assert_not_called()
                    self.exec.assert_not_called()
                finally:
                    if invalid == 'outside_folder':
                        target.unlink()
                        outside.rename(target)
                    (target / 'lib/mysqlsh/libssl.3.dylib').chmod(0o644)

    def test_lock_bump_does_not_report_old_install_as_done(self):
        self.configure()
        self.assertEqual(self.finish()['state'], 'done')
        self.configure(extras=[('file', 'newfile', b'new version')])
        self.assertIsNone(shell.installed_bin_dir())
        self.assertEqual(self.finish()['state'], 'done')
        self.assertEqual(self.opener.open.call_count, 2)

    def test_status_polling_caches_walk_but_install_idempotency_checks_fresh(self):
        self.configure()
        self.assertEqual(self.finish()['state'], 'done')
        with patch.object(shell.connector, '_check_shell_tree_trust',
                          wraps=shell.connector._check_shell_tree_trust) as walk:
            shell.status()
            shell.status()
            self.assertEqual(walk.call_count, 0, 'finish already populated the status cache')
            self.assertEqual(shell.start_install()['state'], 'done')
            self.assertEqual(walk.call_count, 1, 'Already installed requires a fresh trust walk')
        self.assertFalse((self.directory / shell._INSTALL_LOCK).exists())

    def test_stale_files_and_links_cleaned_only_inside_private_folder(self):
        self.configure()
        self.directory.mkdir(mode=0o700)
        sentinel = self.root / 'keep'
        sentinel.write_bytes(b'original')
        (self.directory / '.mysqlsh-download-abcdefgh.part').symlink_to(sentinel)
        stage = self.directory / '.mysqlsh-stage-abcdefgh'
        stage.mkdir(mode=0o700)
        (stage / 'link').symlink_to(self.root)
        probe = self.directory / '.mysqlsh-probe-abcdefgh'
        probe.mkdir(mode=0o700)
        (probe / 'link').symlink_to(self.root)
        retired = self.directory / ('.retire-' + 'a' * 32)
        retired.mkdir(mode=0o700)
        (retired / 'old-library').write_bytes(b'old')
        (retired / 'link').symlink_to(self.root)
        retired_link = self.directory / ('.retire-' + 'b' * 32)
        retired_link.symlink_to(self.root, target_is_directory=True)
        probe_link = self.directory / '.mysqlsh-probe-link'
        probe_link.symlink_to(self.root, target_is_directory=True)
        (self.directory / 'notes.txt').write_text('mine')
        self.assertEqual(self.finish()['state'], 'done')
        self.assertEqual(sentinel.read_bytes(), b'original')
        self.assertEqual((self.directory / 'notes.txt').read_text(), 'mine')
        self.assertFalse(stage.exists())
        for stale in (probe, retired, retired_link, probe_link):
            self.assertFalse(os.path.lexists(str(stale)))

    def test_previous_install_intact_on_bad_download(self):
        self.configure()
        self.assertEqual(self.finish()['state'], 'done')
        bin_dir = Path(shell.installed_bin_dir())
        pointer = (self.directory / 'installed.json').read_bytes()
        self.configure(extras=[('file', 'newfile', b'upgrade')])
        self.lock['archives']['darwin-arm64']['sha256'] = '0' * 64
        self.probe.reset_mock()
        self.assertEqual(self.finish()['state'], 'failed')
        self.assertEqual((self.directory / 'installed.json').read_bytes(), pointer)
        self.assertEqual((bin_dir / 'mysqlsh').read_bytes(), b'program')
        self.probe.assert_not_called()
        self.exec.assert_not_called()

    def test_failed_pointer_publish_rolls_back_same_version(self):
        self.configure()
        self.assertEqual(self.finish()['state'], 'done')
        pointer = (self.directory / 'installed.json').read_bytes()
        target = Path(shell.installed_bin_dir()).parent
        self.configure(extras=[('file', 'newfile', b'new')])
        original = shell.os.replace

        def replace(source, dest):
            if str(dest).endswith('installed.json'):
                raise OSError('secret filesystem detail')
            return original(source, dest)
        with patch.object(shell.os, 'replace', side_effect=replace):
            self.assertEqual(self.finish()['state'], 'failed')
        self.assertEqual((self.directory / 'installed.json').read_bytes(), pointer)
        self.assertEqual((target / 'bin/mysqlsh').read_bytes(), b'program')
        self.assertFalse((target / 'newfile').exists())
        self.assertFalse(any(self.directory.glob('.retire-*')))
        self.assertFalse(any(self.directory.glob('.mysqlsh-*')))
        self.exec.assert_not_called()

    def test_redirects_refused_without_reading_body(self):
        for location in ('http://cdn.mysql.com/Downloads/MySQL-Shell/mysql-shell-evil.tar.gz',
                         'https://evil.example/mysql-shell-evil.tar.gz',
                         'https://cdn.mysql.com:443/Downloads/MySQL-Shell/mysql-shell-evil.tar.gz',
                         'https://cdn.mysql.com/Downloads/MySQL-Shell/../mysql-shell-evil.tar.gz',
                         'https://cdn.mysql.com/Downloads/MySQL-Shell/mysql-shell-%2e%2e.tar.gz',
                         'https://cdn.mysql.com/Downloads/MySQL-Shell/child/../mysql-shell-evil.tar.gz',
                         './mysql-shell-evil.tar.gz'):
            with self.subTest(location=location):
                entry = self.configure()
                body = Response(b'', entry['url'])
                body.read = Mock(side_effect=AssertionError('Redirect body must not be read'))
                headers = Message()
                headers['location'] = location
                handler = shell._ShellRedirectHandler()
                handler.parent = Mock()

                def redirect(request, timeout):
                    request.timeout = timeout
                    return handler.http_error_302(request, body, 302, 'moved', headers)
                self.opener.open.side_effect = redirect
                self.assert_refused()
                body.read.assert_not_called()
                self.assertTrue(body.closed)
                handler.parent.open.assert_not_called()

    def test_allowed_redirect_closes_body_and_preserves_timeout(self):
        entry = self.configure()
        response = Response(b'', entry['url'])
        handler = shell._ShellRedirectHandler()
        handler.parent = Mock()
        request = urllib.request.Request(entry['url'])
        request.timeout = 15
        headers = Message()
        headers['location'] = entry['url'] + '.mirror'
        handler.http_error_302(request, response, 302, 'moved', headers)
        self.assertTrue(response.closed)
        self.assertEqual(response.read_count, 0)
        self.assertEqual(handler.parent.open.call_args[1]['timeout'], 15)


class WindowsToolsDirectoryTests(unittest.TestCase):
    """Exercise Windows folder policy without a Windows filesystem or sockets."""
    def setUp(self):
        self.directory = r'C:\Users\tester\AppData\Local\myflames\tools'
        self.windows = Mock(wraps=os)
        self.windows.name = 'nt'
        self.windows.path = Mock(wraps=ntpath)
        self.windows.path.realpath.side_effect = ntpath.normpath
        self.windows.path.lexists.return_value = True
        self.windows.environ = {'MYFLAMES_TOOLS_DIR': self.directory,
                                'USERPROFILE': r'C:\Users\tester'}
        self.info = os.stat_result((stat.S_IFDIR | 0o700, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        self.windows.lstat.return_value = self.info

    def test_windows_plain_directory_does_not_use_executable_name_trust_check(self):
        with patch.object(shell, 'os', self.windows), patch.object(shell.sys, 'platform', 'win32'), \
                patch.object(shell.connector, '_check_client_trust', side_effect=AssertionError('Not an executable')):
            self.assertEqual(shell._directories(), ntpath.join(self.directory, 'mysqlsh'))

    def test_junctioned_environment_root_does_not_veto_clean_user_profile(self):
        bad_root = r'C:\JunctionLocalAppData'
        self.windows.environ['LOCALAPPDATA'] = bad_root
        reparse = type('Reparse', (), {'st_mode': stat.S_IFDIR | 0o700,
                                      'st_file_attributes': 0x400})()
        for failure in (reparse, PermissionError('unreadable environment root')):
            def lstat(path):
                if ntpath.normcase(path) == ntpath.normcase(bad_root):
                    if isinstance(failure, OSError):
                        raise failure
                    return failure
                return self.info
            self.windows.lstat.side_effect = lstat
            self.windows.path.realpath.reset_mock()
            with self.subTest(failure=failure), patch.object(shell, 'os', self.windows), \
                    patch.object(shell.sys, 'platform', 'win32'):
                self.assertEqual(shell._directories(), ntpath.join(self.directory, 'mysqlsh'))
            self.assertNotIn(bad_root, [call[0][0] for call in self.windows.path.realpath.call_args_list])

    def test_windows_location_links_and_custom_data_refuse_before_network(self):
        reparse = type('Reparse', (), {'st_mode': stat.S_IFDIR | 0o700,
                                      'st_file_attributes': 0x400})()
        for cause in ('outside', 'unc', 'junction', 'symlink', 'custom_data'):
            self.windows.environ = {'MYFLAMES_TOOLS_DIR': self.directory,
                                    'USERPROFILE': r'C:\Users\tester'}
            self.windows.lstat.return_value = self.info
            if cause == 'outside':
                self.windows.environ['MYFLAMES_TOOLS_DIR'] = r'D:\outside\tools'
            elif cause == 'unc':
                self.windows.environ['MYFLAMES_TOOLS_DIR'] = r'\\server\share\tools'
            elif cause == 'junction':
                self.windows.lstat.return_value = reparse
            elif cause == 'symlink':
                self.windows.lstat.return_value = os.stat_result((stat.S_IFLNK | 0o700, 0, 0, 1, 0, 0, 0, 0, 0, 0))
            else:
                self.windows.environ['MYFLAMES_DESKTOP_DATA'] = r'C:\custom'
            with self.subTest(cause=cause), patch.object(shell, 'os', self.windows), \
                    patch.object(shell.sys, 'platform', 'win32'), patch.object(shell, 'host_platform', return_value='win32-x64'), \
                    patch.object(shell, '_JOB', None), patch.object(shell.urllib.request, 'build_opener') as network:
                with self.assertRaises(shell.Refused):
                    shell.start_install()
                network.assert_not_called()


class InstallLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='myflames-shell-lock-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tools = self.root / 'tools'
        self.tools.mkdir(mode=0o700)
        self.directory = self.tools / 'mysqlsh'
        self.directory.mkdir(mode=0o700)
        environment = patch.dict(os.environ, {'MYFLAMES_TOOLS_DIR': str(self.tools),
            'MYFLAMES_DESKTOP_DATA': '', 'MYFLAMES_BUNDLED_CLIENT_DIR': ''})
        environment.start()
        self.addCleanup(environment.stop)

    def test_other_backend_cannot_clean_live_entries_and_crash_releases_lock(self):
        # A real separate process owns both the lock and its partial artifacts.
        # It exits without closing its handle to exercise kernel stale recovery.
        script = (
            'import os, pathlib, sys\n'
            'from myflames import managed_shell as shell\n'
            'directory = pathlib.Path(sys.argv[1])\n'
            'handle = shell._acquire_install_lock(str(directory))\n'
            '(directory / ".mysqlsh-download-live.part").write_bytes(b"live")\n'
            '(directory / ".mysqlsh-stage-live").mkdir()\n'
            '(directory / ".ready").write_text("ready")\n'
            'sys.stdin.read(1)\n'
            'os._exit(0)\n')
        process = subprocess.Popen([sys.executable, '-c', script, str(self.directory)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        try:
            deadline = time.monotonic() + 5
            while not (self.directory / '.ready').exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.025)
            self.assertTrue((self.directory / '.ready').exists(), 'Lock holder did not become ready')
            with patch.object(shell, '_JOB', None), patch.object(shell, 'host_platform', return_value='darwin-arm64'), \
                    patch.object(shell.urllib.request, 'build_opener') as network, \
                    patch.object(shell, '_clean_stale', wraps=shell._clean_stale) as clean:
                with self.assertRaisesRegex(shell.Refused, 'already running in another myflames process'):
                    shell.start_install()
                network.assert_not_called()
                clean.assert_not_called()
                self.assertIsNone(shell._JOB)
            self.assertEqual((self.directory / '.mysqlsh-download-live.part').read_bytes(), b'live')
            self.assertTrue((self.directory / '.mysqlsh-stage-live').is_dir())
            self.assertEqual((self.directory / shell._INSTALL_LOCK).read_text(), '%d\n' % process.pid)
        finally:
            try:
                stdout, stderr = process.communicate('x', timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
        self.assertEqual(process.returncode, 0, stderr)
        with patch.object(shell, '_JOB', None), patch.object(shell, 'host_platform', return_value='darwin-arm64'), \
                patch.object(shell, '_download', side_effect=shell.Refused('Test stops before network.')):
            shell.start_install()
            shell._JOB.thread.join(5)
            self.assertFalse(shell._JOB.thread.is_alive())
            self.assertEqual(shell._JOB.state, 'failed')
        self.assertFalse((self.directory / '.mysqlsh-download-live.part').exists())
        self.assertFalse((self.directory / '.mysqlsh-stage-live').exists())
        self.assertFalse((self.directory / shell._INSTALL_LOCK).exists())
        with shell._acquire_install_lock(str(self.directory)) as handle:
            inode = os.fstat(handle.fileno()).st_ino
            self.assertEqual((self.directory / shell._INSTALL_LOCK).read_text(), '%d\n' % os.getpid())
        self.assertEqual((self.directory / shell._INSTALL_LOCK).stat().st_ino, inode)

    @unittest.skipIf(os.name == 'nt', 'POSIX unlink and flock race')
    def test_waiter_retries_if_previous_owner_removed_open_lock(self):
        import fcntl
        path = self.directory / shell._INSTALL_LOCK
        flock = fcntl.flock
        old_descriptors = []

        def remove_before_recheck(descriptor, operation):
            flock(descriptor, operation)
            if not old_descriptors:
                old_descriptors.append(descriptor)
                path.unlink()  # Previous owner finished after this open.

        with patch.object(fcntl, 'flock', side_effect=remove_before_recheck):
            with shell._acquire_install_lock(str(self.directory)) as handle:
                self.assertEqual(fcntl.flock.call_count, 2)
                self.assertTrue(os.path.samestat(os.fstat(handle.fileno()), path.stat()))

    @unittest.skipIf(os.name == 'nt', 'POSIX unlink and flock race')
    def test_waiter_cannot_install_with_unlinked_lock_while_successor_owns_path(self):
        import fcntl
        path = self.directory / shell._INSTALL_LOCK
        flock = fcntl.flock
        successor = []
        replaced = False

        def replace_before_recheck(descriptor, operation):
            nonlocal replaced
            flock(descriptor, operation)
            if not replaced:
                replaced = True
                path.unlink()
                successor.append(shell._acquire_install_lock(str(self.directory)))

        try:
            with patch.object(fcntl, 'flock', side_effect=replace_before_recheck):
                with self.assertRaisesRegex(shell.Refused, 'already running'):
                    shell._acquire_install_lock(str(self.directory))
            self.assertEqual(path.read_text(), '%d\n' % os.getpid())
            with self.assertRaisesRegex(shell.Refused, 'already running'):
                shell._acquire_install_lock(str(self.directory))
        finally:
            for handle in successor:
                handle.close()

    @unittest.skipIf(os.name == 'nt', 'POSIX unlink and flock race')
    def test_release_never_removes_a_successors_lock(self):
        path = self.directory / shell._INSTALL_LOCK
        original = shell._acquire_install_lock(str(self.directory))
        job = Mock(directory=str(self.directory), install_lock=original)
        path.unlink()
        with shell._acquire_install_lock(str(self.directory)) as successor:
            shell._release_install_lock(job)
            self.assertTrue(original.closed)
            self.assertIsNone(job.install_lock)
            self.assertTrue(os.path.samestat(os.fstat(successor.fileno()), path.stat()))
            with self.assertRaisesRegex(shell.Refused, 'already running'):
                shell._acquire_install_lock(str(self.directory))

    @unittest.skipIf(os.name == 'nt', 'POSIX permissions and links')
    def test_untrusted_lock_is_never_followed_or_truncated(self):
        path = self.directory / shell._INSTALL_LOCK
        sentinel = self.root / 'keep'
        sentinel.write_bytes(b'original')
        path.symlink_to(sentinel)
        with self.assertRaisesRegex(shell.Refused, 'private regular file'):
            shell._acquire_install_lock(str(self.directory))
        self.assertEqual(sentinel.read_bytes(), b'original')
        path.unlink()
        path.write_bytes(b'original')
        path.chmod(0o644)
        with self.assertRaisesRegex(shell.Refused, 'private regular file'):
            shell._acquire_install_lock(str(self.directory))
        self.assertEqual(path.read_bytes(), b'original')
        path.chmod(0o600)
        path.unlink()
        os.link(str(sentinel), str(path))
        with self.assertRaisesRegex(shell.Refused, 'private regular file'):
            shell._acquire_install_lock(str(self.directory))
        self.assertEqual(sentinel.read_bytes(), b'original')

    def test_windows_lock_uses_nonblocking_first_byte_and_closes_on_contention(self):
        windows = Mock(wraps=os)
        windows.name = 'nt'
        # Mock(wraps=os) wraps functions, but its constants are still Mocks.
        for name in ('O_RDWR', 'O_CREAT', 'O_NOFOLLOW', 'O_NONBLOCK', 'O_BINARY'):
            setattr(windows, name, getattr(os, name, 0))
        temporary_flag = 1 << 29
        windows.O_TEMPORARY = temporary_flag

        def open_shared(path, flags, mode):
            self.assertTrue(flags & temporary_flag, 'Windows needs atomic delete on close')
            return os.open(path, flags & ~temporary_flag, mode)
        windows.open.side_effect = open_shared
        runtime = Mock(LK_NBLCK=1)
        with patch.object(shell, 'os', windows), patch.dict(sys.modules, {'msvcrt': runtime}):
            handle = shell._acquire_install_lock(str(self.directory))
            descriptor = handle.fileno()
            runtime.locking.assert_called_once_with(descriptor, runtime.LK_NBLCK, 1)
            handle.close()
            runtime.locking.side_effect = OSError('locked')
            before = (self.directory / shell._INSTALL_LOCK).read_bytes()
            with self.assertRaisesRegex(shell.Refused, 'already running'):
                shell._acquire_install_lock(str(self.directory))
            descriptor = runtime.locking.call_args[0][0]
            with self.assertRaises(OSError):
                os.fstat(descriptor)
            self.assertEqual((self.directory / shell._INSTALL_LOCK).read_bytes(), before)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='myflames-shell-probe-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.binary = self.root / 'mysqlsh'
        self.binary.write_bytes(b'never executed')
        self.binary.chmod(0o700)
        self.entry = dict(shell.load_lock()['archives']['darwin-arm64'])
        self.job = shell.InstallJob('darwin-arm64', self.entry, str(self.root))

    def process(self, stdout=b'mysqlsh   Ver 9.7.0 for macos on arm64\n', returncode=0):
        process = Mock()
        process.stdout, process.stderr = io.BytesIO(stdout), io.BytesIO(b'')
        process.returncode = returncode
        process.poll.return_value = returncode
        process.wait.return_value = returncode
        return process

    def run_probe(self, process):
        with patch.object(shell.subprocess, 'Popen', return_value=process) as spawn, patch.object(shell, '_kill_probe') as kill:
            shell._probe_version(str(self.binary), self.entry, self.job)
        kill.assert_called_once()
        process.wait.assert_called_once()
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)
        return spawn

    def test_real_probe_logic_uses_clean_environment_and_exact_banner(self):
        with patch.dict(os.environ, {'MYSQL_PWD': 'secret', 'PYTHONPATH': '/evil', 'DYLD_INSERT_LIBRARIES': '/evil'}):
            spawn = self.run_probe(self.process())
        argv = spawn.call_args[0][0]
        self.assertEqual(argv, [str(self.binary.resolve()), '--no-defaults', '--disable-plugins',
                                '--disable-builtin-plugins', '--version'])
        options = spawn.call_args[1]
        for name in ('MYSQL_PWD', 'PYTHONPATH', 'DYLD_INSERT_LIBRARIES'):
            self.assertNotIn(name, options['env'])
        self.assertEqual(options['env']['MYSQLSH_USER_CONFIG_HOME'], options['env']['HOME'])
        self.assertEqual(options['env']['OPENSSL_CONF'], os.devnull)
        self.assertFalse(Path(options['env']['HOME']).exists(), 'Probe home is removed')

    def test_probe_failure_never_exposes_child_text(self):
        for body, code in [(b'mysqlsh 9.6.0 SECRET', 0), (b'SECRET raw output', 1)]:
            with self.subTest(code=code):
                with self.assertRaises(shell.Refused) as caught:
                    self.run_probe(self.process(body, code))
                self.assertNotIn('SECRET', str(caught.exception))

    def test_windows_missing_runtime_is_named(self):
        self.job.platform = 'win32-x64'
        with self.assertRaises(shell.Refused) as caught:
            self.run_probe(self.process(b'', -1073741515))
        self.assertIn('Microsoft Visual C++ Redistributable (x64)', str(caught.exception))

    def test_cancel_stops_and_reaps_a_probe_in_progress(self):
        process = self.process()
        process.poll.return_value = None
        polls = threading.Event()

        def poll():
            polls.set()
            self.job.cancel_event.set()
            return None
        process.poll.side_effect = poll
        with patch.object(shell.subprocess, 'Popen', return_value=process) as spawn, patch.object(shell, '_kill_probe') as kill:
            with self.assertRaises(shell._Cancelled):
                shell._probe_version(str(self.binary), self.entry, self.job)
        self.assertTrue(polls.is_set())
        spawn.assert_called_once()
        kill.assert_called_once()
        process.wait.assert_called_once()
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)


class LockAndBinaryTests(unittest.TestCase):
    def test_shipped_lock_and_all_field_validation(self):
        original = shell.load_lock()
        self.assertEqual(original['archives']['win32-x64']['size'], 200100890)
        changes = [('schema_version', 'wrong'), ('version', '../9.7.0'), ('product', 'evil'),
                   ('signing_key', 'A' * 40), ('license_url', 'https://evil.example/'),
                   ('downloads_url', 'https://dev.mysql.com.evil/downloads/shell/'), ('macos_team_id', 'ignored')]
        for field, value in changes:
            with self.subTest(field=field):
                lock = copy.deepcopy(original)
                lock[field] = value
                with self.assertRaises(shell.Refused):
                    shell.validate_lock(lock)

    def test_archive_entry_field_validation(self):
        original = shell.load_lock()
        bad_entries = [('size', True), ('size', 0), ('size', shell.MAX_SHELL_ARCHIVE_BYTES + 1),
                       ('sha256', 'A' * 64), ('sha256', 'a' * 63), ('md5', '0' * 31),
                       ('root', '../escape'), ('root', '/root'), ('root', 'mysql-shell-9.7.0-macos15-x86-64bit'),
                       ('url', 'https://cdn.mysql.com.evil/Downloads/MySQL-Shell/mysql-shell-evil.tar.gz'),
                       ('url', original['archives']['darwin-arm64']['url'] + '?token=x'),
                       ('url', original['archives']['darwin-arm64']['url'] + '#x'),
                       ('url', original['archives']['darwin-arm64']['url'].replace('cdn.mysql.com', 'user@cdn.mysql.com')),
                       ('url', original['archives']['darwin-arm64']['url'].replace('cdn.mysql.com', 'cdn.mysql.com:443'))]
        for field, value in bad_entries:
            with self.subTest(entry_field=field, value=value):
                lock = copy.deepcopy(original)
                lock['archives']['darwin-arm64'][field] = value
                with self.assertRaises(shell.Refused):
                    shell.validate_lock(lock)

    def test_license_and_download_links_must_stay_on_allowed_https_hosts(self):
        original = shell.load_lock()
        for field in ('license_url', 'downloads_url'):
            for url in ('http://dev.mysql.com/downloads/shell/', 'https://evil.example/',
                        'https://user@www.mysql.com/', 'https://www.mysql.com.evil/',
                        'https://dev.mysql.com:443/downloads/shell/', '//dev.mysql.com/downloads/shell/'):
                with self.subTest(field=field, url=url):
                    lock = copy.deepcopy(original)
                    lock[field] = url
                    with self.assertRaises(shell.Refused):
                        shell.validate_lock(lock)

    def test_host_mapping(self):
        for system, machine, expected in [('darwin', 'arm64', 'darwin-arm64'), ('darwin', 'x86_64', 'darwin-x64'),
                ('win32', 'AMD64', 'win32-x64'), ('win32', 'ARM64', None), ('linux', 'x86_64', None)]:
            with self.subTest(system=system, machine=machine), patch.object(shell.sys, 'platform', system), patch.object(shell.platform, 'machine', return_value=machine):
                self.assertEqual(shell.host_platform(), expected)

    def test_binary_formats_without_executing_anything(self):
        with tempfile.TemporaryDirectory() as temp:
            binary = Path(temp) / 'mysqlsh'
            for key, cpu in [('darwin-arm64', 0x0100000c), ('darwin-x64', 0x01000007)]:
                binary.write_bytes(struct.pack('<II', 0xfeedfacf, cpu) + b'\0' * 24)
                shell._check_binary(str(binary), key)
                with self.assertRaises(shell.Refused):
                    shell._check_binary(str(binary), 'darwin-x64' if key.endswith('arm64') else 'darwin-arm64')
            exe = Path(temp) / 'mysqlsh.exe'
            header = bytearray(128)
            header[:2] = b'MZ'
            struct.pack_into('<I', header, 60, 64)
            header[64:68] = b'PE\0\0'
            struct.pack_into('<H', header, 68, 0x8664)
            struct.pack_into('<H', header, 88, 0x20b)
            exe.write_bytes(header)
            shell._check_pe(str(exe))
            for offset, value in [(68, 0x14c), (88, 0x10b)]:
                broken = bytearray(header)
                struct.pack_into('<H', broken, offset, value)
                exe.write_bytes(broken)
                with self.assertRaises(shell.Refused):
                    shell._check_pe(str(exe))


if __name__ == '__main__':
    unittest.main()
