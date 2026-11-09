"""Tests for the Query editor's SQL files: the .sql/.txt chooser set and the
save dialog in myflames.file_picker, the save-path validation and atomic
write, and the /api/editor/file/* endpoints in myflames.editor_session (token
mapping, decode rules, size cap). Every dialog helper is mocked; no dialog
opens. Files live in a throwaway directory."""
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from myflames import editor_session as ed  # noqa: E402
from myflames import file_picker  # noqa: E402


def recording_run(stdout='', returncode=0, stderr=''):
    calls = []

    def run(argv, **kwargs):
        calls.append({'argv': argv, 'kwargs': kwargs})
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)
    run.calls = calls
    return run


class SqlChooserTest(unittest.TestCase):
    """The second fixed file-type set (.sql/.txt), never a caller pattern."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, 'report query.sql')
        with open(self.path, 'w') as handle:
            handle.write('SELECT 1;')

    def choose(self, platform, run):
        with mock.patch('sys.platform', platform), mock.patch('myflames.file_picker.subprocess.run', side_effect=run):
            return file_picker.choose_sql_file()

    def test_macos_fixed_script(self):
        run = recording_run(self.path + '\n')
        self.assertEqual(self.choose('darwin', run), {'path': os.path.realpath(self.path), 'cancelled': False})
        call = run.calls[0]
        self.assertEqual(call['argv'], ['/usr/bin/osascript'])
        self.assertIn('choose file', call['kwargs']['input'])
        self.assertIn('"sql", "txt"', call['kwargs']['input'])
        self.assertNotIn('csv', call['kwargs']['input'])

    def test_windows_fixed_filter(self):
        run = recording_run(self.path + '\r\n')
        with mock.patch.dict(os.environ, {'SystemRoot': 'C:\\Windows'}):
            self.assertEqual(self.choose('win32', run)['path'], os.path.realpath(self.path))
        argv = run.calls[0]['argv']
        self.assertTrue(argv[0].endswith('powershell.exe'))
        self.assertIn('OpenFileDialog', argv[-1])
        self.assertIn('*.sql;*.txt', argv[-1])
        self.assertNotIn('*.csv', argv[-1])

    def test_linux_zenity_filters(self):
        run = recording_run(self.path + '\n')
        self.choose('linux', run)
        argv = run.calls[0]['argv']
        self.assertIn('--file-filter=SQL and text files | *.sql *.txt', argv)
        self.assertNotIn('--save', argv)

    def test_cancel_and_validation(self):
        self.assertEqual(self.choose('darwin', recording_run('\n')), {'path': None, 'cancelled': True})
        with self.assertRaisesRegex(ValueError, 'relative'):
            self.choose('darwin', recording_run('queries.sql\n'))

    def test_shares_the_dialog_lock(self):
        file_picker.folder_picker._DIALOG_LOCK.acquire()
        try:
            with self.assertRaisesRegex(ValueError, 'already open'):
                file_picker.choose_sql_file()
            with self.assertRaisesRegex(ValueError, 'already open'):
                file_picker.choose_save_file('q.sql')
        finally:
            file_picker.folder_picker._DIALOG_LOCK.release()


class SaveDialogTest(unittest.TestCase):
    def save(self, platform, run, name='q.sql'):
        with mock.patch('sys.platform', platform), mock.patch('myflames.file_picker.subprocess.run', side_effect=run):
            return file_picker.choose_save_file(name)

    def test_macos_choose_file_name(self):
        run = recording_run('/tmp/a.sql\n')
        self.assertEqual(self.save('darwin', run, 'my "query".sql'), {'path': '/tmp/a.sql', 'cancelled': False})
        script = run.calls[0]['kwargs']['input']
        self.assertIn('choose file name', script)
        self.assertIn('do shell script "/usr/bin/printenv MYFLAMES_SAVE_NAME"', script)
        self.assertNotIn('my "query".sql', script)
        self.assertEqual(run.calls[0]['kwargs']['env']['MYFLAMES_SAVE_NAME'], 'my "query".sql')

    def test_windows_save_file_dialog(self):
        run = recording_run('C:\\x\\a.sql\r\n')
        with mock.patch.dict(os.environ, {'SystemRoot': 'C:\\Windows'}):
            self.save('win32', run, "o'clock.sql")
        script = run.calls[0]['argv'][-1]
        self.assertIn('SaveFileDialog', script)
        self.assertIn('$dialog.FileName = $env:MYFLAMES_SAVE_NAME', script)
        self.assertNotIn("o'clock.sql", script)
        self.assertEqual(run.calls[0]['kwargs']['env']['MYFLAMES_SAVE_NAME'], "o'clock.sql")
        self.assertIn('*.sql', script)
        self.assertIn('$dialog.AddExtension = $true', script)
        self.assertIn("$dialog.DefaultExt = 'sql'", script)
        self.assertLess(script.index('$dialog.DefaultExt'), script.index('$dialog.ShowDialog()'))

    def test_windows_quote_payloads_are_environment_data_not_script(self):
        for quote in ("'", '\u2018', '\u2019', '\u201a', '\u201b'):
            with self.subTest(quote=quote):
                name = 'a{0};Start-Process calc;$x={0}.sql'.format(quote)
                run = recording_run('')
                with mock.patch.dict(os.environ, {'SF_DIALOG_PARENT': 'kept'}):
                    self.save('win32', run, name)
                call = run.calls[0]
                self.assertFalse(any(name in arg for arg in call['argv']))
                self.assertNotIn('Start-Process', call['argv'][-1])
                self.assertEqual(call['kwargs']['env']['MYFLAMES_SAVE_NAME'], name)
                self.assertEqual(call['kwargs']['env']['SF_DIALOG_PARENT'], 'kept')

    def test_typographic_quote_name_round_trips_after_cleaning(self):
        for platform in ('win32', 'darwin'):
            with self.subTest(platform=platform):
                run = recording_run('')
                self.save(platform, run, '../Bob\u2019s report\x01')
                call = run.calls[0]
                self.assertEqual(call['kwargs']['env']['MYFLAMES_SAVE_NAME'], 'Bob\u2019s report.sql')
                self.assertNotIn('Bob', ''.join(call['argv']) + (call['kwargs']['input'] or ''))

    def test_linux_zenity_save(self):
        run = recording_run('/tmp/a.sql\n')
        with mock.patch('myflames.file_picker.shutil.which', side_effect=lambda name: '/usr/bin/' + name if name == 'zenity' else None):
            self.assertEqual(self.save('linux', run)['path'], '/tmp/a.sql')
        argv = run.calls[0]['argv']
        self.assertIn('--save', argv)
        self.assertIn('--confirm-overwrite', argv)

    def test_linux_kdialog_when_no_zenity(self):
        run = recording_run('/tmp/a.sql\n')
        with mock.patch('myflames.file_picker.shutil.which', side_effect=lambda name: '/usr/bin/kdialog' if name == 'kdialog' else None):
            self.assertEqual(self.save('linux', run)['path'], '/tmp/a.sql')
        self.assertEqual(run.calls[0]['argv'][0], '/usr/bin/kdialog')
        self.assertIn('--getsavefilename', run.calls[0]['argv'])

    def test_linux_without_a_dialog_is_refused(self):
        with mock.patch('myflames.file_picker.shutil.which', return_value=None):
            with self.assertRaisesRegex(ValueError, 'zenity or kdialog'):
                self.save('linux', recording_run(''))

    def test_suggested_name_is_sanitized(self):
        self.assertEqual(file_picker._clean_suggested_name('../etc/passwd'), 'passwd.sql')
        self.assertEqual(file_picker._clean_suggested_name('a\x07b'), 'ab.sql')
        self.assertEqual(file_picker._clean_suggested_name(''), 'query.sql')
        # A colon is a drive or an NTFS stream on Windows.
        self.assertEqual(file_picker._clean_suggested_name('C:x'), 'C-x.sql')
        self.assertEqual(file_picker._clean_suggested_name('Top #3 · a:b.sql'), 'Top #3 · a-b.sql')
        self.assertEqual(file_picker._clean_suggested_name('keep.TXT'), 'keep.TXT')

    def test_cancel(self):
        self.assertEqual(self.save('darwin', recording_run('\n')), {'path': None, 'cancelled': True})


class SavePathTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = self.temp.name

    def check(self, path):
        return file_picker.check_save_path(path)

    def test_extension_added_when_missing(self):
        # The destination comes back canonical (macOS temp folders sit under
        # the /var -> /private/var link).
        self.assertEqual(self.check(os.path.join(self.dir, 'query')),
                         os.path.join(os.path.realpath(self.dir), 'query.sql'))

    def test_sql_and_txt_kept(self):
        self.assertTrue(self.check(os.path.join(self.dir, 'a.sql')).endswith('a.sql'))
        self.assertTrue(self.check(os.path.join(self.dir, 'a.txt')).endswith('a.txt'))
        self.assertTrue(self.check(os.path.join(self.dir, 'a.SQL')).endswith('a.SQL'))

    def test_extension_does_not_approve_an_existing_file(self):
        path = os.path.join(self.dir, 'report.sql')
        with open(path, 'w') as handle:
            handle.write('keep')
        with self.assertRaisesRegex(ValueError, 'choose.*again'):
            self.check(os.path.join(self.dir, 'report'))
        self.assertEqual(self.check(path), os.path.realpath(path))

    def test_symlink_targets_are_refused_including_dangling_links(self):
        for name in ('existing', 'missing'):
            with self.subTest(name=name):
                target = os.path.join(self.dir, name)
                if name == 'existing':
                    with open(target, 'w') as handle:
                        handle.write('keep')
                link = os.path.join(self.dir, name + '.sql')
                os.symlink(target, link)
                with self.assertRaisesRegex(ValueError, 'symbolic link'):
                    self.check(link)

    def test_relative_refused(self):
        with self.assertRaisesRegex(ValueError, 'relative'):
            self.check('query.sql')

    def test_control_characters_refused(self):
        with self.assertRaisesRegex(ValueError, 'characters'):
            self.check(os.path.join(self.dir, 'a\x07b.sql'))
        with self.assertRaisesRegex(ValueError, 'characters'):
            self.check(os.path.join(self.dir, 'a\x7fb.sql'))

    def test_missing_parent_refused(self):
        with self.assertRaisesRegex(ValueError, 'does not exist'):
            self.check(os.path.join(self.dir, 'no-such-folder', 'a.sql'))

    def test_directory_refused(self):
        with self.assertRaisesRegex(ValueError, 'folder'):
            self.check(self.dir)
        sub = os.path.join(self.dir, 'sub.sql')
        os.mkdir(sub)
        with self.assertRaisesRegex(ValueError, 'folder'):
            self.check(sub)

    def test_other_extension_refused(self):
        with self.assertRaisesRegex(ValueError, '.sql or .txt'):
            self.check(os.path.join(self.dir, 'a.exe'))


class AtomicWriteTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, 'q.sql')

    def test_writes_utf8_without_bom(self):
        file_picker.write_text_atomic(self.path, 'SELECT “x”; -- ü\n')
        with open(self.path, 'rb') as handle:
            raw = handle.read()
        self.assertFalse(raw.startswith(b'\xef\xbb\xbf'))
        self.assertEqual(raw.decode('utf-8'), 'SELECT “x”; -- ü\n')

    @unittest.skipUnless(os.name == 'posix', 'POSIX permission bits')
    def test_replaced_file_keeps_its_permissions(self):
        for mode in (0o664, 0o600, 0o640):
            with open(self.path, 'w') as handle:
                handle.write('old')
            os.chmod(self.path, mode)
            file_picker.write_text_atomic(self.path, 'new')
            self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), mode)

    @unittest.skipUnless(os.name == 'posix', 'POSIX permission bits')
    def test_new_file_gets_default_permissions(self):
        previous = os.umask(0o022)
        try:
            file_picker.write_text_atomic(self.path, 'new')
        finally:
            os.umask(previous)
        expected = 0o644 if file_picker._SAVE_DIR_FD else 0o600
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), expected)

    @unittest.skipUnless(os.name == 'posix', 'POSIX permission bits')
    def test_a_file_another_user_owns_never_lends_its_permissions(self):
        with open(self.path, 'w') as handle:
            handle.write('decoy')
        os.chmod(self.path, 0o666)
        previous = os.umask(0o077)
        try:
            with mock.patch('myflames.file_picker.os.geteuid', return_value=os.geteuid() + 1):
                file_picker.write_text_atomic(self.path, 'new')
        finally:
            os.umask(previous)
        # The default under umask 077, not the decoy's 0666.
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    @unittest.skipUnless(os.name == 'posix', 'POSIX permission bits')
    def test_a_hard_link_never_lends_its_permissions(self):
        source = os.path.join(self.temp.name, 'elsewhere.txt')
        with open(source, 'w') as handle:
            handle.write('someone else')
        os.chmod(source, 0o666)
        os.link(source, self.path)
        previous = os.umask(0o077)
        try:
            file_picker.write_text_atomic(self.path, 'new')
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        with open(source) as handle:
            self.assertEqual(handle.read(), 'someone else', 'the other link is untouched')

    def test_another_group_gets_only_what_others_had(self):
        self.assertEqual(file_picker._mode_for((0o640, 20), 0), 0o600)
        self.assertEqual(file_picker._mode_for((0o664, 20), 0), 0o644)
        self.assertEqual(file_picker._mode_for((0o664, 20), 20), 0o664)

    def test_replaces_atomically_and_cleans_up(self):
        with open(self.path, 'w') as handle:
            handle.write('old')
        file_picker.write_text_atomic(self.path, 'new')
        with open(self.path) as handle:
            self.assertEqual(handle.read(), 'new')
        left = [n for n in os.listdir(self.temp.name) if n.startswith('.myflames-')]
        self.assertEqual(left, [])

    def test_failure_leaves_the_target(self):
        with open(self.path, 'w') as handle:
            handle.write('old')
        with mock.patch('myflames.file_picker.os.replace', side_effect=OSError('no space')):
            with self.assertRaises(OSError):
                file_picker.write_text_atomic(self.path, 'new')
        with open(self.path) as handle:
            self.assertEqual(handle.read(), 'old')

    def test_fallback_checks_identity_and_does_not_clobber(self):
        with mock.patch.object(file_picker, '_SAVE_DIR_FD', False):
            file_picker.write_text_atomic(self.path, 'old')
            file_picker.write_text_atomic(self.path, 'new')
            with open(self.path) as handle:
                self.assertEqual(handle.read(), 'new')
            with self.assertRaisesRegex(ValueError, 'choose.*again'):
                file_picker.write_text_atomic(self.path[:-4], 'unapproved')
            fresh = os.path.join(self.temp.name, 'fresh')
            file_picker.write_text_atomic(fresh, 'created')
            with open(fresh + '.sql') as handle:
                self.assertEqual(handle.read(), 'created')

    def test_fallback_restats_before_replace(self):
        parent = os.path.join(self.temp.name, 'parent')
        moved, other = parent + '-moved', parent + '-other'
        os.mkdir(parent)
        os.mkdir(other)
        path = os.path.join(parent, 'q.sql')
        with open(path, 'w') as handle:
            handle.write('old')
        original_fsync = os.fsync

        def swap_after_fsync(fd):
            original_fsync(fd)
            os.rename(parent, moved)
            os.symlink(other, parent)

        with mock.patch.object(file_picker, '_SAVE_DIR_FD', False), \
                mock.patch('myflames.file_picker.os.fsync', side_effect=swap_after_fsync):
            with self.assertRaisesRegex(ValueError, 'folder.*changed'):
                file_picker.write_text_atomic(path, 'new')
        with open(os.path.join(moved, 'q.sql')) as handle:
            self.assertEqual(handle.read(), 'old')
        self.assertEqual(os.listdir(other), [])
        # Without a directory handle cleanup cannot locate the renamed parent
        # safely. TemporaryDirectory removes that abandoned private temp file.

    def test_pinned_parent_does_not_follow_a_swap_at_replace(self):
        if not file_picker._SAVE_DIR_FD:
            self.skipTest('Platform has no descriptor-relative file operations')
        parent = os.path.join(self.temp.name, 'parent')
        moved, other = parent + '-moved', parent + '-other'
        os.mkdir(parent)
        os.mkdir(other)
        path = os.path.join(parent, 'q.sql')
        original_replace = os.replace

        def swap_at_replace(source, target, **kwargs):
            os.rename(parent, moved)
            os.symlink(other, parent)
            return original_replace(source, target, **kwargs)

        with mock.patch('myflames.file_picker.os.replace', side_effect=swap_at_replace):
            file_picker.write_text_atomic(path, 'new')
        self.assertEqual(os.listdir(other), [])
        self.assertEqual(os.listdir(moved), ['q.sql'])
        with open(os.path.join(moved, 'q.sql')) as handle:
            self.assertEqual(handle.read(), 'new')

    def test_target_swapped_to_symlink_during_write_is_refused(self):
        other = os.path.join(self.temp.name, 'other.sql')
        with open(other, 'w') as handle:
            handle.write('keep')
        original_fsync = os.fsync

        def swap_after_fsync(fd):
            original_fsync(fd)
            os.symlink(other, self.path)

        with mock.patch('myflames.file_picker.os.fsync', side_effect=swap_after_fsync):
            with self.assertRaisesRegex(ValueError, 'symbolic link'):
                file_picker.write_text_atomic(self.path, 'new')
        self.assertTrue(os.path.islink(self.path))
        with open(other) as handle:
            self.assertEqual(handle.read(), 'keep')
        self.assertEqual(sorted(os.listdir(self.temp.name)), ['other.sql', 'q.sql'])


class OpenForReadTest(unittest.TestCase):
    """Open goes through the pinned parent: a folder swapped after the
    chooser (for a link or another folder) is refused."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = os.path.realpath(self.temp.name)
        self.folder = os.path.join(root, 'sub')
        os.mkdir(self.folder)
        self.path = os.path.join(self.folder, 'q.sql')
        with open(self.path, 'w') as handle:
            handle.write('SELECT 1;')
        self.other = os.path.join(root, 'private')
        os.mkdir(self.other)
        with open(os.path.join(self.other, 'q.sql'), 'w') as handle:
            handle.write("CREATE USER u IDENTIFIED BY 'private-pw';")
        self.identity = file_picker.file_parent_identity(self.path)

    def read(self):
        fd = file_picker.open_for_read(self.path, self.identity)
        with os.fdopen(fd, 'rb') as handle:
            return handle.read()

    @unittest.skipUnless(hasattr(os, 'symlink'), 'symlinks')
    def test_a_file_swapped_for_a_link_after_the_chooser_is_refused(self):
        # The chooser returned the canonical path; the leaf is not resolved again.
        os.rename(self.path, self.path + '.old')
        os.symlink(os.path.join(self.other, 'q.sql'), self.path)
        with self.assertRaisesRegex(ValueError, 'Could not open the file'):
            self.read()

    def test_reads_the_chosen_file(self):
        self.assertEqual(self.read(), b'SELECT 1;')

    @unittest.skipUnless(hasattr(os, 'symlink') and file_picker._SAVE_DIR_FD, 'POSIX folder handles')
    def test_folder_swapped_for_a_link_is_refused(self):
        os.rename(self.folder, self.folder + '-old')
        os.symlink(self.other, self.folder)
        with self.assertRaisesRegex(ValueError, 'changed while it was being opened'):
            self.read()

    @unittest.skipUnless(file_picker._SAVE_DIR_FD, 'POSIX folder handles')
    def test_folder_swapped_for_another_folder_is_refused(self):
        os.rename(self.folder, self.folder + '-old')
        os.rename(self.other, self.folder)
        with self.assertRaisesRegex(ValueError, 'changed while it was being opened'):
            self.read()


