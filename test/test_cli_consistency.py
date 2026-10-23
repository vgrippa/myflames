"""Regression coverage for the CLI review's flag, stream and exit contracts."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from myflames import cli, workspace_cli
from myflames.teach import cmd_teach

ROOT = Path(__file__).resolve().parent.parent
PLAN = str(ROOT / 'test/fixtures/explain-004-pk-lookup-user.json')


class TestCLIConsistency(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = mock.patch.dict(os.environ, {
            'HOME': self.directory.name, 'USERPROFILE': self.directory.name,
            'MYFLAMES_SECRET_BACKEND': 'vault',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)

    def call(self, argv, function=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with mock.patch.object(cli.sys, 'argv', ['myflames'] + argv):
                try:
                    if function:
                        function(argv)
                    else:
                        cli.main()
                    code = 0
                except SystemExit as exc:
                    code = exc.code
        return code, out.getvalue(), err.getvalue()

    def path(self, name='result.txt'):
        return str(Path(self.directory.name) / name)

    def test_output_modes_are_exclusive_but_cost_json_remains_valid(self):
        for args in (['digest', PLAN, '--json', '--show-prompts'],
                     ['compare', PLAN, PLAN, '--json', '--digest']):
            with self.subTest(args=args):
                code, out, err = self.call(args)
                self.assertEqual(code, 2)
                self.assertEqual(out, '')
                self.assertIn('not allowed', err)
        code, out, err = self.call(['digest', PLAN, '--cost', '--json'])
        self.assertEqual(code, 0, err)
        self.assertIn('tokens_saved', json.loads(out))

    def test_rule_listing_honors_output_and_failures(self):
        output = self.path('rules.json')
        code, out, err = self.call(['advise', '--list-rules', '--json', '-o', output, '-q'])
        self.assertEqual((code, out, err), (0, '', ''))
        self.assertTrue(json.loads(Path(output).read_text(encoding="utf-8")))
        code, out, err = self.call(['advise', '--list-rules', '-o', self.directory.name, '-q'])
        self.assertEqual(code, 2)
        self.assertIn('Cannot write', err)

    def test_import_listing_honors_output(self):
        with mock.patch('myflames.shell_import.is_shell_diagnostics_zip', return_value=True), \
                mock.patch('myflames.shell_import.list_shell_diagnostics_queries',
                           return_value=[{'index': 0, 'prefix': 'query0', 'query': 'SELECT 1'}]):
            code, out, err = self.call(['import-shell', 'bundle.zip', '--list', '-o', self.path(), '-q'])
        self.assertEqual((code, out, err), (0, '', ''))
        self.assertEqual(Path(self.path()).read_text(encoding="utf-8"), '0\tquery0\tSELECT 1\n')

    def test_explicit_query_file_failure_is_two(self):
        invalid = self.path('invalid.sql')
        Path(invalid).write_bytes(b'\xff')
        for query_file in (self.path('missing.sql'), invalid):
            with self.subTest(query_file=query_file):
                code, out, err = self.call([PLAN, '--query-file', query_file, '--no-sidecar', '-q'])
                self.assertEqual(code, 2)
                self.assertEqual(out, '')
                self.assertIn('Cannot read --query-file', err)

    def test_explicit_sidecar_failure_is_two_automatic_is_best_effort(self):
        for extension in ('svg', 'html'):
            for explicit in (True, False):
                with self.subTest(extension=extension, explicit=explicit):
                    args = [PLAN, '-o', self.path('plan.' + extension), '--no-teach-bundle', '-q']
                    if explicit:
                        args += ['--sidecar', self.path('sidecar.json')]
                    with mock.patch('myflames.output_sidecar.write_sidecar', side_effect=PermissionError('denied')):
                        code, _, err = self.call(args)
                    self.assertEqual(code, 2 if explicit else 0)
                    self.assertIn('sidecar emission failed', err)

    def test_execute_activates_live_with_default_host(self):
        conn = mock.MagicMock()
        conn.__enter__.return_value = conn
        conn.is_mariadb.return_value = False
        conn.run_stdin.return_value = Path(PLAN).read_text(encoding="utf-8")
        with mock.patch('myflames.connector.MySQLConnection', return_value=conn) as factory:
            code, _, err = self.call(['-u', 'reader', '-e', 'SELECT 1', self.path('missing.json'),
                                     '--no-collect-schema', '--no-collect-stats', '--no-collect-variables',
                                     '--no-sidecar', '-q'])
        self.assertEqual(code, 0, err)
        self.assertEqual(factory.call_args[1]['host'], '127.0.0.1')
        self.assertEqual(factory.call_args[1]['port'], 3306)
        self.assertIn('EXPLAIN ANALYZE FORMAT=JSON SELECT 1', conn.run_stdin.call_args[0][0])

    def test_connection_flags_without_execute_are_not_ignored(self):
        code, out, err = self.call(['-u', 'reader', PLAN, '--no-sidecar'])
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertIn('needs a query', err)

    def test_profile_resolution_reaches_root_connection(self):
        from myflames.workspace_store import WorkspaceStore
        store = WorkspaceStore()
        profile = store.save('profile', {'name': 'review', 'host': 'profile.example', 'port': 3307,
                                        'user': 'reader', 'database': 'shop', 'read_only': True})
        conn = mock.MagicMock()
        conn.__enter__.return_value = conn
        conn.is_mariadb.return_value = False
        conn.run_stdin.return_value = Path(PLAN).read_text(encoding="utf-8")
        with mock.patch('myflames.connector.MySQLConnection', return_value=conn) as factory:
            code, _, err = self.call(['--profile', profile['id'], '-e', 'SELECT 1',
                                     '--no-collect-schema', '--no-collect-stats', '--no-collect-variables',
                                     '--no-sidecar', '-q'])
        self.assertEqual(code, 0, err)
        self.assertEqual(factory.call_args[1]['host'], 'profile.example')
        self.assertEqual(factory.call_args[1]['port'], 3307)
        self.assertEqual(factory.call_args[1]['database'], 'shop')
        self.assertIn('START TRANSACTION READ ONLY', conn.run_stdin.call_args[0][0])

    def test_live_setup_oserror_is_sanitized_exit_two(self):
        for target in ('myflames.connector.MySQLConnection', 'myflames.connector.tempfile.mkstemp'):
            with self.subTest(target=target), mock.patch(target, side_effect=PermissionError('secret\nfailed')):
                code, out, err = self.call(['-e', 'SELECT 1', '-psecret', '-q'])
            self.assertEqual(code, 2)
            self.assertEqual(out, '')
            self.assertNotIn('secret', err)
            self.assertNotIn('Traceback', err)
            self.assertIn('[redacted]', err)

    def test_artifact_quiet_flags_keep_results_and_suppress_status(self):
        for args, filename in (([PLAN, '--no-teach-bundle'], 'plan.svg'),
                               (['explore', PLAN, '--type', 'treemap'], 'explore.html'),
                               (['teach', 'btree'], 'lesson.html'),
                               (['teach', '--index'], 'index.html'),
                               (['teach', '-q', '--catalog'], 'catalog.html')):
            with self.subTest(args=args):
                path = self.path(filename)
                code, out, err = self.call(args + ['-o', path, '-q'])
                self.assertEqual((code, out, err), (0, '', ''))
                self.assertTrue(Path(path).read_text(encoding="utf-8"))
        job = mock.Mock(state='complete', result={'raw': '{}'})
        with mock.patch.object(workspace_cli, 'LiveJob', return_value=job):
            code, out, err = self.call(['capture', '-e', 'SELECT 1', '-o', self.path(), '-q'])
        self.assertEqual((code, out, err), (0, '', ''))
        self.assertEqual(json.loads(Path(self.path()).read_text(encoding="utf-8"))['schema_version'], 'capture-1.0')

    def test_teach_write_failures_exit_two_including_catalog_alias(self):
        for args in (['btree'], ['--index'], ['--catalog']):
            with self.subTest(args=args):
                code, out, err = self.call(args + ['-o', self.directory.name, '-q'], cmd_teach)
                self.assertEqual(code, 2)
                self.assertEqual(out, '')
                self.assertIn('Cannot write', err)
                self.assertNotIn('Traceback', err)

    def test_guide_parses_help_unknown_flags_and_output(self):
        for flag in ('--json', '--definitely-invalid'):
            code, out, err = self.call(['guide', flag])
            self.assertEqual(code, 2)
            self.assertEqual(out, '')
        code, out, err = self.call(['guide', '--help'])
        self.assertEqual(code, 0)
        self.assertIn('--output', out)
        code, out, err = self.call(['guide', '-o', self.path(), '-q'])
        self.assertEqual((code, out, err), (0, '', ''))
        self.assertEqual(Path(self.path()).read_text(encoding="utf-8"), cli._GUIDE_TEXT)

    def test_deprecated_aliases_warn_unless_quiet(self):
        for alias, args, canonical in (('diff', [PLAN, PLAN, '--json'], 'compare'),
                                       ('tokens', [PLAN, '--json'], 'digest'),
                                       ('findings', [PLAN, '--json'], 'advise')):
            for quiet_flags in ([], ['-q'], ['-qq'], ['--qui'], ['--quie']):
                quiet = bool(quiet_flags)
                with self.subTest(alias=alias, quiet_flags=quiet_flags):
                    code, out, err = self.call([alias] + args + quiet_flags)
                    self.assertEqual(code, 0, err)
                    json.loads(out)
                    self.assertEqual(bool(err), not quiet)
                    if not quiet:
                        self.assertIn(canonical, err)
                        self.assertIn('deprecated', err)
        code, out, err = self.call(['diff', '--help'])
        self.assertEqual(code, 0)
        self.assertIn('deprecated', err)

    def test_alias_inventory_and_readme_connection_reference(self):
        code, out, _ = self.call(['--help'])
        self.assertEqual(code, 0)
        for alias in ('tokens', 'findings', 'diff', 'running'):
            self.assertRegex(out, r'(?m)^  ' + alias + r'\s+Deprecated alias')
        readme = (ROOT / 'README.md').read_text(encoding="utf-8")
        row = next(line for line in readme.splitlines() if line.startswith('| `--profile NAME_OR_ID`'))
        self.assertIn('replication', row)

    def test_repeated_single_schema_is_rejected_before_connecting(self):
        with mock.patch.object(workspace_cli, '_discover_run') as run:
            for command in ('top', 'sessions', 'io'):
                code, out, err = self.call([command, '--schema', 'first', '--schema', 'second'])
                self.assertEqual(code, 2)
                self.assertEqual(out, '')
                self.assertIn('one schema', err)
        run.assert_not_called()

    def test_empty_text_results_still_write_headers(self):
        with mock.patch.object(workspace_cli, '_discover_run', return_value={'statements': [], 'notes': []}):
            for command in ('top', 'running'):
                code, out, err = self.call([command, '-o', self.path(), '-q'])
                self.assertEqual((code, out, err), (0, '', ''))
                self.assertIn('SQL', Path(self.path()).read_text(encoding="utf-8"))
        for command, header in (('profiles', 'NAME'), ('audit', 'WHEN (UTC)')):
            code, out, err = self.call([command, 'list', '-o', self.path(), '-q'])
            self.assertEqual((code, out, err), (0, '', ''))
            self.assertIn(header, Path(self.path()).read_text(encoding="utf-8"))

    def test_capture_digest_alias_warns_and_selector_reaches_lookup(self):
        for flag, quiet in (('--statement-digest', False), ('--digest', False), ('--digest', True)):
            job = mock.Mock(state='complete', result={'raw': '{}'})
            with mock.patch.object(workspace_cli, 'LiveJob', return_value=job), \
                    mock.patch.object(workspace_cli, '_digest_capture',
                                      return_value=('SELECT 1', {'host': 'db'}, None)) as lookup:
                code, out, err = self.call(['capture', flag, '0123456789abcdef'] + (['-q'] if quiet else []))
            self.assertEqual(code, 0, err)
            self.assertEqual(lookup.call_args[0][0].digest, '0123456789abcdef')
            self.assertEqual('deprecated' in err, flag == '--digest' and not quiet)
            json.loads(out)

    def test_cli_client_paths_reach_server_connections_and_capture(self):
        flags = ['--ssl-cert', '/cert.pem', '--ssl-key', '/key.pem', '--mysql-binary', '/client']
        conn = mock.MagicMock()
        conn.__enter__.return_value = conn
        with mock.patch('myflames.connector.MySQLConnection', return_value=conn) as factory, \
                mock.patch('myflames.discover.top_report', return_value={'statements': [], 'notes': []}):
            code, _, err = self.call(['top', '--json'] + flags)
        self.assertEqual(code, 0, err)
        for field, expected in (('ssl_cert', '/cert.pem'), ('ssl_key', '/key.pem'), ('binary', '/client')):
            self.assertEqual(factory.call_args[1][field], expected)
        job = mock.Mock(state='complete', result={'raw': '{}'}, options={})
        with mock.patch.object(workspace_cli, 'LiveJob', return_value=job):
            code, _, err = self.call(['capture', '-e', 'SELECT 1'] + flags)
        self.assertEqual(code, 0, err)
        self.assertEqual(job.options, {'ssl_cert': '/cert.pem', 'ssl_key': '/key.pem', 'binary': '/client'})

    def test_replication_loop_refusal_is_stderr_only_and_audited(self):
        from myflames.workspace_store import WorkspaceStore
        store = WorkspaceStore()
        profile = store.save('profile', {'name': 'readonly', 'host': 'db', 'read_only': True})
        for output in (None, self.path('loop.json')):
            if output:
                Path(output).write_text('previous result')
            args = ['replication', 'skip-loop', '--profile', profile['id'], '--channel', '',
                    '--errors', '1062', '--json', '-q'] + (['-o', output] if output else [])
            with mock.patch('myflames.connector.MySQLConnection') as factory:
                code, out, err = self.call(args)
            self.assertEqual((code, out), (2, ''), err)
            self.assertIn('read-only', err)
            if output:
                self.assertEqual(Path(output).read_text(encoding="utf-8"), 'previous result')
            factory.assert_not_called()
        self.assertEqual(len(store.audit_records()), 2)
        self.assertTrue(all(r['status'] == 'refused' for r in store.audit_records()))

    def test_loop_saves_final_result_and_routes_events_to_stderr(self):
        report = {'available': True, 'replica': True, 'engine': 'mysql', 'server_version': '8.4.11',
                  'channels': [{'name': '', 'kind': 'replication', 'skip_plan': None}]}
        status = {'schema_version': 'replication-skip-loop-1.0', 'stop_reason': 'max_skips',
                  'needs_attention': False, 'message': 'Reached cap.', 'skips': []}
        for as_json in (False, True):
            for quiet in (False, True):
                contents = []
                for output in (None, self.path()):
                    def connect(args, state, work):
                        return work(mock.Mock())

                    def make_loop(*args, **kwargs):
                        def run():
                            kwargs['on_event']({'event': 'stopped', 'message': 'live event'})
                            return status
                        return mock.Mock(run=run)

                    with self.subTest(as_json=as_json, quiet=quiet, output=output), \
                            mock.patch.object(workspace_cli, '_replication_connect', side_effect=connect), \
                            mock.patch('myflames.replication.read_status', return_value=report), \
                            mock.patch('myflames.replication.read_receiver_identity',
                                       return_value=((1, 'version-check', 1),)), \
                            mock.patch('myflames.replication.SkipLoop', side_effect=make_loop):
                        args = ['replication', 'skip-loop', '--channel', '', '--errors', '1062', '--yes']
                        args += (['-o', output] if output else [])
                        code, out, err = self.call(args + (['--json'] if as_json else []) + (['-q'] if quiet else []))
                    self.assertEqual(code, 0, err)
                    content = Path(output).read_bytes() if output else out.encode('utf-8')
                    if output:
                        self.assertEqual(out, '')
                    self.assertEqual(json.loads(content) if as_json else content,
                                     status if as_json else b'Reached cap.\n')
                    self.assertEqual('live event' in err, not quiet)
                    self.assertNotIn('live event', content.decode('utf-8'))
                    contents.append(content)
                self.assertEqual(contents[0], contents[1])

    def test_json_input_errors_use_stderr_and_preserve_output(self):
        bad = self.path('bad.json')
        wrong = self.path('wrong.json')
        Path(bad).write_text('{broken')
        Path(wrong).write_text('{}')
        for source in (self.path('missing.json'), bad, wrong):
            for command in (['compare', PLAN, source], ['digest', source], ['advise', source]):
                for output in (None, self.path('error.json')):
                    if output:
                        Path(output).write_text('existing result')
                    args = command + ['--json', '-qq'] + (['-o', output] if output else [])
                    with self.subTest(args=args):
                        code, out, err = self.call(args)
                        self.assertEqual((code, out), (2, ''))
                        self.assertTrue(err)
                        self.assertNotIn('Traceback', err)
                        if output:
                            self.assertEqual(Path(output).read_text(encoding="utf-8"), 'existing result')
        code, out, err = self.call(['advise', PLAN, '--json', '--ignore', 'bad-category', '-q'])
        self.assertEqual((code, out), (2, ''))
        self.assertIn('unknown --ignore', err)

    def test_loop_text_refusal_preserves_output_file(self):
        from myflames.workspace_store import WorkspaceStore
        profile = WorkspaceStore().save('profile', {'name': 'readonly-text', 'host': 'db', 'read_only': True})
        args = ['replication', 'skip-loop', '--profile', profile['id'], '--channel', '',
                '--errors', '1062', '-q']
        Path(self.path()).write_text('previous result')
        with mock.patch('myflames.connector.MySQLConnection') as connect:
            code, out, err = self.call(args)
            file_code, file_out, file_err = self.call(args + ['-o', self.path()])
        self.assertEqual((code, out, file_code, file_out), (2, '', 2, ''))
        self.assertIn('read-only', err)
        self.assertEqual(Path(self.path()).read_text(encoding="utf-8"), 'previous result')
        self.assertEqual(err, file_err)
        connect.assert_not_called()

    def test_loop_decline_is_stderr_only_without_starting_or_writing(self):
        report = {'available': True, 'replica': True, 'engine': 'mysql', 'server_version': '8.4.11',
                  'channels': [{'name': '', 'kind': 'replication', 'skip_plan': None}]}
        with mock.patch.object(workspace_cli, '_replication_connect',
                               side_effect=lambda args, state, work: work(mock.Mock())), \
                mock.patch('myflames.replication.read_status', return_value=report), \
                mock.patch('myflames.replication.read_receiver_identity',
                           return_value=((1, 'version-check', 1),)), \
                mock.patch.object(workspace_cli, '_replication_confirm',
                                  return_value=('declined', 'declined', 'Nothing was sent.')), \
                mock.patch('myflames.replication.skip_one') as skip:
            code, out, err = self.call(['replication', 'skip-loop', '--channel', '',
                                        '--errors', '1062', '--json', '-q'])
        self.assertEqual(code, 2, err)
        self.assertEqual(out, '')
        self.assertIn('Nothing was sent.', err)
        from myflames.workspace_store import WorkspaceStore
        self.assertEqual(WorkspaceStore().audit_records()[0]['status'], 'declined')
        skip.assert_not_called()


    def test_every_write_refuses_readonly_before_password_with_empty_stdout(self):
        from myflames.workspace_store import WorkspaceStore
        store = WorkspaceStore()
        profile = store.save('profile', {'name': 'readonly', 'host': 'db', 'read_only': True})
        commands = [
            ['locks', '--kill', '41'], ['sessions', '--kill', '41'],
            ['config', 'set', 'max_connections', '300'],
            ['accounts', 'lock', 'app@%'], ['dump', self.path('dump')],
            ['restore', self.path('dump')], ['clone', '--donor-host', 'donor'],
            ['replication', 'skip', '--channel', ''],
            ['replication', 'skip-loop', '--channel', '', '--errors', '1062'],
            ['sql', '-e', 'DELETE FROM t'],
        ]
        for command in commands:
            for fmt in ([], ['--json']):
                with self.subTest(command=command, fmt=fmt):
                    output = self.path('preserved.txt')
                    Path(output).write_text('previous result')
                    flags = ['--profile', profile['id'], '-p', '--yes', '-q'] + fmt
                    if command[0] != 'sql':
                        flags += ['-o', output]
                    before = len(store.audit_records())
                    with mock.patch('getpass.getpass') as prompt, \
                            mock.patch('myflames.connector.MySQLConnection') as connect:
                        code, out, err = self.call(command + flags)
                    self.assertEqual((code, out), (2, ''), err)
                    self.assertIn('read-only', err)
                    self.assertEqual(Path(output).read_text(encoding="utf-8"), 'previous result')
                    prompt.assert_not_called()
                    connect.assert_not_called()
                    records = store.audit_records()
                    self.assertEqual(len(records), before + 1)
                    self.assertEqual(records[0]['status'], 'refused')

    def test_credential_prompt_interruptions_are_declined_once(self):
        from myflames.workspace_store import WorkspaceStore
        store = WorkspaceStore()
        commands = [
            ['locks', '--kill', '41'], ['sessions', '--kill', '41'],
            ['config', 'set', 'max_connections', '300'], ['accounts', 'lock', 'app@%'],
            ['dump', self.path('dump')], ['restore', self.path('dump')],
            ['clone', '--donor-host', 'donor'], ['replication', 'skip', '--channel', ''],
            ['replication', 'skip-loop', '--channel', '', '--errors', '1062'],
            ['sql', '-e', 'DELETE FROM t'],
        ]
        for command in commands:
            for exception in (EOFError, KeyboardInterrupt):
                with self.subTest(command=command, exception=exception):
                    before = len(store.audit_records())
                    with mock.patch('getpass.getpass', side_effect=exception), \
                            mock.patch('myflames.connector.MySQLConnection') as connect:
                        code, out, err = self.call(command + ['-p', '--yes', '--json', '-q'])
                    self.assertEqual((code, out), (2, ''), err)
                    self.assertTrue(err)
                    connect.assert_not_called()
                    records = store.audit_records()
                    # SQL's token review is separately recorded before credentials.
                    fresh = records[:len(records) - before]
                    self.assertEqual(sum(r['status'] == 'declined' for r in fresh), 1)
                    self.assertEqual(fresh[0]['status'], 'declined')


if __name__ == '__main__':
    unittest.main()
