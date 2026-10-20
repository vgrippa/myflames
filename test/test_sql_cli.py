"""CLI contract tests using the editor's real subprocess engine and fake client."""
import ast
import contextlib
import csv
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

from myflames import cli, editor_session as editor, sql_cli
from myflames.workspace_store import WorkspaceStore


class Terminal(io.StringIO):
    def isatty(self):
        return True


@unittest.skipIf(os.name == 'nt', 'the editor fake client is a POSIX script')
class SQLTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        bindir = self.root / 'bin'
        bindir.mkdir()
        self.state = self.root / 'state'
        self.state.mkdir()
        fake = Path(__file__).with_name('_fake_mysql_client.py').read_text(encoding='utf-8')
        # The editor refuses a session whose server identity it cannot read.
        fake = fake.replace('"connection_id": str(self.conn_id),',
                            '"connection_id": str(self.conn_id), "hostname": "fake-db", "port": "3306", "server_id": "1",')
        script = bindir / 'mysql'
        script.write_text('#!%s\n%s' % (sys.executable, fake), encoding='utf-8')
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        patcher = mock.patch.dict(os.environ, {
            'HOME': self.temp.name, 'USERPROFILE': self.temp.name,
            'MYFLAMES_SECRET_BACKEND': 'vault', 'FAKE_ENGINE': 'mysql',
            'FAKE_STATE': str(self.state), 'PATH': str(bindir) + os.pathsep + os.environ.get('PATH', '')})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ('MYFLAMES_VAULT_PASSPHRASE', 'FAKE_DENY', 'FAKE_PS', 'FAKE_COMMANDS'):
            os.environ.pop(name, None)
        # The engine deliberately scrubs arbitrary environment variables. Inject
        # fake-client controls at the process boundary, as its own tests do;
        # never add these controls to the production environment allowlist.
        popen = editor.subprocess.Popen

        def spawn(*args, **kwargs):
            env = dict(kwargs.get('env') or os.environ)
            env.update({k: v for k, v in os.environ.items() if k.startswith('FAKE_')})
            return popen(*args, **dict(kwargs, env=env))

        process_patch = mock.patch.object(editor.subprocess, 'Popen', side_effect=spawn)
        process_patch.start()
        self.addCleanup(process_patch.stop)
        editor._FEATURES.clear()
        self.store = WorkspaceStore()

    def invoke(self, args, stdin=None, dispatch=False):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch('sys.stdin', stdin if stdin is not None else io.StringIO()), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                if dispatch:
                    with mock.patch.object(sys, 'argv', ['myflames', 'sql'] + args):
                        cli.main()
                else:
                    sql_cli.cmd_sql(args)
            except SystemExit as exc:
                code = exc.code
            else:
                code = 0
        # Every invocation must reap its client; no process is left for later tests.
        self.assertFalse([p for p in editor._REGISTRY_PROCS if p.poll() is None])
        self.assertTrue(all(r['status'] != 'pending' and r['finished_at']
                            for r in self.store.audit_records()))
        return code, out.getvalue(), err.getvalue()

    def profile(self, **settings):
        self.store.save('profile', dict(name='test', host='127.0.0.1', port=3306, user='app', **settings))

    def log(self):
        return '\n'.join(p.read_text() for p in self.state.glob('session-*.log'))

    def test_yes_records_only_real_outcome_on_both_profile_types(self):
        for ctype in ('development', 'production'):
            with self.subTest(type=ctype):
                for profile in self.store.list('profile'):
                    self.store.delete('profile', profile['id'])
                self.profile(type=ctype)
                before = {r['id'] for r in self.store.audit_records()}
                code, out, err = self.invoke(['--profile', 'test', '-e', 'CREATE TABLE t (id int)', '--yes'])
                self.assertEqual(code, 0, err)
                records = [r for r in self.store.audit_records() if r['id'] not in before]
                self.assertEqual([r['status'] for r in records], ['ok'])

    def test_development_multistatement_confirmation_grammar(self):
        self.profile(type='development')
        code, out, err = self.invoke(['--profile', 'test', '-e',
                                     'START TRANSACTION; UPDATE t SET v = 1; COMMIT', '--yes'])
        self.assertNotIn('statements is one', err)
        self.assertIn('these 3 statements include one', err)

    def test_review_output_and_insert_table(self):
        target = self.root / "result.sql"
        code, out, err = self.invoke(["-e", "SELECT 7 AS n", "--format", "insert",
                                      "--insert-table", "a`b", "-o", str(target), "-q"])
        self.assertEqual(code, 0, err)
        self.assertEqual((out, err), ("", ""))
        self.assertIn("INSERT INTO `a``b`", target.read_text())
        code, out, err = self.invoke(["-e", "SELECT 7 AS n", "--json", "--output", str(target)])
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "")
        self.assertEqual(json.loads(target.read_text())["status"], "complete")
        before = target.read_bytes()
        code, out, err = self.invoke(["-e", "DROP DATABASE d", "--yes", "-o", str(target)])
        self.assertEqual(code, 2)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(out, "")

    def test_review_drop_database_typed_cli_guard(self):
        for ctype in ("production", "development"):
            for profile in self.store.list("profile"):
                self.store.delete("profile", profile["id"])
            self.profile(type=ctype)
            for args in ([], ["--confirm-target", "camel"], ["--confirm-target", "Camel "]):
                before = self.log()
                code, out, err = self.invoke(["--profile", "test", "-e", "DROP SCHEMA `Camel`", "--yes"] + args)
                self.assertEqual(code, 2, err)
                self.assertEqual(out, "")
                self.assertEqual(self.log(), before)
                self.assertEqual(self.store.audit_records()[0]["status"], "refused")
                self.assertIn(self.store.audit_records()[0]["outcome"], ("no_target", "target_mismatch"))
            code, out, err = self.invoke(["--profile", "test", "-e", "DROP SCHEMA `Camel`", "--yes",
                                          "--confirm-target", "Camel"])
            self.assertNotEqual(code, 2, err)
        code, out, err = self.invoke(["-e", "DROP DATABASE Case"], Terminal("Case\n"))
        self.assertNotEqual(code, 2, err)

    def test_review_alter_preflight_cli_uses_reviewed_guard(self):
        sql = "ALTER TABLE test.tt ADD COLUMN c INT"
        review = {"schema": "test", "table": "tt", "approx_bytes": 1024,
                  "warnings": ["Lock snapshot is advisory"], "suggestion": None}
        with mock.patch.object(editor, "_alter_review", return_value=review):
            code, out, err = self.invoke(["-e", sql, "--yes"])
        self.assertEqual(code, 0, err)
        self.assertIn("ALTER preflight", err)
        self.assertIn("1024", err)
        log = self.log()
        self.assertLess(log.index(editor.ALTER_LOCK_WAIT), log.index(sql))

    def test_review_insert_table_rejects_invalid_input_before_execution(self):
        for name in ("", "x" * 257):
            code, out, err = self.invoke(["-e", "INSERT INTO t VALUES (1)", "--yes",
                                          "--format", "insert", "--insert-table", name])
            self.assertEqual(code, 2)
            self.assertEqual(out, "")
            self.assertNotIn("INSERT INTO t", self.log())

    def test_review_multiple_ddl_refused(self):
        code, out, err = self.invoke(["-e", "DROP TABLE a; DROP TABLE b", "--yes"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("one at a time", err)
        self.assertNotIn("DROP TABLE", self.log())

    def test_json_alias_and_dispatch(self):
        code, out, err = self.invoke(['-e', 'SELECT 7 AS n', '--json'], dispatch=True)
        body = json.loads(out)
        self.assertEqual(code, 0, err)
        self.assertEqual(body['schema_version'], 'sql-1.0')
        self.assertEqual(body['results'][0]['rows'], [['7']])
        self.assertEqual(body['status'], 'complete')
        self.assertEqual(err, '')
        self.assertEqual(self.store.audit_records(), [])

    def test_formats_and_null(self):
        for fmt in ('table', 'csv', 'tsv', 'markdown', 'json'):
            with self.subTest(format=fmt):
                code, out, err = self.invoke(['-e', 'SELECT special', '--format', fmt])
                self.assertEqual(code, 0, err)
                if fmt == 'json':
                    row = json.loads(out)['results'][0]['rows'][0]
                    self.assertEqual(row[:5], [None, '', 'NULL', '0x00FF41', '18446744073709551615'])
                elif fmt in ('csv', 'tsv'):
                    delim = ',' if fmt == 'csv' else '\t'
                    self.assertIn('\r\n' + delim + '""' + delim + 'NULL' + delim, out)
                    parsed = list(csv.reader(io.StringIO(out), delimiter=delim))
                    self.assertEqual(parsed[1][:5], ['', '', 'NULL', '0x00FF41', '18446744073709551615'])
                    self.assertEqual(parsed[1][-2:], ['1', '2'])
                    self.assertEqual(parsed[0][-2:], ['dup', 'dup'])
                elif fmt == 'table':
                    self.assertIn('NULL  ""  "NULL"', out)
                    self.assertIn('"line1\\nline2"', out)
                else:
                    self.assertIn('*NULL*', out)
                    self.assertIn('line1<br>line2', out)

    def test_session_variables_and_transaction_rollback(self):
        sql = ("SET @x='42'; SELECT @x; CREATE TEMPORARY TABLE t (id int, v text); "
               "INSERT INTO t VALUES (1,'before'); START TRANSACTION; "
               "UPDATE t SET v = 'after' WHERE id = 1; ROLLBACK; SELECT * FROM t")
        code, out, err = self.invoke(['-e', sql, '--yes', '--json', '-q'])
        self.assertEqual(code, 0, err)
        results = json.loads(out)['results']
        self.assertEqual([r['rows'] for r in results if r['type'] == 'rows'], [[['42']], [['1', 'before']]])
        self.assertEqual(len(list(self.state.glob('session-*.log'))), 1)
        records = self.store.audit_records()
        self.assertEqual([r['status'] for r in records], ['ok'])
        self.assertEqual(records[0]['surface'], 'cli')
        self.assertEqual(records[0]['operation'], 'sql')
        self.assertEqual(records[0]['status'], 'ok')
        self.assertEqual(json.loads(out)['audit_id'], records[0]['id'])
        self.assertEqual(records[0]['verified'], ['server_identity'])
        self.assertEqual(records[0]['target']['hostname'], 'fake-db')
        # The engine audits the full reviewed script, including its reads.
        self.assertEqual(records[0]['statements'], [u['send'] for u in editor._plan_run({'sql': sql}, None)['units']])

    def test_insert_format_uses_editor_literals_for_typed_results(self):
        result = {'type': 'rows', 'statement': 0,
                  'columns': [{'name': 'blob', 'binary': True}, {'name': 'text'},
                              {'name': 'n', 'type': 'BIGINT'}, {'name': 'nil'}],
                  'rows': [['0x00FF41', "it's", '18446744073709551615', None],
                           ['0x', 'line1\nline2', '0', '']]}
        body = sql_cli._envelope('complete', {'results': [result]})
        with mock.patch.object(sql_cli, '_run', return_value=(0, body, None)):
            code, out, err = self.invoke(['-e', 'SELECT special', '--format', 'insert', '-q'])
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(out, editor.export_result('sql', result, table='result')['content'])
        self.assertIn("(X'00FF41', 'it''s', 18446744073709551615, NULL)", out)
        self.assertIn("(X'', _utf8mb4 X'6C696E65310A6C696E6532', 0, '')", out)

    def test_insert_format_multiple_results_and_empty_result(self):
        code, out, err = self.invoke(['-e', 'SELECT 1; SELECT 2', '--format', 'insert', '-q'])
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(out, "INSERT INTO `result` (`1`) VALUES\n  ('1');\n"
                             "INSERT INTO `result` (`2`) VALUES\n  ('2');\n")
        empty = {'type': 'rows', 'statement': 0, 'columns': [], 'rows': []}
        body = sql_cli._envelope('complete', {'results': [empty]})
        with mock.patch.object(sql_cli, '_run', return_value=(0, body, None)):
            code, out, err = self.invoke(['-e', 'SELECT 1 WHERE FALSE', '--format', 'insert'])
        self.assertEqual((code, out, err), (0, '', ''))

    def test_insert_format_json_override_and_hex_guess(self):
        code, out, err = self.invoke(['-e', 'SELECT special', '--format', 'insert', '--json'])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['schema_version'], 'sql-1.0')
        code, out, err = self.invoke(['-e', 'SELECT special', '--format', 'insert'])
        self.assertEqual(code, 0, err)
        self.assertIn("'0x00FF41'", out, 'untyped hex-looking text must stay text, as in the UI')

    def test_multiple_results_from_one_statement(self):
        code, out, err = self.invoke(['-e', 'CALL two()', '--yes', '--json', '-q'])
        self.assertEqual(code, 0, err)
        self.assertEqual([r['rows'] for r in json.loads(out)['results'] if r['type'] == 'rows'], [[['1']], [['2']]])

    def test_stdin_and_file(self):
        code, out, err = self.invoke(['--json'], io.StringIO('SELECT 9'))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['results'][0]['rows'], [['9']])
        path = self.root / 'script.sql'
        path.write_text('SELECT 8; SELECT 6', encoding='utf-8')
        code, out, err = self.invoke(['-f', str(path), '--format', 'csv'])
        self.assertEqual((code, out), (0, '8\r\n8\r\n6\r\n6\r\n'), err)

    def test_statement_error_keeps_partial_results_and_positions(self):
        code, out, err = self.invoke(['-e', 'SELECT 1;\nSELEC broken;\nSELECT 3', '--yes', '--json', '-q'])
        body = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(body['results'][0]['rows'], [['1']])
        self.assertEqual(body['error']['code'], 1064)
        self.assertEqual(body['error']['position']['line'], 2)
        self.assertIn('line 2, column 1: ERROR 1064 (42000)', err)
        self.assertNotIn('SELECT 3', self.log())
        self.assertEqual(self.store.audit_records()[0]['status'], 'error')

    def test_read_error_needs_no_confirmation(self):
        code, out, err = self.invoke(['-e', 'SELECT * FROM nope', '--json'])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)['error']['code'], 1146)
        self.assertNotIn('--yes', err)
        self.assertEqual(self.store.audit_records(), [])

    def test_read_only_refuses_before_password_or_connect(self):
        self.profile(read_only=True)
        with mock.patch('myflames.workspace_cli.getpass.getpass', side_effect=AssertionError('password prompt')):
            code, out, err = self.invoke(['--profile', 'test', '-p', '-e', 'DELETE FROM t', '--yes', '--json'])
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertIn('read-only', err)
        self.assertEqual(self.log(), '')
        self.assertEqual(self.store.audit_records()[0]['status'], 'refused')

    def test_production_requires_yes_when_piped(self):
        code, out, err = self.invoke(['-e', 'SET @x=1', '--json'])
        self.assertEqual(code, 2)
        self.assertIn('Writes require --yes', err)
        self.assertEqual(out, '')
        self.assertEqual(self.log(), '')
        self.assertEqual(self.store.audit_records()[0]['status'], 'refused')

    def test_saved_password_resolved_after_policy_and_redacted(self):
        with mock.patch.dict(os.environ, {'MYFLAMES_VAULT_PASSPHRASE': 'test-vault-only'}):
            self.store.save('profile', dict(name='saved', host='127.0.0.1', user='app', read_only=True),
                            password='saved-secret', remember=True)
            with mock.patch.object(WorkspaceStore, 'connection_with_saved_password',
                                   side_effect=AssertionError('secret read before policy')):
                code, out, err = self.invoke(['--profile', 'saved', '-e', 'SET @x=1', '--yes', '--json'])
            self.assertEqual(code, 2, err)
            code, out, err = self.invoke(['--profile', 'saved', '-e', 'SELECT 1', '--json'])
        self.assertEqual(code, 0, err)
        self.assertNotIn('saved-secret', out + err + (self.state / 'argv.log').read_text())

    def test_tty_confirmation_yes_and_default_no(self):
        for answer, expected in (('\n', 2), ('y\n', 0)):
            with self.subTest(answer=answer):
                code, out, err = self.invoke(['-e', 'SET @x=1', '--json'], Terminal(answer))
                self.assertEqual(code, expected, err)
                self.assertIn('Run these statements? [y/N]', err)
        records = self.store.audit_records()
        self.assertEqual(sorted(r['status'] for r in records), ['declined', 'ok'])

    def test_review_interrupted_confirmation_is_declined(self):
        for error in (KeyboardInterrupt(), EOFError()):
            with self.subTest(error=type(error).__name__):
                terminal = Terminal()
                with mock.patch.object(terminal, 'readline', side_effect=error), \
                        mock.patch.object(editor.EditorSession, 'start') as start:
                    code, out, err = self.invoke(['-e', 'SET @x=1', '--json'], terminal)
                self.assertEqual(code, 2, err)
                self.assertEqual(out, '')
                self.assertIn('Run these statements? [y/N]', err)
                self.assertNotIn('Cancelled; check the audit trail', err)
                start.assert_not_called()
                self.assertEqual(self.log(), '')
                record = self.store.audit_records()[0]
                self.assertEqual((record['status'], record['outcome']), ('declined', 'declined'))
                self.assertTrue(record['id'])

    def test_development_write_and_risky_policy(self):
        self.profile(type='development')
        flags = ['--profile', 'test', '-e', 'SET @x=1; SELECT @x', '--json', '-q']
        code, out, err = self.invoke(flags)
        self.assertEqual((code, out), (2, ''), err)
        self.assertIn('--yes', err)
        self.assertEqual(self.log(), '')
        self.assertEqual(self.store.audit_records()[0]['outcome'], 'no_terminal')
        code, out, err = self.invoke(flags + ['--yes'])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['results'][1]['rows'], [['1']])
        code, out, err = self.invoke(['--profile', 'test', '-e', 'DROP TABLE t', '--json'])
        self.assertEqual(code, 2, err)
        self.assertEqual(out, '')

    def test_endpoint_override_restores_production_policy(self):
        self.profile(type='development')
        code, out, err = self.invoke(['--profile', 'test', '-P', '3307', '-e', 'SET @x=1', '--json'])
        self.assertEqual(code, 2, err)
        self.assertIn('Production', err)
        self.assertEqual(self.log(), '')

    def test_read_only_guard_applied_for_reads(self):
        self.profile(read_only=True)
        code, out, err = self.invoke(['--profile', 'test', '-e', 'SELECT 1', '--json'])
        self.assertEqual(code, 0, err)
        self.assertIn('SET SESSION TRANSACTION READ ONLY', self.log())

    def test_safe_updates_remains_enabled_on_production(self):
        sql = "CREATE TEMPORARY TABLE t (id int, v text); INSERT INTO t VALUES (1,'v'); UPDATE t SET v = 'bad'"
        code, out, err = self.invoke(['-e', sql, '--yes', '--json', '-q'])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)['error']['code'], 1175)

    def test_limit_and_timeout(self):
        code, out, err = self.invoke(['-e', 'SELECT n FROM big_table LIMIT 20', '--limit', '2', '--json'])
        body = json.loads(out)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(body['results'][0]['rows']), 2)
        self.assertTrue(body['results'][0]['truncated'])
        self.assertIn('2 rows', err)
        code, out, err = self.invoke(['-e', 'SELECT 1 /*gate*/', '--timeout', '0.1', '--json'])
        self.assertEqual(code, 1, err)
        self.assertEqual(json.loads(out)['status'], 'stopped')
        self.assertTrue((self.state / 'kills.log').exists())

    def test_timeout_unresponsive_statement_ends_session_with_unknown_outcome(self):
        # A successful KILL request does not prove the statement ended. The
        # engine must end an unresponsive session, and the CLI must retain 2.
        with mock.patch.object(editor, 'STOP_GRACE', 0):
            code, out, err = self.invoke(['-e', 'SET @x=1; SELECT 1 /*gate:nokill*/; SELECT 3',
                                          '--yes', '--timeout', '0.2', '--json', '-q'])
        body = json.loads(out)
        self.assertEqual(code, 2, err)
        self.assertEqual(body['status'], 'session_ended')
        self.assertIn('did not end after KILL QUERY', err)
        self.assertNotIn('\nSELECT 3\n', self.log())
        record = self.store.audit_records()[0]
        self.assertEqual((record['status'], record['outcome']), ('unknown', 'session_ended'))
        self.assertEqual(body['audit_id'], record['id'])

    def test_connection_loss_after_completed_write_is_audited(self):
        code, out, err = self.invoke(['-e', 'SET @x=1; SELECT lose_connection(); SELECT 3',
                                      '--yes', '--json', '-q'])
        self.assertEqual(code, 2, err)
        body = json.loads(out)
        self.assertEqual(body['status'], 'session_ended')
        self.assertEqual(body['error']['code'], 2013)
        record = self.store.audit_records()[0]
        # This fake reports an error and the statement's end marker before
        # the status helper discovers the lost connection. No write is still
        # outstanding, so the engine completes this run's audit as an error.
        self.assertEqual((record['status'], record['outcome']), ('error', 'session_ended'))
        self.assertEqual(body['audit_id'], record['id'])

    def test_server_identity_change_refuses_write_before_sending(self):
        row = editor.EditorSession._helper_row

        def changed(session, step):
            value = row(session, step)
            if value and 'hostname' in value and 'myflames_editor' not in value:
                value['hostname'] = 'another-server'
            return value

        with mock.patch.object(editor.EditorSession, '_helper_row', new=changed):
            code, out, err = self.invoke(['-e', 'SET @x=99', '--yes', '--json', '-q'])
        self.assertEqual(code, 2, err)
        self.assertIn('server session changed', err)
        self.assertNotIn('SET @x=99', self.log())
        record = self.store.audit_records()[0]
        self.assertEqual((record['status'], record['outcome']), ('refused', 'identity_mismatch'))

    def test_failed_write_connection_open_completes_audit(self):
        with mock.patch.dict(os.environ, {'FAKE_DENY': '1'}):
            code, out, err = self.invoke(['-e', 'SET @x=99', '--yes', '--json', '-q'])
        self.assertEqual(code, 2, err)
        record = self.store.audit_records()[0]
        self.assertEqual(record['status'], 'error')
        self.assertEqual(out, '')
        self.assertTrue(err)
        self.assertEqual(self.log(), '')

    def test_expired_confirmation_token_needs_fresh_audit_and_token(self):
        take = editor.EditorSessions.take_token
        tokens = []

        def expire_once(registry, token, digest):
            tokens.append(token)
            if len(tokens) == 1:
                # Consume the first token, then model it expiring before use.
                self.assertTrue(take(registry, token, digest))
                return False
            return take(registry, token, digest)

        with mock.patch.object(editor.EditorSessions, 'take_token', new=expire_once):
            code, out, err = self.invoke(['-e', 'SET @x=1', '--yes', '--json', '-q'])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(tokens), 2)
        self.assertNotEqual(tokens[0], tokens[1])
        records = self.store.audit_records()
        self.assertEqual([r['status'] for r in records], ['ok', 'refused'])
        self.assertEqual(records[1]['outcome'], 'needs_confirmation')
        self.assertEqual(json.loads(out)['audit_id'], records[0]['id'])

    def test_bad_input(self):
        for args, stdin in ((['-e', ''], None), (['-e', "SELECT 'unclosed"], None),
                            (['-f', str(self.root / 'missing')], None),
                            (['-e', 'SELECT 1', '-f', 'x'], None),
                            (['--timeout', 'nan'], None), (['--timeout', '-1'], None),
                            (['--limit', '0'], None), (['--limit', '100001'], None),
                            (['--profile', 'missing', '-e', 'SELECT 1'], None),
                            ([], Terminal()), ([], io.StringIO('-- only a comment'))):
            with self.subTest(args=args):
                code, out, err = self.invoke(args, stdin)
                self.assertEqual(code, 2, err)
                self.assertEqual(out, '')
                self.assertTrue(err)
        self.assertEqual(self.log(), '')

    def test_connection_failure_and_loss_are_exit_two(self):
        with mock.patch.dict(os.environ, {'FAKE_DENY': '1'}):
            code, out, err = self.invoke(['-e', 'SELECT 1', '--json', '-psecret'])
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertTrue(err)
        self.assertNotIn('secret', out + err)
        code, out, err = self.invoke(['-e', 'SELECT lose_connection()', '--yes', '--json', '-q'])
        self.assertEqual(code, 2, err)
        self.assertEqual(json.loads(out)['status'], 'session_ended')

    def test_unsent_failures_have_only_stderr_in_every_format(self):
        for flags in (['--json'], ['--format', 'json'], ['--format', 'table']):
            for quiet in ([], ['-q']):
                for sql, controls in (('SELECT 1', {'FAKE_DENY': '1'}),
                                      ("SELECT 'unclosed", {}), ('SET @x=1', {})):
                    with self.subTest(flags=flags, quiet=quiet, sql=sql), \
                            mock.patch.dict(os.environ, controls):
                        code, out, err = self.invoke(['-e', sql] + flags + quiet)
                    self.assertEqual(code, 2, err)
                    self.assertEqual(out, '')
                    self.assertTrue(err)
        self.assertEqual(self.log(), '')

    def test_passwords_redacted_in_results_confirmation_audit_and_argv(self):
        sql = "SET @x='cli-password'; SELECT @x; CREATE USER foo IDENTIFIED BY 'new-secret'"
        code, out, err = self.invoke(['-e', sql, '-pcli-password', '--yes', '--json'])
        self.assertEqual(code, 0, err)
        for secret in ('cli-password', 'new-secret'):
            self.assertNotIn(secret, out + err + json.dumps(self.store.audit_records()))
        self.assertNotIn('cli-password', (self.state / 'argv.log').read_text())

    def test_quiet_preserves_payload_and_errors(self):
        code, out, err = self.invoke(['-e', 'SET @x=1; SELECT @x', '--yes', '--json', '-q'])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)['results'])
        self.assertEqual(err, '')
        code, out, err = self.invoke(['-e', 'SELECT * FROM nope', '--json', '-q'])
        self.assertEqual(code, 1)
        self.assertIn('ERROR 1146', err)

    def test_profile_change_during_password_resolution_refuses(self):
        self.profile(type='development')
        resolve = sql_cli._resolve_connection

        def changed(args):
            full = resolve(args)
            profile = self.store.find_profile('test')
            self.store.save('profile', dict(profile, read_only=True), key=profile['id'])
            return full

        with mock.patch.object(sql_cli, '_resolve_connection', side_effect=changed):
            code, out, err = self.invoke(['--profile', 'test', '-e', 'SET @x=1', '--json', '--yes'])
        self.assertEqual(code, 2, err)
        self.assertEqual(out, '')
        self.assertIn('changed after review', err)
        self.assertEqual(self.log(), '')

    def test_replanned_write_requires_fresh_authorization(self):
        planner = editor._plan_run

        def replan(payload, session):
            # Model a newly known sql_mode producing a different sent script.
            return planner(dict(payload, sql='SET @x=1') if session else payload, session)

        with mock.patch.object(editor, '_plan_run', side_effect=replan):
            code, out, err = self.invoke(['-e', 'SELECT 1', '--json'])
        self.assertEqual(code, 2, err)
        self.assertIn('Writes require --yes', err)
        self.assertNotIn('SET @x=1', self.log())
        self.assertEqual(self.store.audit_records()[0]['status'], 'refused')

    def test_review_replanned_writes_audited_on_exception(self):
        planner = editor._plan_run
        replanned_sql = 'SET @replanned=2'

        def replan(payload, session):
            # Isolate the adapter contract: discovering sql_mode can change
            # both the statement boundaries and their write classification.
            return planner(dict(payload, sql=replanned_sql) if session else payload, session)

        for initial in ('SELECT 1', 'SET @initial=1'):
            for error in (OSError('start failed'), KeyboardInterrupt()):
                with self.subTest(initial=initial, error=type(error).__name__):
                    with mock.patch.object(editor, '_plan_run', side_effect=replan), \
                            mock.patch.object(editor.EditorSession, 'start_run', side_effect=error):
                        result, out, err = self.invoke(['-e', initial, '--yes', '--json', '-q'])
                    self.assertEqual(result, 2, err)
                    self.assertEqual(out, '')
                    self.assertTrue(err)
                    record = self.store.audit_records()[0]
                    self.assertEqual(record['status'], 'error')  # failure before sending
                    self.assertEqual(record['statements'], [replanned_sql])
                    self.assertTrue(record['id'])
                    self.assertNotIn(replanned_sql, self.log())
                    self.assertNotIn('SET @initial', self.log())

    def test_audit_failure_sends_no_user_sql(self):
        with mock.patch('myflames.dbops.AuditTrail.attempt', side_effect=OSError('disk full')):
            code, out, err = self.invoke(['-e', 'SET @x=1', '--yes', '--json', '-q'])
        self.assertEqual(code, 2, err)
        self.assertIn('nothing was sent', err)
        self.assertNotIn('SET @x=1', self.log())

    def test_rollback_on_session_close_is_reported(self):
        code, out, err = self.invoke(['-e', 'START TRANSACTION', '--yes', '--json'])
        self.assertEqual(code, 0, err)
        self.assertIn('rolls back any open transaction', err)

    def test_delimiter_uses_editor_splitting(self):
        code, out, err = self.invoke(['-e', 'DELIMITER $$\nSELECT 1$$\nSELECT 2$$', '--json'])
        self.assertEqual(code, 0, err)
        self.assertEqual([r['rows'] for r in json.loads(out)['results']], [[['1']], [['2']]])

    def test_json_overrides_other_format(self):
        code, out, err = self.invoke(['-e', 'SELECT 1', '--format', 'csv', '--json'])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['results'][0]['rows'], [['1']])

    def test_both_client_protocols(self):
        with mock.patch.dict(os.environ, {'FAKE_ENGINE': 'mariadb'}):
            code, out, err = self.invoke(['-e', 'SELECT 2; SELECT special', '--json'])
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)['results'][1]['rows'][0][:3], [None, '', 'NULL'])
            code, out, err = self.invoke(['-e', 'SELECT * FROM nope', '--json'])
            self.assertEqual(code, 1, err)
            self.assertEqual(json.loads(out)['error']['code'], 1146)

    def test_python_37_syntax(self):
        source = Path(sql_cli.__file__).read_text(encoding='utf-8')
        # feature_version was added in Python 3.8; 3.7 checks its own grammar.
        ast.parse(source, **({'feature_version': 7} if sys.version_info >= (3, 8) else {}))


if __name__ == '__main__':
    unittest.main()
