"""Index hints over estimated MySQL 8.0 JSON, MariaDB JSON and fake clients."""
import copy
import json
import unittest
from unittest import mock

from myflames import discover, index_hint
from myflames.connector import ConnectorError
from myflames.parser import parse_explain
from test_discover import digest_row, mysql_top

# Traditional MySQL FORMAT=JSON shape, not an ANALYZE operation tree. Fields
# match sql/opt_explain_json.cc; no timing or actual-row fields are present.
MYSQL80 = {'query_block': {'select_id': 1, 'cost_info': {'query_cost': '10.25'},
    'table': {'table_name': 'orders', 'access_type': 'ALL', 'rows_examined_per_scan': 100,
              'rows_produced_per_join': 10, 'filtered': '10.00',
              'attached_condition': '(`shop`.`orders`.`status` = 1)'}}}
MARIADB = {'query_block': {'select_id': 1, 'nested_loop': [{'table': {
    'table_name': 'orders', 'access_type': 'ALL', 'rows': 100, 'filtered': 10,
    'attached_condition': 'orders.status = 1'}}]}}


def row(**overrides):
    value = {'digest': 'd' * 64, 'digest_text': 'SELECT * FROM `orders` WHERE `status` = ?',
             'sample_text': 'SELECT * FROM orders WHERE status = 1', 'sample_truncated': False,
             'schema': 'shop', 'rows_examined': 1000, 'rows_sent': 100, 'count': 10,
             'capture_problem': None}
    value.update(overrides)
    return value


def index(**overrides):
    value = {'schema': 'shop', 'table': 'orders', 'name': 'by_status', 'columns': ['status', 'id'],
             'sub_parts': [None, None], 'index_type': 'BTREE'}
    value.update(overrides)
    return value


def fake(statement=None, plan=None, metadata='<resultset/>'):
    statement = statement or row()
    conn = mysql_top('8.0.43', [digest_row(statement['digest_text'], schema=statement['schema'],
        sample=statement['sample_text'], examined=statement['rows_examined'], no_index=10)], extra=[
            ('EXPLAIN FORMAT=JSON', plan or json.dumps(MYSQL80)),
            ('information_schema.STATISTICS', metadata)])
    conn.database = 'different_default'
    conn.password = 'secret-not-real'
    # The EXPLAIN goes on standard input (run_write); the fake answers it by the
    # same SQL substring match as run.
    conn.run_write = lambda sql, extra_flags=None, timeout=None: conn.run(sql, extra_flags=extra_flags)
    return conn


