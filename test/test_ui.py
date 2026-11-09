"""Local workspace API, isolation, and lifecycle regressions."""
import http.client
import json
import os
import tempfile
from pathlib import Path
import re
import socket
import threading
import unittest
from unittest import mock

from myflames.parser import parse_explain, analyze_plan
from myflames.output_sidecar import build_sidecar
from myflames.output_compare_sidecar import build_compare_sidecar
from myflames.ui import api_response, WorkspaceHandler, WorkspaceServer, MAX_BODY, VIEWS

TEST_DIR = Path(__file__).resolve().parent
PLAN = (TEST_DIR / 'mysql-explain-json-sample.json').read_text(encoding="utf-8")
MARIA = (TEST_DIR / 'mariadb-explain-join.json').read_text(encoding="utf-8")


class TestBackendReadLimits(unittest.TestCase):
    """HTTP projections preserve the collectors' paging and opt-in limits."""

    def call(self, path, payload, target):
        from myflames import ui
        with mock.patch('myflames.connector.MySQLConnection') as factory, mock.patch(target) as collect:
            collect.return_value = {'result': 'read'}
            result = ui.api_response(path, dict(payload, connection={'host': 'localhost'}), workspace=object())
            self.assertEqual(result, collect.return_value)
            self.assertEqual(collect.call_args[0][0], factory.return_value.__enter__.return_value)
            return collect.call_args

    def test_session_history_route_is_bounded_and_fills_saved_password(self):
        from myflames import ui
        workspace = mock.Mock()
        workspace.store.connection_with_saved_password.side_effect = lambda c: dict(c, password='private-fixture')
        with mock.patch('myflames.connector.MySQLConnection') as factory, \
                mock.patch('myflames.sessions.sessions_history', return_value={'id': 42, 'statements': []}) as collect:
            result = api_response('/api/live/sessions/history',
                                  {'connection': {'profile_id': 'saved', 'host': 'localhost'}, 'id': 42}, workspace)
        self.assertEqual(result, {'id': 42, 'statements': []})
        self.assertEqual(collect.call_args[0][1], 42)
        self.assertEqual(factory.call_args[1]['query_timeout'], 5)
        self.assertEqual(factory.call_args[1]['password'], 'private-fixture')
        self.assertIn('/api/live/sessions/history', ui._SERVER_VIEW_PATHS)
        import inspect
        self.assertIn('"/api/live/sessions/history"', inspect.getsource(WorkspaceHandler.do_POST))

    def test_session_history_rejects_sql_in_id_before_connection(self):
        with mock.patch('myflames.connector.MySQLConnection') as factory:
            with self.assertRaises(ValueError):
                api_response('/api/live/sessions/history', {'connection': {}, 'id': '1; KILL 2'})
            factory.assert_not_called()

    def test_catalog_prefix_and_keyset_are_forwarded_literally(self):
        args, kwargs = self.call('/api/live/catalog', {'schema': 'shop', 'prefix': 'a%_', 'after': 'a%_Z'},
                                 'myflames.schema_browser.catalog_report')
        self.assertEqual(args[1:], ('shop', None))
        self.assertEqual(kwargs, {'prefix': 'a%_', 'after': 'a%_Z', 'stats': False, 'group': None})
        _, kwargs = self.call('/api/live/catalog', {'schema': 'shop'}, 'myflames.schema_browser.catalog_report')
        self.assertEqual(kwargs, {'prefix': None, 'after': None, 'stats': False, 'group': None})
        # The schema browser's Routines, Triggers and Events folders pass a group.
        _, kwargs = self.call('/api/live/catalog', {'schema': 'shop', 'group': 'triggers'},
                              'myflames.schema_browser.catalog_report')
        self.assertEqual(kwargs['group'], 'triggers')
        # Maintenance > Dump asks for engine, rows and size; only a literal true does.
        for value, expected in ((True, True), ('true', False), (1, False), (None, False)):
            _, kwargs = self.call('/api/live/catalog', {'schema': 'shop', 'stats': value},
                                  'myflames.schema_browser.catalog_report')
            self.assertIs(kwargs['stats'], expected, value)

    def test_catalog_rejects_invalid_paging_before_opening_a_connection(self):
        for key in ('prefix', 'after'):
            for value in ('x' * 65, ' ', 'a\x00b', 'a\nb', 'a\rb', True, 1, [], {}):
                with self.subTest(key=key, value=value), mock.patch('myflames.connector.MySQLConnection') as factory:
                    with self.assertRaisesRegex(ValueError, '1 to 64 characters'):
                        api_response('/api/live/catalog', {'connection': {'host': 'localhost'}, 'schema': 'shop',
                                                          key: value}, workspace=object())
                    factory.assert_not_called()

    def test_catalog_paging_accepts_the_module_boundary_and_empty_values(self):
        for value, expected in (('é' * 64, 'é' * 64), ('', None), (None, None)):
            with self.subTest(value=value):
                _, kwargs = self.call('/api/live/catalog', {'schema': 'shop', 'prefix': value, 'after': value},
                                      'myflames.schema_browser.catalog_report')
                self.assertEqual(kwargs, {'prefix': expected, 'after': expected, 'stats': False, 'group': None})

    def test_selected_and_global_schema_limits_require_a_boolean_override(self):
        for route, target in (('tables', 'myflames.schema_health.table_report'),
                              ('indexes', 'myflames.indexes.index_report')):
            for selected in ([], ['shop']):
                for override, limit in ((None, 2000), (False, 2000), (True, None), ('true', 2000), (1, 2000)):
                    with self.subTest(route=route, schemas=selected, override=override):
                        payload = {'schemas': selected}
                        if override is not None:
                            payload['all_schemas'] = override
                        args, kwargs = self.call('/api/live/' + route, payload, target)
                        self.assertEqual(kwargs['require_choice_above'], limit)
                        scope = args[1] if route == 'tables' else kwargs['schemas']
                        self.assertEqual(scope, tuple(selected) if selected else None)

    def test_workspace_table_and_index_lists_are_complete_by_default(self):
        for route, target in (('tables', 'myflames.schema_health.table_report'),
                              ('indexes', 'myflames.indexes.index_report')):
            for payload, expected in (({}, None), ({'limit': 17}, 17)):
                args, kwargs = self.call('/api/live/' + route, payload, target)
                self.assertEqual(args[3] if route == 'tables' else kwargs['limit'], expected)
                self.assertEqual(kwargs['require_choice_above'], 2000)

    def test_io_instance_override_and_scope(self):
        for override in (False, True):
            args, kwargs = self.call('/api/live/io', {'schema': 'shop', 'interval': 3, 'limit': 12,
                                                    'all_instances': override}, 'myflames.io_stats.io_report')
            self.assertEqual(args[1:], (3, 'shop', 12))
            self.assertEqual(kwargs, {'all_instances': override})
        _, kwargs = self.call('/api/live/io', {}, 'myflames.io_stats.io_report')
        self.assertEqual(kwargs, {'all_instances': False})

    def test_io_rejects_a_non_boolean_override_before_reading(self):
        with mock.patch('myflames.connector.MySQLConnection') as factory:
            for value in ('true', 1, None):
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'all_instances as true or false'):
                    api_response('/api/live/io', {'connection': {'host': 'localhost'}, 'all_instances': value},
                                 workspace=object())
            factory.return_value.__enter__.return_value.run.assert_not_called()

    def test_report_override_reaches_the_analysis(self):
        for override, limit in ((False, 2000), (True, None)):
            _, kwargs = self.call('/api/live/report', {'schemas': ['shop'], 'all_schemas': override},
                                  'myflames.report.analysis_report')
            self.assertEqual(kwargs, {'schemas': ('shop',), 'require_choice_above': limit})


class TestWorkspaceRequestLimits(unittest.TestCase):
    """Exercise admission and cleanup without opening sockets or starting threads."""

    def server(self):
        server = object.__new__(WorkspaceServer)
        server._worker_slots = threading.BoundedSemaphore(server.MAX_REQUEST_WORKERS)
        return server

    def test_handler_socket_has_timeout(self):
        self.assertEqual(WorkspaceHandler.timeout, 30)
        request = mock.Mock()
        # BaseHTTPRequestHandler's inherited setup applies the class timeout.
        handler = object.__new__(WorkspaceHandler)
        handler.request = request
        handler.setup()
        request.settimeout.assert_called_with(WorkspaceHandler.timeout)
        handler.rfile.close()
        handler.wfile.close()

    def test_header_deadline_expires_even_while_bytes_keep_arriving(self):
        import io
        from myflames import ui
        raw, connection = mock.Mock(), mock.Mock()
        def trickle(buffer):
            buffer[0] = ord('X')
            return 1
        raw.readinto.side_effect = trickle
        # Four reads arrive 12 seconds apart; an inactivity timeout would
        # never expire. The absolute 30-second header budget does expire.
        stream = io.BufferedReader(ui._HeaderDeadlineReader(raw, connection, lambda: 30))
        try:
            with mock.patch.object(ui.time, 'monotonic', side_effect=(0, 12, 24, 36)):
                with self.assertRaisesRegex(socket.timeout, 'headers timed out'):
                    stream.readline()
            self.assertEqual(raw.readinto.call_count, 3)
            self.assertEqual(connection.settimeout.call_args_list,
                             [mock.call(30), mock.call(18), mock.call(6)])
        finally:
            stream.close()
        raw.close.assert_called_once()

    def test_header_deadline_is_reset_per_request_and_disabled_before_endpoint_work(self):
        from http.server import BaseHTTPRequestHandler
        from myflames import ui
        handler = object.__new__(WorkspaceHandler)
        handler.connection = mock.Mock()
        def headers():
            self.assertEqual(handler._header_deadline, 130)
            with mock.patch.object(BaseHTTPRequestHandler, 'parse_request', return_value=True):
                self.assertTrue(handler.parse_request())
            self.assertIsNone(handler._header_deadline)
        with mock.patch.object(ui.time, 'monotonic', return_value=100), \
                mock.patch.object(BaseHTTPRequestHandler, 'handle_one_request', side_effect=headers):
            handler.handle_one_request()
            handler.handle_one_request()
        self.assertEqual(handler.connection.settimeout.call_args_list, [mock.call(30), mock.call(30)])

    def test_admission_is_bounded_and_rejected_sockets_are_closed(self):
        from http.server import ThreadingHTTPServer
        server = self.server()
        with mock.patch.object(ThreadingHTTPServer, 'process_request') as dispatch, \
                mock.patch.object(server, 'shutdown_request') as shutdown:
            for unused in range(server.MAX_REQUEST_WORKERS):
                server.process_request(mock.Mock(), ('127.0.0.1', 1234))
            self.assertEqual(dispatch.call_count, server.MAX_REQUEST_WORKERS)
            for error in (False, True):
                request = mock.Mock()
                if error:
                    request.sendall.side_effect = OSError('peer closed')
                server.process_request(request, ('127.0.0.1', 1234))
                shutdown.assert_called_with(request)
                request.settimeout.assert_called_with(1)
            self.assertEqual(dispatch.call_count, server.MAX_REQUEST_WORKERS)
        # Complete one admitted handler; the next request can now be admitted.
        with mock.patch.object(ThreadingHTTPServer, 'process_request_thread'):
            server.process_request_thread(mock.Mock(), ('127.0.0.1', 1234))
        with mock.patch.object(ThreadingHTTPServer, 'process_request') as dispatch:
            server.process_request(mock.Mock(), ('127.0.0.1', 1234))
            dispatch.assert_called_once()

    def test_worker_permit_is_returned_after_spawn_and_handler_errors(self):
        from http.server import ThreadingHTTPServer
        server = self.server()
        with mock.patch.object(ThreadingHTTPServer, 'process_request', side_effect=RuntimeError('spawn failed')):
            with self.assertRaisesRegex(RuntimeError, 'spawn failed'):
                server.process_request(mock.Mock(), ('127.0.0.1', 1234))
        with mock.patch.object(ThreadingHTTPServer, 'process_request'):
            server.process_request(mock.Mock(), ('127.0.0.1', 1234))
        with mock.patch.object(ThreadingHTTPServer, 'process_request_thread', side_effect=RuntimeError('handler failed')):
            with self.assertRaisesRegex(RuntimeError, 'handler failed'):
                server.process_request_thread(mock.Mock(), ('127.0.0.1', 1234))
        # Both failure paths restore every permit, with no double release.
        for unused in range(server.MAX_REQUEST_WORKERS):
            self.assertTrue(server._worker_slots.acquire(blocking=False))
        self.assertFalse(server._worker_slots.acquire(blocking=False))

    def test_idle_requests_never_start_more_than_the_worker_cap(self):
        server = self.server()
        release, full = threading.Event(), threading.Event()
        workers, lock = [], threading.Lock()
        def handle(request, address):
            with lock:
                workers.append(threading.current_thread())
                if len(workers) == server.MAX_REQUEST_WORKERS:
                    full.set()
            release.wait(5)
        requests = [mock.Mock() for unused in range(200)]
        with mock.patch.object(server, 'finish_request', side_effect=handle), \
                mock.patch.object(server, 'shutdown_request') as shutdown:
            try:
                for request in requests:
                    server.process_request(request, ('127.0.0.1', 1234))
                self.assertTrue(full.wait(5), 'admitted handlers did not start')
                self.assertEqual(len(workers), server.MAX_REQUEST_WORKERS)
                self.assertEqual(sum(request.sendall.called for request in requests),
                                 len(requests) - server.MAX_REQUEST_WORKERS)
            finally:
                release.set()
                for worker in workers:
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
            self.assertEqual(shutdown.call_count, len(requests))


