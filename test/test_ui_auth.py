"""The public shell has no authority; only a launch secret creates a session."""
import concurrent.futures
import http.client
import json
import os
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import urlsplit

from myflames.ui import WorkspaceServer


class TestWorkspaceAuthentication(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = mock.patch.dict(os.environ, {'HOME': temp.name, 'USERPROFILE': temp.name,
                                          'MYFLAMES_SECRET_BACKEND': 'vault'})
        env.start()
        self.addCleanup(env.stop)
        self.server = WorkspaceServer(store_path=os.path.join(temp.name, 'workspace.sqlite3'))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path='/api/auth/session', payload=None, cookie='', token='', extra=None, method='POST'):
        headers = {'Content-Type': 'application/json', 'Origin': self.server.origin,
                   'X-Myflames-Bootstrap': '1', 'Cookie': cookie, 'X-Myflames-Token': token}
        headers.update(extra or {})
        headers = {k: v for k, v in headers.items() if v is not None}
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=10)
        try:
            conn.request(method, path, json.dumps(payload or {}) if method == 'POST' else None, headers)
            result = conn.getresponse()
            return result.status, result.read(), dict(result.getheaders())
        finally:
            conn.close()

    def bootstrap(self):
        secret = urlsplit(self.server.launch_url).fragment.split('=', 1)[1]
        status, body, headers = self.request(payload={'secret': secret})
        self.assertEqual(status, 200)
        return secret, json.loads(body)['token'], headers['Set-Cookie']

    def test_public_shell_and_unauthenticated_requests_have_no_authority(self):
        status, body, _ = self.request('/', method='GET')
        self.assertEqual(status, 200)
        for secret in (self.server.token, self.server.bootstrap_secret, self.server.session_secret):
            self.assertNotIn(secret.encode(), body)
        self.assertNotIn(b'myflames-token', body)
        for path in ('/api/auth/session', '/api/session/load', '/api/profiles/list'):
            self.assertEqual(self.request(path)[0], 403)
        # Even knowledge of the CSRF header cannot replace the cookie.
        self.assertEqual(self.request('/api/session/load', token=self.server.token)[0], 403)

    def test_one_time_exchange_refresh_and_csrf(self):
        secret, token, cookie = self.bootstrap()
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        self.assertIn('Path=/api/', cookie)
        self.assertNotIn('Domain=', cookie)
        cookie = cookie.split(';', 1)[0]
        self.assertIsNone(self.server.bootstrap_secret)
        self.assertEqual(self.request(payload={'secret': secret})[0], 403)
        self.assertEqual(self.request(cookie=cookie)[0], 403)
        self.assertEqual(self.request(cookie=cookie, token='wrong')[0], 403)
        self.assertEqual(self.request(cookie=cookie, token=token)[0], 200)
        self.assertEqual(self.request(payload={'secret': secret}, cookie=cookie, token=token)[0], 200)
        self.assertEqual(self.request('/api/session/load', cookie=cookie)[0], 403)
        self.assertEqual(self.request('/api/session/load', cookie=cookie, token=token)[0], 200)
        for extra in ({'Origin': None}, {'Origin': 'null'}, {'Origin': self.server.origin + '9'},
                      {'Host': 'evil.example'}, {'X-Myflames-Bootstrap': None}):
            self.assertEqual(self.request(cookie=cookie, extra=extra)[0], 403)
        for extra in ({'Origin': None}, {'Origin': 'https://example.com'}, {'X-Myflames-Token': ''}):
            self.assertEqual(self.request('/api/session/load', cookie=cookie, token=token, extra=extra)[0], 403)

    def test_atomic_exchange_accepts_only_one_browser(self):
        secret = self.server.bootstrap_secret
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            answers = list(pool.map(lambda _: self.request(payload={'secret': secret}), range(4)))
        self.assertEqual([answer[0] for answer in answers].count(200), 1)
        self.assertEqual([answer[0] for answer in answers].count(403), 3)
        self.assertEqual(sum('Set-Cookie' in answer[2] for answer in answers), 1)

    def test_expired_and_wrong_secrets_never_issue_cookie(self):
        for value in ('wrong', '\N{SNOWMAN}', '\ud800', '', 12, {}, []):
            status, _, headers = self.request(payload={'secret': value})
            self.assertIn(status, (400, 403))
            self.assertNotIn('Set-Cookie', headers)
        self.server.bootstrap_deadline = 0
        self.assertEqual(self.request(payload={'secret': self.server.bootstrap_secret})[0], 403)

    def test_instances_do_not_share_cookie_names_or_credentials(self):
        _, token, cookie = self.bootstrap()
        second = WorkspaceServer(store_path=self.server.store.path)
        try:
            self.assertNotEqual(self.server.cookie_name, second.cookie_name)
            self.assertNotEqual(self.server.token, second.token)
            self.assertNotEqual(self.server.session_secret, second.session_secret)
            # A restart/new instance cannot accept the previous instance cookie.
            old_name, old_secret = self.server.cookie_name, self.server.session_secret
            self.server.cookie_name, self.server.session_secret = second.cookie_name, second.session_secret
            self.assertEqual(self.request('/api/session/load', cookie=cookie, token=token)[0], 403)
            self.server.cookie_name, self.server.session_secret = old_name, old_secret
        finally:
            second.server_close()


