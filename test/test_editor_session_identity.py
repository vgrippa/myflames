"""Optional startup identity and guarded KILL through real fake-client processes.

The SQL guard builder and editor state machine are production code. Only the
client's server answers, including a second Galera node, are synthetic.
"""
import os
from pathlib import Path
import unittest
from unittest import mock

import test_editor_session as fixtures
from myflames.connector import ConnectorError


@unittest.skipIf(os.name == 'nt', 'the fake client is a POSIX script')
class EditorIdentityTests(unittest.TestCase):
    engine = 'mysql'
    setUp = fixtures.ProcessTests.setUp
    call = fixtures.ProcessTests.call
    run_sql = fixtures.ProcessTests.run_sql
    rows = fixtures.ProcessTests.rows
    session_log = fixtures.ProcessTests.session_log
    until = fixtures.ProcessTests.until
    logged = fixtures.ProcessTests.logged
    release = fixtures.ProcessTests.release

    def rewrite_client(self, callback):
        script = Path(os.environ['PATH'].split(os.pathsep)[0]) / 'mysql'
        script.write_text(callback(script.read_text(encoding='utf-8')), encoding='utf-8')

    def inject_answers(self, source):
        self.rewrite_client(lambda text: text.replace('        upper = text.upper()\n',
                                                      '        upper = text.upper()\n' + source))

    def assert_usable_without_kill(self):
        result = self.run_sql('SELECT 7')
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(self.rows(result), [['7']])
        session = self.registry.get('tab1')
        with self.assertRaisesRegex(ConnectorError, 'server could not be confirmed'):
            session._send_kill(self.connection, session.connection_id)
        self.assertFalse((self.state / 'kills.log').exists())
        return session

    def test_denied_optional_variables_do_not_prevent_opening_or_reading(self):
        self.inject_answers('''        if upper.startswith("SHOW GLOBAL VARIABLES WHERE "):
            self.error(line, 1142, "42000", "Proxy does not expose optional variables")
            return
''')
        self.assert_usable_without_kill()

    def test_changed_connection_id_during_optional_reads_keeps_the_tab_usable(self):
        self.inject_answers('''        if upper == "SELECT CONNECTION_ID() AS CONNECTION_ID":
            self.result(statement, ["connection_id"], [[str(self.conn_id + 1)]])
            return
''')
        self.assert_usable_without_kill()

    def test_incomplete_initial_identity_keeps_the_tab_usable(self):
        self.rewrite_client(lambda text: text.replace('"hostname": "fake-db"', '"hostname": None'))
        session = self.assert_usable_without_kill()
        self.assertIsNone(session.server_identity)

    def test_invalid_uuid_keeps_the_tab_usable(self):
        self.rewrite_client(lambda text: text.replace('11111111-2222-3333-4444-555555555555', 'invalid-uuid'))
        self.assert_usable_without_kill()

    def maria_client(self, wsrep=False, node_uuid='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'):
        self.rewrite_client(lambda text: text.replace('ENGINE = os.environ.get("FAKE_ENGINE", "mysql")',
                                                      'ENGINE = "mariadb"').replace(
            '"11.4.13-MariaDB-fake" if ENGINE == "mariadb" else "8.4.11-fake"',
            '"11.8.9", "version_comment": "Private database build"'))
        if wsrep:
            self.rewrite_client(lambda text: text.replace('[["wsrep_on", "OFF"]]', '[["wsrep_on", "ON"]]'))
            self.inject_answers('''        if upper == "SHOW GLOBAL STATUS WHERE VARIABLE_NAME = 'WSREP_GCOMM_UUID'":
            self.result(statement, ["Variable_name", "Value"], [["wsrep_gcomm_uuid", %r]])
            return
''' % node_uuid)

    def test_custom_mariadb_version_and_comment_use_absence_of_server_uuid(self):
        self.maria_client()
        result = self.run_sql('SELECT 7')
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(result['session']['engine'], 'mariadb')
        self.assertEqual(result['session']['server_product'], 'MariaDB')
        self.assertFalse(any('@@global.server_uuid' in sql for sql in self.session_log()))
        session = self.registry.get('tab1')
        session._send_kill(self.connection, session.connection_id)
        self.assertTrue((self.state / 'kills.log').exists())

    def test_galera_kill_checks_node_uuid_even_with_identical_server_names_ids_and_ports(self):
        node_uuid = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
        self.maria_client(wsrep=True, node_uuid=node_uuid)
        # Model a helper routed to a different node. Without the UUID clause
        # both nodes pass the id/hostname/port guard and a KILL would be sent.
        self.rewrite_client(lambda text: text.replace('def one_shot(sql):\n', '''def one_shot(sql):
    actual_uuid = os.environ.get("FAKE_GCOMM_UUID", %r)
    if "wsrep_gcomm_uuid" in sql and actual_uuid not in sql:
        sys.stderr.write("ERROR 1242 (21000) at line 2: Subquery returns more than 1 row\\n")
        return 1
''' % node_uuid))
        result = self.run_sql('SELECT 7')
        self.assertEqual(result['status'], 'complete', result)
        session = self.registry.get('tab1')
        self.assertEqual(session._kill_identity['wsrep_gcomm_uuid'], node_uuid)
        self.assertEqual({key: session._kill_identity[key] for key in ('server_id', 'hostname', 'port')},
                         {'server_id': '1', 'hostname': 'fake-db', 'port': '3306'})
        with mock.patch.dict(os.environ, FAKE_GCOMM_UUID='ffffffff-bbbb-cccc-dddd-eeeeeeeeeeee'):
            with self.assertRaisesRegex(ConnectorError, 'server could not be confirmed'):
                session._send_kill(self.connection, session.connection_id)
        self.assertFalse((self.state / 'kills.log').exists(), 'another Galera node received a KILL')
        session._send_kill(self.connection, session.connection_id)
        self.assertTrue((self.state / 'kills.log').exists(), 'the original node must accept its own binding')
        batch = (self.state / 'helper-batches.log').read_text(encoding='utf-8')
        self.assertIn('information_schema.GLOBAL_STATUS', batch)
        self.assertIn(node_uuid, batch)

    def test_missing_galera_uuid_keeps_the_tab_usable_but_refuses_kill(self):
        self.maria_client(wsrep=True, node_uuid=None)
        self.assert_usable_without_kill()

    def test_denied_galera_uuid_keeps_the_tab_usable_but_refuses_kill(self):
        self.maria_client(wsrep=True)
        self.inject_answers('''        if upper.startswith("SHOW GLOBAL STATUS WHERE "):
            self.error(line, 1227, "42000", "Status read denied")
            return
''')
        self.assert_usable_without_kill()

    def test_stop_and_close_with_missing_identity_never_send_kill(self):
        self.inject_answers('''        if upper.startswith("SHOW GLOBAL VARIABLES WHERE "):
            self.error(line, 1142, "42000", "Proxy does not expose optional variables")
            return
''')
        self.run_sql('SELECT 1 /*gate*/', wait=0)
        self.until(lambda: self.logged('tab1', 'SELECT 1 /*gate*/'), 'the statement to start')
        session = self.registry.get('tab1')
        try:
            result = self.call('/api/editor/stop')
            self.assertFalse(result['stopped'], result)
            self.assertIn('server could not be confirmed', result['message'])
            self.assertFalse((self.state / 'kills.log').exists())
        finally:
            self.call('/api/editor/close', discard=True)
            # Close removes the session from the registry. Wait here so its
            # history writer cannot outlive the temporary HOME's cleanup.
            self.assertTrue(session.run.done.wait(5), 'the closed run did not finish')
            for reader in session._readers:
                reader.join(5)
                self.assertFalse(reader.is_alive(), 'the closed client reader did not exit')
        self.assertFalse((self.state / 'kills.log').exists())