class TestLiveJobOwnership(unittest.TestCase):
    def test_status_and_cancel_require_the_starting_connection(self):
        from types import SimpleNamespace
        from myflames.live import LiveJobs
        connection = {'host': 'localhost', 'port': 3306, 'user': 'app', 'ssl_mode': 'REQUIRED',
                      'ssl_ca': 'ca.pem', 'password': 'secret'}
        workspace = SimpleNamespace(jobs=LiveJobs())
        with mock.patch('myflames.live.threading.Thread'):
            started = api_response('/api/live/start', {'connection': connection, 'query': 'SELECT 1'}, workspace)
        job = workspace.jobs.jobs[started['id']]
        strangers = [None, dict(connection, host='other'), dict(connection, port=3307),
                     dict(connection, user='other'), dict(connection, profile_id='other'),
                     dict(connection, ssl_mode='DISABLED'), dict(connection, ssl_ca='other.pem')]
        for path in ('/api/live/status', '/api/live/cancel'):
            with self.assertRaisesRegex(ValueError, 'not found') as unknown:
                api_response(path, {'id': 'missing', 'connection': connection}, workspace)
            for other in strangers:
                with self.subTest(path=path, connection=other):
                    request = {'id': job.id}
                    if other is not None:
                        request['connection'] = other
                    with mock.patch.object(job, 'cancel', wraps=job.cancel) as cancel:
                        with self.assertRaises(ValueError) as caught:
                            api_response(path, request, workspace)
                        self.assertEqual(str(caught.exception), str(unknown.exception))
                        cancel.assert_not_called()
                    self.assertFalse(job.cancelled.is_set())
        # Policy, password and default schema can change without cutting off a job.
        same = dict(connection, password='changed', database='shop', type='development', read_only=True)
        self.assertEqual(api_response('/api/live/status', {'id': job.id, 'connection': same}, workspace),
                         job.snapshot())
        self.assertEqual(api_response('/api/live/cancel', {'id': job.id, 'connection': same}, workspace),
                         job.snapshot())
        self.assertTrue(job.cancelled.is_set())


