"""Round F2: real evidence scopes and policies over synthetic packet routers.

No live server coverage is claimed. Every account read below must be a
single evidence packet; a router can switch its answering node at any read.
"""
import io
import types
import unittest
from unittest import mock

from myflames import accounts, accounts_api, dbops, replication, ui, workspace_cli
from myflames.connection_groups import ProfileReadError
from myflames.connector import ConnectorError
import test_accounts as account_fixtures
import test_replication as replica_fixtures
import test_reviewed_writes_guard as guard_fixtures
from test_reviewed_writes_e1 import PacketRouter, StrictEvidenceTransport, resultset, OTHER_UUID


class AccountRouter(PacketRouter):
    def __init__(self, engine='mysql', exists=True):
        reader = account_fixtures.FakeConn(engine, exists=exists)
        self.first = dict(guard_fixtures.MARIA, version=reader.version)
        if engine == 'mysql':
            self.first['server_uuid'] = guard_fixtures.UUID
        self.second = dict(self.first, server_uuid=OTHER_UUID) if engine == 'mysql' else dict(self.first, server_id='43')
        self.route = lambda body, n: self.first
        self.fail_at = None
        self.read_error = None
        super(AccountRouter, self).__init__(reader, lambda body, n: self.route(body, n), self.answer)

    def answer(self, body, flags):
        if self.fail_at and self.fail_at in body:
            raise self.read_error
        if 'myflames_who' in body:
            node = self.packets[-1][1]
            role = 'NONE' if self.reader.engine == 'mysql' else '\\N'
            return ('myflames_who\tadmin@localhost\tadmin@localhost\t%s\t%s\n' % (role, self.reader.version) +
                    '\n'.join(account_fixtures.actor(self.reader.engine)['grants']) + '\n' +
                    ''.join('%s\t%s\n' % item for item in node.items()))
        if 'information_schema.APPLICABLE_ROLES' in body:
            return ''
        return self.reader.run(body, extra_flags=['--comments'])

    def run_stdin(self, sql, **kwargs):
        self.writes.append(sql)
        self.reader.locked = True
        return ''

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


