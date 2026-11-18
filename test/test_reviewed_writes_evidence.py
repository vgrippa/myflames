"""Abuse cases for node-bound review evidence and ambiguous write results.

Only the transport is synthetic. The packet builder, identity checks,
statement parser, reviewed senders, policy and audits are production code.
"""
import copy
import unittest
from unittest import mock

from myflames import accounts, dbops, locks, privileges, replication, reviewed_writes, variables
from myflames.connector import ConnectorError
from _reviewed_replay import evidence_body, evidence_output
import test_reviewed_writes_guard as guard_fixtures
from test_reviewed_writes_guard import UUID, MYSQL, Writer

OTHER_UUID = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
PROVIDER = '/usr/lib/galera/libgalera_smm.so'


class EvidenceRouter(object):
    """Choose the next node for every COM_QUERY, not for every client login.

    All statements between the chosen DELIMITER are one packet. The captured
    read output is served by that node alongside its own identity suffix.
    """
    def __init__(self, reader, nodes):
        self.reader, self.nodes, self.packets, self.writes = reader, nodes, [], []

    def __getattr__(self, name):
        return getattr(self.reader, name)

    def run(self, sql, extra_flags=None, **kwargs):
        body, marker = evidence_body(sql)
        if marker is None:
            raise AssertionError('evidence was sent as separate client packets: ' + sql)
        delimiter = sql.split('\n', 1)[0].split(' ', 1)[1]
        self.assert_single_packet(sql, delimiter)
        node = self.nodes[len(self.packets) % len(self.nodes)]
        self.packets.append((copy.deepcopy(node), body))
        if 'myflames_who' in body:
            output = ('myflames_who\tadmin@localhost\tadmin@localhost\tNULL\t11.4.13-MariaDB\n'
                      'GRANT ALL PRIVILEGES ON *.* TO `admin`@`localhost` WITH GRANT OPTION\n')
            output += ''.join('%s\t%s\n' % pair for pair in node.items() if pair[0] != 'wsrep_gcomm_uuid')
        else:
            output = self.reader.run(body, extra_flags=['--comments'] +
                                     (['--xml'] if '--xml' in (extra_flags or []) else []), **kwargs)
        return evidence_output(output, marker, node, '--xml' in (extra_flags or []))

    @staticmethod
    def assert_single_packet(sql, delimiter):
        assert sql.split('\n', 1)[1].count(delimiter) == 1

    def run_write(self, sql, **kwargs):
        self.writes.append(sql)
        raise AssertionError('a refused review sent a write')

    run_stdin = run_write


