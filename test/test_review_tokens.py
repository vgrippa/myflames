"""One-shot review authority is separate from HTTP authentication and confirmation."""
import concurrent.futures
import copy
import os
import tempfile
import threading
import types
import unittest
from unittest import mock

from myflames import dbops, lifecycle as lc, locks, privileges, ui
from myflames.review_tokens import ReviewTokens
from myflames.workspace_store import WorkspaceStore
from test_locks import KillScenario, FakeFactory, bound_kill_expect
from test_lifecycle_cli import fake_plan, ReplayConnection
from test_replication import replay, status, sent_scripts


class TestReviewTokens(unittest.TestCase):
    def test_expiry_cap_binding_and_one_use(self):
        now = [0]
        registry = ReviewTokens(ttl=5, capacity=2, clock=lambda: now[0])
        details = {'expect': {'id': 5}, 'statements': ['KILL CONNECTION 5']}
        first = registry.issue('kill', 'owner', details)
        for action, owner, changed in [('restore', 'owner', details), ('kill', 'other', details),
                                        ('kill', 'owner', dict(details, statements=['KILL CONNECTION 6']))]:
            with self.assertRaises(ValueError):
                registry.check(first, action, owner, changed, consume=True)
        consumer = registry.consumer(first, 'kill', 'owner', details)
        details['expect']['id'] = 6
        consumer()  # Captured identity is immutable even if caller mutates it.
        with self.assertRaises(ValueError):
            consumer()
        second = registry.issue('x', 'owner', {})
        registry.issue('x', 'owner', {})
        registry.issue('x', 'owner', {})
        self.assertEqual(len(registry.entries), 2)
        with self.assertRaises(ValueError):
            registry.check(second, 'x', 'owner', {})
        last = registry.issue('x', 'owner', {})
        now[0] = 5
        with self.assertRaises(ValueError):
            registry.check(last, 'x', 'owner', {})
        self.assertEqual(len(registry.entries), 0)

    def test_only_one_racing_consumer_wins(self):
        registry = ReviewTokens()
        token = registry.issue('kill', 'owner', {'statements': ['KILL CONNECTION 1']})
        consumers = [registry.consumer(token, 'kill', 'owner', {'statements': ['KILL CONNECTION 1']}) for _ in range(8)]
        barrier = threading.Barrier(8)
        def use(consume):
            barrier.wait()
            try:
                consume()
                return True
            except ValueError:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(use, consumers)), 1)


