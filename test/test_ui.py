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
PLAN = (TEST_DIR / 'mysql-explain-json-sample.json').read_text()
MARIA = (TEST_DIR / 'mariadb-explain-join.json').read_text()


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

    def test_catalog_prefix_and_keyset_are_forwarded_literally(self):
        args, kwargs = self.call('/api/live/catalog', {'schema': 'shop', 'prefix': 'a%_', 'after': 'a%_Z'},
                                 'myflames.schema_browser.catalog_report')
        self.assertEqual(args[1:], ('shop', None))
        self.assertEqual(kwargs, {'prefix': 'a%_', 'after': 'a%_Z'})
        _, kwargs = self.call('/api/live/catalog', {'schema': 'shop'}, 'myflames.schema_browser.catalog_report')
        self.assertEqual(kwargs, {'prefix': None, 'after': None})

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
                self.assertEqual(kwargs, {'prefix': expected, 'after': expected})

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
        app_css = (TEST_DIR.parent / 'apps/workspace/src/style.css').read_text()
        self.assertIn(style.group(1).strip(), app_css)
        for secret in (self.server.token, self.server.session_secret, self.server.bootstrap_secret):
            self.assertNotIn(secret, page)
        self.assertNotIn('myflames-token', page)


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

    def test_teach_lesson_views_over_http(self):
        status, body, _ = self.request('POST', '/api/teach/lesson', {'lesson': 'btree', 'view': '3d'})
        self.assertEqual(status, 200)
        self.assertIn('myflames-sources-sha256', json.loads(body)['html'])
        for payload in ({'lesson': 'btree', 'view': '4d'}, {'lesson': 'full_scan', 'view': '3d'}):
            with self.subTest(payload=payload):
                status, body, _ = self.request('POST', '/api/teach/lesson', payload)
                self.assertEqual(status, 400)
                self.assertIn('error', json.loads(body))

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

    def test_catalog_lists_the_views_of_every_lesson(self):
        from myflames.teach import LESSON_3D
        lessons = {lesson['key']: lesson for lesson in api_response('/api/teach/catalog', {})['lessons']}
        for key, lesson in lessons.items():
            with self.subTest(lesson=key):
                self.assertIn('views', lesson)
                self.assertEqual(lesson['views'][0], '2d')
                self.assertEqual(lesson['views'], ['2d', '3d'] if key in LESSON_3D else ['2d'])
        self.assertEqual(lessons['btree']['views'], ['2d', '3d'])
        self.assertEqual(lessons['full_scan']['views'], ['2d'])

    def test_lesson_3d_view_returns_the_prebuilt_page(self):
        from myflames.teach import render_lesson_3d
        html = api_response('/api/teach/lesson', {'lesson': 'btree', 'view': '3d'})['html']
        self.assertEqual(html, render_lesson_3d('btree'))
        self.assertIn('myflames-sources-sha256', html)

    def test_lesson_2d_view_is_the_default(self):
        from myflames.teach import render_lesson
        expected = render_lesson('btree')
        self.assertEqual(api_response('/api/teach/lesson', {'lesson': 'btree'})['html'], expected)
        self.assertEqual(api_response('/api/teach/lesson', {'lesson': 'btree', 'view': '2d'})['html'],
                         expected)
        self.assertEqual(api_response('/api/teach/lesson', {'lesson': 'full_scan', 'view': '2d'})['html'],
                         render_lesson('full_scan'))

    def test_unknown_view_or_lesson_without_3d_is_rejected(self):
        cases = [('btree', view) for view in ('4d', '3D', '', None, [], {}, 3)]
        cases.append(('full_scan', '3d'))
        for key, view in cases:
            with self.subTest(lesson=key, view=view):
                with self.assertRaisesRegex(ValueError, 'no .* view'):
                    api_response('/api/teach/lesson', {'lesson': key, 'view': view})


class _FakeDB(object):
    """Stands in for MySQLConnection in connection/test; records the options."""

    def __init__(self, error=None):
        self.error = error
        self.options = None

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
        return '8.4.3'

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

    def test_connection_endpoints_fill_the_saved_password(self):
        from unittest import mock
        from myflames import ui
        profile_id = self.save()['id']
        fake = _FakeDB()
        with mock.patch('myflames.connector.MySQLConnection', fake):
            result = self.call('/api/connection/test', {'connection': self.connect(profile_id)})
        self.assertEqual(result['version'], '8.4.3')
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
        targets = [{'connection_id': 42, 'expect': {'connection_id': 42, 'command': 'Sleep', 'user': 'test'}}]
        res = api_response('/api/live/kill/bulk', {'connection': conn, 'targets': targets, 'confirmed': False}, self.workspace)
        self.assertEqual(res['schema_version'], 'kill-bulk-1.0')
        self.assertEqual(res['total'], 1)
        self.assertEqual(res['killed'], 0)
        self.assertEqual(res['refused'], 1)

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
