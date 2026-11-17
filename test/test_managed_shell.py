"""Installer trust and confinement gates with synthetic, checksum-pinned archives.

All transports are patched build_opener transports, as in test_app_update:
no sockets, Oracle downloads, gpg, or archive executables are needed.
"""
import copy
from email.message import Message
import hashlib
import io
import json
import os
from pathlib import Path
import ssl
import stat
import struct
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
    zipped = key == 'win32-x64'
    binary = 'bin/mysqlsh.exe' if zipped else 'bin/mysqlsh'
    license_file = 'share/mysqlsh/LICENSE.txt' if zipped else 'share/mysqlsh/LICENSE'
    # kind, relative name, data/target, optional mode
    entries = [('dir', '', b''), ('dir', 'bin', b''), ('dir', 'lib', b''),
               ('dir', 'lib/mysqlsh', b''), ('dir', 'share', b''),
               ('dir', 'share/mysqlsh', b''), ('file', binary, b'program', 0o755),
               ('file', license_file, b'license'),
               ('file', 'lib/mysqlsh/libssl.3.dylib', b'library')]
    entries = [e for e in entries if e[1] not in omit] + list(extras)
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
        self.assertTrue((target / 'share/mysqlsh/LICENSE.txt').is_file())
        self.probe.assert_called_once()
        self.exec.assert_not_called()

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
                  [('sym', 'lib/mysqlsh/first', 'second'), ('sym', 'lib/mysqlsh/second', 'libssl.3.dylib')],
                  [('sym', 'lib/mysqlsh/dangling', 'missing')],
                  [('sym', 'lib/mysqlsh/nul', '')],
                  [('fifo', 'lib/fifo', b'')] ]
        for extras in cases:
            with self.subTest(extras=extras):
                self.configure(extras=extras)
                self.assert_refused()

    def test_zip_symlink(self):
        self.configure('win32-x64', [('sym', 'lib/mysqlsh/evil', 'libssl.3.dylib')])
        self.assert_refused()

    def test_private_tools_and_mysqlsh_before_network(self):
        for which in (self.tools, self.directory):
            for mode in (0o755, 0o777):
                with self.subTest(folder=which.name, mode=mode):
                    self.configure()
                    which.mkdir(mode=0o700, exist_ok=True)
                    which.chmod(mode)
                    try:
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

    def test_windows_runtime_is_copied_only_from_checked_bundle(self):
        self.configure('win32-x64')
        bundle = self.root / 'bundle'
        bundle.mkdir()
        program = bundle / 'mysql.exe'
        program.write_bytes(b'client')
        header = bytearray(128)
        header[:2] = b'MZ'
        struct.pack_into('<I', header, 60, 64)
        header[64:68] = b'PE\0\0'
        struct.pack_into('<H', header, 68, 0x8664)
        struct.pack_into('<H', header, 88, 0x20b)
        for name in shell.VC_RUNTIME:
            (bundle / name).write_bytes(header)
        with patch.dict(os.environ, {'MYFLAMES_BUNDLED_CLIENT_DIR': str(bundle)}), patch.object(shell.connector, 'checked_program', return_value=str(program.resolve())) as checked:
            self.assertEqual(self.finish()['state'], 'done')
            checked.assert_called_once_with(str(program), bundled=True)
        target = Path(shell.installed_bin_dir())
        for name in shell.VC_RUNTIME:
            self.assertEqual((target / name).read_bytes(), header)
        self.exec.assert_not_called()

    def test_untrusted_windows_runtime_fails_before_probe(self):
        self.configure('win32-x64')
        with patch.dict(os.environ, {'MYFLAMES_BUNDLED_CLIENT_DIR': str(self.root / 'missing-bundle')}):
            result = self.assert_refused()
        self.assertIn('Microsoft Visual C++ Redistributable (x64)', result['error'])

    def test_conflicting_duplicate_paths(self):
        self.configure(extras=[('file', 'bin/mysqlsh', b'evil')])
        self.assert_refused()

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

    def test_stale_files_and_links_cleaned_only_inside_private_folder(self):
        self.configure()
        self.directory.mkdir(mode=0o700)
        sentinel = self.root / 'keep'
        sentinel.write_bytes(b'original')
        (self.directory / '.mysqlsh-download-abcdefgh.part').symlink_to(sentinel)
        stage = self.directory / '.mysqlsh-stage-abcdefgh'
        stage.mkdir(mode=0o700)
        (stage / 'link').symlink_to(self.root)
        (self.directory / 'notes.txt').write_text('mine')
        self.assertEqual(self.finish()['state'], 'done')
        self.assertEqual(sentinel.read_bytes(), b'original')
        self.assertEqual((self.directory / 'notes.txt').read_text(), 'mine')
        self.assertFalse(stage.exists())

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
        self.assertEqual(argv, [str(self.binary.resolve()), '--no-defaults', '--version'])
        options = spawn.call_args[1]
        for name in ('MYSQL_PWD', 'PYTHONPATH', 'DYLD_INSERT_LIBRARIES'):
            self.assertNotIn(name, options['env'])
        self.assertEqual(options['env']['MYSQLSH_USER_CONFIG_HOME'], options['env']['HOME'])
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