class TestWorkspaceRequestLimits(unittest.TestCase):
    def test_worker_permit_is_released_after_success_failure_and_start_failure(self):
        from http.server import ThreadingHTTPServer
        server = object.__new__(WorkspaceServer)
        server._worker_slots = threading.BoundedSemaphore(1)
        for failure in (None, RuntimeError('handler failed')):
            server._worker_slots.acquire()
            with mock.patch.object(ThreadingHTTPServer, 'process_request_thread', side_effect=failure):
                if failure:
                    with self.assertRaises(RuntimeError):
                        server.process_request_thread(mock.Mock(), ('127.0.0.1', 1))
                else:
                    server.process_request_thread(mock.Mock(), ('127.0.0.1', 1))
            self.assertTrue(server._worker_slots.acquire(blocking=False))
            server._worker_slots.release()
        with mock.patch.object(ThreadingHTTPServer, 'process_request', side_effect=RuntimeError('no threads')):
            with self.assertRaises(RuntimeError):
                server.process_request(mock.Mock(), ('127.0.0.1', 1))
        self.assertTrue(server._worker_slots.acquire(blocking=False))

    def test_worker_cap_refuses_without_starting_a_thread_and_closes_socket(self):
        from http.server import ThreadingHTTPServer
        server = object.__new__(WorkspaceServer)
        server._worker_slots = threading.BoundedSemaphore(1)
        server._worker_slots.acquire()
        for failure in (None, OSError('peer closed')):
            request = mock.Mock()
            request.sendall.side_effect = failure
            with mock.patch.object(ThreadingHTTPServer, 'process_request') as start, \
                    mock.patch.object(server, 'shutdown_request') as close:
                server.process_request(request, ('127.0.0.1', 1))
            start.assert_not_called()
            close.assert_called_once_with(request)
            self.assertIn(b'503 Service Unavailable', request.sendall.call_args[0][0])
            self.assertFalse(server._worker_slots.acquire(blocking=False))

    def test_body_deadline_is_total_despite_continuous_progress(self):
        from myflames.ui import WorkspaceHandler
        handler = object.__new__(WorkspaceHandler)
        handler.connection = mock.Mock()
        handler.rfile = mock.Mock()
        handler.rfile.read1.return_value = b'a'
        with mock.patch('myflames.ui.time.monotonic', side_effect=[0, 0, 8, 16]):
            with self.assertRaisesRegex(TimeoutError, 'deadline'):
                handler._read_body(3)
        self.assertEqual(handler.rfile.read1.call_count, 2)
        self.assertEqual(handler.connection.settimeout.call_args_list, [mock.call(15), mock.call(7)])
        handler.rfile.read1.return_value = b'abc'
        with mock.patch('myflames.ui.time.monotonic', side_effect=[0, 0, 16]):
            with self.assertRaises(TimeoutError):
                handler._read_body(3)

    def test_body_read_accepts_fragments_and_refuses_truncation(self):
        from myflames.ui import WorkspaceHandler
        handler = object.__new__(WorkspaceHandler)
        handler.connection = mock.Mock()
        handler.rfile = mock.Mock()
        handler.rfile.read1.side_effect = [b'a', b'bc']
        with mock.patch('myflames.ui.time.monotonic', return_value=0):
            self.assertEqual(handler._read_body(3), b'abc')
        handler.rfile.read1.side_effect = [b'a', b'']
        with self.assertRaisesRegex(ValueError, 'Incomplete'):
            handler._read_body(3)
