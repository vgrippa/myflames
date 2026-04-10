"""
Unit tests for :mod:`myflames.connector`.

These tests never touch the network. Subprocess execution is replaced with
a ``FakeRun`` capture that records the argv and returns canned stdout, and
``shutil.which`` is monkeypatched so ``_resolve_binary`` picks a predictable
path. Both paths (no-password, full TLS) are covered.

Integration tests that hit real MySQL / MariaDB containers live in
``test_connector_integration.py`` and are skipped when Docker is unavailable.
"""
import os
import stat
import sys
import subprocess
import unittest
from unittest import mock

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(TEST_DIR))

from myflames.connector import MySQLConnection, ConnectorError


def _fake_completed(stdout=b"", stderr=b"", returncode=0):
    """Build a ``subprocess.CompletedProcess``-like stand-in."""
    cp = subprocess.CompletedProcess(args=[], returncode=returncode)
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


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

    def test_defaults_file_is_mode_0600(self):
        c = MySQLConnection(host="h", user="u", password="secret")
        _, mode, _ = self._inspect_file(c)
        self.assertEqual(mode, 0o600, "defaults file must be mode 0600 (got %o)" % mode)

    def test_defaults_file_has_client_section(self):
        c = MySQLConnection(host="h", user="u", password="p")
        _, _, content = self._inspect_file(c)
        self.assertIn("[client]", content)
        self.assertIn("host=h", content)
        self.assertIn("user=u", content)

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
        self.assertIn("ssl-mode=VERIFY_IDENTITY", content)
        self.assertIn("ssl-ca=/ca.pem", content)
        self.assertIn("ssl-cert=/c.pem", content)
        self.assertIn("ssl-key=/k.pem", content)
        self.assertIn("ssl-cipher=AES256", content)

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

    def test_defaults_extra_file_is_first_arg(self):
        c = self._conn(user="u", password="p")
        try:
            argv = c._build_argv("SELECT 1")
            self.assertEqual(argv[0], "/bin/mysql")
            self.assertTrue(argv[1].startswith("--defaults-extra-file="))
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
            "myflames.connector.subprocess.run",
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
            "myflames.connector.subprocess.run",
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
        with mock.patch("myflames.connector.subprocess.run", side_effect=fake_run):
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
        with mock.patch("myflames.connector.subprocess.run", side_effect=fake_run):
            self.conn.explain_analyze("SELECT 1")
        last_sql = calls[-1][-1]
        self.assertIn("ANALYZE FORMAT=JSON", last_sql)
        self.assertNotIn("EXPLAIN ANALYZE FORMAT=JSON", last_sql)


if __name__ == "__main__":
    unittest.main()
