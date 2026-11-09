"""Regressions for audited bulk KILL and protected Group Replication streams."""
import json
import os
import re
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from myflames import dbops, locks, sessions, ui
from myflames.connector import ConnectorError
from myflames.review_tokens import ReviewTokens
from myflames.workspace_store import WorkspaceStore
from test_locks import batch, KillScenario, bound_kill_expect, K_TARGET

GR_COMMAND = 'Group Replicatio'  # COMMAND is VARCHAR(16) in MySQL's process-list tables.
GR_FULL_COMMAND = 'Group Replication Data Stream subscription'
GR_REASON = ('Group Replication uses this connection to talk to the other members; ending it can '
             'remove a member from the group. Use Cluster to stop Group Replication instead.')


class BatchConnection(object):
    """Real kill_session logic, with counted client calls and per-target changes."""
    def __init__(self, changed=None, protected=None, remaining=False, after_error=False, server_changed=None):
        self.calls, self.killed = [], set()
        self.changed, self.protected = changed, protected
        self.remaining, self.after_error = remaining, after_error
        self.server_changed = server_changed

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def run(self, sql, extra_flags=None):
        self.calls.append(sql)
        if 'SELECT @@version, @@performance_schema' in sql:
            return '8.4.6\t1\n'
        if 'SELECT @@server_uuid' in sql:
            return 'server-1\n'
        if sql.startswith(locks.KILL_TAG):
            self.killed.add(int(sql.rsplit(' ', 1)[1]))
            return ''
        if 'information_schema.INNODB_TRX' in sql:
            return ''
        if 'FROM performance_schema.threads' in sql:
            if ' IN (' in sql:
                if self.after_error:
                    raise ConnectorError('ERROR 1142: command denied')
                return batch([[101, 1, 'app', 'localhost', None, 'Query', 3, None, 'SELECT 1']]) if self.remaining else ''
            cid = int(re.search(r'PROCESSLIST_ID = (\d+)', sql).group(1))
            if cid in self.killed:
                return ''
            return batch([[cid + 100, cid, 'other' if cid == self.changed else 'app', 'localhost',
                           None, GR_COMMAND if cid == self.protected else 'Query', 3, None, 'SELECT 1', 999] +
                          (['server-2' if cid == self.server_changed else 'server-1'] if '@@server_uuid' in sql else [])])
        raise AssertionError(sql)


