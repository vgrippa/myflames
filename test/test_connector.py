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
import os
import re
import stat
import sys
import subprocess
import tempfile
import threading
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
        with mock.patch.object(MySQLConnection, "_which", staticmethod(lambda n: None)):
            with self.assertRaises(ConnectorError):
                c._resolve_binary()


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


if __name__ == "__main__":
    unittest.main()
