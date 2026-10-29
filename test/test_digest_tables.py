"""Statement-text table matches and bounded, read-only metadata on both engines."""
import inspect
import json
import os
import unittest
from unittest import mock
from xml.etree import ElementTree

from myflames import collectors, discover, ui
from myflames.connector import ConnectorError


class TestTableExtraction(unittest.TestCase):
    def refs(self, sql, schema='app', limit=20):
        return collectors.extract_table_references(sql, schema, limit)

    def test_joins_aliases_commas_and_subqueries(self):
        pairs, notes = self.refs('SELECT u.id FROM users AS u JOIN orders o ON o.uid=u.id, audit a '
                                 'WHERE EXISTS (SELECT 1 FROM other.events e WHERE e.id=o.id)')
        self.assertEqual(pairs, [('app', 'users'), ('app', 'orders'), ('app', 'audit'), ('other', 'events')])
        self.assertEqual(notes, [])

    def test_derived_tables_and_grouped_joins(self):
        pairs, notes = self.refs('SELECT * FROM (SELECT id FROM real_t) AS d '
                                 'JOIN (a LEFT JOIN b ON a.id=b.id) ON d.id=a.id')
        self.assertEqual(pairs, [('app', 'real_t'), ('app', 'a'), ('app', 'b')])
        self.assertTrue(any('Derived tables' in n for n in notes))

    def test_recursive_and_multiple_ctes_skip_names_but_not_qualified_tables(self):
        pairs, notes = self.refs('WITH RECURSIVE c(id) AS (SELECT id FROM base UNION ALL SELECT id FROM c), '
                                 'd AS (SELECT id FROM c JOIN real_d ON 1=1) '
                                 'SELECT * FROM d JOIN app.c ON 1=1 JOIN `c` ON 1=1')
        self.assertEqual(pairs, [('app', 'base'), ('app', 'real_d'), ('app', 'c')])
        self.assertTrue(any('CTE' in n for n in notes))

    def test_nested_cte_scope_does_not_hide_real_table_outside_it(self):
        pairs, _ = self.refs('SELECT * FROM outer_t WHERE EXISTS '
                            '(WITH c AS (SELECT * FROM inner_t) SELECT * FROM c) '
                            'UNION SELECT * FROM c')
        self.assertEqual(pairs, [('app', 'outer_t'), ('app', 'inner_t'), ('app', 'c')])
        pairs, _ = self.refs('WITH c AS (SELECT * FROM base) SELECT * FROM '
                            '(WITH c AS (SELECT * FROM inner_t) SELECT * FROM c) d JOIN c ON 1=1; SELECT * FROM c')
        self.assertEqual(pairs, [('app', 'base'), ('app', 'inner_t'), ('app', 'c')])

    def test_backquotes_qualified_names_dots_and_escaped_backquote(self):
        pairs, _ = self.refs('SELECT * FROM `db.with.dot` . `t.with.dot` AS x '
                            'JOIN `a``b` AS y ON 1=1 JOIN other . normal ON 1=1')
        self.assertEqual(pairs, [('db.with.dot', 't.with.dot'), ('app', 'a`b'), ('other', 'normal')])
        self.assertEqual(self.refs('SELECT * FROM unqualified', None)[0], [(None, 'unqualified')])

    def test_digest_placeholders_literals_comments_and_expression_from(self):
        pairs, _ = self.refs("SELECT EXTRACT(DAY FROM when_col), 'FROM fake', `FROM` FROM real_t "
                            "WHERE x IN (...) AND y=? /* JOIN fake */ -- FROM fake2\n")
        self.assertEqual(pairs, [('app', 'real_t')])
        self.assertEqual(self.refs('SELECT * FROM <derived2>')[0], [])
        self.assertEqual(self.refs('SELECT 1 FROM DUAL')[0], [])

    def test_dml_names_and_table_functions(self):
        self.assertEqual(self.refs('UPDATE a JOIN b ON a.id=b.id SET a.x=?')[0], [('app', 'a'), ('app', 'b')])
        self.assertEqual(self.refs('INSERT INTO target(x) SELECT x FROM source')[0], [('app', 'target'), ('app', 'source')])
        self.assertEqual(self.refs('DELETE FROM a WHERE x IN (SELECT x FROM b)')[0], [('app', 'a'), ('app', 'b')])
        self.assertEqual(self.refs('DELETE FROM alias USING real_t alias JOIN b USING (id)')[0], [('app', 'real_t'), ('app', 'b')])
        self.assertEqual(self.refs('INSERT IGNORE target(x) SELECT x FROM source')[0], [('app', 'target'), ('app', 'source')])
        self.assertEqual(self.refs('REPLACE target VALUES (...)')[0], [('app', 'target')])
        pairs, notes = self.refs("SELECT * FROM real_t, JSON_TABLE(?, '$' COLUMNS (x INT PATH '$')) j")
        self.assertEqual(pairs, [('app', 'real_t')])
        self.assertTrue(any('Table functions' in n for n in notes))

    def test_cap_deduplication_and_case_distinct_names(self):
        sql = 'SELECT * FROM ' + ', '.join(['t0', 't0'] + ['t%d' % i for i in range(1, 24)])
        pairs, notes = self.refs(sql)
        self.assertEqual(pairs, [('app', 't%d' % i) for i in range(20)])
        self.assertTrue(any('first 20 tables' in n for n in notes))
        self.assertEqual(self.refs('SELECT * FROM t, T')[0], [('app', 't'), ('app', 'T')])


