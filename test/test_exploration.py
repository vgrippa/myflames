"""Identity, truthful metric scales, and projection boundary tests."""
import copy
import json
import re
import unittest
from html.parser import HTMLParser

from myflames.chart_assets import chart_runtime
from myflames.exploration import BUNDLE_VIEWS, METRICS, VIEWS, metric_value, project_tree, render_exploration
from myflames.parser import parse_explain, flatten_nodes
from myflames.output_workbench import _phase


def sample():
    return parse_explain(json.dumps({
        'operation': 'Nested loop inner join', 'access_type': 'join',
        'actual_last_row_ms': 20, 'actual_rows': 10, 'estimated_rows': 5,
        'inputs': [
            {'operation': 'Table scan on same', 'access_type': 'table', 'table_name': 'same',
             'actual_last_row_ms': 3, 'actual_rows': 5, 'estimated_rows': 50},
            {'operation': 'Filter', 'actual_last_row_ms': 10, 'actual_rows': 2,
             'inputs': [{'operation': 'Table scan on same', 'access_type': 'table', 'table_name': 'same',
                         'actual_last_row_ms': 7, 'actual_rows': 3, 'estimated_rows': 0}]}]}))


class Tags(HTMLParser):
    def __init__(self, value):
        super().__init__()
        self.tags = []
        self.feed(value)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def chart_data(text):
    """The chart-data-1.0 payload of a bundle-rendered page."""
    payload = re.search(r'<script type="application/json" class="mfc-data">(.*?)</script>', text, re.S)
    return json.loads(payload.group(1))