class FileApiTest(unittest.TestCase):
    """The /api/editor/file/* endpoints: token mapping and decode rules."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        ed._FILE_TOKENS.clear()
        self.addCleanup(ed._FILE_TOKENS.clear)

    def write(self, raw, name='q.sql'):
        # Canonical, as the real chooser returns it (file_picker.check_file).
        path = os.path.join(os.path.realpath(self.temp.name), name)
        with open(path, 'wb') as handle:
            handle.write(raw)
        return path

    def open_with(self, path):
        # The real chooser returns a canonical path (file_picker.check_file);
        # canonicalize the folder only, so leaf swaps stay what a test made them.
        path = os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))
        with mock.patch('myflames.file_picker.choose_sql_file', return_value={'path': path, 'cancelled': False}):
            return ed.api_response('/api/editor/file/open', {}, None)

    def test_open_decodes_and_maps_a_token(self):
        path = self.write(b'\xef\xbb\xbfSELECT 1;\n')
        reply = self.open_with(path)
        self.assertFalse(reply['cancelled'])
        self.assertEqual(reply['sql'], 'SELECT 1;\n', 'the BOM is stripped')
        self.assertIsNone(reply['warning'])
        self.assertEqual(reply['name'], 'q.sql')
        bound_path, parent_identity = ed._FILE_TOKENS[reply['token']]
        self.assertEqual(bound_path, os.path.realpath(path))
        info = os.stat(os.path.dirname(path))
        self.assertEqual(parent_identity, (info.st_dev, info.st_ino))

    def test_open_replaces_invalid_bytes_with_a_warning(self):
        reply = self.open_with(self.write(b'SELECT \xff\xfe;'))
        self.assertIn('', reply['sql'])
        self.assertIn('UTF-8', reply['warning'])

    def test_open_size_cap(self):
        path = self.write(b'x' * (file_picker.SQL_MAX_BYTES + 1))
        with self.assertRaisesRegex(ValueError, '5 MiB'):
            self.open_with(path)

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFOs require POSIX')
    def test_open_fifo_swap_is_refused_without_waiting_for_a_writer(self):
        path = self.write(b'SELECT 1;')
        # The chooser already checked this regular file; another OS user can
        # replace it before the endpoint opens it. A child deadline makes a
        # regression fail without leaving a blocked request thread behind.
        os.unlink(path)
        os.mkfifo(path)
        script = '''import sys
from unittest import mock
from myflames import editor_session as ed
with mock.patch('myflames.file_picker.choose_sql_file', return_value={'path': sys.argv[1], 'cancelled': False}):
    try:
        ed.api_response('/api/editor/file/open', {}, None)
    except ValueError as exc:
        assert 'regular' in str(exc), str(exc)
    else:
        raise AssertionError('FIFO was accepted')
assert not ed._FILE_TOKENS
'''
        try:
            canonical = os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))
            result = subprocess.run([sys.executable, '-c', script, canonical], capture_output=True,
                                    text=True, timeout=3)
        except subprocess.TimeoutExpired:
            self.fail('Opening the FIFO blocked waiting for a writer')
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(hasattr(os, 'O_NOFOLLOW'), 'O_NOFOLLOW requires POSIX')
    def test_open_refuses_symlink_swapped_in_at_the_open_boundary(self):
        path = self.write(b'SELECT 1;')
        target = self.write(b'SELECT secret;', 'other.sql')
        original_open = os.open

        def swap(chosen, flags, *args, **kwargs):
            os.unlink(path)
            os.symlink(target, path)
            return original_open(chosen, flags, *args, **kwargs)

        with mock.patch.object(ed.os, 'open', side_effect=swap):
            with self.assertRaisesRegex(ValueError, 'Could not open the file'):
                self.open_with(path)
        self.assertEqual(ed._FILE_TOKENS, {})

    def test_open_growth_is_rejected_before_decoding_or_issuing_a_token(self):
        path = self.write(b'SELECT 1;')
        original_fstat = os.fstat

        def grow_after_stat(fd):
            info = original_fstat(fd)
            with open(path, 'ab') as handle:
                handle.write(b'\xff' * file_picker.SQL_MAX_BYTES)
            return info

        with mock.patch('myflames.editor_session.os.fstat', side_effect=grow_after_stat):
            with self.assertRaisesRegex(ValueError, '5 MiB'):
                self.open_with(path)
        self.assertEqual(ed._FILE_TOKENS, {})

    def test_open_exact_cap_is_accepted(self):
        raw = b'\xef\xbb\xbf' + b'x' * (file_picker.SQL_MAX_BYTES - 3)
        reply = self.open_with(self.write(raw))
        self.assertEqual(len(reply['sql']), file_picker.SQL_MAX_BYTES - 3)

    def test_open_cancelled(self):
        with mock.patch('myflames.file_picker.choose_sql_file', return_value={'path': None, 'cancelled': True}):
            self.assertEqual(ed.api_response('/api/editor/file/open', {}, None), {'cancelled': True})

    def test_save_back_by_token_without_a_path(self):
        path = self.write(b'SELECT 1;')
        token = self.open_with(path)['token']
        # The renderer never sends a path: only the token and the text.
        reply = ed.api_response('/api/editor/file/save', {'sql': 'SELECT 2;', 'token': token}, None)
        with open(path) as handle:
            self.assertEqual(handle.read(), 'SELECT 2;')
        self.assertEqual(reply['name'], 'q.sql')
        self.assertNotEqual(reply['token'], token, 'a fresh token is issued')

    def test_save_unknown_token(self):
        with self.assertRaisesRegex(ed.EditorError, 'binding is gone'):
            ed.api_response('/api/editor/file/save', {'sql': 'x', 'token': 'f' * 32}, None)

    def test_save_as_asks_and_validates(self):
        target = os.path.join(self.temp.name, 'chosen')
        with mock.patch('myflames.file_picker.choose_save_file', return_value={'path': target, 'cancelled': False}) as ask:
            reply = ed.api_response('/api/editor/file/save', {'sql': 'SELECT 1;', 'save_as': True, 'name': 'n.sql'}, None)
        self.assertEqual(ask.call_args[0][0], 'n.sql')
        self.assertEqual(reply['path'], os.path.realpath(target) + '.sql',
                         'the extension is added when missing (canonical path)')
        with open(target + '.sql') as handle:
            self.assertEqual(handle.read(), 'SELECT 1;')
        with mock.patch('myflames.file_picker.choose_save_file', return_value={'path': 'relative.sql', 'cancelled': False}):
            with self.assertRaisesRegex(ValueError, 'relative'):
                ed.api_response('/api/editor/file/save', {'sql': 'x', 'save_as': True}, None)

    def test_save_as_cancelled_writes_nothing(self):
        with mock.patch('myflames.file_picker.choose_save_file', return_value={'path': None, 'cancelled': True}):
            self.assertEqual(ed.api_response('/api/editor/file/save', {'sql': 'x', 'save_as': True}, None),
                             {'cancelled': True})

    def save_as(self, path, sql='SELECT 2;'):
        with mock.patch('myflames.file_picker.choose_save_file', return_value={'path': path, 'cancelled': False}):
            return ed.api_response('/api/editor/file/save', {'sql': sql, 'save_as': True}, None)

    def test_save_as_extension_collision_preserves_unapproved_file(self):
        path = self.write(b'keep', 'report.sql')
        with self.assertRaisesRegex(ValueError, 'choose.*again'):
            self.save_as(path[:-4])
        with open(path, 'rb') as handle:
            self.assertEqual(handle.read(), b'keep')
        self.assertEqual(ed._FILE_TOKENS, {})
        self.save_as(path)
        with open(path, 'rb') as handle:
            self.assertEqual(handle.read(), b'SELECT 2;')

    def test_save_token_cannot_be_redirected_by_a_renderer_path(self):
        path = self.write(b'old')
        other = self.write(b'keep', 'other.sql')
        token = self.open_with(path)['token']
        ed.api_response('/api/editor/file/save', {'sql': 'new', 'token': token, 'path': other}, None)
        with open(path, 'rb') as handle:
            self.assertEqual(handle.read(), b'new')
        with open(other, 'rb') as handle:
            self.assertEqual(handle.read(), b'keep')

    def test_ui_routes_need_no_engine_or_password_access(self):
        """Local files have the same contract with either engine selected."""
        from myflames import ui
        for engine in ('mysql', 'mariadb'):
            with self.subTest(engine=engine):
                path = self.write(b'old', engine + '.sql')
                connection = {'engine': engine, 'host': 'not-a-real-server'}
                with mock.patch('myflames.connector.MySQLConnection') as connect, \
                        mock.patch('myflames.ui._with_saved_password') as passwords, \
                        mock.patch('myflames.file_picker.choose_sql_file', return_value={'path': path, 'cancelled': False}):
                    opened = ui.api_response('/api/editor/file/open', {'connection': connection}, workspace=object())
                    saved = ui.api_response('/api/editor/file/save', {'sql': 'new', 'token': opened['token'],
                                                                    'connection': connection}, workspace=object())
                    self.assertFalse(saved['cancelled'])
                    connect.assert_not_called()
                    passwords.assert_not_called()
                with open(path) as handle:
                    self.assertEqual(handle.read(), 'new')

    def test_save_token_refuses_symlink_target(self):
        path = self.write(b'old')
        other = self.write(b'keep', 'other.sql')
        token = self.open_with(path)['token']
        os.unlink(path)
        os.symlink(other, path)
        with self.assertRaisesRegex(ValueError, 'symbolic link'):
            ed.api_response('/api/editor/file/save', {'sql': 'new', 'token': token}, None)
        self.assertTrue(os.path.islink(path))
        with open(other, 'rb') as handle:
            self.assertEqual(handle.read(), b'keep')

    def test_save_as_binds_the_canonical_parent(self):
        real = os.path.join(self.temp.name, 'real')
        other = os.path.join(self.temp.name, 'other')
        alias = os.path.join(self.temp.name, 'alias')
        os.mkdir(real)
        os.mkdir(other)
        os.symlink(real, alias)
        reply = self.save_as(os.path.join(alias, 'q.sql'))
        self.assertEqual(reply['path'], os.path.realpath(os.path.join(real, 'q.sql')))
        os.unlink(alias)
        os.symlink(other, alias)
        ed.api_response('/api/editor/file/save', {'sql': 'new', 'token': reply['token']}, None)
        with open(os.path.join(real, 'q.sql'), 'rb') as handle:
            self.assertEqual(handle.read(), b'new')
        self.assertEqual(os.listdir(other), [])

    def test_parent_swaps_after_open_and_save_as_are_refused(self):
        for initial in ('open', 'save_as'):
            for replacement in ('symlink', 'directory'):
                with self.subTest(initial=initial, replacement=replacement):
                    parent = os.path.join(self.temp.name, initial + '-' + replacement)
                    moved, other = parent + '-moved', parent + '-other'
                    os.mkdir(parent)
                    os.mkdir(other)
                    path = os.path.join(parent, 'q.sql')
                    with open(path, 'w') as handle:
                        handle.write('old')
                    reply = self.open_with(path) if initial == 'open' else self.save_as(path, 'old')
                    os.rename(parent, moved)
                    if replacement == 'symlink':
                        os.symlink(other, parent)
                    else:
                        os.mkdir(parent)
                    with self.assertRaisesRegex(ValueError, 'folder.*changed'):
                        ed.api_response('/api/editor/file/save', {'sql': 'new', 'token': reply['token']}, None)
                    with open(os.path.join(moved, 'q.sql')) as handle:
                        self.assertEqual(handle.read(), 'old')
                    self.assertEqual(os.listdir(parent), [])
                    self.assertEqual(os.listdir(other), [])

    def test_parent_swap_during_write_is_refused_and_temporary_is_removed(self):
        parent = os.path.join(self.temp.name, 'parent')
        moved, other = parent + '-moved', parent + '-other'
        os.mkdir(parent)
        os.mkdir(other)
        path = os.path.join(parent, 'q.sql')
        reply = self.save_as(path, 'old')
        original_fsync = os.fsync

        def swap_after_fsync(fd):
            original_fsync(fd)
            os.rename(parent, moved)
            os.symlink(other, parent)

        with mock.patch('myflames.file_picker.os.fsync', side_effect=swap_after_fsync):
            with self.assertRaisesRegex(ValueError, 'folder.*changed'):
                ed.api_response('/api/editor/file/save', {'sql': 'new', 'token': reply['token']}, None)
        self.assertEqual(os.listdir(other), [])
        self.assertEqual(os.listdir(moved), ['q.sql'])
        with open(os.path.join(moved, 'q.sql')) as handle:
            self.assertEqual(handle.read(), 'old')

    def test_added_extension_race_never_overwrites_a_new_file(self):
        path = os.path.join(self.temp.name, 'report.sql')
        original_fsync = os.fsync

        def create_after_fsync(fd):
            original_fsync(fd)
            with open(path, 'w') as handle:
                handle.write('keep')

        with mock.patch('myflames.file_picker.os.fsync', side_effect=create_after_fsync):
            with self.assertRaisesRegex(ValueError, 'choose.*again'):
                self.save_as(path[:-4])
        with open(path) as handle:
            self.assertEqual(handle.read(), 'keep')
        self.assertEqual(os.listdir(self.temp.name), ['report.sql'])

    def test_save_refuses_a_directory_that_replaced_the_file(self):
        path = self.write(b'SELECT 1;')
        token = self.open_with(path)['token']
        os.unlink(path)
        os.mkdir(path)
        with self.assertRaisesRegex(ValueError, 'folder'):
            ed.api_response('/api/editor/file/save', {'sql': 'x', 'token': token}, None)

    def test_token_map_is_bounded(self):
        for n in range(ed._FILE_TOKEN_MAX + 5):
            ed._file_token_remember(os.path.join(self.temp.name, 'f%d.sql' % n))
        self.assertLessEqual(len(ed._FILE_TOKENS), ed._FILE_TOKEN_MAX)


class UseDatabaseTest(unittest.TestCase):
    """The /api/editor/use request's validation (no live session needed)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = mock.patch.dict(os.environ, {'HOME': self.temp.name, 'USERPROFILE': self.temp.name,
                                           'MYFLAMES_SECRET_BACKEND': 'vault'})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        from myflames.workspace_store import WorkspaceStore
        self.store = WorkspaceStore()
        self.registry = ed.EditorSessions(reap_every=0)
        self.addCleanup(self.registry.close)
        import types
        self.workspace = types.SimpleNamespace(store=self.store, editor=self.registry)
        self.connection = {'host': '127.0.0.1', 'port': 3306, 'user': 'app'}

    def call(self, **payload):
        payload.setdefault('tab_id', 'tab1')
        payload.setdefault('connection', dict(self.connection))
        return ed.api_response('/api/editor/use', payload, self.workspace)

    def test_no_session_keeps_the_choice(self):
        reply = self.call(database='app')
        self.assertEqual(reply['status'], 'none')
        self.assertEqual(reply['database'], 'app')
        self.assertIsNone(reply['session'])

    def test_name_validation(self):
        for bad, words in [('', 'Choose a database'), ('x' * 65, '64'), ('a\x07b', 'characters'), (5, 'Choose')]:
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, words):
                self.call(database=bad)

    def test_statement_escapes_backquotes(self):
        # The identifier rule itself: backquotes double, nothing else changes.
        self.assertEqual('USE `a``b`', 'USE `%s`' % 'a`b'.replace('`', '``'))


if __name__ == '__main__':
    unittest.main()
