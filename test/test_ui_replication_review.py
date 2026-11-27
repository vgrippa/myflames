"""Post-confirmation replication reviews keep unsent refusals in Audit.

Server reads are synthetic. Policy, review validation and AuditTrail storage
remain real, so a refused review cannot silently become a passive status read.
"""
import copy
import json
import types
from unittest import mock

import test_replication as fixtures
from myflames import replication as rp, ui
from myflames.connector import ConnectorError


class TestReplicationReviewAudit(fixtures.ReplicationCase):
    def setUp(self):
        self.reset_workspace()

    def reset_workspace(self):
        self.fresh_store()
        self.workspace = types.SimpleNamespace(store=self.store)
        self.connection = {'host': 'db', 'port': 3306, 'user': 'operator',
                           'password': 'review-password-not-real'}

    def reviewed_payload(self, loop):
        report = fixtures.fake_report(1062)
        plan = rp.plan_skip(report, '')
        report['channels'][0]['skip_plan'] = copy.deepcopy(plan)
        body = {'connection': dict(self.connection), 'channel': '',
                'statements': list(plan['statements'])}
        if loop:
            body.update(expect={'engine': report['engine'],
                                'server_identity': plan['identity']['server_identity']},
                        first_failure=plan['identity'], codes=[1062], max_skips=2,
                        max_minutes=10, poll_s=2, receiver_guard='off')
        else:
            body['expect'] = plan['identity']
        path = '/api/live/replication/' + ('skip-loop' if loop else 'skip') + '/review'
        return report, body, path

    def assert_refused_audit(self, body, loop, text=None):
        records = self.records()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual((record['operation'], record['status'], record['outcome']),
                         (rp.OPERATION_LOOP if loop else rp.OPERATION_SKIP, 'refused', 'unsent'))
        self.assertEqual(record['surface'], 'ui')
        self.assertEqual(record['target']['channel'], body['channel'])
        self.assertEqual(record['target'],
                         ui._replication_review_details(body, loop=True) if loop else body['expect'])
        self.assertEqual(record['statements'], [rp.SKIP_TAG + sql for sql in body['statements']])
        self.assertIsNotNone(record['finished_at'])
        if text:
            self.assertIn(text, record['message'])
        self.assertNotIn(self.connection['password'], json.dumps(record))
        self.assertFalse(ui._review_tokens(self.workspace).entries)
        return record

    def test_changed_failure_after_confirmation_is_audited_for_both_reviews(self):
        for loop in (False, True):
            with self.subTest(loop=loop):
                self.reset_workspace()
                report, body, path = self.reviewed_payload(loop)
                changed = copy.deepcopy(report)
                changed['channels'][0]['sql']['error']['message'] = 'A different failure appeared.'
                conn = fixtures.TestReviewGateSurfaces.connection()
                with mock.patch('myflames.connector.MySQLConnection', return_value=conn), \
                        mock.patch.object(rp, 'read_status', return_value=changed):
                    with self.assertRaisesRegex(ValueError, 'changed since you confirmed'):
                        ui.api_response(path, body, self.workspace)
                conn.run_write.assert_not_called()
                self.assert_refused_audit(body, loop, 'changed since you confirmed')

    def test_read_only_refusal_is_audited_before_connector_construction(self):
        for loop in (False, True):
            with self.subTest(loop=loop):
                self.reset_workspace()
                _report, body, path = self.reviewed_payload(loop)
                profile = self.store.save('profile', dict(self.connection, name='replica',
                                                         type='development', read_only=True))
                body['connection']['profile_id'] = profile['id']
                with mock.patch('myflames.connector.MySQLConnection') as connect:
                    with self.assertRaisesRegex(ValueError, '[Rr]ead.only'):
                        ui.api_response(path, body, self.workspace)
                connect.assert_not_called()
                self.assert_refused_audit(body, loop)

    def test_policy_change_during_review_is_audited_without_issuing_a_token(self):
        for loop in (False, True):
            with self.subTest(loop=loop):
                self.reset_workspace()
                report, body, path = self.reviewed_payload(loop)
                profile = self.store.save('profile', dict(self.connection, name='replica', type='development'))
                body['connection']['profile_id'] = profile['id']
                conn = fixtures.TestReviewGateSurfaces.connection()

                def read(_conn):
                    self.store.save('profile', dict(profile, read_only=True), key=profile['id'])
                    return report

                with mock.patch('myflames.connector.MySQLConnection', return_value=conn), \
                        mock.patch.object(rp, 'read_status', side_effect=read):
                    with self.assertRaisesRegex(ValueError, 'write policy changed'):
                        ui.api_response(path, body, self.workspace)
                conn.run_write.assert_not_called()
                self.assert_refused_audit(body, loop, 'write policy changed')

    def test_receiver_guard_refusal_is_an_audited_loop_action(self):
        report, body, path = self.reviewed_payload(True)
        body['receiver_guard'] = 'on'
        conn = fixtures.TestReviewGateSurfaces.connection()
        with mock.patch('myflames.connector.MySQLConnection', return_value=conn), \
                mock.patch.object(rp, 'read_status', return_value=report), \
                mock.patch.object(rp, 'read_receiver_identity', side_effect=ValueError('Receiver counters unavailable.')):
            with self.assertRaisesRegex(ValueError, 'Receiver counters unavailable'):
                ui.api_response(path, body, self.workspace)
        conn.run_write.assert_not_called()
        self.assert_refused_audit(body, True, 'Receiver counters unavailable')

    def test_review_errors_redact_password_in_exception_and_refused_audit(self):
        for loop in (False, True):
            for error_type in (ValueError, ConnectorError):
                for phase in ('connect', 'status'):
                    with self.subTest(loop=loop, error=error_type.__name__, phase=phase):
                        self.reset_workspace()
                        report, body, path = self.reviewed_payload(loop)
                        error = error_type('Could not read reviewed failure: ' + self.connection['password'])
                        conn = fixtures.TestReviewGateSurfaces.connection()
                        connector = mock.patch('myflames.connector.MySQLConnection',
                                               side_effect=error if phase == 'connect' else None,
                                               return_value=conn)
                        with connector, mock.patch.object(rp, 'read_status',
                                                         side_effect=error if phase == 'status' else None,
                                                         return_value=report):
                            with self.assertRaises(error_type) as caught:
                                ui.api_response(path, body, self.workspace)
                        self.assertNotIn(self.connection['password'], str(caught.exception))
                        self.assertIn('[redacted]', str(caught.exception))
                        conn.run_write.assert_not_called()
                        self.assert_refused_audit(body, loop, '[redacted]')

    def test_saved_password_is_redacted_after_review_opens_the_saved_connection(self):
        for loop in (False, True):
            for phase in ('connect', 'status'):
                with self.subTest(loop=loop, phase=phase):
                    self.reset_workspace()
                    report, body, path = self.reviewed_payload(loop)
                    settings = {key: value for key, value in self.connection.items() if key != 'password'}
                    profile = ui.api_response('/api/profiles/save', {
                        'value': dict(settings, name='replica', type='development'),
                        'password': self.connection['password'], 'remember_password': True,
                    }, self.workspace)
                    body['connection'] = dict(settings, profile_id=profile['id'])
                    conn = fixtures.TestReviewGateSurfaces.connection()
                    error = ConnectorError('ERROR 2003: could not connect using ' + self.connection['password'])
                    with mock.patch('myflames.connector.MySQLConnection',
                                    side_effect=error if phase == 'connect' else None,
                                    return_value=conn) as connect, \
                            mock.patch.object(rp, 'read_status',
                                              side_effect=error if phase == 'status' else None,
                                              return_value=report):
                        with self.assertRaises(ConnectorError) as caught:
                            ui.api_response(path, body, self.workspace)
                    self.assertEqual(connect.call_args[1]['password'], self.connection['password'])
                    self.assertNotIn(self.connection['password'], str(caught.exception))
                    self.assertIn('[redacted]', str(caught.exception))
                    conn.run_write.assert_not_called()
                    self.assert_refused_audit(body, loop, '[redacted]')

    def test_successful_review_and_passive_status_reads_do_not_write_audit(self):
        for loop in (False, True):
            with self.subTest(loop=loop):
                self.reset_workspace()
                report, body, path = self.reviewed_payload(loop)
                conn = fixtures.TestReviewGateSurfaces.connection()
                with mock.patch('myflames.connector.MySQLConnection', return_value=conn), \
                        mock.patch.object(rp, 'read_status', side_effect=lambda *a, **kw: copy.deepcopy(report)):
                    review = ui.api_response(path, body, self.workspace)
                    self.assertIsInstance(review['review_token'], str)
                    ui.api_response('/api/live/replication', {'connection': self.connection}, self.workspace)
                self.assertEqual(self.records(), [])
                conn.run_write.assert_not_called()

        with mock.patch('myflames.connector.MySQLConnection', side_effect=ConnectorError('Status unavailable.')):
            with self.assertRaisesRegex(ConnectorError, 'Status unavailable'):
                ui.api_response('/api/live/replication', {'connection': self.connection}, self.workspace)
        self.assertEqual(self.records(), [])
