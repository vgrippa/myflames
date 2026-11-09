"""
Unit tests for :mod:`myflames.connector`.

These tests never touch the network. Subprocess execution is replaced with
a ``FakeRun`` capture that records the argv and returns canned stdout, and
``shutil.which`` is monkeypatched so ``_resolve_binary`` picks a predictable
path. Both paths (no-password, full TLS) are covered.

Integration tests that hit real MySQL / MariaDB containers live in
``test_connector_integration.py`` and are skipped when Docker is unavailable.
"""
import concurrent.futures
import contextlib
import json
import ntpath
import os
import re
import signal
import stat
import sys
import subprocess
import tempfile
import threading
import time
import traceback
import unittest
from unittest import mock

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(TEST_DIR))

from myflames import connector
from myflames.connector import MySQLConnection, ConnectorError


def _fake_completed(stdout=b"", stderr=b"", returncode=0):
    """Build a ``subprocess.CompletedProcess``-like stand-in."""
    cp = subprocess.CompletedProcess(args=[], returncode=returncode)
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


@contextlib.contextmanager
def option_file_client(directory, answers=None):
    """A real child process that reads credentials and records only their path.

    Optional SQL answers come from fixtures; no credentials are logged. Each
    child writes a separate record, avoiding a shared append-file race.
    """
    path = os.path.join(directory, 'fake-mysql')
    with open(os.path.join(directory, 'answers.json'), 'w', encoding='utf-8') as handle:
        json.dump(answers, handle)
    script = '''import json, os, stat, sys
directory = os.path.dirname(os.path.abspath(__file__))
assert sys.argv[1].startswith('--defaults-file=')
path = sys.argv[1].split('=', 1)[1]
with open(path, encoding='utf-8') as handle:
    content = handle.read()
assert '[client]' in content and 'password=' in content
if os.name == 'posix':
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
with open(os.path.join(directory, 'record-%d.json' % os.getpid()), 'w') as handle:
    json.dump(path, handle)
sql = sys.argv[sys.argv.index('-e') + 1] if '-e' in sys.argv else sys.stdin.read()
with open(os.path.join(directory, 'answers.json'), encoding='utf-8') as handle:
    answers = json.load(handle)
if answers is None:
    sys.stdout.write('1\\n')
else:
    assert sql in answers, 'unexpected SQL: ' + sql
    answer = answers[sql]
    if 'error' in answer:
        sys.stderr.write(answer['error'])
        sys.exit(1)
    sys.stdout.write(answer['stdout'])
'''
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write('#!' + sys.executable + '\n' + script)
    os.chmod(path, 0o700)
    real_popen = subprocess.Popen

    def launch(argv, *args, **kwargs):
        # Use the interpreter explicitly so the fake runs on Windows too.
        # The actual client subprocess, pipes and option-file reads are real.
        if argv[0] == path:
            argv = [sys.executable, path] + argv[1:]
        return real_popen(argv, *args, **kwargs)

    with mock.patch.object(subprocess, 'Popen', side_effect=launch):
        yield path


class TestConcurrentCredentials(unittest.TestCase):
    def test_twenty_concurrent_reads_have_distinct_ephemeral_files(self):
        self._concurrent_calls(MySQLConnection, 'run')

    def test_twenty_concurrent_writes_have_distinct_ephemeral_files(self):
        self._concurrent_calls(MySQLConnection, 'run_write')

    def test_lifecycle_control_calls_have_distinct_ephemeral_files(self):
        from myflames.lifecycle import LifecycleConnection
        self._concurrent_calls(LifecycleConnection, 'run')

    def test_editor_close_cannot_remove_starting_clients_credentials(self):
        from myflames import editor_session
        paths = []
        options = {'host': 'h', 'password': 'private-secret', 'binary': 'mysql'}
        session = editor_session.EditorSession('tab', {}, options)

        def spawn(argv, **kwargs):
            path = argv[1].split('=', 1)[1]
            paths.append(path)
            # Ending a session can close its connection during startup.
            session._conn.close()
            with open(path, encoding='utf-8') as handle:
                self.assertIn('password="private-secret"', handle.read())
            raise OSError('test spawn failure')

        with mock.patch.object(editor_session, '_client_features', return_value={}), \
                mock.patch.object(editor_session.subprocess, 'Popen', side_effect=spawn):
            with self.assertRaisesRegex(ConnectorError, 'test spawn failure'):
                session.start(options)
        self.assertEqual(len(paths), 1)
        self.assertFalse(os.path.exists(paths[0]))

    def _concurrent_calls(self, connection_class, method):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(tempfile, 'tempdir', directory), \
                option_file_client(directory) as binary:
            conn = connection_class(host='h', password='private-secret', binary=binary)
            barrier = threading.Barrier(20)
            original = conn._create_defaults_file
            paths = []
            lock = threading.Lock()

            def create():
                path = original()
                with lock:
                    paths.append(path)
                # All twenty calls must own a file before any child starts.
                barrier.wait(timeout=30)
                conn.close()  # Must not remove any in-flight call's file.
                return path

            try:
                with mock.patch.object(conn, '_create_defaults_file', side_effect=create), \
                        concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
                    futures = [pool.submit(getattr(conn, method), 'SELECT 1') for _ in range(20)]
                    outputs = [future.result(timeout=40) for future in futures]
                self.assertEqual(outputs, ['1\n'] * 20)
                records = []
                for name in os.listdir(directory):
                    if name.startswith('record-'):
                        with open(os.path.join(directory, name)) as handle:
                            records.append(json.load(handle))
                self.assertEqual(len(records), 20)
                self.assertEqual(len(set(records)), 20)
                self.assertEqual(set(paths), set(records))
                self.assertTrue(all(not os.path.exists(path) for path in records))
                self.assertFalse(any(name.endswith('.cnf') for name in os.listdir(directory)))
            finally:
                conn.close()

    def test_failure_paths_remove_each_invocations_file(self):
        failures = (FileNotFoundError('missing client'), RuntimeError('interrupted'),
                    subprocess.TimeoutExpired('mysql', 1))
        for method in ('run', 'run_write'):
            for failure in failures:
                paths = []
                def fail(argv, **kwargs):
                    paths.append(argv[1].split('=', 1)[1])
                    raise failure
                with self.subTest(method=method, failure=type(failure).__name__), \
                        MySQLConnection('h', password='secret', binary='mysql') as conn, \
                        mock.patch.object(connector, '_run_bounded', side_effect=fail):
                    with self.assertRaises((ConnectorError, RuntimeError)):
                        getattr(conn, method)('SELECT 1')
                self.assertEqual(len(paths), 1)
                self.assertFalse(os.path.exists(paths[0]))


class TestConnectorConstructor(unittest.TestCase):
    """Constructor and config handling."""

    def test_defaults(self):
        c = MySQLConnection(host="localhost")
        self.assertEqual(c.host, "localhost")
        self.assertEqual(c.port, 3306)
        self.assertIsNone(c.user)
        self.assertIsNone(c.password)

    def test_port_coerces_to_int(self):
        c = MySQLConnection(host="x", port="3307")
        self.assertEqual(c.port, 3307)

    def test_port_none_becomes_default(self):
        c = MySQLConnection(host="x", port=None)
        self.assertEqual(c.port, 3306)

    def test_ssl_fields_stored(self):
        c = MySQLConnection(
            host="rds.example.com",
            ssl_mode="VERIFY_IDENTITY",
            ssl_ca="/path/bundle.pem",
            ssl_cert="/path/client.pem",
            ssl_key="/path/client.key",
        )
        self.assertEqual(c.ssl_mode, "VERIFY_IDENTITY")
        self.assertEqual(c.ssl_ca, "/path/bundle.pem")
        self.assertEqual(c.ssl_cert, "/path/client.pem")
        self.assertEqual(c.ssl_key, "/path/client.key")


