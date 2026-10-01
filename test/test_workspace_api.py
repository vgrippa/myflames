"""Authenticated workspace persistence and capture routes over real HTTP."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from myflames.connector import ConnectorError
from myflames.ui import ASSET_DIR, WorkspaceServer, api_response

PLAN = (Path(__file__).parent / 'mysql-explain-json-sample.json').read_text()


class TestWorkspaceAPI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'private' / 'workspace.sqlite3'
        self.server = WorkspaceServer(0, store_path=self.path)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def request(self, path, payload=None, headers=None, method='POST'):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        default = {'Content-Type': 'application/json', 'X-Myflames-Token': self.server.token}
        default.update(headers or {})
        try:
            conn.request(method, path, json.dumps(payload or {}) if method == 'POST' else None, default)
            response = conn.getresponse()
            raw = response.read()
            body = json.loads(raw) if response.getheader('Content-Type').startswith('application/json') else raw
            return response.status, body, dict(response.getheaders())
        finally:
            conn.close()

    def test_empty_lists_do_not_create_storage(self):
        for kind in ('investigations', 'profiles'):
            status, body, headers = self.request('/api/' + kind + '/list')
            self.assertEqual(status, 200)
            self.assertEqual(body, {'items': []})
            self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertFalse(self.path.exists())

    def test_investigation_save_update_list_and_delete(self):
        value = {'schema_version': 'investigation-1.0', 'name': 'Before index',
                 'plans': [{'id': 'base', 'name': 'Original', 'raw': PLAN}], 'baseline_id': 'base'}
        status, first, _ = self.request('/api/investigations/save', {'value': value})
        self.assertEqual(status, 200)
        self.assertTrue(first['id'])
        value['name'] = 'After index'
        status, changed, _ = self.request('/api/investigations/save', {'id': first['id'], 'value': value})
        self.assertEqual(status, 200)
        status, listed, _ = self.request('/api/investigations/list')
        self.assertEqual(status, 200)
        self.assertEqual(listed['items'], [changed])
        self.assertEqual(changed['name'], 'After index')
        status, body, _ = self.request('/api/investigations/delete', {'id': first['id']})
        self.assertEqual((status, body), (200, {'deleted': True}))
        self.assertEqual(self.request('/api/investigations/list')[1], {'items': []})

    def test_profile_save_drops_secrets_and_kind_isolation_holds(self):
        value = {'name': 'Reporting', 'host': 'db.example', 'port': '3307', 'user': 'reader',
                 'password': 'secret-for-test', 'connection': {'password': 'secret-for-test'}}
        status, saved, _ = self.request('/api/profiles/save', {'value': value})
        self.assertEqual(status, 200)
        self.assertNotIn('secret-for-test', json.dumps(saved))
        self.assertNotIn('password', saved)
        self.assertEqual(saved['port'], 3307)
        self.assertNotIn(b'secret-for-test', self.path.read_bytes())
        self.request('/api/investigations/delete', {'id': saved['id']})
        self.assertEqual(self.request('/api/profiles/list')[1]['items'], [saved])
        self.request('/api/profiles/delete', {'id': saved['id']})
        self.assertEqual(self.request('/api/profiles/list')[1], {'items': []})

    def test_workspace_routes_require_current_token_and_same_origin(self):
        routes = ('/api/investigations/list', '/api/investigations/save', '/api/profiles/save',
                  '/api/live/start', '/api/live/status', '/api/live/cancel', '/api/connection/test')
        with patch.object(self.server.jobs, 'start') as start, \
                patch('myflames.connector.MySQLConnection') as connect:
            for route in routes:
                for headers in ({'X-Myflames-Token': ''}, {'Origin': 'https://example.com'}, {'Host': 'evil.test'}):
                    with self.subTest(route=route, headers=headers):
                        self.assertEqual(self.request(route, headers=headers)[0], 403)
            start.assert_not_called()
            connect.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_live_start_status_and_cancel_share_server_job_manager(self):
        job = MagicMock()
        job.snapshot.return_value = {'id': 'job-1', 'state': 'running', 'progress': 1}
        value = {'connection': {'host': 'localhost'}, 'query': 'SELECT 1'}
        with patch.object(self.server.jobs, 'start', return_value={'id': 'job-1', 'state': 'queued'}) as start, \
                patch.object(self.server.jobs, 'get', return_value=job) as get:
            status, body, _ = self.request('/api/live/start', value)
            self.assertEqual((status, body), (200, {'id': 'job-1', 'state': 'queued'}))
            start.assert_called_once_with(value)
            status, body, _ = self.request('/api/live/status', {'id': 'job-1'})
            self.assertEqual((status, body['state']), (200, 'running'))
            job.cancel.side_effect = lambda: setattr(job.snapshot, 'return_value', {'id': 'job-1', 'state': 'cancelled'})
            status, body, _ = self.request('/api/live/cancel', {'id': 'job-1'})
            self.assertEqual((status, body['state']), (200, 'cancelled'))
            job.cancel.assert_called_once_with()
            self.assertEqual(get.call_count, 2)
        self.assertFalse(self.path.exists())

    def test_invalid_live_parameters_and_missing_jobs_return_400(self):
        values = ({}, {'connection': {}, 'query': 'DELETE FROM t'},
                  {'connection': {}, 'query': 'SELECT 1', 'repeat': 0},
                  {'connection': {}, 'query': 'SELECT 1', 'parameters': []},
                  {'connection': {'port': 0}, 'query': 'SELECT 1'},
                  {'connection': {}, 'query': 'SELECT 1', 'timeout': 301})
        with patch('myflames.live.MySQLConnection') as connection:
            for value in values:
                with self.subTest(value=value):
                    status, body, _ = self.request('/api/live/start', value)
                    self.assertEqual(status, 400)
                    self.assertIn('error', body)
            connection.assert_not_called()
        for route in ('/api/live/status', '/api/live/cancel'):
            self.assertEqual(self.request(route, {'id': 'unknown'})[0], 400)

    def test_connection_test_uses_validated_options_and_returns_schemas(self):
        with patch('myflames.connector.MySQLConnection') as connection:
            conn = connection.return_value.__enter__.return_value
            conn.server_version.return_value = '9.7.0'
            conn.query_rows.return_value = [['inventory'], [], ['reporting']]
            status, body, _ = self.request('/api/connection/test', {'connection': {
                'host': 'localhost', 'port': '3307', 'password': 'secret-for-test'}})
            self.assertEqual((status, body), (200, {'version': '9.7.0', 'schemas': ['inventory', 'reporting']}))
            self.assertEqual(connection.call_args[1]['port'], 3307)
            self.assertEqual(connection.call_args[1]['password'], 'secret-for-test')
            conn.query_rows.assert_called_once_with('SHOW DATABASES')
            connection.return_value.__exit__.assert_called_once()
        self.assertFalse(self.path.exists())

    def test_connection_errors_are_json_client_errors(self):
        with patch('myflames.connector.MySQLConnection', side_effect=ConnectorError('Connection refused')):
            status, body, _ = self.request('/api/connection/test', {'connection': {}})
        self.assertEqual(status, 400)
        self.assertIn('Connection refused', body['error'])

    def test_invalid_saved_records_return_400(self):
        for path, value in (('/api/investigations/save', {}),
                            ('/api/investigations/save', {'schema_version': 'investigation-1.0', 'plans': [{}]}),
                            ('/api/profiles/save', {'host': 'a\npassword=b'})):
            with self.subTest(path=path, value=value):
                status, body, _ = self.request(path, {'value': value})
                self.assertEqual(status, 400)
                self.assertIn('error', body)
        self.assertFalse(self.path.exists())

    def test_bundled_project_logo_is_served_as_png(self):
        logos = list((ASSET_DIR / 'assets').glob('myflames-logo.png'))
        self.assertEqual(len(logos), 1)
        status, body, headers = self.request('/assets/' + logos[0].name, method='GET')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'].split(';')[0], 'image/png')
        self.assertEqual(body, logos[0].read_bytes())
        self.assertEqual(body, (Path(__file__).resolve().parents[1] / 'docs/brand/myflames-icon-v2.png').read_bytes())
        self.assertTrue(body.startswith(b'\x89PNG\r\n\x1a\n'))
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')

    def test_logo_urls_from_open_older_tabs_still_work(self):
        expected = (ASSET_DIR / 'assets' / 'myflames-logo.png').read_bytes()
        for name in ('myflames-BpuTIrWf.jpeg', 'logo-BpuTIrWf.jpeg',
                     'myflames-icon-v2-CFba5YPm.png'):
            with self.subTest(name=name):
                status, body, headers = self.request('/assets/' + name, method='GET')
                self.assertEqual(status, 200)
                self.assertEqual(body, expected)
                self.assertEqual(headers['Content-Type'].split(';')[0], 'image/png')
                self.assertEqual(headers['Cache-Control'], 'no-store')

    def test_page_favicon_versions_stable_logo_url(self):
        status, body, _ = self.request('/', method='GET')
        self.assertEqual(status, 200)
        self.assertIn(b'href="/assets/myflames-logo.png?v=2"', body)
        status, icon, headers = self.request('/assets/myflames-logo.png?v=2', method='GET')
        self.assertEqual(status, 200)
        self.assertEqual(icon, (ASSET_DIR / 'assets' / 'myflames-logo.png').read_bytes())
        self.assertEqual(headers['Content-Type'].split(';')[0], 'image/png')

    def test_direct_workspace_routes_require_workspace_context(self):
        with self.assertRaisesRegex(ValueError, 'running workspace'):
            api_response('/api/investigations/list', {})

    def test_analysis_returns_normalized_imported_capture(self):
        status, body, _ = self.request('/api/analyze', {'plan': PLAN, 'capture': {
            'mode': 'analyze', 'password': 'secret-for-test', 'connection': {'password': 'secret-for-test'}}})
        self.assertEqual(status, 200)
        self.assertEqual(body['capture']['runs'], [])
        self.assertEqual(body['capture']['warnings'], [])
        self.assertEqual(body['capture']['measurements']['count'], 0)
        self.assertNotIn('secret-for-test', json.dumps(body))

    def test_bad_imported_capture_returns_400_for_analysis_and_save(self):
        for capture in ([], {'runs': 1}, {'runs': [None]}, {'variables': []},
                        {'runs': [{'total_time_ms': float('nan')}]},
                        {'runs': [{'plan': '{}'}]}, {'warnings': 'warning'}):
            with self.subTest(capture=capture):
                status, body, _ = self.request('/api/analyze', {'plan': PLAN, 'capture': capture})
                self.assertEqual(status, 400)
                self.assertIn('error', body)
                value = {'schema_version': 'investigation-1.0', 'plans': [
                    {'id': 'plan', 'raw': PLAN, 'capture': capture}]}
                status, body, _ = self.request('/api/investigations/save', {'value': value})
                self.assertEqual(status, 400)
                self.assertIn('error', body)
        self.assertFalse(self.path.exists())
