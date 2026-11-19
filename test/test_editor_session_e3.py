"""Startup fallback through the identity tests' statement-routing transport.

Only backend answers are synthetic. The subprocess, framing, target guards,
planner, confirmation tokens, write policy and audit all use production code.
"""
import os
import unittest

import test_editor_session as fixtures
import test_editor_session_identity as identity_fixtures
import test_editor_session_e2 as setup_fixtures
from myflames import editor_session as ed
from myflames.connector import ConnectorError


@unittest.skipIf(os.name == 'nt', 'the fake client is a POSIX script')
class EditorRoutedStartupTests(unittest.TestCase):
    engine = 'mysql'
    setUp = fixtures.ProcessTests.setUp
    call = fixtures.ProcessTests.call
    run_sql = fixtures.ProcessTests.run_sql
    rows = fixtures.ProcessTests.rows
    session_log = fixtures.ProcessTests.session_log
    save_profile = fixtures.ProcessTests.save_profile
    until = fixtures.ProcessTests.until
    logged = fixtures.ProcessTests.logged
    release = fixtures.ProcessTests.release
    rewrite_client = identity_fixtures.EditorIdentityTests.rewrite_client
    inject_answers = identity_fixtures.EditorIdentityTests.inject_answers
    packets = identity_fixtures.EditorIdentityTests.packets
    route_packets = identity_fixtures.EditorIdentityTests.route_packets
    route_matching = setup_fixtures.EditorSetupPacketTests.route_matching

    def route_after_identity(self, *backends):
        # Handshake and identity packet use A. All later packets use B (or
        # later multiplexed sessions), without cycling back through startup.
        self.rewrite_client(lambda text: text.replace('routes[count % len(routes)]',
                                                      'routes[min(count, len(routes) - 1)]'))
        self.route_packets({}, {}, *backends)

    def split_proxy_packets(self, enabled=True):
        """Model the lab: a user-variable SET locks this frontend to HG30.

        SELECT packets route to HG32 and fail with 9006 once locked. The
        lock belongs to the client process, not the shared fake server state.
        Only a new process starts unlocked; no later read clears the lock.
        """
        source = '        statement = re.sub(r"/\\*myflames:editor:[0-9a-f]{32}\\*/ ", "", wire)'
        self.rewrite_client(lambda text: text.replace(source, source + '''
        if os.path.exists(os.path.join(STATE, "proxy-split")):
            if statement.startswith("SET @myflames_target = "):
                self.proxy_hostgroup = 30
                self.route_connection_id = self.conn_id + 10000
            elif (getattr(self, "proxy_hostgroup", None) == 30 and
                  statement.replace("/*myflames:editor*/ ", "", 1).lstrip().upper().startswith("SELECT")):
                self.error(line, 9006, "HY000",
                           "ProxySQL Error: connection is locked to hostgroup 30 but trying to reach hostgroup 32")
                return
'''))
        if enabled:
            (self.state / 'proxy-split').write_text('1')

    def client_starts(self):
        # Discovery/version/help probes do not open a server session.
        return [call for call in self.processes.call_args_list if '--xml' in call[0][0]]

    def assert_fresh_unbound_client(self):
        starts = self.client_starts()
        self.assertEqual(len(starts), 2, 'startup may replace its client only once')
        self.assertFalse(os.path.exists(starts[0][1]['env']['HOME']), 'old client HOME was not cleaned up')
        packets = self.packets()
        clients = list(dict.fromkeys(p[0] for p in packets))
        self.assertEqual(len(clients), 2)
        first = [p for p in packets if p[0] == clients[0]]
        second = [p for p in packets if p[0] == clients[1]]
        self.assertIn('SET @myflames_target = ', first[-1][2], 'no read may follow the refused packet')
        self.assertTrue(all('SET @myflames_target = ' not in p[2] for p in second))
        self.assertEqual(second[0][1], 2, 'the fresh client starts its own protocol line numbers')
        self.assertIn('CONNECTION_ID()', second[0][2])
        self.assertIn('SHOW GLOBAL VARIABLES', second[1][2])

    def assert_reads_only(self):
        self.save_profile(type='production')
        opened = self.call('/api/editor/open')
        self.assertEqual(opened['session']['state'], 'idle', opened)
        self.assertFalse(opened['session']['ended'])
        self.assertTrue(opened['session']['safe_updates'])
        self.assertIsNone(opened['session']['in_transaction'])
        self.assertIsNone(opened['session']['transaction_source'])
        self.assertIn('different servers or server sessions', opened['session']['transaction_note'])
        self.assertIn('changes, database switching and Stop', opened['session']['transaction_note'])
        session = self.registry.get('tab1')
        self.assertIsNone(session.server_identity)
        self.assertIsNone(session._kill_identity)
        self.assertIsNone(session.proc.poll())
        self.assertEqual(self.rows(self.run_sql('SELECT 7')), [['7']])

        # The production safe-updates SET and its successful readback are
        # real framed packets, even when A's identity said no SET was needed.
        packets = [p[2] for p in self.packets()]
        hardening = next(i for i, p in enumerate(packets) if ed.SAFE_UPDATES_ON in p and
                         'SET @myflames_target = ' not in p)
        readback = next(i for i, p in enumerate(packets[hardening + 1:], hardening + 1)
                        if 'AS database_name' in p)
        user_read = next(i for i, p in enumerate(packets) if p.endswith('SELECT 7'))
        self.assertLess(hardening, readback)
        self.assertLess(readback, user_read)
        self.assertTrue(all('SET @myflames_target = ' not in p for p in packets[hardening:]))

        for sql in ('DELETE FROM t WHERE id = 1', 'SET @must_not_run = 17', 'BEGIN'):
            with self.subTest(refused_sql=sql):
                review = self.call('/api/editor/run', sql=sql)
                self.assertEqual(review['status'], 'needs_confirmation', review)
                before = len(self.packets())
                with self.assertRaisesRegex(ed.EditorError, 'The server could not be confirmed') as caught:
                    self.call('/api/editor/run', sql=sql, confirmed=True,
                              confirm_token=review['confirmation']['token'])
                self.assertEqual(caught.exception.code, 'identity_unavailable')
                self.assertEqual(len(self.packets()), before)
                self.assertNotIn(sql, self.session_log())

        for path, payload in (('/api/editor/use', {'database': 'target'}),
                              ('/api/editor/run', {'sql': 'USE `target`'}),
                              ('/api/editor/rollback', {})):
            with self.subTest(refused_action=path, payload=payload):
                before = len(self.packets())
                with self.assertRaisesRegex(ed.EditorError, 'The server could not be confirmed') as caught:
                    self.call(path, **payload)
                self.assertEqual(caught.exception.code, 'identity_unavailable')
                self.assertEqual(len(self.packets()), before)

        # Exercise the public Stop action while a real subprocess is blocked
        # on a user read. It must refuse before even spawning a KILL helper.
        self.run_sql('SELECT 7 /*gate*/', wait=0)
        try:
            self.until(lambda: self.logged('tab1', 'SELECT 7 /*gate*/'), 'routed read at the gate')
            before, processes = len(self.packets()), self.processes.call_count
            stopped = self.call('/api/editor/stop')
            self.assertFalse(stopped['stopped'], stopped)
            self.assertIn('The server could not be confirmed', stopped['message'])
            self.assertIn('No KILL QUERY was sent', stopped['message'])
            self.assertEqual(len(self.packets()), before)
            self.assertEqual(self.processes.call_count, processes)
            self.assertFalse((self.state / 'kills.log').exists())
            self.assertFalse(session.ended)
        finally:
            self.release()
        self.assertTrue(session.run.done.wait(10), 'released read did not finish')
        self.assertEqual(self.rows(self.run_sql('SELECT 8')), [['8']])
        self.assertIsNone(session.server_identity, 'an ordinary read must not recreate a binding')
        self.assertIsNone(session._kill_identity)
        self.assertEqual(session.status()['transaction_note'], opened['session']['transaction_note'])
        self.assert_fresh_unbound_client()

    def test_proxy_user_variable_hostgroup_lock_requires_a_fresh_client(self):
        self.split_proxy_packets()
        self.assert_reads_only()

    def test_proxy_development_startup_probe_retries_on_a_fresh_client(self):
        self.split_proxy_packets()
        self.save_profile(type='development')
        opened = self.call('/api/editor/open')
        self.assertEqual(opened['session']['state'], 'idle')
        self.assertFalse(opened['session']['safe_updates'])
        self.assertEqual(self.rows(self.run_sql('SELECT 7')), [['7']])
        self.assert_fresh_unbound_client()

    def test_proxy_first_read_runs_without_a_review_for_unknown_transaction_state(self):
        self.split_proxy_packets()
        self.save_profile(type='development')
        sql = 'SELECT @@hostname AS host, CONNECTION_ID() AS id'
        self.inject_answers('''        if upper == "SELECT @@HOSTNAME AS HOST, CONNECTION_ID() AS ID":
            self.result(statement, ["host", "id"], [["fake-db", str(self.route_connection_id)]])
            return
''')
        result = self.call('/api/editor/run', sql=sql)
        session = self.registry.get('tab1')
        safety = {'type': 'development', 'read_only': False, 'profile_name': 'dev'}
        before = ed._decision(ed._plan_run({'sql': sql}, None), safety)
        after = ed._decision(ed._plan_run({'sql': sql}, session), safety, session)
        self.assertEqual({key for key in before if before[key] != after[key]}, {'in_transaction'})
        self.assertIs(before['in_transaction'], False)
        self.assertIsNone(after['in_transaction'])
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(self.rows(result), [['fake-db', str(session.connection_id)]])
        self.assertEqual(self.session_log().count(sql), 1)
        self.assertFalse(self.store.audit_records(), 'a read needs no write review')
        self.assert_fresh_unbound_client()

    def test_proxy_fake_keeps_reads_locked_until_client_exit(self):
        self.split_proxy_packets(enabled=False)
        self.call('/api/editor/open')
        session = self.registry.get('tab1')
        (self.state / 'proxy-split').write_text('1')
        # Deliberately bypass _check_helper_guard only to validate the fake's
        # transport behavior. The application would close this client here.
        refused = session._send('helper', ed.TAG + 'SELECT 1', guard=session._write_session_guard())
        self.assertTrue(session._wait(lambda: refused.marker, 10))
        self.assertEqual(refused.real_errors()[0]['code'], 1242)
        for _ in range(2):
            read = session._helper(ed.TAG + 'SELECT 7')
            self.assertEqual(read.real_errors()[0]['code'], 9006)
            self.assertEqual(read.real_errors()[0]['sqlstate'], 'HY000')
            self.assertEqual(read.real_errors()[0]['line'], read.first_line)
        self.assertEqual(len(self.client_starts()), 1)
        old = session.proc
        self.call('/api/editor/close', discard=True)
        self.assertIsNotNone(old.poll())

    def test_fresh_client_setup_failure_never_starts_a_third_client(self):
        self.split_proxy_packets()
        self.inject_answers('''        if upper == "SET SESSION SQL_SAFE_UPDATES = 1":
            self.error(line, int(os.environ["FAKE_RETRY_ERROR"]), "HY000", "Fresh client setup failed")
            return
''')
        for code in (1242, 1142, 2013):
            with self.subTest(code=code):
                os.environ['FAKE_RETRY_ERROR'] = str(code)
                before = len(self.client_starts())
                with self.assertRaises(ConnectorError):
                    self.call('/api/editor/open', tab='failure-%d' % code)
                self.assertEqual(len(self.client_starts()) - before, 2)
                self.assertIsNone(self.registry.get('failure-%d' % code))
                self.assertTrue(all(p.poll() is not None for p in ed._REGISTRY_PROCS))
        self.assertFalse(any(p[2].endswith('SELECT 7') for p in self.packets()))

    def test_different_server_after_identity_keeps_reads_and_refuses_changes(self):
        self.route_after_identity({'server_uuid': 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'})
        self.assert_reads_only()

    def test_same_server_different_sessions_keep_reads_and_refuse_changes(self):
        self.route_after_identity({'connection_id': 9001}, {'connection_id': 9002})
        self.assert_reads_only()

    def test_state_readback_refusal_reapplies_already_satisfied_safe_updates(self):
        self.rewrite_client(lambda text: text.replace(
            '"sql_safe_updates": "1" if self.safe_updates else "0",',
            '"sql_safe_updates": "1" if self.route_count <= 2 else ("1" if self.safe_updates else "0"),'))
        self.route_after_identity({'connection_id': 9001})
        self.assert_reads_only()
        setup = [p[2] for p in self.packets() if ed.SAFE_UPDATES_ON in p[2]]
        self.assertEqual(len(setup), 1, 'only the unguarded fallback needs a SET')
        self.assertNotIn('SET @myflames_target = ', setup[0])

    def test_read_only_verification_refusal_reapplies_and_verifies_hardening(self):
        self.save_profile(type='production', read_only=True)
        self.route_matching('SHOW SESSION VARIABLES', 'connection_id')
        opened = self.call('/api/editor/open')
        self.assertFalse(opened['session']['ended'])
        self.assertTrue(opened['session']['safe_updates'])
        self.assertTrue(opened['session']['read_only_transactions'])
        self.assertIsNone(self.registry.get('tab1')._kill_identity)
        self.assert_fresh_unbound_client()
        packets = [p[2] for p in self.packets()]
        for needle in ('SET SESSION TRANSACTION READ ONLY', 'SET SESSION sql_safe_updates = 1',
                       'SHOW SESSION VARIABLES'):
            with self.subTest(startup_packet=needle):
                matching = [p for p in packets if needle in p]
                self.assertEqual(len(matching), 2)
                self.assertIn('SET @myflames_target = ', matching[0])
                self.assertNotIn('SET @myflames_target = ', matching[1])

    def test_performance_schema_probe_refusal_keeps_the_tab_open(self):
        if self.engine != 'mysql':
            self.skipTest('MariaDB reads @@in_transaction instead of a Performance Schema probe')
        self.route_matching('FROM performance_schema.setup_consumers', 'connection_id')
        self.assert_reads_only()

    def test_routed_fallback_cannot_accept_ineffective_safe_updates(self):
        self.route_after_identity({'connection_id': 9001})
        self.inject_answers('''        if upper == "SET SESSION SQL_SAFE_UPDATES = 1":
            self.ok()
            return
''')
        with self.assertRaisesRegex(ConnectorError, 'safe-updates setting could not be verified'):
            self.run_sql('SELECT 7')
        self.assertFalse(any(p[2].endswith('SELECT 7') for p in self.packets()))
        self.assertTrue(all(p.poll() is not None for p in ed._REGISTRY_PROCS))
        self.assert_fresh_unbound_client()

    def test_routed_fallback_cannot_accept_failed_state_readback(self):
        self.route_after_identity({'connection_id': 9001})
        self.inject_answers('''        if "DATABASE() AS DATABASE_NAME" in upper:
            self.error(line, 1142, "42000", "State read denied")
            return
''')
        with self.assertRaisesRegex(ConnectorError, 'Could not verify session setup'):
            self.run_sql('SELECT 7')
        self.assertFalse(any(p[2].endswith('SELECT 7') for p in self.packets()))
        self.assert_fresh_unbound_client()

    def test_startup_setting_server_error_does_not_downgrade(self):
        self.inject_answers('''        if upper == "SET SESSION SQL_SAFE_UPDATES = 1":
            self.error(line, 1142, "42000", "Safe updates denied")
            return
''')
        with self.assertRaisesRegex(ConnectorError, 'Could not prepare the session'):
            self.run_sql('SELECT 7')
        self.assertEqual(len(self.packets()), 3)

    def test_startup_setting_1242_with_late_acknowledgement_does_not_downgrade(self):
        self.inject_answers('''        if upper == "SET SESSION SQL_SAFE_UPDATES = 1":
            saved_flush = self.flush
            self.flush = lambda: None
            self.error(line, 1242, "21000", "Statement's own 1242")
            self.flush = saved_flush
            return
''')
        # Hold the ack beyond GUARD_REPLY_GRACE. Only EOF flushes stdout;
        # draining the closed client must preserve the statement's own error,
        # never turn an elapsed timer into evidence of a guard refusal.
        self.rewrite_client(lambda source: source.replace(
            '            self.result(statement[:marker.end() - 2], [marker.group(1)], [[marker.group(1)]])',
            '            saved_flush = self.flush\n'
            '            self.flush = lambda: None\n'
            '            self.result(statement[:marker.end() - 2], [marker.group(1)], [[marker.group(1)]])\n'
            '            self.flush = saved_flush').replace(
            '                    client.error(number, message=MARKER)',
            '                    saved_flush = client.flush\n'
            '                    client.flush = lambda: None\n'
            '                    client.error(number, message=MARKER)\n'
            '                    client.flush = saved_flush'))
        with self.assertRaises(ConnectorError):
            self.run_sql('SELECT 7')
        self.assertEqual(len(self.client_starts()), 1)
        self.assertEqual(len(self.packets()), 3, 'no drain read or retry follows an acknowledged error')
        self.assertFalse(any(ed.SAFE_UPDATES_ON in p[2] and 'SET @myflames_target = ' not in p[2]
                             for p in self.packets()))

    def test_startup_setting_lost_connection_does_not_downgrade(self):
        self.inject_answers('''        if upper == "SET SESSION SQL_SAFE_UPDATES = 1":
            self.error(line, 2013, "HY000", "Lost connection during setup")
            return
''')
        with self.assertRaisesRegex(ConnectorError, 'Could not verify session setup'):
            self.run_sql('SELECT 7')
        self.assertEqual(len(self.packets()), 3)

    def test_denied_guard_is_not_a_routing_refusal(self):
        self.rewrite_client(lambda source: source.replace(
            '            if mismatch:\n',
            '            if "SET SESSION sql_safe_updates = 1" in statement:\n'
            '                self.error(line, 1142, "42000", "Identity read denied")\n'
            '                return\n'
            '            if mismatch:\n'))
        with self.assertRaises(ed.EditorError) as caught:
            self.run_sql('SELECT 7')
        self.assertEqual(caught.exception.code, 'identity_unavailable')
        self.assertEqual(len(self.packets()), 3)


class MariaDBEditorRoutedStartupTests(EditorRoutedStartupTests):
    engine = 'mariadb'

    def route_packets(self, *routes):
        identity_fixtures.EditorIdentityTests.route_packets(
            self, *(dict(route, mismatch=True) if 'server_uuid' in route else route for route in routes))
