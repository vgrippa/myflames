"""Workspace endpoints for privileges and Users and roles (accounts_api.py
through ui.api_response). Connections are fakes; nothing reaches a server."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import _accounts_replay as replay  # noqa: E402
from test_accounts import FakeConn, actor, catalog_replay  # noqa: E402

from myflames import accounts_api, privileges as p, ui  # noqa: E402
from myflames.connector import ConnectorError  # noqa: E402
from myflames.workspace_store import WorkspaceStore  # noqa: E402

CONNECTION = {'host': '127.0.0.1', 'port': 3306, 'user': 'admin', 'password': 'conn-secret'}


class Workspace(object):
    def __init__(self):
        self.store = WorkspaceStore()


class AccountsApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.dict(os.environ, {'HOME': self.temp.name, 'USERPROFILE': self.temp.name,
                                               'MYFLAMES_SECRET_BACKEND': 'vault'})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        self.workspace = Workspace()
        self.conn = FakeConn(exists=False, name='t_api')
        self.opened = []

        def factory(**options):
            self.opened.append(options)
            return self.conn
        patcher = mock.patch.object(accounts_api, 'MySQLConnection', side_effect=factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.real_read = p.read_privileges
        patcher = mock.patch.object(p, 'read_privileges', side_effect=lambda conn: actor(conn.engine))
        patcher.start()
        self.addCleanup(patcher.stop)
        accounts_api._PLANS.clear()

    def call(self, path, **payload):
        return ui.api_response(path, payload, self.workspace)

    def test_paths_are_registered(self):
        self.assertEqual(set(accounts_api.PATHS), set(ui._ACCOUNT_PATHS))
        self.assertIn('/api/accounts/apply', ui._WRITE_PATHS)
        for path in accounts_api.READ_PATHS:
            self.assertIn(path, ui._CONNECTION_PATHS)
            self.assertNotIn(path, ui._WRITE_PATHS)
        self.assertNotIn('/api/accounts/apply', ui._SERVER_VIEW_PATHS)

    def test_privileges_list_and_read(self):
        for server in ('mysql-8.4', 'mariadb-11.4'):
            with self.subTest(server=server):
                self.conn, data = replay.session(server, 'acct_app')
                with mock.patch.object(p, 'read_privileges', self.real_read):
                    report = self.call('/api/live/privileges', connection=CONNECTION)
                self.assertEqual(report['schema_version'], 'privileges-1.0')
                self.assertEqual(set(report['actions']), set(p.ACTIONS))
                for key, answer in report['actions'].items():
                    self.assertEqual({'state', 'missing', 'reason'} - set(answer), set(), key)
                self.conn, data = replay.reads(server)
                catalog_replay(self.conn, server)
                listing = self.call('/api/accounts/list', connection=CONNECTION)
                self.assertEqual(listing['schema_version'], 'accounts-1.0')
                account = self.call('/api/accounts/read', connection=CONNECTION, user='acct_app', host='%')
                self.assertEqual(account['schema_version'], 'account-1.0')
                self.assertEqual(len(account['fingerprint']), 64)
                text = json.dumps([listing, account])
                for secret in replay.secrets(data):
                    self.assertNotIn(secret, text)

    def test_scope_objects_endpoint_and_allowlist(self):
        self.assertIn('/api/accounts/scope-objects', accounts_api.READ_PATHS)
        original = self.conn.run
        def run(sql, **kwargs):
            if 'information_schema.TABLES' in sql:
                return 'orders\n'
            if 'information_schema.ROUTINES' in sql:
                return 'p\tPROCEDURE\nf\tFUNCTION\n'
            return original(sql, **kwargs)
        self.conn.run = run
        out = self.call('/api/accounts/scope-objects', connection=CONNECTION, database='shop')
        self.assertEqual(out['tables'], [{'name': 'orders'}])
        self.assertEqual(out['functions'], [{'name': 'f'}])
        self.assertEqual(out['procedures'], [{'name': 'p'}])
        self.assertFalse(out['truncated'])
        self.assertFalse(self.conn.stdin)
        with self.assertRaises(ValueError):
            self.call('/api/accounts/scope-objects', connection=CONNECTION, database='x' * 65)

    def test_plan_then_apply_by_plan_id(self):
        plan = self.call('/api/accounts/plan', connection=CONNECTION, operation='create', user='t_api', host='%',
                         password="Api'pw\\9!x")
        self.assertEqual(plan['schema_version'], 'account-plan-1.0')
        self.assertEqual(plan['plan_id'], plan['change_set_id'])
        self.assertEqual(plan['policy']['action'], 'confirm')
        self.assertNotIn("Api'pw", json.dumps(plan))
        self.assertNotIn('conn-secret', json.dumps(plan))
        self.assertEqual(plan['request'], {'operation': 'create', 'user': 't_api', 'host': '%', 'role': False})
        sent = []

        def run_stdin(sql, extra_flags=None, timeout=None):
            sent.append(sql)
            self.conn.exists = True
            return ''
        self.conn.run_stdin = run_stdin
        self.opened = []
        result = self.call('/api/accounts/apply', connection=CONNECTION, plan_id=plan['plan_id'],
                           expect=plan['expect'], confirmed=True, password="Api'pw\\9!x")
        self.assertEqual(result['schema_version'], 'account-change-1.0')
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(result['change_set_id'], plan['plan_id'])
        self.assertIn("'Api''pw\\9!x'", sent[0])
        self.assertNotIn("Api'pw", json.dumps(result))
        records = self.workspace.store.audit_records()
        self.assertEqual(records[0]['target']['change_set_id'], plan['plan_id'])
        self.assertNotIn("Api'pw", json.dumps(records))
        with self.assertRaisesRegex(ValueError, 'expired or is unknown'):
            self.call('/api/accounts/apply', connection=CONNECTION, plan_id=plan['plan_id'], expect=plan['expect'],
                      confirmed=True, password="Api'pw\\9!x")

    def test_policy_runs_before_any_connection(self):
        plan = self.call('/api/accounts/plan', connection=CONNECTION, operation='create', user='t_api', host='%',
                         password='x-pw')
        self.opened = []
        result = self.call('/api/accounts/apply', connection=CONNECTION, plan_id=plan['plan_id'],
                           expect=plan['expect'], confirmed=False, password='x-pw')
        self.assertEqual(result['status'], 'needs_confirmation')
        self.assertEqual(self.opened, [])
        result = self.call('/api/accounts/apply', connection=dict(CONNECTION, read_only=True), plan_id=plan['plan_id'],
                           expect=plan['expect'], confirmed=True, password='x-pw')
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(self.opened, [])
        statuses = [r['status'] for r in self.workspace.store.audit_records()]
        self.assertEqual(sorted(statuses), ['refused', 'refused'])
        self.assertNotIn('x-pw', json.dumps(self.workspace.store.audit_records()))

    def test_raw_plan_is_rejected_before_connection_or_secret_lookup(self):
        self.conn = FakeConn(name='t_api')
        plan = self.call('/api/accounts/plan', connection=CONNECTION, operation='lock', user='t_api', host='%')
        self.opened = []
        for raw in (plan, {'operation': 'lock', 'user': 't_api', 'host': '%'}):
            with self.subTest(raw=bool(raw.get('plan_id'))), \
                    mock.patch.object(ui, '_with_saved_password', wraps=ui._with_saved_password) as passwords:
                with self.assertRaisesRegex(ValueError, 'plan_id'):
                    self.call('/api/accounts/apply', connection=CONNECTION, plan=raw,
                              expect=plan['expect'], confirmed=True)
                passwords.assert_not_called()
        self.assertEqual(self.opened, [])
        self.assertEqual(self.conn.stdin, [])

    def test_missing_typed_name_is_refused_at_audit_insertion_and_plan_remains_usable(self):
        self.conn = FakeConn(name='root', host='localhost')
        plan = self.call('/api/accounts/plan', connection=CONNECTION, operation='lock', user='root', host='localhost')
        created = self.workspace._account_plans[plan['plan_id']]['created']
        rows = []
        original = self.workspace.store.audit_insert

        def insert(row):
            rows.append(dict(row))
            return original(row)
        with mock.patch.object(self.workspace.store, 'audit_insert', side_effect=insert):
            result = self.call('/api/accounts/apply', connection=CONNECTION, plan_id=plan['plan_id'],
                               expect=plan['expect'], confirmed=True)
        self.assertEqual(result['status'], 'needs_typed_name')
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['status'], rows[0]['outcome']), ('refused', 'needs_typed_name'))
        self.assertEqual(self.conn.stdin, [])
        self.assertEqual(self.workspace._account_plans[plan['plan_id']]['created'], created)
        result = self.call('/api/accounts/apply', connection=CONNECTION, plan_id=plan['plan_id'],
                           expect=plan['expect'], confirmed=True, typed_name='root@localhost')
        self.assertEqual(result['status'], 'applied')

    def test_changed_statements_or_account_are_stale(self):
        self.conn = FakeConn(name='t_api')
        plan = self.call('/api/accounts/plan', connection=CONNECTION, operation='grant', user='t_api', host='%',
                         privileges=['SELECT'], level='database', database='shop')
        # The server rebuild must match the statements in its reviewed plan.
        reviewed = self.workspace._account_plans[plan['plan_id']]
        original = list(reviewed['statements'])
        reviewed['statements'] = [original[0].replace('SELECT', 'SELECT, INSERT')]
        result = self.call('/api/accounts/apply', connection=CONNECTION, plan_id=plan['plan_id'],
                           expect=plan['expect'], confirmed=True)
        self.assertEqual(result['status'], 'stale')
        plan = self.call('/api/accounts/plan', connection=CONNECTION, operation='grant', user='t_api', host='%',
                         privileges=['SELECT'], level='database', database='shop')
        self.conn.grants.append('GRANT PROCESS ON *.* TO `t_api`@`%`')
        result = self.call('/api/accounts/apply', connection=CONNECTION, plan_id=plan['plan_id'], expect=plan['expect'],
                           confirmed=True)
        self.assertEqual(result['status'], 'stale')
        self.assertIn('nothing was sent', result['message'])
        self.assertEqual(self.conn.stdin, [])

    def test_precondition_drift_is_an_audited_stale_refusal(self):
        for engine in ('mysql', 'mariadb'):
            for operation in ('create', 'lock', 'revoke'):
                with self.subTest(engine=engine, operation=operation):
                    self.conn = FakeConn(engine=engine, exists=operation != 'create', name='t_api')
                    options = {'random_password': True} if operation == 'create' else (
                        {'privileges': ['SELECT'], 'level': 'global'} if operation == 'revoke' else {})
                    plan = self.call('/api/accounts/plan', connection=CONNECTION, operation=operation,
                                     user='t_api', host='%', **options)
                    if operation == 'revoke':
                        self.conn.grants = ['GRANT USAGE ON *.* TO `t_api`@`%`']
                    else:
                        self.conn.exists = operation == 'create'
                    with mock.patch.object(accounts_api, '_build', side_effect=AssertionError('rebuilt stale plan')):
                        result = self.call('/api/accounts/apply', connection=CONNECTION, plan_id=plan['plan_id'],
                                           expect=plan['expect'], confirmed=True)
                    self.assertEqual(result['schema_version'], 'account-change-1.0')
                    self.assertEqual(result['status'], 'stale')
                    self.assertEqual(result['change_set_id'], plan['plan_id'])
                    self.assertIn('nothing was sent', result['message'])
                    record = next(r for r in self.workspace.store.audit_records()
                                  if r['id'] == result['audit_ids'][0])
                    self.assertEqual((record['status'], record['outcome']), ('refused', 'stale'))
                    self.assertEqual(record['target']['change_set_id'], plan['plan_id'])
                    self.assertEqual(self.conn.stdin, [])
                    with self.assertRaisesRegex(ValueError, 'expired or is unknown'):
                        self.call('/api/accounts/apply', connection=CONNECTION, plan_id=plan['plan_id'],
                                  expect=plan['expect'], confirmed=True)

    def test_drift_during_rebuild_is_stale_but_invalid_input_stays_an_error(self):
        original = accounts_api._build
        for changed in (True, False):
            with self.subTest(changed=changed):
                self.conn = FakeConn(name='t_api')
                plan = self.call('/api/accounts/plan', connection=CONNECTION, operation='lock', user='t_api', host='%')
                before = len(self.workspace.store.audit_records())

                def build(*args, **kwargs):
                    if changed:
                        self.conn.exists = False
                        return original(*args, **kwargs)
                    raise ValueError('Invalid option')
                with mock.patch.object(accounts_api, '_build', side_effect=build):
                    if changed:
                        result = self.call('/api/accounts/apply', connection=CONNECTION, plan_id=plan['plan_id'],
                                           expect=plan['expect'], confirmed=True)
                        self.assertEqual(result['status'], 'stale')
                        self.assertEqual(len(self.workspace.store.audit_records()), before + 1)
                    else:
                        with self.assertRaisesRegex(ValueError, 'Invalid option'):
                            self.call('/api/accounts/apply', connection=CONNECTION, plan_id=plan['plan_id'],
                                      expect=plan['expect'], confirmed=True)
                self.assertEqual(self.conn.stdin, [])

    def test_plan_capacity_is_per_workspace_and_cli_fallback_is_separate(self):
        other = Workspace()
        with mock.patch.object(accounts_api, 'MAX_PLANS', 1):
            first = self.call('/api/accounts/plan', connection=CONNECTION, operation='create', user='t_api', host='%',
                              random_password=True)
            other_plan = ui.api_response('/api/accounts/plan', dict(connection=CONNECTION, operation='create',
                                         user='t_api', host='%', random_password=True), other)
            second = self.call('/api/accounts/plan', connection=CONNECTION, operation='create', user='t_api', host='%',
                               random_password=True)
            self.assertEqual(list(self.workspace._account_plans), [second['plan_id']])
            self.assertEqual(list(other._account_plans), [other_plan['plan_id']])
            identity = second['connection_identity']
            accounts_api._remember(first, identity)
            entry = accounts_api._recall(first['plan_id'], identity, consume=True)
            self.assertEqual(entry['expect'], first['expect'])
            with self.assertRaisesRegex(ValueError, 'expired or is unknown'):
                accounts_api._recall(first['plan_id'], identity)
            self.assertEqual(list(self.workspace._account_plans), [second['plan_id']])

    def test_changed_expect_cannot_replace_reviewed_fingerprint(self):
        self.conn = FakeConn(name='t_api')
        plan = self.call('/api/accounts/plan', connection=CONNECTION, operation='lock', user='t_api', host='%')
        self.conn.grants.append('GRANT PROCESS ON *.* TO `t_api`@`%`')
        current = self.call('/api/accounts/read', connection=CONNECTION, user='t_api', host='%')
        result = self.call('/api/accounts/apply', connection=CONNECTION, plan_id=plan['plan_id'],
                           expect=current['fingerprint'], confirmed=True)
        self.assertEqual(result['status'], 'stale')
        self.assertEqual(self.conn.stdin, [])

    def test_plans_are_workspace_scoped_expire_and_are_claimed_once(self):
        plan = self.call('/api/accounts/plan', connection=CONNECTION, operation='create', user='t_api', host='%',
                         random_password=True)
        other = Workspace()
        self.assertEqual(accounts_api._PLANS, {})
        with self.assertRaisesRegex(ValueError, 'expired or is unknown'):
            ui.api_response('/api/accounts/apply', dict(connection=CONNECTION, plan_id=plan['plan_id'],
                            expect=plan['expect'], confirmed=True), other)
        entry = self.workspace._account_plans[plan['plan_id']]
        with mock.patch.object(accounts_api.time, 'monotonic', return_value=entry['created'] +
                               accounts_api.PLAN_TTL_SECONDS + 1):
            with self.assertRaisesRegex(ValueError, 'expired or is unknown'):
                self.call('/api/accounts/apply', connection=CONNECTION, plan_id=plan['plan_id'],
                          expect=plan['expect'], confirmed=True)
        plan = self.call('/api/accounts/plan', connection=CONNECTION, operation='create', user='t_api', host='%',
                         random_password=True)
        original = accounts_api._build

        def build(*args, **kwargs):
            # A second request arriving while the first builds cannot reuse its id.
            with self.assertRaisesRegex(ValueError, 'expired or is unknown'):
                self.call('/api/accounts/apply', connection=CONNECTION, plan_id=plan['plan_id'],
                          expect=plan['expect'], confirmed=True)
            return original(*args, **kwargs)
        with mock.patch.object(accounts_api, '_build', side_effect=build):
            result = self.call('/api/accounts/apply', connection=CONNECTION, plan_id=plan['plan_id'],
                               expect=plan['expect'], confirmed=True)
        self.assertEqual(result['status'], 'applied')

    def test_plan_for_another_connection_is_refused(self):
        plan = self.call('/api/accounts/plan', connection=CONNECTION, operation='create', user='t_api', host='%',
                         random_password=True)
        # A read-only switch after the review is refused by the policy, not as another connection.
        result = self.call('/api/accounts/apply', connection=dict(CONNECTION, read_only=True), plan_id=plan['plan_id'],
                           expect=plan['expect'], confirmed=True)
        self.assertEqual(result['status'], 'refused')
        with self.assertRaisesRegex(ValueError, 'another connection'):
            self.call('/api/accounts/apply', connection=dict(CONNECTION, host='10.0.0.9'), plan_id=plan['plan_id'],
                      expect=plan['expect'], confirmed=True)

    def test_request_validation(self):
        for payload, message in [({'operation': 'fly', 'user': 'x'}, 'operation'),
                                  ({'operation': 'lock', 'user': 'x', 'password': 'pw'}, 'only accepted'),
                                  ({'operation': 'grant', 'user': 'x', 'privileges': 'SELECT'}, 'wrong type'),
                                  ({'operation': 'roles', 'user': 'x', 'roles': [{'name': 1}]}, 'roles'),
                                  ({'operation': 'lock', 'user': 'x', 'role': 'yes'}, 'role')]:
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, message):
                self.call('/api/accounts/plan', connection=CONNECTION, **payload)
        with self.assertRaisesRegex(ValueError, 'plan_id'):
            self.call('/api/accounts/apply', connection=CONNECTION, plan={'operation': 'lock', 'user': 'x',
                                                                          'plan_id': '../etc'}, expect='e')

    def test_oversized_requests_refuse_before_copy_password_lookup_or_connection(self):
        secret = 'sensitive-account-value'
        payloads = [dict(operation='options', user='t_api', limits={'unknown': secret * 15000}),
                    dict(operation='grant', user='t_api', columns=['é' * 140000]),
                    dict(operation='grant', user='t_api', columns=['\x01' * 50000]),
                    dict(operation='roles', user='t_api', roles=['r'] * 201),
                    dict(operation='options', user='t_api', limits={'nodes': [0] * 10001})]
        nested = 0
        for unused in range(40):
            nested = [nested]
        payloads.append(dict(operation='options', user='t_api', limits={'nested': nested}))
        for payload in payloads:
            with self.subTest(keys=list(payload)):
                with mock.patch.object(accounts_api.copy, 'deepcopy') as copier, \
                        mock.patch.object(ui, '_with_saved_password') as fill:
                    with self.assertRaises(ValueError) as caught:
                        self.call('/api/accounts/plan', connection=CONNECTION, **payload)
                    self.assertEqual(copier.call_count, 0)
                    fill.assert_not_called()
                self.assertEqual(self.opened, [])
                self.assertNotIn(secret, str(caught.exception))
                self.assertNotIn(CONNECTION['password'], str(caught.exception))

    def test_apply_request_limit_does_not_consume_reviewed_plan(self):
        plan = self.call('/api/accounts/plan', connection=CONNECTION, operation='create', user='t_api',
                         random_password=True)
        self.opened = []
        with mock.patch.object(ui, '_with_saved_password') as fill:
            with self.assertRaisesRegex(ValueError, 'too large'):
                self.call('/api/accounts/apply', connection=CONNECTION, plan_id=plan['plan_id'],
                          expect=plan['expect'], confirmed=True, typed_name='x' * 300000)
            fill.assert_not_called()
        self.assertIn(plan['plan_id'], self.workspace._account_plans)
        self.assertEqual(self.opened, [])

    def test_utf8_budget_counts_bytes_and_python_storage_counts_containers(self):
        self.assertEqual(accounts_api._measure({'x': 'é'}, 10, 'Account request'), 10)
        with self.assertRaisesRegex(ValueError, 'too large'):
            accounts_api._measure({'x': 'é'}, 9, 'Account request')
        self.assertGreater(accounts_api._measure({'x': [1]}, 10000, 'Account plan', memory=True),
                           accounts_api._measure({'x': [1]}, 10000, 'Account request'))

    def test_request_measurement_handles_cycles_and_invalid_unicode(self):
        cycle = []
        cycle.append(cycle)
        for value in (cycle, '\ud800', float('inf'), 1 << 2000):
            with self.subTest(kind=type(value).__name__), self.assertRaises(ValueError):
                accounts_api.request_from(dict(operation='options', user='t_api', limits={'bad': value}))
        self.assertEqual(self.opened, [])

    @staticmethod
    def retained_plan(number, statements=None):
        return {'change_set_id': '%032x' % number, 'request': {'operation': 'lock', 'user': 'u', 'host': '%'},
                'expect': 'fingerprint', 'statements': statements or ['ALTER USER `u`@`%` ACCOUNT LOCK']}

    def test_retained_plan_and_statement_caps_precede_copy_and_preserve_other_reviews(self):
        first = self.retained_plan(1)
        accounts_api._remember(first, 'owner', self.workspace)
        huge = self.retained_plan(2, ['x' * (accounts_api.MAX_PLAN_BYTES + 1)])
        many = self.retained_plan(3, ['statement'] * (accounts_api.MAX_PLAN_STATEMENTS + 1))
        for plan in (huge, many):
            with mock.patch.object(accounts_api.copy, 'deepcopy') as copier:
                with self.assertRaises(ValueError):
                    accounts_api._remember(plan, 'owner', self.workspace)
                self.assertEqual(copier.call_count, 0)
            self.assertEqual(list(self.workspace._account_plans), [first['change_set_id']])

    def test_aggregate_memory_evicts_oldest_and_retry_cannot_exceed_budget(self):
        with mock.patch.object(accounts_api.time, 'monotonic', return_value=1):
            accounts_api._remember(self.retained_plan(1), 'owner', self.workspace)
        with mock.patch.object(accounts_api.time, 'monotonic', return_value=2):
            accounts_api._remember(self.retained_plan(2), 'owner', self.workspace)
        plans = self.workspace._account_plans
        budget = accounts_api._registry_size(plans)
        with mock.patch.object(accounts_api, 'MAX_TOTAL_PLAN_BYTES', budget), \
                mock.patch.object(accounts_api.time, 'monotonic', return_value=3):
            entry = accounts_api._recall('%032x' % 1, 'owner', self.workspace, consume=True)
            accounts_api._remember(self.retained_plan(3), 'owner', self.workspace)
            accounts_api._retry('%032x' % 1, entry, self.workspace)
            self.assertEqual(set(plans), {'%032x' % 2, '%032x' % 3})
            accounts_api._remember(self.retained_plan(4), 'owner', self.workspace)
            self.assertEqual(set(plans), {'%032x' % 3, '%032x' % 4})
            self.assertLessEqual(accounts_api._registry_size(plans), budget)
        with mock.patch.object(accounts_api.time, 'monotonic', return_value=2000):
            accounts_api._remember(self.retained_plan(5), 'owner', self.workspace)
        self.assertEqual(list(plans), ['%032x' % 5])

    def test_rejected_retry_does_not_leave_grown_dictionary_storage(self):
        with mock.patch.object(accounts_api.time, 'monotonic', return_value=1):
            for number in range(6):
                accounts_api._remember(self.retained_plan(number), 'owner', self.workspace)
            entry = accounts_api._recall('%032x' % 5, 'owner', self.workspace, consume=True)
            plans = self.workspace._account_plans
            accounts_api._compact_plans(plans)
            budget = accounts_api._registry_size(plans)
            before = set(plans)
            with mock.patch.object(accounts_api, 'MAX_TOTAL_PLAN_BYTES', budget):
                accounts_api._retry('%032x' % 5, entry, self.workspace)
            self.assertEqual(set(plans), before)
            self.assertLessEqual(accounts_api._registry_size(plans), budget)

    def test_rebuilt_plan_cannot_replace_reviewed_actor_server_or_version(self):
        for drift in ('actor', 'server', 'version'):
            with self.subTest(drift=drift):
                self.conn = FakeConn(name='t_api')
                plan = self.call('/api/accounts/plan', connection=CONNECTION, operation='lock', user='t_api')
                new_actor = actor(account='other-admin@localhost') if drift == 'actor' else \
                    actor(variables={'server_id': '99', 'hostname': 'other-server'}) if drift == 'server' else actor()
                if drift == 'version':
                    self.conn.version = '8.4.12'
                with mock.patch.object(p, 'read_privileges', return_value=new_actor):
                    result = self.call('/api/accounts/apply', connection=CONNECTION, plan_id=plan['plan_id'],
                                       expect=plan['expect'], confirmed=True)
                self.assertEqual(result['status'], 'stale')
                self.assertEqual(self.conn.stdin, [])
                self.assertTrue(result['audit_ids'])

    def test_rebuilt_plan_cannot_replace_reviewed_role_dependency(self):
        self.conn = FakeConn(name='t_api')
        plan = self.call('/api/accounts/plan', connection=CONNECTION, operation='roles', user='t_api',
                         mode='grant', roles=[{'name': 'r', 'host': '%'}])
        self.conn.role_grants['`r`@`%`'] = ['GRANT SELECT ON *.* TO `r`@`%`']
        result = self.call('/api/accounts/apply', connection=CONNECTION, plan_id=plan['plan_id'],
                           expect=plan['expect'], confirmed=True)
        self.assertEqual(result['status'], 'stale')
        self.assertEqual(self.conn.stdin, [])
        self.assertTrue(result['audit_ids'])

    def test_errors_never_echo_secrets(self):
        self.conn.run = mock.Mock(side_effect=ConnectorError("ERROR 1045: denied near 'conn-secret' and 'acct-secret'"))
        with self.assertRaises(Exception) as cm:
            self.call('/api/accounts/plan', connection=CONNECTION, operation='create', user='t_api', host='%',
                      password='acct-secret')
        self.assertNotIn('conn-secret', str(cm.exception))
        self.assertNotIn('acct-secret', str(cm.exception))


if __name__ == '__main__':
    unittest.main()
