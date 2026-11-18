"""Editor boundary regressions: lexical controls and routed setup packets.

Only server answers are synthetic. Planning, policy, audit, guard generation
and the persistent client's actual framed pipes all use production code.
"""
import os
import unittest
from unittest import mock

import test_editor_session as fixtures
import test_editor_session_identity as identity_fixtures
from myflames import dbops, editor_session as ed
from myflames.connector import ConnectorError


class EditorBoundaryTests(unittest.TestCase):
    def test_dollar_tags_follow_utf8mb4_client_identifier_rules(self):
        for tag in ('', '123', 'a_b', 'é', '标记', 'a\u0301', '😀', 'aé9_'):
            for body in ('SELECT 1; DELETE FROM t', "'; UPDATE t SET v = 1; '"):
                sql = 'SELECT $%s$%s$%s$' % (tag, body, tag)
                with self.subTest(tag=tag, body=body):
                    with self.assertRaises(ed.EditorError) as caught:
                        ed._plan_run({'sql': sql}, None)
                    self.assertEqual(caught.exception.code, 'invalid_statement')
        # Dollar signs inside identifiers, quoted strings and comments are
        # not openers. Punctuation inside a tag is not an identifier byte.
        for sql in ('SELECT café$tag$', 'SELECT 😀$tag$', 'SELECT a$é$',
                    "SELECT '$é$; DELETE FROM t'", 'SELECT 1 /* $é$ */',
                    'SELECT $a-b$', 'SELECT $a.b$', 'SELECT $a b$'):
            with self.subTest(not_a_dollar_body=sql):
                self.assertEqual(len(ed.plan_statements(sql)), 1)

    def test_wrapped_controls_follow_commit_policy_and_warnings(self):
        controls = ('BEGIN', 'START TRANSACTION', 'SET autocommit = 1',
                    'SET SESSION `autocommit` = ON', 'SET @@SESSION.`autocommit` = DEFAULT',
                    'SET "autocommit" = 1', 'SET @@SESSION."autocommit" = ON')
        wrappers = ('SET STATEMENT max_statement_time = 10 FOR ',
                    "SET STATEMENT max_statement_time = (10), optimizer_switch = 'index_merge=off' FOR ",
                    'SET STATEMENT max_statement_time = 10 FOR SET STATEMENT sql_select_limit = 9 FOR ')
        for wrapper in wrappers:
            for control in controls:
                sql = wrapper + control
                with self.subTest(sql=sql):
                    plan = ed._plan_run({'sql': sql}, None)
                    self.assertTrue(ed.implicit_commit(plan['units'][0]))
                    self.assertNotEqual(plan['verdict']['kind'], 'read')
                    for ctype, readonly in (('production', False), ('development', False), ('production', True)):
                        expected = dbops.decide(dbops.classify('COMMIT'), ctype, readonly)['action']
                        self.assertEqual(ed._decision(plan, {'type': ctype, 'read_only': readonly})['action'], expected)
                    session = type('State', (), {'in_transaction': True})()
                    self.assertTrue(ed._commit_warnings(plan['units'], session))

    def test_wrapper_for_boundary_and_noncommitting_assignments(self):
        for value in ('NEXT VALUE FOR s', 'PREVIOUS VALUE FOR s', 'POSITION(1 IN (1))', "'FOR BEGIN'"):
            sql = 'SET STATEMENT max_statement_time = %s FOR BEGIN' % value
            self.assertTrue(ed.implicit_commit(ed.plan_statements(sql)[0]), sql)
        for inner in ('SET autocommit = 0', 'SET SESSION autocommit = OFF', 'SET "autocommit" = FALSE',
                      'SET @autocommit = 1', 'SET GLOBAL autocommit = 1', 'SET sql_select_limit = @@autocommit = 1',
                      'SELECT 1'):
            sql = 'SET STATEMENT max_statement_time = 10 FOR ' + inner
            self.assertFalse(ed.implicit_commit(ed.plan_statements(sql)[0]), sql)

    def test_transaction_age_handles_wrapped_and_quoted_controls(self):
        for sql in ('SET `autocommit` = 1', 'SET "autocommit" = 1',
                    'SET STATEMENT max_statement_time = 10 FOR BEGIN',
                    'SET STATEMENT max_statement_time = 10 FOR COMMIT'):
            unit = ed.plan_statements(sql)[0]
            self.assertTrue(ed.may_replace_transaction(dict(unit, status='ok'), []), sql)
            self.assertFalse(ed.may_replace_transaction(dict(unit, status='error'), [{'code': 1064}]), sql)

    def test_compound_ddl_and_terminal_comments_need_one_confirmation(self):
        definitions = (
            'CREATE PACKAGE BODY p AS PROCEDURE p1; BEGIN NULL; END p',
            'CREATE PACKAGE BODY p AS PROCEDURE p1 IS n INT; BEGIN NULL; END; BEGIN p1; END p',
            'CREATE PACKAGE BODY p FUNCTION f() RETURNS INT RETURN 1; END',
            'CREATE PACKAGE BODY p FUNCTION f() RETURNS INT RETURN 1; BEGIN SELECT f(); END; END',
            'CREATE PACKAGE BODY p FUNCTION f() RETURNS INT RETURN 1; SELECT f() AS n; BEGIN SELECT 2; END; END',
            'ALTER EVENT e DO BEGIN SELECT 1; SELECT CASE WHEN 1 THEN 2 END; END',
            "ALTER DEFINER = 'u'@'localhost' EVENT e ON SCHEDULE EVERY 1 DAY DO BEGIN SELECT 1; END",
            'CREATE PROCEDURE p() BEGIN SELECT 1; END',
        )
        safety = {'type': 'production', 'read_only': False}
        for definition in definitions:
            for terminal in ('', ';', '; -- trailing comment\n', '; # trailing comment\n', '; /* trailing comment */'):
                sql = 'DELIMITER //\n' + definition + terminal + '//'
                with self.subTest(sql=sql):
                    plan = ed._plan_run({'sql': sql}, None)
                    self.assertEqual(len(ed._ddl_codes(plan['units'][0])), 1)
                    self.assertEqual(ed._decision(plan, safety)['action'], 'confirm')
            for tail in ('SELECT 1', 'DROP DATABASE d', 'CREATE PROCEDURE q() BEGIN SELECT 1; END'):
                sql = 'DELIMITER //\n' + definition + '; ' + tail + '//'
                with self.subTest(trailing_sql=sql):
                    self.assertEqual(ed._decision(ed._plan_run({'sql': sql}, None), safety)['action'], 'refuse')


