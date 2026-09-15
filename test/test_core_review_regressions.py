"""Regression coverage for live connection and range-advisor review findings."""
import json
import unittest
import subprocess
from unittest import mock

from myflames.advisor import _rule_tmp_table_size_vs_materialize
from myflames.connector import MySQLConnection
from myflames.parser import parse_explain, analyze_plan


class TestOptionFilePassword(unittest.TestCase):
    def test_backslashes_and_control_characters_are_escaped(self):
        password = 'literal\\n\\t\\s\\b\\\\"\n\r\t\b'
        with MySQLConnection(host='localhost', password=password) as conn:
            text = conn._build_defaults_file_content()
        self.assertIn('password="literal\\\\n\\\\t\\\\s\\\\b\\\\\\\\\\"\\n\\r\\t\\b"', text)
        self.assertEqual(len([line for line in text.splitlines()
                              if line.startswith('password=')]), 1)
        self.assertNotIn('\n\r', text)


class TestRangeScanAdvice(unittest.TestCase):
    def _mysql(self, index, covering=False):
        return parse_explain(json.dumps({
            'operation': 'Index range scan on t using ' + index,
            'table_name': 't', 'access_type': 'index',
            'index_access_type': 'index_range_scan', 'index_name': index,
            'covering': covering, 'actual_rows': 500,
            'actual_loops': 1, 'actual_last_row_ms': 1,
        }))

    def test_primary_range_is_not_secondary_mrr_candidate(self):
        self.assertEqual(analyze_plan(self._mysql('PRIMARY'))['range_scans'], [])

    def test_covering_range_needs_no_mrr_row_fetch(self):
        self.assertEqual(analyze_plan(self._mysql('idx_a', True))['range_scans'], [])

    def test_secondary_range_keeps_index_name(self):
        self.assertEqual(analyze_plan(self._mysql('idx_a'))['range_scans'][0]['key'], 'idx_a')

    def test_mariadb_covering_range_is_preserved(self):
        root = parse_explain(json.dumps({'query_block': {'select_id': 1, 'nested_loop': [{'table': {
            'table_name': 't', 'access_type': 'range', 'key': 'idx_a',
            'using_index': True, 'r_rows': 500, 'r_loops': 1,
            'r_table_time_ms': 1,
        }}]}}))
        self.assertTrue(root['details']['covering'])
        self.assertEqual(analyze_plan(root)['range_scans'], [])


class TestTemporaryTableEngineLimits(unittest.TestCase):
    def _advise(self, engine, tmp, heap, version='9.7.0'):
        return _rule_tmp_table_size_vs_materialize(
            {'temp_tables': [{'rows': 1}]}, None, None,
            {'internal_tmp_mem_storage_engine': engine,
             'tmp_table_size': str(tmp * 1024 * 1024),
             'max_heap_table_size': str(heap * 1024 * 1024),
             'version': version})

    def test_temptable_ignores_small_heap_limit(self):
        self.assertEqual(self._advise('TempTable', 64, 1), (None, None))

    def test_memory_uses_smaller_heap_limit(self):
        warning, suggestion = self._advise('MEMORY', 64, 1)
        self.assertIn('1.0 MB', warning)
        self.assertIn('same value', suggestion)

    def test_mariadb_uses_smaller_heap_limit(self):
        warning, suggestion = self._advise('', 64, 1, '11.4.8-MariaDB')
        self.assertIn('1.0 MB', warning)
        self.assertIn('same value', suggestion)

    def test_temptable_small_limit_does_not_prove_spill(self):
        warning, suggestion = self._advise('TempTable', 16, 1)
        self.assertIn('16.0 MB', warning)
        self.assertIn('does not show', warning)
        self.assertIn('max_heap_table_size does not limit TempTable', suggestion)


