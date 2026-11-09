"""Source-grounded, synthetic metadata fixtures for both engines, no servers."""
import json
from email.message import Message
import types
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from myflames import readiness, ui
from myflames.connector import ConnectorError

FIXTURES = Path(__file__).parent / 'fixtures' / 'connection-import'


class Connection:
    def __init__(self, fixture='mysql', replica=False, denied=False, group=False):
        self.data = json.loads((FIXTURES / ('readiness-%s.json' % fixture)).read_text())
        self.replica, self.denied, self.group = replica, denied, group
        self.calls = []
        self.errors = ''

    def run_script(self, sql, timeout=None):
        self.calls.append((sql, timeout))
        parts = []
        for key in ('variables', 'wsrep', 'grants'):
            values = self.data.get(key, {} if key != 'grants' else [])
            parts.extend(values if isinstance(values, list) else ['%s\t%s' % pair for pair in values.items()])
        return '\n'.join(parts), self.errors

    def run(self, sql, extra_flags=None, timeout=None):
        self.calls.append((sql, timeout))
        if self.denied:
            raise ConnectorError('ERROR 1142: SELECT command denied')
        if 'replication_group_members' in sql:
            return '1\n' if self.group else ''
        return '1\n' if self.replica else ''


class TestReadiness(unittest.TestCase):
    def test_mysql_metadata_and_three_second_shared_budget(self):
        conn = Connection()
        with mock.patch.object(readiness.time, 'monotonic', side_effect=[10, 10.2, 11.5]):
            report = readiness.read_readiness(conn)
        self.assertEqual(report['product'], 'MySQL')
        self.assertTrue(report['performance_schema'])
        self.assertFalse(report['replica'])
        self.assertTrue(report['privileges']['CONNECTION_ADMIN'])
        self.assertTrue(report['privileges']['PROCESS'])
        self.assertAlmostEqual(conn.calls[0][1], 2.8)
        self.assertAlmostEqual(conn.calls[1][1], 1.5)
        self.assertEqual(len(conn.calls), 2)
        self.assertIn('LIMIT 1', conn.calls[1][0])
        self.assertIn("CHANNEL_NAME NOT IN ('group_replication_applier','group_replication_recovery')", conn.calls[1][0])
        self.assertNotIn('grants', report)

    def test_mariadb_off_replica_galera_and_no_hash(self):
        conn = Connection('mariadb', replica=True)
        report = readiness.read_readiness(conn)
        self.assertEqual(report['product'], 'MariaDB')
        self.assertIsNone(report['super_read_only'])
        self.assertTrue(report['read_only'])
        self.assertTrue(report['replica'])
        self.assertTrue(report['galera_member'])
        self.assertTrue(report['privileges']['CONNECTION ADMIN'])
        self.assertTrue(report['actions']['replication_status']['allowed'])
        self.assertIn('Server > Instruments', ' '.join(report['notes']))
        self.assertNotIn('DO_NOT_EXPOSE', json.dumps(report))
        self.assertIn('SHOW ALL SLAVES STATUS', conn.calls[1][0])

    def test_percona_uses_comment_not_suffix(self):
        conn = Connection()
        conn.data['variables']['version_comment'] = 'Percona Server (GPL), Release 11'
        report = readiness.read_readiness(conn)
        self.assertEqual(report['product'], 'Percona Server')
        self.assertEqual(report['engine'], 'mysql')
        self.assertEqual(report['flavor'], 'percona')

    def test_group_member_is_local_not_a_remote_members_row(self):
        conn = Connection(group=True)
        conn.data['variables']['group_replication_group_name'] = 'group-uuid'
        self.assertTrue(readiness.read_readiness(conn)['group_member'])
        self.assertIn("MEMBER_ID = @@server_uuid AND MEMBER_STATE <> 'OFFLINE' LIMIT 1", conn.calls[-1][0])

    def test_denied_topology_is_unknown_not_standalone(self):
        report = readiness.read_readiness(Connection(denied=True))
        self.assertIsNone(report['replica'])
        self.assertIn('could not be verified', ' '.join(report['notes']))

    def test_denied_grants_are_unknown_and_stderr_is_not_returned(self):
        conn = Connection()
        conn.data['grants'] = []
        conn.errors = 'ERROR 1227: secret details'
        report = readiness.read_readiness(conn)
        self.assertIsNone(report['privileges']['PROCESS'])
        self.assertNotIn('secret details', json.dumps(report))

    def test_known_missing_privileges_are_false(self):
        conn = Connection()
        conn.data['grants'] = ['GRANT USAGE ON *.* TO `reader`@`%`']
        report = readiness.read_readiness(conn)
        self.assertFalse(report['privileges']['PROCESS'])
        self.assertFalse(report['actions']['kill']['allowed'])
        self.assertFalse(report['actions']['replication_status']['allowed'])

    def test_mysql57_and_ps_off_does_not_assume_empty_tables(self):
        conn = Connection(replica=True)
        conn.data['variables'].update(version='5.7.44', performance_schema='OFF')
        conn.data['grants'] = ['GRANT SUPER ON *.* TO `reader`@`%`']
        report = readiness.read_readiness(conn)
        self.assertTrue(report['replica'])
        self.assertIn('SHOW SLAVE STATUS', conn.calls[-1][0])
        self.assertIn('MySQL 5.7 has no CONNECTION_ADMIN', ' '.join(report['notes']))

    def test_ps_off_no_replication_privilege_stays_unknown(self):
        conn = Connection()
        conn.data['variables']['performance_schema'] = 'OFF'
        conn.data['grants'] = ['GRANT USAGE ON *.* TO `reader`@`%`']
        self.assertIsNone(readiness.read_readiness(conn)['replica'])
        self.assertEqual(len(conn.calls), 1)

    def test_mariadb_read_only_modes_are_not_false(self):
        for setting in ('NO_LOCK', 'NO_LOCK_NO_ADMIN'):
            conn = Connection('mariadb')
            conn.data['variables']['read_only'] = setting
            self.assertTrue(readiness.read_readiness(conn)['read_only'])

    def test_followups_do_not_extend_deadline(self):
        conn = Connection()
        with mock.patch.object(readiness.time, 'monotonic', side_effect=[10, 10.5, 13.1]):
            report = readiness.read_readiness(conn)
        self.assertIsNone(report['replica'])
        self.assertEqual(len(conn.calls), 1)

    def test_no_facts_is_an_error(self):
        conn = Connection()
        conn.data['variables'] = {}
        with self.assertRaisesRegex(ConnectorError, 'did not return'):
            readiness.read_readiness(conn)

    def test_endpoint_fills_saved_password_and_bounds_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {'HOME': directory, 'USERPROFILE': directory, 'MYFLAMES_SECRET_BACKEND': 'vault'}
            with mock.patch.dict(os.environ, env), mock.patch('myflames.connector.MySQLConnection') as factory:
                os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
                workspace = mock.Mock()
                workspace.store.connection_with_saved_password.return_value = {'host': 'db.example', 'user': 'reader', 'password': 'private'}
                factory.return_value.__enter__.return_value = Connection()
                report = ui.api_response('/api/live/readiness', {'connection': {'profile_id': 'one'}}, workspace)
                self.assertEqual(report['product'], 'MySQL')
                self.assertEqual(factory.call_args[1]['password'], 'private')
                self.assertEqual(factory.call_args[1]['query_timeout'], 3)
                self.assertEqual(factory.call_args[1]['connect_timeout'], 3)
                self.assertNotIn('private', json.dumps(report))

    def test_endpoint_redacts_connection_error(self):
        with mock.patch('myflames.connector.MySQLConnection') as factory:
            factory.return_value.__enter__.side_effect = ConnectorError('private: cannot connect')
            with self.assertRaises(ConnectorError) as raised:
                readiness.response({'connection': {'host': 'db.example', 'password': 'private'}})
            self.assertNotIn('private', str(raised.exception))


