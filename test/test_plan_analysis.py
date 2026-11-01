"""Plan statistics, occurrence identity and copy-only sharing privacy."""
import copy
import json
import unittest
from pathlib import Path

from myflames.chart_data import build_chart_data
from myflames.parser import parse_explain, hide_plan_json_literals, hide_parsed_plan_literals, flatten_nodes
from myflames.plan_privacy import hide_literals, hide_plan_literals, hide_analysis_literals, LITERAL_FIELDS

REPO = Path(__file__).resolve().parent.parent


def scan(table='orders', ms=2, loops=1):
    return {'operation': 'Table scan on ' + table, 'table_name': table,
            'access_type': 'table', 'actual_last_row_ms': ms,
            'actual_rows': 4, 'actual_loops': loops, 'estimated_rows': 5}


class StatsTests(unittest.TestCase):
    def test_measured_self_time_rows_loops_and_groups(self):
        raw = {'operation': 'Union all', 'actual_last_row_ms': 12,
               'actual_loops': 1, 'inputs': [scan(loops=2), scan(ms=3), scan('customers')]}
        root = parse_explain(json.dumps(raw))
        before = copy.deepcopy(root)
        for compact in (False, True):
            data = build_chart_data(root, compact=compact)
            tables = {row['table']: row for row in data['stats']['tables']}
            orders = tables['orders']
            self.assertEqual(orders['self_ms'], 7)
            self.assertAlmostEqual(orders['share'], 7 / 12)
            self.assertEqual(orders['count'], 2)
            self.assertEqual(orders['loops'], 3)
            self.assertEqual(orders['actual_rows'], 12)
            self.assertEqual(orders['estimated_rows'], 10)
            self.assertEqual(orders['operators'], ['Table scan'])
            types = {row['label']: row for row in data['stats']['operators']}
            self.assertEqual(types['Table scan']['count'], 3)
            self.assertEqual(types['Table scan']['self_ms'], 9)
            self.assertEqual(sum(row['self_ms'] for row in types.values()), 12)
            nodes = data['nodes']
            self.assertEqual(nodes[1]['group'], nodes[2]['group'])
            self.assertNotEqual(nodes[1]['group'], nodes[3]['group'])
            group = data['groups'][nodes[1]['group']]
            self.assertEqual(group['node_ids'], [nodes[1]['id'], nodes[2]['id']])
            self.assertEqual(group['self_ms'], 7)
            self.assertEqual(nodes[0]['children'], [row['id'] for row in nodes[1:]])
        self.assertEqual(root, before)

    def test_index_and_schema_identity_and_values_do_not_establish_group(self):
        raw = {'operation': 'Union all', 'inputs': [
            dict(scan(), operation='Index lookup on orders using k (id = 12)', index_name='k'),
            dict(scan(), operation='Index lookup on orders using k (id = 99)', index_name='k'),
            dict(scan(), operation='Index lookup on orders using k2 (id = 12)', index_name='k2'),
            dict(scan(), operation='Index lookup on orders using k (id = 12)', index_name='k', schema_name='archive')]}
        nodes = build_chart_data(parse_explain(json.dumps(raw)))['nodes'][1:]
        self.assertEqual(nodes[0]['group'], nodes[1]['group'])
        self.assertNotEqual(nodes[0]['group'], nodes[2]['group'])
        self.assertNotEqual(nodes[0]['group'], nodes[3]['group'])

    def test_mysql_and_mariadb_fixtures_share_canonical_time(self):
        for name in ('mysql-explain-hash-join.json', 'mariadb-explain-block-nl-join.json'):
            with self.subTest(name=name):
                root = parse_explain((REPO / 'test' / name).read_text())
                data = build_chart_data(root)
                canonical = {n['node_id']: n for n in flatten_nodes(root)}
                for row in data['stats']['operators'] + data['stats']['tables']:
                    values = [canonical[key]['self_time'] for key in row['node_ids']]
                    if all(canonical[key]['details'].get('actual_last_row_ms') is not None for key in row['node_ids']):
                        self.assertAlmostEqual(row['self_ms'], sum(values))
                        self.assertAlmostEqual(row['share'], sum(values) / root['total_time'])
                self.assertEqual([n['id'] for n in data['nodes']], list(canonical))

    def test_estimated_mysql_and_mariadb_have_counts_without_invented_time(self):
        raw_plans = [
            {'operation': 'Table scan on orders', 'table_name': 'orders', 'estimated_rows': 42},
            {'query_block': {'select_id': 1, 'nested_loop': [{'table': {
                'table_name': 'orders', 'access_type': 'ALL', 'rows': 42, 'filtered': 100}}]}}]
        for raw in raw_plans:
            with self.subTest(raw=raw):
                data = build_chart_data(parse_explain(json.dumps(raw)))
                self.assertFalse(data['timing_available'])
                self.assertIn('Not measured', data['stats']['note'])
                row = data['stats']['tables'][0]
                self.assertEqual(row['count'], 1)
                self.assertEqual(row['estimated_rows'], 42)
                for key in ('self_ms', 'share', 'actual_rows', 'loops'):
                    self.assertIsNone(row[key], key)

    def test_mariadb_rows_produced_apply_filter_and_loops(self):
        raw = {'query_block': {'select_id': 1, 'r_total_time_ms': 4, 'nested_loop': [{'table': {
            'table_name': 'orders', 'access_type': 'ALL', 'rows': 20, 'r_rows': 10,
            'r_loops': 2, 'r_filtered': 30, 'r_table_time_ms': 2}}]}}
        row = build_chart_data(parse_explain(json.dumps(raw)))['stats']['tables'][0]
        self.assertEqual(row['actual_rows'], 6)
        self.assertEqual(row['loops'], 2)

    def test_zero_root_and_missing_counters_stay_unknown(self):
        row = build_chart_data(parse_explain(json.dumps(dict(scan(), actual_last_row_ms=0, actual_rows=None))))['stats']['tables'][0]
        self.assertEqual(row['self_ms'], 0)
        self.assertIsNone(row['share'])
        self.assertIsNone(row['actual_rows'])


