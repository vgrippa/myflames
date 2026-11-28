"""Kill sheet evidence through a balancer with one backend per client login.

Only the transport is synthetic: both report collectors, evidence packets,
identity validation and HTTP dispatch run unchanged production code.
"""
import unittest
from unittest import mock

from myflames import locks, ui
from myflames.connector import ConnectorError, PartialReadError
from myflames.ssh_tunnel import Spec, TunnelError, failure_message
from _reviewed_replay import evidence_body, evidence_call, evidence_options
from test_locks import batch, ma_conn, mysql_conn
from test_sessions import replay as sessions_replay

UUID = '11111111-2222-3333-4444-555555555555'
OTHER_UUID = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'


class KillSheetBalancer(object):
    """Each run is a separate login; its prefix and rows use the same node."""

    def __init__(self, reader, nodes, switch_on=None):
        self.reader, self.nodes, self.switch_on = reader, nodes, switch_on
        self.logins, self.writes = [], []
        self.switched = self.closed = False
        self.failure = self.fail_on = None
        self.fail_unbound = False

    def __getattr__(self, name):
        return getattr(self.reader, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    @evidence_options
    def run(self, sql, extra_flags=None, **kwargs):
        body, marker = evidence_body(sql)
        if self.switch_on:
            self.switched = self.switched or self.switch_on in body
            node = self.nodes[int(self.switched)]
        else:
            node = self.nodes[len(self.logins) % len(self.nodes)]
        self.logins.append((node, body, marker, list(extra_flags or [])))
        if (marker or self.fail_unbound) and self.failure is not None and (self.fail_on is None or self.fail_on in body):
            raise self.failure

        def read():
            engine = 'mysql' if 'server_uuid' in node else 'mariadb'
            keys, projection = locks._identity_projection(engine)
            if 'SELECT ' + projection in body:
                return batch([[node.get(key) for key in keys]])
            return self.reader.run(body, extra_flags=extra_flags, **kwargs)

        return evidence_call(read, marker, node) if marker else read()

    def run_write(self, sql, **kwargs):
        self.writes.append(sql)
        raise AssertionError('A Kill sheet read must not send a write.')

    run_stdin = run_write


class TestKillSheetEvidence(unittest.TestCase):
    def connection(self, place, engine, rotating=False, switch_on=None, standalone=False):
        if place == 'locks':
            reader = ma_conn() if engine == 'mariadb' else mysql_conn()
        else:
            reader = sessions_replay('mariadb-11.4' if engine == 'mariadb' else 'mysql-8.4')
        if engine == 'mariadb':
            # Galera peers may share server_id, host name and port. Only the
            # node UUID distinguishes these synthetic backends.
            node = {'server_id': '1', 'hostname': 'server-one', 'port': '3306',
                    'version': '11.4.13-MariaDB-ubu2404', 'server_uid': 'AQEBAQEBAQEBAQEBAQEBAQEBAQE=',
                    'wsrep_on': 'ON', 'wsrep_provider': '/usr/lib/galera/libgalera_smm.so',
                    'wsrep_gcomm_uuid': UUID, 'wsrep_connected': 'ON', 'wsrep_cluster_size': '2'}
            other = dict(node, wsrep_gcomm_uuid=OTHER_UUID)
            if standalone:
                node.update(wsrep_on='OFF', wsrep_provider='none',
                            wsrep_connected='OFF', wsrep_cluster_size='0')
                node.pop('wsrep_gcomm_uuid')
        else:
            node = {'server_uuid': UUID, 'version': '8.4.6' if place == 'locks' else '8.4.11',
                    'wsrep_on': 'OFF'}
            other = dict(node, server_uuid=OTHER_UUID)
        return KillSheetBalancer(reader, [node, other] if rotating or switch_on else [node], switch_on)

    def read(self, place, conn):
        with mock.patch('myflames.connector.MySQLConnection', return_value=conn):
            return ui._discover_response('/api/live/' + place,
                                         {'connection': {'host': 'balancer'}, 'active_only': False})

    def assert_unidentified_report(self, place, report, conn):
        self.assertTrue(report['blocking_tree' if place == 'locks' else 'sessions'])
        self.assertEqual(report['server_identity'], {})
        self.assertIn('Kill is unavailable because the server identity could not be confirmed on one server.',
                      report['notes'])
        self.assertTrue(any(marker for _node, _body, marker, _flags in conn.logins))
        unbound = [body for _node, body, marker, _flags in conn.logins if not marker]
        self.assertTrue(unbound, 'collect a fresh report after discarding the failed bound scope')
        for engine in ('mysql', 'mariadb'):
            _keys, projection = locks._identity_projection(engine)
            self.assertFalse(any('SELECT ' + projection in body for body in unbound),
                             'an unbound report must not read a Kill identity')
        self.assertEqual(conn.writes, [])
        self.assertTrue(conn.closed)

    def test_connected_galera_with_hidden_uuid_keeps_rows_without_kill_identity(self):
        for place in ('locks', 'sessions'):
            with self.subTest(place=place):
                conn = self.connection(place, 'mariadb')
                conn.nodes[0].update(wsrep_on='OFF')
                conn.nodes[0].pop('wsrep_gcomm_uuid')
                self.assert_unidentified_report(place, self.read(place, conn), conn)

    def test_optional_server_error_with_unavailable_galera_identity_keeps_rows(self):
        conn = self.connection('locks', 'mariadb')
        conn.nodes[0].update(wsrep_on='OFF')
        conn.nodes[0].pop('wsrep_gcomm_uuid')
        read = conn.reader.run
        unavailable_reads = []

        def without_performance_schema(sql, *args, **kwargs):
            if 'SELECT @@version, @@performance_schema' in sql:
                unavailable_reads.append(sql)
                raise ConnectorError("ERROR 1193 (HY000): Unknown system variable 'performance_schema'")
            return read(sql, *args, **kwargs)

        conn.reader.run = without_performance_schema
        conn.reader.server_version = lambda: conn.nodes[0]['version']
        conn.reader.is_mariadb = lambda: True
        self.assert_unidentified_report('locks', self.read('locks', conn), conn)
        self.assertEqual(len(unavailable_reads), 2, 'the collector handles the server error again unbound')
        self.assertEqual(sum(bool(marker) for _node, _body, marker, _flags in conn.logins), 1)

    def test_partial_client_error_or_unrecognized_diagnostic_never_retries_first_login(self):
        for message in ('ERROR 2013 (HY000) at line 2: Lost connection during query',
                        'Client exited without a server diagnostic',
                        '--------------\nERROR 1193 (HY000) at line 2: echoed SQL\n--------------\n'
                        'ERROR 2013 (HY000) at line 2: Lost connection during query'):
            with self.subTest(message=message):
                conn = self.connection('sessions', 'mariadb')
                conn.failure = PartialReadError(message, '')
                conn.fail_unbound = True
                with self.assertRaises(ConnectorError):
                    self.read('sessions', conn)
                self.assertEqual(len(conn.logins), 1)
                self.assertEqual(conn.writes, [])

    def test_line_located_login_and_host_refusals_never_retry_first_login(self):
        messages = (
            "ERROR 1045 (28000) at line 2: ProxySQL Error: Access denied for user 'app'@'10.0.0.1'",
            "ERROR 1698 (28000) at line 2: Access denied for user 'root'@'localhost'",
            "ERROR 1129 (HY000) at line 2: Host '10.0.0.1' is blocked because of many connection errors",
            "ERROR 1130 (HY000) at line 2: Host '10.0.0.1' is not allowed to connect to this MySQL server",
            "ERROR 3118 (HY000) at line 2: Access denied for user 'app'@'%'. Account is locked.",
            "ERROR 3955 (HY000) at line 2: Access denied for user 'app'@'%'. Account is blocked for 1 day(s).",
        )
        for place in ('locks', 'sessions'):
            for message in messages:
                with self.subTest(place=place, message=message):
                    conn = self.connection(place, 'mariadb')
                    conn.failure = PartialReadError(message, '')
                    conn.fail_unbound = True
                    with self.assertRaises(ConnectorError):
                        self.read(place, conn)
                    self.assertEqual(len(conn.logins), 1)
                    self.assertEqual(conn.writes, [])

    def test_unbound_reread_never_publishes_identity_even_if_next_node_has_one(self):
        for place in ('locks', 'sessions'):
            with self.subTest(place=place):
                conn = self.connection(place, 'mariadb', rotating=True)
                conn.nodes[0].update(wsrep_on='OFF')
                conn.nodes[0].pop('wsrep_gcomm_uuid')
                self.assert_unidentified_report(place, self.read(place, conn), conn)
                self.assertTrue(any(node.get('wsrep_gcomm_uuid') and not marker
                                    for node, _body, marker, _flags in conn.logins))

    def test_rotating_logins_never_publish_a_mixed_kill_sheet(self):
        for place in ('locks', 'sessions'):
            for engine in ('mysql', 'mariadb'):
                with self.subTest(place=place, engine=engine):
                    conn = self.connection(place, engine, rotating=True)
                    self.assert_unidentified_report(place, self.read(place, conn), conn)
                    self.assertGreaterEqual(len(conn.logins), 2)
                    self.assertNotEqual(conn.logins[0][0], conn.logins[1][0])

    def test_late_routing_change_caught_by_optional_read_invalidates_identity(self):
        for place in ('locks', 'sessions'):
            for engine in ('mysql', 'mariadb'):
                with self.subTest(place=place, engine=engine):
                    conn = self.connection(place, engine, switch_on='FROM information_schema.INNODB_TRX')
                    self.assert_unidentified_report(place, self.read(place, conn), conn)
                    self.assertTrue(conn.switched, 'reach the optional transaction read after identity was read')
                    keys, projection = locks._identity_projection(engine)
                    self.assertTrue(any('SELECT ' + projection in body and node == conn.nodes[0]
                                        for node, body, _marker, _flags in conn.logins))

    def test_failed_reads_never_retry_without_evidence(self):
        for place in ('locks', 'sessions'):
            for engine in ('mysql', 'mariadb'):
                for error_type, message in (
                            (ConnectorError, 'ERROR 2013: Lost connection to MySQL server during query'),
                            (ConnectorError, 'ERROR 1045: Access denied for user'),
                            (ConnectorError, 'ERROR 1142: SELECT command denied'),
                            (ConnectorError, 'Query timed out after 15s.'),
                            (ValueError, 'Malformed client output'),
                            (OSError, 'Client process could not run')):
                    with self.subTest(place=place, engine=engine, error=message):
                        conn = self.connection(place, engine)
                        conn.failure, conn.fail_unbound = error_type(message), True
                        with self.assertRaisesRegex(ConnectorError, 'identity check could not run'):
                            self.read(place, conn)
                        self.assertEqual(len(conn.logins), 1, 'a failed first login must never be retried')
                        self.assertTrue(conn.logins[0][2])
                        self.assertEqual(conn.writes, [])
                        self.assertTrue(conn.closed)

    def test_first_bound_read_preserves_ssh_explanations(self):
        spec = Spec('bastion.example', 'app', 22, 'database.example', 3306)
        for place in ('locks', 'sessions'):
            for stderr, detail in (
                    ('REMOTE HOST IDENTIFICATION HAS CHANGED', 'has changed since it was saved in known_hosts'),
                    ('Permission denied (publickey).', 'Permission denied')):
                with self.subTest(place=place, stderr=stderr):
                    conn = self.connection(place, 'mysql')
                    conn.failure = TunnelError(failure_message(stderr, spec))
                    with self.assertRaisesRegex(ConnectorError, detail):
                        self.read(place, conn)
                    self.assertEqual(len(conn.logins), 1)
                    self.assertEqual(conn.writes, [])
                    self.assertTrue(conn.closed)

    def test_first_bound_read_timeout_keeps_duration_without_sql_or_marker(self):
        for place in ('locks', 'sessions'):
            with self.subTest(place=place):
                conn = self.connection(place, 'mysql')
                conn.failure = ConnectorError("Query timed out after 15s: SELECT 'myflames_evidence_%s'; "
                                              "SELECT secret_column FROM private_table" % ('a' * 32))
                with self.assertRaisesRegex(ConnectorError, 'timed out after 15s') as caught:
                    self.read(place, conn)
                for private in ('myflames_evidence_', 'SELECT', 'secret_column', 'private_table'):
                    self.assertNotIn(private, str(caught.exception))
                self.assertEqual(len(conn.logins), 1)

    def test_code_free_client_echo_never_surfaces_marker_or_sql(self):
        for place in ('locks', 'sessions'):
            with self.subTest(place=place):
                conn = self.connection(place, 'mysql')
                conn.failure = ConnectorError("mysql client exited 1:\n--------------\n"
                                              "SELECT 'myflames_evidence_%s'; SELECT secret_column "
                                              "FROM private_table\n--------------\nclient stopped" % ('a' * 32))
                with self.assertRaisesRegex(ConnectorError, 'identity check could not run') as caught:
                    self.read(place, conn)
                for private in ('myflames_evidence_', 'SELECT', 'secret_column', 'private_table', 'client stopped'):
                    self.assertNotIn(private, str(caught.exception))
                self.assertEqual(len(conn.logins), 1)

    def test_locks_optional_transport_failures_keep_unidentified_report(self):
        reads = (('mysql', 'SHOW ENGINE INNODB STATUS', 'Could not read SHOW ENGINE INNODB STATUS'),
                 ('mariadb', 'SHOW ENGINE INNODB STATUS', 'Could not read SHOW ENGINE INNODB STATUS'),
                 ('mysql', 'FROM performance_schema.setup_instruments', 'Could not read the Performance Schema setup'),
                 ('mysql', 'FROM performance_schema.events_statements_current', None))
        for engine, fail_on, note in reads:
            for message in ('Query timed out after 15s.', 'ERROR 2013: Lost connection to MySQL server during query'):
                with self.subTest(engine=engine, fail_on=fail_on, message=message):
                    conn = self.connection('locks', engine)
                    conn.fail_on, conn.failure, conn.fail_unbound = fail_on, ConnectorError(message), True
                    report = self.read('locks', conn)
                    self.assert_unidentified_report('locks', report, conn)
                    if note:
                        self.assertTrue(any(note in item and message in item for item in report['notes']), report['notes'])
                    if fail_on == 'SHOW ENGINE INNODB STATUS':
                        self.assertIsNone(report['deadlock'])
                    elif note is None:
                        self.assertFalse(report['capabilities']['last_statement'])
                    self.assertTrue(any(fail_on in body and not marker
                                        for _node, body, marker, _flags in conn.logins),
                                    'the fallback must encounter the same persistent optional failure')

    def test_required_read_transport_failures_still_fail_in_unbound_report(self):
        for place in ('locks', 'sessions'):
            for engine in ('mysql', 'mariadb'):
                for message in ('Query timed out after 15s.', 'ERROR 2013: Lost connection to MySQL server during query'):
                    with self.subTest(place=place, engine=engine, message=message):
                        conn = self.connection(place, engine)
                        conn.fail_on = 'FROM information_schema.INNODB_TRX'
                        conn.failure, conn.fail_unbound = ConnectorError(message), True
                        with self.assertRaises((ConnectorError, ValueError)) as caught:
                            self.read(place, conn)
                        self.assertIn(message, str(caught.exception))
                        self.assertEqual(conn.writes, [])
                        self.assertTrue(conn.closed)

    def test_optional_identity_read_timeout_keeps_sessions_without_kill(self):
        conn = self.connection('sessions', 'mysql')
        conn.fail_on = 'SELECT @@server_uuid'
        conn.failure, conn.fail_unbound = ConnectorError('Query timed out after 15s.'), True
        report = self.read('sessions', conn)
        self.assert_unidentified_report('sessions', report, conn)
        self.assertEqual(sum(conn.fail_on in body for _node, body, _marker, _flags in conn.logins), 1,
                         'the unbound fallback must not retry the failed identity projection')

    def test_one_node_keeps_rows_and_identity_with_bound_packets(self):
        for place in ('locks', 'sessions'):
            for engine, standalone in (('mysql', False), ('mariadb', False), ('mariadb', True)):
                with self.subTest(place=place, engine=engine, standalone=standalone):
                    conn = self.connection(place, engine, standalone=standalone)
                    report = self.read(place, conn)
                    self.assertTrue(report['blocking_tree' if place == 'locks' else 'sessions'])
                    keys, _projection = locks._identity_projection(engine)
                    expected = locks._identity_row(keys, [conn.nodes[0].get(key) for key in keys])
                    self.assertEqual(report['server_identity'], expected)
                    self.assertTrue(all(marker and '--skip-reconnect' in flags
                                        for _node, _body, marker, flags in conn.logins))
                    self.assertEqual(conn.writes, [])
                    self.assertTrue(conn.closed)

    def test_rotating_standalone_mariadb_differing_only_in_server_uid_refuses_kill_identity(self):
        for place in ('locks', 'sessions'):
            with self.subTest(place=place):
                conn = self.connection(place, 'mariadb', rotating=True, standalone=True)
                base = dict(conn.nodes[0], server_uid='AQEBAQEBAQEBAQEBAQEBAQEBAQE=')
                other = dict(base, server_uid='AgICAgICAgICAgICAgICAgICAgI=')
                conn.nodes = [base, other]
                self.assert_unidentified_report(place, self.read(place, conn), conn)


if __name__ == '__main__':
    unittest.main()