class TestBulkKillReview(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = mock.patch.dict(os.environ, {'HOME': self.temp.name, 'USERPROFILE': self.temp.name,
                                           'MYFLAMES_SECRET_BACKEND': 'vault'})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        self.store = WorkspaceStore(os.path.join(self.temp.name, 'workspace.db'))
        self.workspace = SimpleNamespace(store=self.store, review_tokens=ReviewTokens())
        self.connection = {'host': 'localhost', 'port': 3306, 'user': 'app', 'password': 'test-secret'}

    def payload(self, count=2, **changes):
        identity = ui._review_identity(self.connection, self.workspace)
        body = {'connection': self.connection, 'confirmed': True, 'targets': [
            {'connection_id': cid, 'expect': {'user': 'app', 'host': 'localhost', 'ps_thread_id': cid + 100,
             'connection_identity': identity, 'server_identity': {'server_uuid': 'server-1'}}}
            for cid in range(1, count + 1)]}
        body.update(changes)
        body['review_token'] = ui._bulk_kill_prepare(body, self.workspace)['review_token']
        return body

    def records(self):
        return self.store.audit_records()

    def test_policy_refusals_are_audited_without_connecting(self):
        for readonly in (False, True):
            with self.subTest(readonly=readonly):
                self.connection['read_only'] = readonly
                body = self.payload(confirmed=False)
                with mock.patch('myflames.connector.MySQLConnection') as factory:
                    result = ui._bulk_kill_response(body, self.workspace)
                factory.assert_not_called()
                self.assertEqual(result['refused'], 2)
                records = self.records()[:2]
                self.assertEqual(len(records), 2)
                self.assertTrue(all(r['status'] == 'refused' for r in records))
                self.assertTrue(all(r['outcome'] == ('read_only' if readonly else 'needs_confirmation') for r in records))
                self.assertTrue(all(len(r['statements']) == 1 for r in records))

    def test_malformed_targets_are_rejected_before_policy_gate(self):
        for target in (None, 7, {}, {'connection_id': '1'}, {'connection_id': 1, 'expect': {'user': 'app'}}):
            with self.subTest(target=target), mock.patch('myflames.connector.MySQLConnection') as factory:
                with self.assertRaises(ValueError):
                    ui._bulk_kill_response({'connection': self.connection, 'targets': [target]}, self.workspace)
                factory.assert_not_called()
        self.assertEqual(self.records(), [])

    def test_expired_review_and_connection_errors_are_audited(self):
        body = self.payload()
        body['review_token'] = 'expired'
        with self.assertRaisesRegex(ValueError, 'review expired'):
            ui._bulk_kill_response(body, self.workspace)
        self.assertEqual(len(self.records()), 2)
        body = self.payload()
        with mock.patch('myflames.connector.MySQLConnection', side_effect=ConnectorError('login test-secret failed')):
            with self.assertRaises(ConnectorError) as error:
                ui._bulk_kill_response(body, self.workspace)
        self.assertNotIn('test-secret', str(error.exception))
        self.assertEqual(len(self.records()), 4)
        self.assertTrue(all(r['status'] == 'error' for r in self.records()))
        self.assertNotIn('test-secret', json.dumps(self.records()))

    def test_saved_password_and_target_read_failures_are_audited(self):
        body = self.payload(1)
        with mock.patch('myflames.ui._with_saved_password', side_effect=ValueError('lookup test-secret failed')), \
                mock.patch('myflames.connector.MySQLConnection') as factory:
            with self.assertRaises(ValueError) as error:
                ui._bulk_kill_response(body, self.workspace)
        factory.assert_not_called()
        self.assertNotIn('test-secret', str(error.exception))
        self.assertEqual(self.records()[0]['status'], 'error')
        conn = BatchConnection()
        with mock.patch('myflames.connector.MySQLConnection', return_value=conn), \
                mock.patch('myflames.locks.read_target', side_effect=ConnectorError('read test-secret failed')):
            result = ui._bulk_kill_response(self.payload(1), self.workspace)
        self.assertEqual(result['errors'], 1)
        self.assertEqual(conn.killed, set())
        self.assertEqual(len(self.records()), 2)
        self.assertNotIn('test-secret', json.dumps(result))
        self.assertNotIn('test-secret', json.dumps(self.records()))

    def test_bulk_shares_identity_and_after_reads_preserving_target_checks(self):
        conn = BatchConnection(changed=2)
        with mock.patch('myflames.connector.MySQLConnection', return_value=conn), mock.patch('myflames.locks.time.sleep') as sleep:
            result = ui._bulk_kill_response(self.payload(5), self.workspace)
        sleep.assert_not_called()
        self.assertEqual(conn.killed, {1, 3, 4, 5})
        self.assertEqual((result['killed'], result['refused']), (4, 1))
        self.assertEqual(sum('SELECT @@server_uuid' in s for s in conn.calls), 1)
        self.assertEqual(sum('SELECT @@version, @@performance_schema' in s for s in conn.calls), 1)
        self.assertEqual(sum('FROM performance_schema.threads' in s and ' IN (' in s for s in conn.calls), 1)
        self.assertEqual(sum('PROCESSLIST_ID = ' in s for s in conn.calls), 5)
        self.assertEqual(len(conn.calls), 3 + 2 * 5 + 4)
        self.assertTrue(all(r['after'] == {'present': False} for r in result['results'] if r['status'] == 'killed'))
        self.assertEqual(len(self.records()), 5)
        self.assertTrue(all(r['after'] == {'present': False} for r in self.records() if r['status'] == 'ok'))

    def test_target_read_verifies_server_identity_without_an_extra_client_call(self):
        conn = BatchConnection(server_changed=2)
        with mock.patch('myflames.connector.MySQLConnection', return_value=conn):
            result = ui._bulk_kill_response(self.payload(2), self.workspace)
        self.assertEqual(conn.killed, {1})
        self.assertEqual(result['results'][1]['status'], 'refused')
        self.assertIn('server identity', result['results'][1]['message'])
        self.assertEqual(sum('SELECT @@server_uuid' in sql for sql in conn.calls), 1)
        self.assertTrue(all('@@server_uuid' in sql for sql in conn.calls if 'PROCESSLIST_ID = ' in sql))

    def test_bulk_rereads_saved_policy_at_each_send(self):
        saved = self.store.save('profile', dict(self.connection, name='review', type='development'))
        self.connection['profile_id'] = saved['id']
        conn = BatchConnection()
        run = conn.run

        def change_policy(sql, extra_flags=None):
            answer = run(sql, extra_flags)
            if sql.startswith(locks.KILL_TAG):
                self.store.save('profile', dict(self.connection, name='review', type='development', read_only=True),
                                key=saved['id'])
            return answer

        conn.run = change_policy
        with mock.patch('myflames.connector.MySQLConnection', return_value=conn):
            result = ui._bulk_kill_response(self.payload(2), self.workspace)
        self.assertEqual(conn.killed, {1})
        self.assertIn('write policy changed', result['results'][1]['message'])
        self.assertEqual(result['results'][1]['status'], 'refused')
        self.assertEqual(self.records()[0]['status'], 'refused')
        self.assertEqual(len(self.records()), 2)

    def test_bulk_after_read_failure_stays_unknown_and_remaining_query_is_reported(self):
        for mode in ('query', 'connection'):
            for after_error in (False, True):
                with self.subTest(mode=mode, after_error=after_error):
                    conn = BatchConnection(remaining=True, after_error=after_error)
                    with mock.patch('myflames.connector.MySQLConnection', return_value=conn):
                        result = ui._bulk_kill_response(self.payload(1, mode=mode), self.workspace)
                    [row] = result['results']
                    after = row['after']
                    self.assertEqual(after is None, after_error)
                    if after is None:
                        self.assertTrue(row['message'].startswith('Sent; could not verify'), row['message'])
                    else:
                        self.assertTrue(after['present'])
                        self.assertEqual(after['statement'], 'SELECT 1')
                        self.assertIn('still stopping' if mode == 'query' else 'still listed', row['message'])
                    self.assertEqual(self.records()[0]['message'], row['message'])

    def test_group_replication_bulk_refusal_is_audited(self):
        conn = BatchConnection(protected=1)
        with mock.patch('myflames.connector.MySQLConnection', return_value=conn):
            result = ui._bulk_kill_response(self.payload(1), self.workspace)
        self.assertEqual(conn.killed, set())
        self.assertEqual(result['results'][0]['message'], GR_REASON)
        self.assertEqual(self.records()[0]['status'], 'refused')

    def test_bulk_query_unavailable_snapshot_reports_accepted_not_stopped(self):
        conn = BatchConnection(after_error=True)
        body = self.payload(2, mode='query')
        with mock.patch('myflames.connector.MySQLConnection', return_value=conn):
            result = ui._bulk_kill_response(body, self.workspace)
            sends = [sql for sql in conn.calls if sql.startswith(locks.KILL_TAG)]
            self.assertEqual(sends, [locks.KILL_TAG + 'KILL QUERY 1', locks.KILL_TAG + 'KILL QUERY 2'])
            # A failed verification never makes a consumed review reusable.
            with self.assertRaisesRegex(ValueError, 'review expired'):
                ui._bulk_kill_response(body, self.workspace)
            self.assertEqual([sql for sql in conn.calls if sql.startswith(locks.KILL_TAG)], sends)
        self.assertEqual(result['killed'], 2)  # Existing count means accepted sends.
        records = {record['id']: record for record in self.records()}
        for row in result['results']:
            self.assertEqual(row['status'], 'killed')
            self.assertIsNone(row['after'])
            self.assertTrue(row['message'].startswith('Sent; could not verify'), row['message'])
            self.assertNotIn('Stopped the running statement', row['message'])
            record = records[row['audit_id']]
            self.assertEqual(record['status'], 'ok')
            self.assertEqual(record['message'], row['message'])
            self.assertIsNone(record['after'])

    def test_interrupted_bulk_query_verification_does_not_audit_completion(self):
        conn = BatchConnection()
        with mock.patch('myflames.connector.MySQLConnection', return_value=conn), \
                mock.patch('myflames.locks._kill_batch_after', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                ui._bulk_kill_response(self.payload(1, mode='query'), self.workspace)
        [record] = self.records()
        self.assertEqual(record['status'], 'ok')
        self.assertIsNone(record['after'])
        self.assertTrue(record['message'].startswith('Sent; could not verify'), record['message'])
        self.assertEqual([sql for sql in conn.calls if sql.startswith(locks.KILL_TAG)],
                         [locks.KILL_TAG + 'KILL QUERY 1'])

    def test_single_kill_unavailable_after_read_does_not_claim_completion(self):
        for mode in ('query', 'connection'):
            with self.subTest(mode=mode):
                target = list(K_TARGET)
                target[5], target[8] = 'Query', 'SELECT 1'
                scenario = KillScenario(target=target, after=ConnectorError('ERROR 1142: command denied'))
                audit = dbops.AuditTrail(self.store, 'kill_query' if mode == 'query' else 'kill', 'ui')
                result = locks.kill_session(scenario.conn, 41, mode, bound_kill_expect(scenario.conn),
                                            settle_s=0, audit=audit, confirmed=True)
                self.assertEqual(result['status'], 'killed')
                self.assertIsNone(result['after'])
                self.assertTrue(result['message'].startswith('Sent; could not verify'), result['message'])
                self.assertEqual(self.records()[0]['message'], result['message'])
                self.assertIsNone(self.records()[0]['after'])
                self.assertEqual(scenario.kills(), [locks.KILL_TAG + 'KILL ' + mode.upper() + ' 41'])


class TestGroupReplicationKill(unittest.TestCase):
    """MySQL Group Replication streams; MariaDB has no such command."""

    def test_command_match_requires_at_least_16_characters_of_the_exact_name(self):
        for width in range(len(GR_FULL_COMMAND) + 1):
            with self.subTest(width=width):
                self.assertEqual(locks.is_group_replication_command(GR_FULL_COMMAND[:width]), width >= 16)
        for command in (None, 16, b'Group Replicatio', 'Query', 'Binlog Dump GTID',
                        'Group ReplicatioX', GR_FULL_COMMAND + ' extra', GR_COMMAND.lower()):
            with self.subTest(command=command):
                self.assertFalse(locks.is_group_replication_command(command))

    def test_single_kill_refuses_group_replication_stream_and_audits(self):
        for mode in ('connection', 'query'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as home:
                target = list(K_TARGET)
                target[5], target[8] = GR_COMMAND, None
                scenario = KillScenario(target=target)
                store = WorkspaceStore(os.path.join(home, 'workspace.db'))
                audit = dbops.AuditTrail(store, 'kill', 'ui')
                result = locks.kill_session(scenario.conn, 41, mode, bound_kill_expect(scenario.conn),
                                            settle_s=0, audit=audit, confirmed=True)
                self.assertEqual((result['status'], result['message']), ('refused', GR_REASON))
                self.assertEqual(scenario.kills(), [])
                self.assertEqual(store.audit_records()[0]['status'], 'refused')

    def test_sessions_marks_stream_as_protected_replication_thread(self):
        for command, source in ((GR_COMMAND, 'performance_schema.processlist'),
                                (GR_COMMAND, 'information_schema.PROCESSLIST'),
                                (GR_FULL_COMMAND, 'SHOW FULL PROCESSLIST')):
            with self.subTest(command=command, source=source):
                row = [41, 'mysql_innodb_cluster_1', 'localhost', None, command, 2, None, None, None]
                session = sessions._session(row, 'mysql', source, None)
                sessions._assess(session)
                self.assertEqual(session['thread_kind'], 'replication')
                self.assertEqual(session['kill_blocked_reason'], GR_REASON)
                self.assertIn('system_thread', session['flags'])
                self.assertFalse(session['explainable'])

    def test_other_commands_are_not_protected_as_group_replication(self):
        for command in ('Query', 'Sleep', 'Group Replicati', 'Group ReplicatioX', '', None):
            with self.subTest(command=command):
                row = [41, 'mysql_innodb_cluster_1', 'localhost', None, command, 2, None, None, None]
                session = sessions._session(row, 'mysql', 'performance_schema.processlist', None)
                sessions._assess(session)
                self.assertEqual(session['thread_kind'], 'client')
                self.assertIsNone(session['kill_blocked_reason'])
                self.assertNotIn('system_thread', session['flags'])

    def test_full_command_name_remains_protected_by_kill_guard(self):
        target = {'command': GR_FULL_COMMAND, 'user': 'mysql_innodb_cluster_1'}
        self.assertEqual(locks._kill_problem(target, 999, 41, 'connection', None),
                         ('refused', GR_REASON))
