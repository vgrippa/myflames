"""Exercise the real HTTP POST handler using streams, never a listening socket."""
import contextlib
import copy
from email.message import Message
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from myflames import accounts_api, dbops, editor_session, lifecycle, ui
# These handlers load lazily in ui.py and retain connector static helpers.
# Import before replacing the factory so no module caches a test mock when
# this suite runs before the server-view suites in the same interpreter.
from myflames import locks, replication, variables  # noqa: F401
from myflames.connector import ConnectorError
from myflames.workspace_store import SessionTooLarge, WorkspaceStore
from test_accounts import FakeConn, actor
from test_locks import KillScenario, bound_kill_expect
from test_replication import status as replication_status


# Minimal reviewed requests: validation and policy are real, only I/O is guarded.
WRITES = {
    '/api/live/kill': {'connection_id': 41},
    '/api/live/replication/skip': {'channel': ''},
    '/api/live/variable/apply': {
        'name': 'max_connections', 'mode': 'global', 'value': '300',
        'expect': {'name': 'max_connections', 'mode': 'global',
                   'statement': 'SET GLOBAL max_connections = 300',
                   'persisted_state': {}, 'source_metadata': {}, 'server_identity': {},
                   'engine': 'mysql', 'server_version': '8.4.6'}},
    '/api/accounts/apply': {},  # Filled by /api/accounts/plan before apply dispatch.
    '/api/lifecycle/start': {
        'operation': 'dump', 'plan': {'operation': 'dump', 'commands': ['mysqldump shop']}},
    '/api/editor/run': {'tab_id': 'http-write', 'sql': 'DELETE FROM t'},
}


