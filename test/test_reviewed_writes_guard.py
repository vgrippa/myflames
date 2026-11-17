"""Same-packet identity binding: real policy/audit code, fake write clients.

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
from _reviewed_replay import guarded_packets

UUID = '11111111-2222-3333-4444-555555555555'
MYSQL = {'server_uuid': UUID}
MARIA = {'server_id': '42', 'hostname': 'test-server', 'port': '3306'}
MYSQL_GUARD = ("SET @myflames_target = (SELECT 1 FROM (SELECT 1 UNION ALL SELECT 1) AS g "
               "WHERE @@global.server_uuid <> '11111111-2222-3333-4444-555555555555');")
MARIA_GUARD = ("SET @myflames_target = (SELECT 1 FROM (SELECT 1 UNION ALL SELECT 1) AS g "
               "WHERE @@global.server_id <> 42 OR @@global.hostname <> 'test-server' OR @@global.port <> 3306);")
MISMATCH = 'ERROR 1242 (21000) at line 2: Subquery returns more than 1 row'


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
        self.assertEqual(record['statements'][0], guarded_packets(writer.writes[0])[0][0])

    def test_mysql_family_and_mariadb_prefixes_are_exact(self):
        for engine, identity, expected in [('mysql', MYSQL, MYSQL_GUARD), ('percona', MYSQL, MYSQL_GUARD),
                                            ('mariadb', MARIA, MARIA_GUARD)]:
            with self.subTest(engine=engine):
                self.assertEqual(dbops.target_session_guard(engine, identity), [expected])
        node = dict(MARIA, wsrep_node_name='db-1.example')
        self.assertEqual(dbops.target_session_guard('mariadb', node),
                         [MARIA_GUARD[:-2] + " OR @@global.wsrep_node_name <> 'db-1.example');"])

    def test_galera_and_pxc_guard_the_node_with_the_engine_status_table(self):
        for engine, identity, table in [
                ('mariadb', dict(MARIA, wsrep_gcomm_uuid=UUID), 'information_schema.GLOBAL_STATUS'),
                ('pxc', dict(MYSQL, wsrep_gcomm_uuid=UUID), 'performance_schema.global_status'),
                ('mysql', dict(MYSQL, wsrep_gcomm_uuid=UUID), 'performance_schema.global_status')]:
            with self.subTest(engine=engine):
                guard = dbops.target_session_guard(engine, identity)[0]
                self.assertIn("OR NOT ((SELECT VARIABLE_VALUE FROM %s WHERE VARIABLE_NAME = 'wsrep_gcomm_uuid') "
                              "<=> '%s')" % (table, UUID), guard)
                for value in (None, '', 'not-a-uuid', "x'); SET GLOBAL wsrep_desync=ON; --"):
                    with self.assertRaises(ValueError):
                        dbops.target_session_guard(engine, dict(identity, wsrep_gcomm_uuid=value))
        for engine in ('mysql', 'mariadb'):
            plan = self.plan(engine, MYSQL if engine == 'mysql' else MARIA)
            plan['expect']['kind'] = 'galera'
            with self.assertRaisesRegex(ValueError, 'Galera node UUID'):
                reviewed_writes.review_details(plan)

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
        self.assertEqual([(g, stmt) for g, stmt, _ in guarded_packets(writer.writes[0])],
                         [(MYSQL_GUARD, stmt) for stmt in plan['statements']])
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

    def test_packet_start_line_names_second_user_statement(self):
        plan = self.plan()
        writer = Writer('mysql client exited 1: ERROR 1227 (42000) at line 4: denied')
        out = reviewed_writes.execute(writer, plan, lambda: copy.deepcopy(plan), 'test', audit=self.audit())
        self.assertEqual((out['status'], out['states'], out['sent']),
                         ('partial', ['done', 'failed'], plan['statements'][:1]))
        self.assertIn(plan['statements'][1], out['message'])
        self.assertEqual(str(dbops.shift_guard_error(ConnectorError(writer.error), (2, 4))),
                         'mysql client exited 1: ERROR 1227 (42000) at line 2: denied')

    def test_guard_transport_error_stays_unknown(self):
        plan = self.plan()
        out = reviewed_writes.execute(Writer('ERROR 2013 (HY000) at line 1: Lost connection'), plan,
                                       lambda: copy.deepcopy(plan), 'test', audit=self.audit())
        self.assertEqual((out['status'], out['states']), ('unknown', ['unknown', 'unknown']))

    def test_mysql_client_replyless_errors_never_read_after_or_report_partial(self):
        for error in ('ERROR 2013 (HY000) at line 1: Lost connection',
                      'mysql client timed out', 'mysql client exited without an error number'):
            with self.subTest(error=error):
                plan, writer = self.plan(), Writer(error)
                after = mock.Mock(return_value={'changed': True})
                effect = mock.Mock(return_value=[{'what': 'mode', 'before': 'single', 'now': 'multi'}])
                progress = reviewed_writes.Progress(2)
                out = reviewed_writes.execute(writer, plan, lambda: copy.deepcopy(plan), 'test', audit=self.audit(),
                                               read_after=after, effect=effect, verify_on_error=True, progress=progress)
                self.assertEqual((out['status'], out['states']), ('unknown', ['unknown', 'unknown']))
                self.assertIsNone(out['after'])
                self.assertIsNone(out['effect'])
                self.assertEqual(len(writer.writes), 1)
                after.assert_not_called()
                effect.assert_not_called()
                self.assertEqual(progress.snapshot()['status'], 'unknown')
                record = next(r for r in self.records() if r['id'] == out['audit_id'])
                self.assertEqual((record['status'], record['outcome']),
                                 ('unknown', 'timeout' if 'timed out' in error else 'no_reply'))

    def test_shell_replyless_errors_report_only_visible_partial_changes(self):
        for error in ('ERROR 2013 (HY000): Lost connection', 'RuntimeError: AdminAPI operation failed'):
            for observed in ('changed', 'unchanged', 'unreadable'):
                with self.subTest(error=error, observed=observed):
                    plan = self.plan()
                    plan.update(via='mysqlsh', statements=['dba.getCluster().switchToMultiPrimaryMode();'])
                    writer, progress = Writer(error), reviewed_writes.Progress(1)
                    after = mock.Mock(return_value={'changed': observed == 'changed'})
                    if observed == 'unreadable':
                        after.side_effect = ConnectorError('Cannot read the cluster')
                    effect = lambda fresh, current: [{'what': 'mode', 'before': 'single', 'now': 'multi'}] \
                        if current['changed'] else []
                    out = reviewed_writes.execute(writer, plan, lambda: copy.deepcopy(plan), 'test',
                                                   audit=self.audit(), read_after=after, effect=effect,
                                                   verify_on_error=True, progress=progress)
                    expected = 'partial' if observed == 'changed' else 'unknown'
                    self.assertEqual((out['status'], out['states'], out['sent']), (expected, ['unknown'], []))
                    self.assertEqual(len(writer.writes), 1)
                    after.assert_called_once_with()
                    self.assertEqual(progress.snapshot()['status'], expected)
                    record = next(r for r in self.records() if r['id'] == out['audit_id'])
                    self.assertEqual((record['status'], record['outcome']),
                                     ('error', 'partial') if expected == 'partial' else ('unknown', 'no_reply'))

    def test_non_mismatch_packet_errors_are_statement_failures(self):
        for code, message in [(1142, "SELECT command denied to user 'monitor' for table 'global_status'"),
                              (1146, "Table 'performance_schema.global_status' doesn't exist")]:
            with self.subTest(code=code):
                plan = self.plan()
                error = 'ERROR %d (42000) at line 2: %s' % (code, message)
                after = mock.Mock()
                out = reviewed_writes.execute(Writer(error), plan, lambda: copy.deepcopy(plan), 'test',
                                               audit=self.audit(), read_after=after)
                self.assertEqual((out['status'], out['sent']), ('failed', []))
                self.assertNotIn('outcome', out)
                self.assertEqual(out['states'], ['failed', 'not_run'])
                self.assertIn(message, out['message'])
                self.assertNotIn('not the one you reviewed', out['message'])
                after.assert_not_called()
                record = next(r for r in self.records() if r['id'] == out['audit_id'])
                self.assertEqual((record['status'], record['outcome'], record['error_code']),
                                 ('error', 'refused_by_server', code))

    def test_galera_null_reviewed_node_identity_is_unavailable_before_sending(self):
        for engine, identity in [('mysql', MYSQL), ('pxc', MYSQL), ('mariadb', MARIA)]:
            with self.subTest(engine=engine):
                plan = self.plan(engine, dict(identity, wsrep_gcomm_uuid=None))
                plan['expect']['kind'] = 'pxc' if engine == 'pxc' else 'galera'
                writer, after = Writer(), mock.Mock()
                out = reviewed_writes.execute(writer, plan, lambda: copy.deepcopy(plan), 'test',
                                               audit=self.audit(), read_after=after)
                self.assertEqual((out['status'], out['outcome'], writer.writes),
                                 ('refused', 'identity_unavailable', []))
                self.assertIn('Galera node UUID', out['message'])
                self.assertNotIn('enable performance_schema', out['message'])
                after.assert_not_called()

    def test_packet_error_redacts_password_and_ignores_sql_echo(self):
        plan = self.plan()
        secret = 'fixture-guard-secret'
        error = ("mysql client exited 1: --------------\nALTER USER app IDENTIFIED BY '%s';\n--------------\n"
                 "ERROR 1142 (42000) at line 2: SELECT denied for '%s'" % (secret, secret))
        writer = Writer(error)
        writer.password = secret
        out = reviewed_writes.execute(writer, plan, lambda: copy.deepcopy(plan), 'test', audit=self.audit())
        self.assertEqual(out['status'], 'failed')
        self.assertNotIn('outcome', out)
        self.assertIn('SELECT denied', out['message'])
        self.assertNotIn(secret, repr(out))
        self.assertNotIn('ALTER USER', out['message'])
        self.assertNotIn(secret, repr(self.records()))

    def test_real_connector_sends_guard_on_stdin_without_force_or_reconnect(self):
        plan = self.plan()
        reply = subprocess.CompletedProcess([], 1, stdout=b'', stderr=MISMATCH.encode('utf-8'))
        with MySQLConnection('fixture-db', binary='mysql') as conn, \
                mock.patch.object(conn, '_run_client', return_value=reply) as client:
            out = reviewed_writes.execute(conn, plan, lambda: copy.deepcopy(plan), 'test', audit=self.audit())
        self.assertEqual((out['status'], out['outcome'], out['sent']), ('refused', 'identity_mismatch', []))
        self.assertEqual(client.call_args[1]['input'], dbops.target_guarded_script([MYSQL_GUARD], plan['statements'])[0].encode('utf-8'))
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
        for expect, via in [({'proxysql_version': '3.0'}, None), ({'readyset_version': '1'}, None)]:
            plan = self.plan()
            plan['expect'], plan['via'] = expect, via
            writer = Writer()
            out = reviewed_writes.execute(writer, plan, lambda: copy.deepcopy(plan), 'test')
            self.assertEqual(out['status'], 'done')
            self.assertEqual(writer.writes, ['\n'.join(plan['statements'])])

    def test_shell_guard_is_javascript_derived_from_the_reviewed_uuid(self):
        plan = self.plan()
        plan.update(via='mysqlsh', statements=['dba.getCluster().switchToMultiPrimaryMode();'],
                    session_guard=['dba.getCluster().dissolve();'])
        writer = Writer()
        details = reviewed_writes.review_details(plan)
        guard = details['session_guard']
        self.assertEqual(guard, ['if (session.runSql("SELECT @@global.server_uuid").fetchOne()[0] !== "%s") '
                                '{ throw new Error("MYFLAMES_IDENTITY_MISMATCH"); }' % UUID])
        out = reviewed_writes.execute(writer, plan, lambda: copy.deepcopy(plan), 'test', audit=self.audit())
        self.assertEqual(out['status'], 'done')
        self.assertEqual(writer.writes, ['\n'.join(guard + plan['statements'])])
        self.assertEqual(self.records()[0]['statements'], guard + plan['statements'])
        bad = dict(plan, expect={'engine': 'mysql', 'server': {'server_uuid': "bad'uuid"}})
        with self.assertRaises(ValueError):
            reviewed_writes.review_details(bad)
        for identity in ('not-a-dict', [UUID], None):
            bad = dict(plan, expect={'engine': 'mysql', 'server': identity})
            with self.assertRaises(ValueError):
                reviewed_writes.review_details(bad)

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
                self.assertEqual(guarded_packets(writer.writes[0])[0][0], plan['session_guard'][0])

    def test_variables_packet_error_is_denied_without_reading_after(self):
        from test_variables import mysql_variable, mariadb_variable
        for value in (mysql_variable('max_connections'), mariadb_variable('max_connections')):
            with self.subTest(engine=value['engine']):
                plan = variables.build_plan(value, '200', 'global')
                writer = Writer('ERROR 1142 (42000) at line 2: SELECT denied for fixture-secret')
                writer.password = 'fixture-secret'
                with mock.patch.object(variables, 'read_variable', return_value=value), \
                        mock.patch.object(variables, '_after') as after:
                    out = variables.apply_change(writer, plan, plan['expect'], confirmed=True, audit=self.audit())
                self.assert_packet_error(out, writer, 1142, 'denied')
                after.assert_not_called()

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
                self.assertEqual(guarded_packets(writer.writes[0])[0][0], expected)

    def test_accounts_packet_refusal_keeps_the_actual_server_error_code(self):
        from test_accounts import FakeConn, actor
        conn = FakeConn()
        writer = Writer('ERROR 1193 (HY000) at line 2: Unknown system variable')
        conn.run_stdin = writer.run_stdin
        with mock.patch.object(privileges, 'read_privileges', return_value=actor()):
            plan = accounts.plan_lock(conn, 'app')
            out = accounts.apply(conn, plan, plan['expect'], confirmed=True, store=self.store)
        self.assert_packet_error(out, writer, 1193, 'refused_by_server')

    def test_accounts_packet_error_redacts_echo_and_credentials_before_normal_readback(self):
        from test_accounts import FakeConn, actor
        for engine in ('mysql', 'mariadb'):
            with self.subTest(engine=engine):
                conn, calls_at_send = FakeConn(engine), []
                conn.password = 'fixture-secret'
                error = ("mysql client exited 1: --------------\nALTER USER app IDENTIFIED BY 'new-secret';\n"
                         "--------------\nERROR 1142 (42000) at line 2: SELECT denied for fixture-secret and new-secret")
                writer = Writer(error)
                def send(sql, **kwargs):
                    calls_at_send.extend(conn.calls)
                    return writer.run_stdin(sql, **kwargs)
                conn.run_stdin = send
                with mock.patch.object(privileges, 'read_privileges', return_value=actor(engine)):
                    plan = accounts.plan_password(conn, 'app', password='new-secret')
                    out = accounts.apply(conn, plan, plan['expect'], confirmed=True, store=self.store)
                self.assert_packet_error(out, writer, 1142, 'denied')
                self.assertGreater(len(conn.calls), len(calls_at_send))
                self.assertNotIn('new-secret', repr(out))
                self.assertNotIn('new-secret', repr(self.records()))
                self.assertNotIn('ALTER USER', out['message'])

    def test_accounts_guard_each_invocation_and_keep_earlier_successes(self):
        from test_accounts import FakeConn, actor
        for error, outcome in ((MISMATCH, 'identity_mismatch'),
                               ('ERROR 1142 (42000) at line 2: SELECT denied', 'denied')):
            with self.subTest(outcome=outcome):
                conn, writes = FakeConn('mariadb', exists=False), []
                def send(sql, **kwargs):
                    writes.append(sql)
                    if len(writes) == 2:
                        raise ConnectorError(error)
                    return ''
                conn.run_stdin = send
                with mock.patch.object(privileges, 'read_privileges', return_value=actor('mariadb')):
                    plan = accounts.plan_create(conn, 'app', password='fixture-secret')
                    out = accounts.apply(conn, plan, plan['expect'], confirmed=True, store=self.store)
                self.assertEqual((out['status'], len(out['sent']), len(writes)), ('partial', 1, 2))
                if outcome == 'identity_mismatch':
                    self.assertEqual(out['outcome'], outcome)
                self.assertTrue(all(guarded_packets(sql)[0][0] == MARIA_GUARD for sql in writes))
                self.assertEqual([row['status'] for row in out['statements']], ['ok', 'not_sent' if outcome == 'identity_mismatch' else 'denied', 'not_sent'])
                records = [r for r in self.records() if r['id'] in out['audit_ids']]
                self.assertEqual(sorted(row['status'] for row in records), ['ok', 'refused'] if outcome == 'identity_mismatch' else ['error', 'ok'])
                refused = next(r for r in records if r['status'] != 'ok')
                self.assertEqual(refused['outcome'], outcome)
                self.assertNotIn('Nothing was sent', out['message'])
                if outcome == 'identity_mismatch':
                    self.assertIn('Earlier statements ran', out['message'])

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
                self.assertEqual(guarded_packets(writer.writes[0])[0][0], plan['session_guard'][0])
                writer.writes[:] = []
                clock = FakeTime()
                loop = replication.SkipLoop(writer, channel, [1062], 1, audit_factory=self.audit,
                                             receiver_reader=lambda *args: ((1, 'version', 1),),
                                             clock=clock.monotonic, sleep=clock.sleep, settle_s=0)
                final = loop.run()
                self.assertEqual((final['skips_done'], len(writer.writes)), (0, 1))
                self.assertIn('not the one you reviewed', final['message'])

    def test_replication_packet_error_reports_denial_and_reads_after(self):
        from test_replication import status
        for name in ('mysql-8.4-gtid-error', 'mariadb-11.4-error'):
            with self.subTest(server=name):
                report, _ = status(name)
                channel = report['channels'][0]['name']
                plan = replication.plan_skip(report, channel)
                writer = Writer('ERROR 1142 (42000) at line 2: SELECT denied for fixture-secret')
                writer.password = 'fixture-secret'
                reads_at_send = []
                original_write = writer.run_write
                with mock.patch.object(replication, 'read_status', return_value=report) as reads, \
                        mock.patch.object(privileges, 'read_privileges',
                                          return_value=privileges.parse_grants([], checked=False)):
                    def send(sql, **kwargs):
                        reads_at_send.extend(reads.call_args_list)
                        return original_write(sql, **kwargs)
                    writer.run_write = send
                    out = replication.skip_one(writer, channel, plan['identity'], confirmed=True,
                                               audit=self.audit())
                self.assert_packet_error(out, writer, 1142, 'denied')
                self.assertNotIn('stop_reason', out)
                self.assertTrue(reads_at_send)
                self.assertGreater(len(reads.call_args_list), len(reads_at_send))

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
            self.assertEqual(guarded_packets(writer.writes[0])[0][0],
                             dbops.target_session_guard(engine, expect['server_identity'])[0])
            self.assertEqual(locks.format_kill(out).strip(), dbops.TARGET_IDENTITY_MISMATCH)

    def test_locks_non_mismatch_packet_error_uses_existing_failure_contract(self):
        from test_locks import KillScenario, bound_kill_expect
        scenario = KillScenario()
        expect = bound_kill_expect(scenario.conn)
        scenario.conn.run_write = Writer('ERROR 1142 (42000) at line 2: SELECT denied').run_write
        with self.assertRaisesRegex(ValueError, 'Could not end connection'):
            locks.kill_session(scenario.conn, 41, expect=expect, confirmed=True,
                               audit=self.audit(), settle_s=0)
        self.assertFalse(scenario.killed)
        self.assertEqual((self.records()[0]['status'], self.records()[0]['error_code']), ('error', 1142))

    def assert_packet_error(self, out, writer, code, status):
        self.assertEqual(out['status'], status)
        self.assertNotIn(out.get('outcome'), ('identity_mismatch', 'identity_unavailable'))
        self.assertEqual(out['sent'], True if 'channel' in out else [])
        self.assertNotIn('not the one you reviewed', out['message'])
        self.assertNotIn('fixture-secret', repr(out))
        self.assertEqual(len(writer.writes), 1)
        audit_id = out.get('audit_id') or out['audit_ids'][-1]
        record = next(r for r in self.records() if r['id'] == audit_id)
        self.assertEqual((record['status'], record['outcome'], record['error_code']), ('error', status, code))
        self.assertNotIn('fixture-secret', repr(record))

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
            self.assertIn(report['server']['server_uuid'], guarded_packets(writer.writes[0])[0][0])

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
        line = guarded_packets(writer.writes[0])[0][0]
        self.assertIn("@@global.hostname <> 'mf-galera-1'", line)
        self.assertIn('@@global.server_id <> 1', line)
        self.assertIn('@@global.port <> 3306', line)
        self.assertIn("@@global.wsrep_node_name <> 'galera-1'", line)


if __name__ == '__main__':
    unittest.main()