class ExplorationTests(unittest.TestCase):
    def test_native_chart_identity_and_metric_list_all_views(self):
        root = sample()
        original = copy.deepcopy(root)
        self.assertEqual(set(BUNDLE_VIEWS), set(VIEWS), 'every exploration view is drawn by the bundle')
        for view in VIEWS:
            with self.subTest(view=view):
                text = render_exploration(root, view)
                # Drawn by myflames-charts.js; DOM identity, metric rows and
                # the bridge are exercised by test/charts.mjs, exploration.mjs
                # and parity.mjs in apps/workspace.
                data = chart_data(text)
                self.assertEqual([n['id'] for n in data['nodes']], [n['node_id'] for n in flatten_nodes(root)])
                self.assertEqual(text.count(chart_runtime()), 1)
                self.assertIn('data-mfc-view="%s"' % ('workbench' if view == 'diagram' else view), text)
                self.assertIn('data-mfc-panels="metrics"', text)
                self.assertIn('MyflamesCharts.mountAll(document)', text)
                for name in ('myflames-select', 'myflames-node'):
                    self.assertIn(name, chart_runtime())
                self.assertNotIn('__EMBED__', text)
        self.assertEqual(root, original)

    def test_diagram_alias_shares_focus_collapse_and_selection(self):
        root = sample()
        branch = root['children'][1]
        options = dict(width=1320, focus_id=branch['node_id'],
                       collapsed_ids=[branch['node_id']], selected_id=branch['node_id'],
                       metric='rows')
        self.assertEqual(render_exploration(root, 'diagram', **options),
                         render_exploration(root, 'workbench', **options))

    def test_duplicate_flame_labels_have_distinct_identity(self):
        root = sample()
        root['children'][1] = copy.deepcopy(root['children'][0])
        root['children'][1]['node_id'] = 'n:0123456789ab'
        text = render_exploration(root, 'flamegraph')
        nodes = chart_data(text)['nodes']
        # The bundle draws one frame per node ID (charts.mjs, parity.mjs).
        self.assertEqual([n['id'] for n in nodes], [n['node_id'] for n in flatten_nodes(root)])
        self.assertEqual(nodes[1]['folded_label'], nodes[2]['folded_label'])
        self.assertNotEqual(nodes[1]['id'], nodes[2]['id'])
        self.assertNotIn('operator:n:', text)

    def test_microsecond_chart_labels_and_zero_time_are_truthful(self):
        root = parse_explain('{"operation":"Table scan","actual_last_row_ms":0.2}')
        data = chart_data(render_exploration(root, 'flamegraph'))
        # The flame graph prints "(200 µs, 100.00%)" (charts.mjs); values stay ms.
        self.assertEqual(data['display_unit'], 'µs')
        self.assertEqual(data['nodes'][0]['total_ms'], 0.2)
        self.assertEqual(data['nodes'][0]['metrics']['total_time'], [0.2, '0.2 ms'])
        for value in ('0', 'null'):
            root = parse_explain('{"operation":"Table scan","actual_last_row_ms":' + value + '}')
            node = chart_data(render_exploration(root, 'flamegraph'))['nodes'][0]
            # No whole display unit of time: the bundle shows "No positive time".
            self.assertIn(node['self_ms'], (0, None))
            self.assertIn('No positive time', chart_runtime())

    def test_projection_preserves_identity_and_measurements(self):
        root = sample()
        before = copy.deepcopy(root)
        branch = root['children'][1]
        projected = project_tree(root, branch['node_id'], [branch['node_id']])
        self.assertEqual(projected['node_id'], branch['node_id'])
        self.assertEqual(projected['self_time'], 3)
        self.assertEqual(projected['total_time'], 10)
        self.assertEqual(projected['children'], [])
        self.assertEqual(projected['_hidden_descendants'], 1)
        self.assertEqual(root, before)
        with self.assertRaises(ValueError):
            project_tree(root, 'missing')
        with self.assertRaises(ValueError):
            project_tree(root, collapsed_ids=['missing'])
        with self.assertRaises(ValueError):
            project_tree(root, collapsed_ids='not-a-list')

    def test_metrics_distinguish_missing_zero_and_direction(self):
        root = sample()
        self.assertEqual(metric_value(root, 'self_time'), (7, '7 ms'))
        self.assertEqual(metric_value(root, 'total_time'), (20, '20 ms'))
        self.assertEqual(metric_value(root, 'rows'), (10, '10 rows'))
        self.assertEqual(metric_value(root, 'estimate_error'), (2, '2\u00d7 underestimated'))
        self.assertEqual(metric_value(root['children'][0], 'estimate_error'), (10, '10\u00d7 overestimated'))
        self.assertEqual(metric_value(root['children'][1]['children'][0], 'estimate_error'), (None, 'Zero estimate'))
        missing = parse_explain('{"operation":"Table scan"}')
        self.assertEqual(metric_value(missing, 'self_time'), (None, 'Not measured'))
        self.assertEqual(metric_value(missing, 'rows'), (None, 'Not measured'))
        missing['details'].update(actual_rows=0, estimated_rows=0, actual_last_row_ms=0)
        self.assertEqual(metric_value(missing, 'rows'), (0, '0 rows'))
        self.assertEqual(metric_value(missing, 'estimate_error'), (1, '1\u00d7 exact'))
        for metric in METRICS:
            self.assertIn(METRICS[metric][0], render_exploration(root, 'tree', metric=metric))
        with self.assertRaises(ValueError):
            render_exploration(root, 'tree', metric='cost')

    def test_untrusted_labels_cannot_break_document(self):
        root = sample()
        root['short_label'] = '</button><img src=x onerror=alert(1)>'
        root['full_label'] = '</script><script>alert(1)</script>'
        root['folded_label'] = root['full_label']
        for view in VIEWS:
            text = render_exploration(root, view, selected_id='"><img src=x>')
            self.assertNotIn('<img', text)
            self.assertNotIn('<script>alert(1)</script>', text)

    def test_select_list_branches_preserve_regular_order_and_timing(self):
        root = parse_explain(json.dumps({
            'operation': 'Nested loop inner join', 'access_type': 'join', 'actual_last_row_ms': 10,
            'inputs': [ {'operation': 'Outer', 'actual_last_row_ms': 2},
                       {'operation': 'Inner', 'actual_last_row_ms': 3}],
            'inputs_from_select_list': [{'operation': 'Scalar subquery', 'actual_last_row_ms': 8,
                                        'inputs': [{'operation': 'Lookup', 'actual_last_row_ms': 4}]}]}))
        self.assertEqual([n['full_label'] for n in root['children']], ['Outer', 'Inner', 'Scalar subquery'])
        self.assertEqual(root['self_time'], 5)
        self.assertEqual(root['children'][2]['self_time'], 4)
        self.assertEqual(root['children'][2]['details']['input_kind'], 'select_list')
        self.assertEqual(_phase(root, root['children'][2], 2), 'select-list subquery')
        self.assertEqual(len({n['node_id'] for n in flatten_nodes(root)}), 5)
        rows = chart_data(render_exploration(root, 'workbench'))['nodes']
        self.assertEqual([(r['input_kind'], r['phase']) for r in rows if r['parent_id'] == root['node_id']],
                         [('input', 'outer'), ('input', 'inner'), ('select_list', 'select-list subquery')])
        self.assertIn('SELECT-list subquery', render_exploration(root, 'tree'))

    def test_bundle_views_carry_projection_without_rewriting_the_tree(self):
        root = sample()
        branch = root['children'][1]['node_id']
        text = render_exploration(root, 'workbench', focus_id=branch, collapsed_ids=[branch],
                                  selected_id=branch, metric='estimate_error')
        data = chart_data(text)
        self.assertEqual(data['projection'], {'focus_id': branch, 'collapsed_ids': [branch],
                                              'selected_id': branch, 'metric': 'estimate_error'})
        self.assertEqual(len(data['nodes']), 4, 'the canonical tree is serialized; the bundle projects a copy')
        self.assertEqual(data['nodes'][2]['metrics']['self_time'], [3, '3 ms'])
        with self.assertRaises(ValueError):
            render_exploration(root, 'workbench', focus_id='n:missing')


if __name__ == '__main__':
    unittest.main()