class TestReviewedWrites(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = mock.patch.dict(os.environ, {'HOME': temp.name, 'USERPROFILE': temp.name,
                                          'MYFLAMES_SECRET_BACKEND': 'vault'})
        env.start()
        self.addCleanup(env.stop)
        self.workspace = types.SimpleNamespace(store=WorkspaceStore(), lifecycle=lc.LifecycleJobs())
        self.addCleanup(self.workspace.lifecycle.close)
        self.connection = {'host': '127.0.0.1', 'port': 3306, 'user': 'app'}

    def kill_payload(self, scenario):
        return {'connection': self.connection, 'connection_id': 41, 'mode': 'connection', 'confirmed': True,
                'expect': bound_kill_expect(scenario.conn, connection=self.connection)}

    def test_kill_missing_tampered_wrong_owner_and_replayed_review_send_nothing(self):
        scenario = KillScenario()
        payload = self.kill_payload(scenario)
        with mock.patch('myflames.connector.MySQLConnection') as connect:
            with self.assertRaisesRegex(ValueError, 'review'):
                ui.api_response('/api/live/kill', payload, self.workspace)
            connect.assert_not_called()
        review = ui.api_response('/api/live/kill/prepare', payload, self.workspace)
        self.assertEqual(review['statements'], ['KILL CONNECTION 41'])
        payload['review_token'] = review['review_token']
        for changed in (dict(payload, connection_id=42), dict(payload, mode='query'),
                        dict(payload, connection=dict(self.connection, host='other'))):
            with mock.patch('myflames.connector.MySQLConnection') as connect:
                try:
                    result = ui.api_response('/api/live/kill', changed, self.workspace)
                    self.assertEqual(result['status'], 'refused')
                except ValueError as exc:
                    self.assertIn('review', str(exc))
                connect.assert_not_called()
        with mock.patch('myflames.connector.MySQLConnection', FakeFactory(scenario.conn)), mock.patch.object(locks.time, 'sleep'):
            self.assertEqual(ui.api_response('/api/live/kill', payload, self.workspace)['status'], 'killed')
        with mock.patch('myflames.connector.MySQLConnection') as connect:
            with self.assertRaisesRegex(ValueError, 'already used'):
                ui.api_response('/api/live/kill', payload, self.workspace)
            connect.assert_not_called()
        self.assertEqual(len(scenario.kills()), 1)

    def test_kill_identity_refusal_does_not_consume_review(self):
        scenario = KillScenario()
        payload = self.kill_payload(scenario)
        payload['expect']['server_identity'] = {'server_uuid': 'stale'}
        review = ui.api_response('/api/live/kill/prepare', payload, self.workspace)
        payload['review_token'] = review['review_token']
        with mock.patch('myflames.connector.MySQLConnection', FakeFactory(scenario.conn)):
            self.assertEqual(ui.api_response('/api/live/kill', payload, self.workspace)['status'], 'refused')
        ui._review_tokens(self.workspace).check(review['review_token'], 'kill',
            ui._review_identity(self.connection, self.workspace), ui._kill_review_details(payload))
        self.assertEqual(scenario.kills(), [])

    def test_replication_review_from_read_is_consumed_before_send(self):
        conn, _ = replay('replication', 'mysql-8.4-gtid-error')
        with mock.patch('myflames.connector.MySQLConnection', FakeFactory(conn)), \
                mock.patch('myflames.replication.receiver_guard_plan', return_value={}):
            report = ui.api_response('/api/live/replication', {'connection': self.connection}, self.workspace)
        plan = report['channels'][0]['skip_plan']
        payload = {'connection': self.connection, 'channel': plan['channel'], 'expect': plan['identity'],
                   'statements': plan['statements'], 'confirmed': True, 'review_token': plan['review_token']}
        conn, _ = replay('replication', 'mysql-8.4-gtid-skip')
        with mock.patch('myflames.connector.MySQLConnection', FakeFactory(conn)), mock.patch('myflames.replication.time.sleep'), \
                mock.patch.object(privileges, 'read_privileges', return_value=privileges.parse_grants([], checked=False)):
            self.assertEqual(ui.api_response('/api/live/replication/skip', payload, self.workspace)['status'], 'skipped')
        self.assertEqual(len(sent_scripts(conn)), 1)
        with mock.patch('myflames.connector.MySQLConnection') as connect:
            with self.assertRaisesRegex(ValueError, 'already used'):
                ui.api_response('/api/live/replication/skip', payload, self.workspace)
            connect.assert_not_called()

    def test_lifecycle_prepare_binds_exact_plan_and_send_boundary_is_one_shot(self):
        plan = fake_plan()
        with mock.patch.object(ui, '_lifecycle_prepare', return_value=copy.deepcopy(plan)):
            prepared = ui.api_response('/api/lifecycle/prepare', {'operation': 'restore', 'connection': self.connection}, self.workspace)
        token = prepared['review_token']
        payload = {'operation': 'restore', 'connection': self.connection, 'plan': prepared,
                   'typed_target': prepared['target'], 'confirmed': True}
        for changed in (dict(prepared, commands=['changed']), dict(prepared, target='other')):
            with mock.patch.object(ui, '_with_saved_password') as password:
                with self.assertRaisesRegex(ValueError, 'review'):
                    ui.api_response('/api/lifecycle/start', dict(payload, plan=changed), self.workspace)
                password.assert_not_called()
        effects = []
        def action(job, conn, fresh):
            effects.append(fresh['commands'])
            return {'status': 'completed', 'verification': []}
        def make_job(options, incoming, **kwargs):
            return lc._write_job('restore', options, incoming, lambda conn, job: copy.deepcopy(plan), action,
                kwargs['confirmed'], kwargs['typed_target'], kwargs['audit'], safety=kwargs['safety'], start=False)
        with mock.patch.object(lc, 'restore_job', side_effect=make_job), \
                mock.patch.object(lc, '_connect', return_value=ReplayConnection()), \
                mock.patch.object(lc, 'read_server', return_value=plan['server']):
            started = ui.api_response('/api/lifecycle/start', payload, self.workspace)
            job = self.workspace.lifecycle.get(started['id'])
            job.wait()
            self.assertEqual(job.snapshot()['state'], 'done', job.snapshot())
        self.assertEqual(len(effects), 1)
        with mock.patch.object(lc, 'restore_job') as start:
            with self.assertRaisesRegex(ValueError, 'already used'):
                ui.api_response('/api/lifecycle/start', payload, self.workspace)
            start.assert_not_called()