class TestIndexHint(unittest.TestCase):
    def test_owner_example_uses_digest_without_plan_or_metadata(self):
        result = index_hint.suggest(row(digest_text='select * from sbc_stage.calls_disconnect_temp',
            sample_text=None, rows_examined=0, rows_sent=0, count=9326892))
        self.assertEqual(result['status'], 'no_predicate')
        self.assertIn('sbc_stage.calls_disconnect_temp', result['reason'])
        self.assertIn('no WHERE, JOIN condition, GROUP BY or ORDER BY', result['reason'])
        self.assertIn('empty', result['reason'])
        self.assertIn('9.3 M calls', result['reason'])
        self.assertNotIn('ddl', result)

    def test_simple_comments_and_quoted_names(self):
        result = index_hint.suggest(row(digest_text='/* read */ SELECT * FROM `s.b` . `t``x` AS t;'))
        self.assertEqual(result['status'], 'no_predicate')
        self.assertIn('s.b.t`x', result['reason'])
        self.assertNotIn('empty', result['reason'])

    def test_clauses_joins_subqueries_and_covering_scans_are_not_no_predicate(self):
        queries = ['SELECT * FROM orders WHERE id = 1',
                   'SELECT * FROM orders JOIN users ON users.id = orders.user_id',
                   'SELECT * FROM orders JOIN users USING (id)',
                   'SELECT * FROM orders GROUP BY status', 'SELECT * FROM orders ORDER BY status',
                   'SELECT * FROM (SELECT * FROM orders) t',
                   'SELECT *, (SELECT id FROM users) FROM orders',
                   'SELECT * FROM orders WHERE EXISTS (SELECT 1 FROM users)',
                   'WITH o AS (SELECT * FROM orders) SELECT * FROM o',
                   'SELECT count(*) FROM orders', 'SELECT id FROM orders',
                   'SELECT * FROM orders LIMIT 0', 'SELECT DISTINCT * FROM orders',
                   'SELECT * FROM orders WHERE', "SELECT * FROM orders 'WHERE id = 1'"]
        for sql in queries:
            with self.subTest(sql=sql):
                self.assertIsNone(index_hint.no_predicate(row(digest_text=sql, sample_text=sql)))

    def test_estimated_mysql80_and_mariadb_suggestions(self):
        for raw in (MYSQL80, MARIADB):
            with self.subTest(raw=raw):
                root = parse_explain(json.dumps(raw))
                result = index_hint.suggest(row(), root, [])
                self.assertEqual(result['status'], 'suggestion')
                self.assertEqual(result['columns'], ['status'])
                self.assertEqual(result['ddl'], 'CREATE INDEX `idx_orders_status` ON `shop`.`orders` (`status`);')
                self.assertIn('Heuristic', result['caveats'])
                self.assertIsNone(root['details']['actual_last_row_ms'])

    def test_covering_prefix_names_existing_index(self):
        result = index_hint.suggest(row(), parse_explain(json.dumps(MYSQL80)), [index()])
        self.assertEqual(result['status'], 'covered')
        self.assertIn('by_status', result['reason'])
        self.assertIn('optimizer did not use it', result['reason'])
        self.assertNotIn('ddl', result)

    def test_suffix_prefix_length_other_schema_and_hash_do_not_cover(self):
        for existing in (index(columns=['id', 'status']), index(sub_parts=['5', None]),
                         index(schema='other'), index(index_type='HASH')):
            with self.subTest(existing=existing):
                self.assertEqual(index_hint.suggest(row(), parse_explain(json.dumps(MYSQL80)), [existing])['status'], 'suggestion')

    def test_invisible_existing_index_is_named_without_duplicate_ddl(self):
        result = index_hint.suggest(row(), parse_explain(json.dumps(MYSQL80)), [index(visible=False)])
        self.assertEqual(result['status'], 'covered')
        self.assertIn('invisible (MySQL) or ignored (MariaDB)', result['reason'])

    def test_duplicate_name_is_avoided(self):
        result = index_hint.suggest(row(), parse_explain(json.dumps(MYSQL80)),
                                    [index(name='idx_orders_status', columns=['id'])])
        self.assertIn('`idx_orders_status_2`', result['ddl'])

    def test_non_select_capture_problem_and_unsafe_select_refuse(self):
        for sql in ['DELETE FROM orders WHERE id = 1', 'UPDATE orders SET status = 1',
                    'SELECT * FROM orders; DROP TABLE orders', "SELECT * FROM orders INTO OUTFILE '/tmp/x'",
                    "SELECT GET_LOCK('x', 0) FROM orders", 'SELECT * FROM orders /*! WHERE id = 1 */',
                    'SELECT * FROM orders\n\\! echo bad']:
            with self.subTest(sql=sql):
                result = index_hint.suggest(row(digest_text=sql, sample_text=sql))
                self.assertEqual(result['status'], 'not_explainable')
                self.assertIn('Query editor', result['reason'])
        self.assertEqual(index_hint.suggest(row(capture_problem='Refused by capture'))['status'], 'not_explainable')

    def test_truncated_sample_never_explained_without_values(self):
        result = index_hint.suggest(row(sample_text='SELECT * FROM orders WHERE sta', sample_truncated=True))
        self.assertEqual(result['status'], 'not_explainable')
        self.assertIn('cut off', result['reason'])
        self.assertIn('Query editor', result['reason'])

    def test_no_sample_with_values_missing_is_not_explainable(self):
        self.assertEqual(index_hint.suggest(row(sample_text=None))['status'], 'not_explainable')

    def test_unknown_error_and_missing_metadata(self):
        self.assertIn('timeout', index_hint.suggest(row(), error='timeout')['reason'])
        self.assertEqual(index_hint.suggest(row(), parse_explain(json.dumps(MYSQL80)))['status'], 'unknown')
        self.assertEqual(index_hint.suggest(row(), parse_explain('{"query_block":{"message":"No tables used"}}'), [])['status'], 'unknown')

    def test_alias_resolves_to_real_schema_table_and_quotes_identifiers(self):
        raw = copy.deepcopy(MYSQL80)
        raw['query_block']['table'].update(table_name='o', attached_condition='o.status = 1')
        result = index_hint.suggest(row(sample_text='SELECT * FROM `s``x`.`o``rders` AS o WHERE o.status = 1'),
                                    parse_explain(json.dumps(raw)), [])
        self.assertEqual(result['status'], 'suggestion')
        self.assertIn('ON `s``x`.`o``rders` (`status`)', result['ddl'])

    def test_subquery_and_ambiguous_alias_decline_ddl(self):
        raw = copy.deepcopy(MYSQL80)
        raw['query_block']['table'].update(table_name='o', attached_condition='o.status = 1')
        for sql in ['SELECT * FROM (SELECT * FROM orders) o WHERE o.status = 1',
                    'SELECT * FROM shop.orders o JOIN other.orders o ON o.id = o.id WHERE o.status = 1']:
            self.assertEqual(index_hint.suggest(row(sample_text=sql), parse_explain(json.dumps(raw)), [])['status'], 'unknown')

    def test_read_explains_only_sample_in_digest_schema_and_reads_scoped_indexes(self):
        conn = fake()
        original = conn.run
        databases = []
        def run(sql, **kwargs):
            databases.append(conn.database)
            return original(sql, **kwargs)
        conn.run = run
        result = index_hint.read_suggestion(conn, 'd' * 64, 'shop')
        self.assertEqual(result['status'], 'suggestion')
        explained = [(sql, db) for (sql, _), db in zip(conn.calls, databases) if 'EXPLAIN FORMAT=JSON' in sql]
        tag = '/*myflames:index-hint*/ '
        self.assertEqual(explained, [(tag + 'SET SESSION TRANSACTION READ ONLY;\n' + tag + 'START TRANSACTION READ ONLY;\n'
                                      + tag + 'EXPLAIN FORMAT=JSON SELECT * FROM orders WHERE status = 1\n;\n'
                                      + tag + 'ROLLBACK;\n', 'shop')])
        self.assertEqual(conn.database, 'different_default')
        metadata = conn.sql('information_schema.STATISTICS')
        self.assertEqual(len(metadata), 1)
        self.assertIn('BINARY TABLE_SCHEMA', metadata[0])
        self.assertIn('LIMIT 4097', metadata[0])
        self.assertFalse(any('ANALYZE' in sql or 'CREATE INDEX' in sql for sql, _ in conn.calls))

    def test_top_report_precomputes_no_predicate_without_an_extra_read(self):
        conn = fake(row(digest_text='SELECT * FROM orders', sample_text=None, rows_examined=0))
        statement = discover.find_statement(conn, 'd' * 64, 'shop')['statement']
        self.assertEqual(statement['index_hint']['status'], 'no_predicate')
        result = index_hint.read_suggestion(conn, 'd' * 64, 'shop')
        self.assertEqual(result['status'], 'no_predicate')
        self.assertEqual(conn.sql('EXPLAIN'), [])
        self.assertEqual(conn.sql('STATISTICS'), [])

    def test_metadata_leading_columns_case_policy_and_missing_parts(self):
        from xml.sax.saxutils import escape
        def metadata(**overrides):
            fields = {'TABLE_SCHEMA': 'shop', 'TABLE_NAME': 'orders', 'INDEX_NAME': 'by_status',
                      'SEQ_IN_INDEX': '1', 'COLUMN_NAME': 'status', 'INDEX_TYPE': 'BTREE',
                      'IS_VISIBLE': 'YES', 'MYFLAMES_LOWER_CASE_TABLE_NAMES': '1'}
            fields.update(overrides)
            return '<resultset><row>' + ''.join('<field name="%s">%s</field>' % (key, escape(value))
                                               for key, value in fields.items()) + '</row></resultset>'
        conn = fake(row(schema='Shop'), metadata=metadata())
        self.assertEqual(index_hint.read_suggestion(conn, 'd' * 64, 'Shop')['status'], 'covered')
        conn = fake(metadata=metadata(SEQ_IN_INDEX='2'))
        result = index_hint.read_suggestion(conn, 'd' * 64, 'shop')
        self.assertEqual(result['status'], 'unknown')
        self.assertIn('missing key parts', result['reason'])
        conn = fake(metadata=ConnectorError('STATISTICS access denied'))
        self.assertIn('STATISTICS access denied', index_hint.read_suggestion(conn, 'd' * 64, 'shop')['reason'])

    def test_join_inner_table_and_truncated_runnable_digest(self):
        raw = copy.deepcopy(MYSQL80)
        raw['query_block']['table'].update(table_name='users', attached_condition='users.status = 1')
        statement = row(sample_text='SELECT * FROM orders JOIN users ON users.id = orders.id WHERE users.status = 1')
        result = index_hint.suggest(statement, parse_explain(json.dumps(raw)), [])
        self.assertEqual(result['table'], 'users')
        statement = row(digest_text='SELECT * FROM orders WHERE status = 1', sample_text='SELECT * F', sample_truncated=True)
        self.assertEqual(index_hint.suggest(statement, parse_explain(json.dumps(MYSQL80)), [])['status'], 'suggestion')

    def test_server_lookup_schema_mismatch_does_not_explain(self):
        conn = fake()
        result = index_hint.read_suggestion(conn, 'd' * 64, 'missing')
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(conn.sql('EXPLAIN'), [])

    def test_explain_error_redacts_password(self):
        conn = fake(plan=ConnectorError('denied secret-not-real'))
        result = index_hint.read_suggestion(conn, 'd' * 64, 'shop')
        self.assertEqual(result['status'], 'unknown')
        self.assertNotIn('secret-not-real', result['reason'])
        self.assertEqual(conn.sql('STATISTICS'), [])