@unittest.skipIf(os.name == 'nt', 'the fake client is a POSIX script')
class EditorSetupPacketTests(unittest.TestCase):
    engine = 'mysql'
    setUp = fixtures.ProcessTests.setUp
    call = fixtures.ProcessTests.call
    run_sql = fixtures.ProcessTests.run_sql
    session_log = fixtures.ProcessTests.session_log
    save_profile = fixtures.ProcessTests.save_profile
    rewrite_client = identity_fixtures.EditorIdentityTests.rewrite_client
    inject_answers = identity_fixtures.EditorIdentityTests.inject_answers
    packets = identity_fixtures.EditorIdentityTests.packets
    route_packets = identity_fixtures.EditorIdentityTests.route_packets

    def route_matching(self, needle, field='server_uuid'):
        assignment = ('self.route_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"' if field == 'server_uuid'
                      else 'self.route_connection_id += 1')
        source = '        self.route_sql_mode = route.get("sql_mode", self.sql_mode)'
        self.rewrite_client(lambda text: text.replace(source, source + '\n        if %r in wire:\n            %s'
                                                      % (needle, assignment)))

    def test_startup_hardening_and_readback_are_guarded(self):
        self.save_profile(read_only=True)
        self.assertEqual(self.run_sql('SELECT 7')['status'], 'complete')
        packets = [p[2] for p in self.packets()]
        setup = [p for p in packets if 'SET SESSION TRANSACTION READ ONLY' in p or
                 'SET SESSION sql_safe_updates = 1' in p or 'SHOW SESSION VARIABLES' in p]
        self.assertEqual(len(setup), 3)
        self.assertTrue(all('SET @myflames_target = ' in p for p in setup))
        # Between binding and the user's SELECT, every setup/state packet is guarded.
        end = next(i for i, p in enumerate(packets) if 'SHOW SESSION VARIABLES' in p) + 1
        self.assertTrue(all('SET @myflames_target = ' in p for p in packets[2:end]))

    def test_routed_startup_setting_stops_before_any_followup(self):
        self.route_matching('SET SESSION sql_safe_updates = 1')
        with self.assertRaises(ed.EditorError) as caught:
            self.run_sql('SELECT 7')
        self.assertEqual(caught.exception.code, 'identity_mismatch')
        packets = self.packets()
        self.assertEqual(len(packets), 3)
        self.assertIn('SET SESSION sql_safe_updates = 1', packets[-1][2])
        self.assertNotIn('SELECT 7', (self.state / ('session-%s.log' % packets[0][0])).read_text())

    def test_routed_read_only_setup_stops_before_safe_updates(self):
        self.save_profile(read_only=True)
        self.route_matching('SET SESSION TRANSACTION READ ONLY', 'connection_id')
        with self.assertRaises(ed.EditorError) as caught:
            self.run_sql('SELECT 7')
        self.assertEqual(caught.exception.code, 'identity_mismatch')
        self.assertEqual(len(self.packets()), 3)

    def test_routed_startup_readback_ends_without_user_sql(self):
        self.route_matching('AS database_name', 'connection_id')
        with self.assertRaises(ed.EditorError) as caught:
            self.run_sql('SELECT 7')
        self.assertEqual(caught.exception.code, 'identity_mismatch')
        self.assertIn('AS database_name', self.packets()[-1][2])

    def test_acknowledged_but_ineffective_hardening_refuses_start(self):
        self.inject_answers('''        if upper == "SET SESSION SQL_SAFE_UPDATES = 1":
            self.ok()
            return
''')
        with self.assertRaisesRegex(ConnectorError, 'safe-updates setting could not be verified'):
            self.run_sql('SELECT 7')
        self.assertFalse(any(p[2].endswith('SELECT 7') for p in self.packets()))

    def test_read_only_setting_must_be_verified_on_the_server(self):
        self.save_profile(read_only=True)
        self.inject_answers('''        if upper == "SET SESSION TRANSACTION READ ONLY":
            self.ok()
            return
''')
        with self.assertRaisesRegex(ConnectorError, 'read-only transaction setting could not be verified'):
            self.run_sql('SELECT 7')
        self.assertIn('SHOW SESSION VARIABLES', self.packets()[-1][2])

    def test_incomplete_state_readback_cannot_reuse_cached_settings(self):
        self.inject_answers('''        if "DATABASE() AS DATABASE_NAME" in upper:
            values = {"connection_id": str(self.conn_id), "autocommit": "1", "sql_mode": self.sql_mode,
                      "sql_safe_updates": "1", "database_name": None}
            values.pop(os.environ["FAKE_MISSING_STATE"])
            self.result(statement, list(values), [list(values.values())])
            return
''')
        for index, missing in enumerate(('sql_safe_updates', 'autocommit', 'sql_mode', 'database_name')):
            with self.subTest(missing=missing):
                os.environ['FAKE_MISSING_STATE'] = missing
                with self.assertRaisesRegex(ConnectorError, 'Could not verify session setup'):
                    self.run_sql('SELECT 7', tab='missing-%d' % index)
        self.assertFalse(any(p[2].endswith('SELECT 7') for p in self.packets()))

    def test_startup_state_read_error_is_not_silently_ignored(self):
        self.inject_answers('''        if "DATABASE() AS DATABASE_NAME" in upper:
            self.error(line, 1142, "42000", "State read denied")
            return
''')
        with self.assertRaisesRegex(ConnectorError, 'Could not verify session setup'):
            self.run_sql('SELECT 7')
        self.assertIn('AS database_name', self.packets()[-1][2])

    def test_missing_or_denied_read_only_state_refuses_startup(self):
        self.save_profile(read_only=True)
        self.inject_answers('''        if upper.startswith("SHOW SESSION VARIABLES WHERE "):
            if os.environ.get("FAKE_DENY_READ_ONLY_STATE") == "1":
                self.error(line, 1142, "42000", "Read-only state denied")
            else:
                self.result(statement, ["Variable_name", "Value"], [])
            return
''')
        for deny in ('0', '1'):
            with self.subTest(deny=deny):
                os.environ['FAKE_DENY_READ_ONLY_STATE'] = deny
                with self.assertRaisesRegex(ConnectorError, 'read-only transaction setting could not be verified'):
                    self.run_sql('SELECT 7', tab='read-only-' + deny)
        self.assertFalse(any(p[2].endswith('SELECT 7') for p in self.packets()))

    def test_wrapped_commit_controls_require_review_and_produce_audit(self):
        for index, inner in enumerate(('BEGIN', 'START TRANSACTION', 'SET autocommit = 1',
                                       'SET @@SESSION.`autocommit` = ON')):
            sql = 'SET STATEMENT max_statement_time = 10 FOR ' + inner
            tab = 'wrapped-%d' % index
            with self.subTest(sql=sql):
                review = self.call('/api/editor/run', tab=tab, sql=sql)
                self.assertEqual(review['status'], 'needs_confirmation')
                self.assertEqual(review['confirmation']['kind'], 'write')
                result = self.call('/api/editor/run', tab=tab, sql=sql, confirmed=True,
                                   confirm_token=review['confirmation']['token'])
                record = next(r for r in self.store.audit_records() if r['id'] == result['run']['audit_id'])
                self.assertEqual(record['statements'], [sql])
                self.assertTrue(record['finished_at'])
                self.assertTrue(any(sql in p[2] and 'SET @myflames_target = ' in p[2] for p in self.packets()))
                self.call('/api/editor/close', tab=tab, discard=True)

    def test_unavailable_identity_sends_and_verifies_hardening_but_refuses_changes(self):
        self.inject_answers('''        if upper.startswith("SHOW GLOBAL VARIABLES WHERE "):
            self.error(line, 1142, "42000", "Identity denied")
            return
''')
        result = self.run_sql('SELECT 7')
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(fixtures.ProcessTests.rows(self, result), [['7']])
        self.assertTrue(result['session']['safe_updates'])
        session = self.registry.get('tab1')
        packets = [p[2] for p in self.packets()]
        first_read = next(i for i, p in enumerate(packets) if p.endswith('SELECT 7'))
        startup = packets[2:first_read]
        self.assertTrue(any(ed.SAFE_UPDATES_ON in p for p in startup))
        self.assertTrue(any('AS database_name' in p for p in startup))
        self.assertTrue(all('SET @myflames_target = ' not in p for p in startup))

        for sql in ('DELETE FROM t WHERE id = 1', 'SET @must_not_run = 17'):
            with self.subTest(sql=sql):
                review = self.call('/api/editor/run', sql=sql)
                self.assertEqual(review['status'], 'needs_confirmation', review)
                before = len(self.packets())
                with self.assertRaises(ed.EditorError) as caught:
                    self.call('/api/editor/run', sql=sql, confirmed=True,
                              confirm_token=review['confirmation']['token'])
                self.assertEqual(caught.exception.code, 'identity_unavailable')
                self.assertEqual(len(self.packets()), before)
                self.assertNotIn(sql, self.session_log())
        before = len(self.packets())
        with self.assertRaises(ed.EditorError) as caught:
            self.call('/api/editor/run', sql='USE `target`')
        self.assertEqual(caught.exception.code, 'identity_unavailable')
        with self.assertRaises(ed.EditorError) as caught:
            self.call('/api/editor/use', database='target')
        self.assertEqual(caught.exception.code, 'identity_unavailable')
        with self.assertRaisesRegex(ConnectorError, 'server could not be confirmed'):
            session._send_kill(self.connection, session.connection_id)
        self.assertEqual(len(self.packets()), before)
        self.assertFalse((self.state / 'kills.log').exists())
        self.assertEqual(self.run_sql('SELECT 8')['status'], 'complete')

    def test_unavailable_identity_verifies_both_engines_read_only_defaults(self):
        self.save_profile(read_only=True)
        self.inject_answers('''        if upper.startswith("SHOW GLOBAL VARIABLES WHERE "):
            self.error(line, 1142, "42000", "Identity denied")
            return
''')
        for engine in ('mysql', 'mariadb'):
            with self.subTest(engine=engine), mock.patch.dict(os.environ, FAKE_ENGINE=engine):
                result = self.run_sql('SELECT 7', tab=engine)
                self.assertEqual(result['status'], 'complete', result)
                self.assertTrue(result['session']['safe_updates'])
                self.assertTrue(result['session']['read_only_transactions'])
                session = self.registry.get(engine)
                packets = [p[2] for p in self.packets() if p[0] == session.connection_id]
                setup = [p for p in packets if 'SET SESSION TRANSACTION READ ONLY' in p or
                         ed.SAFE_UPDATES_ON in p or 'SHOW SESSION VARIABLES' in p]
                self.assertEqual(len(setup), 3)
                self.assertTrue(all('SET @myflames_target = ' not in p for p in setup))
                state = next(i for i, p in enumerate(packets) if 'AS database_name' in p)
                read_only = next(i for i, p in enumerate(packets) if 'SHOW SESSION VARIABLES' in p)
                first_read = next(i for i, p in enumerate(packets) if p.endswith('SELECT 7'))
                self.assertLess(state, read_only)
                self.assertLess(read_only, first_read)

    def test_unavailable_identity_ineffective_safe_updates_refuses_startup(self):
        self.inject_answers('''        if upper.startswith("SHOW GLOBAL VARIABLES WHERE "):
            self.error(line, 1142, "42000", "Identity denied")
            return
        if upper == "SET SESSION SQL_SAFE_UPDATES = 1":
            self.ok()
            return
''')
        with self.assertRaisesRegex(ConnectorError, 'safe-updates setting could not be verified'):
            self.run_sql('SELECT 7')
        packets = [p[2] for p in self.packets()]
        self.assertTrue(any(ed.SAFE_UPDATES_ON in p for p in packets))
        self.assertIn('AS database_name', packets[-1])
        self.assertTrue(all('SET @myflames_target = ' not in p for p in packets))
        self.assertFalse(any(p.endswith('SELECT 7') for p in packets))

    def test_unavailable_identity_ineffective_read_only_refuses_startup(self):
        self.save_profile(read_only=True)
        self.inject_answers('''        if upper.startswith("SHOW GLOBAL VARIABLES WHERE "):
            self.error(line, 1142, "42000", "Identity denied")
            return
        if upper == "SET SESSION TRANSACTION READ ONLY":
            self.ok()
            return
''')
        for engine in ('mysql', 'mariadb'):
            with self.subTest(engine=engine), mock.patch.dict(os.environ, FAKE_ENGINE=engine):
                with self.assertRaisesRegex(ConnectorError, 'read-only transaction setting could not be verified'):
                    self.run_sql('SELECT 7', tab=engine)
                self.assertIn('SHOW SESSION VARIABLES', self.packets()[-1][2])
        packets = [p[2] for p in self.packets()]
        self.assertTrue(any('SET SESSION TRANSACTION READ ONLY' in p for p in packets))
        self.assertTrue(all('SET @myflames_target = ' not in p for p in packets))
        self.assertFalse(any(p.endswith('SELECT 7') for p in packets))

    def test_unavailable_identity_without_setup_still_requires_valid_state(self):
        self.save_profile(type='development')
        self.inject_answers('''        if upper.startswith("SHOW GLOBAL VARIABLES WHERE "):
            self.error(line, 1142, "42000", "Identity denied")
            return
        if "DATABASE() AS DATABASE_NAME" in upper:
            fault = os.environ.get("FAKE_STATE_FAULT", "")
            if fault == "denied":
                self.error(line, 1142, "42000", "State read denied")
                return
            values = {"connection_id": str(self.conn_id), "autocommit": "1", "sql_mode": self.sql_mode,
                      "sql_safe_updates": "0", "database_name": None}
            if fault.startswith("missing-"):
                values.pop(fault[len("missing-"):])
            elif fault.startswith("invalid-"):
                key = fault[len("invalid-"):]
                values[key] = None if key == "sql_mode" else "invalid"
            self.result(statement, list(values), [list(values.values())])
            return
''')
        result = self.run_sql('SELECT 7')
        self.assertEqual(result['status'], 'complete', result)
        self.assertFalse(result['session']['safe_updates'])
        packets = [p[2] for p in self.packets()]
        state = next(i for i, p in enumerate(packets) if 'AS database_name' in p)
        first_read = next(i for i, p in enumerate(packets) if p.endswith('SELECT 7'))
        self.assertLess(state, first_read)
        self.assertTrue(all('SET @myflames_target = ' not in p for p in packets))
        self.assertFalse(any('SET SESSION' in p for p in packets[:first_read]))
        faults = ('denied', 'missing-autocommit', 'missing-sql_safe_updates', 'missing-sql_mode',
                  'missing-database_name', 'invalid-autocommit', 'invalid-sql_safe_updates', 'invalid-sql_mode')
        for fault in faults:
            with self.subTest(fault=fault), mock.patch.dict(os.environ, FAKE_STATE_FAULT=fault):
                before = len(self.packets())
                with self.assertRaisesRegex(ConnectorError, 'Could not verify session setup'):
                    self.run_sql('SELECT 7', tab=fault)
                startup = [p[2] for p in self.packets()[before:]]
                self.assertIn('AS database_name', startup[-1])
                self.assertTrue(all('SET @myflames_target = ' not in p for p in startup))
                self.assertFalse(any(p.endswith('SELECT 7') for p in startup))

    def test_use_refuses_routing_change_before_database_switch(self):
        self.run_sql('SELECT 7')
        session = self.registry.get('tab1')
        before = len(self.packets())
        self.route_packets({'connection_id': session.connection_id + 1})
        with self.assertRaises(ed.EditorError) as caught:
            self.call('/api/editor/use', database='target')
        self.assertEqual(caught.exception.code, 'identity_mismatch')
        self.assertTrue(session.ended)
        self.assertEqual(len(self.packets()), before + 1)
        self.assertNotIn(ed.TAG + 'USE `target`', self.session_log())

    def test_use_readback_refuses_routing_change_after_switch(self):
        self.run_sql('SELECT 7')
        session = self.registry.get('tab1')
        before = len(self.packets())
        self.route_packets({}, {'server_uuid': 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'})
        with self.assertRaises(ed.EditorError) as caught:
            self.call('/api/editor/use', database='target')
        self.assertEqual(caught.exception.code, 'unknown')
        self.assertTrue(session.ended)
        self.assertEqual(len(self.packets()), before + 2)
        self.assertIn(ed.TAG + 'USE `target`', self.session_log())
        self.assertTrue(all('SET @myflames_target = ' in p[2] for p in self.packets()[before:]))

    def test_nested_transaction_meter_read_keeps_the_readback_guard(self):
        self.route_matching('FROM information_schema.INNODB_TRX WHERE trx_mysql_thread_id = CONNECTION_ID()')
        self.run_sql('BEGIN')
        with self.assertRaises(ed.EditorError) as caught:
            self.call('/api/editor/use', database='target')
        self.assertEqual(caught.exception.code, 'unknown')
        self.assertTrue(self.registry.get('tab1').ended)
        self.assertIn('FROM information_schema.INNODB_TRX', self.packets()[-1][2])
        self.assertIn('SET @myflames_target = ', self.packets()[-1][2])

    def test_unqualified_alter_database_check_is_guarded_and_audited(self):
        self.run_sql('SELECT 7')
        self.call('/api/editor/use', database='test')
        sql = 'ALTER TABLE tt ADD COLUMN c INT'
        with mock.patch.object(ed, '_alter_review', return_value={'schema': 'test', 'table': 'tt', 'warnings': []}):
            review = self.call('/api/editor/run', sql=sql)
            self.route_packets({}, {'server_uuid': 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'})
            with self.assertRaises(ed.EditorError) as caught:
                self.call('/api/editor/run', sql=sql, confirmed=True, confirm_token=review['confirmation']['token'])
        self.assertEqual(caught.exception.code, 'identity_mismatch')
        self.assertIn('SELECT DATABASE() AS database_name', self.packets()[-1][2])
        self.assertIn('SET @myflames_target = ', self.packets()[-1][2])
        self.assertNotIn(sql, self.session_log())
        record = self.store.audit_records()[0]
        self.assertEqual((record['status'], record['outcome']), ('refused', 'identity_mismatch'))

    def test_alter_event_body_and_trailing_comment_are_preserved_in_one_packet(self):
        sql = 'ALTER EVENT e DO BEGIN SELECT 1; SELECT 2; END; -- tail\n'
        result = self.run_sql('DELIMITER //\n' + sql + '//')
        self.assertEqual(result['status'], 'complete', result)
        packet = next(p[2] for p in self.packets() if 'ALTER EVENT e DO' in p[2])
        self.assertIn('SET @myflames_target = ', packet)
        self.assertTrue(packet.endswith(sql.rstrip()))
        self.assertEqual(self.store.audit_records()[0]['statements'], [sql.rstrip()])


class MariaDBEditorSetupPacketTests(EditorSetupPacketTests):
    engine = 'mariadb'

    def route_packets(self, *routes):
        # The transport checks MariaDB's composite guard rather than a UUID.
        identity_fixtures.EditorIdentityTests.route_packets(self, *(dict(route, mismatch=True) if 'server_uuid' in route else route
                                               for route in routes))

    def route_matching(self, needle, field='server_uuid'):
        super().route_matching(needle, 'connection_id')
