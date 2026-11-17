"""Same-client identity binding: real policy/audit code, fake write clients.

No lab coverage is claimed here. The tests inspect the entire client input
and simulate mysql/MariaDB batch diagnostics, including MariaDB's SQL echo.
"""
import copy
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from myflames import accounts, cluster, dbops, locks, privileges, replication, reviewed_writes, variables
from myflames.connector import ConnectorError, MySQLConnection
from myflames.connection_groups import ProfileReadError
from myflames.workspace_store import WorkspaceStore

UUID = '11111111-2222-3333-4444-555555555555'
MYSQL = {'server_uuid': UUID}
MARIA = {'server_id': '42', 'hostname': 'test-server', 'port': '3306'}
MYSQL_GUARD = ("SET @myflames_target = (SELECT 1 FROM (SELECT 1 UNION ALL SELECT 1) AS g "
               "WHERE @@global.server_uuid <> '11111111-2222-3333-4444-555555555555');")
MARIA_GUARD = ("SET @myflames_target = (SELECT 1 FROM (SELECT 1 UNION ALL SELECT 1) AS g "
               "WHERE @@global.server_id <> 42 OR @@global.hostname <> 'test-server' OR @@global.port <> 3306);")
MISMATCH = 'ERROR 1242 (21000) at line 1: Subquery returns more than 1 row'


