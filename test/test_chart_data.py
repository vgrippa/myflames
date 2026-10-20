"""chart-data-1.0 payload, its schema, and the inlined chart bundle.

Covers every fixture: identity and order, the Python-owned semantics the
bundle draws, measurement truthfulness, projection boundaries, script-safe
embedding, and the committed bundle's freshness and size budget.
"""
import copy
import glob
import hashlib
import json
import math
import os
import re
import tempfile
import unittest
from pathlib import Path

from myflames import chart_assets
from myflames.chart_assets import chart_embed, chart_runtime, script_json
from myflames.chart_data import COMPACT_SCHEMA_VERSION, SCHEMA_VERSION, build_chart_data
from myflames.exploration import METRICS, metric_value
from myflames.output_workbench import COLORS, _arrow_width, _details, _join_type, _kind, _phase
from myflames.parser import analyze_plan, enhance_tooltip_flame, flatten_nodes, operator_family, parse_explain
from myflames.teach_hooks import build_teach_hooks, build_teach_index_maps

TEST = Path(__file__).resolve().parent
REPO = TEST.parent
SCHEMA = json.loads((REPO / 'docs' / 'schemas' / 'chart-data-1.0.json').read_text(encoding='utf-8'))
COMPACT_SCHEMA = json.loads((REPO / 'docs' / 'schemas' / 'chart-data-2.0.json').read_text(encoding='utf-8'))
FIXTURES = sorted(glob.glob(str(TEST / 'fixtures' / '*.json')) + glob.glob(str(TEST / '*.json')))
WORKSPACE = REPO / 'apps' / 'workspace'
JOIN_KINDS = ('nested_loop', 'hash', 'bka')


def _types(value):
    if value is None:
        return {'null'}
    if isinstance(value, bool):
        return {'boolean'}
    if isinstance(value, int):
        return {'integer', 'number'}
    if isinstance(value, float):
        return {'number'}
    if isinstance(value, str):
        return {'string'}
    if isinstance(value, list):
        return {'array'}
    if isinstance(value, dict):
        return {'object'}
    return set()


