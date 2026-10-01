"""Identity, truthful metric scales, and projection boundary tests."""
import copy
import json
import re
import unittest
from html.parser import HTMLParser

from myflames.exploration import METRICS, VIEWS, metric_value, project_tree, render_exploration
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


class ExplorationTests(unittest.TestCase):
    def test_native_chart_identity_and_metric_list_all_six_views(self):
        root = sample()
        original = copy.deepcopy(root)
        ids = {node['node_id'] for node in flatten_nodes(root)}
        for view in VIEWS:
            with self.subTest(view=view):
                text = render_exploration(root, view)
                tags = Tags(text).tags
                native_ids = {attrs.get('data-node-id') for tag, attrs in tags
                              if tag != 'button' and attrs.get('data-node-id')}
                self.assertEqual(native_ids, ids)
                self.assertEqual({attrs['data-node-id'] for tag, attrs in tags
                                  if attrs.get('class') == 'metric-row'}, ids)
                self.assertIn('myflames-select', text)
                self.assertIn('myflames-node', text)
                self.assertIn('event.source !== window.parent', text)
                self.assertNotIn('__SVG__', text)
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
        root['children'][1]['node_id'] = 'n:duplicate'
        text = render_exploration(root, 'flamegraph')
        native = [a['data-node-id'] for t, a in Tags(text).tags if t == 'g' and 'data-node-id' in a]
        self.assertEqual(set(native), {n['node_id'] for n in flatten_nodes(root)})
        self.assertNotIn('operator:n:', re.sub(r'<script.*?</script>', '', text, flags=re.S))

    def test_microsecond_chart_labels_and_zero_time_are_truthful(self):
        root = parse_explain('{"operation":"Table scan","actual_last_row_ms":0.2}')
        text = render_exploration(root, 'flamegraph')
        self.assertIn('200 µs', text)
        self.assertNotIn('(200 ms,', text)
        self.assertIn('0.2 ms', text)
        for value in ('0', 'null'):
            root = parse_explain('{"operation":"Table scan","actual_last_row_ms":' + value + '}')
            text = render_exploration(root, 'flamegraph')
            self.assertIn('No positive time', text)
            self.assertNotIn('(1 ms', text)

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
        self.assertIn('SELECT-list subquery', render_exploration(root, 'workbench'))


if __name__ == '__main__':
    unittest.main()