class TestLiveExplainEscaping(unittest.TestCase):
    def test_explain_preserves_json_escapes_with_raw_client_output(self):
        plan = {'operation': 'Filter: (t.name = "a\\b")',
                'condition': 't.name = "a\\b"', 'actual_last_row_ms': 1}
        raw = json.dumps(plan, indent=2)
        # mysql's default batch mode escapes every backslash, including
        # JSON's own escapes. --raw must apply only to this one-column call.
        escaped = raw.replace('\\', '\\\\').replace('\n', '\\n')
        def fake_run(argv, **kwargs):
            stdout = raw if '--raw' in argv else escaped
            return subprocess.CompletedProcess(argv, 0, stdout.encode(), b'')
        with MySQLConnection(host='localhost', binary='/bin/mysql') as conn:
            conn._is_mariadb_cache = False
            with mock.patch('myflames.connector.subprocess.run', side_effect=fake_run):
                result = parse_explain(conn.explain_analyze('SELECT * FROM t'))
        self.assertEqual(result['details']['condition'], plan['condition'])


class TestMetadataSQLTokenization(unittest.TestCase):
    def test_comment_markers_inside_strings_do_not_hide_tables(self):
        from myflames.collectors import extract_table_names
        for literal in ("'-- hidden'", "'/* hidden */'", "'a\\\'-- hidden'",
                        '"-- hidden"'):
            with self.subTest(literal=literal):
                self.assertEqual(extract_table_names(
                    'SELECT ' + literal + ' FROM orders'), ['orders'])

    def test_mysql_hash_comments_do_not_add_fake_tables(self):
        from myflames.collectors import extract_table_names
        self.assertEqual(extract_table_names(
            'SELECT * FROM orders # JOIN secret\nWHERE id=1'), ['orders'])

    def test_subtract_negative_number_is_not_comment(self):
        from myflames.collectors import extract_table_names
        self.assertEqual(extract_table_names('SELECT 1--2 FROM orders'), ['orders'])

    def test_backtick_identifier_keeps_comment_markers(self):
        from myflames.collectors import extract_table_names
        self.assertEqual(extract_table_names('SELECT * FROM `orders--archive`'),
                         ['orders--archive'])


class TestAggregateComplexity(unittest.TestCase):
    def _complexity(self, operation):
        root = parse_explain(json.dumps({'operation': operation,
                                        'access_type': 'aggregate'}))
        return root['details'].get('complexity')

    def test_scalar_aggregate_is_linear(self):
        self.assertEqual(self._complexity('Aggregate: avg(t.amount)')['big_o'], 'O(n)')

    def test_distinct_aggregate_keeps_deduplication_cost(self):
        result = self._complexity('Aggregate: count(distinct t.user_id)')
        self.assertEqual(result['big_o'], 'O(n log n)')
        self.assertEqual(result['confidence'], 'worst_case')

    def test_distinct_column_name_is_not_distinct_aggregation(self):
        result = self._complexity('Aggregate: count(distinct_count)')
        self.assertEqual(result['big_o'], 'O(n)')

    def test_ordered_and_multidimensional_aggregates_are_not_called_linear(self):
        for operation in ('Aggregate: group_concat(t.name order by t.name)',
                          'Group aggregate with cube: sum(t.amount)'):
            with self.subTest(operation=operation):
                self.assertIsNone(self._complexity(operation))


class TestAdvisorEvidenceLimits(unittest.TestCase):
    def test_large_table_does_not_prove_large_query_working_set(self):
        from myflames.advisor import _rule_buffer_pool_vs_data_size
        warning, suggestion = _rule_buffer_pool_vs_data_size(
            {}, {}, {'users': {'data_length': 1024 ** 3, 'index_length': 0}},
            {'innodb_buffer_pool_size': str(64 * 1024 ** 2)})
        self.assertIn('working set may be much smaller', warning)
        self.assertIn('Measure buffer-pool reads', suggestion)
        self.assertNotIn('Raise', suggestion)
        self.assertNotIn('10–100', suggestion)

    def test_small_sort_buffer_does_not_prove_spill(self):
        from myflames.advisor import _rule_sort_buffer_vs_filesort
        warning, suggestion = _rule_sort_buffer_vs_filesort(
            {'filesorts': [{'rows': 1}]}, None, None,
            {'sort_buffer_size': str(256 * 1024)})
        self.assertIn('depends on', warning)
        self.assertIn('Check sort merge passes', suggestion)
        self.assertNotIn('likely spill', warning)
        self.assertNotIn('SET SESSION', suggestion)