def validate(value, schema, path='$', root=None):
    """Validate the JSON Schema subset used by chart-data-1.0 (stdlib only)."""
    root = root or schema
    if '$ref' in schema:
        target = root
        for part in schema['$ref'].lstrip('#/').split('/'):
            target = target[part]
        validate(value, target, path, root)
    if 'oneOf' in schema:
        passed = 0
        for option in schema['oneOf']:
            try:
                validate(value, option, path, root)
                passed += 1
            except AssertionError:
                pass
        assert passed == 1, '%s: matches %d oneOf options' % (path, passed)
    if 'type' in schema:
        allowed = schema['type'] if isinstance(schema['type'], list) else [schema['type']]
        assert _types(value) & set(allowed), '%s: %r is not %s' % (path, value, allowed)
    if 'const' in schema:
        assert value == schema['const'], '%s: expected %r' % (path, schema['const'])
    if 'enum' in schema:
        assert value in schema['enum'], '%s: %r not in enum' % (path, value)
    if isinstance(value, str) and 'pattern' in schema:
        assert re.search(schema['pattern'], value), '%s: %r does not match' % (path, value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        assert math.isfinite(value), '%s: non-finite number' % path
        if 'minimum' in schema:
            assert value >= schema['minimum'], path
        if 'maximum' in schema:
            assert value <= schema['maximum'], path
    if isinstance(value, dict):
        for key in schema.get('required', []):
            assert key in value, '%s: missing %s' % (path, key)
        properties = schema.get('properties', {})
        for key, item in value.items():
            if key in properties:
                validate(item, properties[key], path + '.' + key, root)
            elif schema.get('additionalProperties') is False:
                raise AssertionError('%s: unexpected %s' % (path, key))
            elif isinstance(schema.get('additionalProperties'), dict):
                validate(item, schema['additionalProperties'], path + '.' + key, root)
    if isinstance(value, list):
        if 'minItems' in schema:
            assert len(value) >= schema['minItems'], path
        if 'maxItems' in schema:
            assert len(value) <= schema['maxItems'], path
        prefix = schema.get('prefixItems', [])
        for i, item in enumerate(value):
            if i < len(prefix):
                validate(item, prefix[i], '%s[%d]' % (path, i), root)
            elif 'items' in schema:
                validate(item, schema['items'], '%s[%d]' % (path, i), root)


def plan(**fields):
    value = {'operation': 'Table scan on t', 'access_type': 'table', 'table_name': 't'}
    value.update(fields)
    return parse_explain(json.dumps(value))


def sources_sha():
    """Same algorithm as apps/workspace/vite.charts.config.ts:sourcesSha."""
    paths = []
    for directory, folders, files in os.walk(str(WORKSPACE / 'src' / 'charts')):
        folders[:] = [f for f in folders if not f.startswith('.')]
        for name in files:
            if not name.startswith('.'):
                full = Path(directory, name)
                paths.append(full.relative_to(WORKSPACE).as_posix())
    paths.append('vite.charts.config.ts')
    digest = hashlib.sha256()
    for rel in sorted(paths):
        text = (WORKSPACE / rel).read_text(encoding='utf-8').replace('\r\n', '\n')
        digest.update(rel.encode('utf-8') + b'\0' + text.encode('utf-8') + b'\0')
    return digest.hexdigest()


class ChartDataFixtureTests(unittest.TestCase):
    def test_compact_chart_roundtrips_exact_legacy_semantics_for_every_fixture(self):
        """MySQL and MariaDB share a transport optimization, never new findings."""
        for name in FIXTURES:
            with self.subTest(fixture=os.path.basename(name)):
                root = parse_explain(Path(name).read_text(encoding='utf-8'))
                analysis = analyze_plan(root)
                legacy = build_chart_data(root, analysis=analysis)
                compact = build_chart_data(root, analysis=analysis, compact=True)
                validate(compact, COMPACT_SCHEMA)
                self.assertEqual(compact['schema_version'], COMPACT_SCHEMA_VERSION)
                restored = json.loads(json.dumps(compact, allow_nan=False))
                messages = restored.pop('highlight_messages')
                self.assertEqual(len(messages), len(set(messages)))
                # Every message is used, numbered in the order nodes first use it.
                first_use = []
                for row in restored['nodes']:
                    if row['highlight_id'] is not None and row['highlight_id'] not in first_use:
                        first_use.append(row['highlight_id'])
                self.assertEqual(first_use, list(range(len(messages))))
                for row in restored['nodes']:
                    self.assertNotIn('highlight', row)
                    ref = row.pop('highlight_id')
                    row['highlight'] = messages[ref] if ref is not None else None
                restored['schema_version'] = SCHEMA_VERSION
                self.assertEqual(restored, legacy)

    def test_compact_highlights_preserve_first_match_empty_and_untrusted_text(self):
        root = plan()
        label = root['short_label']
        for message in ('', '</script><img src=x onerror=alert(1)>\n日本語'):
            with self.subTest(message=message):
                analysis = {'node_highlights': [
                    {'short_label': label, 'message': message},
                    {'short_label': label, 'message': 'Later match must not win'}]}
                data = build_chart_data(root, analysis=analysis, compact=True)
                self.assertEqual(data['highlight_messages'], [message])
                self.assertEqual(data['nodes'][0]['highlight_id'], 0)
                self.assertEqual(json.loads(script_json(data)), data)
        data = build_chart_data(root, compact=True)
        self.assertEqual(data['highlight_messages'], [])
        self.assertIsNone(data['nodes'][0]['highlight_id'])

    def test_grouped_scan_warning_serialization_grows_linearly(self):
        sizes = []
        for count in (800, 1600):
            root = parse_explain(json.dumps({'operation': 'Union all', 'inputs': [
                {'operation': 'Table scan on t%d' % i, 'table_name': 't%d' % i,
                 'access_type': 'table', 'actual_rows': 100, 'actual_loops': 1,
                 'actual_last_row_ms': 1} for i in range(count)]}))
            analysis = analyze_plan(root)
            data = build_chart_data(root, analysis=analysis, compact=True)
            warning = analysis['warnings'][0]
            self.assertTrue(warning.startswith('Full table scan:'))
            self.assertEqual(data['highlight_messages'], [warning])
            self.assertEqual(sum(row['highlight_id'] == 0 for row in data['nodes']), count)
            serialized = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
            self.assertEqual(serialized.count(warning), 1)
            sizes.append(len(serialized.encode('utf-8')))
        self.assertLess(sizes[1], sizes[0] * 2.1,
                        'doubling scans must not repeat a growing warning on every node')

    def test_every_fixture_matches_schema_tree_and_python_semantics(self):
        self.assertGreater(len(FIXTURES), 100)
        for name in FIXTURES:
            with self.subTest(fixture=os.path.basename(name)):
                root = parse_explain(Path(name).read_text(encoding='utf-8'))
                before = copy.deepcopy(root)
                data = build_chart_data(root)
                validate(data, SCHEMA)
                json.dumps(data, allow_nan=False)
                self.assertEqual(root, before, 'building chart data never mutates the tree')
                canonical = list(flatten_nodes(root))
                self.assertEqual([n['id'] for n in data['nodes']], [n['node_id'] for n in canonical])
                self.assertEqual(data['root_id'], root['node_id'])
                self.assertEqual(data['engine'], root['engine'])
                parents = {}
                for parent in canonical:
                    for i, child in enumerate(parent['children']):
                        parents[child['node_id']] = (parent, i)
                for row, node in zip(data['nodes'], canonical):
                    d = node['details']
                    self.assertEqual(row['children'], [c['node_id'] for c in node['children']],
                                     'inputs[] order is preserved, never sorted')
                    parent, index = parents.get(node['node_id'], (None, 0))
                    self.assertEqual(row['parent_id'], parent['node_id'] if parent else None)
                    self.assertEqual(row['phase'], _phase(parent, node, index) if parent else '')
                    self.assertEqual(row['wb_kind'], _kind(node))
                    self.assertEqual(row['family'], operator_family(d))
                    self.assertEqual(row['arrow_width'], _arrow_width(node))
                    self.assertEqual(row['details_text'], _details(node))
                    label = node['folded_label']
                    exact = enhance_tooltip_flame(label, {label: d})
                    self.assertEqual(row['flame_details'], exact[len(label) + 2:] if '\n\n' in exact else '',
                                     'flame tooltip lines describe exactly this operator')
                    self.assertIn(row['wb_kind'], COLORS)
                    if row['wb_kind'] in JOIN_KINDS:
                        self.assertEqual(row['join_type'], _join_type(node))
                    else:
                        self.assertIsNone(row['join_type'])
                    for metric in METRICS:
                        self.assertEqual(tuple(row['metrics'][metric]), metric_value(node, metric))
                    if d.get('actual_last_row_ms') is None:
                        self.assertIsNone(row['self_ms'], 'unmeasured time is null, never zero')
                        self.assertIsNone(row['total_ms'])
                    else:
                        self.assertEqual(row['total_ms'], node['total_time'])
                        self.assertEqual(row['self_ms'], node['self_time'])
                    expected_kind = None if parent is None else (d.get('input_kind') or 'input')
                    self.assertEqual(row['input_kind'], expected_kind)
                depth = {data['root_id']: 0}
                for row in data['nodes']:
                    if row['parent_id']:
                        depth[row['id']] = depth[row['parent_id']] + 1
                    self.assertEqual(row['depth'], depth[row['id']])

    def test_mariadb_synthetic_joins_stay_unknown(self):
        seen = 0
        for name in FIXTURES:
            root = parse_explain(Path(name).read_text(encoding='utf-8'))
            if root['engine'] != 'mariadb':
                continue
            for row, node in zip(build_chart_data(root)['nodes'], flatten_nodes(root)):
                if node['details'].get('join_type') == 'unknown':
                    seen += 1
                    self.assertIn(row['wb_kind'], JOIN_KINDS)
                    self.assertEqual(row['join_type'], 'unknown')
        self.assertGreater(seen, 0)

    def test_cost_is_never_milliseconds_and_unmeasured_is_null(self):
        data = build_chart_data(plan(estimated_total_cost=123.5, estimated_rows=40))
        row = data['nodes'][0]
        self.assertFalse(data['timing_available'])
        self.assertFalse(row['measured'])
        self.assertEqual(row['estimated_cost'], 123.5)
        self.assertIsNone(row['self_ms'])
        self.assertIsNone(row['total_ms'])
        self.assertIsNone(row['last_row_ms'])
        self.assertEqual(row['metrics']['self_time'], [None, 'Not measured'])
        self.assertIn('Estimated optimizer cost: 123.5', row['details_text'])
        self.assertNotIn('(ms): 123.5', row['details_text'])
        measured = build_chart_data(plan(actual_last_row_ms=0, estimated_total_cost=9))['nodes'][0]
        self.assertEqual((measured['self_ms'], measured['total_ms']), (0, 0), 'measured zero stays zero')
        self.assertEqual(measured['estimated_cost'], 9)

    def test_display_unit_is_a_hint_and_values_stay_milliseconds(self):
        data = build_chart_data(plan(actual_last_row_ms=0.25))
        self.assertEqual(data['display_unit'], 'µs')
        self.assertEqual(data['nodes'][0]['total_ms'], 0.25)
        self.assertEqual(build_chart_data(plan(actual_last_row_ms=4))['display_unit'], 'ms')

    def test_teach_index_and_highlights_come_from_python_maps(self):
        root = parse_explain((TEST / 'fixtures' / 'explain-044-join-3t-products-category-reviews.json').read_text())
        maps = build_teach_index_maps(build_teach_hooks(root))['by_folded_label']
        message = {'node_highlights': [{'short_label': root['short_label'], 'message': 'Look here'}]}
        data = build_chart_data(root, teach_index_by_folded=maps, analysis=message)
        indexed = [row for row in data['nodes'] if row['teach_index'] is not None]
        self.assertTrue(indexed)
        for row in indexed:
            self.assertEqual(row['teach_index'], maps[row['folded_label'].strip()])
        self.assertEqual(data['nodes'][0]['highlight'], 'Look here')
        self.assertEqual(sum(1 for row in data['nodes'] if row['highlight']), 1)

    def test_select_list_inputs_keep_order_and_kind(self):
        root = parse_explain(json.dumps({
            'operation': 'Nested loop inner join', 'access_type': 'join', 'actual_last_row_ms': 10,
            'inputs': [{'operation': 'Outer', 'actual_last_row_ms': 2}, {'operation': 'Inner', 'actual_last_row_ms': 3}],
            'inputs_from_select_list': [{'operation': 'Scalar subquery', 'actual_last_row_ms': 8}]}))
        rows = build_chart_data(root)['nodes']
        self.assertEqual([r['full_label'] for r in rows], ['Nested loop inner join', 'Outer', 'Inner', 'Scalar subquery'])
        self.assertEqual([r['input_kind'] for r in rows], [None, 'input', 'input', 'select_list'])
        self.assertEqual([r['phase'] for r in rows[1:]], ['outer', 'inner', 'select-list subquery'])
        self.assertEqual(rows[0]['self_ms'], 5, 'SELECT-list time is not subtracted')

    def test_projection_is_metadata_only(self):
        root = parse_explain((TEST / 'fixtures' / 'explain-044-join-3t-products-category-reviews.json').read_text())
        before = copy.deepcopy(root)
        branch = root['children'][0]['node_id']
        plain = build_chart_data(root)
        projected = build_chart_data(root, projection={'focus_id': branch, 'collapsed_ids': [branch],
                                                       'selected_id': 'n:not-in-plan', 'metric': 'rows'})
        self.assertEqual(plain['nodes'], projected['nodes'], 'measurements do not depend on the view')
        self.assertEqual(projected['projection'], {'focus_id': branch, 'collapsed_ids': [branch],
                                                   'selected_id': 'n:not-in-plan', 'metric': 'rows'})
        self.assertEqual(plain['projection'], {'focus_id': '', 'collapsed_ids': [], 'selected_id': '', 'metric': 'self_time'})
        self.assertEqual(root, before)
        for bad in ({'focus_id': 'n:missing'}, {'collapsed_ids': ['n:missing']}, {'collapsed_ids': 'x'},
                    {'metric': 'cost'}):
            with self.assertRaises(ValueError):
                build_chart_data(root, projection=bad)

    def test_flame_details_never_borrow_another_operators_details(self):
        root = parse_explain(json.dumps({
            'operation': 'Nested loop inner join', 'access_type': 'join', 'actual_last_row_ms': 20,
            'inputs': [{'operation': 'Table scan on t', 'access_type': 'table', 'table_name': 't',
                        'actual_last_row_ms': 4, 'actual_rows': 5},
                       {'operation': 'Table scan on t', 'access_type': 'table', 'table_name': 't',
                        'actual_last_row_ms': 9, 'actual_rows': 7}]}))
        rows = build_chart_data(root)['nodes']
        self.assertEqual(rows[1]['folded_label'].split(' rows=')[0], rows[2]['folded_label'].split(' rows=')[0])
        self.assertIn('Rows: 5 actual', rows[1]['flame_details'])
        self.assertIn('Rows: 7 actual', rows[2]['flame_details'])
        self.assertEqual(build_chart_data(plan())['nodes'][0]['flame_details'].splitlines()[0], 'Table: t')

    def test_non_finite_values_never_reach_json(self):
        root = plan(actual_last_row_ms=2)
        root['details']['estimated_total_cost'] = float('inf')
        root['details']['complexity'] = {'big_o': 'O(n)', 'weight': float('nan')}
        data = build_chart_data(root)
        self.assertIsNone(data['nodes'][0]['estimated_cost'])
        self.assertIsNone(data['nodes'][0]['complexity']['weight'])
        json.dumps(data, allow_nan=False)
        self.assertEqual(data['schema_version'], SCHEMA_VERSION)


class ChartEmbedTests(unittest.TestCase):
    def test_untrusted_labels_cannot_leave_the_data_script(self):
        root = plan(table_name='</script><script>alert(1)</script><!--', condition='  & > <')
        root['short_label'] = '</SCRIPT><img src=x onerror=alert(1)>'
        page = chart_embed(build_chart_data(root, title='</script>'), 'workbench')
        self.assertNotIn('<img', page)
        self.assertNotIn('<script>alert(1)', page)
        self.assertEqual(page.lower().count('</script'), page.lower().count('<script'))
        payload = re.search(r'<script type="application/json" class="mfc-data">(.*?)</script>', page, re.S).group(1)
        self.assertNotIn('<', payload)
        self.assertNotIn(' ', payload)
        data = json.loads(payload)
        self.assertEqual(data['nodes'][0]['short_label'], root['short_label'])
        self.assertEqual(data['nodes'][0]['condition'], '  & > <')
        self.assertEqual(json.loads(script_json({'a': '</script>'})), {'a': '</script>'})

    def test_bundle_is_inlined_once_and_mount_attributes_are_escaped(self):
        data = build_chart_data(plan())
        first = chart_embed(data, 'workbench', width=900, panels=('metrics',))
        second = chart_embed(data, 'workbench', runtime=False)
        runtime = chart_runtime()
        self.assertEqual(first.count(runtime), 1)
        self.assertNotIn(runtime, second)
        self.assertIn('MyflamesCharts.mountAll(document)', second)
        self.assertIn('data-mfc-width="900"', first)
        self.assertIn('data-mfc-panels="metrics"', first)
        self.assertIn('<noscript>', first)
        self.assertIn('data-mfc-view="&quot;&gt;"', chart_embed(data, '">', runtime=False))

    def test_mount_options_are_escaped_presentation_settings(self):
        data = build_chart_data(plan())
        page = chart_embed(data, 'flamegraph', options={'inverted': True, 'frame_height': 20}, runtime=False)
        self.assertIn('data-mfc-options="{&quot;frame_height&quot;:20,&quot;inverted&quot;:true}"', page)
        self.assertNotIn('data-mfc-options', chart_embed(data, 'tree', runtime=False))

    def test_html_report_mounts_every_view_with_python_semantics(self):
        from myflames.output_html_report import render_html_report
        text = (TEST / 'fixtures' / 'explain-001-table-scan-users-no-filter.json').read_text()
        for view in ('flamegraph', 'bargraph', 'tree', 'workbench', 'diagram'):
            with self.subTest(view=view):
                page = render_html_report(text, view_type=view, title='Report </script>',
                                          frame_height=20, inverted=True)
                self.assertEqual(page.count(chart_runtime()), 1)
                self.assertIn('data-mfc-view="%s"' % ('workbench' if view == 'diagram' else view), page)
                self.assertIn('<noscript><p class="mfc-noscript">', page)
                self.assertIn('id="chart-panel"', page)
                self.assertIn('application/ld+json', page)
                data = json.loads(re.search(r'<script type="application/json" class="mfc-data">(.*?)</script>',
                                            page, re.S).group(1))
                self.assertEqual(data['title'], 'Report </script>')
                self.assertTrue(any(n['teach_index'] is not None for n in data['nodes']), 'teach CTA indexes')
                self.assertTrue(any(n['highlight'] for n in data['nodes']), 'analysis highlights')
                if view == 'flamegraph':
                    self.assertIn('data-mfc-width="1800"', page)
                    self.assertIn('&quot;frame_height&quot;:20,&quot;inverted&quot;:true', page)

    def test_runtime_rejects_text_that_could_close_the_script(self):
        saved, cache = chart_assets.BUNDLE, dict(chart_assets._runtime_cache)
        try:
            for text in ('var a="</script>";', 'var a=1;<!-- x'):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory, 'bundle.js')
                    path.write_text(text, encoding='utf-8')
                    chart_assets.BUNDLE = path
                    chart_assets._runtime_cache.clear()
                    with self.assertRaises(ValueError):
                        chart_runtime()
        finally:
            chart_assets.BUNDLE = saved
            chart_assets._runtime_cache.clear()
            chart_assets._runtime_cache.update(cache)


