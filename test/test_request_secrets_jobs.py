"""Saved job and SSH credentials enter diagnostics only after owner checks.

Exercise the HTTP handler without a listening socket. Jobs hold credentials
resolved in a previous request; no worker, database or SSH client is started.
"""
from email.message import Message
import io
import json
import os
import tempfile
import types
import unittest
from unittest import mock

from myflames import dbops, lifecycle, replication, request_secrets, secret_store
from myflames import sftp, sftp_api, ssh_connections, ui
from myflames.connector import MySQLConnection
from myflames.workspace_store import WorkspaceStore


class TestJobSecretReuse(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory(prefix='myflames-job-secrets-')
        self.addCleanup(self.home.cleanup)
        environment = mock.patch.dict(os.environ, HOME=self.home.name, USERPROFILE=self.home.name,
                                      MYFLAMES_SECRET_BACKEND='vault')
        environment.start()
        self.addCleanup(environment.stop)
        self.store = WorkspaceStore()
        self.password = "saved-\u03a9-'quote\"-\\backslash"
        self.donor_password = "donor-\u03bb-'quote\"-\\backslash"
        self.other_password = 'another-owners-secret'
        profile = self.store.save('profile', {'name': 'owner', 'host': 'owner.example',
                                  'port': 3306, 'user': 'dba', 'group': 'G'},
                                  password=self.password, remember=True)
        self.connection = {key: profile[key] for key in ('host', 'port', 'user')}
        self.connection.update(profile_id=profile['id'], group_name='G')
        donor = self.store.save('profile', {'name': 'donor', 'host': 'donor.example',
                                'port': 3306, 'user': 'dba'},
                                password=self.donor_password, remember=True)
        # End the initiating request before any reuse request begins.
        with request_secrets.scope():
            self.full = self.store.connection_with_saved_password(self.connection)
            donor_full = self.store.connection_with_saved_password(dict(donor, profile_id=donor['id']))
        self.workspace = types.SimpleNamespace(store=self.store, token='test-token',
                                               lifecycle=lifecycle.LifecycleJobs())
        self.job = lifecycle.LifecycleJob('clone', lambda job: None, self.full,
                                          passwords=(donor_full['password'],))
        with mock.patch.object(self.job, 'start'):
            ui._lifecycle_add(self.workspace, self.job, 'owner.example:3306', self.connection)
        other = lifecycle.LifecycleJob('dump', lambda job: None,
                                      {'host': 'other.example', 'password': self.other_password})
        with mock.patch.object(other, 'start'):
            ui._lifecycle_add(self.workspace, other, 'other.example:3306', {'host': 'other.example'})
        self.other_job = other

    def post(self, path, payload):
        """Keep real POST dispatch, error handling, and request-scope lifetime."""
        handler = object.__new__(ui.WorkspaceHandler)
        handler.path, handler.server = path, self.workspace
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
        return handler._send.call_args[0]

    def forms(self, password):
        return (password, repr(password)[1:-1], json.dumps(password)[1:-1])

    def failure(self, passwords, seen):
        def fail(*args, **kwargs):
            seen.append(request_secrets.values())
            forms = [form for password in passwords for form in self.forms(password)]
            raise RuntimeError('credential-bearing failure: ' + ' | '.join(forms))
        return fail

    def assert_redacted_failure(self, path, payload, passwords, target, method):
        seen, output = [], io.StringIO()
        with mock.patch.object(target, method, side_effect=self.failure(passwords, seen)) as action, \
                mock.patch.object(secret_store, 'get_secret') as lookup, \
                mock.patch.object(self.store, 'connection_with_saved_password') as fill, \
                mock.patch.object(ui.sys, 'stderr', output):
            status, response = self.post(path, payload)
        action.assert_called_once()
        lookup.assert_not_called()
        fill.assert_not_called()
        self.assertEqual(status, 500, response)
        logged = output.getvalue()
        self.assertIn('RuntimeError: credential-bearing failure:', logged)
        self.assertGreaterEqual(logged.count('[redacted]'), 3 * len(passwords))
        for password in passwords:
            for form in self.forms(password):
                self.assertNotIn(form, logged)
                self.assertNotIn(form, json.dumps(response))
        self.assertEqual(seen, [set(passwords)])
        self.assertEqual(request_secrets.values(), set())

    def test_lifecycle_status_cancel_and_jobs_capture_all_owned_passwords(self):
        for route in ('status', 'cancel', 'jobs'):
            with self.subTest(route=route):
                self.assert_redacted_failure(
                    '/api/lifecycle/' + route, {'connection': self.connection, 'id': self.job.id},
                    (self.password, self.donor_password), self.job,
                    'cancel' if route == 'cancel' else 'snapshot')

    def test_lifecycle_refused_owners_and_revoked_group_capture_nothing(self):
        for connection in (dict(self.connection, host='wrong.example'),
                           dict(self.connection, group_name='revoked'),
                           {'host': 'owner.example', 'port': 3306, 'user': 'stranger'}):
            for route in ('status', 'cancel', 'jobs'):
                with self.subTest(route=route, connection=connection), request_secrets.scope(), \
                        mock.patch.object(self.job, 'snapshot') as snapshot, \
                        mock.patch.object(self.job, 'cancel') as cancel, \
                        mock.patch.object(secret_store, 'get_secret') as lookup, \
                        mock.patch.object(self.store, 'connection_with_saved_password') as fill:
                    status, response = self.post('/api/lifecycle/' + route,
                                                 {'connection': connection, 'id': self.job.id})
                    self.assertEqual(status, 200 if route == 'jobs' else 400, response)
                    if route == 'jobs':
                        self.assertEqual(response['jobs'], [])
                    self.assertEqual(request_secrets.values(), set())
                    snapshot.assert_not_called()
                    cancel.assert_not_called()
                    lookup.assert_not_called()
                    fill.assert_not_called()

    def loops(self):
        identity = dbops.connection_identity(self.connection,
                    dbops.connection_safety(self.store.get('profile', self.connection['profile_id']),
                                            self.connection))
        loop = replication.SkipLoop(MySQLConnection('owner.example', password=self.full['password']), '', [1062], 1,
                                    identity=identity)
        other_identity = dbops.connection_identity({'host': 'other.example'})
        other = replication.SkipLoop(MySQLConnection('other.example', password=self.other_password), '', [1062], 1,
                                     identity=other_identity)
        registry = replication.SkipLoopRegistry()
        # Register the already-created objects without starting replication IO.
        registry._jobs = [(identity, loop), (other_identity, other)]
        self.workspace.replication_loops = registry
        return loop, other

    def test_skip_loop_status_list_and_stop_capture_only_matching_connection(self):
        loop, other = self.loops()
        for route, specific in (('status', True), ('status', False), ('stop', True)):
            with self.subTest(route=route, specific=specific), \
                    mock.patch.object(other, 'status') as unrelated:
                payload = {'connection': self.connection}
                if specific:
                    payload['job_id'] = loop.job_id
                self.assert_redacted_failure('/api/live/replication/skip-loop/' + route,
                                             payload, (self.password,), loop,
                                             'stop' if route == 'stop' else 'status')
                unrelated.assert_not_called()

    def test_skip_loop_refused_identity_and_revoked_group_capture_nothing(self):
        loop, other = self.loops()
        for connection in (dict(self.connection, host='wrong.example'),
                           dict(self.connection, group_name='revoked')):
            for route, specific in (('status', True), ('status', False), ('stop', True)):
                with self.subTest(route=route, specific=specific, connection=connection), \
                        request_secrets.scope(), mock.patch.object(loop, 'status') as status_call, \
                        mock.patch.object(loop, 'stop') as stop, \
                        mock.patch.object(secret_store, 'get_secret') as lookup, \
                        mock.patch.object(self.store, 'connection_with_saved_password') as fill:
                    payload = {'connection': connection}
                    if specific:
                        payload['job_id'] = loop.job_id
                    status, response = self.post('/api/live/replication/skip-loop/' + route, payload)
                    self.assertEqual(status, 400 if specific else 200, response)
                    if not specific:
                        self.assertEqual(response['loops'], [])
                    self.assertEqual(request_secrets.values(), set())
                    status_call.assert_not_called()
                    stop.assert_not_called()
                    lookup.assert_not_called()
                    fill.assert_not_called()

    def test_saved_ssh_secret_is_captured_after_binding_with_no_diagnostic_lookup(self):
        draft = dict(name='SSH', host='ssh.example', port=22, user='dba', auth='password',
                     key_path='', remote_dir='', local_dir='')
        saved = ssh_connections.save(self.store, {'connection': draft, 'secret': self.password})['connection']
        record = ssh_connections.get(self.store, saved['id'])
        seen, output = [], io.StringIO()
        with mock.patch.object(sftp, 'SftpSession', side_effect=self.failure((self.password,), seen)) as create, \
                mock.patch.object(secret_store, 'get_secret', wraps=secret_store.get_secret) as lookup, \
                mock.patch.object(self.store, 'connection_with_saved_password') as fill, \
                mock.patch.object(ui.sys, 'stderr', output):
            status, response = self.post('/api/ssh/connections/test', {'id': saved['id']})
        create.assert_called_once()
        lookup.assert_called_once_with(record['secret_ref'], record['secret_backend'])
        fill.assert_not_called()
        self.assertEqual(status, 500, response)
        self.assertIn('RuntimeError: credential-bearing failure:', output.getvalue())
        self.assertGreaterEqual(output.getvalue().count('[redacted]'), 3)
        for form in self.forms(self.password):
            self.assertNotIn(form, output.getvalue())
            self.assertNotIn(form, json.dumps(response))
        self.assertEqual(seen, [{self.password}])
        self.assertEqual(request_secrets.values(), set())

    def test_ssh_binding_refusal_and_missing_secret_capture_nothing(self):
        draft = dict(name='SSH', host='ssh.example', port=22, user='dba', auth='password',
                     key_path='', remote_dir='', local_dir='')
        saved = ssh_connections.save(self.store, {'connection': draft, 'secret': self.password})['connection']
        record = ssh_connections.get(self.store, saved['id'])
        with request_secrets.scope(), mock.patch.object(secret_store, 'get_secret') as lookup, \
                mock.patch.object(sftp, 'SftpSession') as create:
            with self.assertRaisesRegex(ValueError, 'different connection details'):
                ssh_connections.open_session(dict(record, host='wrong.example'))
            lookup.assert_not_called()
            create.assert_not_called()
            self.assertEqual(request_secrets.values(), set())
        with request_secrets.scope(), mock.patch.object(secret_store, 'get_secret', return_value=None), \
                mock.patch.object(sftp, 'SftpSession') as create:
            with self.assertRaisesRegex(ValueError, 'secret is missing'):
                ssh_connections.open_session(record)
            create.assert_not_called()
            self.assertEqual(request_secrets.values(), set())

    def test_cached_ssh_reuse_captures_only_the_selected_sessions_secret(self):
        draft = dict(name='SSH cache', host='ssh.example', port=22, user='dba', auth='password',
                     key_path='', remote_dir='', local_dir='')
        saved = ssh_connections.save(self.store, {'connection': draft, 'secret': self.password})['connection']
        sessions = []

        def transport(secret, **kwargs):
            session = types.SimpleNamespace(_password=secret, alive=lambda: True,
                                            close=mock.Mock(), realpath=mock.Mock())
            sessions.append(session)
            return session

        self.addCleanup(sftp_api.close_sessions)
        # The normal open resolves the saved password once. Only the external
        # transport and idle timer are replaced; the cache and key checks run.
        with mock.patch.object(sftp, 'SftpSession', side_effect=transport), \
                mock.patch.object(sftp_api.threading, 'Timer'), request_secrets.scope():
            with sftp_api._session(self.store, saved['id']):
                pass
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]._password, self.password)
        self.assertEqual(request_secrets.values(), set())
        with request_secrets.scope(), mock.patch.object(secret_store, 'get_secret') as lookup, \
                mock.patch.object(self.store, 'connection_with_saved_password') as fill:
            status, response = self.post('/api/files/remote/list', {'ssh': 'f' * 32, 'path': ''})
            self.assertEqual(status, 400, response)
            self.assertEqual(request_secrets.values(), set())
            lookup.assert_not_called()
            fill.assert_not_called()
            sessions[0].realpath.assert_not_called()
        with mock.patch.object(ssh_connections, 'open_session') as reopen:
            self.assert_redacted_failure('/api/files/remote/list', {'ssh': saved['id'], 'path': ''},
                                         (self.password,), sessions[0], 'realpath')
            reopen.assert_not_called()
        sessions[0].close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
