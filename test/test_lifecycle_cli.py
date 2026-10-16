"""CLI (lifecycle_cli.py) and workspace endpoints (ui._lifecycle_*) for upgrade-check, dump, restore, clone.

Replays only: the server answers come from test/fixtures/lifecycle (recorded
live) or ReplayConnection; tool runs are replaced by in-process jobs. HOME is
a temporary directory and the secret backend is the vault, so no test touches
~/.myflames or the keychain.
"""
import io
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

from myflames import dbops, cli, lifecycle as lc, lifecycle_cli as lcli, ui
from myflames.connector import ConnectorError
from myflames.workspace_store import WorkspaceStore

from test_lifecycle import FIXTURES, ReplayConnection, StrictReplay, server_variables

PASSWORD = 'pw with spaces'


def fake_plan(operation='restore', target='127.0.0.1:3306', ready=True):
    server = {'engine': 'mysql', 'version': '8.4.11', 'variables': {},
              'identity': {'hostname': 'db1', 'server_uuid': 'u1'}}
    return {'schema_version': 'lifecycle-plan-1.0', 'operation': operation, 'request': {}, 'server': server,
            'connection_key': '[]', 'tool': {'name': 'mysql', 'version': 'mysql Ver 8.4.8'}, 'target': target,
            'commands': ['mysql --defaults-extra-file=<0600 file> < /backups/dump.sql'],
            'checks': [{'check': 'privileges', 'status': 'passed', 'reason': 'hint'}],
            'warnings': ['Restore cannot be undone.'], 'ready': ready, 'binding': 'b'}


def finished_job(operation, status='completed'):
    return lc.LifecycleJob(operation, lambda job: {'status': status, 'verification': [
        {'check': 'row_count', 'status': 'passed' if status == 'completed' else 'failed', 'reason': 'exact',
         'schema': 'app', 'table': 't', 'expected': 2, 'actual': 2 if status == 'completed' else 3}]})


class Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {'HOME': self.tmp.name, 'USERPROFILE': self.tmp.name,
                                           'MYFLAMES_SECRET_BACKEND': 'vault'})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        detect = mock.patch.object(lc, 'detect_tools', return_value={'tools': {}})
        detect.start()
        self.addCleanup(detect.stop)

    def store(self):
        return WorkspaceStore()

    def run_cli(self, argv, conn=None, tty=False, typed=''):
        """cli.main() with lifecycle connections answered by *conn* (None: connecting fails the test)."""
        out, err = io.StringIO(), io.StringIO()
        stdin = mock.Mock() if isinstance(typed, BaseException) else io.StringIO(typed)
        if isinstance(typed, BaseException):
            stdin.readline.side_effect = typed
        stdin.isatty = lambda: tty
        def connect(factory, options):
            if conn is None:
                raise AssertionError('must not connect')
            return conn
        with mock.patch.object(lc, '_connect', side_effect=connect) as connected, \
                mock.patch.object(sys, 'argv', ['myflames'] + argv), mock.patch.object(sys, 'stdout', out), \
                mock.patch.object(sys, 'stderr', err), mock.patch.object(sys, 'stdin', stdin):
            try:
                cli.main()
                code = 0
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 2)
        return code, out.getvalue(), err.getvalue(), connected


class UpgradeCheckCli(Isolated):
    def replay(self):
        data = json.loads((FIXTURES / 'native-8.0.46-to-8.4.0.json').read_text())
        return StrictReplay(data['calls'] + data.get('synthetic_calls', []))

    def test_native_report_text_json_and_fail_on(self):
        argv = ['upgrade-check', '-u', 'root', '--target-version', '8.4.0', '--no-shell']
        code, out, err, unused = self.run_cli(argv, self.replay())
        self.assertEqual(code, 0, err)
        self.assertIn('MySQL 8.0.46 -> 8.4.0 (native subset, partial)', out)
        self.assertIn('ERROR    removed_variables', out)
        self.assertIn('1 error, 4 warnings, 2 notices', out)
        code, out, err, unused = self.run_cli(argv + ['--json'], self.replay())
        report = json.loads(out)
        self.assertEqual(report['schema_version'], 'upgrade-check-1.0')
        self.assertNotIn('variables', report['server'])
        self.assertEqual(self.run_cli(argv + ['--fail-on', 'error', '-q'], self.replay())[0], 1)
        self.assertEqual(self.run_cli(argv + ['--fail-on', 'warning', '-q'], self.replay())[0], 1)

    def test_shell_cannot_check_this_pair_exits_2_with_hint(self):
        tools = {'tools': {'mysqlsh': {'available': True, 'version': 'mysqlsh Ver 9.6.0', 'engine': 'mysql',
                                       'path': '/usr/bin/mysqlsh'}}}
        with mock.patch.object(lc, 'detect_tools', return_value=tools):
            code, out, err, unused = self.run_cli(['upgrade-check', '--target-version', '9.7.0'],
                                                  ReplayConnection(server_variables(version='8.4.3')))
        self.assertEqual(code, 2)
        self.assertIn('--no-shell', err)

    def test_mariadb_guidance_and_bad_target(self):
        code, out, err, unused = self.run_cli(['upgrade-check', '--target-version', '11.8.0'],
                                              ReplayConnection(server_variables('mariadb')))
        self.assertEqual(code, 0, err)
        self.assertIn('MariaDB guidance', out)
        code, out, err, unused = self.run_cli(['upgrade-check', '--target-version', 'latest'], ReplayConnection())
        self.assertEqual(code, 2)

    def test_review_upgrade_status_without_problem_counts_trips_gate_and_renders(self):
        for status in ('ERROR', 'CONFIGURATION_ERROR'):
            report = lc.parse_upgrade_json(json.dumps({'checksPerformed': [
                {'id': 'configFailure', 'title': 'Configuration failed', 'status': status,
                 'description': 'Cannot inspect the configuration', 'detectedProblems': []}],
                'errorCount': 0, 'warningCount': 0, 'noticeCount': 0}))
            report.update(server=lc.read_server(ReplayConnection()), target_version='9.0.0', method='mysqlsh')
            with mock.patch.object(lc, 'upgrade_check', return_value=report):
                for threshold in ('error', 'warning'):
                    code, out, err, unused = self.run_cli(['upgrade-check', '--target-version', '9.0.0',
                                                           '--fail-on', threshold], ReplayConnection())
                    self.assertEqual(code, 1, (status, out, err))
                    self.assertIn('ERROR    configFailure', out)
                    self.assertIn('Cannot inspect the configuration', out)