class TestDigestTables(unittest.TestCase):
    def metadata(self, name='orders', schema='app', kind='BASE TABLE', **kw):
        return dict(TABLE_SCHEMA=schema, TABLE_NAME=name, TABLE_TYPE=kind, ENGINE='InnoDB',
                    TABLE_ROWS='150000', DATA_LENGTH='1048576', INDEX_LENGTH='524288', **kw)

    def index(self, name='PRIMARY', column='id', seq=1, unique=True, **kw):
        return dict(TABLE_SCHEMA='app', TABLE_NAME='orders', INDEX_NAME=name, COLUMN_NAME=column,
                    NON_UNIQUE='0' if unique else '1', SEQ_IN_INDEX=str(seq), **kw)

    def read(self, metadata, indexes, text='SELECT * FROM orders', schema='app'):
        reader = mock.Mock()
        reader.named.side_effect = [metadata, indexes]
        with mock.patch('myflames.perf_common.Reader', return_value=reader):
            result = discover.digest_tables(mock.Mock(), schema, text)
        return result, reader

    def test_mysql_shapes_ordered_indexes_visibility_and_nullable_metadata(self):
        result, reader = self.read([self.metadata()], [self.index(IS_VISIBLE='YES'),
            self.index('idx_customer', 'customer_id', unique=False, IS_VISIBLE='NO'),
            self.index('idx_customer', 'created_at', seq=2, unique=False, IS_VISIBLE='NO'),
            self.index('functional', None, EXPRESSION='lower(`email`)', IS_VISIBLE='YES')])
        table = result['tables'][0]
        self.assertEqual(result['schema_version'], 'digest-tables-1.0')
        self.assertEqual(result['method'], 'statement text')
        self.assertEqual((table['found'], table['type'], table['rows_estimate'], table['data_bytes'], table['index_bytes']),
                         (True, 'table', 150000, 1048576, 524288))
        self.assertEqual(table['indexes'], [
            dict(name='PRIMARY', unique=True, columns=['id'], visible=True),
            dict(name='idx_customer', unique=False, columns=['customer_id', 'created_at'], visible=False),
            dict(name='functional', unique=True, columns=['lower(`email`)'], visible=True)])
        self.assertEqual(reader.named.call_count, 2)
        for call in reader.named.call_args_list:
            sql = call[0][0]
            self.assertIn('TABLE_SCHEMA = ' + discover._literal('app'), sql)
            self.assertIn('TABLE_NAME = ' + discover._literal('orders'), sql)
            self.assertIn('LIMIT ', sql)
            self.assertNotIn('ANALYZE', sql)
            self.assertNotIn('SHOW TABLE STATUS', sql)
        self.assertIn('SEQ_IN_INDEX', reader.named.call_args_list[1][0][0])
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_mariadb_has_no_is_visible_and_views_have_unknown_sizes(self):
        view = self.metadata('order_view', kind='VIEW')
        view.update(ENGINE=None, TABLE_ROWS=None, DATA_LENGTH=None, INDEX_LENGTH=None)
        result, _ = self.read([self.metadata(), view], [self.index()], text='SELECT * FROM orders JOIN order_view ON 1=1')
        self.assertIsNone(result['tables'][0]['indexes'][0]['visible'])
        table = result['tables'][1]
        self.assertEqual(table['type'], 'view')
        for key in ('rows_estimate', 'data_bytes', 'index_bytes', 'engine'):
            self.assertIsNone(table[key])

    def test_missing_or_unresolvable_names_and_empty_query(self):
        result, _ = self.read([], [], text='SELECT * FROM missing')
        self.assertFalse(result['tables'][0]['found'])
        self.assertTrue(any('not found or is not visible' in n for n in result['notes']))
        for sql, schema in [('SELECT 1', 'app'), ('SELECT * FROM unknown', None)]:
            result, reader = self.read([], [], sql, schema)
            reader.named.assert_not_called()
            if schema is None:
                self.assertIn('No default schema', result['notes'][0])

    def test_pairs_are_scoped_together_and_names_are_hex_literals(self):
        result, reader = self.read([], [], text='SELECT * FROM `a.b`.`x.y` JOIN `c`.`z\' OR 1=1 --` ON 1=1')
        self.assertEqual([(t['schema'], t['name']) for t in result['tables']], [('a.b', 'x.y'), ('c', "z' OR 1=1 --")])
        sql = reader.named.call_args_list[0][0][0]
        for schema, name in [('a.b', 'x.y'), ('c', "z' OR 1=1 --")]:
            self.assertIn('(TABLE_SCHEMA = %s AND TABLE_NAME = %s AND ' % (discover._literal(schema), discover._literal(name)), sql)
        self.assertIn(' OR ', sql)
        self.assertIn('@@lower_case_table_names <> 0', sql)
        self.assertIn('BINARY TABLE_NAME = BINARY ', sql)
        self.assertNotIn("OR 1=1 --", sql)

    def test_cap_scopes_only_twenty_names_and_index_cap_is_explicit(self):
        text = 'SELECT * FROM ' + ', '.join('t%d' % i for i in range(22))
        result, reader = self.read([], [], text)
        self.assertEqual(len(result['tables']), 20)
        self.assertNotIn(discover._literal('t20'), reader.named.call_args_list[0][0][0])
        result, _ = self.read([self.metadata()], [self.index()] * (discover.DIGEST_INDEX_PART_LIMIT + 1))
        self.assertTrue(any('incomplete' in n for n in result['notes']))
        self.assertEqual(len(result['tables'][0]['indexes'][0]['columns']), discover.DIGEST_INDEX_PART_LIMIT)

    def test_case_matching_does_not_merge_distinct_tables(self):
        result, _ = self.read([self.metadata(name='orders', MYFLAMES_LOWER_CASE_TABLE_NAMES='0')], [], text='SELECT * FROM ORDERS')
        self.assertFalse(result['tables'][0]['found'])
        result, _ = self.read([self.metadata(name='orders', MYFLAMES_LOWER_CASE_TABLE_NAMES='1')], [self.index()], text='SELECT * FROM ORDERS, orders')
        self.assertTrue(all(t['found'] for t in result['tables']))
        self.assertTrue(all(len(t['indexes']) == 1 for t in result['tables']))
        result, _ = self.read([self.metadata(name='orders'), self.metadata(name='ORDERS')], [], text='SELECT * FROM orders, ORDERS')
        self.assertTrue(all(t['found'] for t in result['tables']))

    def test_real_named_reader_uses_two_xml_reads_without_an_engine_probe(self):
        def xml(rows):
            root = ElementTree.Element('resultset')
            for row in rows:
                item = ElementTree.SubElement(root, 'row')
                for key, value in row.items():
                    field = ElementTree.SubElement(item, 'field', name=key)
                    if value is None:
                        field.set('{http://www.w3.org/2001/XMLSchema-instance}nil', 'true')
                    else:
                        field.text = str(value)
            return ElementTree.tostring(root, encoding='unicode')
        for engine in ('mysql', 'mariadb'):
            with self.subTest(engine=engine):
                conn = mock.Mock()
                index = self.index(**({'IS_VISIBLE': 'YES'} if engine == 'mysql' else {}))
                conn.run.side_effect = [xml([self.metadata()]), xml([index])]
                result = discover.digest_tables(conn, 'app', 'SELECT * FROM orders')
                self.assertEqual(conn.run.call_count, 2)
                conn.server_version.assert_not_called()
                conn.is_mariadb.assert_not_called()
                for call in conn.run.call_args_list:
                    self.assertEqual(call[1]['extra_flags'], ['--comments', '--xml'])
                    self.assertTrue(call[0][0].startswith('/*myflames:monitor*/ SELECT '))
                self.assertEqual(result['tables'][0]['indexes'][0]['visible'], True if engine == 'mysql' else None)

    def test_metadata_errors_remain_errors(self):
        for side in (0, 1):
            reader = mock.Mock()
            reader.named.side_effect = [ConnectorError('Access denied')] if side == 0 else [[self.metadata()], ConnectorError('Access denied')]
            with mock.patch('myflames.perf_common.Reader', return_value=reader):
                with self.assertRaisesRegex(ValueError, 'Could not read statement table metadata: Access denied'):
                    discover.digest_tables(mock.Mock(), 'app', 'SELECT * FROM orders')

    def test_validation_happens_before_reader_or_connection(self):
        for schema, text in [('x' * 65, 'SELECT 1'), (12, 'SELECT 1'), ('app', []), ('app', 'x' * 1000001), ('app', 'SELECT\x00')]:
            with mock.patch('myflames.connector.MySQLConnection') as factory:
                with self.assertRaises(ValueError):
                    ui.api_response('/api/live/digest-tables', {'connection': {'host': 'localhost', 'user': 'app'}, 'schema': schema, 'text': text}, workspace=object())
                factory.assert_not_called()

    def test_endpoint_allowlists_timeout_and_saved_password_path(self):
        path = '/api/live/digest-tables'
        self.assertIn(path, ui._SERVER_VIEW_PATHS)
        self.assertIn(path, ui._CONNECTION_PATHS)
        self.assertIn('"%s"' % path, inspect.getsource(ui.WorkspaceHandler.do_POST))
        reader = mock.Mock()
        reader.named.side_effect = [[self.metadata()], [self.index()]]
        with mock.patch('myflames.connector.MySQLConnection') as factory, mock.patch('myflames.perf_common.Reader', return_value=reader):
            result = ui.api_response(path, {'connection': {'host': 'localhost', 'user': 'app'}, 'schema': 'app', 'text': 'SELECT * FROM orders'}, workspace=object())
        self.assertEqual(result['schema_version'], 'digest-tables-1.0')
        self.assertEqual(factory.call_args[1]['query_timeout'], 30)

    def test_contract_schema_matches_report(self):
        result, _ = self.read([self.metadata()], [self.index()])
        path = os.path.join(os.path.dirname(__file__), '..', 'docs', 'schemas', 'digest-tables-1.0.json')
        with open(path) as handle:
            schema = json.load(handle)
        self.assertEqual(set(schema['required']), set(result))
        table_schema = schema['properties']['tables']['items']
        self.assertEqual(set(table_schema['required']), set(result['tables'][0]))
        self.assertEqual(set(table_schema['properties']['indexes']['items']['required']), set(result['tables'][0]['indexes'][0]))


if __name__ == '__main__':
    unittest.main()
