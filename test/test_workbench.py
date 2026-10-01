"""Workbench projection keeps the canonical tree and measurement semantics."""
import copy
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

from myflames.parser import parse_explain, flatten_nodes
from myflames.output_workbench import render_workbench, _layout, _kind, _phase, _arrow_width, _join_type

SVG = '{http://www.w3.org/2000/svg}'
TEST = Path(__file__).resolve().parent


def plan(op, inputs=None, **fields):
    import json
    value = dict(operation=op, actual_rows=3, actual_loops=1, actual_last_row_ms=4)
    value.update(fields)
    if inputs is not None:
        value['inputs'] = inputs
    return parse_explain(json.dumps(value))


class TestWorkbench(unittest.TestCase):
    def test_zoom_controls_are_labeled_and_have_feedback_styles(self):
        document = ET.fromstring(render_workbench(plan('Table scan on t')))
        controls = {item.attrib['data-action']: item
                    for item in document.iter()
                    if 'data-action' in item.attrib}
        self.assertEqual(controls['in'].attrib['aria-label'], 'Zoom in')
        self.assertEqual(controls['out'].attrib['aria-label'], 'Zoom out')
        self.assertEqual(controls['reset'].attrib['aria-label'], 'Reset zoom to 100%')
        self.assertIn('wb-activated', document.find(SVG+'style').text)

    def test_every_operator_and_edge_survives_complex_plans(self):
        for filename in ('fixtures/explain-044-join-3t-products-category-reviews.json',
                         'fixtures/explain-065-complex-join-agg-sort.json',
                         'fixtures/explain-061-union-all-us-uk-users.json',
                         'mariadb-explain-complex.json', 'mariadb-explain-index-merge.json'):
            with self.subTest(filename=filename):
                root = parse_explain((TEST / filename).read_text())
                original = copy.deepcopy(root)
                svg = ET.fromstring(render_workbench(root))
                node_ids = [g.attrib['data-node-id'] for g in svg.iter(SVG+'g') if 'data-node-id' in g.attrib]
                self.assertEqual(node_ids, [n['node_id'] for n in flatten_nodes(root)])
                edges = [p for p in svg.iter(SVG+'path') if p.attrib.get('class') == 'wb-edge']
                expected = {(child['node_id'], parent['node_id']) for parent in flatten_nodes(root) for child in parent['children']}
                self.assertEqual({(p.attrib['data-from'], p.attrib['data-to']) for p in edges}, expected)
                self.assertEqual(len(edges), len(node_ids)-1)
                self.assertEqual(root, original)
                self.assertEqual(float(svg.attrib['height']), float(svg.attrib['viewBox'].split()[3]))

    def test_layout_preserves_order_and_has_no_box_collisions(self):
        import json
        source = lambda name: {'operation':'Table scan on '+name, 'access_type':'table'}
        root = parse_explain(json.dumps({'operation':'Append', 'access_type':'append', 'inputs':[
            {'operation':'Filter', 'inputs':[source('left')]}, source('middle'),
            {'operation':'Materialize', 'inputs':[source('right'), source('extra')]}]}))
        nodes, edges, width, height = _layout(root)
        from myflames.output_workbench import BOX_W
        for i, a in enumerate(nodes):
            self.assertGreaterEqual(a['x'], 0)
            self.assertLessEqual(a['x']+BOX_W, width)
            self.assertLessEqual(a['y']+a['height'], height)
            for b in nodes[i+1:]:
                collision = a['x'] < b['x']+BOX_W and b['x'] < a['x']+BOX_W and a['y'] < b['y']+b['height'] and b['y'] < a['y']+a['height']
                self.assertFalse(collision)
        for edge in edges:
            self.assertLess(edge['from']['y'], edge['to']['y'])
        children = [next(item for item in nodes if item['node'] is child) for child in root['children']]
        self.assertEqual([item['x'] for item in children], sorted(item['x'] for item in children))
        self.assertEqual(_kind(root), 'operation')

    def test_cost_rows_and_times_remain_distinct(self):
        root = plan('Index lookup on t', access_type='index', index_access_type='index_lookup',
                    actual_last_row_ms=.5, actual_loops=4, actual_rows=0,
                    estimated_rows=10000, estimated_total_cost=123)
        svg = render_workbench(root)
        self.assertIn('Actual rows / loop: 0', svg)
        self.assertIn('Estimated rows / loop: 10000', svg)
        self.assertIn('Actual last row (ms / loop): 0.5', svg)
        self.assertIn('Total across loops (ms): 2.0', svg)
        self.assertIn('Estimated optimizer cost: 123', svg)
        self.assertIn('Self 2.00 ms', svg)
        missing = plan('Constant row', actual_rows=None, actual_last_row_ms=None)
        text = render_workbench(missing)
        self.assertIn('Self n/a · total n/a', text)
        self.assertIn('Est. cost n/a', text)
        self.assertNotIn('Actual rows / loop:', text)

    def test_color_is_operation_based_and_arrow_width_is_estimated(self):
        self.assertEqual(_kind(plan('Table scan on join_data', access_type='table')), 'table')
        self.assertEqual(_kind(plan('Index range scan on t', access_type='index', index_access_type='index_range_scan')), 'range')
        self.assertEqual(_kind(plan('Index lookup on t')), 'lookup')
        self.assertEqual(_kind(plan('Covering index range scan on t')), 'range')
        self.assertEqual(_kind(plan('Hash semijoin')), 'hash')
        self.assertEqual(_kind(plan('Sort: t.a', access_type='sort')), 'operation')
        first = plan('Table scan on t', estimated_rows=1, actual_rows=1000000)
        second = plan('Table scan on t', estimated_rows=1000000, actual_rows=1)
        self.assertLess(_arrow_width(first), _arrow_width(second))
        self.assertEqual(_arrow_width(plan('Table scan on t')), 1)
        self.assertEqual(_arrow_width(plan('Table scan on t', estimated_rows=0)), 1)

    def test_join_phase_requires_evidence_and_does_not_turn_append_into_join(self):
        hash_plan = plan('Inner hash join', inputs=[{'operation':'Table scan on a'}, {'operation':'Hash', 'inputs':[{'operation':'Table scan on b'}]}], join_algorithm='hash')
        self.assertEqual(_phase(hash_plan, hash_plan['children'][0], 0), 'probe')
        self.assertEqual(_phase(hash_plan, hash_plan['children'][1], 1), 'build')
        hash_plan['children'][1]['details']['operation'] = 'Table scan on b'
        hash_plan['children'][1]['full_label'] = 'Table scan on b'
        self.assertEqual(_phase(hash_plan, hash_plan['children'][1], 1), 'input 2')
        self.assertEqual(_kind(plan('Append', inputs=[{'operation':'a'}, {'operation':'b'}])), 'operation')

    def test_escape_labels_attributes_and_details(self):
        dangerous = '</text><script>alert("x")</script>&\'"☺'
        root = plan('Table scan on '+dangerous, table_name=dangerous, condition=dangerous)
        svg = render_workbench(root, title=dangerous)
        document = ET.fromstring(svg)
        scripts = list(document.iter(SVG+'script'))
        self.assertEqual(len(scripts), 1)
        nodes = [n for n in document.iter(SVG+'g') if n.attrib.get('class') == 'wb-node']
        self.assertIn(dangerous, nodes[0].attrib['data-details'])
        self.assertNotIn('<script>alert', svg)

    def test_join_semantics_preserved_from_metadata_not_condition_keywords(self):
        root = plan('Nested loop inner join (left join text)', join_type='inner join')
        self.assertEqual(root['details']['join_type'], 'inner join')
        self.assertEqual(_join_type(root), 'inner')
        self.assertEqual(_join_type(plan('Nested loop inner join (FirstMatch)', join_type='inner join')), 'inner')
        self.assertEqual(_join_type(plan('Left hash join', join_type='unknown')), 'unknown')
        self.assertEqual(_join_type(plan('Filter: left join on t')), 'unknown')
        self.assertEqual(_kind(plan('Table scan on left join')), 'table')

    def test_legacy_operation_prefixes(self):
        for operation, expected in [
                ('Nested loop left join', 'left'), ('Left hash join (a.id = b.id)', 'left'),
                ('Hash antijoin', 'anti'), ('Hash semijoin (FirstMatch)', 'semi'),
                ('Batched key access left join', 'left'), ('Nested loop join', 'unknown'),
                ('Inner hash join', 'inner'), ('Right hash join', 'right')]:
            with self.subTest(operation=operation):
                self.assertEqual(_join_type(plan(operation)), expected)

    def test_large_venn_regions_match_join_semantics(self):
        for join_type, region in [('left join', 'left'), ('right join', 'right'),
                                  ('inner join', 'overlap'), ('semijoin', 'overlap'),
                                  ('antijoin', 'left'), ('unknown', None)]:
            with self.subTest(join_type=join_type):
                root = plan('Nested loop join', join_type=join_type, access_type='join')
                svg = ET.fromstring(render_workbench(root))
                symbol = next(g for g in svg.iter(SVG+'g') if g.attrib.get('class') == 'wb-venn')
                highlights = [g for g in symbol if g.attrib.get('class') == 'wb-join-highlight']
                self.assertEqual([g.attrib['data-region'] for g in highlights], [region] if region else [])
                self.assertTrue(all(float(c.attrib['r']) >= 40 for c in symbol.iter(SVG+'circle')))
                excluded = [g for g in symbol if g.attrib.get('data-region') == 'excluded-overlap']
                self.assertEqual(bool(excluded), join_type == 'antijoin')
                self.assertIn('aria-label', symbol.attrib)
                self.assertIn('not row counts or duplicates', render_workbench(root))

    def test_real_left_and_inner_plans_have_correct_regions(self):
        for filename, expected in [('explain-040-left-join-products-reviews.json', 'left'),
                                   ('explain-044-join-3t-products-category-reviews.json', 'inner')]:
            root = parse_explain((TEST / 'fixtures' / filename).read_text())
            joins = [node for node in flatten_nodes(root) if _kind(node) in ('nested_loop', 'hash', 'bka')]
            self.assertTrue(joins)
            self.assertTrue(all(_join_type(node) == expected for node in joins))

    def test_mariadb_synthetic_join_does_not_invent_inner_semantics(self):
        root = parse_explain((TEST / 'mariadb-explain-complex.json').read_text())
        joins = [node for node in flatten_nodes(root) if node['details']['join_algorithm'] == 'nested_loop']
        self.assertTrue(joins)
        self.assertTrue(all(_join_type(node) == 'unknown' for node in joins))

    def test_join_cards_have_room_for_symbols_and_connect_at_their_edges(self):
        root = plan('Nested loop left join', inputs=[
            {'operation': 'Table scan on a'},
            {'operation': 'Nested loop inner join', 'inputs': [
                {'operation': 'Table scan on b'}, {'operation': 'Table scan on c'}]}])
        nodes, edges, width, height = _layout(root)
        self.assertGreater(nodes[0]['height'], nodes[1]['height'])
        self.assertEqual(nodes[0]['y'] + nodes[0]['height'], height)
        for edge in edges:
            self.assertGreaterEqual(edge['to']['y'] - (edge['from']['y'] + edge['from']['height']), 90)

    def test_programmatic_render_alias_includes_same_analysis(self):
        from myflames.render import render_explain
        raw = (TEST / 'fixtures' / 'explain-040-left-join-products-reviews.json').read_text()
        diagram = render_explain(raw, output_type='diagram')
        self.assertEqual(diagram, render_explain(raw, output_type='workbench'))
        self.assertIn('data-join-type="left"', diagram)
        self.assertIn('Query Analysis', diagram)
