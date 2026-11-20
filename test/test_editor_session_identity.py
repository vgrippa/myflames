"""Optional startup identity and guarded KILL through real fake-client processes.

The SQL guard builder and editor state machine are production code. Only the
client's server answers, including a second Galera node, are synthetic.
"""
import os
import json
import re
import subprocess
from pathlib import Path
import unittest
from unittest import mock

import test_editor_session as fixtures
from myflames.connector import ConnectorError
from myflames import dbops, editor_session as ed


@unittest.skipIf(os.name == 'nt', 'the fake client is a POSIX script')

def _read_if_present(path):
    """The fake client removes its active-statement file between polls;
    an exists() check followed by a read can race with that."""
    try:
        return path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return ''

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
        self.assertTrue(result['session']['safe_updates'])
        self.assertIn(ed.SAFE_UPDATES_ON, self.session_log())
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
        self.assert_writes_refused_without_identity()

    def test_startup_identity_fields_are_bound_in_one_routed_packet(self):
        first = '11111111-2222-3333-4444-555555555555'
        second = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
        self.rewrite_client(lambda text: text.replace('"hostname": "fake-db"',
            '"hostname": "host-" + getattr(self, "route_uuid", "1")[0]'))
        self.rewrite_client(lambda text: text.replace('routes[count % len(routes)]',
                                                      'routes[min(count, len(routes) - 1)]'))
        self.route_packets({'server_uuid': first}, {'server_uuid': second})
        self.assertEqual(self.run_sql('SELECT 7')['status'], 'complete')
        session = self.registry.get('tab1')
        self.assertEqual(session.server_identity['server_uuid'], second)
        self.assertEqual(session.server_identity['hostname'], 'host-a')
        packets = [packet[2] for packet in self.packets()]
        identity = [p for p in packets if 'SHOW GLOBAL VARIABLES' in p]
        self.assertEqual(len(identity), 1)
        self.assertIn('CONNECTION_ID() AS connection_id', identity[0])
        self.assertIn('@@server_id AS server_id', identity[0])
        self.assertIn('@@sql_safe_updates AS sql_safe_updates', identity[0])
        self.assertIn('SHOW GLOBAL STATUS', identity[0])
        self.assertIn('wsrep_provider', identity[0])
        self.assertEqual(session._kill_identity['server_uuid'], second)
        self.assertFalse(any(p.endswith('SELECT CONNECTION_ID() AS connection_id') for p in packets))

    def test_startup_settings_are_read_from_the_bound_backend(self):
        # The first handshake says safe updates is already enabled. The
        # complete binding reports it disabled, so setup must still send SET.
        self.rewrite_client(lambda text: text.replace(
            '"sql_safe_updates": "1" if self.safe_updates else "0",',
            '"sql_safe_updates": "1" if "myflames_editor" in names else "0",'))
        self.run_sql('SELECT 7')
        self.assertIn(ed.SAFE_UPDATES_ON, self.session_log())

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

    def test_startup_retains_wsrep_off_for_conditional_sql_and_packet_guard(self):
        self.maria_client()
        self.assertEqual(self.run_sql('SELECT 7')['status'], 'complete')
        session = self.registry.get('tab1')
        self.assertEqual(session._kill_identity.get('wsrep_on'), 'OFF')
        self.assertIs(ed._lexer_session_context(session)['wsrep_on'], False)
        sql = 'SELECT 7 /*!99997 INTO OUTFILE \'/tmp/must-not-run\' */'
        plan = ed._plan_run({'sql': sql}, session)
        self.assertEqual(plan['units'][0]['code'], 'SELECT 7')
        result = self.call('/api/editor/run', sql=sql)
        self.assertEqual(result['status'], 'complete', result)
        packet = next(p[2] for p in self.packets() if sql in p[2])
        self.assertIn('@@SESSION.wsrep_on <=> 0', packet)
        self.assertIn('SET @myflames_target = ', packet)
        self.assertIn(sql, packet, 'the original SQL stays intact after the session guard')
        self.assertEqual(session._kill_identity.get('wsrep_on'), 'OFF', 'state refresh keeps the binding')

    def test_startup_retains_wsrep_on_and_refuses_special_marker(self):
        self.maria_client(wsrep=True)
        self.assertEqual(self.run_sql('SELECT 7')['status'], 'complete')
        session = self.registry.get('tab1')
        self.assertEqual(session._kill_identity.get('wsrep_on'), 'ON')
        self.assertIs(ed._lexer_session_context(session)['wsrep_on'], True)
        before = len(self.packets())
        result = self.call('/api/editor/run', sql='SELECT 7 /*!99997 INTO OUTFILE \'/tmp/must-not-run\' */')
        self.assertEqual(result['status'], 'invalid', result)
        self.assertIn('wsrep', result['message'])
        self.assertEqual(len(self.packets()), before)

    def test_missing_wsrep_variable_cannot_authorize_special_marker(self):
        self.maria_client()
        # Keep a row so the XML fake reports its column names, as the real
        # identity packet does. wsrep_on itself is absent, not assumed OFF.
        self.rewrite_client(lambda text: text.replace('[["wsrep_on", "OFF"]]', '[["wsrep_provider", "none"]]'))
        self.assertEqual(self.run_sql('SELECT 7')['status'], 'complete')
        session = self.registry.get('tab1')
        self.assertNotIn('wsrep_on', session._kill_identity)
        self.assertIsNone(ed._lexer_session_context(session)['wsrep_on'])
        before = len(self.packets())
        result = self.call('/api/editor/run', sql='SELECT 7 /*!99997 INTO OUTFILE \'/tmp/must-not-run\' */')
        self.assertEqual(result['status'], 'invalid', result)
        self.assertEqual(len(self.packets()), before)

    def test_first_run_replans_typed_drop_target_before_sending(self):
        # The provisional unknown-version reading includes TEMPORARY. The
        # started server skips it, revealing a DROP DATABASE typed target.
        # Real planner, classifier and gate throughout; only the client is fake.
        # Definitions require a known non-ORACLE session since K2. A plain
        # DROP still exercises provisional review and the changed typed target.
        definition = 'DROP /*!999999 TEMPORARY */ DATABASE prod'
        sql = 'DELIMITER //\n' + definition + '//'
        review = self.call('/api/editor/run', sql=sql)
        self.assertEqual(review['status'], 'needs_confirmation', review)
        self.assertIsNone(review['confirmation']['typed_target'])
        self.assertIsNone(self.registry.get('tab1'))
        unused = self.call('/api/editor/run', sql=sql)['confirmation']['token']
        old_token = review['confirmation']['token']
        changed = self.call('/api/editor/run', sql=sql, confirmed=True, confirm_token=old_token)
        self.assertEqual(changed['status'], 'needs_confirmation', changed)
        self.assertEqual(changed['confirmation']['typed_target'], 'prod')
        self.assertNotEqual(changed['confirmation']['token'], old_token)
        self.assertFalse(any(definition in p[2] for p in self.packets()), 'old review sends no user SQL')
        for token, target in ((old_token, 'prod'), (unused, 'prod'), (changed['confirmation']['token'], None),
                              (changed['confirmation']['token'], 'PROD')):
            with self.subTest(token=token, target=target):
                rejected = self.call('/api/editor/run', sql=sql, confirmed=True,
                                     confirm_token=token, typed_target=target)
                self.assertEqual(rejected['status'], 'needs_confirmation', rejected)
                self.assertFalse(any(definition in p[2] for p in self.packets()))
        result = self.call('/api/editor/run', sql=sql, confirmed=True, typed_target='prod',
                           confirm_token=changed['confirmation']['token'])
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(sum(definition in p[2] for p in self.packets()), 1)

    def test_first_run_replans_each_unit_even_when_overall_decision_is_unchanged(self):
        sql = 'SET @first = 1; /*!999999 SET @second = 2 */'
        provisional = ed._plan_run({'sql': sql}, None)
        review = self.call('/api/editor/run', sql=sql)
        self.assertEqual(review['status'], 'needs_confirmation', review)
        changed = self.call('/api/editor/run', sql=sql, confirmed=True,
                            confirm_token=review['confirmation']['token'])
        self.assertEqual(changed['status'], 'needs_confirmation', changed)
        session = self.registry.get('tab1')
        bound = ed._plan_run({'sql': sql}, session)
        safety = {'type': 'production', 'read_only': False}
        self.assertEqual(ed._decision(provisional, safety), ed._decision(bound, safety, session))
        self.assertEqual(provisional['units'][1]['kind'], 'write')
        self.assertEqual(bound['units'][1]['kind'], 'read')
        self.assertNotIn('SET @first = 1', self.session_log())
        self.assertFalse(any('/*!999999 SET @second = 2 */' in p[2] for p in self.packets()))
        result = self.call('/api/editor/run', sql=sql, confirmed=True,
                           confirm_token=changed['confirmation']['token'])
        self.assertEqual(result['status'], 'complete', result)

    def test_first_run_changed_to_read_runs_without_another_review(self):
        sql = '/*!999999 SET @must_not_run = 1 */'
        review = self.call('/api/editor/run', sql=sql)
        self.assertEqual(review['status'], 'needs_confirmation', review)
        unused = self.call('/api/editor/run', sql=sql)
        self.assertEqual(unused['status'], 'needs_confirmation', unused)
        changed = self.call('/api/editor/run', sql=sql, confirmed=True,
                            confirm_token=review['confirmation']['token'])
        self.assertEqual(changed['status'], 'complete', changed)
        self.assertEqual(changed['run']['statements'][0]['kind'], 'read')
        self.assertEqual(sum(p[2].endswith(sql) for p in self.packets()), 1)
        audits = len(self.store.audit_records())
        for previous in (review, unused):
            # Tokens convey no authority when the bound policy allows the read.
            result = self.call('/api/editor/run', sql=sql, confirmed=True,
                               confirm_token=previous['confirmation']['token'])
            self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(sum(p[2].endswith(sql) for p in self.packets()), 3)
        self.assertEqual(len(self.store.audit_records()), audits, 'allowed reads create no new review')

    def test_uncertain_create_wrapper_refuses_then_bound_alter_has_preflight(self):
        sql = '/*!999999 CREATE */ ALTER TABLE d.tt ADD COLUMN c INT'
        provisional = ed._plan_run({'sql': sql}, None)
        self.assertEqual(provisional['units'][0]['code'], 'CREATE ALTER TABLE d.tt ADD COLUMN c INT')
        self.assertIsNone(ed._alter_unit(provisional))
        initial = self.call('/api/editor/run', sql=sql)
        self.assertEqual(initial['status'], 'refused', initial)
        self.assertNotIn('confirmation', initial)
        self.assertIsNone(self.registry.get('tab1'))
        self.assertFalse(self.registry.tokens)
        self.call('/api/editor/open')
        review = self.call('/api/editor/run', sql=sql)
        self.assertEqual(review['status'], 'needs_confirmation', review)
        self.assertEqual(review['confirmation']['statements'], [ed.ALTER_LOCK_WAIT, sql])
        preflight = review['confirmation']['alter_preflight']
        self.assertEqual((preflight['schema'], preflight['table']), ('d', 'tt'))
        self.assertTrue(preflight['warnings'])
        self.assertFalse(any(p[2].endswith(sql) for p in self.packets()))
        self.assertNotIn(ed.ALTER_LOCK_WAIT, self.session_log())
        result = self.call('/api/editor/run', sql=sql, confirmed=True,
                           confirm_token=review['confirmation']['token'])
        self.assertEqual(result['status'], 'complete', result)
        packet = next(p[2] for p in self.packets() if p[2].endswith(sql))
        self.assertIn(ed.ALTER_LOCK_WAIT, packet)

    def test_loaded_galera_provider_requires_uuid_even_with_wsrep_off(self):
        node = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
        self.maria_client(wsrep=True, node_uuid=node)
        self.rewrite_client(lambda text: text.replace('[["wsrep_on", "ON"]]',
            '[["wsrep_on", "OFF"], ["wsrep_provider", "/usr/lib/galera/libgalera_smm.so"]]'))
        self.assertEqual(self.run_sql('SELECT 7')['status'], 'complete')
        session = self.registry.get('tab1')
        self.assertEqual(session._kill_identity['wsrep_gcomm_uuid'], node)
        self.assertIn(node, session._write_session_guard()[0])

    def test_galera_missing_node_uuid_never_falls_back_to_shared_names(self):
        self.maria_client(wsrep=True, node_uuid=None)
        session = self.assert_usable_without_kill()
        with self.assertRaisesRegex(ValueError, 'The server could not be confirmed'):
            session._write_session_guard()

    def assert_writes_refused_without_identity(self):
        self.assertEqual(self.run_sql('SELECT 7')['status'], 'complete')
        self.assertIn(ed.SAFE_UPDATES_ON, self.session_log())
        sql = 'SET @must_not_run = 17'
        review = self.call('/api/editor/run', sql=sql)
        before = len(self.packets())
        with self.assertRaisesRegex(ed.EditorError, 'The server could not be confirmed') as caught:
            self.call('/api/editor/run', sql=sql, confirmed=True,
                      confirm_token=review['confirmation']['token'])
        self.assertEqual(caught.exception.code, 'identity_unavailable')
        self.assertEqual(len(self.packets()), before, 'no probe or user SQL should follow an incomplete binding')
        self.assertNotIn(sql, self.session_log())
        self.assertEqual(self.store.audit_records()[0]['outcome'], 'identity_unavailable')
        self.assertEqual(self.run_sql('SELECT 8')['status'], 'complete')

    def test_confirmed_write_refuses_missing_galera_uuid_but_reads_continue(self):
        self.maria_client(wsrep=True, node_uuid=None)
        self.assert_writes_refused_without_identity()

    def test_confirmed_write_requires_loaded_galera_provider_uuid_with_wsrep_off(self):
        self.maria_client(wsrep=True, node_uuid=None)
        self.rewrite_client(lambda text: text.replace('[["wsrep_on", "ON"]]',
            '[["wsrep_on", "OFF"], ["wsrep_provider", "/usr/lib/galera/libgalera_smm.so"]]'))
        self.assert_writes_refused_without_identity()

    def test_confirmed_write_refuses_denied_startup_identity_but_reads_continue(self):
        self.inject_answers('''        if upper.startswith("SHOW GLOBAL STATUS WHERE "):
            self.error(line, 1227, "42000", "Status read denied")
            return
''')
        self.assert_writes_refused_without_identity()

    def test_confirmed_write_refuses_invalid_mysql_uuid_but_reads_continue(self):
        self.rewrite_client(lambda text: text.replace('11111111-2222-3333-4444-555555555555', 'invalid-uuid'))
        self.assert_writes_refused_without_identity()

    def test_safe_updates_refuses_missing_galera_uuid_before_sending(self):
        self.maria_client(wsrep=True, node_uuid=None)
        self.assertEqual(self.run_sql('SELECT 7')['status'], 'complete')
        self.assertIn(ed.SAFE_UPDATES_ON, self.session_log())
        review = self.call('/api/editor/open', safe_updates=False)
        before = len(self.packets())
        with self.assertRaisesRegex(ed.EditorError, 'The server could not be confirmed'):
            self.call('/api/editor/open', safe_updates=False, confirmed=True,
                      confirm_token=review['confirmation']['token'])
        self.assertEqual(len(self.packets()), before)
        self.assertNotIn(ed.SAFE_UPDATES_OFF, self.session_log())
        self.assertEqual(self.store.audit_records()[0]['outcome'], 'identity_unavailable')

    def test_safe_updates_change_and_guard_share_a_packet_and_refuse_routing_changes(self):
        for changed in ('server_uuid', 'connection_id'):
            tab = 'safe-' + changed
            self.assertEqual(self.run_sql('SELECT 7', tab=tab)['status'], 'complete')
            session = self.registry.get(tab)
            review = self.call('/api/editor/open', tab=tab, safe_updates=False)
            wrong = ({'server_uuid': 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'} if changed == 'server_uuid'
                     else {'connection_id': session.connection_id + 1})
            self.route_packets({}, wrong)
            before = len(self.packets())
            with self.assertRaises(ed.EditorError) as caught:
                self.call('/api/editor/open', tab=tab, safe_updates=False, confirmed=True,
                          confirm_token=review['confirmation']['token'])
            self.assertEqual(caught.exception.code, 'identity_mismatch')
            packets = self.packets()[before:]
            self.assertEqual(len(packets), 2, 'a refused setting must not trigger a readback')
            self.assertIn(ed.SAFE_UPDATES_OFF, packets[-1][2])
            self.assertIn('SET @myflames_target = ', packets[-1][2])
            self.assertNotIn(ed.SAFE_UPDATES_OFF, self.session_log(tab))
            self.assertEqual(self.store.audit_records()[0]['status'], 'refused')
            self.assertFalse(session.run_lock.locked())
            self.route_packets()

    def test_confirmed_compound_definitions_and_custom_delimiters_reach_one_guarded_packet(self):
        definitions = (
            'CREATE PROCEDURE p() BEGIN SELECT 1; SELECT 2; END',
            'CREATE FUNCTION f() RETURNS INT BEGIN RETURN 1; END',
            'CREATE TRIGGER tr BEFORE INSERT ON t FOR EACH ROW BEGIN SET NEW.a = 1; END',
            'CREATE EVENT e ON SCHEDULE EVERY 1 DAY DO BEGIN SELECT 1; SELECT 2; END',
            'CREATE OR REPLACE PACKAGE BODY p AS FUNCTION f RETURN INT IS BEGIN RETURN 1; END; END',
            'SET @custom = 42',
            'SET @first = 1; SET @second = 2',
        )
        for index, definition in enumerate(definitions):
            with self.subTest(definition=definition):
                tab = 'compound-%d' % index
                self.assertEqual(self.run_sql('SELECT 7', tab=tab)['status'], 'complete')
                result = self.run_sql('DELIMITER //\n' + definition + '//', tab=tab)
                self.assertEqual(result['status'], 'complete', result)
                session = self.registry.get(tab)
                packets = [p[2] for p in self.packets() if definition in p[2].replace(
                    '; ' + session.statement_tag, ';').replace(session.statement_tag, '')]
                self.assertEqual(len(packets), 1)
                self.assertIn('SET @myflames_target = ', packets[0])
                if definition.startswith('CREATE'):
                    self.assertTrue(packets[0].endswith(definition))
                record = next(r for r in self.store.audit_records() if r['id'] == result['run']['audit_id'])
                self.assertEqual(record['statements'], [definition])

    def test_safe_updates_readback_refusal_is_unknown_after_the_change_was_accepted(self):
        self.run_sql('SELECT 7')
        review = self.call('/api/editor/open', safe_updates=False)
        self.route_packets({}, {}, {'server_uuid': 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'})
        before = len(self.packets())
        with self.assertRaisesRegex(ed.EditorError, 'outcome is unknown') as caught:
            self.call('/api/editor/open', safe_updates=False, confirmed=True,
                      confirm_token=review['confirmation']['token'])
        self.assertEqual(caught.exception.code, 'unknown')
        self.assertEqual(len(self.packets()) - before, 3)
        self.assertEqual(self.session_log().count(ed.SAFE_UPDATES_OFF), 1)
        self.assertEqual(self.store.audit_records()[0]['status'], 'unknown')

    def test_confirmed_quote_uses_known_mode_when_client_boundaries_agree(self):
        for mode, sql in (('', r"SET @note = 'it\'s'"),
                          ('NO_BACKSLASH_ESCAPES', r"SET @note = 'C:\logs'")):
            tab = 'mode-' + (mode or 'default')
            self.run_sql("SET SESSION sql_mode = '%s'" % mode, tab=tab)
            result = self.run_sql(sql, tab=tab)
            self.assertEqual(result['status'], 'complete', result)
            self.assertIn(sql, self.session_log(tab))
            self.assertTrue(any(sql in p[2] and 'SET @myflames_target = ' in p[2] for p in self.packets()))

    def test_routed_state_cannot_authorize_a_write_under_another_quote_mode(self):
        # Identical client boundaries keep this test focused on the packet's
        # server-mode guard; a trailing backslash-quote now refuses in planning.
        sql = r"DELETE FROM t WHERE id = 1 AND note = 'C:\logs'"
        for index, actual in enumerate(('', 'NO_BACKSLASH_ESCAPES')):
            tab = 'routed-mode-%d' % index
            self.route_packets()
            self.run_sql("SET SESSION sql_mode = '%s'" % actual, tab=tab)
            session = self.registry.get(tab)
            other = '' if actual else 'NO_BACKSLASH_ESCAPES'
            # An unguarded refresh lands on another UUID with the same
            # connection id and teaches the editor that backend's mode.
            self.route_packets({'server_uuid': 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
                                'connection_id': session.connection_id, 'sql_mode': other})
            self.assertTrue(session.refresh_state())
            self.assertEqual(session.sql_mode, other)
            self.route_packets({'server_uuid': session.server_identity['server_uuid'],
                                'connection_id': session.connection_id, 'sql_mode': actual})
            result = self.run_sql(sql, tab=tab)
            self.assertEqual(result['run']['counts']['run'], 0, result)
            self.assertEqual(result['run']['statements'][0]['status'], 'not_run', result)
            packet = next(p[2] for p in reversed(self.packets()) if sql in p[2])
            self.assertIn("FIND_IN_SET('NO_BACKSLASH_ESCAPES', @@SESSION.sql_mode)", packet)
            self.assertIn('CONNECTION_ID() <> %d' % session.connection_id, packet)
            self.assertNotIn(sql, self.session_log(tab))
            audit = next(r for r in self.store.audit_records() if r['id'] == result['run']['audit_id'])
            self.assertEqual((audit['status'], audit['outcome']), ('refused', 'identity_mismatch'))

    def test_unknown_quote_mode_keeps_two_reading_packet_validation(self):
        self.run_sql('SELECT 7')
        session = self.registry.get('tab1')
        session.sql_mode = None
        before = len(self.packets())
        with self.assertRaises(ValueError):
            session._guarded_statement(session._write_session_guard(), r"SET @note = 'it\'s'")
        self.assertEqual(len(self.packets()), before)
        packet, _ = session._guarded_statement(session._write_session_guard(), 'SET @note = 1')
        self.assertNotIn('@@SESSION.sql_mode', packet)

    def test_alter_setup_and_statement_share_one_packet_and_keep_separate_results(self):
        sql = 'ALTER TABLE test.tt ADD COLUMN c INT'
        with mock.patch.object(ed, '_alter_review', return_value={'schema': 'test', 'table': 'tt', 'warnings': []}):
            result = self.run_sql(sql)
        self.assertEqual(result['status'], 'complete', result)
        packets = [p[2] for p in self.packets() if sql in p[2] or ed.ALTER_LOCK_WAIT in p[2]]
        self.assertEqual(len(packets), 1)
        self.assertIn('SET @myflames_target = ', packets[0])
        self.assertLess(packets[0].index(ed.ALTER_LOCK_WAIT), packets[0].index(sql))
        self.assertEqual([u['status'] for u in result['run']['statements']], ['ok', 'ok'])
        self.assertEqual([r['statement'] for r in result['run']['results']], [0, 1])
        self.assertEqual(result['run']['counts']['run'], 2)
        self.assertEqual(self.store.audit_records()[0]['statements'], [ed.ALTER_LOCK_WAIT, sql])

    def test_alter_setup_error_does_not_send_alter_and_is_attributed_to_setup(self):
        self.inject_answers('''        if upper == "SET SESSION LOCK_WAIT_TIMEOUT = 5":
            self.error(line, 1227, "42000", "Lock-wait setting denied")
            return
''')
        sql = 'ALTER TABLE test.tt ADD COLUMN c INT'
        with mock.patch.object(ed, '_alter_review', return_value={'schema': 'test', 'table': 'tt', 'warnings': []}):
            result = self.run_sql(sql)
        self.assertEqual([u['status'] for u in result['run']['statements']], ['error', 'not_run'])
        self.assertEqual(result['run']['error']['statement'], 0)
        self.assertEqual(result['run']['counts']['run'], 1)
        self.assertNotIn(sql, self.session_log())

    def test_alter_guard_refusal_sends_neither_setup_nor_alter(self):
        self.run_sql('SELECT 7')
        sql = 'ALTER TABLE test.tt ADD COLUMN c INT'
        with mock.patch.object(ed, '_alter_review', return_value={'schema': 'test', 'table': 'tt', 'warnings': []}):
            review = self.call('/api/editor/run', sql=sql)
            self.route_packets({}, {}, {'server_uuid': 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'})
            result = self.call('/api/editor/run', sql=sql, confirmed=True,
                               confirm_token=review['confirmation']['token'])
        self.assertEqual([u['status'] for u in result['run']['statements']], ['not_run', 'not_run'])
        self.assertEqual(result['run']['counts']['run'], 0)
        self.assertNotIn(ed.ALTER_LOCK_WAIT, self.session_log())
        self.assertNotIn(sql, self.session_log())

    def test_alter_error_preserves_successful_setup_and_reports_the_alter_error(self):
        self.inject_answers('''        if upper.startswith("ALTER TABLE "):
            self.error(line, 1146, "42S02", "Table does not exist")
            return
''')
        sql = 'ALTER TABLE test.tt ADD COLUMN c INT'
        with mock.patch.object(ed, '_alter_review', return_value={'schema': 'test', 'table': 'tt', 'warnings': []}):
            result = self.run_sql(sql)
        self.assertEqual([u['status'] for u in result['run']['statements']], ['ok', 'error'])
        self.assertEqual(result['run']['error']['statement'], 1)
        self.assertEqual(result['run']['counts']['run'], 2)
        self.assertEqual([r['statement'] for r in result['run']['results']], [0, 1])
        self.assertEqual(self.store.audit_records()[0]['status'], 'error')

    def test_transaction_controls_all_share_the_guarded_packet(self):
        controls = ('BEGIN', 'START TRANSACTION', 'ROLLBACK', 'ROLLBACK TO SAVEPOINT s',
                    'SAVEPOINT s', 'RELEASE SAVEPOINT s', 'SET autocommit = 0',
                    'SET autocommit = 1', 'SET SESSION TRANSACTION READ ONLY')
        for index, sql in enumerate(controls):
            tab = 'control-%d' % index
            with self.subTest(sql=sql):
                result = self.run_sql(sql, tab=tab)
                self.assertIn(result['status'], ('complete', 'error'), result)
                packets = [p[2] for p in self.packets() if sql in p[2] and 'SET @myflames_target = ' in p[2]]
                self.assertTrue(packets, sql)
                self.call('/api/editor/close', tab=tab, discard=True)

    def test_read_classified_control_refuses_a_same_server_session_switch(self):
        self.run_sql('SELECT 7')
        session = self.registry.get('tab1')
        self.route_packets({'connection_id': session.connection_id},
                           {'connection_id': session.connection_id},
                           {'connection_id': session.connection_id + 1})
        result = self.run_sql('ROLLBACK')
        self.assertEqual(result['run']['statements'][0]['status'], 'not_run', result)
        self.assertNotIn('ROLLBACK', self.session_log())

    def test_rollback_button_is_guarded_without_write_confirmation(self):
        self.run_sql('SELECT 7')
        session = self.registry.get('tab1')
        self.route_packets({'connection_id': session.connection_id}, {'connection_id': session.connection_id + 1})
        result = self.call('/api/editor/rollback')
        self.assertEqual(result['run']['statements'][0]['status'], 'not_run', result)
        self.assertIn(ed.TAG + 'ROLLBACK', self.packets()[-1][2])

    def test_plan_controls_state_reads_and_explain_keep_the_session_guard(self):
        self.run_sql('SELECT 7')
        session = self.registry.get('tab1')
        before = len(self.packets())
        self.assertIsInstance(json.loads(session.run_plan('SELECT 1')), dict)
        packets = [p[2] for p in self.packets()[before:]]
        for sql in ('START TRANSACTION READ ONLY', ed.TAG + 'ROLLBACK', 'EXPLAIN FORMAT=JSON SELECT 1',
                    'AS state', '@@autocommit AS autocommit'):
            matching = [p for p in packets if sql in p]
            self.assertEqual(len(matching), 1, sql)
            self.assertIn('SET @myflames_target = ', matching[0])

    def test_plan_refuses_routed_begin_before_it_can_commit_another_backend(self):
        self.rewrite_client(lambda text: text.replace(
            '        self.route_connection_id = route.get("connection_id", self.conn_id)',
            '        self.route_connection_id = route.get("connection_id", self.conn_id)\n'
            '        if "START TRANSACTION READ ONLY" in wire:\n'
            '            self.route_connection_id += 1'))
        self.run_sql('SELECT 7')
        session = self.registry.get('tab1')
        before = len(self.packets())
        with self.assertRaisesRegex(ed.EditorError, 'session could not be confirmed'):
            session.run_plan('SELECT 1')
        self.assertTrue(session.ended)
        self.assertNotIn(ed.TAG + 'START TRANSACTION READ ONLY', self.session_log())
        packets = [p[2] for p in self.packets()[before:]]
        self.assertIn('START TRANSACTION READ ONLY', packets[-1])
        self.assertFalse(any('EXPLAIN FORMAT=JSON SELECT 1' in p or ed.TAG + 'ROLLBACK' in p for p in packets))

    def test_commit_capable_controls_use_commit_policy_and_audit(self):
        for index, sql in enumerate(('BEGIN', 'START TRANSACTION', 'SET autocommit = 1',
                                     "SET @@SESSION.`autocommit` = 'ON'")):
            tab = 'commit-control-%d' % index
            with self.subTest(sql=sql):
                review = self.call('/api/editor/run', tab=tab, sql=sql)
                self.assertEqual(review['status'], 'needs_confirmation', review)
                self.assertEqual(review['confirmation']['kind'], 'write')
                self.assertIn(review['confirmation']['category'], ('transaction', 'session'))
                result = self.call('/api/editor/run', tab=tab, sql=sql, confirmed=True,
                                   confirm_token=review['confirmation']['token'])
                audit = next(r for r in self.store.audit_records() if r['id'] == result['run']['audit_id'])
                self.assertIn(sql, audit['statements'])
                self.assertTrue(audit['finished_at'])
                self.call('/api/editor/close', tab=tab, discard=True)

    def test_autocommit_expressions_and_multiline_controls_cannot_evade_review(self):
        safety = {'type': 'production', 'read_only': False}
        for sql in ("SET\nSESSION `autocommit` = 'ON'", 'SET @@session.autocommit = DEFAULT',
                    'SET autocommit = @enabled', 'SET autocommit = 1, autocommit = 0',
                    'SET autocommit = (1)', 'DELIMITER //\nSELECT 1; BEGIN//'):
            with self.subTest(sql=sql):
                plan = ed._plan_run({'sql': sql}, None)
                self.assertEqual(plan['verdict']['kind'], 'write')
                self.assertEqual(ed._decision(plan, safety)['action'], 'confirm')
        for sql in ('SET autocommit = 0', 'SET SESSION autocommit = OFF', 'SET @@autocommit = FALSE'):
            with self.subTest(sql=sql):
                plan = ed._plan_run({'sql': sql}, None)
                self.assertEqual(plan['verdict']['kind'], dbops.classify(sql)['kind'])
                self.assertFalse(ed.implicit_commit(plan['units'][0]))
                self.assertTrue(ed._needs_session_guard(plan['units'][0]))
        for sql in ('SET @autocommit = 1', 'SET GLOBAL autocommit = 1', 'SET @@GLOBAL.`autocommit` = 1',
                    'SET sql_select_limit = @@autocommit = 1', "SET sql_mode = 'autocommit = 1'"):
            with self.subTest(non_committing=sql):
                plan = ed._plan_run({'sql': sql}, None)
                self.assertFalse(ed.implicit_commit(plan['units'][0]))
        for sql in ('BEGIN', 'START TRANSACTION', 'SET autocommit = 1'):
            for kind, readonly in (('production', False), ('development', False), ('production', True)):
                with self.subTest(sql=sql, policy=kind, readonly=readonly):
                    expected = dbops.decide(dbops.classify('COMMIT'), kind, readonly)['action']
                    plan = ed._plan_run({'sql': sql}, None)
                    self.assertEqual(ed._decision(plan, {'type': kind, 'read_only': readonly})['action'], expected)

    def test_stop_preserves_acknowledgement_for_kill_errors(self):
        self.run_sql('SELECT 7 /*gate*/', wait=0)
        self.until(lambda: self.logged('tab1', 'SELECT 7 /*gate*/'), 'owned statement to start')
        session = self.registry.get('tab1')
        try:
            for code, message in (('1094', 'already ended'), ('1095', 'server rejected KILL QUERY'),
                                  ('1242', 'server rejected KILL QUERY'), ('1227', 'server rejected KILL QUERY')):
                with self.subTest(code=code), mock.patch.dict(os.environ, FAKE_KILL_ERROR=code):
                    result = self.call('/api/editor/stop')
                    self.assertFalse(result['stopped'], result)
                    self.assertIn(message, result['message'])
                    self.assertNotIn('server could not be confirmed', result['message'])
                    self.assertIsNone(session.run.current.kill_sent_at)
        finally:
            self.release()
            self.assertTrue(session.run.done.wait(5))

    def test_stop_still_checks_ownership_with_performance_schema_off(self):
        with mock.patch.dict(os.environ, FAKE_PS='off'):
            self.run_sql('SELECT 7 /*gate*/', wait=0)
            self.until(lambda: self.logged('tab1', 'SELECT 7 /*gate*/'), 'owned statement to start')
            result = self.call('/api/editor/stop')
        self.assertTrue(result['stopped'], result)
        self.assertTrue(self.registry.get('tab1').run.done.wait(5))
        self.assertIn('information_schema.PROCESSLIST', (self.state / 'kills.log').read_text())

    def test_rewritten_credential_statement_refuses_stop_with_explanation(self):
        self.run_sql("CREATE USER 'slow' IDENTIFIED BY 'secret' /*gate*/", wait=0)
        session = self.registry.get('tab1')
        active = self.state / ('active-%d' % session.connection_id)
        self.until(lambda: self.logged('tab1', "CREATE USER 'slow'"), 'credential statement to start')
        active.write_text("CREATE USER 'slow'@'%' IDENTIFIED WITH caching_sha2_password AS '<secret>'")
        try:
            result = self.call('/api/editor/stop')
            self.assertFalse(result['stopped'], result)
            self.assertIn('rewrite', result['message'])
            self.assertIn('statement continues', result['message'])
            self.assertNotIn('secret', result['message'])
            self.assertFalse((self.state / 'kills.log').exists())
        finally:
            self.release()
            self.assertTrue(session.run.done.wait(5))

    def test_stop_during_routine_or_prepared_statement_keeps_ownership_check(self):
        for sql in ('CALL slow_routine()', 'EXECUTE slow_statement', "EXECUTE IMMEDIATE 'SELECT SLEEP(8)'"):
            with self.subTest(sql=sql):
                self.run_sql(sql + ' /*gate*/', wait=0)
                session = self.registry.get('tab1')
                active = self.state / ('active-%d' % session.connection_id)
                self.until(lambda: sql in _read_if_present(active),
                           'outer statement to start')
                # Engines display the untagged inner SQL, not the caller's
                # private token. It is not safe to kill by connection id alone.
                active.write_text('SELECT SLEEP(8)', encoding='utf-8')
                try:
                    stopped = self.call('/api/editor/stop')
                    self.assertFalse(stopped['stopped'], stopped)
                    self.assertIn('routine or prepared statement', stopped['message'])
                    self.assertIn('continues', stopped['message'])
                    self.assertNotIn(session.statement_tag.strip(), stopped['message'])
                    self.assertFalse((self.state / 'kills.log').exists())
                    self.assertIsNone(session.run.current.kill_sent_at)
                    self.assertFalse(session.run.done.is_set())
                finally:
                    self.release()
                    self.assertTrue(session.run.done.wait(5))

    def test_stop_timeout_never_exposes_generated_sql_or_tab_token(self):
        self.run_sql('SELECT 7 /*gate*/', wait=0)
        self.until(lambda: self.logged('tab1', 'SELECT 7 /*gate*/'), 'owned statement to start')
        session = self.registry.get('tab1')

        def timeout(argv, **kwargs):
            script = kwargs['input'].decode('utf-8')
            marker = re.search(r"SELECT '(myflames_guard_ok_[0-9a-f]{32}_[0-9]+)'", script).group(1)
            self.assertNotIn(script, argv)
            raise subprocess.TimeoutExpired(argv, 1, output=(marker + '\n').encode(), stderr=script.encode())

        try:
            with mock.patch.object(ed.MySQLConnection, '_run_client', side_effect=timeout):
                result = self.call('/api/editor/stop')
            visible = json.dumps(result) + json.dumps(session.status())
            self.assertIn('unknown', result['message'])
            for private in (session.statement_tag.strip(), 'SET @myflames_target', 'myflames_guard_ok_'):
                self.assertNotIn(private, visible)
        finally:
            self.release()
            self.assertTrue(session.run.done.wait(5))

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
            self.until(lambda: 'SELECT 7 /*gate*/' in _read_if_present(active),
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
                with self.subTest(code=code), mock.patch.object(ed.MySQLConnection, 'run_write', side_effect=error):
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
                                       ('mysql', '8.0.22', 'information_schema.PROCESSLIST'),
                                       ('mysql', '8.4.11', 'information_schema.PROCESSLIST'),
                                       ('mysql', '9.7.0', 'information_schema.PROCESSLIST'),
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
        # Versioned CREATE comments now require the connected server facts.
        from test_sql_lexer import session as bound_session
        bound = bound_session()
        for key in ("engine", "server_version", "sql_mode", "connection_id", "server_identity", "_kill_identity"):
            setattr(session, key, getattr(bound, key))
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