class AccountScopeTests(unittest.TestCase):
    setUp = guard_fixtures.GuardTests.setUp
    records = guard_fixtures.GuardTests.records

    def test_each_optional_read_rejects_another_node(self):
        for stage in ('SHOW CREATE USER', 'SHOW GRANTS FOR `r`', 'SHOW PRIVILEGES', 'SHOW DATABASES'):
            with self.subTest(stage=stage):
                router = AccountRouter()
                router.reader.grants.append('GRANT `r`@`%` TO `app`@`%`')
                router.route = lambda body, n: router.second if stage in body else router.first
                scope = dbops.TargetEvidenceConnection(router)
                with self.assertRaisesRegex(ConnectorError, 'identity check could not run'):
                    accounts.read_account(scope, 'app')
                self.assertTrue(any(stage in body for body, node in router.packets))
                with self.assertRaises(ConnectorError):
                    scope.run_stdin('ALTER USER `app`@`%` ACCOUNT LOCK')
                self.assertEqual(router.writes, [])

    def test_every_packet_on_a_different_node_refuses(self):
        router = AccountRouter()
        router.route = lambda body, n: dict(router.first, server_uuid='%08x-2222-3333-4444-555555555555' % (n + 1))
        with self.assertRaises(ConnectorError):
            accounts.read_account(router, 'app')
        self.assertEqual(len(router.packets), 2)
        self.assertEqual(router.writes, [])

    def test_verified_optional_server_errors_keep_the_fallbacks(self):
        for stage in ('SHOW CREATE USER', 'SHOW PRIVILEGES', 'SHOW DATABASES'):
            with self.subTest(stage=stage):
                router = AccountRouter()
                router.fail_at, router.read_error = stage, ConnectorError('ERROR 1142: SELECT denied')
                scope = dbops.TargetEvidenceConnection(router)
                result = accounts.read_account(scope, 'app')
                self.assertTrue(result['exists'])
                self.assertTrue(result['warnings'])
                scope.check()

    def test_absent_account_1141_can_still_be_planned_on_both_engines(self):
        for engine in ('mysql', 'mariadb'):
            with self.subTest(engine=engine):
                router = AccountRouter(engine, exists=False)
                plan = accounts.plan_create(router, 'app', password='fixture-secret')
                self.assertFalse(plan['before']['exists'])
                self.assertTrue(plan['statements'])
                self.assertEqual(router.writes, [])

    def test_consent_refusal_survives_optional_catch_and_plan_lock(self):
        router = AccountRouter()
        error = ProfileReadError('This saved connection is outside the current connection group.')
        router.fail_at, router.read_error = 'SHOW CREATE USER', error
        scope = dbops.TargetEvidenceConnection(router)
        with self.assertRaises(ProfileReadError) as caught:
            accounts.plan_lock(scope, 'app')
        self.assertIs(caught.exception, error)
        calls = len(router.packets)
        for operation in (scope.check, lambda: scope.run('SELECT 1'), lambda: scope.run_stdin('ALTER USER app')):
            with self.assertRaises(ProfileReadError) as caught:
                operation()
            self.assertIs(caught.exception, error)
        self.assertEqual(len(router.packets), calls)
        self.assertEqual(router.writes, [])

    def test_apply_optional_identity_failure_is_refused_before_stale(self):
        router = AccountRouter()
        plan = accounts.plan_lock(router, 'app')
        router.route = lambda body, n: router.second if 'SHOW CREATE USER' in body else router.first
        out = accounts.apply(router, plan, plan['expect'], confirmed=True, store=self.store)
        self.assertEqual((out['status'], out['outcome']), ('refused', 'identity_unavailable'))
        self.assertEqual(router.writes, [])
        self.assertEqual(self.records()[0]['outcome'], 'identity_unavailable')

    def test_apply_checks_guard_failure_before_requesting_a_typed_name(self):
        router = AccountRouter()
        plan = accounts.plan_lock(router, 'app')
        def optional_guard(*args, **kwargs):
            scope = kwargs['conn']
            router.route = lambda body, n: router.second
            try:
                scope.run('SELECT @@version')
            except ConnectorError:
                pass
            return {'blocked': [], 'typed_name': 'app@%'}
        with mock.patch.object(accounts, '_guards', side_effect=optional_guard):
            out = accounts.apply(router, plan, plan['expect'], confirmed=True, store=self.store)
        self.assertEqual((out['status'], out['outcome']), ('refused', 'identity_unavailable'))
        self.assertEqual(router.writes, [])

    def test_post_write_identity_failure_is_unknown_in_result_and_audit(self):
        router = AccountRouter()
        plan = accounts.plan_lock(router, 'app')
        router.route = lambda body, n: router.second if router.writes and 'SHOW CREATE USER' in body else router.first
        out = accounts.apply(router, plan, plan['expect'], confirmed=True, store=self.store)
        self.assertEqual((out['status'], out['outcome']), ('unknown', 'unverified'))
        self.assertEqual(len(router.writes), 1)
        self.assertEqual(out['after'], {})
        self.assertEqual(out['diff'], {})
        self.assertEqual(out['statements'][0]['status'], 'ok')
        self.assertEqual((self.records()[0]['status'], self.records()[0]['outcome']), ('unknown', 'unverified'))

    def test_final_audit_remains_pending_until_readback_then_keeps_after_state(self):
        router = AccountRouter()
        plan = accounts.plan_lock(router, 'app')
        pending = []
        def route(body, n):
            if router.writes:
                pending.append(self.records()[0]['status'])
            return router.first
        router.route = route
        out = accounts.apply(router, plan, plan['expect'], confirmed=True, store=self.store)
        self.assertEqual(out['status'], 'applied')
        self.assertTrue(pending)
        self.assertEqual(set(pending), {'pending'})
        record = self.records()[0]
        self.assertEqual((record['status'], record['outcome']), ('ok', 'applied'))
        self.assertEqual(record['after']['attributes'], out['after']['attributes'])
        self.assertTrue(record['after']['attributes']['locked'])
        # Completion remains one-way; no store-level rewrite exception was
        # introduced to repair a prematurely completed account record.
        self.assertFalse(self.store.audit_complete(record['id'], {'status': 'unknown'}))
        self.assertEqual(self.records()[0]['status'], 'ok')

    def test_post_write_consent_failure_keeps_unknown_result_and_audit(self):
        router = AccountRouter()
        plan = accounts.plan_lock(router, 'app')
        answer = router.answer
        def lose_consent(body, flags):
            if router.writes and 'SHOW CREATE USER' in body:
                raise ProfileReadError('This saved connection left the current connection group.')
            return answer(body, flags)
        router.answer = lose_consent
        out = accounts.apply(router, plan, plan['expect'], confirmed=True, store=self.store)
        self.assertEqual((out['status'], out['outcome']), ('unknown', 'unverified'))
        self.assertEqual(len(router.writes), 1)
        self.assertEqual(out['after'], {})
        self.assertEqual(out['diff'], {})
        self.assertEqual((self.records()[0]['status'], self.records()[0]['outcome']), ('unknown', 'unverified'))

    def test_verified_post_write_read_error_completes_as_sent(self):
        router = AccountRouter()
        plan = accounts.plan_lock(router, 'app')
        answer = router.answer
        def read_error(body, flags):
            if router.writes and 'SHOW GRANTS FOR `app`' in body:
                raise ConnectorError('ERROR 1142: SELECT denied')
            return answer(body, flags)
        router.answer = read_error
        out = accounts.apply(router, plan, plan['expect'], confirmed=True, store=self.store)
        self.assertEqual(out['status'], 'applied')
        self.assertTrue(out['warnings'])
        self.assertEqual(out['after'], {})
        self.assertEqual((self.records()[0]['status'], self.records()[0]['outcome']), ('ok', 'sent'))

    def test_identity_error_without_server_error_code_is_audited(self):
        router = AccountRouter()
        plan = accounts.plan_lock(router, 'app')
        error = ConnectorError('The identity check could not run.')
        error.identity_outcome = 'identity_unavailable'
        with mock.patch.object(accounts, '_run_stdin', side_effect=error):
            out = accounts.apply(router, plan, plan['expect'], confirmed=True, store=self.store)
        self.assertEqual(out['status'], 'refused')
        self.assertIsNone(self.records()[0]['error_code'])

    def test_api_pre_read_plan_and_apply_share_one_scope(self):
        for changed_at in (1, 2):
            with self.subTest(actor_read=changed_at):
                router = AccountRouter()
                workspace = types.SimpleNamespace(store=self.store)
                connection = {'host': router.host, 'port': router.port, 'user': router.user}
                def call(path, **payload):
                    return accounts_api.response(path, dict(payload, connection=connection), workspace,
                                                 lambda value: value, lambda connection, store: None)
                with mock.patch.object(accounts_api, 'MySQLConnection', return_value=router):
                    plan = call('/api/accounts/plan', operation='lock', user='app', host='%')
                    actor_reads = [0]
                    def route(body, n):
                        if 'myflames_who' in body:
                            actor_reads[0] += 1
                        return router.second if actor_reads[0] >= changed_at else router.first
                    router.route = route
                    before = len(self.store.audit_records())
                    out = call('/api/accounts/apply', plan_id=plan['plan_id'], expect=plan['expect'], confirmed=True)
                    self.assertEqual((out['status'], out['outcome']), ('refused', 'identity_unavailable'))
                    records = self.store.audit_records()
                    self.assertEqual(len(records), before + 1)
                    self.assertEqual(records[-1]['outcome'], 'identity_unavailable')
                self.assertEqual(router.writes, [])

    def test_mariadb_unsafe_hostname_explains_the_remedy_without_echoing_it(self):
        for hostname in ('node space', 'n\u00f8de', "node'quote", 'node\\slash'):
            with self.subTest(hostname=hostname):
                transport = StrictEvidenceTransport()
                transport.identity = dict(guard_fixtures.MARIA, hostname=hostname, version='11.4.13-MariaDB')
                transport.fail_read = False
                scope = dbops.TargetEvidenceConnection(transport)
                with self.assertRaisesRegex(ConnectorError, 'host name contains characters') as caught:
                    scope.run('SELECT 1')
                self.assertIn('Set a plain host name or use a MySQL server with a UUID', str(caught.exception))
                self.assertNotIn(hostname, str(caught.exception))
                with self.assertRaises(ConnectorError):
                    scope.run_write('ALTER USER app')
                self.assertEqual(transport.writes, [])


