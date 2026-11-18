"""Optional startup identity and guarded KILL through real fake-client processes.

The SQL guard builder and editor state machine are production code. Only the
client's server answers, including a second Galera node, are synthetic.
"""
import os
import json
from pathlib import Path
import unittest
from unittest import mock

import test_editor_session as fixtures
from myflames.connector import ConnectorError
from myflames import dbops, editor_session as ed


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
        self.run_sql('SELECT 7 /*gate*/', wait=0)
        self.until(lambda: self.logged('tab1', 'SELECT 7 /*gate*/'), 'owned statement to start')
        session._send_kill(self.connection, session.connection_id)
        self.assertTrue(session.run.done.wait(5))
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
        self.run_sql('SELECT 7 /*gate*/', wait=0)
        self.until(lambda: self.logged('tab1', 'SELECT 7 /*gate*/'), 'owned statement to start')
        with mock.patch.dict(os.environ, FAKE_GCOMM_UUID='ffffffff-bbbb-cccc-dddd-eeeeeeeeeeee'):
            with self.assertRaisesRegex(ConnectorError, 'server could not be confirmed'):
                session._send_kill(self.connection, session.connection_id)
        self.assertFalse((self.state / 'kills.log').exists(), 'another Galera node received a KILL')
        session._send_kill(self.connection, session.connection_id)
        self.assertTrue(session.run.done.wait(5))
        self.assertTrue((self.state / 'kills.log').exists(), 'the original node must accept its own binding')
        batch = (self.state / 'helper-batches.log').read_text(encoding='utf-8')
        self.assertIn('information_schema.GLOBAL_STATUS', batch)
        self.assertIn(node_uuid, batch)

    def packets(self):
        return [json.loads(line) for line in (self.state / 'packets.log').read_text(encoding='utf-8').splitlines()]

    def route_packets(self, *routes):
        # Every COM_QUERY chooses a backend independently. Identity reads do
        # not pin the next packet. No production classifier/guard is mocked.
        (self.state / 'packet-routes.json').write_text(json.dumps(routes), encoding='utf-8')

    def test_confirmed_write_refuses_a_backend_changed_after_a_matching_probe(self):
        self.assertEqual(self.run_sql('SELECT 7')['status'], 'complete')
        sql = 'SET @owned = 17; SET @later = 23'
        review = self.call('/api/editor/run', sql=sql)
        self.assertEqual(review['status'], 'needs_confirmation')
        good = {'server_uuid': '11111111-2222-3333-4444-555555555555'}
        wrong = {'server_uuid': 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'}
        self.route_packets(good, good, wrong)  # probe, history boundary, first write
        before = len(self.packets())
        result = self.call('/api/editor/run', sql=sql, confirmed=True,
                           confirm_token=review['confirmation']['token'])
        self.assertEqual(result['status'], 'error', result)
        self.assertEqual([u['status'] for u in result['run']['statements']], ['not_run', 'not_run'])
        self.assertEqual(result['run']['counts']['run'], 0)
        self.assertIn('No requested statements were sent', result['run']['message'])
        self.assertIsNone(result['run']['error']['position'])
        packets = self.packets()[before:]
        self.assertEqual(len(packets), 3, packets)  # no after-read or later write
        protected = packets[-1][2]
        self.assertIn('SET @myflames_target = ', protected)
        self.assertIn('SET @owned = 17', protected)
        self.assertNotIn('SET @later = 23', protected)
        self.assertNotIn('SET @owned = 17', '\n'.join(self.session_log()))
        record = self.store.audit_records()[0]
        self.assertEqual((record['status'], record['outcome']), ('refused', 'identity_mismatch'))
        self.assertTrue(record['finished_at'])

    def test_every_write_is_guarded_and_identity_refusal_overrides_continue(self):
        self.assertEqual(self.run_sql('SELECT 7')['status'], 'complete')
        sql = 'SET @first = 1; SET @second = 2; SET @last = 3'
        review = self.call('/api/editor/run', sql=sql, on_error='continue')
        good = {'server_uuid': '11111111-2222-3333-4444-555555555555'}
        wrong = {'server_uuid': 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'}
        self.route_packets(good, wrong)  # probe A, boundary B, write A, write B
        before = len(self.packets())
        result = self.call('/api/editor/run', sql=sql, on_error='continue', confirmed=True,
                           confirm_token=review['confirmation']['token'])
        self.assertEqual([u['status'] for u in result['run']['statements']], ['ok', 'not_run', 'not_run'])
        self.assertEqual(result['run']['counts']['run'], 1)
        self.assertEqual(len([r for r in result['run']['results'] if r['type'] == 'ok']), 1,
                         'the guard OK must not be a user result')
        packets = self.packets()[before:]
        self.assertEqual(len(packets), 4, packets)
        self.assertTrue(all('SET @myflames_target = ' in p[2] for p in packets[2:]))
        self.assertNotIn('SET @last = 3', '\n'.join(p[2] for p in packets))
        record = self.store.audit_records()[0]
        self.assertEqual((record['status'], record['outcome']), ('error', 'identity_mismatch'))
        self.assertEqual(record['after']['statements_run'], 1)

    def test_confirmed_commit_and_its_guard_share_the_routed_packet(self):
        self.assertEqual(self.run_sql('START TRANSACTION')['status'], 'complete')
        review = self.call('/api/editor/commit')
        self.assertEqual(review['status'], 'needs_confirmation')
        self.route_packets({'server_uuid': '11111111-2222-3333-4444-555555555555'},
                           {'server_uuid': 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'})
        before = len(self.packets())
        result = self.call('/api/editor/commit', confirmed=True, confirm_token=review['confirmation']['token'])
        self.assertEqual(result['run']['statements'][0]['status'], 'not_run', result)
        packets = self.packets()[before:]
        self.assertEqual(len(packets), 2, packets)
        self.assertIn('SET @myflames_target = ', packets[-1][2])
        self.assertIn(ed.TAG + 'COMMIT', packets[-1][2])
        self.assertNotIn(ed.TAG + 'COMMIT', '\n'.join(self.session_log()))
        self.assertEqual(self.store.audit_records()[0]['outcome'], 'identity_mismatch')

    def test_same_server_backend_session_switch_refuses_confirmed_write(self):
        self.assertEqual(self.run_sql('SELECT 7')['status'], 'complete')
        session = self.registry.get('tab1')
        sql = 'SET @must_stay_in_this_session = 1'
        review = self.call('/api/editor/run', sql=sql)
        self.route_packets({'connection_id': session.connection_id},
                           {'connection_id': session.connection_id + 1},
                           {'connection_id': session.connection_id + 1})
        result = self.call('/api/editor/run', sql=sql, confirmed=True,
                           confirm_token=review['confirmation']['token'])
        self.assertEqual(result['run']['counts']['run'], 0, result)
        self.assertEqual(result['run']['statements'][0]['status'], 'not_run')
        packet = self.packets()[-1][2]
        self.assertIn('CONNECTION_ID() <> %d' % session.connection_id, packet)
        self.assertIn(sql, packet)
        self.assertNotIn(sql, '\n'.join(self.session_log()))
        self.assertEqual(self.store.audit_records()[0]['outcome'], 'identity_mismatch')

    def test_stop_refuses_recycled_id_running_another_clients_sql_on_the_same_server(self):
        self.run_sql('SELECT 7 /*gate*/', wait=0)
        self.until(lambda: self.logged('tab1', 'SELECT 7 /*gate*/'), 'owned statement to start')
        session = self.registry.get('tab1')
        # Same id and server, different backend session selected by the proxy.
        active = self.state / ('active-%d' % session.connection_id)
        active.write_text('/*another-client*/ SELECT SLEEP(60)', encoding='utf-8')
        try:
            stopped = self.call('/api/editor/stop')
            self.assertFalse(stopped['stopped'], stopped)
            self.assertIn('The server could not be confirmed. No KILL QUERY was sent.', stopped['message'])
            self.assertFalse((self.state / 'kills.log').exists())
            self.assertIsNone(session.run.current.kill_sent_at)
        finally:
            self.release()
            self.assertTrue(session.run.done.wait(5))

    def test_stop_refuses_a_different_tabs_token_and_a_token_inside_another_query(self):
        for actual in ('/*myflames:editor:%s*/ SELECT SLEEP(60)' % ('f' * 32),
                       "SELECT 'embedded %s'"):
            self.run_sql('SELECT 7 /*gate*/', wait=0)
            session = self.registry.get('tab1')
            active = self.state / ('active-%d' % session.connection_id)
            self.until(lambda: active.exists() and 'SELECT 7 /*gate*/' in active.read_text(encoding='utf-8'),
                       'owned statement to be visible')
            active.write_text(actual % session.statement_tag if '%s' in actual else actual, encoding='utf-8')
            try:
                stopped = self.call('/api/editor/stop')
                self.assertFalse(stopped['stopped'], stopped)
                self.assertFalse((self.state / 'kills.log').exists())
            finally:
                self.release()
                self.assertTrue(session.run.done.wait(5))

    def test_stop_does_not_mistake_its_own_guard_for_the_target_statement(self):
        self.run_sql('SELECT 7 /*gate*/', wait=0)
        self.until(lambda: self.logged('tab1', 'SELECT 7 /*gate*/'), 'owned statement to start')
        session = self.registry.get('tab1')
        try:
            # A multiplexing proxy can lend the helper the old backend id.
            # Its own token-prefixed guard must not satisfy the target check.
            with mock.patch.dict(os.environ, FAKE_KILL_CONTROL_ID=str(session.connection_id)):
                stopped = self.call('/api/editor/stop')
            self.assertFalse(stopped['stopped'], stopped)
            self.assertFalse((self.state / 'kills.log').exists())
        finally:
            self.release()
            self.assertTrue(session.run.done.wait(5))

    def test_missing_processlist_row_and_denied_processlist_never_send_kill(self):
        self.run_sql('SELECT 7 /*gate*/', wait=0)
        self.until(lambda: self.logged('tab1', 'SELECT 7 /*gate*/'), 'owned statement to start')
        session = self.registry.get('tab1')
        active = self.state / ('active-%d' % session.connection_id)
        active.unlink()
        try:
            stopped = self.call('/api/editor/stop')
            self.assertFalse(stopped['stopped'], stopped)
            self.assertIn('server could not be confirmed', stopped['message'])
            self.assertFalse((self.state / 'kills.log').exists())
            for code in (1054, 1142, 1146, 1227):
                error = ConnectorError('ERROR %d (42000) at line 2: processlist unavailable' % code)
                with self.subTest(code=code), mock.patch.object(ed.MySQLConnection, 'run', side_effect=error):
                    stopped = self.call('/api/editor/stop')
                    self.assertFalse(stopped['stopped'], stopped)
                    self.assertIn('The server could not be confirmed. No KILL QUERY was sent.', stopped['message'])
                    self.assertFalse((self.state / 'kills.log').exists())
        finally:
            self.release()
            self.assertTrue(session.run.done.wait(5))

    def test_guard_delimiter_collision_refuses_before_sending_any_user_sql(self):
        self.assertEqual(self.run_sql('SELECT 7')['status'], 'complete')
        sql = "SET @reserved = '$$ // __mf_end_1__ __mf_end_2__'"
        review = self.call('/api/editor/run', sql=sql)
        with self.assertRaises(ed.EditorError) as caught:
            self.call('/api/editor/run', sql=sql, confirmed=True,
                      confirm_token=review['confirmation']['token'])
        self.assertEqual(caught.exception.code, 'identity_unavailable')
        self.assertNotIn(sql, '\n'.join(self.session_log()))
        self.assertEqual(self.store.audit_records()[0]['status'], 'refused')
        self.assertFalse(self.registry.get('tab1').run_lock.locked())

    def test_all_packets_keep_comments_and_private_token_out_of_user_results(self):
        result = self.run_sql('SET @owned = 17; SELECT @owned')
        self.assertEqual(self.rows(result), [['17']])
        self.assertEqual([r['type'] for r in result['run']['results']], ['ok', 'rows'])
        self.assertEqual(result['run']['counts']['rows'], 1)
        self.assertEqual(result['run']['counts']['affected_rows'], 0)
        session = self.registry.get('tab1')
        packets = self.packets()
        self.assertTrue(all(packet[2].startswith(session.statement_tag) for packet in packets))
        self.assertNotIn(session.statement_tag, json.dumps(result))
        self.assertNotIn(session.statement_tag, json.dumps(self.store.audit_records()))
        self.assertNotIn('myflames_guard_ok_', json.dumps(result))
        for call in self.processes.call_args_list:
            if '--xml' in call[0][0]:
                self.assertIn('--comments', call[0][0])

    def test_statement_1242_after_acknowledgement_keeps_its_statement_and_continue_mode(self):
        self.inject_answers('''        if upper == "SET @STATEMENT_ERROR = 1":
            self.error(line, 1242, "21000", "Subquery returns more than 1 row")
            return
''')
        for engine in ('mysql', 'mariadb'):
            with self.subTest(engine=engine), mock.patch.dict(os.environ, FAKE_ENGINE=engine):
                result = self.run_sql('SET @statement_error = 1; SELECT 17', tab=engine,
                                      on_error='continue')
                self.assertEqual(result['status'], 'error', result)
                self.assertEqual([s['status'] for s in result['run']['statements']], ['error', 'ok'])
                self.assertEqual(result['run']['error']['statement'], 0)
                self.assertEqual(result['run']['error']['code'], 1242)
                self.assertEqual(self.rows(result), [['17']])
                self.assertFalse(result['session']['ended'])
                record = next(r for r in self.store.audit_records() if r['id'] == result['run']['audit_id'])
                self.assertEqual((record['status'], record['outcome']), ('error', 'error'))

    def test_marker_shaped_user_row_is_preserved_after_the_private_acknowledgement(self):
        public = 'myflames_guard_ok_' + 'f' * 32 + '_0'
        self.inject_answers('''        if upper == "SELECT ACK_LOOKALIKE()":
            self.result(statement, ["public"], [[%r]])
            return
''' % public)
        result = self.run_sql('SELECT ACK_LOOKALIKE()')
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(self.rows(result), [[public]])
        self.assertEqual(len(result['run']['results']), 1)
        self.assertEqual(result['run']['counts']['rows'], 1)

    def test_late_stdout_acknowledgement_does_not_turn_a_statement_error_into_identity_refusal(self):
        self.maria_client()
        self.inject_answers('''        if upper == "SET @STATEMENT_ERROR = 1":
            self.error(line, 1242, "21000", "Subquery returns more than 1 row")
            return
''')
        # MariaDB can deliver stderr before flushing stdout. Hold the private
        # result until after the client's end line, then deliver it late.
        self.rewrite_client(lambda source: source.replace(
            '            self.result(statement[:marker.end() - 2], [marker.group(1)], [[marker.group(1)]])',
            '            saved_flush = self.flush\n'
            '            self.flush = lambda: None\n'
            '            self.result(statement[:marker.end() - 2], [marker.group(1)], [[marker.group(1)]])\n'
            '            self.flush = saved_flush').replace(
                '                    client.error(number, message=MARKER)',
                '                    client.error(number, message=MARKER)\n'
                '                    time.sleep(0.1)\n'
                '                    client.flush()'))
        result = self.run_sql('SET @statement_error = 1; SELECT 17', on_error='continue')
        self.assertEqual(result['status'], 'error', result)
        self.assertEqual([s['status'] for s in result['run']['statements']], ['error', 'ok'])
        self.assertEqual(self.rows(result), [['17']])
        self.assertFalse(result['session']['ended'])

    def test_guard_read_error_is_identity_unavailable_and_preserves_the_server_reason(self):
        self.rewrite_client(lambda source: source.replace(
            '            if mismatch:\n',
            '            if "SET @guard_denied = 1" in statement:\n'
            '                self.error(line, 1142, "42000", "SELECT command denied for identity table")\n'
            '                return\n'
            '            if mismatch:\n'))
        for prefix in ('', 'SET @earlier = 1; '):
            with self.subTest(prefix=prefix):
                result = self.run_sql(prefix + 'SET @guard_denied = 1; SELECT 17', tab='denied-' + str(bool(prefix)),
                                      on_error='continue')
                self.assertEqual(result['status'], 'error', result)
                self.assertEqual([s['status'] for s in result['run']['statements']],
                                 (['ok'] if prefix else []) + ['not_run', 'not_run'])
                self.assertIn('SELECT command denied for identity table', result['run']['message'])
                if prefix:
                    self.assertIn('Earlier statements ran', result['run']['message'])
                    self.assertNotIn('Nothing was sent', result['run']['message'])
                record = next(r for r in self.store.audit_records() if r['id'] == result['run']['audit_id'])
                self.assertEqual((record['status'], record['outcome'], record['error_code']),
                                 ('error' if prefix else 'refused', 'identity_unavailable', 1142))
                self.assertEqual(result['run']['error']['position'], None)
                self.assertEqual(result['run']['error']['statement'], 1 if prefix else 0)

    def test_an_unreadable_guard_reply_and_client_that_cannot_drain_keep_an_unknown_outcome(self):
        self.rewrite_client(lambda source: source.replace(
            '            self.result(statement[:marker.end() - 2], [marker.group(1)], [[marker.group(1)]])',
            '            if "SET @guard_hangs = 1" in statement:\n'
            '                self.guard_hangs = True\n'
            '                self.error(line, 1064, "42000", "Packet rejected without acknowledgement")\n'
            '                return\n'
            '            self.result(statement[:marker.end() - 2], [marker.group(1)], [[marker.group(1)]])').replace(
                '                    client.error(number, message=MARKER)',
                '                    client.error(number, message=MARKER)\n'
                '                    if getattr(client, "guard_hangs", False):\n'
                '                        if os.environ.get("FAKE_GUARD_EXIT") == "1":\n'
                '                            os._exit(7)\n'
                '                        client.gate(honor_kill=False)'))
        for exit_early in ('0', '1'):
            with self.subTest(exit_early=exit_early), mock.patch.dict(os.environ, FAKE_GUARD_EXIT=exit_early):
                result = self.run_sql('SET @guard_hangs = 1; SELECT 17', tab='unreadable-' + exit_early,
                                      on_error='continue')
                self.assertEqual(result['status'], 'session_ended', result)
                self.assertEqual([s['status'] for s in result['run']['statements']], ['session_ended', 'not_run'])
                record = next(r for r in self.store.audit_records() if r['id'] == result['run']['audit_id'])
                self.assertEqual((record['status'], record['outcome']), ('unknown', 'session_ended'))
                self.assertFalse((self.state / 'kills.log').exists())

    def test_processlist_table_is_selected_for_the_engine_and_version(self):
        session = ed.EditorSession('test', 'identity', {})
        for engine, version, table in (('mysql', '5.7.44', 'information_schema.PROCESSLIST'),
                                       ('mysql', '8.0.21', 'information_schema.PROCESSLIST'),
                                       ('mysql', '8.0.22', 'performance_schema.processlist'),
                                       ('mysql', '8.4.11', 'performance_schema.processlist'),
                                       ('mariadb', '11.4.13-MariaDB', 'information_schema.PROCESSLIST')):
            with self.subTest(engine=engine, version=version):
                session.engine, session.server_version = engine, version
                identity = ({'server_uuid': '11111111-2222-3333-4444-555555555555'} if engine == 'mysql' else
                            {'server_id': '1', 'hostname': 'fake-db', 'port': '3306'})
                guard = session._kill_statement_guard(dbops.target_session_guard(engine, identity), 41)[0]
                self.assertIn('FROM ' + table, guard)
                self.assertIn('LEFT(BINARY INFO, ', guard)
                self.assertIn('WHERE ID = 41', guard)

    def test_token_tagging_preserves_quoted_semicolons_and_tags_suffix_statements(self):
        session = ed.EditorSession('test', 'identity', {})
        sql = "SELECT 'a;b', `x;y`, /* keep ; */ 1; SELECT 2"
        tagged = session._tag_statement(sql)
        self.assertEqual(tagged.count(session.statement_tag), 2)
        self.assertEqual(tagged.replace('; ' + session.statement_tag, ';').replace(session.statement_tag, ''), sql)
        self.assertNotEqual(session.statement_tag, ed.EditorSession('other', 'identity', {}).statement_tag)

    def test_token_tagging_does_not_store_a_private_token_inside_routine_bodies(self):
        session = ed.EditorSession('test', 'identity', {})
        for sql in ('CREATE PROCEDURE p() BEGIN SELECT 1; SELECT 2; END',
                    '/* deployment */ CREATE TRIGGER tr BEFORE INSERT ON t FOR EACH ROW BEGIN SET @a = 1; END',
                    'CREATE OR REPLACE PACKAGE BODY p AS FUNCTION f RETURN INT IS BEGIN RETURN 1; END; END'):
            with self.subTest(sql=sql):
                self.assertEqual(session._tag_statement(sql), session.statement_tag + sql)

    def test_token_tagging_keeps_routine_bodies_intact_in_both_batch_orders(self):
        session = ed.EditorSession('test', 'identity', {})
        definitions = (
            'CREATE PROCEDURE p() BEGIN SELECT 1; SELECT 2; END',
            'CREATE DEFINER = `owner`@`host` FUNCTION f() RETURNS INT BEGIN RETURN 1; END',
            '/* deploy */ CREATE TRIGGER tr BEFORE INSERT ON t FOR EACH ROW BEGIN SET @a = 1; END',
            'CREATE EVENT e ON SCHEDULE EVERY 1 DAY DO BEGIN SELECT 1; SELECT 2; END',
            'CREATE OR REPLACE PACKAGE BODY p AS FUNCTION f RETURN INT IS BEGIN RETURN 1; END; END',
            '/*!50003 CREATE*/ PROCEDURE p() BEGIN SELECT 1; SELECT 2; END',
        )
        for definition in definitions:
            for before in (False, True):
                with self.subTest(definition=definition, before=before):
                    sql = 'SELECT 1; ' + definition if before else definition + '; SELECT 1'
                    units = ed.plan_statements('DELIMITER //\n' + sql + '//')
                    self.assertEqual(len(units), 1)
                    tagged = session._tag_statement(units[0]['text'])
                    expected = (session.statement_tag + 'SELECT 1; ' + session.statement_tag + ' ' + definition
                                if before else session.statement_tag + sql)
                    self.assertEqual(tagged, expected)
                    self.assertNotIn(session.statement_tag, tagged[tagged.index(definition) + len('CREATE'):])
                    self.assertEqual(session._tag_statement(sql + ';'), expected + ';')

    def test_statement_history_strips_only_this_sessions_tag_before_exact_matching(self):
        from myflames.editor_insights import FIELDS
        session = ed.EditorSession('test', 'identity', {})
        sql = 'SELECT 7'
        units = ed.plan_statements(sql)
        ed.classify_units(units, True, sql)
        units[0]['status'] = 'ok'
        run = ed.Run(units, 1000, {}, sql, ed.Offsets(sql), 'all')
        row = dict({key: '0' for key in FIELDS}, event_id='2', nesting_event_type=None, nesting_event_level='0',
                   sql_text=session.statement_tag + sql)
        helper = dict(row, event_id='3', sql_text=session.statement_tag + ed.TAG + 'SELECT 7')
        foreign = dict(row, event_id='4', sql_text='/*myflames:editor:%s*/ SELECT 7' % ('f' * 32))
        session._attribute_history(run, [row, helper, foreign], 1)
        self.assertTrue(units[0]['server_stats']['available'])
        self.assertEqual(units[0]['server_stats']['event_id'], '2')

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