class WriteCommandsCli(Isolated):
    def setUp(self):
        super(WriteCommandsCli, self).setUp()
        self.out_dir = os.path.join(os.path.realpath(self.tmp.name), 'backup')
        tools = {'tools': {'mysqldump': {'available': True, 'engine': 'mysql', 'version': 'mysqldump Ver 8.4.8',
                                         'path': '/usr/bin/mysqldump', 'deprecated': False}}}
        detect = mock.patch.object(lc, 'detect_tools', return_value=tools)
        detect.start()
        self.addCleanup(detect.stop)

    def audit(self):
        return [(r['operation'], r['status'], r['outcome']) for r in self.store().audit_records()]

    def test_dump_without_terminal_needs_yes(self):
        code, out, err, unused = self.run_cli(['dump', self.out_dir, '--schema', 'app', '-p' + PASSWORD],
                                              ReplayConnection())
        self.assertEqual(code, 2)
        self.assertIn('Re-run with --yes', err)
        self.assertIn('DUMP on MySQL', err)  # the plan is described on stderr
        self.assertFalse(os.path.exists(self.out_dir))
        self.assertEqual(self.audit(), [('dump', 'refused', 'no_terminal')])
        self.assertNotIn(PASSWORD, json.dumps(self.store().audit_records()))

    def test_dump_preflight_prints_the_plan_and_writes_nothing(self):
        code, out, err, unused = self.run_cli(['dump', self.out_dir, '--schema', 'app', '--preflight', '--json'],
                                              ReplayConnection())
        self.assertEqual(code, 0, err)
        plan = json.loads(out)
        self.assertEqual(plan['schema_version'], 'lifecycle-plan-1.0')
        self.assertEqual(plan['target'], self.out_dir)
        self.assertFalse(os.path.exists(self.out_dir))
        self.assertEqual(self.audit(), [])

    def test_dump_interactive_yes_runs_the_job(self):
        with mock.patch.object(lc, 'dump_job', return_value=finished_job('dump')) as make:
            code, out, err, unused = self.run_cli(['dump', self.out_dir, '--schema', 'app'], ReplayConnection(),
                                                  tty=True, typed='y\n')
        self.assertEqual(code, 0, err)
        self.assertIn('Dump: done (completed)', out)
        self.assertEqual(make.call_args[1]['typed_target'], self.out_dir)
        self.assertTrue(make.call_args[1]['confirmed'])
        with mock.patch.object(lc, 'dump_job', return_value=finished_job('dump')):
            code, out, err, unused = self.run_cli(['dump', self.out_dir + '2'], ReplayConnection(), tty=True,
                                                  typed='n\n')
        self.assertEqual(code, 2)
        self.assertIn(('dump', 'declined', 'declined'), self.audit())

    def test_failed_verification_exits_1(self):
        with mock.patch.object(lc, 'dump_job', return_value=finished_job('dump', 'verification_failed')):
            code, out, err, unused = self.run_cli(['dump', self.out_dir, '--yes', '-q'], ReplayConnection())
        self.assertEqual(code, 1)
        self.assertIn('expected 2, got 3', out)
        self.assertEqual(err, '')

    def test_read_only_profile_refuses_before_connecting(self):
        store = self.store()
        store.save('profile', {'name': 'ro', 'host': 'db1', 'port': 3306, 'user': 'u', 'read_only': True},
                   password=PASSWORD, remember=True)
        for argv in (['dump', self.out_dir, '--profile', 'ro', '--yes'],
                     ['restore', self.out_dir, '--profile', 'ro', '--yes', '--confirm-target', 'db1:3306'],
                     ['clone', '--profile', 'ro', '--donor-host', 'db2', '--yes', '--confirm-target', 'db1:3306']):
            with self.subTest(command=argv[0]):
                code, out, err, connected = self.run_cli(argv)
                self.assertEqual(code, 2)
                self.assertIn('read-only', err)
                connected.assert_not_called()
        self.assertEqual([a[1:] for a in self.audit()], [('refused', 'read_only')] * 3)

    def test_restore_wrong_target_refuses_before_job_with_precise_message(self):
        for flags, tty, typed in ((['--yes', '--confirm-target', 'wrong'], False, ''),
                                  ([], True, 'wrong\n')):
            with self.subTest(flags=flags), mock.patch.object(lc, 'prepare_restore', return_value=fake_plan()), \
                    mock.patch.object(lc, 'restore_job') as make:
                code, out, err, _ = self.run_cli(['restore', self.out_dir, '--json'] + flags,
                                                ReplayConnection(), tty=tty, typed=typed)
            self.assertEqual((code, out), (2, ''), err)
            self.assertIn('Nothing was restored: the typed target does not match 127.0.0.1:3306.', err)
            self.assertNotIn('Partial', err)
            make.assert_not_called()
            self.assertIn('Nothing was restored:', self.store().audit_records()[0]['message'])

    def test_restore_typed_target(self):
        with mock.patch.object(lc, 'prepare_restore', return_value=fake_plan()), \
                mock.patch.object(lc, 'restore_job', return_value=finished_job('restore')) as make:
            code, out, err, unused2 = self.run_cli(['restore', self.out_dir, '--yes'], ReplayConnection())
            self.assertEqual(code, 2)
            self.assertIn('--confirm-target 127.0.0.1:3306', err)
            code, out, err, unused2 = self.run_cli(['restore', self.out_dir, '--yes', '--confirm-target', 'x'],
                                                      ReplayConnection())
            self.assertEqual(code, 2)
            code, out, err, unused2 = self.run_cli(['restore', self.out_dir], ReplayConnection(), tty=True,
                                                      typed='127.0.0.1:9999\n')
            self.assertEqual(code, 2)
            make.assert_not_called()
            code, out, err, unused = self.run_cli(['restore', self.out_dir], ReplayConnection(), tty=True,
                                                  typed='127.0.0.1:3306\n')
            self.assertEqual(code, 0, err)
            self.assertEqual(make.call_args[1]['typed_target'], '127.0.0.1:3306')
        self.assertEqual([a[1:] for a in self.audit()][:3], [('declined', 'declined'), ('refused', 'target_mismatch'),
                                                             ('refused', 'no_target')])
        code, out, err, unused = self.run_cli(['restore', self.out_dir, '--confirm-target', 'x'], ReplayConnection())
        self.assertEqual(code, 2)
        record = next(row for row in self.store().audit_records()
                      if row['outcome'] == 'target_mismatch')
        self.assertEqual(record['target'], fake_plan()['server']['identity'])

    def test_not_ready_plan_is_refused_and_preflight_exits_1(self):
        with mock.patch.object(lc, 'prepare_restore', return_value=fake_plan(ready=False)):
            code, out, err, unused2 = self.run_cli(['restore', self.out_dir, '--preflight'], ReplayConnection())
            self.assertEqual(code, 1)
            self.assertIn('Not ready', out)
            code, out, err, unused2 = self.run_cli(['restore', self.out_dir, '--yes', '--confirm-target',
                                                       '127.0.0.1:3306'], ReplayConnection())
            self.assertEqual(code, 2)
            self.assertIn('failed checks', err)

    def test_refusal_at_preflight_and_connection_failure_are_exit_2(self):
        os.mkdir(self.out_dir)
        open(os.path.join(self.out_dir, 'keep'), 'w').close()
        code, out, err, unused = self.run_cli(['dump', self.out_dir, '--yes', '-p' + PASSWORD], ReplayConnection())
        self.assertEqual(code, 2)
        self.assertIn('refusing to overwrite', err)

        class Down(ReplayConnection):
            def run(self, sql, extra_flags=None, timeout=None):
                raise ConnectorError("ERROR 2003 (HY000): Can't connect to MySQL server")
        code, out, err, unused = self.run_cli(['dump', self.out_dir + '2', '--yes'], Down())
        self.assertEqual(code, 2)
        self.assertIn("Can't connect", err)
        self.assertIn(('dump', 'error', 'error'), self.audit())

    def test_clone_needs_a_donor_and_mariadb_gets_guidance(self):
        code, out, err, unused = self.run_cli(['clone', '--yes'])
        self.assertEqual(code, 2)
        self.assertIn('--donor-host', err)
        code, out, err, unused = self.run_cli(['clone', '--donor-host', 'db2', '--preflight'],
                                              ReplayConnection(server_variables('mariadb')))
        self.assertEqual(code, 2)
        self.assertIn('mariadb-backup', err)

    def test_clone_donor_secret_redacted_from_preflight_failures(self):
        secret = 'donor-secret-with-spaces'
        self.store().save('profile', {'name': 'donor', 'host': 'db2', 'user': 'd'},
                          password=secret, remember=True)
        message = 'ERROR 1045: connection rejected ' + PASSWORD + ' and ' + secret
        for failure in (ConnectorError, ValueError, OSError, lc.Refused):
            for json_output in (False, True):
                for source in ('saved', 'prompt'):
                    with self.subTest(failure=failure, json=json_output, source=source):
                        argv = ['clone', '--ssl-mode', 'REQUIRED', '--donor-profile', 'donor', '-p' + PASSWORD, '--yes']
                        if json_output:
                            argv.append('--json')
                        if source == 'prompt':
                            argv.append('--donor-password')
                        with mock.patch.object(lc, 'prepare_clone', side_effect=failure(message)), \
                                mock.patch.object(lcli.getpass, 'getpass', return_value=secret):
                            code, out, err, unused = self.run_cli(argv, ReplayConnection())
                        self.assertEqual((code, out), (2, ''), err)
                        records = self.store().audit_records()
                        self.assertTrue(records)
                        text = out + err + json.dumps(records)
                        for password in (PASSWORD, secret):
                            self.assertNotIn(password, text)
                        self.assertIn('[redacted]', out + err)
                        self.assertEqual(out, '')

    def test_clone_saved_donor_secret_tracked_before_option_validation(self):
        secret = 'donor-validation-secret'
        self.store().save('profile', {'name': 'donor', 'host': 'db2', 'user': 'd'},
                          password=secret, remember=True)
        original = lcli._options
        def options(settings):
            if settings.get('password') == secret:
                raise ValueError('Invalid options for ' + secret)
            return original(settings)
        with mock.patch.object(lcli, '_options', side_effect=options):
            code, out, err, connected = self.run_cli(['clone', '--ssl-mode', 'REQUIRED', '--donor-profile', 'donor', '--yes'])
        self.assertEqual(code, 2)
        connected.assert_not_called()
        self.assertNotIn(secret, out + err + json.dumps(self.store().audit_records()))

    def test_clone_job_construction_and_decline_scrub_plan_and_error(self):
        secret = 'donor-job-secret'
        self.store().save('profile', {'name': 'donor', 'host': 'db2', 'user': 'd'},
                          password=secret, remember=True)
        plan = fake_plan('clone')
        plan['commands'] = ['echo ' + PASSWORD + ' ' + secret]
        plan['warnings'] = [PASSWORD + ' ' + secret]
        for failure in (ConnectorError, lc.Refused, None):
            with self.subTest(failure=failure), mock.patch.object(lc, 'prepare_clone', return_value=plan), \
                    mock.patch.object(lc, 'clone_job', side_effect=(failure('Failed ' + PASSWORD + ' ' + secret)
                                                                  if failure else None)) as make:
                argv = ['clone', '--ssl-mode', 'REQUIRED', '--donor-profile', 'donor', '-p' + PASSWORD, '--json']
                if failure:
                    argv += ['--yes', '--confirm-target', plan['target']]
                code, out, err, unused = self.run_cli(argv, ReplayConnection())
                self.assertEqual((code, out), (2, ''), err)
                if not failure:
                    make.assert_not_called()
                records = self.store().audit_records()
                self.assertTrue(records)
                for password in (PASSWORD, secret):
                    self.assertNotIn(password, out + err + json.dumps(records))
                self.assertIn('[redacted]', err)

    def test_clone_progress_rows_redacted_in_cli_json(self):
        secret = 'donor-json-secret'
        plan = fake_plan('clone')
        progress = {'schema_version': 'clone-progress-1.0',
                    'status': [['Failed', '1', PASSWORD + ' ' + secret]],
                    'progress': [['FILE COPY', secret, '100', '10']]}
        def make(donor, recipient, *args, **kwargs):
            return lc.LifecycleJob('clone', lambda job: {'status': 'completed', 'clone_progress': progress},
                                   recipient, passwords=(donor['password'],))
        with mock.patch.object(lc, 'prepare_clone', return_value=plan), \
                mock.patch.object(lc, 'clone_job', side_effect=make):
            code, out, err, unused = self.run_cli(['clone', '--donor-host', 'db2', '--donor-password=' + secret,
                                                  '-p' + PASSWORD, '--yes', '--confirm-target', plan['target'],
                                                  '--json'], ReplayConnection())
        self.assertEqual(code, 0, (out, err))
        self.assertEqual(json.loads(out)['result']['clone_progress']['status'][0][2], '[redacted] [redacted]')
        for password in (PASSWORD, secret):
            self.assertNotIn(password, out + err)

    def test_clone_cli_refuses_saved_donor_before_secret_lookup_without_recipient_tls(self):
        self.store().save('profile', {'name': 'saved-donor', 'host': 'db2', 'user': 'd'},
                          password='saved-donor-secret', remember=True)
        for mode in ('DISABLED', 'PREFERRED'):
            with self.subTest(mode=mode), \
                    mock.patch.object(WorkspaceStore, 'connection_with_saved_password') as secret:
                code, out, err, connected = self.run_cli([
                    'clone', '--donor-profile', 'saved-donor', '--ssl-mode', mode, '--yes'])
                self.assertEqual(code, 2, (out, err))
                self.assertIn('recipient', out + err)
                secret.assert_not_called()
                connected.assert_not_called()

    def test_clone_cli_saved_donor_tls_cannot_be_downgraded(self):
        for mode in ('REQUIRED', 'VERIFY_CA', 'VERIFY_IDENTITY'):
            with self.subTest(mode=mode):
                self.store().save('profile', {'name': mode, 'host': 'db2', 'user': 'd', 'ssl_mode': mode},
                                  password='donor-tls-secret', remember=True)
                with mock.patch.object(WorkspaceStore, 'connection_with_saved_password') as secret:
                    code, out, err, connected = self.run_cli([
                        'clone', '--donor-profile', mode, '--donor-ssl-mode', 'DISABLED',
                        '--no-require-ssl', '--yes'])
                self.assertEqual(code, 2, (out, err))
                self.assertIn('donor requires TLS', out + err)
                secret.assert_not_called()
                connected.assert_not_called()
                records = self.store().audit_records()
                self.assertEqual(records[0]['status'], 'refused')
                self.assertNotIn('donor-tls-secret', out + err + json.dumps(records))

    def test_clone_cli_preserves_saved_donor_policy_in_plan_and_job(self):
        profile = self.store().save('profile', {'name': 'donor', 'host': 'db2', 'port': 3308,
                                    'user': 'd', 'type': 'development', 'read_only': True},
                                    password='donor-policy-secret', remember=True)
        plan = fake_plan('clone')
        with mock.patch.object(lc, 'prepare_clone', return_value=plan) as prepare, \
                mock.patch.object(lc, 'clone_job', return_value=finished_job('clone')) as make:
            code, out, err, unused = self.run_cli(['clone', '--donor-profile', 'donor', '--ssl-mode', 'REQUIRED', '--yes',
                                                  '--confirm-target', plan['target']], ReplayConnection())
        self.assertEqual(code, 0, (out, err))
        for options in (prepare.call_args[0][2], make.call_args[0][0]):
            self.assertEqual(options.get('profile_id'), profile['id'])
            self.assertEqual(options.get('type'), 'development')
            self.assertIs(options.get('read_only'), True)
            self.assertEqual(options['password'], 'donor-policy-secret')
        self.assertNotIn('donor-policy-secret', out + err)

    def test_root_help_lists_the_commands(self):
        code, out, unused, unused2 = self.run_cli(['--help'])
        for name in ('upgrade-check', 'dump ', 'restore ', 'clone '):
            self.assertIn('\n  ' + name, out)

    def test_review_background_connection_and_input_errors_exit_2(self):
        # Preflight succeeds; the worker's fresh connection then fails.
        for exc in (ConnectorError('ERROR 2003: Cannot connect'), ValueError('Invalid input')):
            conn = ReplayConnection()
            conn.opens = 0
            def enter():
                conn.opens += 1
                if conn.opens > 1:
                    raise exc
                return conn
            with mock.patch.object(ReplayConnection, '__enter__', side_effect=enter):
                code, out, err, unused = self.run_cli(['dump', self.out_dir, '--schema', 'app', '--yes', '--json'], conn)
            self.assertEqual(code, 2, (out, err))
            self.assertIn(json.loads(out)['result']['failure_category'], ('input', 'connection'))
        for status in ('refused', 'cancelled', 'verification_failed'):
            self.assertEqual(lcli._exit_code({'state': status, 'result': {'status': status}}), 1)

    def test_review_cancel_cli_joins_and_restores_signal_handler(self):
        job = mock.Mock()
        job.thread.is_alive.side_effect = [True, False]
        job.thread.join.side_effect = KeyboardInterrupt
        job.snapshot.return_value = {'state': 'cancelled'}
        previous = object()
        with mock.patch.object(lcli.signal, 'signal', return_value=previous) as signals:
            self.assertEqual(lcli._run_job(types.SimpleNamespace(quiet=True), job)['state'], 'cancelled')
        job.close.assert_called_once_with()
        job.wait.assert_not_called()
        self.assertEqual(signals.call_args[0], (lcli.signal.SIGINT, previous))

    def test_review_system_table_acknowledgement_and_staging_cli(self):
        plan = fake_plan()
        required = 'REPLACE mysql SYSTEM TABLES ON ' + plan['target']
        plan['required_acknowledgement'] = required
        plan['checks'].append({'check': 'staging_space', 'status': 'passed',
                               'reason': 'Private restore snapshot in /chosen requires 4096 bytes.'})
        with mock.patch.object(lc, 'prepare_restore', return_value=plan) as prepare, \
                mock.patch.object(lc, 'restore_job', return_value=finished_job('restore')) as make:
            argv = ['restore', self.out_dir, '--yes', '--confirm-target', plan['target'], '--existing', 'drop']
            code, out, err, unused = self.run_cli(argv, ReplayConnection())
            self.assertEqual(code, 2)
            make.assert_not_called()
            code, out, err, unused = self.run_cli(argv + ['--acknowledge-system-tables', required,
                                                         '--staging-directory', '/chosen'], ReplayConnection())
            self.assertEqual(code, 0, (out, err))
            self.assertEqual(make.call_args[1]['typed_acknowledgement'], required)
            self.assertEqual(prepare.call_args[1]['staging_directory'], '/chosen')
            self.assertIn('/chosen requires 4096 bytes', err)
            self.assertIn(required, err)


    def test_typed_target_exact_and_interrupted_acknowledgement_declined(self):
        plan = fake_plan()
        for value in (' 127.0.0.1:3306', '127.0.0.1:3306 '):
            with self.subTest(value=value), mock.patch.object(lc, 'prepare_restore', return_value=plan), \
                    mock.patch.object(lc, 'restore_job') as make:
                code, out, err, _ = self.run_cli(['restore', self.out_dir, '--json'], ReplayConnection(),
                                                 tty=True, typed=value + '\n')
                self.assertEqual((code, out), (2, ''), err)
                self.assertEqual(self.audit()[0][1:], ('declined', 'declined'))
                make.assert_not_called()
        plan['required_acknowledgement'] = 'REPLACE SYSTEM TABLES'
        for exception in (EOFError, KeyboardInterrupt):
            with self.subTest(exception=exception), mock.patch.object(lc, 'prepare_restore', return_value=plan), \
                    mock.patch.object(lc, 'restore_job') as make:
                code, out, err, _ = self.run_cli(['restore', self.out_dir, '--json'], ReplayConnection(),
                                                 tty=True, typed=exception())
                self.assertEqual((code, out), (2, ''), err)
                self.assertEqual(self.audit()[0][1:], ('declined', 'declined'))
                make.assert_not_called()