class EvidenceTests(unittest.TestCase):
    setUp = guard_fixtures.GuardTests.setUp
    audit = guard_fixtures.GuardTests.audit
    records = guard_fixtures.GuardTests.records
    plan = guard_fixtures.GuardTests.plan

    def readers(self, on='OFF', other=True):
        from test_accounts import FakeConn
        from test_variables import replay as variable_replay
        from test_replication import replay as replication_replay
        account = FakeConn('mariadb')
        variable, _ = variable_replay('mariadb-11.4', 'global')
        replica, _ = replication_replay('replication', 'mariadb-11.4-error')
        for name, reader, read, base in (
                ('accounts', account, lambda c: accounts.plan_lock(c, 'app'),
                 {'server_id': '42', 'hostname': 'test-server', 'port': '3306', 'version': account.version}),
                ('variables', variable, lambda c: variables.plan_change(c, 'max_connections', '300', 'global', grants=False),
                 variable.evidence_values),
                ('replication', replica, replication.read_status, replica.evidence_values)):
            first = dict(base, wsrep_on=on, wsrep_provider=PROVIDER, wsrep_gcomm_uuid=UUID)
            second = dict(first, wsrep_gcomm_uuid=OTHER_UUID if other else UUID)
            yield name, EvidenceRouter(reader, [first, second]), read

    def test_rotating_proxy_cannot_combine_review_state_and_another_nodes_uuid(self):
        for on in ('ON', 'OFF'):
            for name, conn, read in self.readers(on):
                with self.subTest(sender=name, wsrep_on=on):
                    with self.assertRaisesRegex(ConnectorError, 'identity check could not run'):
                        read(conn)
                    self.assertGreaterEqual(len(conn.packets), 2)
                    self.assertNotEqual(conn.packets[0][0]['wsrep_gcomm_uuid'], conn.packets[1][0]['wsrep_gcomm_uuid'])
                    self.assertEqual(conn.writes, [])

    def test_loaded_provider_with_wsrep_off_requires_node_identity_for_all_readers(self):
        for name, conn, read in self.readers(other=False):
            with self.subTest(sender=name):
                report = read(conn)
                identity = (report['actor_server'] if name == 'accounts' else
                            report['expect']['server_identity'] if name == 'variables' else
                            report['settings']['server_identity'])
                self.assertEqual(identity['wsrep_on'], 'OFF')
                self.assertEqual(identity['wsrep_gcomm_uuid'], UUID)
                self.assertIn('wsrep_gcomm_uuid', dbops.target_session_guard('mariadb', identity)[0])
        for name, conn, read in self.readers(other=False):
            for node in conn.nodes:
                node.pop('wsrep_gcomm_uuid')
            with self.subTest(sender=name, missing=True), self.assertRaisesRegex(ConnectorError, 'identity check could not run'):
                read(conn)
            self.assertEqual(conn.writes, [])

    def test_identity_failure_caught_by_an_optional_reader_is_still_fatal(self):
        name, conn, _ = next(self.readers())
        evidence = dbops.TargetEvidenceConnection(conn)
        sql = accounts.TAG + "SHOW GLOBAL VARIABLES WHERE Variable_name = 'partial_revokes'"
        evidence.run(sql)
        with self.assertRaises(ConnectorError):
            evidence.run(sql)
        with self.assertRaises(ConnectorError):
            evidence.run_stdin('ALTER USER app ACCOUNT LOCK')
        self.assertEqual(conn.writes, [])

    def test_supplied_actor_cannot_replace_fresh_node_bound_privileges(self):
        from test_accounts import actor
        _, conn, _ = next(self.readers(other=False))
        stale = actor('mariadb', account='other@localhost')
        plan = accounts.plan_change(conn, 'lock', 'app', actor=stale)
        self.assertEqual(plan['actor'], 'admin@localhost')
        self.assertTrue(any('myflames_who' in packet[1] for packet in conn.packets))
        self.assertEqual(plan['actor_server']['wsrep_gcomm_uuid'], UUID)

    def test_transport_exception_poisoning_survives_an_optional_reader(self):
        for failure in (ConnectorError('ERROR 1142: identity suffix denied'), RuntimeError('client failed')):
            _, conn, _ = next(self.readers(other=False))
            evidence = dbops.TargetEvidenceConnection(conn)
            sql = accounts.TAG + "SHOW GLOBAL VARIABLES WHERE Variable_name = 'partial_revokes'"
            evidence.run(sql)
            with mock.patch.object(conn, 'run', side_effect=failure):
                with self.assertRaises(ConnectorError) as caught:
                    evidence.run(sql)
            self.assertEqual(caught.exception.identity_outcome, 'identity_unavailable')
            for action in (lambda: evidence.run(sql), lambda: evidence.run_write('KILL 41'),
                           lambda: evidence.run_stdin('ALTER USER app ACCOUNT LOCK')):
                with self.assertRaises(ConnectorError) as caught:
                    action()
                self.assertEqual(caught.exception.identity_outcome, 'identity_unavailable')
            self.assertEqual(conn.writes, [])

    def test_optional_variable_suffix_failure_cannot_return_a_ready_plan(self):
        from test_variables import replay
        reader, _ = replay('mysql-8.4', 'global')
        original = reader.run
        def failed_suffix(sql, **kwargs):
            if 'performance_schema.variables_info' in sql:
                raise ConnectorError('ERROR 1142: identity suffix denied')
            return original(sql, **kwargs)
        with mock.patch.object(reader, 'run', side_effect=failed_suffix):
            with self.assertRaises(ConnectorError) as caught:
                variables.plan_change(reader, 'max_connections', '300', 'global', grants=False)
        self.assertEqual(caught.exception.identity_outcome, 'identity_unavailable')

    def test_interrupted_packet_cannot_be_reused_after_catching_cancellation(self):
        _, conn, _ = next(self.readers(other=False))
        evidence = dbops.TargetEvidenceConnection(conn)
        with mock.patch.object(conn, 'run', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                evidence.run('SELECT 1')
        with self.assertRaises(ConnectorError) as caught:
            evidence.run_write('KILL 41')
        self.assertEqual(caught.exception.identity_outcome, 'identity_unavailable')
        self.assertEqual(conn.writes, [])

    def test_fresh_wrapper_cannot_substitute_caller_supplied_identity(self):
        writer = Writer()
        evidence = dbops.TargetEvidenceConnection(writer)
        with self.assertRaises(ConnectorError) as caught:
            dbops.target_galera_identity(evidence, 'mysql', MYSQL, dict(MYSQL, wsrep_on='OFF'))
        self.assertEqual(caught.exception.identity_outcome, 'identity_unavailable')
        with self.assertRaises(ConnectorError):
            evidence.run_write('KILL 41')
        self.assertEqual(writer.writes, [])

    def test_ambiguous_internal_read_is_a_sticky_identity_refusal(self):
        writer = Writer()
        evidence = dbops.TargetEvidenceConnection(writer)
        with mock.patch.object(writer, 'run', create=True) as read:
            with self.assertRaises(ConnectorError) as caught:
                evidence.run(r"SELECT 'it\'s'")
        read.assert_not_called()
        self.assertEqual(caught.exception.identity_outcome, 'identity_unavailable')
        with self.assertRaises(ConnectorError):
            evidence.run_write('KILL 41')
        self.assertEqual(writer.writes, [])

    def test_version_helpers_use_bound_metadata_instead_of_transport_caches(self):
        for maria in (False, True):
            values = {'version': '11.4.13-MariaDB' if maria else '8.4.11',
                      'version_comment': 'MariaDB Server' if maria else 'MySQL Community Server',
                      'server_id': '1', 'hostname': 'one', 'port': '3306'}
            if not maria:
                values['server_uuid'] = UUID
            transport = mock.Mock()
            transport.server_version.side_effect = AssertionError('unbound version')
            transport.server_version_comment.side_effect = AssertionError('unbound comment')
            transport.is_mariadb.side_effect = AssertionError('unbound engine')
            def read(sql, **kwargs):
                body, marker = evidence_body(sql)
                self.assertEqual(body, 'SELECT @@version')
                self.assertIsNotNone(marker)
                return evidence_output(values['version'] + '\n', marker, values)
            transport.run.side_effect = read
            evidence = dbops.TargetEvidenceConnection(transport)
            self.assertEqual(evidence.server_version(), values['version'])
            self.assertEqual(evidence.server_version_comment(), values['version_comment'])
            self.assertEqual(evidence.is_mariadb(), maria)
            self.assertEqual(transport.run.call_count, 1)

    def test_kill_transaction_and_xa_packets_refuse_a_different_backend(self):
        from test_locks import KillScenario, bound_kill_expect
        for stage in ('information_schema.INNODB_TRX', 'events_statements_current'):
            for bulk in (False, True):
                with self.subTest(stage=stage, bulk=bulk):
                    scenario = KillScenario()
                    expected = bound_kill_expect(scenario.conn)
                    context = locks._kill_context(scenario.conn) if bulk else None
                    original, packets = scenario.conn.run, []
                    def route(sql, **kwargs):
                        body, marker = evidence_body(sql)
                        if marker is None:
                            return original(sql, **kwargs)
                        node = dict(MYSQL, version='8.4.6')
                        if stage in body:
                            node['server_uuid'] = OTHER_UUID
                        packets.append((node, body))
                        return evidence_output(original(body, **kwargs), marker, node)
                    scenario.conn.run = route
                    with mock.patch.object(scenario.conn, 'run_write') as send:
                        result = locks.kill_session(scenario.conn, 41, expect=expected, confirmed=True,
                                                    audit=self.audit(), settle_s=0, _batch_context=context)
                    send.assert_not_called()
                    self.assertEqual((result['status'], result['outcome'], result['sent']),
                                     ('refused', 'identity_unavailable', []))
                    self.assertEqual(packets[0][0]['server_uuid'], UUID)
                    self.assertEqual(packets[-1][0]['server_uuid'], OTHER_UUID)
                    self.assertIn(stage, packets[-1][1])

    def test_account_apply_refuses_and_audits_changed_evidence_before_writing(self):
        _, stable, read = next(self.readers(other=False))
        plan = read(stable)
        _, rotating, _ = next(self.readers())
        result = accounts.apply(rotating, plan, plan['expect'], confirmed=True, store=self.store)
        self.assertEqual((result['status'], result['outcome'], result['sent']),
                         ('refused', 'identity_unavailable', []))
        self.assertEqual(self.records()[0]['outcome'], 'identity_unavailable')
        self.assertEqual(rotating.writes, [])

    def test_single_kill_rejects_target_read_on_a_different_packet_backend(self):
        from test_locks import KillScenario, bound_kill_expect, VERSION_SQL, batch
        for galera in (False, True):
            with self.subTest(galera=galera):
                scenario = KillScenario()
                if galera:
                    scenario.conn.responses[0] = (VERSION_SQL, batch([['11.4.13-MariaDB', 1]]))
                    scenario.conn.responses[2] = (scenario.conn.responses[2][0],
                        batch([['1', 'server-one', '11.4.13-MariaDB', '3306', 'ON', UUID, None]]))
                expected = bound_kill_expect(scenario.conn)
                key = 'wsrep_gcomm_uuid' if galera else 'server_uuid'
                expected['server_identity'][key] = OTHER_UUID
                if galera:
                    expected['server_identity']['wsrep_on'] = 'ON'  # identity-required slot
                original = scenario.conn.run
                # Every packet alternates A/B, including version, transaction
                # and XA reads. Identity is read on B, the target row on A.
                nodes = [dict(MYSQL, wsrep_on='OFF', wsrep_provider=PROVIDER, wsrep_gcomm_uuid=UUID),
                         dict(MYSQL, server_uuid=OTHER_UUID, wsrep_on='OFF', wsrep_provider=PROVIDER,
                              wsrep_gcomm_uuid=OTHER_UUID)]
                packets, writes = [], []
                def route(sql, **kwargs):
                    node = nodes[len(packets) % len(nodes)]
                    packets.append((node, sql))
                    if galera and '@@global.server_id' in sql:
                        self.assertIn("VARIABLE_NAME = 'wsrep_provider'", sql)
                        self.assertIn("NOT IN ('', 'none')", sql)
                    return original(sql, **kwargs).replace(UUID, node[key])
                scenario.conn.run = route
                scenario.conn.run_write = lambda sql, **kwargs: writes.append(sql)
                result = locks.kill_session(scenario.conn, 41, expect=expected, confirmed=True,
                                            audit=self.audit(), settle_s=0)
                self.assertEqual(result['status'], 'refused')
                self.assertEqual(writes, [])
                self.assertEqual(packets[1][0][key], OTHER_UUID)
                self.assertEqual(packets[2][0][key], UUID)
                self.assertIn('CONNECTION_ID()', packets[2][1])
                self.assertIn('@@global.server_id' if galera else '@@server_uuid', packets[2][1])
                self.assertEqual(self.records()[0]['outcome'], 'identity_unavailable')

    def test_bulk_target_projection_failure_is_audited_and_does_not_send(self):
        from test_locks import KillScenario, bound_kill_expect
        for batch in (False, True):
            scenario = KillScenario()
            expected = bound_kill_expect(scenario.conn)
            context = locks._kill_context(scenario.conn) if batch else None
            original = scenario.conn.run
            def denied(sql, **kwargs):
                if 'CONNECTION_ID()' in sql and '@@server_uuid' in sql:
                    raise ConnectorError('ERROR 1142 (42000): identity projection denied')
                return original(sql, **kwargs)
            scenario.conn.run = denied
            with mock.patch.object(scenario.conn, 'run_write') as send:
                result = locks.kill_session(scenario.conn, 41, expect=expected, confirmed=True,
                                            audit=self.audit(), settle_s=0, _batch_context=context)
            send.assert_not_called()
            self.assertEqual((result['status'], result['outcome'], result['sent']),
                             ('refused', 'identity_unavailable', []))
            self.assertEqual(self.records()[0]['outcome'], 'identity_unavailable')

    def test_kill_never_uses_a_proxy_sessions_mariadb_server_id(self):
        from test_locks import KillScenario, bound_kill_expect, VERSION_SQL, batch
        scenario = KillScenario()
        scenario.conn.responses[0] = (VERSION_SQL, batch([['11.4.13-MariaDB', 1]]))
        expected = bound_kill_expect(scenario.conn)
        original, packets = scenario.conn.run, []
        def route(sql, **kwargs):
            global_id = 1 if len(packets) % 2 == 0 else 2
            packets.append(sql)
            if 'SELECT @@server_id' in sql or 'SELECT @@global.server_id' in sql:
                # Both reused proxy sessions have SET SESSION server_id=1;
                # the actual backend globals still differ.
                observed = global_id if '@@global.server_id' in sql else 1
                return batch([[observed, 'server-one', '11.4.13-MariaDB', '3306', 'OFF', None, None]])
            return original(sql, **kwargs)
        scenario.conn.run = route
        with mock.patch.object(scenario.conn, 'run_write') as send:
            result = locks.kill_session(scenario.conn, 41, expect=expected, confirmed=True,
                                        audit=self.audit(), settle_s=0)
        send.assert_not_called()
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(len(packets), 2)
        self.assertEqual(self.records()[0]['outcome'], 'identity')

    def test_dollar_quoted_bodies_cannot_swallow_the_guarded_packet_delimiter(self):
        for tag in ('$$', '$q$', '$9_tag$', '$é$', '$标签$'):
            for suffix in ('', ' body; SELECT 2', ' body ' + tag.upper() if tag != tag.upper() else ' body $other$'):
                statement = 'CREATE FUNCTION f() RETURNS INT LANGUAGE JAVASCRIPT AS ' + tag + suffix
                with self.subTest(statement=statement):
                    plan, writer = self.plan(), Writer()
                    plan['statements'] = [statement]
                    with self.assertRaises(ValueError):
                        reviewed_writes.check_reviewed(plan)
                    result = reviewed_writes.execute(writer, plan, lambda: plan, 'test')
                    self.assertEqual((result['status'], writer.writes), ('refused', []))
        for sql in ("SELECT '$q$'", 'SELECT column$q$', 'SELECT é$q$',
                    'SELECT 1 /* $q$ */', 'SELECT 1 -- $q$', 'SELECT $q$ body $q$'):
            dbops.target_statement(sql)
        self.assertEqual(len(dbops.split_statements('SELECT $q$ ; \' /* $q$; SELECT 2', dollar_quotes=True)), 2)
        # On legacy clients these are identifiers; the intervening write
        # must never disappear into a modern-client string classification.
        legacy = 'SELECT 1 AS $q$; DROP TABLE important; SELECT $q$'
        self.assertEqual(dbops.classify(legacy)['kind'], 'risky')
        with self.assertRaises(ValueError):
            dbops.target_statement(legacy)

    def test_replication_line_free_error_after_acknowledgement_audits_unknown(self):
        from test_replication import status
        report, _ = status('mysql-8.4-gtid-error')
        plan = replication.plan_skip(report, '')
        class Ambiguous(Writer):
            def run_write(self, sql, **kwargs):
                self.writes.append(sql)
                exc = ConnectorError('ERROR 2050 (HY000): client library failed')
                exc.guard_markers = dbops.target_script_markers(sql)[:1]
                raise exc
        writer = Ambiguous()
        with mock.patch.object(replication, 'read_status', return_value=report), \
                mock.patch.object(privileges, 'read_privileges', return_value=privileges.parse_grants([], checked=False)), \
                mock.patch.object(replication, '_read_after') as after:
            with self.assertRaisesRegex(ConnectorError, 'outcome is unknown'):
                replication.skip_one(writer, '', plan['identity'], confirmed=True, audit=self.audit())
        after.assert_not_called()
        record = self.records()[0]
        self.assertEqual((record['status'], record['outcome'], record['error_code']), ('unknown', 'unknown', 2050))


if __name__ == '__main__':
    unittest.main()
