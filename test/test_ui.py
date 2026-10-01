"""Local workspace API, isolation, and lifecycle regressions."""
import http.client
import json
from pathlib import Path
import re
import threading
import unittest

from myflames.parser import parse_explain, analyze_plan
from myflames.output_sidecar import build_sidecar
from myflames.output_compare_sidecar import build_compare_sidecar
from myflames.ui import api_response, WorkspaceServer, MAX_BODY, VIEWS

TEST_DIR = Path(__file__).resolve().parent
PLAN = (TEST_DIR / 'mysql-explain-json-sample.json').read_text()
MARIA = (TEST_DIR / 'mariadb-explain-join.json').read_text()


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

    def test_wrapped_mysql_preserves_query(self):
        result = api_response('/api/analyze', {'plan': json.dumps({'query_plan': json.loads(PLAN)})})
        self.assertIn('select', result['query'].lower())
        self.assertEqual(result['analysis']['query']['raw'], result['query'])

    def test_each_existing_view_renders_both_engines(self):
        for raw in (PLAN, MARIA):
            for view in VIEWS:
                with self.subTest(view=view):
                    html = api_response('/api/render', {'plan': raw, 'view': view})['html']
                    self.assertIn('<svg', html)
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


class TestWorkspaceHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = WorkspaceServer()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, method, path, payload=None, headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=10)
        defaults = {'Content-Type': 'application/json', 'X-Myflames-Token': self.server.token}
        defaults.update(headers or {})
        body = json.dumps(payload) if payload is not None else None
        try:
            conn.request(method, path, body=body, headers=defaults)
            response = conn.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            conn.close()

    def test_packaged_index_and_bundles(self):
        status, body, headers = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(self.server.token.encode(), body)
        self.assertNotIn(b'__MYFLAMES_TOKEN__', body)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        for asset in re.findall(r'(?:src|href)="(/assets/[^\"]+)"', body.decode()):
            status, content, _ = self.request('GET', asset)
            self.assertEqual(status, 200, asset)
            self.assertGreater(len(content), 0)

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