class ReadysetTransport(object):
    def __init__(self):
        self.reads, self.writes = [], []

    def run(self, sql, **kwargs):
        self.reads.append(sql)
        if sql.startswith('DELIMITER '):
            raise ConnectorError('ERROR 1105 (HY000) at line 2: unsupported SHOW GLOBAL VARIABLES')
        if sql == 'SELECT @@version_comment':
            return resultset([[('@@version_comment', 'Readyset')]])
        if sql == 'SHOW READYSET VERSION':
            return resultset([[('version', 'readyset-test')]])
        raise AssertionError(sql)

    def run_write(self, sql, **kwargs):
        self.writes.append(sql)
        raise AssertionError('Readyset hint must never authorize a write')

    run_stdin = run_write


class ReplicationScopeTests(unittest.TestCase):
    setUp = guard_fixtures.GuardTests.setUp

    def test_readyset_hint_preserves_a_refused_scope_for_skip_and_writes(self):
        transport = ReadysetTransport()
        scope = dbops.TargetEvidenceConnection(transport)
        report = replication.read_status(scope)
        self.assertTrue(report['readyset_hint'])
        self.assertEqual(report['channels'], [])
        self.assertEqual(report['settings'], {})
        self.assertIsNone(scope.variables)
        self.assertTrue(scope.problem)
        self.assertEqual(len(transport.reads), 3)
        recorded, _ = replica_fixtures.status('mysql-8.4-gtid-error')
        plan = recorded['channels'][0]['skip_plan']
        with self.assertRaises(ConnectorError):
            replication.skip_one(scope, '', plan['identity'], confirmed=True, statements=plan['statements'])
        for send in (scope.run_write, scope.run_stdin):
            with self.assertRaises(ConnectorError):
                send('START REPLICA SQL_THREAD')
        self.assertEqual(len(transport.reads), 3)
        self.assertEqual(transport.writes, [])

    def test_readyset_probe_never_runs_after_a_validated_identity(self):
        transport = StrictEvidenceTransport()
        transport.fail_read = False
        scope = dbops.TargetEvidenceConnection(transport)
        scope.run('SELECT 1')
        scope.problem = 'The identity check could not run.'
        with mock.patch('myflames.readyset.identify') as identify:
            with self.assertRaises(ConnectorError):
                replication.read_status(scope)
        identify.assert_not_called()

    def test_readyset_probe_never_runs_on_permission_or_connection_failures(self):
        for code in (1044, 1045, 1095, 1142, 1227, 6038, 2003, 2013):
            with self.subTest(code=code):
                transport = ReadysetTransport()
                with mock.patch.object(transport, 'run', side_effect=ConnectorError(
                        'ERROR %d (HY000) at line 2: Connection or permission failure' % code)), \
                        mock.patch('myflames.readyset.identify') as identify:
                    with self.assertRaises(ConnectorError):
                        replication.read_status(dbops.TargetEvidenceConnection(transport))
                identify.assert_not_called()
                self.assertEqual(transport.writes, [])

    def test_receiver_guard_identity_failure_cannot_disable_coverage(self):
        report, reader = replica_fixtures.status('mysql-8.4-gtid-error')
        first = dict(reader.evidence_values)
        router = PacketRouter(reader, lambda body, n: first if n == 0 else dict(first, server_uuid=OTHER_UUID),
                              answer=lambda body, flags: '7\tYES\tWaiting for source to send event\tstage/sql/Checking source version\t1\tYES\tYES\tYES\n')
        scope = dbops.TargetEvidenceConnection(router)
        scope.run('SELECT 1')
        with self.assertRaisesRegex(ConnectorError, 'identity check could not run'):
            replication.receiver_guard_plan(scope, report)
        self.assertEqual(router.writes, [])

    def test_loop_start_reuses_preflight_scope_without_starting_a_worker_on_mismatch(self):
        reader, _ = replica_fixtures.replay('replication', 'mysql-8.4-gtid-error')
        node = dict(reader.evidence_values)
        router = PacketRouter(reader, lambda body, n: node)
        scope = dbops.TargetEvidenceConnection(router)
        replication.read_status(scope)
        router.node_for = lambda body, n: dict(node, server_uuid=OTHER_UUID)
        loop = replication.SkipLoop(scope, '', [1062], 1)
        loop.start()
        self.assertIs(loop.conn, scope)
        self.assertIsNone(loop._thread)
        self.assertEqual(loop.status()['stop_reason'], 'connection_lost')
        self.assertEqual(router.writes, [])

    def test_ui_start_passes_the_status_scope_into_the_loop(self):
        reader, _ = replica_fixtures.replay('replication', 'mysql-8.4-gtid-error')
        workspace = types.SimpleNamespace(store=self.store)
        registry = mock.Mock()
        registry.start.side_effect = lambda identity, loop: loop
        with mock.patch.object(ui, '_replication_open', return_value=reader), \
                mock.patch.object(ui, '_replication_loops', return_value=registry), \
                mock.patch.object(replication, 'read_status', wraps=replication.read_status) as status:
            loop = ui._replication_loop_start({'connection': {'host': 'db', 'user': 'u'},
                'channel': '', 'codes': [1062], 'max_skips': 1, 'confirmed': True}, workspace)
        self.assertIs(loop.conn, status.call_args[0][0])
        self.assertIsInstance(loop.conn, dbops.TargetEvidenceConnection)
        self.assertIsNone(loop._thread)

    def test_cli_preserves_advertised_receiver_coverage_and_scope(self):
        reader, _ = replica_fixtures.replay('replication', 'mysql-8.4-gtid-error')
        scope_seen = []
        def coverage(conn, report):
            scope_seen.append(conn)
            return {'receiver_guard': 'on'}
        loop = mock.Mock()
        loop.run.return_value = {'message': 'Stopped.', 'stop_reason': 'max_skips', 'needs_attention': False}
        def connect(args, state, work):
            return work(reader)
        with mock.patch.object(workspace_cli, '_replication_connect', side_effect=connect), \
                mock.patch.object(replication, 'receiver_guard_plan', side_effect=coverage), \
                mock.patch.object(replication, 'SkipLoop', return_value=loop) as factory, \
                mock.patch('sys.stdout', io.StringIO()), mock.patch('sys.stderr', io.StringIO()):
            with self.assertRaises(SystemExit) as exit_code:
                workspace_cli._replication_skip_loop(['--host', 'db', '--user', 'u', '--channel', '',
                                                       '--errors', '1062', '--max', '1', '--yes'])
        self.assertEqual(exit_code.exception.code, 0)
        self.assertTrue(factory.call_args[1]['receiver_required'])
        self.assertIs(factory.call_args[0][0], scope_seen[0])
        self.assertIsInstance(scope_seen[0], dbops.TargetEvidenceConnection)
        self.assertIsNotNone(scope_seen[0].variables)