class TestDefaultsFile(unittest.TestCase):
    """Password and SSL handling go through the defaults-extra-file."""

    def _inspect_file(self, c):
        path = c._ensure_defaults_file()
        try:
            st = os.stat(path)
            mode = stat.S_IMODE(st.st_mode)
            with open(path) as f:
                content = f.read()
            return path, mode, content
        finally:
            c.close()

    @unittest.skipUnless(os.name == "posix", "Windows ignores POSIX file modes")
    def test_defaults_file_is_mode_0600(self):
        c = MySQLConnection(host="h", user="u", password="secret")
        _, mode, _ = self._inspect_file(c)
        self.assertEqual(mode, 0o600, "defaults file must be mode 0600 (got %o)" % mode)

    def test_defaults_file_has_client_section(self):
        c = MySQLConnection(host="h", user="u", password="p")
        _, _, content = self._inspect_file(c)
        self.assertIn("[client]", content)
        self.assertIn('host="h"', content)
        self.assertIn('user="u"', content)

    def test_tcp_transport_is_explicit_for_localhost_and_remote_hosts(self):
        for host in ('localhost', '127.0.0.1', 'db.example', '::1'):
            with self.subTest(host=host), MySQLConnection(host=host, port=3317) as conn:
                content = conn._build_defaults_file_content()
                self.assertIn('protocol=TCP\n', content)
                self.assertIn('port=3317\n', content)
                self.assertNotIn('socket=', content)

    def test_password_is_quoted(self):
        """Passwords with # / ; / = must be quoted so the INI parser in
        the CLI does not interpret them as comment delimiters."""
        c = MySQLConnection(host="h", user="u", password='p#a;s"s=word')
        _, _, content = self._inspect_file(c)
        self.assertIn('password="p#a;s\\"s=word"', content)

    def test_ssl_options_written(self):
        c = MySQLConnection(
            host="h", user="u", password="p",
            ssl_mode="VERIFY_IDENTITY",
            ssl_ca="/ca.pem",
            ssl_cert="/c.pem",
            ssl_key="/k.pem",
            ssl_cipher="AES256",
        )
        _, _, content = self._inspect_file(c)
        self.assertIn('ssl-mode="VERIFY_IDENTITY"', content)
        self.assertIn('ssl-ca="/ca.pem"', content)
        self.assertIn('ssl-cert="/c.pem"', content)
        self.assertIn('ssl-key="/k.pem"', content)
        self.assertIn('ssl-cipher="AES256"', content)

    def test_defaults_file_deleted_on_close(self):
        c = MySQLConnection(host="h", user="u", password="p")
        path = c._ensure_defaults_file()
        self.assertTrue(os.path.exists(path))
        c.close()
        self.assertFalse(os.path.exists(path))

    def test_close_is_idempotent(self):
        c = MySQLConnection(host="h", user="u", password="p")
        c._ensure_defaults_file()
        c.close()
        c.close()  # must not raise

    def test_context_manager_closes(self):
        with MySQLConnection(host="h", user="u", password="p") as c:
            path = c._ensure_defaults_file()
            self.assertTrue(os.path.exists(path))
        self.assertFalse(os.path.exists(path))

    def test_defaults_file_works_without_fchmod(self):
        """Windows has no os.fchmod: the file is still written and removed."""
        class OsWithoutFchmod(object):
            def __getattr__(self, name):
                if name == "fchmod":
                    raise AttributeError(name)
                return getattr(os, name)
        with mock.patch("myflames.connector.os", OsWithoutFchmod()):
            c = MySQLConnection(host="h", user="u", password="p#w")
            path = c._ensure_defaults_file()
            with open(path, encoding="utf-8") as f:
                content = f.read()
            c.close()
        self.assertIn('password="p#w"', content)
        self.assertFalse(os.path.exists(path))

    def test_defaults_file_is_removed_when_writing_fails(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("tempfile.tempdir", directory), \
                mock.patch.object(MySQLConnection, "_build_defaults_file_content",
                                  side_effect=RuntimeError("boom")):
            c = MySQLConnection(host="h", user="u", password="p")
            with self.assertRaisesRegex(RuntimeError, "boom"):
                c._ensure_defaults_file()
            self.assertEqual(os.listdir(directory), [])
            self.assertIsNone(c._defaults_file_path)


class TestBinaryResolution(unittest.TestCase):
    """Binary lookup order: override → mysql → mariadb."""

    def setUp(self):
        from myflames import client_tools
        patch = mock.patch.object(client_tools, 'snapshot', return_value=[])
        patch.start()
        self.addCleanup(patch.stop)

    def test_explicit_binary_override(self):
        c = MySQLConnection(host="h", binary="/opt/mysql/bin/mysql")
        self.assertEqual(c._resolve_binary(), "/opt/mysql/bin/mysql")

    def test_prefers_mysql_over_mariadb(self):
        c = MySQLConnection(host="h")
        def fake_which(name):
            return "/bin/" + name if name in ("mysql", "mariadb") else None
        with mock.patch.object(MySQLConnection, "_which", staticmethod(fake_which)):
            self.assertEqual(c._resolve_binary(), "/bin/mysql")

    def test_falls_back_to_mariadb(self):
        c = MySQLConnection(host="h")
        def fake_which(name):
            return "/bin/mariadb" if name == "mariadb" else None
        with mock.patch.object(MySQLConnection, "_which", staticmethod(fake_which)):
            self.assertEqual(c._resolve_binary(), "/bin/mariadb")

    def test_error_when_binary_missing(self):
        c = MySQLConnection(host="h")
        with mock.patch.object(MySQLConnection, "_which", staticmethod(lambda n: None)), \
                mock.patch.dict(os.environ, {"MYFLAMES_BUNDLED_CLIENT_DIR": ""}):
            with self.assertRaises(ConnectorError):
                c._resolve_binary()


@unittest.skipIf(os.name == 'nt', 'POSIX executable fixtures; Windows names tested separately')
class TestClientSelection(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old_default = connector.client_choices()
        connector.set_client_choices({})
        self.addCleanup(connector.set_client_choices, self.old_default)
        env = mock.patch.dict(os.environ, {'HOME': self.temp.name, 'USERPROFILE': self.temp.name,
                                         'PATH': '', 'MYFLAMES_SECRET_BACKEND': 'vault',
                                         'MYFLAMES_BUNDLED_CLIENT_DIR': '', 'MYSQL_PWD': 'must-not-leak', 'AWS_SECRET_ACCESS_KEY': 'must-not-leak'})
        env.start()
        self.addCleanup(env.stop)
        from myflames import lifecycle, client_tools
        patch = mock.patch.object(lifecycle, 'COMMON_BIN_DIRS', ())
        patch.start()
        self.addCleanup(patch.stop)
        client_tools._FACTS.clear()
        client_tools._PROBE_CACHE.clear()

    def binary(self, name='mysql', body='print("mysql Ver 8.4.0")', folder='bin'):
        # The real temp path (macOS /var is a link): a chosen client runs by its real path.
        directory = os.path.join(os.path.realpath(self.temp.name), folder)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, 'w') as stream:
            stream.write('#!' + sys.executable + '\n' + body + '\n')
        os.chmod(path, 0o700)
        return path

    def test_missing_error_is_typed_and_has_exact_workspace_message(self):
        with self.assertRaises(connector.ClientMissingError) as caught:
            MySQLConnection(host="localhost")._resolve_binary()
        self.assertIsInstance(caught.exception, ConnectorError)
        self.assertEqual(caught.exception.code, 'client-missing')
        self.assertEqual(str(caught.exception),
                         'myflames could not find the MySQL or MariaDB command-line client (mysql or mariadb).')
        self.assertNotIn('--mysql-binary', str(caught.exception))



    def test_probe_checks_argv_and_isolates_credentials_and_login_files(self):
        body = ("import os, sys\n"
                "assert sys.argv[1:] == ['--no-defaults', '--version']\n"
                "assert 'MYSQL_PWD' not in os.environ and 'AWS_SECRET_ACCESS_KEY' not in os.environ\n"
                "assert os.environ['HOME'] == os.environ['USERPROFILE']\n"
                "assert not os.path.exists(os.environ['MYSQL_TEST_LOGIN_FILE'])\n"
                "assert not os.listdir(os.environ['HOME'])\n"
                "print('mysql Ver 8.4.0')")
        path = self.binary(body=body, folder='has spaces')
        self.assertEqual(connector.probe_client(path), {'path': path, 'name': 'mysql', 'version': 'mysql Ver 8.4.0'})

    def test_probe_refuses_relative_missing_directory_wrong_name_and_directory_symlink(self):
        path = self.binary()
        directory = os.path.join(self.temp.name, 'directory', 'mysql')
        os.makedirs(directory)
        link = os.path.join(self.temp.name, 'mariadb')
        os.symlink(directory, link)
        for candidate in ('mysql', os.path.join(self.temp.name, 'mysql'), directory, link,
                          self.binary('other'), None, 42, '', path + '\0'):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                connector.probe_client(candidate)

    def test_probe_refuses_nonzero_unrecognized_and_oversized_output(self):
        for body in ('print("mysql 8.4"); raise SystemExit(2)', 'print("unrelated 8.4.0")',
                     'print("mysql " + "x" * 20000)'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                connector.probe_client(self.binary(body=body))

    def test_file_symlink_and_stderr_banner_are_usable(self):
        path = self.binary(body='import sys; print("MariaDB 11.4", file=sys.stderr)', folder='target')
        link = os.path.join(self.temp.name, 'mariadb')
        os.symlink(path, link)
        self.assertEqual(connector.probe_client(link)['version'], 'MariaDB 11.4')

    def test_non_executable_client_is_refused(self):
        path = self.binary()
        os.chmod(path, 0o600)
        with self.assertRaisesRegex(ValueError, 'Could not run'):
            connector.probe_client(path)

    def test_writable_client_and_nonsticky_folder_are_refused_before_execution(self):
        path = self.binary()
        folder = os.path.dirname(path)
        for file_mode, folder_mode in ((0o777, 0o755), (0o775, 0o755),
                                       (0o755, 0o777), (0o755, 0o775)):
            with self.subTest(file_mode=file_mode, folder_mode=folder_mode):
                os.chmod(path, file_mode)
                os.chmod(folder, folder_mode)
                with mock.patch.object(connector, '_run_bounded') as run:
                    with self.assertRaisesRegex(ValueError, 'can be changed by other users'):
                        connector.probe_client(path)
                    run.assert_not_called()

    def _fake_stat(self, original, uid=None, mode=None):
        fields = list(original)
        if uid is not None:
            fields[4] = uid
        if mode is not None:
            fields[0] = mode
        return os.stat_result(fields)

    def test_writable_sticky_folder_is_accepted_only_when_root_owns_it(self):
        path = self.binary()
        folder = os.path.dirname(path)
        real_lstat = os.lstat
        real_uid = os.geteuid()
        user_uid = real_uid or 501
        for owner, mode, accepted in ((0, 0o1777, True), (0, 0o777, False),
                                      (user_uid, 0o1777, False), (user_uid + 1, 0o1777, False)):
            def lstat(candidate, *args, **kwargs):
                info = real_lstat(candidate, *args, **kwargs)
                return self._fake_stat(info, owner, stat.S_IFDIR | mode) if candidate == folder else info
            with self.subTest(owner=owner, mode=mode), \
                    mock.patch.object(connector.os, 'geteuid', return_value=user_uid), \
                    mock.patch.object(connector.os, 'lstat', side_effect=lstat):
                if accepted:
                    self.assertEqual(connector.probe_client(path)['path'], path)
                else:
                    with self.assertRaisesRegex(ValueError, 'can be changed by other users'):
                        connector.probe_client(path)

    def test_macos_admin_group_write_is_accepted_but_not_others(self):
        # Homebrew's /opt/homebrew/bin and /Applications are writable by the
        # admin group (gid 80), which can already act as root; other gids and
        # any write for others stay refused, and only on macOS.
        path = self.binary()
        folder = os.path.dirname(path)
        real_lstat = os.lstat
        user_uid = os.geteuid() or 501
        for platform, gid, mode, accepted in (('darwin', 80, 0o775, True), ('darwin', 20, 0o775, False),
                                              ('darwin', 80, 0o777, False), ('linux', 80, 0o775, False)):
            def lstat(candidate, *args, **kwargs):
                info = real_lstat(candidate, *args, **kwargs)
                if candidate != folder:
                    return info
                fields = list(self._fake_stat(info, user_uid, stat.S_IFDIR | mode))
                fields[5] = gid
                return os.stat_result(fields)
            with self.subTest(platform=platform, gid=gid, mode=oct(mode)), \
                    mock.patch.object(connector.sys, 'platform', platform), \
                    mock.patch.object(connector.os, 'geteuid', return_value=user_uid), \
                    mock.patch.object(connector.os, 'lstat', side_effect=lstat):
                if accepted:
                    self.assertEqual(connector.probe_client(path)['path'], path)
                else:
                    with self.assertRaisesRegex(ValueError, 'can be changed by other users'):
                        connector.probe_client(path)

    def test_a_mysql_link_to_a_versioned_file_is_a_valid_choice(self):
        # Code review D7: the name rule applies to the chosen path; the resolved
        # (versioned) file is probed and run.
        real = self.binary(name='mysql-8.4')
        alias_folder = os.path.join(os.path.realpath(self.temp.name), 'links')
        os.makedirs(alias_folder)
        alias = os.path.join(alias_folder, 'mysql')
        os.symlink(real, alias)
        self.assertEqual(connector.probe_client(alias)['name'], 'mysql')
        connector.set_client_choices({'mysql': alias})
        self.addCleanup(connector.set_client_choices, {})
        self.assertEqual(MySQLConnection(host='localhost')._resolve_binary(), real)
        self.assertEqual(MySQLConnection(host='localhost')._resolve_binary(), real)

    def test_chosen_client_runs_its_resolved_file_not_the_alias_chain(self):
        # Code review D6: /safe/mysql -> /hop/mysql -> real file. The client is
        # run by its real path, so a swapped intermediate link cannot redirect it.
        real = self.binary()
        hop_folder = tempfile.mkdtemp(dir=self.temp.name)
        os.chmod(hop_folder, 0o700)
        hop = os.path.join(hop_folder, 'mysql')
        os.symlink(real, hop)
        alias_folder = tempfile.mkdtemp(dir=self.temp.name)
        os.chmod(alias_folder, 0o700)
        alias = os.path.join(alias_folder, 'mysql')
        os.symlink(hop, alias)
        connector.set_client_choices({'mysql': alias})
        self.addCleanup(connector.set_client_choices, {})
        self.assertEqual(MySQLConnection(host='localhost')._resolve_binary(), os.path.realpath(real))

    def test_other_users_client_is_refused(self):
        path = self.binary()
        real_lstat = os.lstat
        def lstat(candidate, *args, **kwargs):
            info = real_lstat(candidate, *args, **kwargs)
            return self._fake_stat(info, os.geteuid() + 1) if candidate == path else info
        with mock.patch.object(connector.os, 'lstat', side_effect=lstat), \
                mock.patch.object(connector, '_run_bounded') as run:
            with self.assertRaisesRegex(ValueError, 'can be changed by other users'):
                connector.probe_client(path)
            run.assert_not_called()

    def test_cached_selection_rechecks_folder_permissions_before_receiving_secrets(self):
        marker = os.path.join(self.temp.name, 'ran-with-credentials')
        path = self.binary(body=('import sys\n'
                                 'if "--version" in sys.argv:\n'
                                 ' print("mysql Ver 8.4.0")\n'
                                 'else:\n'
                                 ' open(%r, "w").close()' % marker))
        connector.set_client_choices({'mysql': path})
        with MySQLConnection(host='localhost', password='private-secret') as conn:
            self.assertEqual(conn._resolve_binary(), path)
            os.chmod(os.path.dirname(path), 0o777)
            with mock.patch.object(connector, 'probe_client', wraps=connector.probe_client) as probe:
                with self.assertRaises(connector.ClientMissingError):
                    conn.run('SELECT 1')
                probe.assert_not_called()
        self.assertFalse(os.path.exists(marker))

    def test_trust_checks_symlink_owner_and_both_alias_and_target_parents(self):
        path = self.binary(folder='target')
        alias_folder = os.path.join(self.temp.name, 'alias')
        os.mkdir(alias_folder)
        link = os.path.join(alias_folder, 'mysql')
        os.symlink(path, link)
        self.assertEqual(connector.probe_client(link)['path'], link)
        for folder in (alias_folder, os.path.dirname(path)):
            os.chmod(folder, 0o777)
            try:
                with self.subTest(folder=folder), self.assertRaisesRegex(ValueError, 'can be changed'):
                    connector.probe_client(link)
            finally:
                os.chmod(folder, 0o755)
        real_lstat = os.lstat
        def lstat(candidate, *args, **kwargs):
            info = real_lstat(candidate, *args, **kwargs)
            return self._fake_stat(info, os.geteuid() + 1) if candidate == link else info
        with mock.patch.object(connector.os, 'lstat', side_effect=lstat):
            with self.assertRaisesRegex(ValueError, 'can be changed by other users'):
                connector.probe_client(link)






    def test_real_probe_timeout_kills_and_reaps_the_process(self):
        import time
        path = self.binary(body='import time; time.sleep(30)')
        children = []
        original = subprocess.Popen
        def start(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append(child)
            return child
        before = time.monotonic()
        with mock.patch.object(connector.subprocess, 'Popen', side_effect=start):
            with self.assertRaisesRegex(ValueError, 'timed out after 5 seconds'):
                connector.probe_client(path)
        self.assertLess(time.monotonic() - before, 8)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())

    def test_probe_deadline_kills_background_child_after_leader_exits(self):
        late_marker = os.path.join(self.temp.name, 'background-survived')
        child_code = 'import time\ntime.sleep(30)\nopen(%r, "w").close()' % late_marker
        path = self.binary(body=('import subprocess, sys\n'
                                 'subprocess.Popen([sys.executable, "-c", %r])\n'
                                 'print("mysql 8.4", flush=True)\n' % child_code))
        leaders = []
        original = subprocess.Popen
        def launch(*args, **kwargs):
            self.assertTrue(kwargs.get('start_new_session'))
            process = original(*args, **kwargs)
            leaders.append(process)
            return process
        started = time.monotonic()
        try:
            with mock.patch.object(connector.subprocess, 'Popen', side_effect=launch), \
                    mock.patch.object(connector.os, 'killpg', wraps=os.killpg) as killpg:
                with self.assertRaisesRegex(ValueError, 'timed out after 5 seconds'):
                    connector.probe_client(path)
                killpg.assert_called_once_with(leaders[0].pid, signal.SIGKILL)
            self.assertLess(time.monotonic() - started, 6.2)
            self.assertEqual(len(leaders), 1)
            self.assertEqual(leaders[0].returncode, 0, 'the leader already exited successfully')
            self.assertFalse(os.path.exists(late_marker))
            # Returning also proves the pipe workers joined: the sleeping
            # child inherited both writers and would hold them for 30 s.
        finally:
            for leader in leaders:
                try:
                    os.killpg(leader.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                leader.wait(timeout=2)




    def test_wrapped_missing_errors_are_found_without_matching_text(self):
        try:
            raise connector.ClientMissingError()
        except ConnectorError:
            try:
                raise ValueError('a redacted wrapper')
            except ValueError as wrapped:
                self.assertIsInstance(connector.client_missing_error(wrapped), connector.ClientMissingError)
                wrapped.__context__.__context__ = wrapped
                self.assertIsInstance(connector.client_missing_error(wrapped), connector.ClientMissingError)
        unrelated = ValueError('no client')
        unrelated.__context__ = unrelated
        self.assertIsNone(connector.client_missing_error(unrelated))
        self.assertIsNone(connector.client_missing_error(ValueError(connector.ClientMissingError.MESSAGE)))

    def test_suppressed_context_and_unrelated_cleanup_failures_are_not_client_missing(self):
        for kind in (PermissionError, OSError, RuntimeError):
            try:
                try:
                    raise connector.ClientMissingError()
                finally:
                    raise kind('credential cleanup denied')
            except kind as exc:
                with self.subTest(kind=kind):
                    self.assertIsNone(connector.client_missing_error(exc))
        # A redaction wrapper ("from None", as ui._lifecycle_reraise does) keeps it.
        try:
            raise connector.ClientMissingError()
        except ConnectorError:
            try:
                raise ValueError('redacted') from None
            except ValueError as exc:
                self.assertIsInstance(connector.client_missing_error(exc), connector.ClientMissingError)

    def test_explicit_causes_take_priority_and_preserve_missing_errors(self):
        missing = connector.ClientMissingError()
        wrapper = RuntimeError('explicit wrapper')
        wrapper.__cause__ = missing
        self.assertIs(connector.client_missing_error(wrapper), missing)
        wrapper = ValueError('new failure')
        wrapper.__cause__ = OSError('actual cause')
        wrapper.__context__ = missing
        wrapper.__suppress_context__ = False
        self.assertIsNone(connector.client_missing_error(wrapper))
        wrapper.__cause__.__cause__ = wrapper
        self.assertIsNone(connector.client_missing_error(wrapper))

    def test_health_and_editor_wrappers_preserve_missing_client_type(self):
        from myflames import editor_session, health
        with self.assertRaises(ValueError) as caught:
            health.health_report(MySQLConnection(host='localhost'), interval=1)
        self.assertTrue(str(caught.exception).startswith('Could not read the server status: '))
        self.assertIsInstance(connector.client_missing_error(caught.exception), connector.ClientMissingError)
        registry = mock.Mock()
        registry.open.side_effect = lambda tab, make, identity: make()
        missing = connector.ClientMissingError()
        with mock.patch.object(editor_session.EditorSession, 'start', side_effect=missing):
            with self.assertRaises(ConnectorError) as caught:
                editor_session._open_session(registry, 'tab', {'host': 'localhost'}, {},
                                             {'type': 'development', 'read_only': False}, 'test', {})
        self.assertIs(connector.client_missing_error(caught.exception), missing)
        registry.remove.assert_called_once()

    def test_cli_hint_for_shared_failure_and_health_prefix_only(self):
        import io
        from myflames import cli
        for error in (connector.ClientMissingError(), ValueError('another problem')):
            stream = io.StringIO()
            with mock.patch.object(sys, 'stderr', stream), self.assertRaises(SystemExit) as caught:
                cli._fail(error)
            self.assertEqual(caught.exception.code, 2)
            hint = ' Install it or pass --mysql-binary /path/to/mysql.'
            self.assertEqual(stream.getvalue(), 'myflames: ' + str(error) +
                             (hint if isinstance(error, connector.ClientMissingError) else '') + '\n')
        from myflames.health import health_report
        try:
            health_report(MySQLConnection(host="localhost"), interval=1)
        except ValueError as exc:
            self.assertIn('--mysql-binary /path/to/mysql', cli._client_error_text(exc))
        else:
            self.fail('health should fail without a client')

    def test_real_cli_missing_client_keeps_hint_and_exit_code(self):
        result = subprocess.run([sys.executable, '-c', 'from myflames import lifecycle; lifecycle.COMMON_BIN_DIRS=(); from myflames.cli import main; main()', '-e', 'SELECT 1'],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, b'')
        self.assertIn(b' Install it or pass --mysql-binary /path/to/mysql.', result.stderr)


class TestVersionAwareClients(unittest.TestCase):
    setUp = TestClientSelection.setUp
    binary = TestClientSelection.binary
    def layout(self):
        latest = self.binary(folder='Cellar/mysql/9.7.0/bin', body='print("mysql Ver 9.7.0")')
        old = self.binary(folder='Cellar/mysql@8.0/8.0.45/bin', body='print("mysql Ver 8.0.45")')
        bundle = self.binary(folder='app/mysql-client/bin', body='print("mysql Ver 8.4.13")')
        os.environ['PATH'] = os.pathsep.join(map(os.path.dirname, (latest, old, bundle)))
        os.environ['MYFLAMES_BUNDLED_CLIENT_DIR'] = os.path.dirname(bundle)
        return latest, old, bundle

    def test_one_candidate_fast_path_has_no_probe(self):
        from myflames import client_tools as ct
        path = self.binary()
        os.environ['PATH'] = os.path.dirname(path)
        with mock.patch.object(ct, 'probe', side_effect=AssertionError('must not probe')):
            self.assertEqual(MySQLConnection(host='fast')._resolve_binary(), path)

    def test_owner_mac_layout_first_contact_matches_series(self):
        from myflames import client_tools as ct
        latest, old, bundle = self.layout()
        for version, expected in (('8.4.14', bundle), ('8.0.46', old)):
            with self.subTest(version=version), mock.patch.object(connector, '_run_bounded',
                    return_value=_fake_completed((version + '\tMySQL Community Server\n').encode())) as run:
                conn = MySQLConnection(host='server-' + version, port=4444, ssl_mode='REQUIRED')
                self.assertEqual(conn._resolve_binary(), expected)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args[0][0][0], latest)
                self.assertEqual(ct.server_facts(conn._endpoint_key())['version'], version)

    def test_chosen_installation_wins_and_removal_has_no_fallback(self):
        from myflames import client_tools as ct
        latest, old, bundle = self.layout()
        conn = MySQLConnection(host='chosen')
        ct.remember_server(conn._endpoint_key(), 'mysql', '8.4.14')
        self.assertEqual(conn._resolve_binary(), bundle)
        connector.set_client_choices({'mysql': old})
        self.assertEqual(conn._resolve_binary(), old)
        self.binary(folder='Cellar/mysql@8.0/8.0.45/bin', body='raise SystemExit(1)')
        with self.assertRaises(connector.ClientMissingError):
            conn._resolve_binary()
        self.binary(folder='Cellar/mysql@8.0/8.0.45/bin', body='print("mysql Ver 8.0.45")')
        self.assertEqual(conn._resolve_binary(), old)
        os.unlink(old)
        with self.assertRaisesRegex(connector.ClientMissingError, re.escape(
                'The client tools chosen in Settings > Client tools are no longer at ' + old + '.')):
            conn._resolve_binary()
        connector.set_client_choices({})
        self.assertEqual(conn._resolve_binary(), bundle)

    def test_off_path_choice_counts_as_a_first_contact_candidate(self):
        mysql = self.binary(folder='offpath')
        maria = self.binary('mariadb', body='print("mariadb Ver 11.4.2-MariaDB")', folder='path')
        os.environ['PATH'] = os.path.dirname(maria)
        connector.set_client_choices({'mysql': mysql})
        with mock.patch.object(connector, '_run_bounded', return_value=_fake_completed(
                b'8.4.14\tMySQL Community Server\n')) as run:
            self.assertEqual(MySQLConnection(host='offpath-choice')._resolve_binary(), mysql)
            self.assertEqual(run.call_count, 1)

    def test_generation_discards_cache_even_without_intervening_resolution(self):
        first = self.binary(folder='first')
        second = self.binary(folder='second')
        os.environ['PATH'] = os.path.dirname(first)
        conn = MySQLConnection(host='generation')
        self.assertEqual(conn._resolve_binary(), first)
        connector.set_client_choices({'mysql': second})
        connector.set_client_choices({})
        os.environ['PATH'] = os.path.dirname(second)
        self.assertEqual(conn._resolve_binary(), second)

    def test_first_contact_is_single_flight_and_preserves_transport(self):
        self.layout()
        entered, release = threading.Event(), threading.Event()
        paths = []
        def read(argv, **kwargs):
            paths.append(argv[0])
            with open(argv[1].split('=', 1)[1]) as handle:
                content = handle.read()
            self.assertIn('host="remote"', content)
            self.assertIn('port=4410', content)
            self.assertIn('ssl-mode="REQUIRED"', content)
            entered.set()
            self.assertTrue(release.wait(5))
            return _fake_completed(b'8.4.14\tMySQL\n')
        with mock.patch.object(connector, '_run_bounded', side_effect=read), \
                concurrent.futures.ThreadPoolExecutor(2) as pool:
            first = pool.submit(MySQLConnection(host='remote', port=4410, ssl_mode='REQUIRED')._resolve_binary)
            self.assertTrue(entered.wait(5))
            second = pool.submit(MySQLConnection(host='remote', port=4410, ssl_mode='REQUIRED')._resolve_binary)
            time.sleep(0.1)
            release.set()
            self.assertEqual(first.result(5), second.result(5))
        self.assertEqual(len(paths), 1)

    def test_first_contact_connection_error_is_not_retried(self):
        self.layout()
        with mock.patch.object(connector, '_run_bounded', return_value=_fake_completed(
                stderr=b"ERROR 2003 (HY000): Can't connect", returncode=1)) as run:
            with self.assertRaisesRegex(ConnectorError, 'ERROR 2003'):
                MySQLConnection(host='failed')._resolve_binary()
        self.assertEqual(run.call_count, 1)

    def test_first_contact_server_login_failure_is_not_treated_as_sql_error(self):
        self.layout()
        for error in (b'ERROR 1040 (HY000): Too many connections',
                      b'ERROR 2013 (HY000) at line 1: Lost connection'):
            with mock.patch.object(connector, '_run_bounded', return_value=_fake_completed(
                    stderr=error, returncode=1)) as run:
                with self.assertRaises(ConnectorError):
                    MySQLConnection(host='failed-login')._resolve_binary()
                self.assertEqual(run.call_count, 1)

    def test_sql_error_and_proxysql_version_are_not_cached(self):
        from myflames import client_tools as ct
        latest, _, _ = self.layout()
        for response in (_fake_completed(b'2.7.3\tProxySQL\n'), _fake_completed(
                stderr=b'ERROR 1064 (42000) at line 1: syntax', returncode=1)):
            with mock.patch.object(connector, '_run_bounded', return_value=response) as run:
                conn = MySQLConnection(host='proxy')
                self.assertEqual(conn._resolve_binary(), latest)
                self.assertIsNone(ct.server_facts(conn._endpoint_key()))
                self.assertEqual(run.call_count, 1)

    def test_2059_retries_other_engine_only_during_first_contact(self):
        from myflames import client_tools as ct
        mysql = self.binary()
        maria = self.binary('mariadb', body='print("mariadb Ver 11.4.2-MariaDB")', folder='maria')
        os.environ['PATH'] = os.pathsep.join(map(os.path.dirname, (mysql, maria)))
        failure = _fake_completed(stderr=b"ERROR 2059 (HY000): Authentication plugin 'client_ed25519' cannot be loaded", returncode=1)
        with mock.patch.object(connector, '_run_bounded', side_effect=[failure,
                _fake_completed(b'11.4.2-MariaDB\tMariaDB Server\n')]) as run:
            conn = MySQLConnection(host='maria')
            self.assertEqual(conn._resolve_binary(), maria)
            self.assertEqual([c.args[0][0] for c in run.call_args_list], [mysql, maria])
        with mock.patch.object(connector, '_run_bounded', return_value=failure) as run:
            with self.assertRaises(connector.ClientAuthPluginError) as caught:
                conn.run('SELECT 1')
            self.assertEqual(run.call_count, 1)
            self.assertEqual(caught.exception.code, 'client-auth-plugin')
            self.assertIn('mariadb 11.4.2 (On PATH)', str(caught.exception))
            self.assertIn('client_ed25519 plugin', str(caught.exception))
            self.assertIs(connector.client_missing_error(ValueError('wrapped', caught.exception)), None)

    def test_first_contact_uses_socket_and_ssh_lease(self):
        from myflames.lifecycle import LifecycleConnection
        self.layout()
        for socket_path in ('/tmp/test-client.sock', None):
            conn = LifecycleConnection(host='through-ssh' if socket_path is None else 'socket',
                                       socket=socket_path, ssh_host='jump' if socket_path is None else None,
                                       ssl_mode='REQUIRED')
            lease = mock.Mock(port=45678)
            def read(argv, **kwargs):
                with open(argv[1].split('=', 1)[1]) as handle:
                    content = handle.read()
                self.assertIn('ssl-mode="REQUIRED"', content)
                if socket_path:
                    self.assertIn('protocol=SOCKET', content)
                    self.assertIn('socket="' + socket_path + '"', content)
                else:
                    self.assertIn('host="127.0.0.1"', content)
                    self.assertIn('port=45678', content)
                return _fake_completed(b'8.4.13\tMySQL\n')
            with mock.patch.object(conn, '_acquire_tunnel', return_value=lease), \
                    mock.patch.object(connector, '_run_bounded', side_effect=read):
                conn._resolve_binary()
            if socket_path is None:
                lease.release.assert_called_once()

    def test_fast_path_and_lifecycle_2059_have_full_client_metadata(self):
        from myflames.lifecycle import LifecycleConnection
        path = self.binary(body=("import sys\n"
            "if '--version' in sys.argv:\n print('mysql Ver 8.4.13')\n"
            "else:\n sys.stderr.write(\"ERROR 2059 (HY000): Authentication plugin 'authentication_ldap_sasl_client' cannot be loaded\"); sys.exit(1)"))
        os.environ['PATH'] = os.path.dirname(path)
        for cls in (MySQLConnection, LifecycleConnection):
            with self.subTest(cls=cls), self.assertRaises(connector.ClientAuthPluginError) as caught:
                cls(host='auth').run('SELECT 1')
            self.assertEqual(str(caught.exception), 'mysql 8.4.13 (On PATH) cannot sign in to this server: '
                'the account uses the authentication_ldap_sasl_client plugin, which these client tools do not include. '
                'Choose client tools that include it in Settings > Client tools.')

    def test_choice_replacement_while_discovering_cannot_publish_stale_binary(self):
        from myflames import client_tools as ct
        first = self.binary(folder='first')
        second = self.binary(folder='second')
        connector.set_client_choices({'mysql': first})
        real = ct.discover_choices
        def discover(choices):
            found = real(choices)
            if os.path.exists(first):
                os.unlink(first)
            connector.set_client_choices({'mysql': second})
            return found
        with mock.patch.object(ct, 'discover_choices', side_effect=discover):
            self.assertEqual(MySQLConnection(host='race')._resolve_binary(), second)


class TestWindowsClientTrust(unittest.TestCase):
    """Check Windows paths without starting a Windows executable on POSIX."""

    def setUp(self):
        windows_os = mock.Mock(wraps=os)
        windows_os.name = 'nt'
        windows_os.path = mock.Mock(wraps=ntpath)
        windows_os.path.realpath.side_effect = ntpath.normpath
        windows_os.path.isfile.return_value = True
        windows_os.environ = {'ProgramFiles': r'C:\Program Files',
                              'USERPROFILE': r'C:\Users\tester'}
        self.windows_os = windows_os
        windows_os.pathsep = ';'
        windows_os.curdir = '.'
        patch = mock.patch.object(connector, 'os', windows_os)
        patch.start()
        self.addCleanup(patch.stop)
        self.real_run_bounded = connector._run_bounded
        run = mock.patch.object(connector, '_run_bounded', return_value=_fake_completed(b'MySQL 8.4'))
        self.run = run.start()
        self.addCleanup(run.stop)

    def test_only_local_exe_names_under_allowed_roots_are_accepted(self):
        for path in (r'C:\Program Files\MySQL\bin\MYSQL.EXE',
                     r'c:\PROGRAM FILES\MariaDB\mariadb.exe',
                     r'C:\Users\tester\bin\mysql.exe'):
            with self.subTest(path=path):
                self.assertEqual(connector.probe_client(path)['path'], path)
                self.assertEqual(self.run.call_args[0][0], [path, '--no-defaults', '--version'])
                self.assertTrue(self.run.call_args[1]['kill_tree'])
        for path in (r'\\server\share\mysql.exe', r'\\?\C:\Program Files\mysql.exe',
                     r'\\.\C:\Program Files\mysql.exe', '//server/share/mysql.exe',
                     r'C:\Program Files\MySQL\mysql', r'C:\Program Files\mariadb',
                     r'C:\Program Files\mysql.cmd', r'C:\Program FilesOther\mysql.exe',
                     r'C:\Users\tester-other\mysql.exe', r'C:\untrusted\mysql.exe',
                     r'D:\Program Files\mysql.exe', r'C:\Program Files\..\untrusted\mysql.exe'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                connector.probe_client(path)

    def test_network_paths_are_refused_before_filesystem_access(self):
        for path in (r'\\server\share\mysql.exe', r'\\?\C:\Program Files\mysql.exe',
                     r'\\.\pipe\mysql.exe', '//server/share/mysql.exe'):
            self.windows_os.path.realpath.reset_mock()
            self.windows_os.path.isfile.reset_mock()
            with self.subTest(path=path), self.assertRaises(ValueError):
                connector.probe_client(path)
            self.windows_os.path.realpath.assert_not_called()
            self.windows_os.path.isfile.assert_not_called()
        self.run.assert_not_called()

    def test_lifecycle_executable_allowlist_accepts_every_installation_root(self):
        names = ('mysql.exe', 'mariadb.exe', 'mysqldump.exe', 'mariadb-dump.exe',
                 'mariadb-backup.exe', 'mysqlsh.exe')
        for key in ('ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432', 'LOCALAPPDATA',
                    'MYFLAMES_USER_PROFILE', 'USERPROFILE'):
            self.windows_os.environ = {key: r'C:\Allowed'}
            for name in names:
                path = ntpath.join(r'C:\Allowed\bin', name.upper())
                with self.subTest(root=key, name=name):
                    connector._check_client_trust(path)
        # Trust validation itself never starts an executable.
        self.run.assert_not_called()

    def test_lifecycle_executable_allowlist_rejects_other_programs_before_resolution(self):
        for name in ('cmd.exe', 'mysqladmin.exe'):
            self.windows_os.path.realpath.reset_mock()
            with self.subTest(name=name), self.assertRaises(ValueError):
                connector._check_client_trust(ntpath.join(r'C:\Program Files\MySQL\bin', name))
            self.windows_os.path.realpath.assert_not_called()
        self.run.assert_not_called()

    def test_lifecycle_executables_still_require_safe_local_roots(self):
        for name in ('mysqldump.exe', 'mariadb-dump.exe', 'mariadb-backup.exe', 'mysqlsh.exe'):
            for root in (r'C:\untrusted', r'C:\Program FilesOther', r'C:\Users\tester-other',
                         r'D:\Program Files', r'C:\Program Files\..\untrusted'):
                with self.subTest(name=name, root=root), self.assertRaises(ValueError):
                    connector._check_client_trust(ntpath.join(root, name))
            for root in (r'\\server\share', r'\\?\C:\Program Files', r'\\.\C:\Program Files', '//server/share'):
                self.windows_os.path.realpath.reset_mock()
                with self.subTest(name=name, root=root), self.assertRaises(ValueError):
                    connector._check_client_trust(ntpath.join(root, name))
                self.windows_os.path.realpath.assert_not_called()
        self.run.assert_not_called()

    def test_lifecycle_executable_resolved_targets_cannot_escape_trusted_roots(self):
        for name in ('mysqldump.exe', 'mariadb-dump.exe', 'mariadb-backup.exe', 'mysqlsh.exe'):
            path = ntpath.join(r'C:\Program Files\MySQL\bin', name)
            for root in (r'C:\outside', r'\\server\share'):
                destination = ntpath.join(root, name)
                self.windows_os.path.realpath.side_effect = lambda value: destination if value == path else value
                with self.subTest(name=name, root=root), self.assertRaises(ValueError):
                    connector._check_client_trust(path)
        self.run.assert_not_called()

    def test_choice_skips_network_locations_before_filesystem_access(self):
        from myflames import client_tools
        with mock.patch.object(client_tools, 'os', self.windows_os):
            with self.assertRaises(ValueError):
                client_tools.validate_choice('mysql', r'\\server\share\mysql.exe')
        self.windows_os.path.realpath.assert_not_called()
        self.windows_os.path.isfile.assert_not_called()
        self.run.assert_not_called()

    def test_each_installation_root_is_accepted_and_unset_roots_are_not(self):
        # MYFLAMES_USER_PROFILE: the desktop's real profile (its backend's USERPROFILE is isolated).
        for key in ('ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432', 'LOCALAPPDATA', 'MYFLAMES_USER_PROFILE',
                    'USERPROFILE'):
            self.windows_os.environ = {key: r'C:\Allowed'}
            with self.subTest(key=key):
                self.assertEqual(connector.probe_client(r'C:\Allowed\bin\mysql.exe')['name'], 'mysql.exe')
        self.windows_os.environ = {}
        with self.assertRaises(ValueError):
            connector.probe_client(r'C:\Allowed\bin\mysql.exe')

    def test_resolved_targets_and_roots_determine_containment(self):
        path = r'C:\Program Files\MySQL\mysql.exe'
        for destination in (r'C:\outside\mysql.exe', r'\\server\share\mysql.exe'):
            self.windows_os.path.realpath.side_effect = lambda value: destination if value == path else value
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                connector.probe_client(path)
        self.windows_os.environ = {'ProgramFiles': r'C:\alias'}
        self.windows_os.path.realpath.side_effect = lambda value: (
            r'D:\actual' if value == r'C:\alias' else r'd:\ACTUAL\bin\mysql.exe')
        self.assertEqual(connector.probe_client(r'C:\alias\bin\mysql.exe')['name'], 'mysql.exe')

    def test_windows_process_group_and_taskkill_argument_list(self):
        # Exercise transport setup/cleanup separately from the mocked probe.
        process = mock.Mock()
        process.pid = 1234
        process.stdin = None
        import io
        process.stdout, process.stderr = io.BytesIO(b''), io.BytesIO(b'')
        process.poll.return_value = None
        with mock.patch.object(connector, '_run_bounded', wraps=self.real_run_bounded), \
                mock.patch.object(connector.subprocess, 'CREATE_NEW_PROCESS_GROUP', 512, create=True), \
                mock.patch.object(connector.subprocess, 'Popen', return_value=process) as popen, \
                mock.patch.object(connector.subprocess, 'run') as taskkill:
            with self.assertRaises(subprocess.TimeoutExpired):
                connector._run_bounded(['mysql.exe', '--version'], timeout=0, kill_tree=True)
        self.assertEqual(popen.call_args[1]['creationflags'], 512)
        self.assertNotIn('start_new_session', popen.call_args[1])
        self.assertEqual(taskkill.call_args[0][0],
                         [r'C:\Windows\System32\taskkill.exe', '/PID', '1234', '/T', '/F'])
        self.assertFalse(taskkill.call_args[1].get('shell', False))
        process.kill.assert_called_once()
        process.wait.assert_called_once()


class TestBundledClientResolution(unittest.TestCase):
    """Bundled stat-only discovery edge cases and unchanged lifecycle jobs."""

    def setUp(self):
        from myflames import lifecycle, client_tools
        patch = mock.patch.object(lifecycle, 'COMMON_BIN_DIRS', ())
        patch.start()
        self.addCleanup(patch.stop)
        connector.set_client_choices({})
        client_tools._FACTS.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bundled = os.path.join(self.tmp.name, "resources", "mysql-client", "bin")
        self.installed = os.path.join(self.tmp.name, "usr", "bin")
        os.makedirs(self.installed)
        self.program = self.make(self.bundled, "mysql")
        self.make(self.bundled, "mysqldump")

    @staticmethod
    def make(folder, name):
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name + (".exe" if os.name == "nt" else ""))
        with open(path, "w") as handle:
            handle.write("")
        os.chmod(path, 0o755)
        return path

    def resolve(self, *entries, bundled=None):
        env = {"PATH": os.pathsep.join(entries), "MYFLAMES_BUNDLED_CLIENT_DIR": bundled or self.bundled}
        with mock.patch.dict(os.environ, env):
            return MySQLConnection(host="h")._resolve_binary()

    def same(self, a, b):
        self.assertEqual(os.path.normcase(os.path.realpath(a)), os.path.normcase(os.path.realpath(b)))




    def test_bundled_mysql_is_the_last_resort(self):
        self.same(self.resolve(self.installed, self.bundled), self.program)
        # Also when its folder is not on PATH at all.
        self.same(self.resolve(self.installed), self.program)

    def test_a_folder_name_holding_the_path_separator_finds_only_the_bundled_client(self):
        # Abuse case: with the app under /Users/Shared/x:y/, searching that
        # folder as a PATH string would split it and run a mysql planted in
        # /Users/Shared/x. The bundled program is built directly instead.
        planted = self.make(os.path.join(self.tmp.name, "x"), "mysql")
        bundled = os.path.join(self.tmp.name, "x" + os.pathsep + "y", "mysql-client", "bin")
        program = self.make(bundled, "mysql")
        found = self.resolve(self.installed, bundled=bundled)
        self.same(found, program)
        self.assertNotEqual(os.path.normcase(found), os.path.normcase(planted))

    def test_only_an_absolute_bundled_folder_counts(self):
        with mock.patch.dict(os.environ, {"PATH": self.installed, "MYFLAMES_BUNDLED_CLIENT_DIR": "mysql-client/bin"}):
            with self.assertRaises(ConnectorError):
                MySQLConnection(host="h")._resolve_binary()


    def test_lifecycle_tools_find_installed_ones_first_then_the_bundled_ones(self):
        from myflames import lifecycle
        dump = self.make(self.installed, "mysqldump")
        env = {"PATH": os.pathsep.join([self.bundled + os.sep, self.installed]), "MYFLAMES_BUNDLED_CLIENT_DIR": self.bundled}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(lifecycle.subprocess, "run", side_effect=OSError("not probed")):
            tools = lifecycle.detect_tools(common_dirs=())["tools"]
        self.same(tools["mysqldump"]["path"], dump)
        self.same(tools["mysql"]["path"], self.program)
        self.assertNotIn("path", tools["mariadb"])


class TestArgvConstruction(unittest.TestCase):
    """The assembled argv must match the mysql CLI's expected ordering."""

    def _conn(self, **kw):
        kw.setdefault("binary", "/bin/mysql")
        return MySQLConnection(host="h", **kw)

    def test_exclusive_defaults_file_is_first_arg(self):
        c = self._conn(user="u", password="p")
        try:
            argv = c._build_argv("SELECT 1")
            self.assertEqual(argv[0], "/bin/mysql")
            self.assertTrue(argv[1].startswith("--defaults-file="))
        finally:
            c.close()

    def test_batch_flags_present(self):
        """The client runs in ``--batch --skip-column-names --silent`` mode.

        ``--raw`` is intentionally NOT used: without it, the mysql CLI
        escapes embedded newlines/tabs as ``\\n``/``\\t`` so each row stays
        on a single line, which is what query_rows expects. The value-side
        unescape is done by :meth:`MySQLConnection._unescape_mysql`.
        """
        c = self._conn(user="u")
        try:
            argv = c._build_argv("SELECT 1")
            for flag in ("--batch", "--skip-column-names", "--silent"):
                self.assertIn(flag, argv)
            self.assertNotIn("--raw", argv, "--raw must NOT be set (breaks multi-line rows)")
        finally:
            c.close()

    def test_local_infile_disabled_right_after_defaults_file(self):
        """A hostile server can request a readable local file in reply to any
        statement when the client's LOCAL capability stays at its build
        default (MariaDB's is AUTO/ON). Every invocation pins it off; only the
        CSV LOAD DATA session overrides it later in argv (csv_import_api).
        """
        for method in ("run", "run_write", "run_script"):
            with self.subTest(method=method), self._conn(binary="/bin/mariadb") as conn, \
                    mock.patch.object(connector, "_run_bounded", return_value=_fake_completed()) as run:
                getattr(conn, method)("SELECT 1")
                argv = run.call_args[0][0]
                self.assertTrue(argv[1].startswith("--defaults-file="))
                self.assertEqual(argv[2], "--local-infile=0", argv)

    def test_utf8mb4_for_reads_and_writes_before_and_after_engine_detection(self):
        # A MySQL (or unknown) client talking to MariaDB 10.11 needs SET
        # NAMES too: its utf8mb4 handshake collation can be unrecognized.
        # Initial connections must work before is_mariadb() can be called.
        for binary in ("/bin/mysql", "/bin/mariadb", "/bin/unknown-client"):
            for engine in (None, False, True):
                for method in ("run", "run_write"):
                    with self.subTest(binary=binary, mariadb=engine, method=method), \
                            self._conn(binary=binary) as conn, \
                            mock.patch.object(connector, "_run_bounded",
                                              return_value=_fake_completed()) as run:
                        conn._is_mariadb_cache = engine
                        getattr(conn, method)("SELECT 1")
                        run.assert_called_once()
                        argv = run.call_args[0][0]
                        self.assertTrue(argv[1].startswith("--defaults-file="))
                        self.assertIn("--default-character-set=utf8mb4", argv)
                        self.assertIn("--init-command=SET NAMES utf8mb4", argv)
                        self.assertIn("--batch", argv)
                        if method == "run_write":
                            self.assertIn("--skip-reconnect", argv)
                            self.assertEqual(run.call_args[1]["input"], b"SELECT 1")

    def test_unescape_mysql_sequences(self):
        """query_rows values are unescaped — \\n and \\t round-trip."""
        u = MySQLConnection._unescape_mysql
        self.assertEqual(u("line1\\nline2"), "line1\nline2")
        self.assertEqual(u("a\\tb"), "a\tb")
        self.assertEqual(u("back\\\\slash"), "back\\slash")
        self.assertEqual(u(None), None)
        self.assertEqual(u(""), "")

    def test_defaults_file_has_public_key_fetch(self):
        """``get-server-public-key=ON`` lets caching_sha2_password users
        authenticate over plaintext (no TLS required) — it's a no-op for
        MariaDB."""
        c = MySQLConnection(host="h", user="u", password="p")
        path = c._ensure_defaults_file()
        try:
            with open(path) as f:
                content = f.read()
            self.assertIn("get-server-public-key=ON", content)
        finally:
            c.close()

    def test_database_flag(self):
        c = self._conn(user="u", database="shop")
        try:
            argv = c._build_argv("SELECT 1")
            self.assertIn("-D", argv)
            self.assertIn("shop", argv)
        finally:
            c.close()

    def test_sql_passed_via_dash_e(self):
        c = self._conn(user="u")
        try:
            argv = c._build_argv("SELECT now()")
            self.assertEqual(argv[-2], "-e")
            self.assertEqual(argv[-1], "SELECT now()")
        finally:
            c.close()

    def test_password_never_on_argv(self):
        """The defaults-extra-file is the ONLY place the password may live."""
        c = self._conn(user="u", password="hunter2")
        try:
            argv = c._build_argv("SELECT 1")
            for a in argv:
                self.assertNotIn("hunter2", a, "password leaked into argv")
        finally:
            c.close()


class TestRunAndQueries(unittest.TestCase):
    """Subprocess execution is mocked so tests run without any DB."""

    def setUp(self):
        self.conn = MySQLConnection(host="h", user="u", password="p", binary="/bin/mysql")

    def tearDown(self):
        self.conn.close()

    def _patch_run(self, stdout=b"", stderr=b"", returncode=0):
        return mock.patch(
            "myflames.connector._run_bounded",
            return_value=_fake_completed(stdout, stderr, returncode),
        )

    def test_run_returns_stdout_decoded(self):
        with self._patch_run(stdout=b"hello\tworld\n"):
            self.assertEqual(self.conn.run("SELECT 1"), "hello\tworld\n")

    def test_run_raises_on_nonzero_exit(self):
        with self._patch_run(stderr=b"Access denied for user 'u'@'h'", returncode=1):
            with self.assertRaises(ConnectorError) as ctx:
                self.conn.run("SELECT 1")
            self.assertIn("Access denied", str(ctx.exception))

    def test_run_raises_on_timeout(self):
        with mock.patch(
            "myflames.connector._run_bounded",
            side_effect=subprocess.TimeoutExpired(cmd=["mysql"], timeout=5),
        ):
            with self.assertRaises(ConnectorError) as ctx:
                self.conn.run("SELECT sleep(60)", timeout=5)
            self.assertIn("timed out", str(ctx.exception))

    def test_query_rows_parses_tab_separated(self):
        with self._patch_run(stdout=b"1\talpha\n2\tbeta\n3\tgamma\n"):
            rows = self.conn.query_rows("SELECT id, name FROM t")
            self.assertEqual(rows, [["1", "alpha"], ["2", "beta"], ["3", "gamma"]])

    def test_query_rows_skips_empty_lines(self):
        with self._patch_run(stdout=b"1\ta\n\n2\tb\n"):
            self.assertEqual(len(self.conn.query_rows("x")), 2)

    def test_query_kv_parses_two_columns(self):
        with self._patch_run(stdout=b"sort_buffer_size\t262144\ntmp_table_size\t16777216\n"):
            kv = self.conn.query_kv("SHOW SESSION VARIABLES")
            self.assertEqual(kv["sort_buffer_size"], "262144")
            self.assertEqual(kv["tmp_table_size"], "16777216")

    def test_query_dicts_uses_supplied_columns(self):
        with self._patch_run(stdout=b"10\t20\t30\n"):
            rows = self.conn.query_dicts("x", ["a", "b", "c"])
            self.assertEqual(rows, [{"a": "10", "b": "20", "c": "30"}])

    def test_server_version_cached(self):
        with self._patch_run(stdout=b"8.4.8\n") as mocked_run:
            v1 = self.conn.server_version()
            v2 = self.conn.server_version()
            self.assertEqual(v1, "8.4.8")
            self.assertEqual(v2, "8.4.8")
            # Only called once thanks to caching
            self.assertEqual(mocked_run.call_count, 1)

    def test_is_mariadb_true(self):
        with self._patch_run(stdout=b"11.4.10-MariaDB-ubu2404\n"):
            self.assertTrue(self.conn.is_mariadb())

    def test_is_mariadb_false_for_mysql(self):
        with self._patch_run(stdout=b"8.4.8\n"):
            self.assertFalse(self.conn.is_mariadb())

    def test_explain_analyze_uses_mysql_syntax(self):
        """Prefixes SET explain_json_format_version=2 for MySQL."""
        calls = []
        def fake_run(argv, **kw):
            calls.append(argv)
            if "SELECT @@version" in argv[-1]:
                return _fake_completed(b"8.4.8\n")
            return _fake_completed(b'{"operation":"Table scan"}\n')
        with mock.patch("myflames.connector._run_bounded", side_effect=fake_run):
            out = self.conn.explain_analyze("SELECT * FROM t WHERE id=1")
            self.assertIn("Table scan", out)
        # Last call should have EXPLAIN ANALYZE FORMAT=JSON in its SQL.
        last_sql = calls[-1][-1]
        self.assertIn("EXPLAIN ANALYZE FORMAT=JSON", last_sql)
        self.assertIn("explain_json_format_version=2", last_sql)

    def test_explain_analyze_uses_mariadb_syntax(self):
        """MariaDB uses ANALYZE FORMAT=JSON instead of EXPLAIN ANALYZE."""
        calls = []
        def fake_run(argv, **kw):
            calls.append(argv)
            if "SELECT @@version" in argv[-1]:
                return _fake_completed(b"11.4.10-MariaDB\n")
            return _fake_completed(b'{"query_block":{}}\n')
        with mock.patch("myflames.connector._run_bounded", side_effect=fake_run):
            self.conn.explain_analyze("SELECT 1")
        last_sql = calls[-1][-1]
        self.assertIn("ANALYZE FORMAT=JSON", last_sql)
        self.assertNotIn("EXPLAIN ANALYZE FORMAT=JSON", last_sql)

    def test_explain_analyze_gates_json_format_variable_by_server_version(self):
        for version, maria, has_setting in (
                ('8.3.0', False, True),
                ('8.4.11', False, True), ('9.7.2', False, True),
                ('26.7.0', False, True), ('11.4.13-MariaDB', True, False)):
            with self.subTest(version=version), \
                    mock.patch.object(self.conn, 'server_version', return_value=version), \
                    mock.patch.object(self.conn, 'is_mariadb', return_value=maria), \
                    mock.patch.object(self.conn, 'run', return_value='{}') as run:
                self.assertEqual(self.conn.explain_analyze('SELECT 1 -- trailing'), '{}')
                sql = run.call_args[0][0]
                self.assertEqual('explain_json_format_version=2' in sql, has_setting)
                self.assertIn('SET SESSION TRANSACTION READ ONLY; START TRANSACTION READ ONLY;', sql)
                self.assertIn('SELECT 1 -- trailing\n;\nROLLBACK', sql)
                self.assertNotIn('--force', run.call_args[1]['extra_flags'])

    def test_explain_analyze_refuses_before_sending_on_mysql_without_explain_analyze(self):
        # MySQL 5.7 (and 8.0 before 8.0.18) has no EXPLAIN ANALYZE: one plain
        # sentence, and the client never runs the statement or its wrapper.
        for version in ('5.7.44-log', '8.0.17', '8.0.46', '8.2.0'):
            with self.subTest(version=version), \
                    mock.patch.object(self.conn, 'server_version', return_value=version), \
                    mock.patch.object(self.conn, 'is_mariadb', return_value=False), \
                    mock.patch.object(self.conn, 'run') as run:
                with self.assertRaisesRegex(ConnectorError, 'needs EXPLAIN ANALYZE FORMAT=JSON, which MySQL has from 8.3.*'
                                            'nothing was sent'):
                    self.conn.explain_analyze('SELECT 1')
                run.assert_not_called()

    def test_explain_analyze_uses_capture_error_mapping_for_old_mysql(self):
        raw = "ERROR 1235 (42000) at line 3: This version does not yet support EXPLAIN ANALYZE with JSON format"
        with mock.patch.object(self.conn, 'server_version', return_value='8.3.0'), \
                mock.patch.object(self.conn, 'is_mariadb', return_value=False), \
                mock.patch.object(self.conn, 'run', side_effect=ConnectorError(raw)):
            with self.assertRaisesRegex(ConnectorError, 'needs MySQL 8.4 or later') as caught:
                self.conn.explain_analyze('SELECT 1')
        self.assertIn('this server is 8.3.0', str(caught.exception))
        self.assertNotIn('at line 3', str(caught.exception))


class TestTerminalText(unittest.TestCase):
    """Server text printed on a terminal cannot carry control sequences."""

    def test_c0_c1_and_del_become_visible_escapes(self):
        from myflames.connector import terminal_text
        hostile = "a\x1b]52;c;ZXZpbA==\x07b\rc\x9b2Jd\x7fe\x00f\x9d0;t\x9c"
        safe = terminal_text(hostile)
        self.assertIsNone(re.search(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", safe))
        self.assertEqual(safe, "a\\u001b]52;c;ZXZpbA==\\u0007b\\u000dc\\u009b2Jd\\u007fe\\u0000f"
                               "\\u009d0;t\\u009c")
        self.assertEqual(terminal_text("line 1\n\tline 2 é ☃"), "line 1\n\tline 2 é ☃")
        self.assertEqual(terminal_text(""), "")
        self.assertIsNone(terminal_text(None))
        self.assertEqual(terminal_text(safe), safe, "escaping twice changes nothing")

    def test_json_text_keeps_its_values(self):
        import json
        from myflames.connector import terminal_text
        value = {"sql": "SELECT '\x1b[31m\x07\x9b\x7f'", "n": [1, "\u2028"]}
        text = json.dumps(value, indent=2, ensure_ascii=False)
        safe = terminal_text(text)
        self.assertIsNone(re.search(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", safe))
        self.assertEqual(json.loads(safe), value)


class TestRowSplitting(unittest.TestCase):
    """Only \\n separates rows in batch output; a raw \\r (CRLF statement
    text) or a Unicode line separator inside a value must not split it."""

    def test_raw_carriage_return_and_line_separator_stay_in_the_value(self):
        conn = MySQLConnection.__new__(MySQLConnection)
        output = "1\tSELECT 1\r\\nFROM t\u2028x\n2\tok\n"
        with mock.patch.object(MySQLConnection, "run", return_value=output):
            self.assertEqual(conn.query_rows("x"),
                             [["1", "SELECT 1\r\nFROM t\u2028x"], ["2", "ok"]])

    def test_discover_rows_use_the_same_rule(self):
        from myflames import discover
        conn = mock.Mock()
        conn.run.return_value = "7\tUPDATE t SET a=1\r\\nWHERE id=2\n"
        self.assertEqual(discover._rows(conn, "SELECT 1"), [["7", "UPDATE t SET a=1\r\nWHERE id=2"]])

    def test_crlf_records_preserve_embedded_carriage_returns(self):
        with MySQLConnection('h') as conn, mock.patch.object(conn, 'run', return_value=
                '1\tbefore\rafter\r\n2\tline\r\\nnext\r\n\r\n3\tlast\r'):
            self.assertEqual(conn.query_rows('x'),
                             [['1', 'before\rafter'], ['2', 'line\r\nnext'], ['3', 'last\r']])


class TestSafetyRegressions(unittest.TestCase):
    def test_client_environment_is_private_and_allowlisted(self):
        poisoned = {key: 'must-not-reach-client' for key in (
            'MYSQL_HOST', 'MYSQL_PWD', 'MYSQL_UNIX_PORT', 'LIBMYSQL_ENABLE_CLEARTEXT_PLUGIN',
            'MYSQL_TEST_LOGIN_FILE', 'MYFLAMES_VAULT_PASSPHRASE', 'UNRELATED_API_SECRET')}
        poisoned.update(LANG='C', LC_ALL='C', TZ='UTC')
        for method in ('run', 'run_write'):
            homes = []
            def inspect_client(argv, **kwargs):
                env = kwargs['env']
                home = env['HOME']
                homes.append(home)
                self.assertTrue(os.path.isdir(home))
                self.assertEqual(os.listdir(home), [])
                self.assertEqual(env['USERPROFILE'], home)
                self.assertNotEqual(home, os.environ.get('HOME'))
                self.assertEqual(env['MYSQL_TEST_LOGIN_FILE'], os.path.join(home, 'no-login-file'))
                self.assertFalse(os.path.exists(env['MYSQL_TEST_LOGIN_FILE']))
                self.assertEqual(set(env) - set(MySQLConnection._STDIN_ENV),
                                 {'HOME', 'USERPROFILE', 'MYSQL_TEST_LOGIN_FILE'})
                for key in poisoned:
                    if key not in MySQLConnection._STDIN_ENV and key != 'MYSQL_TEST_LOGIN_FILE':
                        self.assertNotIn(key, env)
                for key in MySQLConnection._STDIN_ENV:
                    if key in os.environ:
                        self.assertEqual(env[key], os.environ[key])
                return _fake_completed(b'ok')
            with self.subTest(method=method), \
                    MySQLConnection('h', password='secret', binary='mysql') as conn, \
                    mock.patch.dict(os.environ, poisoned), \
                    mock.patch.object(connector, '_run_bounded', side_effect=inspect_client):
                for _ in range(2):
                    self.assertEqual(getattr(conn, method)('SELECT 1'), 'ok')
                    self.assertFalse(os.path.exists(homes[-1]))
            self.assertNotEqual(homes[0], homes[1])

    def test_public_key_fetch_is_omitted_only_for_explicit_disabled_tls(self):
        for mode in (None, 'REQUIRED', 'PREFERRED', 'VERIFY_CA', 'VERIFY_IDENTITY', 'DISABLED', 'disabled'):
            with self.subTest(mode=mode), MySQLConnection('h', ssl_mode=mode) as conn:
                content = conn._build_defaults_file_content()
                self.assertEqual('get-server-public-key=ON\n' in content,
                                 (mode or '').upper() != 'DISABLED')

    def test_call_owned_option_files_are_removed_on_success_and_failure(self):
        for method in ('run', 'run_write'):
            for outcome in ('ok', 'exit', 'timeout', 'missing', 'interrupt'):
                paths = []
                def client(argv, **kwargs):
                    path = argv[1].split('=', 1)[1]
                    paths.append(path)
                    self.assertTrue(os.path.isfile(path))
                    with open(path) as handle:
                        self.assertIn('password="secret"', handle.read())
                    if outcome == 'timeout':
                        raise subprocess.TimeoutExpired(argv, 1)
                    if outcome == 'missing':
                        raise FileNotFoundError('missing client')
                    if outcome == 'interrupt':
                        raise KeyboardInterrupt()
                    return _fake_completed(b'ok', b'failure', 1 if outcome == 'exit' else 0)
                with self.subTest(method=method, outcome=outcome), \
                        MySQLConnection('h', password='secret', binary='mysql') as conn, \
                        mock.patch.object(connector, '_run_bounded', side_effect=client):
                    for _ in range(2):
                        if outcome == 'ok':
                            self.assertEqual(getattr(conn, method)('SELECT 1'), 'ok')
                        else:
                            error = KeyboardInterrupt if outcome == 'interrupt' else ConnectorError
                            with self.assertRaises(error):
                                getattr(conn, method)('SELECT 1')
                        self.assertFalse(os.path.exists(paths[-1]))
                        self.assertIsNone(conn._defaults_file_path)

    def test_explicit_option_files_survive_calls_until_close(self):
        for method in ('run', 'run_write'):
            for fails in (False, True):
                with self.subTest(method=method, fails=fails), \
                        MySQLConnection('h', password='secret', binary='mysql') as conn:
                    path = conn._ensure_defaults_file()
                    with mock.patch.object(connector, '_run_bounded',
                                           return_value=_fake_completed(returncode=int(fails))):
                        if fails:
                            with self.assertRaises(ConnectorError):
                                getattr(conn, method)('SELECT 1')
                        else:
                            getattr(conn, method)('SELECT 1')
                    self.assertEqual(conn._defaults_file_path, path)
                    self.assertTrue(os.path.isfile(path))
                self.assertFalse(os.path.exists(path))

    def test_option_file_is_removed_if_binary_resolution_fails(self):
        for method in ('run', 'run_write'):
            with self.subTest(method=method), tempfile.TemporaryDirectory() as directory, \
                    mock.patch('tempfile.tempdir', directory), \
                    MySQLConnection('h', password='secret') as conn, \
                    mock.patch.object(conn, '_resolve_binary', side_effect=ConnectorError('missing')):
                with self.assertRaises(ConnectorError):
                    getattr(conn, method)('SELECT 1')
                self.assertIsNone(conn._defaults_file_path)
                self.assertEqual(os.listdir(directory), [])

    def test_all_option_strings_use_one_escaped_quoted_encoding(self):
        # Literal backslashes (including \n and \t in Windows paths), quotes,
        # comment markers, spaces and actual controls cannot become options.
        value = ' C:\\new dir\\tls\\"cert"#;=\t\r\n\b '
        expected = '" C:\\\\new dir\\\\tls\\\\\\"cert\\"#;=\\t\\r\\n\\b "'
        fields = ('host', 'user', 'password', 'ssl_ca', 'ssl_cert', 'ssl_key', 'ssl_cipher', 'ssl_mode')
        for field in fields:
            with self.subTest(field=field):
                options = {'host': 'h', field: value}
                with MySQLConnection(**options) as conn:
                    content = conn._build_defaults_file_content()
                self.assertIn(field.replace('_', '-') + '=' + expected + '\n', content)
                self.assertEqual(content.count('[client]'), 1)

    def test_read_timeout_redacts_before_truncating_and_suppresses_traceback_secrets(self):
        password = 'connection-secret'
        sql = "ALTER USER app IDENTIFIED BY '" + 'private-' * 30 + "'; SELECT 'connection-secret'"
        # Both old subprocess.run and the bounded transport invoke Popen.
        with MySQLConnection('h', password=password, binary='mysql') as conn, \
                mock.patch.object(subprocess, 'Popen', side_effect=subprocess.TimeoutExpired(
                    ['mysql', '-e', sql], 1, output=b'connection-secret')):
            try:
                conn.run(sql, timeout=1)
            except ConnectorError:
                text = traceback.format_exc()
            else:
                self.fail('timeout must raise')
        self.assertIn('timed out', text)
        self.assertNotIn('private-', text)
        self.assertNotIn(password, text)

    def test_all_client_paths_isolate_ambient_configuration(self):
        import pathlib
        hostile = {'MYSQL_PWD': 'ambient-secret', 'MYSQL_HOST': 'other-host',
                   'MYSQL_HOME': '/other-config', 'MYSQL_UNIX_PORT': '/other.sock',
                   'MYSQL_TEST_LOGIN_FILE': '/other-login',
                   'LIBMYSQL_ENABLE_CLEARTEXT_PLUGIN': '1',
                   'MYFLAMES_VAULT_PASSPHRASE': 'vault-secret'}
        for method in ('run', 'run_write'):
            homes = []
            def execute(argv, **kwargs):
                self.assertTrue(argv[1].startswith('--defaults-file='))
                env = kwargs['env']
                homes.append(env['HOME'])
                self.assertEqual(env['USERPROFILE'], env['HOME'])
                self.assertTrue(pathlib.Path(env['HOME']).is_dir())
                self.assertEqual(list(pathlib.Path(env['HOME']).iterdir()), [])
                self.assertFalse(pathlib.Path(env['MYSQL_TEST_LOGIN_FILE']).exists())
                self.assertEqual(pathlib.Path(env['MYSQL_TEST_LOGIN_FILE']).parent,
                                 pathlib.Path(env['HOME']))
                for key in hostile:
                    if key != 'MYSQL_TEST_LOGIN_FILE':
                        self.assertNotIn(key, env)
                return _fake_completed(b'1\n')
            with self.subTest(method=method), mock.patch.dict(os.environ, hostile), \
                    MySQLConnection('localhost', binary='mysql') as conn, \
                    mock.patch.object(connector, '_run_bounded', side_effect=execute):
                self.assertEqual(getattr(conn, method)('SELECT 1'), '1\n')
                self.assertTrue(all(not pathlib.Path(home).exists() for home in homes))

    def test_password_file_lives_only_for_the_client_invocation(self):
        for method in ('run', 'run_write'):
            paths = []
            def execute(argv, **kwargs):
                path = argv[1].split('=', 1)[1]
                paths.append(path)
                self.assertTrue(os.path.isfile(path))
                return _fake_completed(b'1')
            with self.subTest(method=method), MySQLConnection('localhost', binary='mysql') as conn, \
                    mock.patch.object(connector, '_run_bounded', side_effect=execute):
                getattr(conn, method)('SELECT 1')
                self.assertIsNone(conn._defaults_file_path)
                self.assertFalse(os.path.exists(paths[-1]))
                getattr(conn, method)('SELECT 2')
                self.assertNotEqual(paths[0], paths[1])
                self.assertFalse(os.path.exists(paths[-1]))

    def test_client_environment_is_removed_after_spawn_failure(self):
        homes = []
        def fail(argv, **kwargs):
            homes.append(kwargs['env']['HOME'])
            raise FileNotFoundError('missing client')
        with MySQLConnection('localhost', binary='mysql') as conn, \
                mock.patch.object(connector, '_run_bounded', side_effect=fail):
            with self.assertRaises(ConnectorError):
                conn.run('SELECT 1')
        self.assertTrue(homes)
        self.assertTrue(all(not os.path.exists(home) for home in homes))

    def test_run_write_uses_stdin_scrubs_environment_and_requires_write_flags(self):
        sql = "ALTER USER app IDENTIFIED BY 'new-secret'"
        with MySQLConnection('h', password='connection-secret', binary='mysql') as conn, \
                mock.patch.dict(os.environ, {'MYSQL_PWD': 'bad', 'MYSQL_HOST': 'bad',
                                             'MYFLAMES_VAULT_PASSPHRASE': 'bad'}), \
                mock.patch.object(connector, '_run_bounded', return_value=_fake_completed(b'ok')) as run:
            self.assertEqual(conn.run_write(sql), 'ok')
            argv, kw = run.call_args[0][0], run.call_args[1]
            self.assertEqual(kw['input'], sql.encode('utf-8'))
            self.assertNotIn('-e', argv)
            self.assertNotIn(sql, ' '.join(argv))
            for flag in ('--comments', '--binary-mode', '--skip-reconnect'):
                self.assertIn(flag, argv)
            for key in ('MYSQL_PWD', 'MYSQL_HOST', 'MYFLAMES_VAULT_PASSPHRASE'):
                self.assertNotIn(key, kw['env'])
            self.assertNotEqual(kw['env']['HOME'], os.environ['HOME'])
        self.assertFalse(os.path.exists(kw['env']['HOME']))

    def test_run_write_redacts_client_errors_and_timeout_exception_chains(self):
        sql = "ALTER USER app IDENTIFIED BY 'new-secret'"
        errors = [ConnectorError("connection-secret IDENTIFIED BY 'new-secret' AS 0x414243"),
                  subprocess.TimeoutExpired(['mysql', sql], 1, output=b'new-secret', stderr=b'connection-secret')]
        for error in errors:
            with self.subTest(error=type(error).__name__), \
                    MySQLConnection('h', password='connection-secret', binary='mysql') as conn, \
                    mock.patch.object(subprocess, 'Popen', side_effect=error):
                try:
                    conn.run_write(sql)
                except ConnectorError:
                    text = traceback.format_exc()
                else:
                    self.fail('expected failure')
                for secret in ('connection-secret', 'new-secret', '414243'):
                    self.assertNotIn(secret, text)


class TestBoundedTransport(unittest.TestCase):
    """Exercise OS pipes with a local Python child, never a database."""
    def child(self, script):
        real_popen = subprocess.Popen
        self.children = []

        def launch(argv, **kwargs):
            child = real_popen([sys.executable, '-c', script] + argv[1:], **kwargs)
            self.children.append(child)
            return child
        return mock.patch.object(subprocess, 'Popen', side_effect=launch)

    def test_utf8_result_round_trips_under_c_locale(self):
        sql = "SELECT 'é', '中', '😀'"
        # Use byte pipes and argv bytes just as a UTF-8-configured client
        # does; the child's Python text encoding must not mask a locale bug.
        script = (
            'import os, sys\n'
            'assert "--default-character-set=utf8mb4" in sys.argv\n'
            'assert "--init-command=SET NAMES utf8mb4" in sys.argv\n'
            'data = os.fsencode(sys.argv[-1]) if "-e" in sys.argv else sys.stdin.buffer.read()\n'
            'assert data == %r\n'
            'sys.stdout.buffer.write(data)\n' % sql.encode('utf-8'))
        for method in ('run', 'run_write'):
            with self.subTest(method=method), \
                    mock.patch.dict(os.environ, {'LC_ALL': 'C', 'PYTHONUTF8': '0',
                                                 'PYTHONCOERCECLOCALE': '0'}), \
                    self.child(script), MySQLConnection('h', binary='mysql') as conn:
                self.assertEqual(getattr(conn, method)(sql), sql)
                self.assertIsNotNone(self.children[0].poll())

    def test_oversized_stdout_and_stderr_stop_the_client(self):
        for method in ('run', 'run_stdin'):
            for stream in ('stdout', 'stderr'):
                with self.subTest(method=method, stream=stream), self.child(
                        'import sys\nfor i in range(256):\n sys.%s.buffer.write(b"x" * 8192)\n' % stream), \
                        mock.patch.object(connector, 'MAX_STDOUT_BYTES', 1024, create=True), \
                        mock.patch.object(connector, 'MAX_STDERR_BYTES', 1024, create=True), \
                        MySQLConnection('h', binary='mysql') as conn:
                    with self.assertRaisesRegex(ConnectorError, stream + ' response exceeds'):
                        getattr(conn, method)('SELECT 1')
                    self.assertIsNotNone(self.children[0].poll(), 'oversized client must be reaped')

    def test_stdin_nonzero_exit_redacts_raw_password_and_credential_clauses(self):
        message = "ERROR 1819: connection-secret IDENTIFIED BY 'new-secret' AS 0x414243"
        with self.child('import sys\nsys.stderr.write(%r)\nsys.exit(1)' % message), \
                MySQLConnection('h', binary='mysql', password='connection-secret') as conn:
            with self.assertRaises(ConnectorError) as caught:
                conn.run_stdin("ALTER USER app IDENTIFIED BY 'new-secret'")
        self.assertIn('1819', str(caught.exception))
        for secret in ('connection-secret', 'new-secret', '414243'):
            self.assertNotIn(secret, str(caught.exception))

    def test_byte_limits_accept_exact_boundary_and_count_utf8_bytes(self):
        for method in ('run', 'run_stdin'):
            with self.subTest(method=method), self.child(
                    'import sys\nsys.stdout.buffer.write("é".encode() * 512)\nsys.stderr.buffer.write(b"e" * 1024)'), \
                    mock.patch.object(connector, 'MAX_STDOUT_BYTES', 1024), \
                    mock.patch.object(connector, 'MAX_STDERR_BYTES', 1024), \
                    MySQLConnection('h', binary='mysql') as conn:
                self.assertEqual(getattr(conn, method)('SELECT 1'), 'é' * 512)
        with self.child('import sys\nsys.stdout.buffer.write("é".encode() * 513)'), \
                mock.patch.object(connector, 'MAX_STDOUT_BYTES', 1024), \
                MySQLConnection('h', binary='mysql') as conn:
            with self.assertRaisesRegex(ConnectorError, '1024-byte limit'):
                conn.run('SELECT 1')

    def test_drain_both_pipes_while_writing_large_stdin(self):
        script = ('import sys\n'
                  'for i in range(32):\n'
                  ' sys.stdout.buffer.write(b"o" * 8192)\n'
                  ' sys.stderr.buffer.write(b"e" * 8192)\n'
                  'sys.stdout.buffer.write(str(len(sys.stdin.buffer.read())).encode())\n')
        with self.child(script), MySQLConnection('h', binary='mysql') as conn:
            sql = 'x' * (512 * 1024)
            self.assertEqual(conn.run_write(sql, timeout=10), 'o' * (32 * 8192) + str(len(sql)))
        self.assertIsNotNone(self.children[0].poll())

    def test_timeout_reaps_client_with_open_pipes(self):
        # The child blocks on its own pipe without sleeping or consuming CPU.
        with self.child('import os\nr,w=os.pipe()\nos.read(r,1)'), \
                MySQLConnection('h', binary='mysql') as conn:
            with self.assertRaisesRegex(ConnectorError, 'timed out'):
                conn.run_write('SELECT 1', timeout=0.2)
            self.assertIsNotNone(self.children[0].poll())

    def test_identity_tail_has_its_own_budget_and_retains_output_after_reaping(self):
        for newline in ('\n', '\r\n'):
            output = newline.join(('myflames_reading_0', 'name\tvalue', 'myflames_reading_1', ''))
            script = ('import os,sys\nassert "--unbuffered" in sys.argv\n'
                      'os.write(1, %r)\nr,w=os.pipe()\nos.read(r,1)' % output.encode())
            with self.subTest(newline=newline), self.child(script), MySQLConnection('h', binary='mysql') as conn:
                tail_started = []
                with self.assertRaises(connector.PartialReadError) as caught:
                    conn.run('SELECT 1', timeout=5, tail_timeout=('myflames_reading_1', 0.2),
                             on_tail_start=lambda: tail_started.append(self.children[0].poll()))
                self.assertEqual(tail_started, [None], 'capture completion once, before the tail times out')
                self.assertIn('after 0.2s', str(caught.exception), 'the tail does not inherit the data budget')
                self.assertEqual(caught.exception.stdout.replace('\r\n', '\n'), output.replace('\r\n', '\n'))
                self.assertNotIn('name\tvalue', str(caught.exception), 'partial server data is not an error message')
                self.assertEqual(len(self.children), 1)
                self.assertIsNotNone(self.children[0].poll())

    def test_marker_in_data_or_without_newline_cannot_extend_the_read_budget(self):
        for output in ('name\tmyflames_reading_1\n', 'myflames_reading_1',
                       'name\tmyflames_reading_1\\nspoof\n'):
            script = 'import os\nos.write(1, %r)\nr,w=os.pipe()\nos.read(r,1)' % output.encode()
            with self.subTest(output=output), self.child(script), MySQLConnection('h', binary='mysql') as conn:
                on_tail_start = mock.Mock()
                with self.assertRaises(connector.PartialReadError) as caught:
                    conn.run('SELECT 1', timeout=0.2, tail_timeout=('myflames_reading_1', 5),
                             on_tail_start=on_tail_start)
                on_tail_start.assert_not_called()
                self.assertIn('after 0.2s', str(caught.exception), 'only a complete marker row changes the deadline')
                self.assertIsNotNone(self.children[0].poll())

    def test_identity_sql_error_retains_stdout_only_for_opted_in_read_batches(self):
        output = 'myflames_reading_0\nname\tvalue\nmyflames_reading_1\n'
        script = 'import sys\nsys.stdout.write(%r)\nsys.stderr.write("ERROR 1227: denied")\nsys.exit(1)' % output
        for options in ({}, {'tail_timeout': ('myflames_reading_1', 3)}):
            with self.subTest(options=options), self.child(script), MySQLConnection('h', binary='mysql') as conn:
                with self.assertRaises(ConnectorError) as caught:
                    conn.run('SELECT 1', **options)
                self.assertEqual(isinstance(caught.exception, connector.PartialReadError), bool(options))
                if options:
                    self.assertEqual(caught.exception.stdout, output)
                self.assertIsNotNone(self.children[0].poll())


class TestClientText(unittest.TestCase):
    """Windows clients write CRLF line ends; other systems keep data as is."""

    def test_windows_line_ends_become_lf(self):
        from myflames import connector
        with mock.patch.object(connector.os, "name", "nt"):
            self.assertEqual(connector._client_text(b"1\r\n2\r\n"), "1\n2\n")

    def test_other_systems_keep_crlf_from_data(self):
        from myflames import connector
        with mock.patch.object(connector.os, "name", "posix"):
            self.assertEqual(connector._client_text(b"a\r\nb\n"), "a\r\nb\n")
        self.assertEqual(connector._client_text(None), "")


if __name__ == "__main__":
    unittest.main()