class TestWriteHTTP(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = mock.patch.dict(os.environ, {'HOME': temp.name, 'USERPROFILE': temp.name,
                                          'MYFLAMES_SECRET_BACKEND': 'vault'})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        self.server = SimpleNamespace(origin='http://127.0.0.1:8765', token='test-session-token',
                                      cookie_name='myflames_test', session_secret='test-cookie-secret',
                                      store=WorkspaceStore(),
                                      editor=editor_session.EditorSessions(reap_every=0))
        self.addCleanup(self.server.editor.close)
        accounts_api._PLANS.clear()
        self.addCleanup(accounts_api._PLANS.clear)
        self.connection = {'host': 'db.internal', 'port': 3306, 'user': 'admin',
                           'password': 'http-connection-secret'}
        self.kill_expect = bound_kill_expect(KillScenario().conn, connection=self.connection)
        report, _ = replication_status('mysql-8.4-gtid-error')
        self.skip_plan = replication.plan_skip(report, '')
        # Even a regression past the connection guards cannot open a socket.
        for name in ('socket.socket', 'socket.create_connection'):
            guard = mock.patch(name, side_effect=AssertionError('HTTP tests must not open sockets'))
            guard.start()
            self.addCleanup(guard.stop)

    def payload(self, path):
        payload = copy.deepcopy(WRITES[path])
        payload['connection'] = dict(self.connection)
        if path == '/api/live/kill':
            payload['expect'] = copy.deepcopy(self.kill_expect)
        elif path == '/api/live/replication/skip':
            # Full reviewed identities are validated before the policy gate.
            payload['expect'] = copy.deepcopy(self.skip_plan['identity'])
            payload['statements'] = list(self.skip_plan['statements'])
        elif path == '/api/live/variable/apply':
            payload['expect']['connection_identity'] = dbops.connection_identity(self.connection)
        return payload

    def test_socket_setup_preserves_headers_and_authenticated_json_body(self):
        plan = Path(__file__).with_name('mysql-explain-json-sample.json').read_text(encoding="utf-8")
        body = json.dumps({'plan': plan}).encode('utf-8')
        headers = ('POST /api/analyze HTTP/1.1\r\nHost: 127.0.0.1:8765\r\n'
                   'Origin: %s\r\nCookie: myflames_test=test-cookie-secret\r\n'
                   'X-Myflames-Token: %s\r\nContent-Type: application/json\r\n'
                   'Content-Length: %d\r\n\r\n' % (self.server.origin, self.server.token, len(body)))
        for buffered in (False, True):
            with self.subTest(buffered=buffered):
                request, sent = mock.Mock(), []
                raw = io.BytesIO(headers.encode('ascii') + body)
                request.makefile.return_value = io.BufferedReader(raw) if buffered else raw
                request.sendall.side_effect = sent.append
                # Exercise setup, the stdlib HTTP parser, dispatch and finish,
                # using memory streams even when socket binding is forbidden.
                ui.WorkspaceHandler(request, ('127.0.0.1', 1234), self.server)
                head, content = b''.join(sent).split(b'\r\n\r\n', 1)
                self.assertIn(b' 200 ', head.split(b'\r\n', 1)[0])
                self.assertIn('analysis', json.loads(content))
                self.assertTrue(raw.closed)

    def reviewed_payload(self, path, connection=None):
        """Review accounts over HTTP before installing apply-time I/O guards."""
        payload = self.payload(path)
        if connection is not None:
            payload['connection'] = dict(connection)
        if path == '/api/accounts/apply':
            conn = FakeConn(name='app')
            with mock.patch.object(accounts_api, 'MySQLConnection', return_value=conn), \
                    mock.patch.object(accounts_api.privileges, 'read_privileges',
                                      side_effect=lambda conn: actor(conn.engine)):
                status, plan = self.post('/api/accounts/plan', {
                    'connection': payload['connection'], 'operation': 'lock', 'user': 'app', 'host': '%'})
            self.assertEqual(status, 200)
            self.assertRegex(plan['plan_id'], r'^[0-9a-f]{32}$')
            self.assertEqual(conn.stdin, [])
            payload.update(plan_id=plan['plan_id'], expect=plan['expect'])
        if path == '/api/live/kill':
            payload['review_token'] = ui._review_tokens(self.server).issue('kill',
                ui._review_identity(payload['connection'], self.server), ui._kill_review_details(payload))
        elif path == '/api/live/replication/skip':
            payload['review_token'] = ui._review_tokens(self.server).issue('replication_skip',
                ui._review_identity(payload['connection'], self.server),
                {k: payload.get(k) for k in ('channel', 'expect', 'statements')})
        elif path == '/api/lifecycle/start':
            payload['plan']['review_token'] = ui._review_tokens(self.server).issue('lifecycle',
                ui._review_identity(payload['connection'], self.server), ui._lifecycle_review_details(payload['plan']))
        return payload

    def post(self, path, payload=None, headers=None, raw=None):
        """Bypass socket setup, retaining do_POST, dispatch and HTTP serialization."""
        body = raw if raw is not None else json.dumps(payload).encode('utf-8')
        values = {'Host': '127.0.0.1:8765', 'Origin': self.server.origin,
                  'Cookie': 'myflames_test=test-cookie-secret',
                  'X-Myflames-Token': self.server.token, 'Content-Type': 'application/json',
                  'Content-Length': str(len(body))}
        values.update(headers or {})
        handler = object.__new__(ui.WorkspaceHandler)
        handler.server = self.server
        handler.connection = mock.Mock()
        handler.path, handler.command = path, 'POST'
        handler.request_version = 'HTTP/1.1'
        handler.requestline = 'POST %s HTTP/1.1' % path
        handler.headers = Message()
        for key, value in values.items():
            if value is not None:
                handler.headers[key] = value
        handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
        handler.do_POST()
        head, content = handler.wfile.getvalue().split(b'\r\n\r\n', 1)
        lines = head.decode('ascii').split('\r\n')
        status = int(lines[0].split()[1])
        response_headers = dict(line.split(': ', 1) for line in lines[1:])
        self.assertEqual(int(response_headers['Content-Length']), len(content))
        self.assertEqual(response_headers['Cache-Control'], 'no-store')
        self.assertEqual(response_headers['X-Content-Type-Options'], 'nosniff')
        return status, json.loads(content)

    def no_connections(self):
        """Guard all six routes, including factories imported under local aliases."""
        # ExitStack also lets callers assert mocks were untouched: do_POST catches
        # arbitrary exceptions and would otherwise turn a guard failure into 500.
        context = contextlib.ExitStack()
        guards = [context.enter_context(mock.patch('myflames.connector.MySQLConnection',
                          side_effect=AssertionError('connection opened before policy')))]
        for obj, name in ((accounts_api, 'MySQLConnection'), (lifecycle, '_connect'),
                          (lifecycle, 'dump_job'), (editor_session, '_open_session'),
                          (ui, '_with_saved_password')):
            guards.append(context.enter_context(mock.patch.object(
                obj, name, side_effect=AssertionError('I/O before policy: ' + name))))
        return context, guards

    def test_write_routes_are_registered_outside_read_only_server_views(self):
        for path in WRITES:
            with self.subTest(path=path):
                self.assertIn(path, ui._WRITE_PATHS)
                self.assertNotIn(path, ui._SERVER_VIEW_PATHS)

    def test_every_write_rejects_invalid_token_host_and_origin_before_dispatch(self):
        bad_headers = ({'Cookie': None}, {'Cookie': 'myflames_test=wrong'}, {'Origin': None}, {'X-Myflames-Token': None}, {'X-Myflames-Token': 'wrong'},
                       {'Host': 'attacker.example:8765'}, {'Host': None},
                       {'Origin': 'https://attacker.example'}, {'Origin': 'null'})
        with mock.patch.object(ui, 'api_response') as dispatch:
            for path in WRITES:
                for headers in bad_headers:
                    with self.subTest(path=path, headers=headers):
                        status, result = self.post(path, self.payload(path), headers)
                        self.assertEqual(status, 403)
                        self.assertIn('error', result)
            dispatch.assert_not_called()
        self.assertEqual(self.server.store.audit_records(), [])

    def test_every_write_rejects_bad_framing_and_json_before_dispatch(self):
        cases = (({'Content-Length': str(ui.MAX_BODY + 1)}, b'{}', 413),
                 ({'Content-Length': '0'}, b'', 413),
                 ({'Transfer-Encoding': 'chunked'}, b'{}', 413),
                 ({'Content-Length': 'not-an-integer'}, b'{}', 400),
                 ({}, b'{broken json', 400), ({}, b'\xff', 400),
                 ({'Content-Type': 'text/plain'}, b'{}', 415))
        with mock.patch.object(ui, 'api_response') as dispatch:
            for path in WRITES:
                for headers, body, expected in cases:
                    with self.subTest(path=path, headers=headers, body=body):
                        status, result = self.post(path, headers=headers, raw=body)
                        self.assertEqual(status, expected)
                        self.assertIn('error', result)
            dispatch.assert_not_called()

    def test_missing_or_nonboolean_confirmation_sends_nothing(self):
        for path in WRITES:
            for confirmed in (None, False, 'true', 1):
                with self.subTest(path=path, confirmed=confirmed):
                    payload = self.reviewed_payload(path)
                    if confirmed is not None:
                        payload['confirmed'] = confirmed
                    context, guards = self.no_connections()
                    with context:
                        status, result = self.post(path, payload)
                        self.assertEqual(status, 200)
                        envelope = result['result'] if path == '/api/lifecycle/start' else result
                        self.assertEqual(envelope['status'], 'needs_confirmation')
                        for guard in guards:
                            guard.assert_not_called()

    def test_kill_requires_reviewed_identity_before_confirmation(self):
        """A malformed target is rejected before checking literal confirmation."""
        context, guards = self.no_connections()
        with context:
            for expect in (None, {}, {'user': 'app'}, {'host': 'client:5000'}):
                with self.subTest(expect=expect):
                    payload = self.payload('/api/live/kill')
                    if expect is None:
                        payload.pop('expect')
                    else:
                        payload['expect'] = expect
                    status, result = self.post('/api/live/kill', payload)
                    self.assertEqual(status, 400)
                    self.assertIn('session you confirmed', result['error'])
            for guard in guards:
                guard.assert_not_called()

    def test_read_only_saved_profile_cannot_be_overridden_by_request(self):
        profile = self.server.store.save('profile', dict(self.connection, name='locked', read_only=True))
        for path in WRITES:
            with self.subTest(path=path):
                # The reviewed identity includes the saved profile id.
                payload = self.reviewed_payload(path, dict(
                    self.connection, profile_id=profile['id'], read_only=False, type='development'))
                payload['confirmed'] = True
                before = {r['id'] for r in self.server.store.audit_records()}
                context, guards = self.no_connections()
                with context:
                    status, result = self.post(path, payload)
                    self.assertEqual(status, 200)
                    envelope = result['result'] if path == '/api/lifecycle/start' else result
                    self.assertEqual(envelope['status'], 'refused')
                    records = [r for r in self.server.store.audit_records() if r['id'] not in before]
                    self.assertEqual(len(records), 1)
                    record = records[0]
                    # Lifecycle stores gate's refusal outcome verbatim; the
                    # other handlers label the read-only reason separately.
                    outcome = 'refused' if path == '/api/lifecycle/start' else 'read_only'
                    self.assertEqual((record['status'], record['outcome'], record['surface']),
                                     ('refused', outcome, 'ui'))
                    self.assertTrue(record['connection']['read_only'])
                    ids = result.get('audit_ids', [envelope.get('audit_id')])
                    self.assertEqual(ids, [record['id']])
                    self.assertNotIn(self.connection['password'], json.dumps(records))
                    for guard in guards:
                        guard.assert_not_called()

    def test_error_status_mapping(self):
        cases = ((editor_session.EditorError('tab busy', 'busy'), 409, 'busy'),
                 (editor_session.EditorError('no tab', 'no_session'), 400, 'no_session'),
                 (SessionTooLarge('workspace too large'), 413, 'session_too_large'),
                 (RuntimeError('private implementation detail'), 500, None))
        for error, expected, code in cases:
            with self.subTest(error=type(error).__name__, code=code):
                with mock.patch.object(ui, 'api_response', side_effect=error):
                    status, result = self.post('/api/editor/run', self.payload('/api/editor/run'))
                self.assertEqual(status, expected)
                if code:
                    self.assertEqual(result['code'], code)
                if expected == 500:
                    self.assertNotIn('private implementation detail', json.dumps(result))

    def test_unknown_post_path_is_404_without_dispatch(self):
        with mock.patch.object(ui, 'api_response') as dispatch:
            status, result = self.post('/api/live/not-a-write', {})
        self.assertEqual((status, result), (404, {'error': 'Not found.'}))
        dispatch.assert_not_called()

    def test_connector_errors_are_400_and_redacted_by_each_real_write_route(self):
        secret = self.connection['password']
        for path in WRITES:
            with self.subTest(path=path):
                payload = self.reviewed_payload(path)
                payload['confirmed'] = True
                if path == '/api/editor/run':
                    status, asked = self.post(path, self.payload(path))
                    self.assertEqual((status, asked['status']), (200, 'needs_confirmation'))
                    payload['confirm_token'] = asked['confirmation']['token']
                error = ConnectorError('ERROR 1045: Access denied using password ' + secret)
                with mock.patch('myflames.connector.MySQLConnection', side_effect=error) as connect, \
                        mock.patch.object(accounts_api, 'MySQLConnection', side_effect=error) as account, \
                        mock.patch.object(lifecycle, 'dump_job', side_effect=error) as dump, \
                        mock.patch.object(editor_session, '_open_session', side_effect=error) as editor:
                    status, result = self.post(path, payload)
                self.assertEqual(status, 400)
                self.assertIn('ERROR 1045', result['error'])
                self.assertIn('[redacted]', result['error'])
                self.assertNotIn(secret, json.dumps(result))
                self.assertEqual(sum(g.call_count for g in (connect, account, dump, editor)), 1)
                self.assertNotIn(secret, json.dumps(self.server.store.audit_records()))


if __name__ == '__main__':
    unittest.main()