class Writer(object):
    password = None

    def __init__(self, error=None):
        self.writes = []
        self.error = error

    def run_write(self, sql, **kwargs):
        self.writes.append(sql)
        if self.error:
            raise ConnectorError(self.error)
        return ''

    run_stdin = run_write


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patch = mock.patch.dict(os.environ, {'HOME': self.temp.name, 'USERPROFILE': self.temp.name,
                                             'MYFLAMES_SECRET_BACKEND': 'vault'})
        patch.start()
        self.addCleanup(patch.stop)
        self.store = WorkspaceStore()

    def audit(self):
        return dbops.AuditTrail(self.store, 'guard.test', 'ui')

    def records(self):
        return dbops.audit_report(self.store)['records']

    def plan(self, engine='mysql', identity=None):
        return {'action': 'set', 'statements': ['SET GLOBAL max_connections = 200;',
                                               'SET GLOBAL max_connections = 201;'],
                'expect': {'engine': engine, 'server_identity': copy.deepcopy(identity or MYSQL)}}

    def assert_mismatch(self, out, writer):
        self.assertEqual((out['status'], out['outcome'], out['sent']), ('refused', 'identity_mismatch', []))
        self.assertEqual(out['message'], dbops.TARGET_IDENTITY_MISMATCH)
        self.assertEqual(len(writer.writes), 1)
        record = self.records()[0]
        self.assertEqual((record['status'], record['outcome'], record['error_code']),
                         ('refused', 'identity_mismatch', 1242))
        self.assertEqual(record['statements'][0], writer.writes[0].split('\n', 1)[0])

    def test_mysql_family_and_mariadb_prefixes_are_exact(self):
        for engine, identity, expected in [('mysql', MYSQL, MYSQL_GUARD), ('percona', MYSQL, MYSQL_GUARD),
                                            ('pxc', MYSQL, MYSQL_GUARD), ('mariadb', MARIA, MARIA_GUARD)]:
            with self.subTest(engine=engine):
                self.assertEqual(dbops.target_session_guard(engine, identity), [expected])
        node = dict(MARIA, wsrep_node_name='db-1.example')
        self.assertEqual(dbops.target_session_guard('mariadb', node),
                         [MARIA_GUARD[:-2] + " OR @@global.wsrep_node_name <> 'db-1.example');"])

    def test_missing_or_unsafe_identity_never_builds_a_script(self):
        for engine, base, key in [('mysql', MYSQL, 'server_uuid'), ('mariadb', MARIA, 'hostname'),
                                  ('mariadb', dict(MARIA, wsrep_node_name='node'), 'wsrep_node_name')]:
            for value in (None, '', "bad'name", 'bad\\name', 'bad\nname'):
                with self.subTest(engine=engine, key=key, value=value):
                    writer = Writer()
                    plan = self.plan(engine, dict(base, **{key: value}))
                    out = reviewed_writes.execute(writer, plan, lambda: copy.deepcopy(plan), 'test', audit=self.audit())
                    self.assertEqual(out['status'], 'refused')
                    self.assertEqual(writer.writes, [])
        for key in ('server_id', 'port'):
            for value in (None, True, 1.5, '-1', "1'", '1\\0', '1 OR 1', '１'):
                with self.subTest(key=key, value=value):
                    with self.assertRaises(ValueError):
                        dbops.target_session_guard('mariadb', dict(MARIA, **{key: value}))

    def test_review_metadata_is_derived_and_audit_keeps_guard(self):
        plan = self.plan()
        plan['session_guard'] = ['DROP USER root;']
        details = reviewed_writes.review_details(plan)
        self.assertEqual(plan['session_guard'], [MYSQL_GUARD])
        self.assertEqual(details['session_guard'], [MYSQL_GUARD])
        writer = Writer()
        out = reviewed_writes.execute(writer, plan, lambda: copy.deepcopy(plan), 'test', audit=self.audit())
        self.assertEqual(writer.writes, ['\n'.join([MYSQL_GUARD] + plan['statements'])])
        self.assertEqual(out['sent'], plan['statements'])
        self.assertEqual(self.records()[0]['statements'], [MYSQL_GUARD] + plan['statements'])

    def test_guard_refusal_is_audited_and_never_reads_after(self):
        for echo in ('', 'mysql client exited 1: --------------\nSET @myflames_target = (...)\n--------------\n'):
            with self.subTest(echo=bool(echo)):
                plan = self.plan()
                writer = Writer(echo + MISMATCH)
                after = mock.Mock()
                progress = reviewed_writes.Progress(2)
                out = reviewed_writes.execute(writer, plan, lambda: copy.deepcopy(plan), 'test', audit=self.audit(),
                                               read_after=after, progress=progress)
                self.assert_mismatch(out, writer)
                self.assertEqual(progress.snapshot()['states'], ['not_sent', 'not_sent'])
                after.assert_not_called()

    def test_write_login_consent_refusal_completes_one_pending_audit(self):
        for error in (reviewed_writes.Refusal('Group consent changed. Nothing was sent.', 'changed'),
                      ProfileReadError('This saved connection is outside the current connection group.')):
            with self.subTest(refusal=type(error).__name__):
                plan, writer, audit = self.plan(), Writer(), self.audit()
                after = mock.Mock()
                progress = reviewed_writes.Progress(2)
                def refuse_login(sql, **kwargs):
                    self.assertTrue(audit.pending)
                    raise error
                writer.run_write = refuse_login
                before_count = len(self.records())
                out = reviewed_writes.execute(writer, plan, lambda: copy.deepcopy(plan), 'test', audit=audit,
                                               read_after=after, progress=progress)
                self.assertEqual((out['status'], out['outcome'], out['sent']), ('refused', 'changed', []))
                self.assertEqual(out['not_sent'], plan['statements'])
                self.assertEqual(out['states'], ['not_sent', 'not_sent'])
                self.assertEqual(progress.snapshot()['phase'], 'finished')
                self.assertEqual(progress.snapshot()['status'], 'refused')
                records = self.records()
                self.assertEqual(len(records), before_count + 1)
                record = next(r for r in records if r['id'] == out['audit_id'])
                self.assertEqual((record['status'], record['outcome']), ('refused', 'changed'))
                self.assertEqual(record['target'], plan['expect'])
                self.assertEqual(record['statements'], [MYSQL_GUARD] + plan['statements'])
                self.assertEqual(writer.writes, [])
                after.assert_not_called()

    def test_error_at_line_three_names_second_user_statement(self):
        plan = self.plan()
        writer = Writer('mysql client exited 1: ERROR 1227 (42000) at line 3: denied')
        out = reviewed_writes.execute(writer, plan, lambda: copy.deepcopy(plan), 'test', audit=self.audit())
        self.assertEqual((out['status'], out['states'], out['sent']),
                         ('partial', ['done', 'failed'], plan['statements'][:1]))
        self.assertIn(plan['statements'][1], out['message'])
        self.assertEqual(str(dbops.shift_guard_error(ConnectorError(writer.error))),
                         'mysql client exited 1: ERROR 1227 (42000) at line 2: denied')

    def test_guard_transport_error_stays_unknown(self):
        plan = self.plan()
        out = reviewed_writes.execute(Writer('ERROR 2013 (HY000) at line 1: Lost connection'), plan,
                                       lambda: copy.deepcopy(plan), 'test', audit=self.audit())
        self.assertEqual((out['status'], out['states']), ('unknown', ['unknown', 'unknown']))

    def test_real_connector_sends_guard_on_stdin_without_force_or_reconnect(self):
        plan = self.plan()
        reply = subprocess.CompletedProcess([], 1, stdout=b'', stderr=MISMATCH.encode('utf-8'))
        with MySQLConnection('fixture-db', binary='mysql') as conn, \
                mock.patch.object(conn, '_run_client', return_value=reply) as client:
            out = reviewed_writes.execute(conn, plan, lambda: copy.deepcopy(plan), 'test', audit=self.audit())
        self.assertEqual((out['status'], out['outcome'], out['sent']), ('refused', 'identity_mismatch', []))
        self.assertEqual(client.call_args[1]['input'], '\n'.join([MYSQL_GUARD] + plan['statements']).encode('utf-8'))
        argv = client.call_args[0][0]
        self.assertIn('--skip-reconnect', argv)
        self.assertNotIn('--force', argv)
        self.assertNotIn('-e', argv)

    def test_only_an_error_line_can_identify_guard_failure(self):
        echo = ("mysql client exited 1: --------------\nALTER USER x IDENTIFIED BY "
                "'ERROR 1242 (21000) at line 1: fake'\n--------------\nERROR 1064 (42000) at line 4: syntax")
        self.assertFalse(dbops.target_guard_failed(ConnectorError(echo)))
        self.assertIn('ERROR 1064 (42000) at line 3:', str(dbops.shift_guard_error(ConnectorError(echo))))

    def test_other_protocols_keep_their_own_send_contract(self):
        for expect, via in [({'proxysql_version': '3.0'}, None), ({'readyset_version': '1'}, None),
                            ({'engine': 'mysql', 'server': MYSQL}, 'mysqlsh')]:
            plan = self.plan()
            plan['expect'], plan['via'] = expect, via
            writer = Writer()
            out = reviewed_writes.execute(writer, plan, lambda: copy.deepcopy(plan), 'test')
            self.assertEqual(out['status'], 'done')
            self.assertEqual(writer.writes, ['\n'.join(plan['statements'])])

    def test_request_cannot_select_shell_to_suppress_a_sql_guard(self):
        fresh = self.plan()
        reviewed = dict(fresh, via='mysqlsh')
        writer = Writer()
        out = reviewed_writes.execute(writer, reviewed, lambda: fresh, 'test', audit=self.audit())
        self.assertEqual((out['status'], out['sent'], writer.writes), ('refused', [], []))

    def test_variables_guard_each_engine_and_refuse_swapped_server(self):
        from test_variables import mysql_variable, mariadb_variable
        for value in (mysql_variable('max_connections'), mariadb_variable('max_connections')):
            with self.subTest(engine=value['engine']):
                plan = variables.build_plan(value, '200', 'global')
                writer = Writer(MISMATCH)
                with mock.patch.object(variables, 'read_variable', return_value=value):
                    out = variables.apply_change(writer, plan, plan['expect'], confirmed=True, audit=self.audit())
                self.assert_mismatch(out, writer)
                self.assertEqual(writer.writes[0].split('\n', 1)[0], plan['session_guard'][0])

    def test_accounts_guard_each_engine_and_refuse_swapped_server(self):
        from test_accounts import FakeConn, actor
        for engine, expected in (('mysql', MYSQL_GUARD), ('mariadb', MARIA_GUARD)):
            with self.subTest(engine=engine):
                conn = FakeConn(engine)
                writer = Writer(MISMATCH)
                conn.run_stdin = writer.run_stdin
                with mock.patch.object(privileges, 'read_privileges', return_value=actor(engine)):
                    plan = accounts.plan_lock(conn, 'app')
                    out = accounts.apply(conn, plan, plan['expect'], confirmed=True, store=self.store)
                self.assert_mismatch(out, writer)
                self.assertEqual(writer.writes[0].split('\n', 1)[0], expected)

    def test_accounts_guard_refusal_keeps_the_actual_server_error_code(self):
        from test_accounts import FakeConn, actor
        conn = FakeConn()
        writer = Writer('ERROR 1193 (HY000) at line 1: Unknown system variable')
        conn.run_stdin = writer.run_stdin
        with mock.patch.object(privileges, 'read_privileges', return_value=actor()):
            plan = accounts.plan_lock(conn, 'app')
            out = accounts.apply(conn, plan, plan['expect'], confirmed=True, store=self.store)
        self.assertEqual((out['status'], out['sent']), ('refused', []))
        self.assertEqual(self.records()[0]['error_code'], 1193)

    def test_accounts_guard_each_invocation_and_keep_earlier_successes(self):
        from test_accounts import FakeConn, actor
        conn = FakeConn('mariadb', exists=False)
        writes = []

        def send(sql, **kwargs):
            writes.append(sql)
            if len(writes) == 2:
                raise ConnectorError(MISMATCH)
            return ''

        conn.run_stdin = send
        with mock.patch.object(privileges, 'read_privileges', return_value=actor('mariadb')):
            plan = accounts.plan_create(conn, 'app', password='fixture-secret')
            out = accounts.apply(conn, plan, plan['expect'], confirmed=True, store=self.store)
        self.assertEqual((out['status'], out['outcome'], len(out['sent']), len(writes)),
                         ('partial', 'identity_mismatch', 1, 2))
        self.assertTrue(all(sql.startswith(MARIA_GUARD + '\n') for sql in writes))
        self.assertEqual([row['status'] for row in out['statements']], ['ok', 'not_sent', 'not_sent'])
        self.assertEqual(sorted(row['status'] for row in self.records()), ['ok', 'refused'])

    def test_replication_single_and_loop_guard_each_engine(self):
        from test_replication import status
        from _perf_replay import FakeTime
        for name in ('mysql-8.4-gtid-error', 'mariadb-11.4-error'):
            report, _ = status(name)
            channel = report['channels'][0]['name']
            plan = replication.plan_skip(report, channel)
            writer = Writer(MISMATCH)
            with mock.patch.object(replication, 'read_status', return_value=report), \
                    mock.patch.object(privileges, 'read_privileges',
                                      return_value=privileges.parse_grants([], checked=False)):
                out = replication.skip_one(writer, channel, plan['identity'], confirmed=True, audit=self.audit())
                self.assert_mismatch(out, writer)
                self.assertEqual(writer.writes[0].split('\n', 1)[0], plan['session_guard'][0])
                writer.writes[:] = []
                clock = FakeTime()
                loop = replication.SkipLoop(writer, channel, [1062], 1, audit_factory=self.audit,
                                             receiver_reader=lambda *args: ((1, 'version', 1),),
                                             clock=clock.monotonic, sleep=clock.sleep, settle_s=0)
                final = loop.run()
                self.assertEqual((final['skips_done'], len(writer.writes)), (0, 1))
                self.assertIn('not the one you reviewed', final['message'])

    def test_locks_and_sessions_share_the_guarded_kill_sender(self):
        from test_locks import KillScenario, VERSION_SQL, bound_kill_expect, batch
        for engine in ('mysql', 'mariadb'):
            scenario = KillScenario()
            if engine == 'mariadb':
                scenario.conn.responses[0] = (VERSION_SQL, batch([['11.4.13-MariaDB', 1]]))
            expect = bound_kill_expect(scenario.conn)
            writer = Writer(MISMATCH)
            scenario.conn.run_write = writer.run_write
            out = locks.kill_session(scenario.conn, 41, expect=expect, confirmed=True, audit=self.audit(), settle_s=0)
            self.assert_mismatch(out, writer)
            self.assertEqual(writer.writes[0].split('\n', 1)[0],
                             dbops.target_session_guard(engine, expect['server_identity'])[0])
            self.assertEqual(locks.format_kill(out).strip(), dbops.TARGET_IDENTITY_MISMATCH)

    def test_cluster_sql_and_udf_refuse_swapped_server(self):
        import _cluster_fixtures as fixtures
        report = fixtures.report('gr-8.4-two-members')
        local = next(member for member in report['members'] if member['local'])
        for action in ('stop_member', 'switch_to_multi_primary'):
            plan = cluster.plan_action(report, action, local['key'])
            writer = Writer(MISMATCH)
            with mock.patch.object(cluster, 'read_status', return_value=report):
                out = cluster.execute_action(writer, plan, audit=self.audit(), typed=plan.get('typed_guard'))
            self.assert_mismatch(out, writer)
            self.assertIn(report['server']['server_uuid'], writer.writes[0].split('\n', 1)[0])

    def test_cluster_missing_reviewed_mariadb_id_is_refused(self):
        # An older reviewed plan without server_id must be refused, never
        # supplemented from the fresh preflight identity at send.
        import _cluster_fixtures as fixtures
        report = fixtures.report('galera-11.8-healthy')
        local = next(member for member in report['members'] if member['local'])
        plan = cluster.plan_action(report, 'desync_node', local['key'])
        plan['expect']['server'].pop('server_id')
        with self.assertRaisesRegex(ValueError, 'server_id'):
            reviewed_writes.session_guard(plan)
        writer = Writer()
        with mock.patch.object(cluster, 'read_status', return_value=report):
            out = cluster.execute_action(writer, plan, audit=self.audit())
        self.assertEqual((out['status'], out['sent'], writer.writes), ('refused', [], []))
        self.assertEqual(out['outcome'], 'changed')

    def test_cluster_mariadb_uses_only_complete_reviewed_node_identity(self):
        import _cluster_fixtures as fixtures
        report = fixtures.report('galera-11.8-healthy')
        local = next(member for member in report['members'] if member['local'])
        plan = cluster.plan_action(report, 'desync_node', local['key'])
        # The fixture's preflight supplies server_id=1 outside the public
        # report; the reviewed plan must retain that complete read identity.
        self.assertNotIn('server_id', report['server'])
        self.assertEqual(plan['expect']['server']['server_id'], 1)
        writer = Writer(MISMATCH)
        with mock.patch.object(cluster, 'read_status', return_value=report):
            out = cluster.execute_action(writer, plan, audit=self.audit())
        self.assert_mismatch(out, writer)
        line = writer.writes[0].split('\n', 1)[0]
        self.assertIn("@@global.hostname <> 'mf-galera-1'", line)
        self.assertIn('@@global.server_id <> 1', line)
        self.assertIn('@@global.port <> 3306', line)
        self.assertIn("@@global.wsrep_node_name <> 'galera-1'", line)


if __name__ == '__main__':
    unittest.main()