class Endpoints(Isolated):
    def setUp(self):
        super(Endpoints, self).setUp()
        self.workspace = types.SimpleNamespace(store=WorkspaceStore(), lifecycle=lc.LifecycleJobs())
        self.addCleanup(self.workspace.lifecycle.close)
        self.connection = {'host': '127.0.0.1', 'port': 3306, 'user': 'u', 'password': PASSWORD}

    def reviewed(self, payload):
        payload = dict(payload)
        plan = payload.get('plan')
        if isinstance(plan, dict):
            payload['plan'] = dict(plan)
            payload['plan']['review_token'] = ui._review_tokens(self.workspace).issue('lifecycle',
                ui._review_identity(payload.get('connection'), self.workspace), ui._lifecycle_review_details(plan))
        return payload

    def call(self, path, payload):
        if path == '/api/lifecycle/start':
            payload = self.reviewed(payload)
        return ui.api_response(path, payload, self.workspace)

    def test_paths_are_registered(self):
        self.assertIn('/api/lifecycle/start', ui._WRITE_PATHS)
        for path in ui._LIFECYCLE_READ_PATHS + ui._LIFECYCLE_WRITE_PATHS:
            self.assertIn(path, ui._CONNECTION_PATHS)
        for path in ('/api/lifecycle/status', '/api/lifecycle/cancel'):
            self.assertNotIn(path, ui._CONNECTION_PATHS)
            self.assertIn(path, ui._LIFECYCLE_PATHS)
        # Real HTTP parsing/routing, without a listening socket: sandboxed
        # test runners may prohibit bind even on loopback.
        self.workspace.origin, self.workspace.token = 'http://127.0.0.1:8765', 'test-token'
        self.workspace.cookie_name, self.workspace.session_secret = 'test-cookie', 'test-session'
        request = (b'POST /api/lifecycle/status HTTP/1.0\r\nHost: 127.0.0.1:8765\r\n'
                   b'Origin: http://127.0.0.1:8765\r\nCookie: test-cookie=test-session\r\n'
                   b'Content-Type: application/json\r\nX-Myflames-Token: test-token\r\n'
                   b'Content-Length: 11\r\n\r\n{"id": "x"}')
        connection = mock.Mock()
        connection.makefile.return_value = io.BytesIO(request)
        chunks = []
        connection.sendall.side_effect = chunks.append
        ui.WorkspaceHandler(connection, ('127.0.0.1', 1234), self.workspace)
        response = b''.join(chunks)
        self.assertTrue(response.startswith(b'HTTP/1.0 400'), response)
        self.assertIn(b'No such job', response)

    def test_start_needs_confirmation_and_audits_the_refusal(self):
        with mock.patch.object(lc, '_connect', side_effect=AssertionError('must not connect')):
            result = self.call('/api/lifecycle/start', {'operation': 'restore', 'plan': fake_plan(),
                                                        'connection': self.connection, 'typed_target': 'x'})
        self.assertEqual(result['state'], 'refused')
        self.assertEqual(result['result']['status'], 'needs_confirmation')
        records = self.workspace.store.audit_records()
        self.assertEqual([(r['status'], r['outcome'], r['surface']) for r in records],
                         [('refused', 'needs_confirmation', 'ui')])
        self.assertNotIn(PASSWORD, json.dumps(records))

    def test_read_only_profile_refuses_before_the_saved_password(self):
        profile = self.workspace.store.save('profile', {'name': 'ro', 'host': '127.0.0.1', 'port': 3306,
                                                         'user': 'u', 'read_only': True},
                                            password=PASSWORD, remember=True)
        connection = {'host': '127.0.0.1', 'port': 3306, 'user': 'u', 'profile_id': profile['id']}
        with mock.patch.object(self.workspace.store, 'connection_with_saved_password',
                               side_effect=AssertionError('password read before the policy')):
            result = self.call('/api/lifecycle/start', {'operation': 'dump', 'plan': fake_plan('dump'),
                                                        'connection': connection, 'confirmed': True})
        self.assertEqual(result['state'], 'refused')
        self.assertIn('read-only', result['error'])

    def test_restore_wrong_target_refuses_before_password_or_job(self):
        payload = {'operation': 'restore', 'plan': fake_plan(), 'connection': self.connection,
                   'confirmed': True, 'typed_target': 'wrong'}
        with mock.patch.object(ui, '_with_saved_password') as password, \
                mock.patch.object(lc, 'restore_job') as make, \
                mock.patch.object(ui, '_lifecycle_add') as add:
            with self.assertRaisesRegex(lc.Refused,
                    'Nothing was restored: the typed target does not match 127.0.0.1:3306.'):
                self.call('/api/lifecycle/start', payload)
        password.assert_not_called()
        make.assert_not_called()
        add.assert_not_called()
        records = self.workspace.store.audit_records()
        self.assertEqual([(r['status'], r['outcome']) for r in records], [('refused', 'target_mismatch')])
        self.assertEqual(records[0]['target'], payload['plan']['server']['identity'])
        self.assertNotIn('Partial', json.dumps(records))

    def test_start_status_and_cancel(self):
        with mock.patch.object(lc, 'restore_job', return_value=finished_job('restore')) as make:
            started = self.call('/api/lifecycle/start', {'operation': 'restore', 'plan': fake_plan(),
                                                         'connection': self.connection, 'confirmed': True,
                                                         'typed_target': '127.0.0.1:3306'})
        self.assertEqual(make.call_args[1]['typed_target'], '127.0.0.1:3306')
        self.assertTrue(make.call_args[1]['confirmed'])
        self.assertEqual(make.call_args[0][0]['password'], PASSWORD)
        job = self.workspace.lifecycle.get(started['id'])
        job.wait(5)
        status = self.call('/api/lifecycle/status', {'id': started['id'], 'connection': self.connection})
        self.assertEqual(status['state'], 'done')
        self.assertEqual(self.call('/api/lifecycle/cancel', {'id': started['id'], 'connection': self.connection})['state'], 'done')
        with self.assertRaises(ValueError):
            self.call('/api/lifecycle/status', {'id': 'gone'})
        with self.assertRaises(ValueError):
            self.call('/api/lifecycle/start', {'operation': 'drop', 'plan': fake_plan()})
        with self.assertRaises(ValueError):
            self.call('/api/lifecycle/start', {'operation': 'dump', 'plan': fake_plan('restore'),
                                               'connection': self.connection, 'confirmed': True})

    def test_status_cancel_require_the_job_owner_without_loading_credentials(self):
        job = finished_job('restore')
        ui._lifecycle_add(self.workspace, job, 'target', self.connection)
        job.wait(5)
        with mock.patch.object(job, 'cancel') as cancel, \
                mock.patch.object(self.workspace.store, 'connection_with_saved_password') as password:
            for route in ('status', 'cancel'):
                for connection in (None, {}, dict(self.connection, host='other'),
                                   dict(self.connection, user='other'), dict(self.connection, port=3307),
                                   dict(self.connection, ssl_mode='REQUIRED'),
                                   dict(self.connection, profile_id='other')):
                    with self.subTest(route=route, connection=connection), self.assertRaisesRegex(ValueError, 'No such job'):
                        self.call('/api/lifecycle/' + route, {'id': job.id, 'connection': connection})
                with self.assertRaisesRegex(ValueError, 'No such job'):
                    self.call('/api/lifecycle/' + route, {'id': 'gone', 'connection': self.connection})
            cancel.assert_not_called()
            for route in ('status', 'cancel'):
                result = self.call('/api/lifecycle/' + route, {'id': job.id, 'connection':
                                   dict(self.connection, type='development', read_only=True, password='changed')})
                self.assertEqual(result['id'], job.id)
            cancel.assert_called_once_with()
            password.assert_not_called()

    def clone_payload(self, ssl_mode=None):
        value = {'name': 'donor', 'host': '127.0.0.1', 'port': 3308, 'user': 'd',
                 'type': 'development', 'read_only': True}
        if ssl_mode:
            value['ssl_mode'] = ssl_mode
        profile = self.workspace.store.save('profile', value, password='donor-secret', remember=True)
        donor = dict(value, profile_id=profile['id'])
        donor.pop('name')
        payload = {'operation': 'clone', 'connection': dict(self.connection, ssl_mode='REQUIRED'),
                   'donor': donor, 'require_ssl': True}
        with mock.patch.object(lc, '_connect', return_value=ReplayConnection()), \
                mock.patch.object(lc, 'detect_tools', return_value={'tools': {
                    'mysql': {'available': True, 'path': '/fake/mysql', 'engine': 'mysql', 'version': '8.4.11'}}}):
            plan = self.call('/api/lifecycle/prepare', payload)
        return dict(payload, plan=plan, confirmed=True, typed_target=plan['target'])

    def test_clone_saved_donor_never_released_to_recipient_without_tls(self):
        payload = self.clone_payload('VERIFY_IDENTITY')
        for mode in (None, 'DISABLED', 'PREFERRED'):
            value = dict(payload, connection=dict(payload['connection'], ssl_mode=mode))
            with self.subTest(mode=mode), \
                    mock.patch.object(self.workspace.store, 'connection_with_saved_password') as secret:
                with self.assertRaisesRegex(lc.Refused, 'recipient'):
                    ui._lifecycle_donor(value, self.workspace, [])
                secret.assert_not_called()
        for mode in ('REQUIRED', 'VERIFY_CA', 'VERIFY_IDENTITY'):
            value = dict(payload, connection=dict(payload['connection'], ssl_mode=mode))
            options = ui._lifecycle_donor(value, self.workspace, [])
            self.assertEqual(options['password'], 'donor-secret')
            self.assertIs(options['_saved_password'], True)
        value = dict(payload, connection=dict(payload['connection'], ssl_mode='DISABLED'),
                     donor=dict(payload['donor'], password='explicit-password'))
        options = ui._lifecycle_donor(value, self.workspace, [])
        self.assertEqual(options['password'], 'explicit-password')
        self.assertNotIn('_saved_password', options)

    def test_clone_donor_policy_is_recorded_before_saved_secret_is_read(self):
        original = dbops.connection_safety
        seen = []
        def safety(profile, connection):
            result = original(profile, connection)
            seen.append(result)
            return result
        saved = self.workspace.store.connection_with_saved_password
        def fill(connection):
            self.assertTrue(any(item['profile_id'] == connection['profile_id'] for item in seen))
            return saved(connection)
        with mock.patch.object(dbops, 'connection_safety', side_effect=safety), \
                mock.patch.object(self.workspace.store, 'connection_with_saved_password', side_effect=fill):
            payload = self.clone_payload()
            with mock.patch.object(lc, 'clone_job', return_value=finished_job('clone')) as make:
                result = self.call('/api/lifecycle/start', payload)
        self.workspace.lifecycle.get(result['id']).wait(5)
        donor = payload['plan']['expect']['donor']
        self.assertEqual((donor['endpoint'], donor['type'], donor['read_only']),
                         ('127.0.0.1:3308', 'development', True))
        self.assertEqual(payload['plan']['expect']['recipient']['endpoint'], '127.0.0.1:3306')
        self.assertEqual(make.call_args[0][0]['password'], 'donor-secret')

    def test_clone_changed_pair_or_policy_refused_before_secret_lookup(self):
        payload = self.clone_payload()
        changed = dict(payload, connection=dict(payload['connection'], port=3310))
        recipient_key = json.loads(payload['plan']['expect']['recipient']['connection_key'])
        recipient_key[2] = 3310
        recipient_key = json.dumps(recipient_key, separators=(',', ':'))
        forged_expect = dict(payload['plan']['expect'], recipient=dict(payload['plan']['expect']['recipient'],
                            endpoint='127.0.0.1:3310', connection_key=recipient_key))
        changed['plan'] = dict(payload['plan'], expect=forged_expect, connection_key=recipient_key)
        with mock.patch.object(self.workspace.store, 'connection_with_saved_password') as secret:
            with self.assertRaisesRegex(ValueError, 'Confirm both endpoints'):
                self.call('/api/lifecycle/start', changed)
            secret.assert_not_called()
        for key, fields in (('donor', {'host': 'other'}), ('donor', {'user': 'other'}),
                            ('connection', {'port': 3310}), ('donor', {'ssl_mode': 'REQUIRED'})):
            changed = dict(payload, **{key: dict(payload[key], **fields)})
            with self.subTest(key=key, fields=fields), \
                    mock.patch.object(self.workspace.store, 'connection_with_saved_password') as secret, \
                    mock.patch.object(lc, 'clone_job') as make:
                with self.assertRaisesRegex(ValueError, 'Confirm both endpoints'):
                    self.call('/api/lifecycle/start', changed)
                secret.assert_not_called()
                make.assert_not_called()
        profile = self.workspace.store.get('profile', payload['donor']['profile_id'])
        self.workspace.store.save('profile', dict(profile, type='production'), profile['id'])
        with mock.patch.object(self.workspace.store, 'connection_with_saved_password') as secret:
            with self.assertRaisesRegex(ValueError, 'Confirm both endpoints'):
                self.call('/api/lifecycle/start', payload)
            secret.assert_not_called()

    def test_clone_donor_tls_cannot_be_downgraded(self):
        payload = self.clone_payload('REQUIRED')
        for route in ('prepare', 'start'):
            changed = dict(payload, require_ssl=False)
            changed['plan'] = dict(payload['plan'], request=dict(payload['plan']['request'], require_ssl=False))
            with self.subTest(route=route), mock.patch.object(lc, '_connect', return_value=ReplayConnection()), \
                    mock.patch.object(self.workspace.store, 'connection_with_saved_password') as secret:
                with self.assertRaisesRegex(ValueError, 'donor requires TLS'):
                    self.call('/api/lifecycle/' + route, changed)
                secret.assert_not_called()

    def test_clone_errors_and_audits_redact_both_passwords(self):
        self.connection['ssl_mode'] = 'REQUIRED'
        recipient = self.workspace.store.save('profile', dict(self.connection, name='recipient'),
                                               password=PASSWORD, remember=True)
        self.connection = dict(self.connection, profile_id=recipient['id'])
        self.connection.pop('password')
        payload = self.clone_payload()
        message = PASSWORD + ' donor-secret'
        for route, function in (('prepare', 'prepare_clone'), ('start', 'clone_job')):
            with self.subTest(route=route), mock.patch.object(lc, '_connect', return_value=ReplayConnection()), \
                    mock.patch.object(lc, function, side_effect=RuntimeError(message)):
                with self.assertRaises(RuntimeError) as caught:
                    self.call('/api/lifecycle/' + route, payload)
                text = str(caught.exception) + json.dumps(self.workspace.store.audit_records())
                self.assertNotIn(PASSWORD, text)
                self.assertNotIn('donor-secret', text)
        # Refusals before any secret lookup still scrub both submitted passwords.
        payload['connection']['password'] = PASSWORD
        payload['donor']['password'] = 'donor-secret'
        payload['plan']['commands'] = ['invalid ' + message]
        payload['confirmed'] = False
        self.call('/api/lifecycle/start', payload)
        text = json.dumps(self.workspace.store.audit_records())
        self.assertNotIn(PASSWORD, text)
        self.assertNotIn('donor-secret', text)

    def test_clone_start_http_failure_scrubs_saved_donor_in_audit(self):
        payload = self.clone_payload()
        self.workspace.origin, self.workspace.token = 'http://127.0.0.1:8765', 'test-token'
        self.workspace.cookie_name, self.workspace.session_secret = 'test-cookie', 'test-session'
        for function in ('clone_job', '_lifecycle_add'):
            for error in (ValueError, ConnectorError):
                with self.subTest(function=function, error=error):
                    body = json.dumps(self.reviewed(payload)).encode('utf-8')
                    request = (b'POST /api/lifecycle/start HTTP/1.0\r\nHost: 127.0.0.1:8765\r\n'
                               b'Origin: http://127.0.0.1:8765\r\nCookie: test-cookie=test-session\r\n'
                   b'Content-Type: application/json\r\nX-Myflames-Token: test-token\r\n'
                               b'Content-Length: ' + str(len(body)).encode('ascii') + b'\r\n\r\n' + body)
                    connection = mock.Mock()
                    connection.makefile.return_value = io.BytesIO(request)
                    chunks = []
                    connection.sendall.side_effect = chunks.append
                    module = lc if function == 'clone_job' else ui
                    with mock.patch.object(lc, 'clone_job', return_value=finished_job('clone')), \
                            mock.patch.object(module, function,
                                              side_effect=error('Rejected ' + PASSWORD + ' donor-secret')):
                        ui.WorkspaceHandler(connection, ('127.0.0.1', 1234), self.workspace)
                    response = b''.join(chunks).decode('utf-8')
                    self.assertTrue(response.startswith('HTTP/1.0 400'), response)
                    records = self.workspace.store.audit_records()
                    self.assertTrue(records)
                    for secret in (PASSWORD, 'donor-secret'):
                        self.assertNotIn(secret, response + json.dumps(records))
                    self.assertIn('[redacted]', response)

    def test_clone_progress_rows_redacted_in_status_and_jobs(self):
        secret = 'donor-progress-secret'
        progress = {'schema_version': 'clone-progress-1.0',
                    'status': [['Failed', '1', PASSWORD + ' ' + secret]],
                    'progress': [['FILE COPY', secret, '100', '10']]}
        job = lc.LifecycleJob('clone', lambda j: {'status': 'completed', 'clone_progress': progress},
                               self.connection, passwords=(secret,))
        job.detail = progress
        ui._lifecycle_add(self.workspace, job, 'target', self.connection)
        job.wait(5)
        for route in ('status', 'jobs'):
            result = self.call('/api/lifecycle/' + route, {'id': job.id, 'connection': self.connection})
            text = json.dumps(result)
            self.assertNotIn(PASSWORD, text)
            self.assertNotIn(secret, text)
            self.assertIn('[redacted]', text)

    def test_http_config_path_is_rejected_before_connecting(self):
        for route in ('upgrade-check', 'prepare'):
            for value in ('/etc/passwd', '/home/user/my.cnf', [], ''):
                with self.subTest(route=route, value=value), mock.patch.object(lc, '_connect') as connect:
                    with self.assertRaisesRegex(ValueError, 'config_path is not accepted over HTTP'):
                        self.call('/api/lifecycle/' + route, {'connection': self.connection, 'config_path': value})
                    connect.assert_not_called()

    def test_jobs_lists_this_connections_jobs_so_a_lost_start_reply_is_recovered(self):
        """A page whose Start reply was lost (or that reloaded) finds the job by
        its connection instead of starting it again; the policy flags do not
        change the owner, another endpoint sees nothing, expired jobs drop out."""
        self.assertIn('/api/lifecycle/jobs', ui._LIFECYCLE_PATHS)
        self.assertNotIn('/api/lifecycle/jobs', ui._CONNECTION_PATHS)
        self.assertNotIn('/api/lifecycle/jobs', ui._WRITE_PATHS)
        self.assertEqual(self.call('/api/lifecycle/jobs', {'connection': self.connection})['jobs'], [])
        with mock.patch.object(lc, 'restore_job', return_value=finished_job('restore')):
            started = self.call('/api/lifecycle/start', {'operation': 'restore', 'plan': fake_plan(),
                                                         'connection': self.connection, 'confirmed': True,
                                                         'typed_target': '127.0.0.1:3306'})
        self.workspace.lifecycle.get(started['id']).wait(5)
        listed = self.call('/api/lifecycle/jobs', {'connection': dict(self.connection, password='other')})
        self.assertEqual(listed['schema_version'], 'lifecycle-jobs-1.0')
        self.assertEqual([(j['id'], j['operation'], j['state']) for j in listed['jobs']],
                         [(started['id'], 'restore', 'done')])
        self.assertNotIn(PASSWORD, json.dumps(listed))
        development = dict(self.connection, type='development', read_only=True)
        self.assertEqual([j['id'] for j in self.call('/api/lifecycle/jobs', {'connection': development})['jobs']],
                         [started['id']])
        for other in (dict(self.connection, host='db2'), dict(self.connection, port=3307),
                      dict(self.connection, user='v'), dict(self.connection, profile_id='p1')):
            self.assertEqual(self.call('/api/lifecycle/jobs', {'connection': other})['jobs'], [])
        with self.assertRaises(ValueError):
            self.call('/api/lifecycle/jobs', {})
        # A job the registry let go of (an hour old, or past 20) drops out of the list.
        self.workspace.lifecycle.jobs.pop(started['id'])
        self.assertEqual(self.call('/api/lifecycle/jobs', {'connection': self.connection})['jobs'], [])
        self.assertNotIn(started['id'], self.workspace.lifecycle_owners)

    def test_jobs_forgets_an_owner_when_the_registry_refuses_the_job(self):
        with mock.patch.object(lc, 'restore_job', return_value=finished_job('restore')), \
                mock.patch.object(self.workspace.lifecycle, 'add',
                                  side_effect=ValueError('Two lifecycle operations are running.')):
            with self.assertRaises(ValueError):
                self.call('/api/lifecycle/start', {'operation': 'restore', 'plan': fake_plan(),
                                                   'connection': self.connection, 'confirmed': True,
                                                   'typed_target': '127.0.0.1:3306'})
        self.assertEqual(self.call('/api/lifecycle/jobs', {'connection': self.connection})['jobs'], [])
        self.assertEqual(getattr(self.workspace, 'lifecycle_owners', {}), {})

    def test_read_endpoints_fill_saved_passwords_for_both_connections(self):
        store = self.workspace.store
        recipient = store.save('profile', {'name': 'r', 'host': '127.0.0.1', 'port': 3307, 'user': 'u', 'ssl_mode': 'REQUIRED'},
                               password='recipient-pw', remember=True)
        donor = store.save('profile', {'name': 'd', 'host': '127.0.0.1', 'port': 3308, 'user': 'c'},
                           password='donor-pw', remember=True)
        seen = []
        def connect(factory, options):
            seen.append(options)
            return ReplayConnection()
        with mock.patch.object(lc, '_connect', side_effect=connect), \
                mock.patch.object(lc, 'prepare_clone', return_value=fake_plan('clone')) as prepare:
            plan = self.call('/api/lifecycle/prepare', {
                'operation': 'clone', 'donor_address': 'primary:3306', 'data_directory': '/copy',
                'connection': {'host': '127.0.0.1', 'port': 3307, 'user': 'u', 'profile_id': recipient['id'], 'ssl_mode': 'REQUIRED'},
                'donor': {'host': '127.0.0.1', 'port': 3308, 'user': 'c', 'profile_id': donor['id']}})
        self.assertEqual(plan['operation'], 'clone')
        self.assertEqual(sorted(o['password'] for o in seen), ['donor-pw', 'recipient-pw'])
        self.assertEqual(prepare.call_args[1], {'data_directory': '/copy', 'require_ssl': True,
                                                'donor_address': 'primary:3306'})
        tools = self.call('/api/lifecycle/tools', {})
        self.assertEqual(tools, {'tools': {}})

    def test_upgrade_check_endpoint_redacts_password_in_errors(self):
        class Denied(ReplayConnection):
            def run(self, sql, extra_flags=None, timeout=None):
                raise ConnectorError('ERROR 1045 (28000): Access denied (password %s)' % PASSWORD)
        with mock.patch.object(lc, '_connect', return_value=Denied()):
            with self.assertRaises(ConnectorError) as caught:
                self.call('/api/lifecycle/upgrade-check', {'connection': self.connection, 'target_version': '8.4.0'})
        self.assertNotIn(PASSWORD, str(caught.exception))

    def test_review_restore_endpoints_forward_staging_and_typed_acknowledgement(self):
        with mock.patch.object(lc, '_connect', return_value=ReplayConnection()), \
                mock.patch.object(lc, 'prepare_restore', return_value=fake_plan()) as prepare:
            self.call('/api/lifecycle/prepare', {'operation': 'restore', 'connection': self.connection,
                                                'directory': '/backup', 'staging_directory': '/chosen'})
        self.assertEqual(prepare.call_args[1]['staging_directory'], '/chosen')
        required = 'REPLACE mysql SYSTEM TABLES ON 127.0.0.1:3306'
        with mock.patch.object(lc, 'restore_job', return_value=finished_job('restore')) as make:
            result = self.call('/api/lifecycle/start', {'operation': 'restore', 'plan': fake_plan(),
                               'connection': self.connection, 'confirmed': True, 'typed_target': '127.0.0.1:3306',
                               'typed_acknowledgement': required})
        self.assertEqual(make.call_args[1]['typed_acknowledgement'], required)
        self.workspace.lifecycle.get(result['id']).wait(5)


if __name__ == '__main__':
    unittest.main()