class LiteralTests(unittest.TestCase):
    def test_generated_prose_hides_values_without_hiding_measurements(self):
        cases = [
            ('Query runs 7 operators; examines ~1,234 rows to return 20 in 12.5 ms. No warnings.',
             'Query runs 7 operators; examines ~1,234 rows to return 20 in 12.5 ms. No warnings.'),
            ("Filter (status = 'paid')", 'Filter (status = ?)'),
            ('Filter: (o.total > 10)', 'Filter: (o.total > ?)'),
            ('examines ~1,234 rows', 'examines ~1,234 rows'),
            ('Index range scan on idx (id BETWEEN 5 AND 9)',
             'Index range scan on idx (id BETWEEN ? AND ?)'),
            ('Runs 2 loops (50%) on MySQL 8.4.7 in 0.25 ms.',
             'Runs 2 loops (50%) on MySQL 8.4.7 in 0.25 ms.'),
            ('Filter: id = 10; returns 4 rows in 2 ms.',
             'Filter: id = ?; returns 4 rows in 2 ms.'),
            ('Filter: 10 <= id AND id != -20; returns 4 rows.',
             'Filter: ? <= id AND id != -?; returns 4 rows.'),
            ('Filter: id IN (1, -2, 3e-4); 12 operators.',
             'Filter: id IN (?, -?, ?); 12 operators.'),
            ('Filter: id NOT IN (0xCAFE, 0b10); 2 loops.',
             'Filter: id NOT IN (?, ?); 2 loops.'),
            ('Filter: id BETWEEN -5 AND +9; 3 rows.',
             'Filter: id BETWEEN -? AND +?; 3 rows.'),
            ('Filter: code NOT LIKE 123; 4 rows.', 'Filter: code NOT LIKE ?; 4 rows.'),
            ('Filter: (coalesce(id, 5) >= 10); 2 operators.',
             'Filter: (coalesce(id, ?) >= ?); 2 operators.'),
            ('Filter: "o"."status" = "paid"; examines 30 rows.',
             'Filter: ?.? = ?; examines 30 rows.'),
            ('Filter: "status" = \'paid\'; examines 30 rows.',
             'Filter: ? = ?; examines 30 rows.'),
            ('Value "private" and \'secret\'; 4 rows.', 'Value ? and ?; 4 rows.'),
            ('Filter: (`column42` = 9); 42 rows.', 'Filter: (`column42` = ?); 42 rows.'),
            ('Filter: "id = 42"; 4 rows.', 'Filter: ?; 4 rows.'),
            ("Query #12 doesn't use an index; 42 rows.", "Query #12 doesn't use an index; 42 rows."),
            ("The operators' timings cover 42 rows.", "The operators' timings cover 42 rows."),
            ('Value "private". 4 rows.', 'Value ?. 4 rows.'),
            ('Value "private" not recorded; 4 rows.', 'Value ? not recorded; 4 rows.'),
        ]
        for operator in ('=', '<>', '!=', '<', '>', '<=', '>=', '<=>', 'like', 'NOT LIKE'):
            cases.append(('Filter: id {} 42; 4 rows.'.format(operator),
                          'Filter: id {} ?; 4 rows.'.format(operator)))
        for field in ('executive_summary', 'why', 'action', 'rationale', 'note',
                      'reason', 'text', 'message', 'explanation', 'description', 'hint'):
            for original, expected in cases:
                with self.subTest(field=field, original=original):
                    self.assertEqual(hide_analysis_literals({field: original}), {field: expected})

    def test_sql_fields_keep_full_literal_hiding_and_prose_lists_keep_counts(self):
        for field in ('raw', 'beautified', 'query_sql', 'operation', 'short_label',
                      'full_label', 'folded_label', 'node_labels', 'label', 'condition',
                      'ranges', 'sort_key'):
            with self.subTest(field=field):
                self.assertEqual(hide_analysis_literals({field: ['SELECT 10, \'secret\'']}),
                                 {field: ['SELECT ?, ?']})
        self.assertEqual(hide_analysis_literals({'note': ['Scans 42 rows.', 'id = 42']}),
                         {'note': ['Scans 42 rows.', 'id = ?']})
        self.assertEqual(hide_plan_literals({'message': "Scans 42 rows; id = 9"}),
                         {'message': 'Scans 42 rows; id = ?'})

    def test_tricky_sql_table(self):
        cases = [
            ("SELECT 'a''b', \"x\" FROM `123` WHERE id IN (1, -2, 3e-4)", "SELECT ?, ? FROM `123` WHERE id IN (?, -?, ?)", True),
            (r"SELECT 'a\'b', 'c\\d', 1", "SELECT ?, ?, ?", True),
            (r"SELECT 'a\', 'b''c', 1", "SELECT ?, ?, ?", False),
            ("SELECT 0xCAFE, X'ABCD', b'0101', 0b10, .5, 5., -0.2, +3", "SELECT ?, ?, ?, ?, ?, ?, -?, +?", True),
            ("SELECT DATE'2025-01-02', TIMESTAMP '2025-01-02 10:20:30', INTERVAL 3 DAY", "SELECT DATE?, TIMESTAMP ?, INTERVAL ? DAY", True),
            ("SELECT _utf8mb4'private', N'private', ?, :p1, @value1", "SELECT ?, ?, ?, :p1, @value1", True),
            ("SELECT `12``3`, `t123`.`42`, column123, 123column, e10 FROM t1", "SELECT `12``3`, `t123`.`42`, column123, 123column, e10 FROM t1", True),
            ("SELECT 1 /* secret 555 */, 2 -- secret 'x'\nFROM t # private\nWHERE x=3", "SELECT ? ?, ? ?\nFROM t ?\nWHERE x=?", True),
            ("SELECT 1--2, /*!80000 private('secret') */ 3", "SELECT ?--?, ? ?", True),
            ("SELECT 'unterminated secret", "SELECT ?", True),
        ]
        for sql, expected, mode in cases:
            with self.subTest(sql=sql, mode=mode):
                self.assertEqual(hide_literals(sql, backslash_escapes=mode), expected)

    def test_unknown_mode_hides_both_interpretations(self):
        from myflames.plan_privacy import hide_prose_literals
        sql = r"SELECT 'one\', secret_word, 'two'"
        result = hide_literals(sql)
        self.assertNotIn('one', result)
        self.assertNotIn('two', result)
        self.assertNotIn('secret_word', result)
        self.assertEqual(hide_literals('SELECT "col123", 7', ansi_quotes=True), 'SELECT "col123", ?')
        self.assertEqual(hide_prose_literals('Filter: "col123" = 7', ansi_quotes=True),
                         'Filter: "col123" = ?')

    def test_raw_plan_fields_redacted_without_touching_identifiers_or_measurements(self):
        source = dict(scan('table42'), condition="id=999 AND s='secret-token'",
                      ranges=["1 <= id <= 9"], query="SELECT * FROM `table42` WHERE s='secret-token'")
        source['operation'] += " (s = 'secret-token')"
        source['nested'] = {'attached_condition': "s='secret-token'", 'actual_rows': 123}
        before = copy.deepcopy(source)
        result = json.loads(hide_plan_json_literals(json.dumps(source)))
        self.assertNotIn('secret-token', json.dumps(result))
        self.assertEqual(result['table_name'], 'table42')
        self.assertEqual(result['actual_rows'], 4)
        self.assertEqual(result['ranges'], ['? <= id <= ?'])
        self.assertEqual(source, before)
        self.assertEqual(hide_plan_literals(source), result)
        for field in LITERAL_FIELDS:
            self.assertEqual(hide_plan_literals({field: "'private-value'"})[field], '?')

    def test_canonical_redaction_preserves_numeric_identifiers_metrics_and_ids(self):
        root = parse_explain(json.dumps(dict(scan('42'), condition="id=123 AND name='private-token'")))
        before = copy.deepcopy(root)
        hidden = hide_parsed_plan_literals(root)
        self.assertEqual(hidden['node_id'], root['node_id'])
        self.assertEqual(hidden['short_label'], root['short_label'])
        self.assertEqual(hidden['self_time'], root['self_time'])
        self.assertIn('42', hidden['folded_label'])
        self.assertNotIn('private-token', json.dumps(hidden))
        self.assertEqual(root, before)

    def test_prefixed_literals_scan_without_recursion(self):
        result = hide_literals(','.join(["X'abcdef'"] * 2000))
        self.assertEqual(result, ','.join(['?'] * 2000))

    def test_html_and_explore_hide_embedded_payloads(self):
        from myflames.output_html_report import render_html_report
        from myflames.exploration import render_exploration
        text = json.dumps(dict(scan(), operation="Filter: (name='secret-token')", condition="name='secret-token'"))
        html = render_html_report(text, query_text="SELECT * FROM orders WHERE name='secret-token'", hide_literals=True)
        self.assertNotIn('secret-token', html)
        self.assertIn('Literal values hidden', html)
        self.assertIn('Time per table', html)
        self.assertIn('Time per operator type', html)
        explore = render_exploration(parse_explain(text), 'tree', hide_literals=True)
        self.assertNotIn('secret-token', explore)
        self.assertIn('Literal values hidden', explore)
        self.assertIn('secret-token', text)


