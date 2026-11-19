"""Run-time SQL-mode predictions, startup replanning and login diagnostics.

The disposable client supplies server answers only. Production planning,
classification, review tokens, packet guards, framing and audit stay intact.
"""
import os
import unittest
from unittest import mock

import test_editor_session as fixtures
import test_editor_session_identity as identity_fixtures
from myflames import connector, dbops, editor_session as ed


@unittest.skipIf(os.name == 'nt', 'the fake client is a POSIX script')
class EditorRunPredictionTests(unittest.TestCase):
    engine = 'mysql'
    setUp = fixtures.ProcessTests.setUp
    call = fixtures.ProcessTests.call
    run_sql = fixtures.ProcessTests.run_sql
    rows = fixtures.ProcessTests.rows
    session_log = fixtures.ProcessTests.session_log
    rewrite_client = identity_fixtures.EditorIdentityTests.rewrite_client
    inject_answers = identity_fixtures.EditorIdentityTests.inject_answers
    packets = identity_fixtures.EditorIdentityTests.packets

    def test_literal_mode_prediction_reaches_each_bound_packet(self):
        cases = (
            ('NO_AUTO_VALUE_ON_ZERO', r"SELECT 'it\'s'", 0),
            ('NO_AUTO_VALUE_ON_ZERO', r"INSERT INTO t VALUES ('it\'s')", 0),
            ('NO_BACKSLASH_ESCAPES', r"SELECT 'tail\'", 1),
            ('NO_BACKSLASH_ESCAPES', r"INSERT INTO t VALUES ('tail\')", 1),
            ('ANSI_QUOTES', r'SELECT 1 AS "a\"; DELETE FROM t; -- "', 0),
        )
        for index, (mode, following, no_backslash) in enumerate(cases):
            tab = 'mode-%d' % index
            with self.subTest(mode=mode, following=following):
                self.call('/api/editor/open', tab=tab)
                if following.startswith('INSERT'):
                    created = self.run_sql('CREATE TEMPORARY TABLE t (note VARCHAR(100))', tab=tab)
                    self.assertEqual(created['status'], 'complete', created)
                sql = "SET sql_mode='%s'; %s" % (mode, following)
                before = len(self.packets())
                log = self.session_log(tab)
                tokens = dict(self.registry.tokens)
                review = self.call('/api/editor/run', tab=tab, sql=sql)
                if mode == 'ANSI_QUOTES' and self.engine == 'mysql':
                    # The mysql client ignores ANSI_QUOTES. Its one unit
                    # contains two server statements, so even SET must wait.
                    self.assertEqual((review['status'], review.get('code')), ('invalid', 'invalid_statement'), review)
                    self.assertIn('several statements on the server', review['message'])
                    self.assertIn('one for the client', review['message'])
                    self.assertIn('ending with the delimiter', review['message'])
                    self.assertNotIn('confirmation', review)
                    self.assertEqual(len(self.packets()), before)
                    self.assertEqual(self.session_log(tab), log)
                    self.assertEqual(self.registry.tokens, tokens)
                    continue
                following_statements = ([r'SELECT 1 AS "a\"', 'DELETE FROM t']
                                        if mode == 'ANSI_QUOTES' else [following])
                statements = ["SET sql_mode='%s'" % mode] + following_statements
                self.assertEqual(review['status'], 'needs_confirmation', review)
                self.assertEqual(review['confirmation']['statements'], statements)
                self.assertEqual(len(self.packets()), before, 'review must send no user SQL or helpers')
                result = self.call('/api/editor/run', tab=tab, sql=sql, confirmed=True,
                                   confirm_token=review['confirmation']['token'])
                self.assertEqual(result['status'], 'complete', result)
                self.assertEqual(result['run']['counts']['run'], len(statements))
                # Packet framing drops terminal delimiters/comments. The
                # executable SQL must still appear exactly once and intact.
                for statement in statements:
                    self.assertEqual(self.session_log(tab).count(statement), 1, statement)
                if mode == 'ANSI_QUOTES':
                    self.assertEqual([unit['kind'] for unit in result['run']['statements']],
                                     ['write', 'read', 'risky'])
                tag = self.registry.get(tab).statement_tag
                for statement in following_statements:
                    packet = next(p[2] for p in reversed(self.packets()) if statement in p[2].replace(tag, ''))
                    self.assertIn("FIND_IN_SET('NO_BACKSLASH_ESCAPES', @@SESSION.sql_mode) > 0) <=> %d" % no_backslash,
                                  packet)
                    self.assertIn("FIND_IN_SET('ANSI_QUOTES', @@SESSION.sql_mode) > 0) <=> %d" % (mode == 'ANSI_QUOTES'),
                                  packet)
                    self.assertIn('CONNECTION_ID() <> %d' % self.registry.get(tab).connection_id, packet)
                record = next(r for r in self.store.audit_records() if r['id'] == result['run']['audit_id'])
                self.assertEqual(record['statements'], review['confirmation']['statements'])

    def test_unknown_mode_refuses_server_client_count_disagreement_without_review(self):
        cases = [r'SELECT 1 AS "a\"; DELETE FROM t; -- "']
        if self.engine == 'mariadb':
            cases.append("SELECT 1 AS [a'] ; DELETE FROM t; -- '")
        self.call('/api/editor/open')
        self.registry.get('tab1').sql_mode = None  # Model an unavailable session-mode fact.
        before = len(self.packets())
        log = self.session_log()
        for sql in cases:
            with self.subTest(sql=sql):
                result = self.call('/api/editor/run', sql=sql)
                self.assertEqual((result['status'], result['code']), ('invalid', 'invalid_statement'), result)
                self.assertIn('quote mode is unknown', result['message'])
                self.assertNotIn('confirmation', result)
                self.assertEqual(len(self.packets()), before)
                self.assertEqual(self.session_log(), log)
                self.assertFalse(self.registry.tokens)
                self.assertFalse(self.store.audit_records())

    def test_failed_literal_assignment_cannot_authorize_later_sensitive_sql(self):
        self.inject_answers('''        if upper == "SET SQL_MODE='NO_BACKSLASH_ESCAPES'":
            self.error(line, 1231, "42000", "Mode assignment refused")
            return
''')
        self.call('/api/editor/open')
        following = r"INSERT INTO t VALUES ('tail\')"
        result = self.run_sql("SET sql_mode='NO_BACKSLASH_ESCAPES'; " + following, on_error='continue')
        self.assertEqual(result['status'], 'error', result)
        self.assertEqual([s['status'] for s in result['run']['statements']], ['error', 'not_run'])
        self.assertNotIn(following, self.session_log())
        record = next(r for r in self.store.audit_records() if r['id'] == result['run']['audit_id'])
        self.assertEqual(record['outcome'], 'identity_mismatch')

    def test_nonliteral_mode_change_still_refuses_ambiguous_following_sql(self):
        self.call('/api/editor/open')
        count = len(self.packets())
        result = self.call('/api/editor/run', sql=r"SET sql_mode=CONCAT('ANSI_', 'QUOTES'); SELECT 'it\'s'")
        self.assertEqual((result['status'], result['code']), ('invalid', 'invalid_statement'), result)
        self.assertEqual(len(self.packets()), count)

    def test_packet_prediction_still_requires_the_complete_identity(self):
        self.call('/api/editor/open')
        sql = 'INSERT INTO t VALUES (1)'
        review = self.call('/api/editor/run', sql=sql)
        session = self.registry.get('tab1')
        # Removing facts models lost identity; classification and gates run normally.
        session.server_identity = None
        with self.assertRaises(ed.EditorError) as caught:
            self.call('/api/editor/run', sql=sql, confirmed=True,
                      confirm_token=review['confirmation']['token'])
        self.assertEqual(caught.exception.code, 'identity_unavailable')
        self.assertNotIn(sql, self.session_log())

    def test_first_run_skipped_alter_has_no_preflight_or_lock_setting(self):
        markers = ('999999', '80000') if self.engine == 'mariadb' else ('999999',)
        for marker in markers:
            tab = 'skipped-' + marker
            sql = '/*!%s ALTER TABLE tt ADD COLUMN c INT */ SELECT 1' % marker
            with self.subTest(marker=marker), mock.patch.object(ed, '_alter_review', wraps=ed._alter_review) as review:
                result = self.call('/api/editor/run', tab=tab, sql=sql)
                self.assertEqual(result['status'], 'complete', result)
                review.assert_not_called()
                self.assertEqual(result['run']['counts']['run'], 1)
                self.assertNotIn(ed.ALTER_LOCK_WAIT, self.session_log(tab))
                self.assertFalse(any('SET SESSION lock_wait_timeout' in p[2] for p in self.packets()))
                self.assertFalse(self.store.audit_records())

    def test_first_run_executable_alter_requires_bound_preflight_review(self):
        sql = '/*!40101 ALTER TABLE tt ADD COLUMN c INT */'
        with mock.patch.object(ed, '_alter_review', return_value={'schema': 'test', 'table': 'tt', 'warnings': []}) as review:
            result = self.call('/api/editor/run', sql=sql)
        self.assertEqual(result['status'], 'needs_confirmation', result)
        self.assertIsNotNone(self.registry.get('tab1'))
        review.assert_called_once()
        self.assertEqual(result['confirmation']['statements'], [ed.ALTER_LOCK_WAIT, sql])
        self.assertNotIn(sql, self.session_log())

    def test_alter_revealed_after_startup_needs_a_new_bound_review(self):
        sql = '/*!999999 CREATE */ ALTER TABLE tt ADD COLUMN c INT'
        initial = self.call('/api/editor/run', sql=sql)
        self.assertEqual(initial['status'], 'needs_confirmation', initial)
        self.assertIsNone(self.registry.get('tab1'))
        self.assertEqual(initial['confirmation']['statements'], [sql])
        with mock.patch.object(ed, '_alter_review', return_value={'schema': 'test', 'table': 'tt', 'warnings': []}) as review:
            result = self.call('/api/editor/run', sql=sql, confirmed=True,
                               confirm_token=initial['confirmation']['token'])
        self.assertEqual(result['status'], 'needs_confirmation', result)
        review.assert_called_once()
        self.assertNotEqual(initial['confirmation']['token'], result['confirmation']['token'])
        self.assertEqual(result['confirmation']['statements'], [ed.ALTER_LOCK_WAIT, sql])
        self.assertNotIn(sql, self.session_log())

    def test_first_run_explain_does_not_prompt_when_startup_changes_prefix(self):
        self.inject_answers('''        if upper in ("EXPLAIN SELECT 1", "EXPLAIN FORMAT=TRADITIONAL SELECT 1"):
            self.result(statement, ["id", "select_type"], [["1", "SIMPLE"]])
            return
''')
        result = self.call('/api/editor/run', sql='SELECT 1', explain_format='table')
        self.assertEqual(result['status'], 'complete', result)
        expected = 'EXPLAIN SELECT 1' if self.engine == 'mariadb' else 'EXPLAIN FORMAT=TRADITIONAL SELECT 1'
        self.assertEqual(result['run']['statements'][0]['sent'], expected)
        self.assertEqual(self.session_log().count(expected), 1)
        self.assertFalse(self.store.audit_records())

    def test_expired_password_hint_in_handshake_notes_reports_1862(self):
        # Wait for the first helper's input, avoiding a startup write/exit race.
        self.rewrite_client(lambda source: source.replace('    client = Client()\n',
            '    for _ in range(4):\n        sys.stdin.buffer.readline()\n'
            '    sys.stdout.write(%r)\n    return 1\n    client = Client()\n' %
            (connector._EXPIRED_PASSWORD_HINT + '\n')))
        with self.assertRaises(connector.ConnectorError) as caught:
            self.call('/api/editor/open')
        self.assertEqual(str(caught.exception), connector.EXPIRED_PASSWORD_MESSAGE)
        self.assertEqual(dbops.error_code(str(caught.exception)), 1862)
        self.assertFalse(self.registry.sessions)


class MariaDBRunPredictionTests(EditorRunPredictionTests):
    engine = 'mariadb'

    def test_mssql_brackets_refuse_before_mode_assignment_or_review(self):
        # Neither client recognizes MariaDB's bracket identifiers. Both see
        # the semicolons inside a string, while the server sees a DELETE.
        sql = "SET sql_mode='MSSQL'; SELECT 1 AS [a'] ; DELETE FROM t; -- '"
        self.call('/api/editor/open')
        before = len(self.packets())
        log = self.session_log()
        result = self.call('/api/editor/run', sql=sql)
        self.assertEqual((result['status'], result.get('code')), ('invalid', 'invalid_statement'), result)
        self.assertIn('several statements on the server', result['message'])
        self.assertIn('one for the client', result['message'])
        self.assertIn('ending with the delimiter', result['message'])
        self.assertNotIn('confirmation', result)
        self.assertEqual(len(self.packets()), before)
        self.assertEqual(self.session_log(), log)
        self.assertFalse(self.registry.tokens)
        self.assertFalse(self.store.audit_records())