class TestAutomaticExplainSafety(unittest.TestCase):
    """The EXPLAIN runs without a click: read-only, rolled back, never on argv."""

    def test_explain_is_sent_on_stdin_inside_a_read_only_transaction(self):
        conn = fake()
        sent = []
        def run_write(sql, extra_flags=None, timeout=None):
            sent.append((sql, list(extra_flags or [])))
            return json.dumps(MYSQL80)
        conn.run_write = run_write
        index_hint.read_suggestion(conn, 'd' * 64, 'shop')
        self.assertEqual(len(sent), 1)
        script, flags = sent[0]
        lines = [line for line in script.split('\n') if line.strip()]
        self.assertTrue(lines[0].endswith('SET SESSION TRANSACTION READ ONLY;'))
        self.assertTrue(lines[1].endswith('START TRANSACTION READ ONLY;'))
        self.assertIn('EXPLAIN FORMAT=JSON SELECT * FROM orders WHERE status = 1', lines[2])
        self.assertTrue(lines[-1].endswith('ROLLBACK;'))
        self.assertEqual(flags, ['--raw'])
        self.assertFalse(any('EXPLAIN' in sql for sql, _ in conn.calls if sql not in [s for s, _ in sent]),
                         'no EXPLAIN goes through run (argv -e)')

    def test_sample_never_reaches_the_client_command_line(self):
        from myflames import connector
        seen = []
        def bounded(argv, timeout, input=None, env=None):
            seen.append((list(argv), input))
            class Done:
                returncode, stdout, stderr = 0, json.dumps(MYSQL80).encode(), b''
            return Done()
        conn = connector.MySQLConnection(host='127.0.0.1', port=3306, user='u', password='p')
        with mock.patch.object(connector, '_run_bounded', bounded), \
                mock.patch.object(connector.MySQLConnection, '_resolve_binary', lambda self: '/usr/bin/mysql'):
            conn.run_write('/*myflames:index-hint*/ EXPLAIN FORMAT=JSON SELECT secret_value FROM t WHERE a = 1\n;\n',
                           extra_flags=['--raw'])
        argv, stdin = seen[0]
        self.assertFalse(any('secret_value' in arg for arg in argv))
        self.assertIn(b'secret_value', stdin)
        self.assertIn('--binary-mode', argv)
        self.assertNotIn('--force', argv)

    def test_sleep_and_benchmark_are_not_explained_automatically(self):
        for sample in ('SELECT * FROM orders WHERE status = SLEEP(30)',
                       'SELECT * FROM orders WHERE status = 1 AND BENCHMARK (100000000, MD5(1)) = 0'):
            conn = fake(statement=row(sample_text=sample))
            sent = []
            conn.run_write = lambda sql, extra_flags=None, timeout=None: sent.append(sql) or ''
            result = index_hint.read_suggestion(conn, 'd' * 64, 'shop')
            self.assertEqual(result['status'], 'not_explainable', sample)
            # The capture rule already refuses some waiting functions with its
            # own wording; the automatic path refuses the rest.
            self.assertRegex(result['reason'], r'SLEEP or BENCHMARK|waiting functions')
            self.assertEqual(sent, [], 'nothing is sent for ' + sample)

    def test_sleep_inside_a_string_literal_is_still_explained(self):
        conn = fake(statement=row(sample_text="SELECT * FROM orders WHERE note = 'SLEEP(1)'"))
        sent = []
        conn.run_write = lambda sql, extra_flags=None, timeout=None: sent.append(sql) or json.dumps(MYSQL80)
        index_hint.read_suggestion(conn, 'd' * 64, 'shop')
        self.assertEqual(len(sent), 1)

    def test_identifiers_with_line_breaks_get_no_ddl(self):
        raw = copy.deepcopy(MYSQL80)
        raw['query_block']['table'].update(table_name='or\nders')
        result = index_hint.suggest(row(sample_text='SELECT * FROM `or\nders` WHERE status = 1'),
                                    parse_explain(json.dumps(raw)), [])
        self.assertNotIn('ddl', result)