class ChartBundleTests(unittest.TestCase):
    def test_bundle_banner_and_globals(self):
        text = chart_runtime()
        self.assertTrue(text.startswith('/*! myflames-charts sources-sha256='))
        self.assertRegex(chart_assets.bundle_sources_sha(), r'^[0-9a-f]{64}$')
        self.assertIn('MyflamesCharts', text[:600])
        for forbidden in ('innerHTML', 'react', 'd3-'):
            self.assertNotIn(forbidden, text, forbidden + ' must not be in the chart bundle')

    def test_bundle_registers_every_chart_view(self):
        text = chart_runtime()
        for view in ('"workbench"', '"diagram"', '"tree"', '"bargraph"', '"flamegraph"'):
            self.assertIn(view, text)
        for forbidden in ('prompt(', 'alert('):
            self.assertNotIn(forbidden, text, 'charts search inline; sandboxed frames need no allow-modals')

    def test_bundle_size_budget(self):
        size = chart_assets.BUNDLE.stat().st_size
        # Plan budget: 60 KB for all four charts. Above 80 KB, split into
        # per-chart entries instead of raising this limit.
        self.assertLess(size, 80 * 1024, 'myflames-charts.js is %d bytes' % size)

    @unittest.skipUnless((WORKSPACE / 'src' / 'charts').is_dir(), 'chart sources are not in this checkout')
    def test_committed_bundle_matches_sources(self):
        self.assertEqual(chart_assets.bundle_sources_sha(), sources_sha(),
                         'myflames/assets/myflames-charts.js is stale: run `npm run build` in apps/workspace')


if __name__ == '__main__':
    unittest.main()
