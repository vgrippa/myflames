"""The workspace server's listen backlog and its 503 overload answer."""
import http.client
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from myflames.ui import WorkspaceServer


class TestWorkspaceRefusalDrain(unittest.TestCase):
    """Admission stays bounded even when refused clients never finish sending."""

    def server(self):
        server = object.__new__(WorkspaceServer)
        server._worker_slots = threading.BoundedSemaphore(1)
        server._worker_slots.acquire()
        server._refusal_drain_slots = threading.BoundedSemaphore(server.MAX_REFUSAL_DRAINS)
        return server

    def assert_drain_slots_returned(self, server):
        for _ in range(server.MAX_REFUSAL_DRAINS):
            self.assertTrue(server._refusal_drain_slots.acquire(blocking=False))
        self.assertFalse(server._refusal_drain_slots.acquire(blocking=False))

    def test_drain_pool_caps_threads_and_closes_overflow_immediately(self):
        server = self.server()
        release = threading.Event()
        drains, requests = [], []
        real_thread = threading.Thread

        def make_thread(**kwargs):
            thread = real_thread(**kwargs)
            drains.append(thread)
            return thread

        with mock.patch('myflames.ui.threading.Thread', side_effect=make_thread) as spawn, \
                mock.patch.object(server, 'shutdown_request') as close:
            try:
                for _ in range(server.MAX_REFUSAL_DRAINS):
                    entered = threading.Event()
                    request = mock.Mock()

                    def receive(size, entered=entered):
                        entered.set()
                        release.wait(5)
                        return b''

                    request.recv.side_effect = receive
                    requests.append(request)
                    server.process_request(request, ('127.0.0.1', 1))
                    self.assertTrue(entered.wait(1), 'refused socket did not enter its drain')
                    self.assertEqual(request.method_calls[:3], [
                        mock.call.settimeout(0),
                        mock.call.sendall(b'HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n'),
                        mock.call.shutdown(socket.SHUT_WR)])
                close.assert_not_called()
                self.assertEqual(spawn.call_count, server.MAX_REFUSAL_DRAINS)
                self.assertTrue(all(thread.daemon for thread in drains))
                for _ in range(16):
                    overflow = mock.Mock()
                    server.process_request(overflow, ('127.0.0.1', 1))
                    close.assert_called_with(overflow)
                    overflow.recv.assert_not_called()
                self.assertEqual(close.call_count, 16)
                self.assertEqual(spawn.call_count, server.MAX_REFUSAL_DRAINS)
                self.assertFalse(server._worker_slots.acquire(blocking=False))
            finally:
                release.set()
                for thread in drains:
                    thread.join(5)
                    self.assertFalse(thread.is_alive(), 'refusal drain did not stop')
            self.assertEqual(close.call_count, 16 + server.MAX_REFUSAL_DRAINS)
        self.assert_drain_slots_returned(server)

    def test_drain_deadline_is_total_despite_continuous_bytes(self):
        server = self.server()
        server._refusal_drain_slots.acquire()
        request = mock.Mock()
        request.recv.return_value = b'x'
        with mock.patch('myflames.ui.time.monotonic', side_effect=[0, 0, 0.75, 1.5, 2.01]), \
                mock.patch.object(server, 'shutdown_request') as close:
            server._drain_refused_request(request)
        self.assertEqual(request.recv.call_count, 3)
        self.assertEqual(request.settimeout.call_args_list,
                         [mock.call(2), mock.call(1.25), mock.call(0.5)])
        close.assert_called_once_with(request)
        self.assert_drain_slots_returned(server)

    def test_drain_byte_cap_bounds_reads_without_waiting_for_eof(self):
        server = self.server()
        server.REFUSAL_DRAIN_BYTES = 65536 + 7
        server._refusal_drain_slots.acquire()
        request = mock.Mock()
        request.recv.side_effect = lambda size: b'x' * size
        with mock.patch('myflames.ui.time.monotonic', return_value=0), \
                mock.patch.object(server, 'shutdown_request') as close:
            server._drain_refused_request(request)
        self.assertEqual(request.recv.call_args_list, [mock.call(65536), mock.call(7)])
        close.assert_called_once_with(request)
        self.assert_drain_slots_returned(server)

    def test_drain_read_errors_close_and_return_the_slot(self):
        for error in (socket.timeout('slow client'), OSError('peer closed')):
            with self.subTest(error=error):
                server = self.server()
                server._refusal_drain_slots.acquire()
                request = mock.Mock()
                request.recv.side_effect = error
                with mock.patch.object(server, 'shutdown_request') as close:
                    server._drain_refused_request(request)
                close.assert_called_once_with(request)
                self.assert_drain_slots_returned(server)

    def test_nonblocking_send_failure_does_not_start_a_drain(self):
        server = self.server()
        request = mock.Mock()
        request.sendall.side_effect = BlockingIOError('client is not reading')
        with mock.patch('myflames.ui.threading.Thread') as spawn, \
                mock.patch.object(server, 'shutdown_request') as close:
            server.process_request(request, ('127.0.0.1', 1))
        request.settimeout.assert_called_once_with(0)
        request.recv.assert_not_called()
        spawn.assert_not_called()
        close.assert_called_once_with(request)
        self.assert_drain_slots_returned(server)

    def test_drain_start_failure_closes_and_returns_the_slot(self):
        server = self.server()
        request = mock.Mock()
        with mock.patch('myflames.ui.threading.Thread') as spawn, \
                mock.patch.object(server, 'shutdown_request') as close:
            spawn.return_value.start.side_effect = RuntimeError('no threads')
            with self.assertRaisesRegex(RuntimeError, 'no threads'):
                server.process_request(request, ('127.0.0.1', 1))
        request.recv.assert_not_called()
        close.assert_called_once_with(request)
        self.assert_drain_slots_returned(server)


