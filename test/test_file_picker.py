"""Tests for myflames.file_picker (the native CSV file chooser and the
typed-path check). Every platform helper is mocked; no dialog opens. Files
live in a throwaway HOME (vault secret backend, no keychain)."""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from myflames import file_picker, folder_picker  # noqa: E402


def recording_run(stdout='', returncode=0, stderr=''):
    calls = []

    def run(argv, **kwargs):
        calls.append({'argv': argv, 'kwargs': kwargs})
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)
    run.calls = calls
    return run


class FilePickerTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        env = mock.patch.dict(os.environ, {'HOME': self.home.name, 'USERPROFILE': self.home.name,
                                          'MYFLAMES_SECRET_BACKEND': 'vault'})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        self.path = os.path.join(self.home.name, 'orders file.csv')
        with open(self.path, 'w') as handle:
            handle.write('a,b\n1,2\n')

    def choose(self, platform, run):
        with mock.patch('sys.platform', platform), mock.patch('myflames.file_picker.subprocess.run', side_effect=run):
            return file_picker.choose_file()

    def test_takes_no_arguments(self):
        import inspect
        self.assertEqual(list(inspect.signature(file_picker.choose_file).parameters), [])

    def test_macos_selection_and_fixed_script(self):
        run = recording_run(self.path + '\n')
        self.assertEqual(self.choose('darwin', run), {'path': os.path.realpath(self.path), 'cancelled': False})
        call = run.calls[0]
        self.assertEqual(call['argv'], ['/usr/bin/osascript'])
        self.assertIn('choose file', call['kwargs']['input'])
        self.assertIn('"csv", "tsv", "txt"', call['kwargs']['input'])
        self.assertEqual(call['kwargs']['timeout'], 120)

    def test_cancel(self):
        self.assertEqual(self.choose('darwin', recording_run('\n')), {'path': None, 'cancelled': True})
        self.assertEqual(self.choose('linux', recording_run('', returncode=1)), {'path': None, 'cancelled': True})

    def test_windows_uses_an_open_file_dialog(self):
        run = recording_run(self.path + '\r\n')
        with mock.patch.dict(os.environ, {'SystemRoot': 'C:\\Windows'}):
            self.assertEqual(self.choose('win32', run)['path'], os.path.realpath(self.path))
        argv = run.calls[0]['argv']
        self.assertTrue(argv[0].endswith('powershell.exe'))
        self.assertIn('OpenFileDialog', argv[-1])
        self.assertIn('*.csv;*.tsv;*.txt', argv[-1])

    def test_linux_zenity_filters(self):
        run = recording_run(self.path + '\n')
        self.choose('linux', run)
        self.assertIn('--file-filter=CSV, TSV and text files | *.csv *.tsv *.txt', run.calls[0]['argv'])

    def test_failures_point_at_the_typed_path(self):
        cases = [(recording_run('relative.csv\n'), 'relative'), (recording_run(self.home.name + '\n'), 'folder'),
                 (recording_run('/no/such/file.csv\n'), 'No file'), (recording_run('/x\x07y\n'), 'control'),
                 (recording_run('', returncode=1, stderr='boom'), 'status 1')]
        for run, words in cases:
            with self.subTest(words=words), self.assertRaisesRegex(ValueError, words) as caught:
                self.choose('darwin', run)
            self.assertIn('Type the file path instead.', str(caught.exception))
        with self.assertRaisesRegex(ValueError, 'timed out'):
            self.choose('darwin', mock.Mock(side_effect=subprocess.TimeoutExpired('osascript', 120)))
        with self.assertRaisesRegex(ValueError, 'Could not start'):
            self.choose('darwin', mock.Mock(side_effect=OSError('missing')))
        with self.assertRaisesRegex(ValueError, 'No native file chooser'):
            self.choose('sunos5', recording_run(''))

    def test_one_dialog_at_a_time(self):
        self.assertTrue(folder_picker._DIALOG_LOCK.acquire(blocking=False))
        try:
            with self.assertRaisesRegex(ValueError, 'already open'):
                self.choose('darwin', recording_run(self.path))
        finally:
            folder_picker._DIALOG_LOCK.release()

    def test_check_file(self):
        self.assertEqual(file_picker.check_file(self.path), os.path.realpath(self.path))
        self.assertEqual(file_picker.check_file('  %s  ' % self.path), os.path.realpath(self.path))
        for value, words in (('', 'full path'), (None, 'full path'), ('x.csv', 'full path'),
                             (self.home.name, 'folder'), ('/no/such.csv', 'No file'), ('/a\x00b', 'characters')):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, words):
                file_picker.check_file(value)
        if hasattr(os, 'mkfifo'):
            fifo = os.path.join(self.home.name, 'pipe')
            os.mkfifo(fifo)
            with self.assertRaisesRegex(ValueError, 'not a regular file'):
                file_picker.check_file(fifo)
        big = os.stat(self.path)
        fake = os.stat_result((big.st_mode, big.st_ino, big.st_dev, big.st_nlink, big.st_uid, big.st_gid,
                               file_picker.MAX_BYTES + 1, int(big.st_atime), int(big.st_mtime), int(big.st_ctime)))
        with mock.patch('myflames.file_picker.os.stat', return_value=fake):
            with self.assertRaisesRegex(ValueError, '4 GiB'):
                file_picker.check_file(self.path)
        with mock.patch('myflames.file_picker.os.access', return_value=False):
            with self.assertRaisesRegex(ValueError, 'cannot read'):
                file_picker.check_file(self.path)


if __name__ == '__main__':
    unittest.main()
