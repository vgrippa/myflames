"""Abuse cases for confirmation-bound replication loops.

The server reads below are controlled fixtures. Classification, write policy,
review-token validation, loop registration and the skip guards remain real.
No database, worker service or client subprocess is started.
"""
import copy
import threading
import types
from unittest import mock

import test_replication as fixtures
from myflames import privileges, replication as rp, ui


REVIEW = '/api/live/replication/skip-loop/review'
START = '/api/live/replication/skip-loop/start'
REAL_THREAD = threading.Thread


class TestLoopReviewAbuse(fixtures.ReplicationCase):
    def setUp(self):
        self.reset_workspace()

    def reset_workspace(self):
        self.fresh_store()
        self.registry = rp.SkipLoopRegistry()
        self.workspace = types.SimpleNamespace(store=self.store, replication_loops=self.registry)
        self.connection = {'host': 'db', 'port': 3306, 'user': 'u'}
        self.addCleanup(self.registry.stop_all, wait=True)

    def reviewed(self, report=None, **options):
        report = copy.deepcopy(report or fixtures.fake_report(1062))
        name = report['channels'][0]['name']
        plan = rp.plan_skip(report, name)
        self.assertTrue(plan['available'], plan['message'])
        body = {'connection': dict(self.connection), 'channel': name,
                'expect': {'engine': report['engine'], 'server_identity': plan['identity']['server_identity']},
                'first_failure': plan['identity'], 'statements': plan['statements'],
                'codes': [1062], 'max_skips': 2, 'max_minutes': 10, 'poll_s': 2,
                'receiver_guard': 'off', 'confirmed': True}
        body.update(options)
        conn = fixtures.TestReviewGateSurfaces.connection()
        with mock.patch('myflames.connector.MySQLConnection', return_value=conn), \
                mock.patch.object(rp, 'read_status', return_value=report):
            review = ui.api_response(REVIEW, body, self.workspace)
        conn.run_write.assert_not_called()
        conn.run.assert_not_called()
        body['review_token'] = review['review_token']
        return report, body

    def check_token(self, body, identity=None):
        ui._review_tokens(self.workspace).check(body['review_token'], 'replication_skip_loop',
            identity or ui._review_identity(body['connection'], self.workspace),
            ui._replication_review_details(body, loop=True))

    def assert_spent(self, body):
        with self.assertRaisesRegex(ValueError, 'expired, changed or was already used'):
            self.check_token(body)

    @staticmethod
    def synchronous_worker(thread):
        # Execute deterministically without leaving a background worker behind.
        thread.return_value.start.side_effect = lambda: thread.call_args[1]['target']()

    def test_changed_options_or_displayed_failure_refuse_before_any_login(self):
        _report, body = self.reviewed()
        changes = (
            ('codes', [1062, 1032]), ('max_skips', 1000), ('max_minutes', None),
            ('poll_s', 60), ('receiver_guard', 'on'),
            ('first_failure', dict(body['first_failure'], gtid=fixtures.U + ':2')),
            ('statements', body['statements'][:-1] + ["START REPLICA SQL_THREAD FOR CHANNEL 'other'"]),
        )
        for field, value in changes:
            with self.subTest(field=field):
                changed = copy.deepcopy(body)
                changed[field] = value
                with mock.patch('myflames.connector.MySQLConnection') as connect, \
                        mock.patch.object(ui, '_with_saved_password') as password, \
                        mock.patch.object(rp, 'read_status') as read:
                    with self.assertRaisesRegex(ValueError, 'expired, changed or was already used'):
                        ui.api_response(START, changed, self.workspace)
                connect.assert_not_called()
                password.assert_not_called()
                read.assert_not_called()
                self.check_token(body)
        self.assertEqual(self.registry.status(ui._review_identity(self.connection, self.workspace))['loops'], [])
        self.assertTrue(all(r['status'] == 'refused' and r['outcome'] == 'unsent' for r in self.records()))

    def test_equivalent_options_use_the_same_canonical_review(self):
        report, body = self.reviewed(codes=[1062, 1032, 1062], max_minutes=None)
        body['codes'] = [1032, 1062]
        body['poll_s'] = 2.0
        body.pop('max_minutes')
        conn = fixtures.TestReviewGateSurfaces.connection()
        with mock.patch('myflames.connector.MySQLConnection', return_value=conn), \
                mock.patch.object(rp, 'read_status', return_value=report), \
                mock.patch.object(rp.threading, 'Thread') as thread:
            answer = ui.api_response(START, body, self.workspace)
        self.assertEqual(answer['codes'], [1032, 1062])
        self.assertEqual(answer['poll_s'], 2.0)
        self.assertIsNone(answer['max_duration_s'])
        thread.return_value.start.assert_called_once_with()
        conn.run_write.assert_not_called()
        self.assert_spent(body)

    def test_passive_multichannel_status_reads_never_issue_or_evict_reviews(self):
        report, body = self.reviewed()
        report['channels'] = [dict(copy.deepcopy(report['channels'][0]), name='channel-%d' % n)
                              for n in range(130)]
        for row in report['channels']:
            row['skip_plan'] = rp.plan_skip(report, row['name'])
        tokens = ui._review_tokens(self.workspace)
        existing = dict(tokens.entries)
        conn = fixtures.TestReviewGateSurfaces.connection()
        with mock.patch('myflames.connector.MySQLConnection', return_value=conn), \
                mock.patch.object(rp, 'read_status', side_effect=lambda *a, **kw: copy.deepcopy(report)):
            for _ in range(2):
                answer = ui.api_response('/api/live/replication', {'connection': self.connection}, self.workspace)
                for row in answer['channels']:
                    self.assertNotIn('review_token', row['skip_plan'])
                    self.assertNotIn('loop_review_token', row['skip_plan'])
        self.assertEqual(dict(tokens.entries), existing)
        self.check_token(body)
        conn.run_write.assert_not_called()

    def test_changed_first_failure_refuses_at_every_read_before_the_first_skip(self):
        phases = ('preflight', 'prepare', 'first_iteration', 'skip_recheck', 'final_recheck')
        grants = privileges.parse_grants([
            'GRANT SESSION_VARIABLES_ADMIN, REPLICATION_SLAVE_ADMIN ON *.* TO `operator`@`%`'],
            engine='mysql', version='8.4.11')
        for phase, before in zip(phases, range(len(phases))):
            with self.subTest(phase=phase):
                self.reset_workspace()
                report, body = self.reviewed()
                changed = fixtures.fake_report(1062, gtid=fixtures.U + ':2', pos=200)
                conn = fixtures.TestReviewGateSurfaces.connection()
                with mock.patch('myflames.connector.MySQLConnection', return_value=conn), \
                        mock.patch.object(rp, 'read_status', side_effect=[report] * before + [changed]), \
                        mock.patch.object(privileges, 'read_privileges', return_value=grants), \
                        mock.patch.object(rp.threading, 'Thread') as thread:
                    self.synchronous_worker(thread)
                    if phase == 'preflight':
                        with self.assertRaisesRegex(ValueError, 'changed since you confirmed'):
                            ui.api_response(START, body, self.workspace)
                    else:
                        answer = ui.api_response(START, body, self.workspace)
                        self.assertEqual(answer['skips_done'], 0)
                        self.assertIn('changed since you confirmed', answer['message'])
                        if phase == 'prepare':
                            self.assertEqual((answer['state'], answer['job_id']), ('refused', None))
                        else:
                            self.assertEqual(answer['state'], 'stopped')
                    if before < 2:
                        thread.assert_not_called()
                conn.run_write.assert_not_called()
                conn.run.assert_not_called()
                conn.close.assert_called_once_with()
                if before < 2:
                    self.check_token(body)
                    self.assertEqual(self.registry.status(ui._review_identity(self.connection, self.workspace))['loops'], [])
                else:
                    self.assert_spent(body)

    def test_receiver_preflight_refusal_keeps_only_the_original_review_usable(self):
        report, body = self.reviewed(receiver_guard='on')
        conn = fixtures.TestReviewGateSurfaces.connection()
        with mock.patch('myflames.connector.MySQLConnection', return_value=conn), \
                mock.patch.object(rp, 'read_status', return_value=report), \
                mock.patch.object(rp, 'read_receiver_identity', return_value=None), \
                mock.patch.object(rp.threading, 'Thread') as thread:
            answer = ui.api_response(START, body, self.workspace)
        self.assertEqual((answer['state'], answer['job_id'], answer['stop_reason']),
                         ('refused', None, 'receiver_unverified'))
        thread.assert_not_called()
        conn.run_write.assert_not_called()
        conn.close.assert_called_once_with()
        self.check_token(body)
        changed = dict(body, receiver_guard='off')
        with mock.patch('myflames.connector.MySQLConnection') as connect:
            with self.assertRaisesRegex(ValueError, 'expired, changed or was already used'):
                ui.api_response(START, changed, self.workspace)
        connect.assert_not_called()
        self.check_token(body)
        retry = fixtures.TestReviewGateSurfaces.connection()
        with mock.patch('myflames.connector.MySQLConnection', return_value=retry), \
                mock.patch.object(rp, 'read_status', return_value=report), \
                mock.patch.object(rp.threading, 'Thread') as thread:
            accepted = ui.api_response(START, body, self.workspace)
        self.assertEqual(accepted['state'], 'starting')
        thread.return_value.start.assert_called_once_with()
        retry.run_write.assert_not_called()
        self.assert_spent(body)

    def test_resolved_or_missing_first_failure_is_a_controlled_unsent_refusal(self):
        grants = privileges.parse_grants([
            'GRANT SESSION_VARIABLES_ADMIN, REPLICATION_SLAVE_ADMIN ON *.* TO `operator`@`%`'],
            engine='mysql', version='8.4.11')
        phases = ('preflight', 'prepare', 'first_iteration', 'skip_recheck', 'final_recheck')
        for condition in ('healthy', 'channel_gone'):
            for phase, before in zip(phases, range(len(phases))):
                with self.subTest(condition=condition, phase=phase):
                    self.reset_workspace()
                    report, body = self.reviewed()
                    current = fixtures.fake_report(0, running='Yes')
                    if condition == 'channel_gone':
                        current['channels'] = []
                    conn = fixtures.TestReviewGateSurfaces.connection()
                    with mock.patch('myflames.connector.MySQLConnection', return_value=conn), \
                            mock.patch.object(rp, 'read_status', side_effect=[report] * before + [current]), \
                            mock.patch.object(privileges, 'read_privileges', return_value=grants), \
                            mock.patch.object(rp.threading, 'Thread') as thread:
                        self.synchronous_worker(thread)
                        if phase == 'preflight':
                            with self.assertRaises(ValueError):
                                ui.api_response(START, body, self.workspace)
                        else:
                            answer = ui.api_response(START, body, self.workspace)
                            self.assertEqual(answer['skips_done'], 0)
                            self.assertEqual(answer['stop_reason'], 'refused')
                            self.assertEqual(answer['state'], 'refused' if phase == 'prepare' else 'stopped')
                        if before < 2:
                            thread.assert_not_called()
                    conn.run_write.assert_not_called()
                    conn.run.assert_not_called()
                    conn.close.assert_called_once_with()
                    records = self.records()
                    self.assertEqual(len(records), 1)
                    self.assertEqual(records[0]['status'], 'refused')
                    if before < 2:
                        self.check_token(body)
                    else:
                        self.assert_spent(body)

    def test_successful_first_skip_allows_a_later_selected_failure_with_one_review(self):
        first, body = self.reviewed(max_skips=2)
        second = fixtures.fake_report(1062, gtid=fixtures.U + ':2', pos=200)
        second['channels'][0]['position']['executed_gtid_set'] = fixtures.U + ':1'
        healthy = fixtures.fake_report(0, running='Yes', pos=300)
        healthy['channels'][0]['position']['executed_gtid_set'] = fixtures.U + ':1-2'
        conn = fixtures.TestReviewGateSurfaces.connection()
        grants = privileges.parse_grants([
            'GRANT SESSION_VARIABLES_ADMIN, REPLICATION_SLAVE_ADMIN ON *.* TO `operator`@`%`'],
            engine='mysql', version='8.4.11')
        # Startup and all three first-skip reads see the reviewed failure.
        # The first readback sees a new selected failure; the next iteration
        # plans it independently, then verifies that it too was passed.
        reports = [first] * 5 + [second] * 4 + [healthy]
        with mock.patch('myflames.connector.MySQLConnection', return_value=conn), \
                mock.patch.object(rp, 'read_status', side_effect=reports) as read, \
                mock.patch.object(privileges, 'read_privileges', return_value=grants), \
                mock.patch.object(rp.SkipLoop, '_wait'), mock.patch.object(rp.time, 'sleep'), \
                mock.patch.object(rp.threading, 'Thread') as thread:
            self.synchronous_worker(thread)
            answer = ui.api_response(START, body, self.workspace)
        self.assertEqual((answer['state'], answer['stop_reason'], answer['skips_done']),
                         ('stopped', 'max_skips', 2))
        self.assertEqual([skip['gtid'] for skip in answer['skips']], [fixtures.U + ':1', fixtures.U + ':2'])
        self.assertTrue(all(skip['passed'] for skip in answer['skips']))
        self.assertEqual(read.call_count, 10)
        self.assertEqual(conn.run_write.call_count, 2)
        scripts = [call[0][0] for call in conn.run_write.call_args_list]
        for statement in body['statements']:
            self.assertIn(rp.SKIP_TAG + statement, scripts[0])
        for statement in rp.plan_skip(second, body['channel'])['statements']:
            self.assertIn(rp.SKIP_TAG + statement, scripts[1])
        self.assertNotIn("SET GTID_NEXT = '%s:2'" % fixtures.U, scripts[0])
        self.assertNotIn("SET GTID_NEXT = '%s:1'" % fixtures.U, scripts[1])
        self.assertEqual([(r['operation'], r['status']) for r in self.records()],
                         [(rp.OPERATION_LOOP, 'ok'), (rp.OPERATION_LOOP, 'ok')])
        self.assert_spent(body)
        self.assertEqual(len(ui._review_tokens(self.workspace).entries), 0)
        conn.close.assert_called_once_with()

    def test_concurrent_starts_register_one_loop_and_preserve_a_losing_distinct_token(self):
        for same_token in (True, False):
            with self.subTest(same_token=same_token):
                self.reset_workspace()
                report, first = self.reviewed()
                second = copy.deepcopy(first) if same_token else self.reviewed()[1]
                requests = (first, second)
                barrier = threading.Barrier(2)
                local = threading.local()
                clients, answers, errors = {}, {}, {}
                lock = threading.Lock()

                def connection_factory(**kwargs):
                    client = fixtures.TestReviewGateSurfaces.connection()
                    with lock:
                        clients[threading.current_thread().name] = client
                    return client

                def read(conn):
                    local.reads = getattr(local, 'reads', 0) + 1
                    if local.reads == 2:
                        # Both callers passed the nonconsuming token check and
                        # duplicate check before either may finish preflight.
                        barrier.wait(timeout=5)
                    return copy.deepcopy(report)

                def start(index):
                    try:
                        answers[index] = ui.api_response(START, requests[index], self.workspace)
                    except BaseException as exc:
                        errors[index] = exc

                with mock.patch('myflames.connector.MySQLConnection', side_effect=connection_factory), \
                        mock.patch.object(rp, 'read_status', side_effect=read), \
                        mock.patch.object(rp.threading, 'Thread') as worker:
                    callers = [REAL_THREAD(target=start, args=(n,), name='review-start-%d' % n) for n in range(2)]
                    for caller in callers:
                        caller.start()
                    for caller in callers:
                        caller.join(10)
                    self.assertTrue(all(not caller.is_alive() for caller in callers), 'all callers finished')
                    self.assertEqual(len(answers), 1, repr(errors))
                    self.assertEqual(len(errors), 1)
                    loser = next(iter(errors))
                    self.assertIsInstance(errors[loser], ValueError)
                    self.assertIn('already running', str(errors[loser]))
                    worker.return_value.start.assert_called_once_with()
                identity = ui._review_identity(self.connection, self.workspace)
                self.assertEqual(len(self.registry.status(identity)['loops']), 1)
                for client in clients.values():
                    client.run_write.assert_not_called()
                    client.run.assert_not_called()
                winner = next(iter(answers))
                self.assert_spent(requests[winner])
                if not same_token:
                    self.check_token(requests[loser])
                    with self.assertRaises(ValueError):
                        self.check_token(dict(requests[loser], max_skips=1000))

    def test_saved_read_only_flip_during_preflight_cannot_consume_or_launch(self):
        for flip_read in (1, 2):
            with self.subTest(flip_read=flip_read):
                self.reset_workspace()
                profile = self.store.save('profile', dict(self.connection, name='replica', type='development'))
                self.connection['profile_id'] = profile['id']
                report, body = self.reviewed()
                review_identity = ui._review_identity(self.connection, self.workspace)
                conn = fixtures.TestReviewGateSurfaces.connection()
                reads = [0]

                def read(connection):
                    reads[0] += 1
                    if reads[0] == flip_read:
                        self.store.save('profile', dict(profile, read_only=True), key=profile['id'])
                    return report

                with mock.patch('myflames.connector.MySQLConnection', return_value=conn), \
                        mock.patch.object(rp, 'read_status', side_effect=read), \
                        mock.patch.object(rp.threading, 'Thread') as thread:
                    with self.assertRaisesRegex(ValueError, 'write policy changed'):
                        ui.api_response(START, body, self.workspace)
                thread.assert_not_called()
                conn.run_write.assert_not_called()
                conn.run.assert_not_called()
                conn.close.assert_called_once_with()
                self.assertEqual(self.registry.status(review_identity)['loops'], [])
                self.check_token(body, identity=review_identity)
                self.store.save('profile', dict(profile, read_only=False), key=profile['id'])
                self.check_token(body)

    def test_shutdown_during_preflight_prevents_late_registration_and_consumption(self):
        report, body = self.reviewed()
        conn = fixtures.TestReviewGateSurfaces.connection()
        reads = [0]

        def read(connection):
            reads[0] += 1
            if reads[0] == 2:
                self.registry.stop_all()
            return report

        with mock.patch('myflames.connector.MySQLConnection', return_value=conn), \
                mock.patch.object(rp, 'read_status', side_effect=read), \
                mock.patch.object(rp.threading, 'Thread') as thread:
            with self.assertRaisesRegex(ValueError, 'clos|shut|stop'):
                ui.api_response(START, body, self.workspace)
        thread.assert_not_called()
        conn.run_write.assert_not_called()
        conn.close.assert_called_once_with()
        self.check_token(body)
        self.assertEqual(self.registry.status(ui._review_identity(self.connection, self.workspace))['loops'], [])

    def test_denied_status_reports_the_denial_even_with_incomplete_wsrep_identity(self):
        for engine, fixture_name in (('mysql', 'mysql-8.4-gtid-error'), ('mariadb', 'mariadb-11.4-error')):
            for phase in ('preflight', 'prepare', 'first_iteration', 'final_recheck'):
                with self.subTest(engine=engine, phase=phase):
                    self.reset_workspace()
                    original, _ = fixtures.status(fixture_name)
                    original['settings']['server_identity']['wsrep_gcomm_uuid'] = fixtures.U
                    if engine == 'mariadb':
                        original['settings']['server_identity']['wsrep_node_name'] = 'reviewed-node'
                    original.pop('server_identity', None)
                    report, body = self.reviewed(original)
                    denied, _ = fixtures.status(engine + ('-8.4-denied' if engine == 'mysql' else '-11.4-denied'))
                    self.assertFalse(denied['available'])
                    self.assertNotEqual(denied['settings']['server_identity'], body['expect']['server_identity'])
                    before = {'preflight': 0, 'prepare': 1, 'first_iteration': 2, 'final_recheck': 4}[phase]
                    conn = fixtures.TestReviewGateSurfaces.connection()
                    grants = privileges.parse_grants(['GRANT ALL PRIVILEGES ON *.* TO `operator`@`%`'],
                                                     engine=engine, version=report['server_version'])
                    with mock.patch('myflames.connector.MySQLConnection', return_value=conn), \
                            mock.patch.object(rp, 'read_status', side_effect=[report] * before + [denied]), \
                            mock.patch.object(privileges, 'read_privileges', return_value=grants), \
                            mock.patch.object(rp.threading, 'Thread') as thread:
                        self.synchronous_worker(thread)
                        if phase == 'preflight':
                            with self.assertRaises(ValueError) as error:
                                ui.api_response(START, body, self.workspace)
                            message = str(error.exception)
                        else:
                            answer = ui.api_response(START, body, self.workspace)
                            message = answer['message']
                            self.assertEqual(answer['skips_done'], 0)
                    self.assertIn(denied['message'], message)
                    self.assertNotIn('identity changed', message)
                    conn.run_write.assert_not_called()
                    conn.run.assert_not_called()