class TestWorkspaceBacklog(unittest.TestCase):
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

    def hold_workers(self):
        held = 0
        for _ in range(self.server.MAX_REQUEST_WORKERS):
            if self.server._worker_slots.acquire(blocking=False):
                held += 1
        self.assertEqual(held, self.server.MAX_REQUEST_WORKERS)
        self.addCleanup(self._release_workers)

    def _release_workers(self):
        for _ in range(self.server.MAX_REQUEST_WORKERS):
            self.server._worker_slots.release()

    def test_listen_backlog_absorbs_a_burst(self):
        # socketserver's default backlog of 5 drops connections that arrive
        # while the workers are busy; the browser reports those as network
        # errors. The workspace listens with room for its bursts.
        self.assertGreaterEqual(WorkspaceServer.request_queue_size, 64)
        self.hold_workers()
        count = WorkspaceServer.MAX_REQUEST_WORKERS + 32
        results = []

        def ask():
            conn = http.client.HTTPConnection(*self.server.server_address, timeout=10)
            try:
                conn.request('GET', '/')
                results.append(conn.getresponse().status)
            except OSError as exc:
                results.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=ask) for _ in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results, [503] * count)

    def test_overload_answer_is_a_503_not_a_reset(self):
        # A POST whose body is still in flight must read the 503 response;
        # closing the socket with unread request bytes answers RST instead.
        self.hold_workers()
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=10)
        try:
            conn.request('POST', '/api/session/load', json.dumps({'x': 'y' * (2 * 1024 * 1024)}),
                         {'Content-Type': 'application/json'})
            response = conn.getresponse()
            self.assertEqual(response.status, 503)
            self.assertEqual(response.read(), b'')
        finally:
            conn.close()

    def test_slow_refused_body_does_not_delay_the_next_accept(self):
        self.hold_workers()
        slow = socket.create_connection(self.server.server_address, timeout=1)
        stop, sending = threading.Event(), threading.Event()
        response = None

        def trickle():
            while not stop.wait(0.01):
                try:
                    slow.sendall(b'x')
                    sending.set()
                except OSError:
                    return

        sender = threading.Thread(target=trickle, daemon=True)
        try:
            slow.sendall(b'POST /api/session/load HTTP/1.1\r\nHost: localhost\r\n'
                         b'Content-Length: 8388608\r\n\r\n')
            sender.start()
            response = http.client.HTTPResponse(slow)
            response.begin()
            self.assertEqual(response.status, 503)
            self.assertEqual(response.read(), b'')
            self.assertTrue(sending.wait(1), 'slow client did not start sending')
            next_conn = http.client.HTTPConnection(*self.server.server_address, timeout=0.75)
            started = time.monotonic()
            try:
                next_conn.request('GET', '/')
                answer = next_conn.getresponse()
                self.assertEqual(answer.status, 503)
                self.assertEqual(answer.read(), b'')
                self.assertLess(time.monotonic() - started, 0.75)
                self.assertTrue(sender.is_alive(), 'slow client stopped before the next accept')
            finally:
                next_conn.close()
        finally:
            stop.set()
            if sender.ident is not None:
                sender.join(2)
                self.assertFalse(sender.is_alive(), 'slow client did not stop')
            if response is not None:
                response.close()
            slow.close()


if __name__ == '__main__':
    unittest.main()
