"""First-run consent with real session startup and disposable client processes.

Only server answers are synthetic. Planning, classification, policy, tokens,
identity guards, framing and audit use the production implementations.
"""
import copy
import os
import unittest

import test_editor_session as fixtures
import test_editor_session_identity as identity_fixtures
from myflames import editor_session as ed


@unittest.skipIf(os.name == 'nt', 'the fake client is a POSIX script')
class EditorFirstRunTests(unittest.TestCase):
    engine = 'mysql'
    setUp = fixtures.ProcessTests.setUp
    call = fixtures.ProcessTests.call
    rows = fixtures.ProcessTests.rows
    session_log = fixtures.ProcessTests.session_log
    rewrite_client = identity_fixtures.EditorIdentityTests.rewrite_client
    inject_answers = identity_fixtures.EditorIdentityTests.inject_answers
    packets = identity_fixtures.EditorIdentityTests.packets

    def answer_version(self, version, transaction_tracking=True):
        self.rewrite_client(lambda source: source.replace(
            '"11.4.13-MariaDB-fake" if ENGINE == "mariadb" else "8.4.11-fake"', repr(version)))
        if not transaction_tracking:
            # Model an unavailable transaction consumer, as on the GA 5.7
            # server. SQL mode and server identity still come from startup.
            os.environ['FAKE_PS'] = 'off'

    def test_direct_first_read_on_the_default_engine_needs_no_confirmation(self):
        result = self.call('/api/editor/run', sql='SELECT 7')
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(self.rows(result), [['7']])
        self.assertEqual(self.session_log().count('SELECT 7'), 1)
        self.assertFalse(self.store.audit_records(), 'an allowed read needs no review')

    def test_direct_first_read_needs_no_confirmation_with_unknown_transaction_state(self):
        if self.engine == 'mysql':
            self.answer_version('5.7.44-fake', transaction_tracking=False)
        self.inject_answers('''        if upper == "SELECT @@HOSTNAME AS HOST, CONNECTION_ID() AS ID":
            self.result(statement, ["host", "id"], [["fake-db", str(self.conn_id)]])
            return
''')
        sql = 'SELECT @@hostname AS host, CONNECTION_ID() AS id'
        result = self.call('/api/editor/run', sql=sql)
        session = self.registry.get('tab1')
        if self.engine == 'mysql':
            provisional = ed._decision(ed._plan_run({'sql': sql}, None),
                                       {'type': 'production', 'read_only': False})
            started = ed._decision(ed._plan_run({'sql': sql}, session),
                                   {'type': 'production', 'read_only': False}, session)
            self.assertEqual({key for key in provisional if provisional[key] != started[key]},
                             {'in_transaction'})
            self.assertIs(provisional['in_transaction'], False)
            self.assertIsNone(started['in_transaction'])
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(self.rows(result), [['fake-db', str(session.connection_id)]])
        self.assertEqual(self.session_log().count(sql), 1)
        self.assertFalse(self.store.audit_records(), 'ordinary reads do not produce write reviews')

    def test_first_create_database_uses_one_confirmation_and_runs_once(self):
        version = '11.4.13-MariaDB-fake' if self.engine == 'mariadb' else '5.7.44-fake'
        self.answer_version(version, transaction_tracking=self.engine != 'mysql')
        for suffix in ('', " /*!80016 DEFAULT ENCRYPTION='N' */"):
            sql = 'CREATE DATABASE `first_run`' + suffix
            tab = 'create-' + str(bool(suffix))
            initial = self.call('/api/editor/run', tab=tab, sql=sql)
            self.assertEqual(initial['status'], 'needs_confirmation', initial)
            self.assertIsNone(self.registry.get(tab))
            self.assertEqual(initial['confirmation']['statements'], [sql])
            result = self.call('/api/editor/run', tab=tab, sql=sql, confirmed=True,
                               confirm_token=initial['confirmation']['token'])
            session = self.registry.get(tab)
            self.assertEqual(session.server_version, version)
            if self.engine == 'mysql':
                self.assertIsNone(session.in_transaction)
                safety = {'type': 'production', 'read_only': False}
                before = ed._decision(ed._plan_run({'sql': sql}, None), safety)
                after = ed._decision(ed._plan_run({'sql': sql}, session), safety, session)
                self.assertEqual({key for key in before if before[key] != after[key]},
                                 {'in_transaction', 'warnings'})
                self.assertEqual(before['warnings'], [])
                self.assertIn('may be open', after['warnings'][0])
            self.assertEqual(result['status'], 'complete', result)
            self.assertEqual(result['run']['counts']['run'], 1)
            self.assertEqual(self.session_log(tab).count(sql), 1)
            self.assertEqual(sum(p[2].endswith(sql) for p in self.packets()), 1)
            replay = self.call('/api/editor/run', tab=tab, sql=sql, confirmed=True,
                               confirm_token=initial['confirmation']['token'])
            self.assertEqual(replay['status'], 'needs_confirmation', replay)
            self.assertEqual(self.session_log(tab).count(sql), 1)

    def test_default_engine_first_create_database_uses_one_confirmation(self):
        version = '11.4.13-MariaDB-fake' if self.engine == 'mariadb' else '8.4.11-fake'
        self.answer_version(version)
        sql = 'CREATE DATABASE `default_engine_first_run`'
        initial = self.call('/api/editor/run', sql=sql)
        self.assertEqual(initial['status'], 'needs_confirmation', initial)
        result = self.call('/api/editor/run', sql=sql, confirmed=True,
                           confirm_token=initial['confirmation']['token'])
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(result['session']['server_version'], version)
        self.assertEqual(self.session_log().count(sql), 1)
        self.assertEqual(sum(p[2].endswith(sql) for p in self.packets()), 1)

    def test_another_tabs_provisional_token_sends_nothing(self):
        sql = 'CREATE DATABASE `other_tab`'
        initial = self.call('/api/editor/run', sql=sql)
        refused = self.call('/api/editor/run', tab='tab2', sql=sql, confirmed=True,
                            confirm_token=initial['confirmation']['token'])
        self.assertEqual(refused['status'], 'needs_confirmation', refused)
        self.assertIsNone(self.registry.get('tab2'))
        self.assertFalse((self.state / 'packets.log').exists(),
                         'a different tab cannot even start with this token')

    def test_another_started_tabs_token_sends_nothing(self):
        sql = 'CREATE DATABASE `other_started_tab`'
        self.call('/api/editor/open')
        initial = self.call('/api/editor/run', sql=sql)
        self.assertEqual(initial['status'], 'needs_confirmation', initial)
        self.call('/api/editor/open', tab='tab2')
        packets = len(self.packets())
        refused = self.call('/api/editor/run', tab='tab2', sql=sql, confirmed=True,
                            confirm_token=initial['confirmation']['token'])
        self.assertEqual(refused['status'], 'needs_confirmation', refused)
        self.assertEqual(len(self.packets()), packets, 'another session cannot use this token')
        self.assertFalse(any(p[2].endswith(sql) for p in self.packets()))

    def test_provisional_confirmation_cannot_transfer_to_a_concurrently_opened_session(self):
        sql = 'CREATE DATABASE `concurrent_session`'
        initial = self.call('/api/editor/run', sql=sql)
        self.assertEqual(initial['status'], 'needs_confirmation', initial)

        def open_during_password_lookup(payload):
            # Deterministically model a second request opening this tab after
            # the provisional token is consumed but before _open_session.
            self.call('/api/editor/open')
            return payload

        payload = dict(tab_id='tab1', connection=dict(self.connection), sql=sql, wait=15,
                       confirmed=True, confirm_token=initial['confirmation']['token'])
        refused = ed.api_response('/api/editor/run', payload, self.workspace,
                                  fill_password=open_during_password_lookup)
        self.assertEqual(refused['status'], 'needs_confirmation', refused)
        self.assertFalse(any(p[2].endswith(sql) for p in self.packets()))
        result = self.call('/api/editor/run', sql=sql, confirmed=True,
                           confirm_token=refused['confirmation']['token'])
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(self.session_log().count(sql), 1)

    def test_started_and_unused_provisional_tokens_cannot_authorize_replacement_session(self):
        sql = 'CREATE DATABASE `replacement`'
        provisional = self.call('/api/editor/run', sql=sql)
        self.call('/api/editor/open')
        initial = self.call('/api/editor/run', sql=sql)
        old_session = self.registry.get('tab1')
        self.assertTrue(self.call('/api/editor/close', discard=True)['closed'])
        self.assertIsNotNone(old_session.proc.poll())
        self.call('/api/editor/open')
        self.assertNotEqual(old_session.statement_tag, self.registry.get('tab1').statement_tag)
        for review in (provisional, initial):
            refused = self.call('/api/editor/run', sql=sql, confirmed=True,
                                confirm_token=review['confirmation']['token'])
            self.assertEqual(refused['status'], 'needs_confirmation', refused)
            self.assertFalse(any(p[2].endswith(sql) for p in self.packets()))
        result = self.call('/api/editor/run', sql=sql, confirmed=True,
                           confirm_token=refused['confirmation']['token'])
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(self.session_log().count(sql), 1)

    def test_review_state_ignores_explanations_but_keeps_every_consent_requirement(self):
        self.call('/api/editor/open')
        session = self.registry.get('tab1')
        plan = ed._plan_run({'sql': 'SET @first_run = 1; SELECT 7'}, session)
        decision = ed._decision(plan, {'type': 'production', 'read_only': False}, session)
        state = ed._run_review_state(plan, decision)
        statements = [unit['send'] for unit in plan['units']]

        def digest(review_plan, review_decision):
            return ed._review_digest('tab1', session.identity, statements, review_decision,
                                     'stop', session, review_plan)

        token_state = digest(plan, decision)
        informational = copy.deepcopy(plan)
        changed_decision = copy.deepcopy(decision)
        changed_decision.update(reasons=['More detail'], text='New explanation', title='New title',
                                warnings=['Transaction tracking is unavailable'], in_transaction=None)
        informational['verdict']['reasons'] = ['New aggregate explanation']
        informational['verdict']['statements'][0]['reasons'] = ['New statement explanation']
        informational['units'][0]['reasons'] = ['New unit explanation']
        self.assertEqual(ed._run_review_state(informational, changed_decision), state)
        self.assertEqual(digest(informational, changed_decision), token_state)
        for key, value in (('action', 'refuse'), ('typed_target', 'production'),
                           ('alter_preflight', {'schema': 'd', 'table': 't'}),
                           ('alter_binding', [41, {'server_uuid': 'changed'}, 'd', 't'])):
            with self.subTest(decision_field=key):
                changed = dict(decision, **{key: value})
                self.assertNotEqual(ed._run_review_state(plan, changed), state)
                self.assertNotEqual(digest(plan, changed), token_state)
        for index in range(len(plan['units'])):
            for key, value in (('send', 'SET @changed = 2'), ('kind', 'risky'), ('category', 'ddl'),
                               ('verb', 'DROP'), ('single', False),
                               ('session_control', not plan['units'][index]['session_control']),
                               ('conditional_sql', True)):
                with self.subTest(unit=index, unit_field=key):
                    changed = copy.deepcopy(plan)
                    changed['units'][index][key] = value
                    self.assertNotEqual(ed._run_review_state(changed, decision), state)
                    self.assertNotEqual(digest(changed, decision), token_state)


class MariaDBEditorFirstRunTests(EditorFirstRunTests):
    engine = 'mariadb'