class TestDatabaseClientAPI(unittest.TestCase):
    """Dispatch through the real handler without requiring a loopback socket."""

    def setUp(self):
        import types
        from myflames import connector, lifecycle
        from myflames.workspace_store import WorkspaceStore
        from myflames.editor_session import EditorSessions
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = mock.patch.dict(os.environ, {'HOME': self.temp.name, 'USERPROFILE': self.temp.name,
                                         'PATH': '', 'MYFLAMES_SECRET_BACKEND': 'vault'})
        env.start()
        self.addCleanup(env.stop)
        folders = mock.patch.object(lifecycle, 'COMMON_BIN_DIRS', ())
        folders.start()
        self.addCleanup(folders.stop)
        previous = connector.default_binary()
        connector.set_default_binary(None)
        self.addCleanup(connector.set_default_binary, previous)
        self.workspace = types.SimpleNamespace(store=WorkspaceStore(Path(self.temp.name) / 'workspace.sqlite3'),
                                               token='test-token', editor=EditorSessions())
        self.addCleanup(self.workspace.editor.close)

    def binary(self, body='print("mysql Ver 8.4.0")', name='mysql', folder='bin'):
        import sys
        # The real temp path (macOS /var is a link): a chosen client runs by its real path.
        directory = Path(os.path.realpath(self.temp.name)) / folder
        directory.mkdir(exist_ok=True)
        path = directory / name
        path.write_text('#!' + sys.executable + '\n' + body + '\n')
        path.chmod(0o700)
        return str(path)

    def post(self, path, payload, authenticated=True):
        from email.message import Message
        handler = object.__new__(WorkspaceHandler)
        handler.path, handler.server = path, self.workspace
        handler.headers = Message()
        body = json.dumps(payload).encode('utf-8')
        handler.headers['Content-Type'] = 'application/json'
        handler.headers['Content-Length'] = str(len(body))
        handler.headers['X-Myflames-Token'] = self.workspace.token
        handler._local_request = lambda: True
        handler._authenticated = lambda: authenticated
        handler._read_body = lambda size: body
        handler._send = mock.Mock()
        handler.do_POST()
        return handler._send.call_args[0][:2]

    def test_missing_client_code_through_direct_health_and_editor_paths(self):
        from myflames.connector import ClientMissingError
        for route, extra in (('/api/connection/test', {}), ('/api/live/health', {'interval': 1}),
                             ('/api/editor/open', {'tab_id': 'client-recovery'})):
            with self.subTest(route=route):
                status, body = self.post(route, dict(extra, connection={'host': 'localhost'}))
                self.assertEqual(status, 400, body)
                self.assertEqual(body['code'], 'client-missing')
                self.assertIn(ClientMissingError.MESSAGE, body['error'])
                self.assertNotIn('--mysql-binary', body['error'])
                self.assertEqual(body['error'], ClientMissingError.MESSAGE)
        self.assertEqual(self.workspace.editor.sessions, {})

    def test_client_missing_wrappers_return_only_the_typed_cause(self):
        from myflames import ui
        from myflames.connector import ClientMissingError, ConnectorError
        # myflames' redaction wrappers keep the typed cause; other types never inherit it.
        for error_type in (ConnectorError, ValueError, TypeError, KeyError, AttributeError, RecursionError,
                           OverflowError):
            with self.subTest(error_type=error_type.__name__):
                try:
                    try:
                        raise ClientMissingError('The chosen database client is unavailable.')
                    except ClientMissingError:
                        raise error_type('wrapper details must not reach the response') from None
                except error_type as error:
                    with mock.patch.object(ui, 'api_response', side_effect=error):
                        status, body = self.post('/api/connection/test', {})
                    if error_type in (ConnectorError, ValueError):
                        self.assertEqual((status, body), (400, {'error': 'The chosen database client is unavailable.',
                                                                'code': 'client-missing'}))
                    else:
                        self.assertNotIn('code', body)

    def test_unexpected_errors_with_client_missing_context_stay_generic_500(self):
        import io
        from myflames import ui
        from myflames.connector import ClientMissingError
        for error_type in (RuntimeError, OSError):
            with self.subTest(error_type=error_type.__name__):
                try:
                    try:
                        raise ClientMissingError()
                    except ClientMissingError:
                        raise error_type('internal detail with secret-password')
                except error_type as error:
                    stderr = io.StringIO()
                    with mock.patch.object(ui, 'api_response', side_effect=error), \
                            mock.patch.object(ui.sys, 'stderr', stderr):
                        status, body = self.post('/api/connection/test',
                                                 {'connection': {'password': 'secret-password'}})
                self.assertEqual(status, 500)
                self.assertEqual(body, {'error': ui._unexpected_error_message('/api/connection/test')})
                self.assertNotIn('internal detail', body['error'])
                self.assertNotIn('client-missing', body)
                self.assertIn('Traceback (most recent call last)', stderr.getvalue())
                self.assertIn(error_type.__name__ + ': internal detail with [redacted]', stderr.getvalue())
                self.assertNotIn('secret-password', stderr.getvalue())

    def test_other_refused_error_payloads_stay_unchanged(self):
        from myflames import ui
        from myflames.connector import ConnectorError, ClientMissingError
        from myflames.editor_session import EditorError
        for error, expected in ((ValueError('bad input'), {'error': 'Could not complete the request: bad input'}),
                                (ConnectorError('client error'), {'error': 'client error'}),
                                (ValueError(ClientMissingError.MESSAGE),
                                 {'error': 'Could not complete the request: ' + ClientMissingError.MESSAGE}),
                                (EditorError('busy message', 'busy'), {'error': 'busy message', 'code': 'busy'})):
            with self.subTest(error=error), mock.patch.object(ui, 'api_response', side_effect=error):
                status, body = self.post('/api/connection/test', {})
                self.assertEqual(body, expected)
                self.assertEqual(status, 409 if isinstance(error, EditorError) else 400)
        ssh = ConnectorError('SSH failed')
        ssh.reason = 'ssh-failed'
        with mock.patch.object(ui, 'api_response', side_effect=ssh):
            self.assertEqual(self.post('/api/connection/test', {}),
                             (400, {'error': 'SSH failed', 'code': 'ssh-failed'}))

    def test_settings_and_detection_without_clients_do_not_create_store(self):
        expected = {'schema_version': 'client-settings-1.0', 'path': None, 'active': None, 'detected': []}
        for route in ('/api/client/settings', '/api/client/detect'):
            self.assertEqual(self.post(route, {}), (200, expected))
        self.assertFalse(self.workspace.store.path.exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX executable fixture')
    def test_settings_round_trip_clear_and_automatic_active_client(self):
        from myflames import connector
        path = self.binary(folder='chosen')
        status, result = self.post('/api/client/settings', {'path': path})
        self.assertEqual(status, 200, result)
        self.assertEqual(result['path'], path)
        self.assertEqual(result['active'], {'path': path, 'version': 'mysql Ver 8.4.0', 'source': 'chosen'})
        self.assertEqual(self.post('/api/client/settings', {})[1], result)
        self.assertEqual(connector.default_binary(), path)
        self.assertEqual(connector.MySQLConnection(host='localhost')._resolve_binary(), path)
        automatic = self.binary(folder='automatic')
        with mock.patch.dict(os.environ, {'PATH': str(Path(automatic).parent)}):
            status, cleared = self.post('/api/client/settings', {'path': None})
        self.assertEqual(status, 200, cleared)
        self.assertIsNone(cleared['path'])
        self.assertIsNone(connector.default_binary())
        self.assertEqual(cleared['active']['path'], automatic)
        self.assertEqual(cleared['active']['source'], 'path')
        self.assertEqual(cleared['detected'][0]['path'], automatic)
        # An automatic PATH client in a shared folder is described as PATH finds it
        # (the connector uses it so), but never offered as a choice (security re-review R2).
        shared = self.binary(folder='shared')
        os.chmod(str(Path(shared).parent), 0o777)
        self.addCleanup(os.chmod, str(Path(shared).parent), 0o755)
        with mock.patch.dict(os.environ, {'PATH': str(Path(shared).parent)}):
            status, described = self.post('/api/client/settings', {})
        self.assertEqual(status, 200, described)
        self.assertNotIn('error', described)
        self.assertEqual((described['active']['path'], described['active']['source']), (shared, 'path'))
        self.assertEqual(described['detected'], [])

    @unittest.skipIf(os.name == 'nt', 'POSIX executable fixture')
    def test_every_invalid_choice_is_refused_without_replacing_saved_choice(self):
        from myflames import connector
        good = self.binary(folder='good')
        self.assertEqual(self.post('/api/client/settings', {'path': good})[0], 200)
        directory = Path(self.temp.name) / 'mysql'
        directory.mkdir()
        link = Path(self.temp.name) / 'mariadb'
        link.symlink_to(directory)
        bad = ['relative/mysql', str(Path(self.temp.name) / 'missing' / 'mysql'), str(directory), str(link),
               self.binary(name='not-a-client', folder='wrong'),
               self.binary(body='print("mysql"); raise SystemExit(1)', folder='nonzero'),
               self.binary(body='print("not a database client")', folder='banner'),
               self.binary(body='import time; time.sleep(30)', folder='timeout'), '', False, {}, 1]
        for candidate in bad:
            with self.subTest(candidate=candidate):
                status, error = self.post('/api/client/settings', {'path': candidate})
                self.assertEqual(status, 400, error)
                self.assertEqual(self.workspace.store.client_binary(), good)
                self.assertEqual(connector.default_binary(), good)

    @unittest.skipIf(os.name == 'nt', 'POSIX executable fixture')
    def test_changed_stored_client_is_reported_unavailable_and_can_be_replaced(self):
        from myflames import connector
        good = self.binary()
        self.assertEqual(self.post('/api/client/settings', {'path': good})[0], 200)
        conn = connector.MySQLConnection(host='localhost')
        self.assertEqual(conn._resolve_binary(), good)
        self.binary(body='raise SystemExit(1)')
        status, result = self.post('/api/client/settings', {})
        self.assertEqual(status, 200)
        self.assertIsNone(result['active'])
        self.assertEqual(result['path'], good)
        self.assertIn('unsuccessfully', result['error'])
        with self.assertRaises(connector.ClientMissingError):
            conn._resolve_binary()
        replacement = self.binary(folder='replacement')
        status, result = self.post('/api/client/settings', {'path': replacement})
        self.assertEqual(status, 200, result)
        self.assertEqual(conn._resolve_binary(), replacement)

    def test_settings_are_authenticated_and_detection_cannot_write(self):
        for route in ('/api/client/settings', '/api/client/detect'):
            self.assertEqual(self.post(route, {'path': '/bad/mysql'}, authenticated=False)[0], 403)
        self.assertEqual(self.post('/api/client/detect', {'path': None})[0], 400)
        self.assertEqual(self.post('/api/client/settings', {'binary': '/bad/mysql'})[0], 400)
        self.assertFalse(self.workspace.store.path.exists())

    def test_stored_broken_path_is_reported_and_not_used(self):
        from myflames import connector
        path = str(Path(self.temp.name) / 'missing' / 'mysql')
        self.workspace.store.set_client_binary(path)
        status, result = self.post('/api/client/settings', {})
        self.assertEqual(status, 200, result)
        self.assertEqual(result['path'], path)
        self.assertIsNone(result['active'])
        self.assertIn('regular file', result['error'])
        with self.assertRaises(connector.ClientMissingError):
            connector.MySQLConnection(host='localhost')._resolve_binary()
        self.assertIsNone(self.post('/api/client/settings', {'path': None})[1]['path'])

    @unittest.skipIf(os.name == 'nt', 'POSIX executable fixture')
    def test_server_start_restores_and_validates_choice_without_blocking_recovery(self):
        from myflames import connector, ui
        path = self.binary()
        self.workspace.store.set_client_binary(path)
        # Stop immediately before socket creation. Everything up to the
        # server bind is real; no worker thread or client session is started.
        for broken in (False, True):
            if broken:
                Path(path).unlink()
            connector.set_default_binary(None)
            server = object.__new__(WorkspaceServer)
            try:
                with mock.patch.object(ui.ThreadingHTTPServer, '__init__', side_effect=OSError('test bind boundary')):
                    with self.assertRaisesRegex(OSError, 'test bind boundary'):
                        server.__init__(store_path=self.workspace.store.path)
            finally:
                with mock.patch.object(ui.ThreadingHTTPServer, 'server_close'):
                    server.server_close()
            self.assertEqual(connector.default_binary(), path)
            if broken:
                with self.assertRaises(connector.ClientMissingError):
                    connector.MySQLConnection(host='localhost')._resolve_binary()
            else:
                self.assertEqual(connector.MySQLConnection(host='localhost')._resolve_binary(), path)


class TestWorkspaceAnalysis(unittest.TestCase):
    def test_analysis_uses_canonical_engine_and_node_ids(self):
        for raw, engine in ((PLAN, 'mysql'), (MARIA, 'mariadb')):
            with self.subTest(engine=engine):
                result = api_response('/api/analyze', {'plan': raw})
                root = parse_explain(raw)
                expected = build_sidecar(root, analyze_plan(root))
                for key in ('plan_summary', 'warnings', 'suggestions', 'plan_tree'):
                    self.assertEqual(result['analysis'][key], expected[key])
                self.assertEqual(result['analysis']['source']['engine'], engine)
                self.assertEqual(result['operators'][0]['node_id'], root['node_id'])
                self.assertEqual(result['operators'][0]['total_time_ms'], root['total_time'])
                self.assertEqual(len(result['operators']), expected['plan_summary']['operator_count'])

    def test_analysis_carries_full_tree_chart_data(self):
        from myflames.chart_data import build_chart_data
        from myflames.teach_hooks import build_teach_index_maps
        for raw, engine in ((PLAN, 'mysql'), (MARIA, 'mariadb')):
            with self.subTest(engine=engine):
                result = api_response('/api/analyze', {'plan': raw})
                data = result['chart_data']
                root = parse_explain(raw)
                self.assertEqual(data['schema_version'], 'chart-data-2.0')
                self.assertEqual(data['engine'], engine)
                # Full canonical tree in pre-order, same IDs as the operator
                # table; the browser applies focus/collapse as a projection.
                self.assertEqual([n['id'] for n in data['nodes']],
                                 [op['node_id'] for op in result['operators']])
                self.assertEqual(data['root_id'], root['node_id'])
                self.assertEqual(data['projection'], {
                    'focus_id': '', 'collapsed_ids': [], 'selected_id': '', 'metric': 'self_time'})
                by_folded = build_teach_index_maps(result['teach_hooks'])['by_folded_label']
                expected = build_chart_data(root, title='Query plan', engine=engine,
                                            teach_index_by_folded=by_folded, analysis=analyze_plan(root), compact=True)
                self.assertEqual(data, expected)
                if by_folded:
                    self.assertTrue(any(n['teach_index'] is not None for n in data['nodes']))
                # The JSON response must stay strict (no NaN/Infinity).
                json.dumps(result, allow_nan=False)

    def test_wrapped_mysql_preserves_query(self):
        result = api_response('/api/analyze', {'plan': json.dumps({'query_plan': json.loads(PLAN)})})
        self.assertIn('select', result['query'].lower())
        self.assertEqual(result['analysis']['query']['raw'], result['query'])

    def test_each_existing_view_renders_both_engines(self):
        for raw in (PLAN, MARIA):
            for view in VIEWS:
                with self.subTest(view=view):
                    html = api_response('/api/render', {'plan': raw, 'view': view})['html']
                    # Charts are drawn in the browser by the inlined bundle:
                    # the page carries the mount, its chart data, and the
                    # mount call instead of a server-rendered <svg>.
                    expected = 'workbench' if view == 'diagram' else view
                    self.assertIn('class="mfc-mount" data-mfc-view="%s"' % expected, html)
                    match = re.search(r'<script type="application/json" class="mfc-data">(.*?)</script>', html)
                    self.assertIsNotNone(match)
                    data = json.loads(match.group(1))
                    self.assertEqual(data['schema_version'], 'chart-data-1.0')
                    self.assertEqual(data['root_id'], parse_explain(raw)['node_id'])
                    self.assertIn('MyflamesCharts.mountAll(document)', html)
                    self.assertNotIn('%% n', html)
                    self.assertIn('<!DOCTYPE html>', html)

    def test_diagram_api_alias_keeps_the_shared_visual_explain_view(self):
        for raw in (PLAN, MARIA):
            with self.subTest(engine=raw[:40]):
                self.assertEqual(
                    api_response('/api/render', {'plan': raw, 'view': 'diagram'}),
                    api_response('/api/render', {'plan': raw, 'view': 'workbench'}))

    def test_exports_contain_canonical_analysis(self):
        html = api_response('/api/report', {'plan': PLAN, 'view': 'tree'})['html']
        self.assertIn('application/ld+json', html)
        self.assertIn('Export Analysis JSON', html)
        before = json.loads(PLAN)
        before['actual_last_row_ms'] *= 2
        before = json.dumps(before)
        result = api_response('/api/compare', {'before': before, 'after': PLAN})
        expected = build_compare_sidecar(before, PLAN)
        self.assertEqual(result['analysis']['summary'], expected['summary'])
        self.assertLess(result['analysis']['summary']['time_delta_ms'], 0)
        self.assertIn('<!DOCTYPE html>', result['html'])

    def test_malformed_and_unsupported_inputs(self):
        for raw in ('', '{}', '[]', 'null', '{bad', '{"query_plan":42}', '{"query_block":null}'):
            with self.subTest(raw=raw):
                with self.assertRaises((ValueError, TypeError, AttributeError)):
                    api_response('/api/analyze', {'plan': raw})
        with self.assertRaises(ValueError):
            api_response('/api/render', {'plan': PLAN, 'view': 'invalid'})

    def test_rejects_nonfinite_numbers_and_deep_metadata(self):
        plan = json.loads(PLAN)
        plan['actual_last_row_ms'] = float('nan')
        with self.assertRaisesRegex(ValueError, 'finite'):
            api_response('/api/analyze', {'plan': json.dumps(plan)})
        plan = json.loads(PLAN)
        nested = {}
        plan['metadata'] = nested
        for _ in range(90):
            nested['child'] = {}
            nested = nested['child']
        with self.assertRaisesRegex(ValueError, 'nested'):
            api_response('/api/analyze', {'plan': json.dumps(plan)})


class TestWorkspaceLaunchGuidance(unittest.TestCase):
    """Run the real GET handler without a listening socket or credentials."""

    def request(self, method, path, headers=None):
        from types import SimpleNamespace
        from myflames.ui import WorkspaceHandler
        self.server = SimpleNamespace(token='test-api-secret-unique',
                                      session_secret='test-cookie-secret-unique',
                                      bootstrap_secret='test-launch-secret-unique')
        handler = WorkspaceHandler.__new__(WorkspaceHandler)
        handler.path = path
        handler._local_request = lambda: True
        sent = []
        handler._send = lambda status, body, content_type: sent.append(
            (status, body.encode('utf-8'), {'Content-Type': content_type}))
        handler.do_GET()
        self.assertEqual(len(sent), 1)
        return sent[0]

    def test_public_shell_has_static_themed_launch_guidance_without_authority(self):
        # This body is useful with JavaScript disabled; authentication still
        # requires the one-use launch URL and never appears in the public HTML.
        status, body, headers = self.request('GET', '/', headers={
            'Cookie': '', 'X-Myflames-Token': '',
        })
        self.assertEqual(status, 200)
        self.assertIn('text/html', headers['Content-Type'])
        page = body.decode('utf-8')
        refusal = re.search(r'<main class="launch-refusal" role="alert">(.*?)</main>', page, re.S)
        self.assertIsNotNone(refusal)
        self.assertIn('Open your workspace', refusal.group(1))
        self.assertIn('Open the launch URL printed by myflames ui.', refusal.group(1))
        self.assertIn('restart myflames ui', refusal.group(1))
        self.assertIn('src="/assets/myflames-logo.png"', refusal.group(1))
        self.assertNotIn('<script', refusal.group(1))
        style = re.search(r'<style id="launch-refusal-style">(.*?)</style>', page, re.S)
        self.assertIsNotNone(style)
        for rule in ('prefers-color-scheme: dark', '--font-sans:', 'max-width: 480px',
                     'width: 100%', 'box-sizing: border-box', 'overflow-wrap: anywhere'):
            self.assertIn(rule, style.group(1))
        # The server fallback and React error share the same scoped rules.
        app_css = (TEST_DIR.parent / 'apps/workspace/src/style.css').read_text(encoding="utf-8")
        self.assertIn(style.group(1).strip(), app_css)
        for secret in (self.server.token, self.server.session_secret, self.server.bootstrap_secret):
            self.assertNotIn(secret, page)
        self.assertNotIn('myflames-token', page)


class TestAppUpdateRoutes(unittest.TestCase):
    """Exercise dispatch and persistence without opening a listening socket."""

    def setUp(self):
        from types import SimpleNamespace
        from myflames.workspace_store import WorkspaceStore
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = mock.patch.dict(os.environ, {'HOME': temp.name, 'USERPROFILE': temp.name,
                                          'MYFLAMES_SECRET_BACKEND': 'vault', 'MYFLAMES_UPDATES_DIR': ''})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        self.workspace = SimpleNamespace(store=WorkspaceStore(Path(temp.name) / 'store' / 'workspace.sqlite3'))
        net = mock.patch('myflames.app_update._opener', side_effect=AssertionError('network'))
        self.net = net.start()
        self.addCleanup(net.stop)
        browser = mock.patch('webbrowser.open', side_effect=AssertionError('browser'))
        browser.start()
        self.addCleanup(browser.stop)

    def test_update_status_reports_auto_check_and_stage_snapshots_without_network(self):
        staging = {'schema_version': 'app-update-stage-1.0', 'state': 'running', 'version': '99.0.0',
                   'received': 12, 'total': 24, 'reason': None}
        staged = {'version': '98.0.0', 'file': 'myflames-98.0.0-macos-arm64.zip', 'size': 24, 'sha256': 'a' * 64}
        with mock.patch('myflames.app_update.staging_state', return_value=staging), \
                mock.patch('myflames.app_update.staged_update', return_value=staged):
            for workspace in (None, self.workspace):
                status = api_response('/api/app/update/status', {}, workspace)
                self.assertIs(status['auto_check'], True)
                self.assertEqual(status['staging'], staging)
                self.assertEqual(status['staged'], staged)
                self.assertIsNone(status['last_check'])
        self.assertFalse(self.workspace.store.path.exists())
        self.net.assert_not_called()

    def test_update_status_names_running_lifecycle_operations(self):
        # Restart to update warns first: quitting stops dumps, restores and clones.
        from types import SimpleNamespace
        jobs = SimpleNamespace(active_operations=lambda: ['dump', 'clone'])
        workspace = SimpleNamespace(store=self.workspace.store, lifecycle=jobs)
        self.assertEqual(api_response('/api/app/update/status', {}, workspace)['active_operations'], ['dump', 'clone'])
        self.assertEqual(api_response('/api/app/update/status', {}, self.workspace)['active_operations'], [])
        self.assertEqual(api_response('/api/app/update/status', {})['active_operations'], [])
        self.net.assert_not_called()

    def test_update_settings_round_trip_and_strict_boolean(self):
        from myflames.workspace_store import WorkspaceStore
        expected = {'schema_version': 'app-update-settings-1.0', 'auto_check': True}
        self.assertEqual(api_response('/api/app/update/settings', {}, self.workspace), expected)
        self.assertFalse(self.workspace.store.path.exists())
        for enabled in (False, True):
            expected['auto_check'] = enabled
            self.assertEqual(api_response('/api/app/update/settings', {'auto_check': enabled}, self.workspace), expected)
            self.workspace.store = WorkspaceStore(self.workspace.store.path)
            self.assertEqual(api_response('/api/app/update/settings', {}, self.workspace), expected)
            self.assertIs(api_response('/api/app/update/status', {}, self.workspace)['auto_check'], enabled)
        for invalid in (None, 0, 1, 'false', [], {}):
            with self.subTest(value=invalid), self.assertRaisesRegex(ValueError, 'true or false'):
                api_response('/api/app/update/settings', {'auto_check': invalid}, self.workspace)
        with self.assertRaisesRegex(ValueError, 'running workspace'):
            api_response('/api/app/update/settings', {'auto_check': False})
        self.net.assert_not_called()

    def test_update_stage_dispatch_and_post_allowlist(self):
        from myflames.ui import _APP_UPDATE_PATHS
        state = {'schema_version': 'app-update-stage-1.0', 'state': 'running'}
        with mock.patch('myflames.app_update.stage_update', return_value=state) as stage:
            self.assertEqual(api_response('/api/app/update/stage', {}, self.workspace), state)
            stage.assert_called_once_with()
        for path in ('/api/app/update/stage', '/api/app/update/settings'):
            self.assertIn(path, _APP_UPDATE_PATHS)

    def test_update_post_authentication_and_json_dispatch_without_listening_socket(self):
        import io
        from email.message import Message
        self.workspace.origin = 'http://127.0.0.1:12345'
        self.workspace.token = 'update-api-token'
        self.workspace.cookie_name = 'session'
        self.workspace.session_secret = 'update-session-secret'

        def request(path, overrides):
            handler = object.__new__(WorkspaceHandler)
            handler.server, handler.command, handler.path = self.workspace, 'POST', path
            headers = {'Host': '127.0.0.1:12345', 'Origin': self.workspace.origin,
                       'Cookie': 'session=update-session-secret', 'X-Myflames-Token': self.workspace.token,
                       'Content-Type': 'application/json', 'Content-Length': '2'}
            headers.update(overrides)
            handler.headers = Message()
            for key, value in headers.items():
                handler.headers[key] = value
            handler.rfile, handler.connection = io.BytesIO(b'{}'), mock.Mock()
            sent = []
            handler._send = lambda status, body: sent.append((status, body))
            handler.do_POST()
            self.assertEqual(len(sent), 1)
            return sent[0]

        with mock.patch('myflames.app_update.stage_update', return_value={'state': 'running'}) as stage:
            for path in ('/api/app/update/stage', '/api/app/update/settings'):
                for headers in ({'Host': 'evil.test'}, {'Origin': 'https://evil.test'}, {'Origin': ''},
                                {'Cookie': ''}, {'X-Myflames-Token': 'wrong'}):
                    with self.subTest(path=path, headers=headers):
                        self.assertEqual(request(path, headers)[0], 403)
                self.assertEqual(request(path, {'Content-Type': 'text/plain'})[0], 415)
            stage.assert_not_called()
            self.assertEqual(request('/api/app/update/stage', {}), (200, {'state': 'running'}))
            self.assertEqual(request('/api/app/update/settings', {}),
                             (200, {'schema_version': 'app-update-settings-1.0', 'auto_check': True}))
        self.assertFalse(self.workspace.store.path.exists())


class TestWorkspaceHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, {'HOME': cls.home.name, 'USERPROFILE': cls.home.name,
                                              'MYFLAMES_SECRET_BACKEND': 'vault'})
        cls.env.start()
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        try:
            cls.server = WorkspaceServer()
            cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
            cls.thread.start()
        except Exception:
            cls.env.stop()
            cls.home.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.shutdown()
            cls.server.server_close()
            cls.thread.join(timeout=5)
        finally:
            cls.env.stop()
            cls.home.cleanup()

    def request(self, method, path, payload=None, headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=10)
        defaults = {'Content-Type': 'application/json', 'X-Myflames-Token': self.server.token,
                    'Origin': self.server.origin,
                    'Cookie': self.server.cookie_name + '=' + self.server.session_secret}
        defaults.update(headers or {})
        body = json.dumps(payload) if payload is not None else None
        try:
            conn.request(method, path, body=body, headers=defaults)
            response = conn.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            conn.close()

    def test_app_update_status_needs_the_session_and_reads_no_network(self):
        with mock.patch('myflames.app_update.urllib.request.build_opener', side_effect=AssertionError('network')) as net:
            status, body, _ = self.request('POST', '/api/app/update/status', {},
                                           headers={'Cookie': self.server.cookie_name + '=wrong'})
            self.assertEqual(status, 403)
            status, body, _ = self.request('POST', '/api/app/update/status', {}, headers={'X-Myflames-Token': 'wrong'})
            self.assertEqual(status, 403)
            status, body, _ = self.request('POST', '/api/app/update/status', {})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)['schema_version'], 'app-update-status-1.0')
            net.assert_not_called()
        with mock.patch.dict(os.environ, {'MYFLAMES_TEST_UPDATE': '99.0.0'}), \
                mock.patch.dict('myflames.app_update._CACHE', clear=True):
            status, body, _ = self.request('POST', '/api/app/update/check', {'force': True})
        self.assertEqual(status, 200)
        checked = json.loads(body)
        status, body, _ = self.request('POST', '/api/app/update/status', {})
        self.assertEqual(json.loads(body)['last_check'], checked['last_check'])
        self.assertEqual(checked['last_check']['latest_version'], '99.0.0')

    def test_app_update_stage_and_settings_require_session_authentication(self):
        with mock.patch('myflames.app_update.stage_update', return_value={'state': 'running'}) as stage:
            for path in ('/api/app/update/stage', '/api/app/update/settings'):
                for headers in ({'Cookie': self.server.cookie_name + '=wrong'}, {'X-Myflames-Token': 'wrong'},
                                {'Origin': 'https://example.com'}):
                    with self.subTest(path=path, headers=headers):
                        status, _, _ = self.request('POST', path, {}, headers=headers)
                        self.assertEqual(status, 403)
            stage.assert_not_called()
            status, body, _ = self.request('POST', '/api/app/update/stage', {})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {'state': 'running'})
        try:
            status, body, _ = self.request('POST', '/api/app/update/settings', {'auto_check': False})
            self.assertEqual(status, 200)
            self.assertIs(json.loads(body)['auto_check'], False)
            status, body, _ = self.request('POST', '/api/app/update/status', {})
            self.assertEqual(status, 200)
            result = json.loads(body)
            self.assertIs(result['auto_check'], False)
            self.assertIn('staging', result)
            self.assertIn('staged', result)
        finally:
            self.server.store.set_app_update_auto(True)

    def test_unexpected_error_is_neutral_for_live_reads_and_logged_to_stderr(self):
        import io
        from myflames import ui
        secret = 'hunter2-Pa55'
        cases = (
            ('/api/live/sessions', 'Could not read this from the server. Try Refresh or the CLI.'),
            ('/api/live/replication', 'Could not read this from the server. Try Refresh or the CLI.'),
            ('/api/analyze', 'Could not render this plan. Try another view or the CLI.'),
            ('/api/profiles/list', 'Could not complete the request. Try again or use the CLI.'),
        )
        for path, message in cases:
            with self.subTest(path=path):
                err = io.StringIO()
                with mock.patch.object(ui, 'api_response', side_effect=NameError('boom with ' + secret)), \
                        mock.patch.object(ui.sys, 'stderr', err):
                    status, body, _ = self.request('POST', path, {'connection': {'host': 'h', 'password': secret}})
                self.assertEqual(status, 500)
                self.assertEqual(json.loads(body), {'error': message})
                self.assertNotIn('boom', body.decode())
                logged = err.getvalue()
                self.assertIn('unexpected error in POST ' + path, logged)
                self.assertIn('Traceback (most recent call last)', logged)
                self.assertIn('NameError: boom with [redacted]', logged)
                self.assertNotIn(secret, logged)

    def test_unexpected_error_log_redacts_every_password_form(self):
        import io
        from myflames import ui
        saved = 'saved-Pa55"quote\\slash'
        typed = "typed-Pa55'quote"
        current = 'current-Pa55'
        profile = self.server.store.save('profile', {'name': 'redact', 'host': 'db.internal', 'port': 3306,
                                                     'user': 'app', 'ssl_mode': 'PREFERRED'},
                                         password=saved, remember=True)
        self.addCleanup(self.server.store.delete, 'profile', profile['id'])
        payload = {'connection': {'host': 'db.internal', 'port': 3306, 'user': 'app', 'ssl_mode': 'PREFERRED',
                                  'profile_id': profile['id']},
                   'other': {'password': typed}, 'current_password': current}
        err = io.StringIO()
        message = 'saved=%s typed=%r json=%s current=%s' % (saved, typed, json.dumps(saved), current)
        with mock.patch.object(ui, 'api_response', side_effect=RuntimeError(message)), \
                mock.patch.object(ui.sys, 'stderr', err):
            status, body, _ = self.request('POST', '/api/live/sessions', payload)
        self.assertEqual(status, 500)
        logged = err.getvalue()
        self.assertIn('RuntimeError: saved=[redacted] typed=', logged)
        for secret in (saved, typed, current, repr(saved)[1:-1], repr(typed)[1:-1], json.dumps(saved)[1:-1]):
            self.assertNotIn(secret, logged)
            self.assertNotIn(secret, body.decode())

    def test_packaged_index_and_bundles(self):
        status, body, headers = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertNotIn(self.server.token.encode(), body)
        self.assertNotIn(self.server.session_secret.encode(), body)
        self.assertNotIn(self.server.bootstrap_secret.encode(), body)
        self.assertNotIn(b'__MYFLAMES_TOKEN__', body)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        for asset in re.findall(r'(?:src|href)="(/assets/[^\"]+)"', body.decode()):
            status, content, _ = self.request('GET', asset)
            self.assertEqual(status, 200, asset)
            self.assertGreater(len(content), 0)

    def test_index_carries_package_version_for_about(self):
        # About myflames reads the version from this meta tag, not a constant
        # in the bundle, so it always matches the installed package.
        from myflames import __version__
        status, body, _ = self.request('GET', '/')
        self.assertEqual(status, 200)
        page = body.decode()
        self.assertNotIn('__MYFLAMES_VERSION__', page)
        match = re.search(r'<meta name="myflames-version" content="([^"]*)"', page)
        self.assertIsNotNone(match, 'the page has a myflames-version meta tag')
        self.assertEqual(match.group(1), __version__)
        self.assertRegex(match.group(1), r'^\d+\.\d+')

    def test_analyze_and_bad_input_return_json(self):
        status, body, _ = self.request('POST', '/api/analyze', {'plan': PLAN})
        self.assertEqual(status, 200)
        self.assertIn('analysis', json.loads(body))
        status, body, _ = self.request('POST', '/api/analyze', {'plan': 'invalid'})
        self.assertEqual(status, 400)
        self.assertIn('error', json.loads(body))

    def test_cross_origin_missing_token_and_rebinding_denied(self):
        for headers in ({'Origin': 'https://example.com'}, {'X-Myflames-Token': ''}, {'Host': 'evil.example'}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request('POST', '/api/analyze', {'plan': PLAN}, headers)[0], 403)
        self.assertEqual(self.request('GET', '/', headers={'Host': 'evil.example'})[0], 403)

    def test_teach_endpoints_use_the_local_session(self):
        status, body, _ = self.request('POST', '/api/teach/catalog', {})
        self.assertEqual(status, 200)
        self.assertIn('full_scan', json.loads(body)['curriculum'])
        status, body, _ = self.request('POST', '/api/teach/lesson', {'lesson': 'full_scan'})
        self.assertEqual(status, 200)
        self.assertIn('btn-play', json.loads(body)['html'])
        self.assertEqual(self.request('POST', '/api/teach/lesson', {'lesson': 'missing'})[0], 400)
        self.assertEqual(self.request('POST', '/api/teach/catalog', {}, {'X-Myflames-Token': ''})[0], 403)

    def test_teach_lesson_rejects_unavailable_views_over_http(self):
        for lesson in ('btree', 'full_scan'):
            for view in ('3d', '4d', '3D', '', None, [], {}, 3):
                with self.subTest(lesson=lesson, view=view):
                    status, body, _ = self.request('POST', '/api/teach/lesson',
                                                   {'lesson': lesson, 'view': view})
                    self.assertEqual(status, 400)
                    self.assertIn('Only the 2D lesson view is available.', json.loads(body)['error'])
        for payload in ({'lesson': 'btree'}, {'lesson': 'btree', 'view': '2d'}):
            with self.subTest(payload=payload):
                status, body, _ = self.request('POST', '/api/teach/lesson', payload)
                self.assertEqual(status, 200)
                self.assertIn('btn-play', json.loads(body)['html'])

    def test_limits_content_type_and_paths(self):
        self.assertEqual(self.request('POST', '/api/analyze', {}, {'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.request('POST', '/api/analyze', {}, {'Content-Length': str(MAX_BODY + 1)})[0], 413)
        for path in ('/../pyproject.toml', '/assets/../ui.py', '/assets/%2e%2e/ui.py', '/api/unknown'):
            self.assertEqual(self.request('GET', path)[0], 404)
        self.assertEqual(self.request('POST', '/api/unknown', {})[0], 404)


class TestWorkspaceTeach(unittest.TestCase):
    def test_catalog_tracks_registry_and_curriculum(self):
        from myflames.teach import LESSONS, CURRICULUM, LESSON_FAMILIES
        result = api_response('/api/teach/catalog', {})
        keys = [lesson['key'] for lesson in result['lessons']]
        self.assertEqual(set(keys), set(LESSONS))
        self.assertEqual(len(keys), len(LESSONS))
        self.assertEqual(result['curriculum'], [key for key in CURRICULUM if key in LESSONS])
        self.assertEqual(keys[:len(result['curriculum'])], result['curriculum'])
        self.assertEqual({family['key'] for family in result['families']}, set(LESSON_FAMILIES))
        for lesson in result['lessons']:
            self.assertEqual(lesson['title'], LESSONS[lesson['key']]['title'])
            self.assertEqual(lesson['summary'], LESSONS[lesson['key']]['summary'])

    def test_all_registered_lessons_render_without_plan_input(self):
        from myflames.teach import LESSONS, render_lesson
        for key in LESSONS:
            with self.subTest(lesson=key):
                html = api_response('/api/teach/lesson', {'lesson': key})['html']
                self.assertEqual(html, render_lesson(key))
                self.assertIn('<html', html.lower())
                self.assertIn('<script', html)

    def test_unknown_lesson_and_paths_are_rejected(self):
        for key in (None, '', '../ui.py', 'missing', [], 1):
            with self.subTest(lesson=key):
                with self.assertRaisesRegex(ValueError, 'Teach catalog'):
                    api_response('/api/teach/lesson', {'lesson': key})

    def test_catalog_contains_only_lesson_metadata(self):
        for lesson in api_response('/api/teach/catalog', {})['lessons']:
            with self.subTest(lesson=lesson['key']):
                self.assertEqual(set(lesson), {'key', 'title', 'summary', 'family'})

    def test_lesson_2d_view_is_the_default(self):
        from myflames.teach import render_lesson
        expected = render_lesson('btree')
        self.assertEqual(api_response('/api/teach/lesson', {'lesson': 'btree'})['html'], expected)
        self.assertEqual(api_response('/api/teach/lesson', {'lesson': 'btree', 'view': '2d'})['html'],
                         expected)
        self.assertEqual(api_response('/api/teach/lesson', {'lesson': 'full_scan', 'view': '2d'})['html'],
                         render_lesson('full_scan'))

    def test_unavailable_views_are_rejected(self):
        cases = [(key, view) for key in ('btree', 'full_scan')
                 for view in ('3d', '4d', '3D', '', None, [], {}, 3)]
        for key, view in cases:
            with self.subTest(lesson=key, view=view):
                with self.assertRaisesRegex(ValueError, 'Only the 2D lesson view is available'):
                    api_response('/api/teach/lesson', {'lesson': key, 'view': view})


class _FakeDB(object):
    """Stands in for MySQLConnection in connection/test; records the options."""

    def __init__(self, error=None, version='8.4.3', comment='MySQL Community Server - GPL'):
        self.error = error
        self.options = None
        self.version, self.comment = version, comment

    def __call__(self, **options):
        self.options = options
        return self

    def __enter__(self):
        if self.error:
            raise self.error
        return self

    def __exit__(self, *exc):
        return False

    def server_version(self):
        return self.version

    def server_version_comment(self):
        return self.comment

    def is_mariadb(self):
        return 'mariadb' in (self.version + self.comment).lower()

    def query_rows(self, sql):
        return [['shop']]


class _FakeJobs(object):
    def __init__(self):
        self.payloads = []

    def start(self, payload):
        self.payloads.append(payload)
        return {'id': 'job', 'state': 'queued'}


class TestProfilePasswordAPI(unittest.TestCase):
    """Remembered passwords: saved outside SQLite, filled per request by profile_id."""
    PASSWORD = 'remembered-Pa55'

    def setUp(self):
        import os
        import tempfile
        import types
        from unittest import mock
        from myflames.workspace_store import WorkspaceStore
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.dict(os.environ, {'HOME': self.temp.name, 'USERPROFILE': self.temp.name,
                                               'MYFLAMES_SECRET_BACKEND': 'vault'})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        self.db = Path(self.temp.name) / 'workspace.sqlite3'
        self.workspace = types.SimpleNamespace(store=WorkspaceStore(self.db), jobs=_FakeJobs())
        self.settings = {'name': 'prod', 'host': 'db.internal', 'port': 3306, 'user': 'app'}

    def call(self, path, payload):
        return api_response(path, payload, self.workspace)

    def test_clone_recipient_tls_is_checked_before_donor_secret_lookup(self):
        from myflames import lifecycle as lc, ui
        for donor_mode in (None, 'REQUIRED', 'VERIFY_CA', 'VERIFY_IDENTITY'):
            profile = self.workspace.store.save('profile', dict(self.settings, ssl_mode=donor_mode),
                                                password=self.PASSWORD, remember=True)
            donor = dict(self.settings, profile_id=profile['id'], ssl_mode=donor_mode)
            recipients = [{'host': '127.0.0.1', 'ssl_mode': mode} for mode in ('DISABLED', 'PREFERRED')]
            recipients.append({'host': 'recipient.internal'})
            for recipient in recipients:
                with self.subTest(donor_mode=donor_mode, recipient=recipient), \
                        mock.patch.object(self.workspace.store, 'connection_with_saved_password') as secret:
                    with self.assertRaisesRegex(lc.Refused, 'recipient'):
                        ui._lifecycle_donor({'donor': donor, 'connection': recipient}, self.workspace, [])
                    secret.assert_not_called()
            for mode in ('REQUIRED', 'VERIFY_CA', 'VERIFY_IDENTITY'):
                options = ui._lifecycle_donor({'donor': donor, 'connection': {
                    'host': 'recipient.internal', 'ssl_mode': mode}}, self.workspace, [])
                self.assertEqual(options['password'], self.PASSWORD)

    def test_clone_recipient_saved_explicit_non_tls_policy_is_checked(self):
        from myflames import lifecycle as lc, ui
        donor_profile = self.workspace.store.save('profile', dict(self.settings, ssl_mode='REQUIRED'),
                                                  password=self.PASSWORD, remember=True)
        donor = dict(self.settings, profile_id=donor_profile['id'], ssl_mode='REQUIRED')
        for mode in ('DISABLED', 'PREFERRED'):
            profile = self.workspace.store.save('profile', dict(self.settings, host='127.0.0.1', ssl_mode=mode))
            recipient = dict(self.settings, host='127.0.0.1', profile_id=profile['id'])
            with self.subTest(mode=mode), mock.patch.object(self.workspace.store, 'connection_with_saved_password') as secret:
                with self.assertRaisesRegex(lc.Refused, 'recipient.*TLS|TLS.*recipient'):
                    ui._lifecycle_donor({'donor': donor, 'connection': recipient}, self.workspace, [])
                secret.assert_not_called()

    def save(self):
        return self.call('/api/profiles/save', {'value': dict(self.settings, password='ignored'),
                                                'password': self.PASSWORD, 'remember_password': True})

    def test_profile_changes_stop_only_matching_replication_loops(self):
        from myflames import dbops, replication, ui
        profile = self.save()
        registry = ui._replication_loops(self.workspace)
        loops = []
        for profile_id in (profile['id'], 'other'):
            identity = dbops.connection_identity(dict(self.settings, profile_id=profile_id))
            loop = replication.SkipLoop(object(), '', [1062], 1)
            loops.append(loop)
            with mock.patch.object(loop, 'start'):
                registry.start(identity, loop)
        self.call('/api/profiles/save', {'id': profile['id'], 'value': dict(self.settings, read_only=True)})
        self.assertEqual(loops[0]._stop_requested, 'policy_changed')
        self.assertIsNone(loops[1]._stop_requested)
        # A real loop consumes the event on its next iteration and remains visible.
        loops[0].run()
        key = dbops.connection_identity(dict(self.settings, profile_id=profile['id'], read_only=True))
        self.assertEqual(registry.status(key, loops[0].job_id)['stop_reason'], 'policy_changed')
        self.assertEqual(registry.stop(key, loops[0].job_id)['state'], 'stopped')
        # Delete also stops a read-write profile; no profile lookup is needed for Stop.
        profile2 = self.workspace.store.save('profile', dict(self.settings, name='second'))
        identity2 = dbops.connection_identity(dict(self.settings, profile_id=profile2['id']))
        loop2 = replication.SkipLoop(object(), '', [1062], 1)
        with mock.patch.object(loop2, 'start'):
            registry.start(identity2, loop2)
        self.call('/api/profiles/delete', {'id': profile2['id']})
        self.assertEqual(loop2._stop_requested, 'policy_changed')
        loop2.run()
        self.assertEqual(registry.status(identity2, loop2.job_id)['stop_reason'], 'policy_changed')
        self.assertIsNone(loops[1]._stop_requested)
        registry.stop_all()

    def test_server_close_stops_and_joins_replication_loops(self):
        from myflames import replication
        server = object.__new__(WorkspaceServer)
        server.jobs, server.editor, server.lifecycle = mock.Mock(), mock.Mock(), mock.Mock()
        server.replication_loops = replication.SkipLoopRegistry()
        loop = replication.SkipLoop(object(), '', [1062], 1)
        with mock.patch.object(loop, 'start'):
            server.replication_loops.start('endpoint', loop)
        with mock.patch.object(loop, 'stop', wraps=loop.stop) as stop, \
                mock.patch.object(loop, 'join') as join, \
                mock.patch('http.server.ThreadingHTTPServer.server_close'):
            server.server_close()
        stop.assert_called_once_with()
        join.assert_called_once_with()
        self.assertTrue(loop._stop_event.is_set())

    def connect(self, profile_id, **changes):
        connection = dict(self.settings, profile_id=profile_id, password='')
        connection.update(changes)
        return connection

    def test_save_with_remember_and_list_never_return_the_password(self):
        saved = self.save()
        self.assertNotIn('password', saved)
        self.assertTrue(saved['has_saved_password'])
        self.assertEqual(saved['secret_backend_label'], 'encrypted file')
        listed = self.call('/api/profiles/list', {})
        self.assertEqual(listed['secret_store'], {'backend': 'vault', 'label': 'encrypted file', 'available': True})
        self.assertEqual(listed['items'], [saved])
        for blob in (json.dumps(saved), json.dumps(listed), self.db.read_bytes().decode('latin-1')):
            self.assertNotIn(self.PASSWORD, blob)
            self.assertNotIn('ignored', blob)
        with self.assertRaisesRegex(ValueError, 'true or false'):
            self.call('/api/profiles/save', {'value': self.settings, 'remember_password': 'yes'})

    def test_connection_test_preserves_version_comment_product_identity(self):
        """Use the real metadata cache; only client execution is doubled."""
        cases = (
            ('11.4.13', 'MariaDB Server', ('mariadb', 'mariadb', 'MariaDB')),
            ('11.4.13-MariaDB', 'MariaDB Server', ('mariadb', 'mariadb', 'MariaDB')),
            ('8.4.11', 'MySQL Community Server - GPL', ('mysql', 'mysql', 'MySQL')),
            ('8.4.11-11', 'Percona Server (GPL)', ('mysql', 'percona', 'Percona Server')),
            ('8.4.11-11.1', 'Percona XtraDB Cluster (GPL)', ('mysql', 'percona', 'Percona XtraDB Cluster')),
        )
        for version, comment, identity in cases:
            with self.subTest(version=version, comment=comment):
                answers = {'SELECT @@version': version + '\n',
                           'SELECT @@version_comment': comment + '\n',
                           'SHOW DATABASES': 'shop\n'}
                with mock.patch('myflames.connector.MySQLConnection.run',
                                side_effect=lambda sql, **kwargs: answers[sql]) as run:
                    result = self.call('/api/connection/test', {'connection': self.settings})
                self.assertEqual((result['engine'], result['flavor'], result['product']), identity)
                self.assertEqual(result['version'], version)
                self.assertEqual(result['schemas'], ['shop'])
                self.assertEqual(result['databases'], ['shop'])
                self.assertFalse(result['databases_truncated'])
                self.assertNotIn('databases_error', result)
                self.assertEqual(sum(call[0][0] == 'SELECT @@version_comment'
                                     for call in run.call_args_list), 1)

    def test_connection_test_databases_bounded_and_error_path(self):
        from unittest import mock
        from myflames.connector import ConnectorError
        # Bounded at 1,000 database names.
        many = '\n'.join('db_%04d' % i for i in range(1, 1006)) + '\n'
        answers = {
            'SELECT @@version': '8.4.11\n',
            'SELECT @@version_comment': 'MySQL Community Server - GPL\n',
            'SHOW DATABASES': many,
        }
        with mock.patch('myflames.connector.MySQLConnection.run',
                        side_effect=lambda sql, **kwargs: answers[sql]):
            result = self.call('/api/connection/test', {'connection': self.settings})
        self.assertEqual(len(result['databases']), 1000)
        self.assertEqual(result['databases'][0], 'db_0001')
        self.assertEqual(result['databases'][-1], 'db_1000')
        self.assertEqual(result['schemas'], result['databases'])
        self.assertTrue(result['databases_truncated'])
        self.assertNotIn('databases_error', result)

        # Error path: when SHOW DATABASES fails (e.g. privilege denied), the test
        # still succeeds and returns databases_error with any password redacted.
        profile_id = self.save()['id']
        def run_with_show_error(sql, **kwargs):
            if sql == 'SHOW DATABASES':
                raise ConnectorError('ERROR 1227 (42000): Access denied for ' + self.PASSWORD)
            return answers[sql]

        with mock.patch('myflames.connector.MySQLConnection.run', side_effect=run_with_show_error):
            result = self.call('/api/connection/test', {'connection': self.connect(profile_id)})
        self.assertEqual(result['version'], '8.4.11')
        self.assertEqual(result['databases'], [])
        self.assertEqual(result['schemas'], [])
        self.assertFalse(result['databases_truncated'])
        self.assertIn('Access denied', result['databases_error'])
        self.assertNotIn(self.PASSWORD, result['databases_error'])
        self.assertIn('[redacted]', result['databases_error'])

    def test_connection_endpoints_fill_the_saved_password(self):
        from unittest import mock
        from myflames import ui
        profile_id = self.save()['id']
        fake = _FakeDB()
        with mock.patch('myflames.connector.MySQLConnection', fake):
            result = self.call('/api/connection/test', {'connection': self.connect(profile_id)})
        self.assertEqual(result['version'], '8.4.3')
        self.assertEqual((result['engine'], result['flavor'], result['product']), ('mysql', 'mysql', 'MySQL'))
        self.assertEqual(fake.options['password'], self.PASSWORD)
        self.call('/api/live/start', {'connection': self.connect(profile_id), 'query': 'SELECT 1'})
        self.assertEqual(self.workspace.jobs.payloads[-1]['connection']['password'], self.PASSWORD)
        self.assertEqual(self.workspace.jobs.payloads[-1]['query'], 'SELECT 1')
        for path in ('/api/live/top', '/api/live/running', '/api/live/explain-connection'):
            with self.subTest(path=path), mock.patch.object(ui, '_discover_response',
                                                            side_effect=lambda p, payload: payload) as discover:
                payload = self.call(path, {'connection': self.connect(profile_id), 'limit': 5})
                self.assertEqual(payload['connection']['password'], self.PASSWORD)
                self.assertEqual(payload['limit'], 5)
                self.assertEqual(discover.call_count, 1)
        # A typed password wins for the session.
        with mock.patch('myflames.connector.MySQLConnection', fake):
            self.call('/api/connection/test', {'connection': self.connect(profile_id, password='typed')})
        self.assertEqual(fake.options['password'], 'typed')
        # The saved password is never sent to another host, port, or user.
        with self.assertRaisesRegex(ValueError, 'own host, port, user, and TLS settings'):
            self.call('/api/connection/test', {'connection': self.connect(profile_id, host='evil.example')})


    def test_connection_test_names_percona_server(self):
        """Percona names itself only in @@version_comment (read live on 8.4.11-11)."""
        from unittest import mock
        profile_id = self.save()['id']
        fake = _FakeDB(version='8.4.11-11', comment='Percona Server (GPL), Release 11, Revision 57878ff8')
        with mock.patch('myflames.connector.MySQLConnection', fake):
            result = self.call('/api/connection/test', {'connection': self.connect(profile_id)})
        self.assertEqual((result['version'], result['engine'], result['flavor'], result['product']),
                         ('8.4.11-11', 'mysql', 'percona', 'Percona Server'))
    def test_saved_password_binds_to_the_exact_tls_mode(self):
        # A profile saved without ssl_mode means "the client's own default",
        # which is not PREFERRED for every client (MariaDB 11.4+ verifies by
        # default), so an explicit PREFERRED never receives its password, and
        # a saved PREFERRED is never filled into a request that names none.
        from unittest import mock
        fake = _FakeDB()
        no_mode = self.save()['id']
        explicit = self.call('/api/profiles/save', {
            'value': dict(self.settings, name='explicit', ssl_mode='PREFERRED'),
            'password': self.PASSWORD, 'remember_password': True})['id']
        with mock.patch('myflames.connector.MySQLConnection', fake):
            for profile_id, mode in ((no_mode, ''), (no_mode, None), (explicit, 'PREFERRED')):
                with self.subTest(profile=profile_id, ssl_mode=mode):
                    self.call('/api/connection/test', {'connection': self.connect(profile_id, ssl_mode=mode)})
                    self.assertEqual(fake.options['password'], self.PASSWORD)
            for profile_id, mode in ((no_mode, 'PREFERRED'), (explicit, ''), (explicit, None), (explicit, 'REQUIRED')):
                with self.subTest(profile=profile_id, refused=mode):
                    with self.assertRaisesRegex(ValueError, 'own host, port, user, and TLS settings'):
                        self.call('/api/connection/test', {'connection': self.connect(profile_id, ssl_mode=mode)})

    def test_connection_errors_redact_the_saved_password(self):
        from unittest import mock
        from myflames.connector import ConnectorError
        profile_id = self.save()['id']
        fake = _FakeDB(ConnectorError('Access denied using password ' + self.PASSWORD))
        with mock.patch('myflames.connector.MySQLConnection', fake):
            with self.assertRaises(ConnectorError) as caught:
                self.call('/api/connection/test', {'connection': self.connect(profile_id)})
        self.assertNotIn(self.PASSWORD, str(caught.exception))
        self.assertIn('[redacted]', str(caught.exception))

    def test_forget_and_delete_remove_the_secret(self):
        from unittest import mock
        from test_secret_store import saved_vault_password
        profile_id = self.save()['id']
        forgotten = self.call('/api/profiles/forget-password', {'id': profile_id})
        self.assertFalse(forgotten['has_saved_password'])
        self.assertIsNone(saved_vault_password(profile_id))
        fake = _FakeDB()
        with mock.patch('myflames.connector.MySQLConnection', fake):
            self.call('/api/connection/test', {'connection': self.connect(profile_id)})
        self.assertNotIn('password', fake.options)
        self.save()
        self.call('/api/profiles/save', {'id': profile_id, 'value': self.settings, 'password': self.PASSWORD,
                                         'remember_password': True})
        self.assertEqual(saved_vault_password(profile_id), self.PASSWORD)
        self.assertEqual(self.call('/api/profiles/delete', {'id': profile_id}), {'deleted': True})
        self.assertIsNone(saved_vault_password(profile_id))
        with self.assertRaisesRegex(ValueError, 'no longer exists'):
            self.call('/api/connection/test', {'connection': self.connect(profile_id)})

    def test_http_round_trip_and_allowlist(self):
        import inspect
        from myflames.ui import WorkspaceHandler
        source = inspect.getsource(WorkspaceHandler.do_POST)
        self.assertIn('"/api/profiles/forget-password"', source)
        server = WorkspaceServer(store_path=str(self.db))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def post(path, payload):
            conn = http.client.HTTPConnection(*server.server_address, timeout=10)
            try:
                conn.request('POST', path, body=json.dumps(payload),
                             headers={'Content-Type': 'application/json', 'X-Myflames-Token': server.token,
                                      'Origin': server.origin,
                                      'Cookie': server.cookie_name + '=' + server.session_secret})
                response = conn.getresponse()
                return response.status, response.read().decode('utf-8')
            finally:
                conn.close()

        status, body = post('/api/profiles/save', {'value': self.settings, 'password': self.PASSWORD,
                                                   'remember_password': True})
        self.assertEqual(status, 200)
        self.assertNotIn(self.PASSWORD, body)
        profile_id = json.loads(body)['id']
        status, body = post('/api/profiles/forget-password', {'id': profile_id})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)['has_saved_password'])
        status, body = post('/api/profiles/save', {'value': self.settings, 'remember_password': True})
        self.assertEqual(status, 400)
        self.assertIn('Enter the password', json.loads(body)['error'])


class TestLiveBulkKillAndTableColumns(unittest.TestCase):
    def setUp(self):
        from myflames.workspace_store import WorkspaceStore
        from myflames.live import LiveJobs
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = WorkspaceStore(os.path.join(self.temp_dir.name, 'workspace.db'))
        self.workspace = mock.Mock()
        self.workspace.store = self.store
        self.workspace.jobs = LiveJobs()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_bulk_kill_validation(self):
        conn = {'host': 'localhost', 'port': 3306, 'user': 'root'}
        # targets missing or not a list
        with self.assertRaisesRegex(ValueError, 'Choose between 1 and 200'):
            api_response('/api/live/kill/bulk', {'connection': conn, 'targets': None}, self.workspace)
        with self.assertRaisesRegex(ValueError, 'Choose between 1 and 200'):
            api_response('/api/live/kill/bulk', {'connection': conn, 'targets': []}, self.workspace)
        with self.assertRaisesRegex(ValueError, 'Choose between 1 and 200'):
            api_response('/api/live/kill/bulk', {'connection': conn, 'targets': [{'connection_id': i} for i in range(201)]}, self.workspace)
        # invalid mode
        with self.assertRaisesRegex(ValueError, "mode must be 'connection' or 'query'"):
            api_response('/api/live/kill/bulk', {'connection': conn, 'targets': [{'connection_id': 1}], 'mode': 'invalid'}, self.workspace)

    def test_bulk_kill_unconfirmed_gate(self):
        conn = {'host': 'localhost', 'port': 3306, 'user': 'root'}
        targets = [{'connection_id': 42, 'expect': {'connection_id': 42, 'command': 'Sleep', 'user': 'test', 'host': 'localhost'}}]
        res = api_response('/api/live/kill/bulk', {'connection': conn, 'targets': targets, 'confirmed': False}, self.workspace)
        self.assertEqual(res['schema_version'], 'kill-bulk-1.0')
        self.assertEqual(res['total'], 1)
        self.assertEqual(res['killed'], 0)
        self.assertEqual(res['refused'], 1)
        records = self.store.audit_records()
        self.assertEqual(len(records), 1)
        self.assertEqual((records[0]['status'], records[0]['outcome']), ('refused', 'needs_confirmation'))

    def test_bulk_kill_prepare_and_execute(self):
        from myflames.review_tokens import ReviewTokens
        from myflames import ui
        self.workspace.review_tokens = ReviewTokens()
        conn = {'host': 'localhost', 'port': 3306, 'user': 'root'}
        identity = ui._review_identity(conn, self.workspace)
        targets = [
            {'connection_id': 10, 'expect': {'connection_id': 10, 'command': 'Query', 'user': 'app', 'host': '127.0.0.1:1234', 'connection_identity': identity}},
            {'connection_id': 20, 'expect': {'connection_id': 20, 'command': 'Sleep', 'user': 'app', 'host': '127.0.0.1:1235', 'connection_identity': identity}}
        ]
        prep = api_response('/api/live/kill/bulk/prepare', {'connection': conn, 'targets': targets}, self.workspace)
        self.assertIn('review_token', prep)
        self.assertEqual(len(prep['statements']), 2)

        fake_conn = mock.MagicMock()
        fake_conn.__enter__.return_value = fake_conn
        fake_conn.__exit__.return_value = False

        with mock.patch('myflames.connector.MySQLConnection', return_value=fake_conn), \
             mock.patch('myflames.locks._kill_context', return_value=(('mysql', '8.4.6', (8, 4, 6), True), {})), \
             mock.patch('myflames.locks._kill_batch_after', return_value={}), \
             mock.patch('myflames.locks.kill_session') as mock_kill:
            mock_kill.side_effect = [
                {'status': 'killed', 'message': 'Killed connection 10.'},
                {'status': 'gone', 'message': 'Connection 20 was already gone.'}
            ]
            res = api_response('/api/live/kill/bulk', {
                'connection': conn,
                'targets': targets,
                'confirmed': True,
                'review_token': prep['review_token']
            }, self.workspace)
            self.assertEqual(res['schema_version'], 'kill-bulk-1.0')
            self.assertEqual(res['total'], 2)
            self.assertEqual(res['killed'], 1)
            self.assertEqual(res['gone'], 1)
            self.assertEqual(res['refused'], 0)
            self.assertEqual(len(res['results']), 2)
            self.assertEqual(res['message'], 'Kill sessions finished: 1 killed, 1 already gone')

    def test_table_columns_names_are_hex_encoded(self):
        conn = {'host': 'localhost', 'port': 3306, 'user': 'root'}
        fake_conn = mock.MagicMock()
        fake_conn.__enter__.return_value = fake_conn
        fake_conn.__exit__.return_value = False
        fake_conn.query_rows.return_value = []
        schema = "x\\'' ; DROP TABLE t; -- "
        with mock.patch('myflames.connector.MySQLConnection', return_value=fake_conn):
            api_response('/api/live/table-columns',
                         {'connection': conn, 'schema': schema, 'table': "t'\\"}, self.workspace)
            sql = fake_conn.query_rows.call_args[0][0]
            self.assertNotIn('DROP TABLE', sql)
            self.assertNotIn("t'", sql)
            self.assertNotIn(';', sql)
            self.assertIn("X'%s'" % schema.encode().hex(), sql)
            with self.assertRaisesRegex(ValueError, '1 to 64'):
                api_response('/api/live/table-columns',
                             {'connection': conn, 'schema': 'a' * 65, 'table': 't'}, self.workspace)

    def test_table_columns_response(self):
        conn = {'host': 'localhost', 'port': 3306, 'user': 'root'}
        with self.assertRaisesRegex(ValueError, 'schema and table are required'):
            api_response('/api/live/table-columns', {'connection': conn}, self.workspace)
        with self.assertRaisesRegex(ValueError, 'schema and table are required'):
            api_response('/api/live/table-columns', {'connection': conn, 'schema': 'shop'}, self.workspace)

        fake_conn = mock.MagicMock()
        fake_conn.__enter__.return_value = fake_conn
        fake_conn.__exit__.return_value = False
        fake_conn.query_rows.return_value = [
            ['id', 'bigint', 'NO', None, 'PRI', 'auto_increment'],
            ['username', 'varchar(255)', 'YES', 'NULL', '', ''],
        ]

        with mock.patch('myflames.connector.MySQLConnection', return_value=fake_conn):
            res = api_response('/api/live/table-columns', {'connection': conn, 'schema': 'shop', 'table': 'users'}, self.workspace)
            self.assertEqual(res['schema'], 'shop')
            self.assertEqual(res['table'], 'users')
            self.assertEqual(len(res['columns']), 2)
            self.assertEqual(res['columns'][0], {
                'name': 'id',
                'type': 'bigint',
                'nullable': False,
                'default': None,
                'key': 'PRI',
                'extra': 'auto_increment'
            })
            self.assertEqual(res['columns'][1]['nullable'], True)


class TestFilesEndpoints(unittest.TestCase):
    """The Files place's /api/files/* and /api/ssh/* routes (ui.py -> sftp_api.py, ssh_connections.py)."""

    SSH = 'a' * 32

    def setUp(self):
        self.workspace = mock.Mock()
        self.workspace.store = mock.sentinel.store

    def call(self, path, payload):
        return api_response(path, payload, workspace=self.workspace)

    def test_workspace_is_required(self):
        for path in ('/api/files/remote/list', '/api/files/jobs', '/api/ssh/connections/list'):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, 'running workspace'):
                    api_response(path, {}, workspace=None)

    def test_remote_routes_pass_the_store_and_connection_id(self):
        listing = {'path': '/var/log', 'home': '/home/deploy', 'entries': []}
        with mock.patch('myflames.sftp_api.api_listdir', return_value=listing) as listdir:
            self.assertEqual(self.call('/api/files/remote/list', {'ssh': self.SSH, 'path': '/var/log'}), listing)
        self.assertEqual(listdir.call_args[0], (mock.sentinel.store, self.SSH, '/var/log'))
        with mock.patch('myflames.sftp_api.api_mkdir', return_value={'path': '/var/log/new'}) as mkdir:
            self.call('/api/files/remote/mkdir', {'ssh': self.SSH, 'path': '/var/log/new'})
        self.assertEqual(mkdir.call_args[0], (mock.sentinel.store, self.SSH, '/var/log/new'))
        with mock.patch('myflames.sftp_api.api_rename', return_value={'path': '/b'}) as rename:
            self.call('/api/files/remote/rename', {'ssh': self.SSH, 'from': '/a', 'to': '/b'})
        self.assertEqual(rename.call_args[0], (mock.sentinel.store, self.SSH, '/a', '/b'))
        with mock.patch('myflames.sftp_api.api_remove', return_value={'removed': 3}) as remove:
            self.assertEqual(self.call('/api/files/remote/remove', {'ssh': self.SSH, 'paths': ['/old']}),
                             {'removed': 3})
        self.assertEqual(remove.call_args[0], (mock.sentinel.store, self.SSH, ['/old']))
        with mock.patch('myflames.ssh_connections.list_connections', return_value={'connections': []}) as listed:
            self.assertEqual(self.call('/api/ssh/connections/list', {}), {'connections': []})
        self.assertEqual(listed.call_args[0], (mock.sentinel.store,))

    def test_local_list_opens_at_home_and_describes_entries(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'slow.log').write_text('x', encoding='utf-8')
            os.mkdir(os.path.join(folder, 'archive'))
            os.symlink(os.path.join(folder, 'archive'), os.path.join(folder, 'archive-link'))
            with mock.patch.dict(os.environ, {'MYFLAMES_SSH_HOME': folder}):
                result = self.call('/api/files/local/list', {'path': ''})
        self.assertEqual((result['path'], result['home']), (os.path.realpath(folder), os.path.realpath(folder)))
        by_name = {entry['name']: entry for entry in result['entries']}
        self.assertEqual(set(by_name), {'slow.log', 'archive', 'archive-link'})
        self.assertEqual((by_name['slow.log']['kind'], by_name['slow.log']['size']), ('file', 1))
        self.assertEqual((by_name['archive']['kind'], by_name['archive']['is_dir']), ('dir', True))
        self.assertEqual((by_name['archive-link']['kind'], by_name['archive-link']['is_dir']), ('link', True))

    def test_local_list_rejects_bad_paths_before_touching_disk(self):
        for value in (None, '  ', 'relative/path', 'a\x00b', 42, ['/tmp']):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.call('/api/files/local/list', {'path': value})
        with self.assertRaisesRegex(ValueError, 'no local folder'):
            self.call('/api/files/local/list', {'path': '/definitely/not/a/folder/myflames'})

    def test_local_list_skips_broken_entries_and_keeps_the_full_mode(self):
        from myflames import sftp_api
        broken = mock.MagicMock()
        broken.name = 'gone'
        broken.stat.side_effect = FileNotFoundError()
        kept = mock.MagicMock()
        kept.name = 'kept'
        kept.stat.return_value = os.stat_result((0o100644, 0, 0, 1, 0, 0, 5, 0, 0, 0))
        scan = mock.MagicMock()
        scan.__enter__.return_value = iter([broken, kept])
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch('os.scandir', return_value=scan):
                result = sftp_api.api_local_list(folder)
        self.assertEqual([entry['name'] for entry in result['entries']], ['kept'])
        self.assertEqual(result['entries'][0]['mode'], 0o100644)

    def test_transfer_and_queue_shapes(self):
        job = {'id': 'b' * 32, 'state': 'queued'}
        for path, target in (('/api/files/upload', 'myflames.sftp_api.api_start_upload'),
                             ('/api/files/download', 'myflames.sftp_api.api_start_download')):
            with self.subTest(path=path), mock.patch(target, return_value=job) as start:
                payload = {'ssh': self.SSH, 'on_exists': 'fail', 'local_paths': ['/tmp/a.log'], 'remote_dir': '/var/log'}
                self.assertEqual(self.call(path, payload), {'job': job})
            self.assertEqual(start.call_args[0], (mock.sentinel.store, payload))
        with mock.patch('myflames.sftp_jobs.jobs', return_value=[job]) as jobs:
            self.assertEqual(self.call('/api/files/jobs', {'ssh': self.SSH}), {'jobs': [job]})
        self.assertEqual(jobs.call_args[1], {'ssh': self.SSH})
        with mock.patch('myflames.sftp_jobs.cancel', return_value=job) as cancel:
            self.assertEqual(self.call('/api/files/cancel', {'job_id': 'b' * 32}), {'job': job})
        self.assertEqual(cancel.call_args[0][0], 'b' * 32)
        with mock.patch('myflames.sftp_jobs.retry', return_value=job):
            self.assertEqual(self.call('/api/files/retry', {'job_id': 'b' * 32}), {'job': job})
        with mock.patch('myflames.sftp_jobs.clear', return_value=2):
            self.assertEqual(self.call('/api/files/clear', {}), {'cleared': 2})
        with self.assertRaises(ValueError):
            self.call('/api/files/cancel', {'job_id': '../x'})

    def test_files_paths_are_in_the_post_allowlist(self):
        import inspect
        from myflames import ui
        source = inspect.getsource(ui.WorkspaceHandler.do_POST)
        self.assertIn('_SSH_CONNECTION_PATHS + _FILES_PATHS', source)
        self.assertFalse(hasattr(ui, '_SFTP_PATHS'), 'the unreleased /api/live/sftp routes are gone')


class TestBaselineSamplingDispatch(unittest.TestCase):
    def test_identity_is_passed_to_collectors_without_a_later_client_read(self):
        from myflames import connector, ui
        for path, target, args in (
            ('/api/live/health', 'myflames.health.health_report', {'interval': 5}),
            ('/api/live/top', 'myflames.discover.top_report', {'limit': 50}),
        ):
            for flag in (None, False, True, 'true'):
                with self.subTest(path=path, flag=flag), mock.patch.object(connector, 'MySQLConnection') as factory, \
                        mock.patch(target, return_value={'reading_server': {'verified': False, 'error': 'unavailable'}}) as read:
                    result = ui._discover_response(path, dict(args, connection={'host': 'db'}, with_identity=flag))
                    self.assertEqual(read.call_args[1]['with_identity'], flag is True)
                    self.assertIs(result, read.return_value)
                    factory.return_value.__enter__.return_value.run.assert_not_called()
