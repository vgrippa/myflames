"""Reused sessions expose only already-authorized credentials to HTTP redaction.

The request handler, ownership checks, saved-secret loading and policy gates
are real. Database I/O is replaced at the client/session boundary; no server
or listener is started by these tests.
"""
from email.message import Message
import io
import json
import os
import tempfile
import types
import unittest
from unittest import mock

from myflames import editor_session as ed, live, request_secrets, secret_store, table_data, ui
from myflames.connector import MySQLConnection
from myflames.workspace_store import WorkspaceStore
from test_table_data import meta, model


SECRET = 'saved-session-\'"\\line\n雪'


class ReusedSessionSecrets(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        environment = dict(os.environ, HOME=self.home.name, USERPROFILE=self.home.name,
                           MYFLAMES_SECRET_BACKEND='vault')
        environment.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        patcher = mock.patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = WorkspaceStore()
        profile = self.store.save('profile', {
            'name': 'redaction session', 'host': 'db.example', 'port': 3306,
            'user': 'dba', 'type': 'production',
        }, password=SECRET, remember=True)
        self.route = {'profile_id': profile['id'], 'host': profile['host'],
                      'port': profile['port'], 'user': profile['user']}
        self.workspace = types.SimpleNamespace(
            store=self.store, token='redaction-test-token',
            editor=ed.EditorSessions(reap_every=0), jobs=live.LiveJobs())
        self.addCleanup(self.workspace.editor.close)
        self.addCleanup(self.workspace.jobs.close)

        def start(session, options, defer_safe_updates=False):
            # Authentication's external I/O is the only replaced part of open.
            # The saved password reaches this object via the real store path.
            session._conn = MySQLConnection(**options)
            session.state = 'idle'
            session.engine = 'mysql'
            session.server_version = '8.4.11'
            session.sql_mode = ''
            session.in_transaction = False
            session.ready.set()

        with mock.patch.object(ed.EditorSession, 'start', start):
            opened = ui.api_response('/api/editor/open', self.payload(), self.workspace)
        self.assertEqual(opened['status'], 'idle')
        self.session = self.workspace.editor.get('redaction-tab')
        self.assertEqual(self.session._conn.password, SECRET)
        self.assertEqual(request_secrets.values(), set())

    def payload(self, **fields):
        result = {'connection': dict(self.route), 'tab_id': 'redaction-tab'}
        result.update(fields)
        return result

    def post(self, path, payload):
        """Execute the real HTTP dispatch/error boundary without a socket."""
        handler = object.__new__(ui.WorkspaceHandler)
        handler.path = path
        handler.server = self.workspace
        handler.headers = Message()
        body = json.dumps(payload).encode('utf-8')
        handler.headers['Content-Type'] = 'application/json'
        handler.headers['Content-Length'] = str(len(body))
        handler.headers['X-Myflames-Token'] = self.workspace.token
        handler._local_request = lambda: True
        handler._authenticated = lambda: True
        handler._read_body = lambda size: body
        handler._send = mock.Mock()
        handler.do_POST()
        handler._send.assert_called_once()
        return handler._send.call_args[0][:2]

    def failure(self):
        return RuntimeError('reuse failure: raw=%s; repr=%r; json=%s'
                            % (SECRET, SECRET, json.dumps(SECRET)))

    def assert_reuse_redacted(self, path, payload, target, method):
        output = io.StringIO()
        with request_secrets.scope() as registry, \
                mock.patch.object(self.store, 'connection_with_saved_password') as saved, \
                mock.patch.object(secret_store, 'get_secret') as secrets, \
                mock.patch.object(target, method, side_effect=self.failure()) as fault, \
                mock.patch.object(ui.sys, 'stderr', output):
            saved.side_effect = AssertionError('reuse must not resolve saved credentials')
            secrets.side_effect = AssertionError('reuse must not read the secret backend')
            status, response = self.post(path, payload)
            fault.assert_called_once()
            saved.assert_not_called()
            secrets.assert_not_called()
            self.assertEqual(status, 500, response)
            captured = request_secrets.values()
        self.assertEqual(registry.values(), set())
        logged = output.getvalue()
        self.assertIn('RuntimeError: reuse failure:', logged)
        self.assertGreaterEqual(logged.count('[redacted]'), 3)
        for variant in (SECRET, repr(SECRET)[1:-1], json.dumps(SECRET)[1:-1]):
            self.assertNotIn(variant, logged)
            self.assertNotIn(variant, json.dumps(response))
        self.assertEqual(captured, {SECRET})

    def assert_refused_without_capture(self, path, payload, statuses=(400, 409)):
        with request_secrets.scope(), \
                mock.patch.object(self.store, 'connection_with_saved_password') as saved, \
                mock.patch.object(secret_store, 'get_secret') as secrets:
            status, response = self.post(path, payload)
            self.assertIn(status, statuses, response)
            self.assertEqual(request_secrets.values(), set())
            saved.assert_not_called()
            secrets.assert_not_called()

    def test_commit_and_rollback_capture_after_their_identity_checked_lookups(self):
        for action in ('commit', 'rollback'):
            with self.subTest(action=action):
                self.assert_reuse_redacted('/api/editor/' + action, self.payload(),
                                           self.session, 'busy')

    def test_status_and_close_capture_only_the_owned_session(self):
        for action, method in (('status', 'status'), ('close', 'end')):
            with self.subTest(action=action):
                self.assert_reuse_redacted('/api/editor/' + action, self.payload(),
                                           self.session, method)

    def test_use_captures_before_the_database_change(self):
        self.assert_reuse_redacted('/api/editor/use', self.payload(database='app'),
                                   ed, '_use_database')

    def test_run_captures_before_a_gate_that_does_not_resolve_the_password(self):
        # Production UPDATE requires review. The real gate returns before
        # fill_password, then the session-status read fails unexpectedly.
        self.assert_reuse_redacted('/api/editor/run',
                                   self.payload(sql='UPDATE app.t SET n = 2'),
                                   self.session, 'status')
        self.assertTrue(self.workspace.editor.tokens)

    def test_stop_fallback_captures_the_retained_run_password_without_a_connection(self):
        connection = self.session._conn
        self.session._conn = None
        self.session.run = types.SimpleNamespace(options={'password': SECRET})
        try:
            # A resolver can refuse before consulting the store. Cleanup then
            # uses the authenticated run's retained options, even after _conn
            # has been released. This is separate from _owned_session capture.
            with mock.patch.object(ui, '_with_saved_password',
                                   side_effect=ValueError('connection consent revoked')) as fill:
                self.assert_reuse_redacted('/api/editor/stop', self.payload(),
                                           self.session, 'stop')
                fill.assert_called_once()
        finally:
            self.session._conn = connection
            self.session.run = None

    def test_editor_identity_refusals_never_capture_the_other_sessions_password(self):
        for action in ('commit', 'rollback', 'status', 'close', 'stop', 'use', 'run'):
            with self.subTest(action=action):
                payload = self.payload(database='app', sql='SELECT 1')
                payload['connection']['host'] = 'other.example'
                statuses = (200,) if action in ('status', 'close') else (409,)
                self.assert_refused_without_capture('/api/editor/' + action, payload, statuses)
        self.assertIs(self.workspace.editor.get('redaction-tab'), self.session)
        self.assertFalse(self.session.ended)

    def test_missing_sessions_and_sessions_without_credentials_capture_nothing(self):
        for lookup in (ed._session_for, ed._owned_session):
            with self.subTest(lookup=lookup.__name__), request_secrets.scope():
                self.assertIsNone(lookup(self.workspace.editor, 'absent', self.session.identity))
                connection = self.session._conn
                try:
                    self.session._conn = None
                    self.assertIs(lookup(self.workspace.editor, self.session.tab_id,
                                         self.session.identity), self.session)
                finally:
                    self.session._conn = connection
                self.assertEqual(request_secrets.values(), set())

    def race_with_authenticated_session(self, new_password):
        """Publish another request's session during this request's real fill.

        Its old authenticated password differs from the newly saved password.
        The initial lookup sees no session; _open_session must attach the
        winner's credential after checking the returned session's identity.
        """
        profile_id = self.route['profile_id']
        self.store.save('profile', self.store.get('profile', profile_id), profile_id,
                        password=new_password, remember=True)
        self.workspace.editor.remove(self.session.tab_id)
        fill = self.store.connection_with_saved_password

        def fill_and_publish(connection):
            filled = fill(connection)
            self.workspace.editor.open(self.session.tab_id, lambda: self.session,
                                       self.session.identity)
            return filled

        return fill_and_publish

    def test_open_race_captures_the_winning_sessions_different_password(self):
        fresh = 'new-saved-password-for-opening-request'
        for action, fault_method in (('open', 'status'), ('run', 'busy')):
            with self.subTest(action=action):
                publish = self.race_with_authenticated_session(fresh)
                output = io.StringIO()
                with request_secrets.scope() as registry, \
                        mock.patch.object(self.store, 'connection_with_saved_password',
                                          side_effect=publish) as saved, \
                        mock.patch.object(secret_store, 'get_secret',
                                          wraps=secret_store.get_secret) as secrets, \
                        mock.patch.object(self.session, fault_method,
                                          side_effect=self.failure()) as fault, \
                        mock.patch.object(ui.sys, 'stderr', output):
                    status, response = self.post('/api/editor/' + action, self.payload(sql='SELECT 1'))
                    self.assertEqual(status, 500, response)
                    fault.assert_called_once()
                    # One authorized fill for the provisional open. Adopting
                    # the winner and error logging may not read another secret.
                    saved.assert_called_once()
                    secrets.assert_called_once()
                    captured = request_secrets.values()
                self.assertEqual(registry.values(), set())
                logged = output.getvalue()
                self.assertIn('RuntimeError: reuse failure:', logged)
                self.assertGreaterEqual(logged.count('[redacted]'), 3)
                for variant in (SECRET, repr(SECRET)[1:-1], json.dumps(SECRET)[1:-1]):
                    self.assertNotIn(variant, logged)
                    self.assertNotIn(variant, json.dumps(response))
                self.assertEqual(captured, {SECRET, fresh})

    def test_open_race_rechecks_identity_before_capturing_the_winner(self):
        fresh = 'new-saved-password-for-refused-opening-request'
        publish = self.race_with_authenticated_session(fresh)
        identity = self.session.identity

        def changed_while_waiting(timeout):
            self.session.identity = 'a different session identity'
            return True

        try:
            with request_secrets.scope(), \
                    mock.patch.object(self.store, 'connection_with_saved_password',
                                      side_effect=publish) as saved, \
                    mock.patch.object(secret_store, 'get_secret',
                                      wraps=secret_store.get_secret) as secrets, \
                    mock.patch.object(self.session.ready, 'wait', side_effect=changed_while_waiting):
                status, response = self.post('/api/editor/open', self.payload())
                self.assertEqual(status, 409, response)
                self.assertEqual(response['code'], 'identity_mismatch')
                self.assertEqual(request_secrets.values(), {fresh})
                saved.assert_called_once()
                secrets.assert_called_once()
        finally:
            self.session.identity = identity

    def test_open_race_without_a_connection_has_no_additional_credential(self):
        fresh = 'new-saved-password-with-no-winning-connection'
        publish = self.race_with_authenticated_session(fresh)
        connection = self.session._conn
        self.session._conn = None
        try:
            with request_secrets.scope(), \
                    mock.patch.object(self.store, 'connection_with_saved_password', side_effect=publish):
                status, response = self.post('/api/editor/open', self.payload())
                self.assertEqual(status, 200, response)
                self.assertEqual(response['status'], 'idle')
                self.assertEqual(request_secrets.values(), {fresh})
        finally:
            self.session._conn = connection

    def test_in_session_plan_capture_logs_only_redacted_credentials(self):
        payload = self.payload(session_tab=self.session.tab_id, query='SELECT 1')
        self.assert_reuse_redacted('/api/live/start', payload, self.session, 'run_plan')

    def test_in_session_plan_capture_refuses_a_foreign_identity_without_capture(self):
        payload = self.payload(session_tab=self.session.tab_id, query='SELECT 1')
        payload['connection']['host'] = 'other.example'
        self.assert_refused_without_capture('/api/live/start', payload, (409,))

    def open_capture(self):
        # Start and join the real registry worker, with only server work stubbed.
        # Completed workers retain options here so status/cancel can exercise
        # the credential while a normal capture would still be in progress.
        with mock.patch.object(live.LiveJob, 'run', lambda job: None):
            opened = ui.api_response('/api/live/start', self.payload(query='SELECT 1'), self.workspace)
            worker = self.workspace.jobs.threads[opened['id']]
            worker.join(5)
            self.assertFalse(worker.is_alive())
        job = self.workspace.jobs.jobs[opened['id']]
        self.assertEqual(job.options['password'], SECRET)
        return job

    def test_capture_status_and_cancel_register_the_owned_jobs_password(self):
        job = self.open_capture()
        for action, method in (('status', 'snapshot'), ('cancel', 'cancel')):
            with self.subTest(action=action):
                self.assert_reuse_redacted('/api/live/' + action, self.payload(id=job.id), job, method)

    def test_capture_identity_refusals_never_capture_the_other_jobs_password(self):
        job = self.open_capture()
        for action in ('status', 'cancel'):
            for connection in (dict(self.route, host='other.example'), None):
                with self.subTest(action=action, connection=connection):
                    self.assert_refused_without_capture('/api/live/' + action,
                                                       self.payload(id=job.id, connection=connection))
        self.assertFalse(job.cancelled.is_set())

    def open_table_snapshot(self):
        registry = table_data.TableData()
        self.workspace.table_data = registry

        def close():
            registry.close()
            registry.reaper.join(5)
            self.assertFalse(registry.reaper.is_alive())

        self.addCleanup(close)
        options, identity, safety, label = ed._connection_facts(self.payload(), self.store)
        snapshot = table_data.Snapshot(identity, safety, self.session, 'app', 't', meta(),
                                       [['31', '6F6C64', '00FF']], model(), 0, 50)
        registry.add(snapshot)
        return snapshot

    def test_table_snapshot_registers_the_owned_sessions_password(self):
        snapshot = self.open_table_snapshot()
        for action, target, method in (('status', snapshot, 'status'),
                                       ('cancel', snapshot, 'status'),
                                       ('close', self.session, 'end')):
            with self.subTest(action=action):
                self.assert_reuse_redacted('/api/table-data/' + action,
                                           self.payload(snapshot_id=snapshot.id), target, method)

    def test_table_snapshot_identity_refusals_capture_nothing(self):
        snapshot = self.open_table_snapshot()
        for action in ('status', 'cancel', 'close', 'prepare', 'apply', 'decline'):
            with self.subTest(action=action):
                payload = self.payload(snapshot_id=snapshot.id)
                payload['connection']['host'] = 'other.example'
                self.assert_refused_without_capture('/api/table-data/' + action, payload)
        self.assertFalse(snapshot.cancel.is_set())
        self.assertIs(self.workspace.table_data.get(snapshot.id, snapshot.identity), snapshot)


if __name__ == '__main__':
    unittest.main()