class TestReadinessHTTPGuards(unittest.TestCase):
    """Run the actual handler without binding a forbidden sandbox socket."""
    def handler(self, path, token='token', origin='http://127.0.0.1:9999'):
        handler = object.__new__(ui.WorkspaceHandler)
        handler.path = path
        handler.command = 'POST'
        handler.server = types.SimpleNamespace(origin='http://127.0.0.1:9999', token='token',
                                               cookie_name='mf_session', session_secret='cookie')
        handler.headers = Message()
        for key, value in {'Host': '127.0.0.1:9999', 'Origin': origin, 'Cookie': 'mf_session=cookie',
                           'X-Myflames-Token': token, 'Content-Type': 'application/json', 'Content-Length': '2'}.items():
            handler.headers[key] = value
        handler._read_body = mock.Mock(return_value=b'{}')
        handler._send = mock.Mock()
        return handler

    def test_both_endpoints_are_post_allowlisted_and_behind_cookie_token_origin(self):
        for path in ('/api/live/readiness', '/api/connections/import'):
            for token, origin, status in (('token', 'http://127.0.0.1:9999', 200),
                                          ('wrong', 'http://127.0.0.1:9999', 403),
                                          ('token', 'https://hostile.example', 403)):
                with self.subTest(path=path, token=token, origin=origin):
                    handler = self.handler(path, token, origin)
                    with mock.patch.object(ui, 'api_response', return_value={'ok': True}) as response:
                        handler.do_POST()
                    self.assertEqual(handler._send.call_args[0][0], status)
                    self.assertEqual(response.call_count, 1 if status == 200 else 0)
            handler = self.handler(path)
            del handler.headers['Cookie']
            with mock.patch.object(ui, 'api_response') as response:
                handler.do_POST()
            self.assertEqual(handler._send.call_args[0][0], 403)
            response.assert_not_called()


if __name__ == '__main__':
    unittest.main()
