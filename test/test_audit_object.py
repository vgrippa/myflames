"""Local object history: exact audit targets, bounded output, no server I/O."""
import os
import json
from email.message import Message
import tempfile
import unittest
from unittest import mock

from myflames import dbops, ui
from myflames.workspace_store import WorkspaceStore


class TestAuditObject(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env = mock.patch.dict(os.environ, {'HOME': self.directory.name, 'USERPROFILE': self.directory.name,
                                           'MYFLAMES_SECRET_BACKEND': 'vault'})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        self.store = WorkspaceStore()

    def record(self, operation='set_global', target=None, engine='mysql', host='db', port=3306,
               status='ok', statement='/*myflames:variable*/ SET GLOBAL max_connections = 100'):
        return dbops.record_outcome(self.store, operation, [statement], 'ui', status,
                                    connection={'host': host, 'port': port}, engine=engine, target=target)

    def lookup(self, kind='parameter', key='max_connections', server='db:3306'):
        return dbops.audit_object_report(self.store, server, kind, key)['records']

    def test_both_engines_match_target_not_statement_or_other_server_or_profile(self):
        for engine in ('mysql', 'mariadb'):
            with self.subTest(engine=engine):
                expected = self.record(target={'name': 'max_connections', 'mode': 'global'}, engine=engine)
                self.record(target={'name': 'max_connections_extra'}, engine=engine)
                self.record(target={'name': 'max_connections'}, engine=engine, host='elsewhere')
                self.record(target={'name': 'max_connections'}, engine=engine, port=3307)
                self.record(operation='editor_run', target={'name': 'max_connections'}, engine=engine)
                self.record(target=None, engine=engine)
                self.assertEqual(self.lookup()[0]['id'], expected)
                self.assertTrue(all(r['connection']['host'] == 'db' and r['connection']['port'] == 3306
                                    and r['operation'] == 'set_global' for r in self.lookup()))
        self.assertEqual(self.lookup(server='elsewhere:3307'), [])
        dbops.record_outcome(self.store, 'set_global', ['SET GLOBAL max_connections=100'], 'ui', 'ok',
                            connection={'host': 'other', 'port': 3306}, safety={'profile_name': 'db:3306'},
                            target={'name': 'max_connections'})
        self.assertTrue(all(r['connection']['host'] == 'db' for r in self.lookup()),
                        'a profile name cannot impersonate the requested endpoint')

    def test_limit_and_status_apply_after_exact_matching_newest_first_including_ties(self):
        ids = []
        with mock.patch('myflames.dbops._now', return_value='2026-11-08T12:00:00+00:00'):
            for i in range(8):
                ids.append(self.record(target={'name': 'max_connections'}, status='unknown' if i % 2 else 'ok'))
            for status in ('pending', 'error', 'declined', 'refused', 'stopped'):
                if status == 'pending':
                    dbops.record_attempt(self.store, 'set_global', ['SET GLOBAL max_connections=1'], 'ui',
                                         connection={'host': 'db', 'port': 3306}, target={'name': 'max_connections'})
                else:
                    self.record(target={'name': 'max_connections'}, status=status)
            for _ in range(20):
                self.record(target={'name': 'different'})
        records = self.lookup()
        self.assertEqual([r['id'] for r in records], ids[-5:][::-1])
        self.assertEqual([r['status'] for r in records], ['unknown', 'ok', 'unknown', 'ok', 'unknown'])

    def test_accounts_use_actual_quoted_target_with_engine_role_and_host(self):
        cases = [('mysql', 'app`name', '%', False, '`app``name`@`%`'),
                 ('mysql', '', '%', False, '``@`%`'),
                 ('mariadb', 'app', 'localhost', False, '`app`@`localhost`'),
                 ('mysql', 'reader', '%', True, '`reader`@`%`'),
                 ('mariadb', 'reader', '', True, '`reader`'),
                 ('mariadb', 'PUBLIC', '', True, 'PUBLIC')]
        for engine, name, host, role, target in cases:
            with self.subTest(engine=engine, target=target):
                expected = self.record('accounts.grant', {'account': target, 'change_set_id': 'example'}, engine=engine)
                self.record('accounts.grant', {'account': '`other`@`%`'}, engine=engine)
                self.record('editor_run', {'account': target}, engine=engine)
                key = {'name': name, 'host': host, 'role': role}
                records = self.lookup('account', key)
                self.assertEqual(records[0]['id'], expected)
                self.assertTrue(all(r['target']['account'] == target and r['operation'].startswith('accounts.')
                                    for r in records))

    def test_index_table_target_does_not_claim_a_specific_index(self):
        self.record('editor_run', {'server_uuid': 'id', 'alter_table': {'schema': 'shop', 'table': 'orders'}},
                    statement='ALTER TABLE shop.orders ALTER INDEX customer_id INVISIBLE')
        self.assertEqual(self.lookup('index', {'schema': 'shop', 'table': 'orders', 'index': 'customer_id'}), [])

    def test_two_ssh_tunnels_and_legacy_direct_records_never_mix(self):
        for engine in ('mysql', 'mariadb'):
            ids = {}
            for host in ('bastion-a', 'bastion-b', None):
                connection = {'host': '127.0.0.1', 'port': 3306}
                if host:
                    connection.update(ssh_host=host, ssh_user='deploy', ssh_port=2222)
                ids[host] = dbops.record_outcome(self.store, 'set_global', ['synthetic statement'], 'ui', 'ok',
                                                connection=connection, engine=engine,
                                                target={'name': 'max_connections', 'mode': 'global',
                                                        'global_value': host or 'direct'})
            for host in ids:
                ssh = {'host': host, 'user': 'deploy', 'port': 2222} if host else None
                reply = ui.api_response('/api/audit/object', {'server': '127.0.0.1:3306', 'kind': 'parameter',
                                        'key': 'max_connections', 'ssh': ssh}, mock.Mock(store=self.store))
                self.assertEqual(reply['records'][0]['id'], ids[host])
                self.assertTrue(all(r['target']['global_value'] == (host or 'direct') for r in reply['records']))
            for ssh in ({'host': 'bastion-a', 'user': 'other', 'port': 2222},
                        {'host': 'bastion-a', 'user': 'deploy', 'port': 22}):
                self.assertEqual(dbops.audit_object_report(self.store, '127.0.0.1:3306', 'parameter',
                                 'max_connections', ssh)['records'], [])

    def test_index_matches_exact_structured_name_on_both_engines(self):
        for engine in ('mysql', 'mariadb'):
            key = {'schema': 'shop', 'table': 'orders', 'index': 'customer_id'}
            expected = self.record('editor_run', {'alter_table': dict(key)}, engine=engine)
            for field in key:
                wrong = dict(key, **{field: key[field] + '_other'})
                self.record('editor_run', {'alter_table': wrong}, engine=engine)
            self.record('editor_run', {'alter_table': {'schema': 'shop', 'table': 'orders'}}, engine=engine)
            self.record('set_global', {'alter_table': key}, engine=engine)
            records = self.lookup('index', key)
            self.assertEqual(records[0]['id'], expected)
            self.assertTrue(all(r['target']['alter_table'] == key and r['operation'] == 'editor_run' for r in records))

    def test_ssh_filter_is_validated_and_bound_as_data(self):
        for ssh in ('host', [], {}, {'host': 'x', 'user': '', 'port': True},
                    {'host': 'x', 'user': '', 'port': 65536}, {'host': '', 'user': '', 'port': 0},
                    {'host': 'x', 'user': '', 'port': 0, 'password': 'secret'},
                    {'host': 'x\x00', 'user': '', 'port': 0}):
            with self.subTest(ssh=ssh), mock.patch.object(self.store, 'audit_object_records') as read:
                with self.assertRaises(ValueError):
                    dbops.audit_object_report(self.store, 'db:3306', 'parameter', 'x', ssh)
                read.assert_not_called()
        self.record(target={'name': 'max_connections'})
        self.assertEqual(dbops.audit_object_report(self.store, 'db:3306', 'parameter', 'max_connections',
                         {'host': "' OR 1=1 --", 'user': '', 'port': 0})['records'], [])

    def test_session_parameter_changes_are_history_even_without_a_global_inverse(self):
        # Synthetic identity only; no claim about a real variable's scope.
        expected = self.record('set_session', {'name': 'synthetic_parameter', 'mode': 'session'},
                               statement='synthetic statement')
        self.assertEqual([r['id'] for r in self.lookup(key='synthetic_parameter')], [expected])

    def test_malformed_truncated_and_non_object_targets_fail_closed(self):
        for target in ('{', 'null', '[]', '"name"', '1'):
            self.assertFalse(dbops.audit_object_matches('set_global', target, 'mysql', 'parameter', {'name': 'x'}))
        for key in ({'name': 'X'}, {'name': 'max_connections_extra'}):
            self.record(target=key)
        self.assertEqual(self.lookup(), [])

    def test_invalid_inputs_fail_before_store_read(self):
        for server, kind, key in [(None, 'parameter', 'x'), ('db', 'parameter', 'x'),
                                  ('db:3306', [], 'x'), ('db:3306', 'statement', 'x'),
                                  ('db:3306', 'account', {'name': 'x', 'host': '%'}),
                                  ('db:3306', 'account', {'name': 'x', 'host': '%', 'role': 1}),
                                  ('db:3306', 'parameter', 'x\x00'), ('db:3306', 'parameter', 'x' * 257),
                                  ('db:3306', 'index', {'schema': 's', 'table': 't', 'index': ''})]:
            with self.subTest(server=server, kind=kind, key=key), mock.patch.object(self.store, 'audit_object_records') as read:
                with self.assertRaises(ValueError):
                    dbops.audit_object_report(self.store, server, kind, key)
                read.assert_not_called()

    def test_one_select_and_no_connector_and_list_deeplink_filters_before_limit(self):
        expected = self.record(target={'name': 'max_connections'})
        for _ in range(8):
            self.record(target={'name': 'other'})
        queries = []
        connect = self.store._connect

        def traced():
            conn = connect()
            conn.set_trace_callback(lambda sql: queries.append(sql))  # Python 3.7 hashes the callback
            return conn

        workspace = mock.Mock(store=self.store)
        with mock.patch.object(self.store, '_connect', side_effect=traced), mock.patch('myflames.connector.MySQLConnection') as connector:
            reply = ui.api_response('/api/audit/object', {'server': 'DB:3306', 'kind': 'parameter', 'key': 'max_connections'}, workspace)
            self.assertEqual(reply['records'][0]['id'], expected)
            connector.assert_not_called()
        self.assertEqual(len([q for q in queries if q.startswith('SELECT ')]), 1)
        filtered = ui.api_response('/api/audit/list', {'id': expected, 'limit': 1}, workspace)
        self.assertEqual([r['id'] for r in filtered['records']], [expected])
        self.assertFalse(filtered['has_more'])
        exported = ui.api_response('/api/audit/export', {'id': expected, 'format': 'json'}, workspace)
        self.assertEqual([r['id'] for r in json.loads(exported['content'])['records']], [expected])
        with self.assertRaises(ValueError):
            ui.api_response('/api/audit/list', {'id': "' OR 1=1 --"}, workspace)

    def test_http_route_retains_host_origin_cookie_and_token_guards_without_sockets(self):
        self.record(target={'name': 'max_connections'})
        body = json.dumps({'server': 'db:3306', 'kind': 'parameter', 'key': 'max_connections'}).encode('utf-8')
        headers = {'Host': '127.0.0.1:8766', 'Origin': 'http://127.0.0.1:8766',
                   'Cookie': 'session=proof', 'X-Myflames-Token': 'token', 'Content-Type': 'application/json',
                   'Content-Length': str(len(body))}
        for altered, status in (({}, 200), ({'Host': 'evil.test'}, 403), ({'Origin': 'https://evil.test'}, 403),
                                ({'Cookie': ''}, 403), ({'X-Myflames-Token': 'wrong'}, 403)):
            with self.subTest(altered=altered):
                handler = object.__new__(ui.WorkspaceHandler)
                handler.server = mock.Mock(store=self.store, origin='http://127.0.0.1:8766',
                                           token='token', cookie_name='session', session_secret='proof')
                handler.path, handler.command = '/api/audit/object', 'POST'
                handler.headers = Message()
                for key, value in dict(headers, **altered).items():
                    handler.headers[key] = value
                handler._read_body = mock.Mock(return_value=body)
                handler._send = mock.Mock()
                handler.do_POST()
                self.assertEqual(handler._send.call_args[0][0], status)
                if status == 403:
                    handler._read_body.assert_not_called()
                else:
                    self.assertEqual(len(handler._send.call_args[0][1]['records']), 1)

    def test_local_scan_budget_returns_a_short_reason_instead_of_unbounded_work(self):
        for _ in range(300):
            self.record(target={'name': 'unrelated'})
        with mock.patch('time.monotonic', side_effect=[0] + [1] * 100):
            with self.assertRaisesRegex(ValueError, 'Local change history took too long'):
                self.lookup()


if __name__ == '__main__':
    unittest.main()