class ShareEndpointTests(unittest.TestCase):
    def test_collected_schema_hides_expressions_and_preserves_structure(self):
        from myflames.collectors import parse_show_create_table
        ddl = "CREATE TABLE `42` (\n `123` varchar(20) DEFAULT 'DDL_SECRET'\n) ENGINE=InnoDB"
        schema = parse_show_create_table(ddl)
        schema.update({'generation_expression': "concat('GENERATED_SECRET', `123`)",
                       'check_clause': "`123` != 'CHECK_SECRET'",
                       'partition_expression': 'id + 987654321',
                       'view_definition': "SELECT 'VIEW_SECRET'",
                       'routine_definition': "RETURN 'ROUTINE_SECRET'"})
        schema['columns'][0]['column_default'] = 'BARE_DEFAULT_SECRET'
        schema['columns'].append({'name': '456', 'type': 'int', 'default': 987654321})
        schema['indexes'] = [{'name': '789', 'columns': ['123', "concat(`123`, 'INDEX_SECRET')"],
            'parts': [{'name': '123', 'expression': False, 'length': 20},
                      {'name': "concat(`123`, 'INDEX_SECRET')", 'expression': True, 'length': None}]}]
        original = {'collected': {'schema': {'42': schema}}}
        before = copy.deepcopy(original)
        result = hide_analysis_literals(original)['collected']['schema']['42']
        self.assertEqual(result['table_name'], '42')
        self.assertEqual(result['columns'][0]['name'], '123')
        self.assertEqual(result['columns'][0]['type'], 'varchar(20)')
        self.assertEqual(result['indexes'][0]['name'], '789')
        self.assertEqual(result['indexes'][0]['columns'][0], '123')
        self.assertEqual(result['indexes'][0]['parts'][0]['length'], 20)
        for secret in ('DDL_SECRET', 'GENERATED_SECRET', 'CHECK_SECRET',
                       '987654321', 'VIEW_SECRET', 'ROUTINE_SECRET', 'BARE_DEFAULT_SECRET', 'INDEX_SECRET'):
            self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(original, before)

    def test_sql_in_teach_notes_hides_double_quoted_left_operand(self):
        from myflames.ui import api_response
        raw = json.dumps(dict(scan('t'), index_name='k',
            operation='Index lookup on t using k ("PROSE_SECRET" = id)'))
        analysis = api_response('/api/analyze', {'plan': raw})['analysis']
        self.assertIn('PROSE_SECRET', analysis['teach_hooks'][0]['note'])
        result = api_response('/api/report', {'plan': raw, 'format': 'json',
            'hide_literals': True, 'analysis': analysis})['analysis']
        self.assertNotIn('PROSE_SECRET', json.dumps(result))
        self.assertIn('PROSE_SECRET', analysis['teach_hooks'][0]['note'])

    def test_hidden_comparison_preserves_matching_and_deltas(self):
        from myflames.ui import api_response
        before, after = sibling_filter_plans()
        plain = api_response('/api/compare', {'before': before, 'after': after})
        hidden = api_response('/api/compare', {'before': before, 'after': after, 'hide_literals': True})
        self.assertEqual(plain['analysis']['summary']['regressions'], 1)
        self.assertEqual(hidden['analysis']['summary'], plain['analysis']['summary'])
        for old, new in zip(plain['analysis']['deltas'], hidden['analysis']['deltas']):
            for key in ('matching', 'classification', 'self_time_ms', 'rows',
                        'before_node_id', 'after_node_id'):
                self.assertEqual(new[key], old[key], key)
        self.assertNotIn('Uncertain match', hidden['html'])
        self.assertIn('+100.0%', hidden['html'])
        self.assertNotIn('12345', json.dumps(hidden))
        self.assertNotIn('67890', json.dumps(hidden))

    def test_hidden_custom_titles_reach_no_html_channel(self):
        from myflames.output_html_report import render_html_report
        from myflames.output_compare import render_compare
        raw = json.dumps(scan())
        title = "SELECT 'TITLE_SECRET'"
        for html in (render_html_report(raw, title=title, hide_literals=True),
                     render_compare(raw, raw, title=title, hide_literals=True)):
            self.assertNotIn('TITLE_SECRET', html)
            self.assertIn('SELECT ?', html)

    def test_hidden_exports_cover_every_channel_end_to_end(self):
        from myflames.collectors import parse_show_create_table
        from myflames.output_html_report import render_html_report
        from myflames.output_compare import render_compare
        from myflames.ui import api_response
        raw = json.dumps(dict(scan('42', ms=12.5), index_name='k',
            operation='Index lookup on `42` using k ("NOTE_SECRET" = `123`)',
            condition="`123` = 'CONDITION_SECRET'", query="SELECT 'QUERY_SECRET' FROM `42`"))
        ddl = "CREATE TABLE `42` (\n `123` varchar(20) DEFAULT 'DDL_SECRET'\n) ENGINE=InnoDB"
        schema = {'42': parse_show_create_table(ddl)}
        artifacts = {'schema': schema, 'variables': {'version': '8.4.7'}}
        original = copy.deepcopy(artifacts)
        query, title = "SELECT 'QUERY_SECRET' FROM `42`", "SELECT 'TITLE_SECRET'"
        analysis = api_response('/api/analyze', {'plan': raw})['analysis']
        analysis['collected'] = copy.deepcopy(artifacts)
        outputs = [render_html_report(raw, query_text=query, title=title,
                       live_artifacts=artifacts, hide_literals=True).encode('utf-8'),
                   json.dumps(api_response('/api/report', {'plan': raw, 'format': 'json',
                       'analysis': analysis, 'hide_literals': True})['analysis']).encode('utf-8'),
                   render_compare(raw, raw, title=title, hide_literals=True).encode('utf-8')]
        for output in outputs:
            for secret in ('QUERY_SECRET', 'CONDITION_SECRET', 'NOTE_SECRET', 'DDL_SECRET', 'TITLE_SECRET'):
                self.assertNotIn(secret.encode('ascii'), output)
            self.assertIn(b'42', output)
            self.assertIn(b'12.5', output)
        for output in outputs[:2]:
            self.assertIn(b'varchar(20)', output)
            self.assertIn(b'123', output)
        self.assertEqual(artifacts, original)

    def test_report_and_compare_redact_on_export_only(self):
        from myflames.ui import api_response
        raw = json.dumps(dict(scan(), operation="Filter: (name='private-token')", condition="name='private-token'"))
        before = api_response('/api/analyze', {'plan': raw})
        for format_name in ('html', 'json'):
            result = api_response('/api/report', {'plan': raw, 'hide_literals': True, 'format': format_name,
                                                 'query': "SELECT 'private-token'"})
            self.assertNotIn('private-token', json.dumps(result))
        result = api_response('/api/compare', {'before': raw, 'after': raw, 'hide_literals': True})
        self.assertNotIn('private-token', json.dumps(result))
        self.assertIn('Literal values hidden', result['html'])
        self.assertIn('private-token', json.dumps(before))
        self.assertIn('private-token', raw)

    def test_json_export_keeps_collected_evidence_and_original_references(self):
        from myflames.ui import api_response
        raw = json.dumps(dict(scan('42'), condition="id=123"))
        analysis = api_response('/api/analyze', {'plan': raw})['analysis']
        analysis['query'] = {'raw': "SELECT 'private-token'"}
        analysis['collected'] = {'schema': {'42': "CREATE TABLE `42` (v varchar(20) DEFAULT 'private-token')"},
                                 'variables': {'version': '8.4.7', 'sort_buffer_size': 262144}}
        before = copy.deepcopy(analysis)
        result = api_response('/api/report', {'plan': raw, 'format': 'json', 'hide_literals': True,
                                              'analysis': analysis})['analysis']
        self.assertEqual(result['collected']['variables'], analysis['collected']['variables'])
        self.assertEqual(result['plan_tree']['node_id'], analysis['plan_tree']['node_id'])
        self.assertEqual(result['plan_tree']['short_label'], analysis['plan_tree']['short_label'])
        self.assertNotIn('private-token', json.dumps(result))
        self.assertEqual(analysis, before)

    def test_mysql_and_mariadb_json_sharing_keeps_summary_and_measurements(self):
        from myflames.ui import api_response
        for name in ('mysql-explain-hash-join.json', 'mariadb-explain-join.json'):
            with self.subTest(name=name):
                raw = (REPO / 'test' / name).read_text()
                analysis = api_response('/api/analyze', {'plan': raw})['analysis']
                analysis['warnings'].append({'severity': 'info', 'category': 'env',
                    'source': 'environment', 'text': "Filter: (o.total > 987654321); examines 1,234 rows in 12.5 ms."})
                before = copy.deepcopy(analysis)
                result = api_response('/api/report', {'plan': raw, 'format': 'json',
                    'hide_literals': True, 'analysis': analysis})['analysis']
                self.assertEqual(result['executive_summary'], analysis['executive_summary'])
                self.assertEqual(result['plan_summary'], analysis['plan_summary'])
                self.assertEqual(result['warnings'][-1]['text'],
                    'Filter: (o.total > ?); examines 1,234 rows in 12.5 ms.')
                self.assertEqual(analysis, before)

    def test_hide_literal_option_is_boolean(self):
        from myflames.ui import api_response
        raw = json.dumps(scan())
        for value in ('false', 1, [], None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, 'boolean'):
                    api_response('/api/report', {'plan': raw, 'hide_literals': value})
                with self.assertRaisesRegex(ValueError, 'boolean'):
                    api_response('/api/compare', {'before': raw, 'after': raw, 'hide_literals': value})


def sibling_filter_plans():
    """MySQL operation strings reproduce predicate-based sibling identity."""
    before = dict(scan(ms=40), operation='Union all', inputs=[
        dict(scan(ms=10), operation='Filter: (id=12345)', condition='id=12345'),
        dict(scan(ms=10), operation='Filter: (id=67890)', condition='id=67890')])
    after = copy.deepcopy(before)
    after['actual_last_row_ms'] = 50
    after['inputs'][0]['actual_last_row_ms'] = 20
    return json.dumps(before), json.dumps(after)
