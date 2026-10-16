"""The classic -e renderer must use the capture guard and one guarded session."""
import argparse
import contextlib
import io
import subprocess
import unittest
from unittest import mock

from myflames import cli
from myflames.connector import ConnectorError, MySQLConnection


class TestClassicLiveSafety(unittest.TestCase):
    def args(self, query='SELECT 1'):
        return argparse.Namespace(execute=query, host='localhost', port=3306, user='app', password='login-secret',
                                  database='db', no_collect_schema=True, no_collect_stats=True,
                                  no_collect_variables=True)

    def invoke(self, args, conn):
        err = io.StringIO()
        with mock.patch('myflames.connector.MySQLConnection', return_value=conn) as factory, \
                contextlib.redirect_stderr(err):
            try:
                result = cli._run_live_explain(args)
            except SystemExit as exc:
                return exc.code, None, err.getvalue(), factory
        return 0, result, err.getvalue(), factory

    def connection(self, maria=False):
        conn = mock.MagicMock()
        conn.__enter__.return_value = conn
        conn.is_mariadb.return_value = maria
        conn.run_stdin.return_value = '{"query_plan": {}}\n'
        return conn

    def test_live_rejects_writes_multiple_statements_and_ambiguous_quotes_before_connecting(self):
        for query in ('DELETE FROM t', 'UPDATE t SET n=1', 'SELECT 1; DROP TABLE t',
                      "SELECT '\\'; DROP TABLE t; -- '", "SELECT 1 /*! INTO OUTFILE '/tmp/x' */",
                      'WITH c AS (SELECT 1) DELETE FROM t', 'SELECT 1 INTO OUTFILE "/tmp/x"',
                      "SELECT 'unclosed", 'SELECT 1\\g'):
            with self.subTest(query=query):
                conn = self.connection()
                args = self.args(query)
                args.password = '__PROMPT__'
                with mock.patch('getpass.getpass', side_effect=AssertionError('must validate before prompting')):
                    code, _, err, factory = self.invoke(args, conn)
                self.assertEqual(code, 2)
                self.assertIn('refused', err)
                factory.assert_not_called()

    def test_live_select_uses_one_read_only_script_on_both_engines(self):
        for maria in (False, True):
            with self.subTest(maria=maria):
                conn = self.connection(maria)
                query = "WITH c AS (SELECT 'it''s; safe' AS x) SELECT x FROM c -- tail"
                code, result, err, _ = self.invoke(self.args(query), conn)
                self.assertEqual(code, 0, err)
                self.assertEqual(result[0], conn.run_stdin.return_value)
                script = conn.run_stdin.call_args[0][0]
                analyze = 'ANALYZE FORMAT=JSON ' if maria else 'EXPLAIN ANALYZE FORMAT=JSON '
                self.assertIn('SET SESSION TRANSACTION READ ONLY;\nSTART TRANSACTION READ ONLY;\n' + analyze,
                              script)
                self.assertTrue(script.endswith('\n;\nROLLBACK;\n'))
                self.assertEqual('explain_json_format_version=2' in script, not maria)
                self.assertEqual(conn.run_stdin.call_args[1]['extra_flags'],
                                 ['--raw', '--comments', '--binary-mode'])
                conn.explain_analyze.assert_not_called()

    def test_live_guard_failure_stops_without_fallback_and_redacts_errors(self):
        conn = self.connection()
        conn.run_stdin.side_effect = ConnectorError("guard failed: login-secret IDENTIFIED BY 'other-secret'")
        code, _, err, _ = self.invoke(self.args(), conn)
        self.assertEqual(code, 2)
        self.assertNotIn('login-secret', err)
        self.assertNotIn('other-secret', err)
        self.assertIn('[redacted]', err)
        conn.explain_analyze.assert_not_called()
        conn.run.assert_not_called()

    def test_live_connection_and_collection_errors_redact_secrets(self):
        for phase in ('enter', 'schema', 'stats', 'freshness', 'variables'):
            with self.subTest(phase=phase):
                conn = self.connection()
                args = self.args('SELECT * FROM t')
                args.no_collect_schema = args.no_collect_stats = args.no_collect_variables = False
                error = ConnectorError("login-secret SOURCE_PASSWORD='other-secret'")
                names = {'schema': 'collect_schema', 'stats': 'collect_stats',
                         'freshness': 'collect_stats_freshness', 'variables': 'collect_session_variables'}
                with contextlib.ExitStack() as stack:
                    for label, name in names.items():
                        stack.enter_context(mock.patch('myflames.collectors.' + name,
                                                       side_effect=error if phase == label else None,
                                                       return_value={}))
                    if phase == 'enter':
                        conn.__enter__.side_effect = error
                    code, _, err, _ = self.invoke(args, conn)
                self.assertEqual(code, 2 if phase == 'enter' else 0)
                self.assertNotIn('login-secret', err)
                self.assertNotIn('other-secret', err)
                self.assertIn('[redacted]', err)

    def test_live_script_client_stops_on_error_and_disables_reconnect(self):
        # Exercise the real connector's subprocess boundary: SQL stays on stdin,
        # no --force, no reconnect and no fallback after a failed READ ONLY.
        conn = MySQLConnection(host='localhost', binary='mysql', password='login-secret')
        proc = subprocess.CompletedProcess([], 1, b'', b'ERROR 1792: read-only guard failed')
        with mock.patch.object(conn, 'is_mariadb', return_value=False), \
                mock.patch('myflames.connector._run_bounded', return_value=proc) as run:
            code, _, err, _ = self.invoke(self.args(), conn)
        self.assertEqual(code, 2, err)
        self.assertEqual(run.call_count, 1)
        argv = run.call_args[0][0]
        for flag in ('--batch', '--raw', '--silent', '--skip-column-names', '--skip-reconnect',
                     '--default-character-set=utf8mb4'):
            self.assertIn(flag, argv)
        self.assertNotIn('--force', argv)
        self.assertNotIn('-e', argv)
        self.assertIn(b'START TRANSACTION READ ONLY;', run.call_args[1]['input'])
        self.assertFalse(any('login-secret' in part for part in argv))


if __name__ == '__main__':
    unittest.main()
