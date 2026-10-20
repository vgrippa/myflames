"""Tests for myflames.folder_picker (native directory chooser).

Every platform launcher is mocked; no real dialog is ever started and no real
helper executable is invoked. Filesystem checks run against temporary
directories under a throwaway HOME (with USERPROFILE and the vault secret
backend set, and MYFLAMES_VAULT_PASSPHRASE popped) so ~/.myflames and the OS
keychain are never touched. Windows-path plumbing is exercised with paths that
are valid on the test host; on a real Windows host os.path is ntpath and the
same code accepts native C:\\ paths.
"""
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from myflames import folder_picker


def fake_run(stdout='', returncode=0, stderr=''):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout,
                                           stderr=stderr)
    return run


def recording_run(stdout='', returncode=0, stderr=''):
    calls = []

    def run(argv, **kwargs):
        calls.append({'argv': argv, 'kwargs': kwargs})
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout,
                                           stderr=stderr)
    run.calls = calls
    return run


class FolderPickerTest(unittest.TestCase):
    def test_undecodable_path_is_not_replaced_with_another_name(self):
        with mock.patch('sys.platform', 'darwin'), mock.patch(
                'myflames.folder_picker.subprocess.run',
                side_effect=UnicodeDecodeError('utf8', b'\xff', 0, 1, 'invalid')):
            with self.assertRaisesRegex(ValueError, 'unreadable path'):
                folder_picker.choose_directory()

    def test_linux_cancel_exit_with_output_is_not_a_selection(self):
        proc = subprocess.CompletedProcess([], 1, stdout='/tmp\n', stderr='')
        with mock.patch('sys.platform', 'linux'), mock.patch(
                'myflames.folder_picker.subprocess.run', return_value=proc):
            with self.assertRaisesRegex(ValueError, 'status 1'):
                folder_picker.choose_directory()

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.env = mock.patch.dict(os.environ, {
            'HOME': self.home.name,
            'USERPROFILE': self.home.name,
            'MYFLAMES_SECRET_BACKEND': 'vault',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)

    def make_dir(self, name):
        path = os.path.join(self.home.name, name)
        os.mkdir(path)
        return path

    def choose(self, platform, run):
        with mock.patch('sys.platform', platform), \
                mock.patch('myflames.folder_picker.subprocess.run', side_effect=run):
            return folder_picker.choose_directory()

    # -- contract shape -----------------------------------------------------

    def test_takes_no_arguments(self):
        import inspect
        self.assertEqual(list(inspect.signature(folder_picker.choose_directory).parameters), [])

    def test_result_shape(self):
        target = self.make_dir('picked')
        result = self.choose('darwin', fake_run(stdout=target + '\n'))
        self.assertEqual(result, {'path': target, 'cancelled': False})
        self.assertIs(result['cancelled'], False)

    # -- macOS ---------------------------------------------------------------

    def test_macos_success_preserves_spaces_unicode_quotes(self):
        target = self.make_dir("d\u00e9r 'q' sp ace")
        run = recording_run(stdout=target + '\n')
        result = self.choose('darwin', run)
        self.assertEqual(result, {'path': target, 'cancelled': False})
        call = run.calls[0]
        self.assertEqual(call['argv'], ['/usr/bin/osascript'])
        script = call['kwargs']['input']
        self.assertIn('choose folder with prompt', script)
        self.assertIn('POSIX path', script)
        self.assertEqual(call['kwargs'].get('timeout'), 120)
        self.assertIsNot(call['kwargs'].get('shell'), True)

    def test_macos_crlf_terminator_stripped(self):
        target = self.make_dir('crlf')
        result = self.choose('darwin', fake_run(stdout=target + '\r\n'))
        self.assertEqual(result['path'], target)

    def test_macos_cancel_empty_output(self):
        result = self.choose('darwin', fake_run(stdout='\n'))
        self.assertEqual(result, {'path': None, 'cancelled': True})

    def test_macos_nonzero_exit_is_failure(self):
        run = fake_run(stdout='', returncode=1, stderr='execution error: nope (-600)')
        with self.assertRaises(ValueError) as ctx:
            self.choose('darwin', run)
        self.assertIn('manual', str(ctx.exception))
        self.assertIn('status 1', str(ctx.exception))

    def test_macos_launch_failure(self):
        def run(argv, **kwargs):
            raise FileNotFoundError(2, 'No such file or directory', argv[0])
        with self.assertRaises(ValueError) as ctx:
            self.choose('darwin', run)
        self.assertIn('manual', str(ctx.exception))

    def test_macos_timeout(self):
        def run(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=120)
        with self.assertRaises(ValueError) as ctx:
            self.choose('darwin', run)
        self.assertIn('timed out', str(ctx.exception))

    def test_macos_relative_path_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.choose('darwin', fake_run(stdout='some/relative/dir\n'))
        self.assertIn('relative', str(ctx.exception))

    def test_macos_nonexistent_directory_rejected(self):
        missing = os.path.join(self.home.name, 'missing')
        with self.assertRaises(ValueError) as ctx:
            self.choose('darwin', fake_run(stdout=missing + '\n'))
        self.assertIn('does not exist', str(ctx.exception))

    def test_macos_control_characters_rejected(self):
        target = self.make_dir('plain')
        for bad in ('\x07', '\x1b', '\x7f'):
            with self.assertRaises(ValueError) as ctx:
                self.choose('darwin', fake_run(stdout=target + bad + '\n'))
            self.assertIn('control', str(ctx.exception))

    # -- Windows --------------------------------------------------------------

    def test_windows_success_uses_fixed_powershell(self):
        target = self.make_dir('win picked')
        run = recording_run(stdout=target + '\r\n')
        result = self.choose('win32', run)
        self.assertEqual(result, {'path': target, 'cancelled': False})
        argv = run.calls[0]['argv']
        self.assertTrue(argv[0].replace('/', '\\').endswith(
            os.path.join('System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe').replace('/', '\\')))
        for flag in ('-NoProfile', '-NonInteractive', '-STA', '-Command'):
            self.assertIn(flag, argv)
        script = argv[-1]
        self.assertIn('System.Windows.Forms', script)
        self.assertIn('FolderBrowserDialog', script)
        self.assertIn('UTF8', script)
        self.assertEqual(run.calls[0]['kwargs'].get('timeout'), 120)
        self.assertIsNot(run.calls[0]['kwargs'].get('shell'), True)

    def test_windows_cancel_empty_output(self):
        result = self.choose('win32', fake_run(stdout=''))
        self.assertEqual(result, {'path': None, 'cancelled': True})

    def test_windows_unicode_path_preserved(self):
        target = self.make_dir('\u00fc\u00f1\u00ef code')
        result = self.choose('win32', fake_run(stdout=target + '\n'))
        self.assertEqual(result['path'], target)

    def test_windows_launch_failure(self):
        def run(argv, **kwargs):
            raise OSError(13, 'Access denied', argv[0])
        with self.assertRaises(ValueError) as ctx:
            self.choose('win32', run)
        self.assertIn('manual', str(ctx.exception))

    # -- Linux ----------------------------------------------------------------

    def test_linux_zenity_success(self):
        target = self.make_dir('zenity pick')
        run = recording_run(stdout=target + '\n')
        result = self.choose('linux', run)
        self.assertEqual(result, {'path': target, 'cancelled': False})
        self.assertEqual(run.calls[0]['argv'], [
            'zenity', '--file-selection', '--directory',
            '--title=Choose a directory for myflames',
        ])

    def test_linux_zenity_cancel_exit_code_one(self):
        result = self.choose('linux', fake_run(stdout='', returncode=1))
        self.assertEqual(result, {'path': None, 'cancelled': True})

    def test_linux_zenity_not_installed(self):
        def run(argv, **kwargs):
            raise FileNotFoundError(2, 'No such file or directory', argv[0])
        with self.assertRaises(ValueError) as ctx:
            self.choose('linux', run)
        self.assertIn('manual', str(ctx.exception))

    def test_linux_zenity_unexpected_error(self):
        with self.assertRaises(ValueError):
            self.choose('linux', fake_run(stdout='', returncode=255, stderr='boom'))

    # -- platform support ------------------------------------------------------

    def test_unsupported_platform(self):
        def run(argv, **kwargs):
            raise AssertionError('no helper may be launched on an unsupported platform')
        with self.assertRaises(ValueError) as ctx:
            self.choose('aix', run)
        self.assertIn('unsupported', str(ctx.exception))

    # -- single-flight lock -----------------------------------------------------

    def test_busy_when_lock_held(self):
        folder_picker._DIALOG_LOCK.acquire()
        try:
            with self.assertRaises(ValueError) as ctx:
                self.choose('darwin', lambda argv, **kwargs: self.fail('must not launch'))
            self.assertIn('already open', str(ctx.exception))
        finally:
            folder_picker._DIALOG_LOCK.release()
        target = self.make_dir('after-busy')
        result = self.choose('darwin', fake_run(stdout=target + '\n'))
        self.assertEqual(result['path'], target)

    def test_lock_released_after_timeout(self):
        def run(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=120)
        with self.assertRaises(ValueError):
            self.choose('darwin', run)
        target = self.make_dir('after-timeout')
        result = self.choose('darwin', fake_run(stdout=target + '\n'))
        self.assertEqual(result['path'], target)

    def test_second_caller_while_dialog_open_gets_busy(self):
        entered = threading.Event()
        release = threading.Event()
        target = self.make_dir('thread-pick')

        def slow_run(argv, **kwargs):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('test did not release the dialog')
            return subprocess.CompletedProcess(argv, 0, stdout=target + '\n',
                                               stderr='')

        results = []
        with mock.patch('sys.platform', 'darwin'), \
                mock.patch('myflames.folder_picker.subprocess.run', side_effect=slow_run):
            worker = threading.Thread(
                target=lambda: results.append(folder_picker.choose_directory()))
            worker.start()
            self.assertTrue(entered.wait(10))
            try:
                with self.assertRaises(ValueError) as ctx:
                    folder_picker.choose_directory()
                self.assertIn('already open', str(ctx.exception))
            finally:
                release.set()
            worker.join(10)
            self.assertFalse(worker.is_alive())
        self.assertEqual(results, [{'path': target, 'cancelled': False}])


if __name__ == '__main__':
    unittest.main()
