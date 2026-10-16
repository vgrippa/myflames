"""Offline schema interchange and bounded read-only metadata regression tests."""
import copy
import json
import unittest
from unittest import mock
from xml.sax.saxutils import escape

from myflames import schema_diagram as d
from myflames import schema_diagram_api as api
from myflames.connector import ConnectorError

SQL = """USE shop;
CREATE TABLE parent (tenant INT NOT NULL, id INT NOT NULL,
 label VARCHAR(50) DEFAULT 'a; b', PRIMARY KEY (tenant,id)) ENGINE=InnoDB;
CREATE TABLE child (tenant INT NOT NULL, id INT NOT NULL, parent_id INT,
 PRIMARY KEY (tenant,id), KEY ix (tenant,parent_id),
 CONSTRAINT fk FOREIGN KEY (tenant,parent_id) REFERENCES parent (tenant,id) ON DELETE CASCADE);
"""


def xml(rows):
    out = ['<resultset xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">']
    for row in rows:
        out.append('<row>')
        for index, value in enumerate(row):
            if value is None:
                out.append('<field name="c%d" xsi:nil="true" />' % index)
            else:
                out.append('<field name="c%d">%s</field>' % (index, escape(str(value))))
        out.append('</row>')
    return ''.join(out) + '</resultset>'


class Conn:
    def __init__(self, answers=None, error=None):
        self.answers, self.error, self.calls = answers or {}, error, []

    def run(self, sql, extra_flags=None, timeout=None):
        self.calls.append((sql, extra_flags, timeout))
        if self.error:
            raise self.error
        for name, rows in self.answers.items():
            if 'information_schema.' + name + ' ' in sql:
                return xml(rows)
        return xml([])


class TestSQLDiagram(unittest.TestCase):
    def test_composite_keys_and_verified_original_definition(self):
        model = d.import_sql(SQL)
        self.assertFalse(model['warnings'])
        self.assertEqual(model['relationships'][0]['from_columns'], ['tenant', 'parent_id'])
        self.assertEqual(model['relationships'][0]['to_columns'], ['tenant', 'id'])
        self.assertEqual(model['tables'][0]['indexes'][0]['columns'], ['tenant', 'id'])
        self.assertTrue(all(c['primary_key'] for c in model['tables'][0]['columns'][:2]))
        result = d.export_sql(model)
        self.assertFalse(result['warnings'])
        self.assertIn("DEFAULT 'a; b'", result['text'])
        self.assertIn('ENGINE=InnoDB', result['text'])
        self.assertLess(result['text'].rindex('CREATE TABLE'), result['text'].index('ALTER TABLE'))
        again = d.import_sql(result['text'])
        for key in ('from_columns', 'to_columns', 'on_delete'):
            self.assertEqual(again['relationships'][0][key], model['relationships'][0][key])

    def test_quotes_comments_enum_and_reserved_identifiers(self):
        model = d.import_sql("""# heading
        CREATE TABLE `weird``table` (`PRIMARY` ENUM('a,b','it''s','x; y'),
        `NULL` INT, /* x */ `foreign` VARCHAR(9) COMMENT 'ordinary'); -- end
        """)
        self.assertEqual(model['tables'][0]['name'], 'weird`table')
        self.assertEqual([c['name'] for c in model['tables'][0]['columns']], ['PRIMARY', 'NULL', 'foreign'])
        self.assertIn("'it''s'", d.export_sql(model)['text'])

    def test_self_reference_cycles_and_external_database(self):
        model = d.import_sql('''CREATE TABLE a(id INT PRIMARY KEY,b INT, p INT,
          CONSTRAINT ab FOREIGN KEY(b) REFERENCES b(id),
          CONSTRAINT aa FOREIGN KEY(p) REFERENCES a(id));
          CREATE TABLE b(id INT PRIMARY KEY,a INT,
          CONSTRAINT ba FOREIGN KEY(a) REFERENCES a(id),
          CONSTRAINT bx FOREIGN KEY(a) REFERENCES other.x(id));''')
        self.assertEqual(len(model['relationships']), 4)
        external = [t for t in model['tables'] if t.get('external')]
        self.assertEqual([(t['schema'], t['name'], t['columns']) for t in external], [('other', 'x', [])])
        sql = d.export_sql(model)['text']
        self.assertEqual(sql.count('CREATE TABLE'), 2)
        self.assertEqual(sql.count('ALTER TABLE'), 4)
        self.assertIn('REFERENCES `other`.`x`', sql)

    def test_unnamed_keys_receive_unique_model_names(self):
        model = d.import_sql('CREATE TABLE t(id INT, KEY(id), UNIQUE(id), KEY(id));')
        names = [i['name'] for i in model['tables'][0]['indexes']]
        self.assertEqual(names, ['id', 'id_2', 'id_3'])
        self.assertFalse(d.export_sql(model)['warnings'])

    def test_index_algorithm_subset_is_explicit(self):
        for algorithm in ('BTREE', 'HASH'):
            model = d.import_sql('CREATE TABLE t(id INT, KEY ix USING %s(id));' % algorithm)
            self.assertFalse(model['warnings'])
            self.assertIn('USING ' + algorithm, d.export_sql(model)['text'])
        model = d.import_sql('CREATE TABLE t(id INT, KEY ix USING FAKE(id));')
        self.assertTrue(any('Index algorithms' in w for w in model['warnings']))
        self.assertNotIn('sql_definition', model['tables'][0])
        self.assertNotIn('FAKE', d.export_sql(model)['text'])

    def test_alter_uses_schema_at_that_statement(self):
        model = d.import_sql('USE a; CREATE TABLE t(id INT); ALTER TABLE t ADD PRIMARY KEY(id);'
                             'USE b; CREATE TABLE t(id INT); ALTER TABLE t ADD UNIQUE KEY uk(id);')
        self.assertEqual(model['tables'][0]['indexes'][0]['name'], 'PRIMARY')
        self.assertEqual(model['tables'][1]['indexes'][0]['name'], 'uk')
        self.assertFalse(any('sql_definition' in t for t in model['tables']))

    def test_unsupported_clauses_are_explicit_and_not_preserved(self):
        model = d.import_sql("""CREATE TABLE t(id INT, g INT GENERATED ALWAYS AS (id+1),
          KEY p(id(2)), KEY expression ((id+1)), CHECK(id>0))
          CONNECTION='mysql://private:secret@host/db/t';
          INSERT INTO t VALUES(1,2); DROP TABLE t;""")
        self.assertTrue(model['warnings'])
        self.assertNotIn('sql_definition', model['tables'][0])
        self.assertFalse(model['tables'][0]['indexes'])
        text = d.export_sql(model)['text']
        self.assertNotIn('secret', json.dumps(model))
        self.assertNotIn('INSERT', text)
        self.assertNotIn('DROP', text)
        self.assertNotIn('CONNECTION', text)

    def test_version_comments_are_not_executed_or_preserved(self):
        model = d.import_sql('/*!50000 CREATE TABLE hidden(x INT) */; CREATE TABLE t(x INT /*!99999 INVISIBLE */);')
        self.assertEqual([t['name'] for t in model['tables']], ['t'])
        self.assertNotIn('sql_definition', model['tables'][0])
        self.assertTrue(any('executable comments' in w for w in model['warnings']))

    def test_mode_dependent_backslash_default_cannot_survive_export(self):
        model = d.import_sql("CREATE TABLE t(x VARCHAR(20) DEFAULT 'it\\'s');")
        self.assertNotIn('sql_definition', model['tables'][0])
        self.assertNotIn('DEFAULT', d.export_sql(model)['text'])
        self.assertTrue(any('SQL mode' in w for w in model['warnings']))

    def test_changed_model_cannot_keep_stale_definition(self):
        model = d.import_sql('CREATE TABLE t(id INT DEFAULT 7) ENGINE=InnoDB;')
        model['tables'][0]['columns'][0]['data_type'] = 'VARCHAR(20)'
        result = d.export_sql(model)
        self.assertIn('VARCHAR ( 20 )', result['text'])
        self.assertNotIn('DEFAULT 7', result['text'])
        self.assertTrue(result['warnings'])

    def test_untrusted_sql_definition_is_never_copied_blindly(self):
        model = d.import_sql('CREATE TABLE t(id INT);')
        for raw in ('CREATE TABLE t(id INT); DROP USER root;',
                    'CREATE TABLE t(id INT) CONNECTION=\'mysql://secret\';',
                    'CREATE TABLE t(id INT DEFAULT (evil()));',
                    'CREATE TABLE other(id INT);'):
            with self.subTest(raw=raw):
                model['tables'][0]['sql_definition'] = raw
                result = d.export_sql(model)
                self.assertTrue(result['warnings'])
                for forbidden in ('DROP USER', 'CONNECTION', 'evil', 'CREATE TABLE `other`'):
                    self.assertNotIn(forbidden, result['text'])

    def test_type_and_identifier_injection_are_safe(self):
        model = d.import_sql('CREATE TABLE t(id INT);')
        model['tables'][0].pop('sql_definition')
        model['tables'][0]['name'] = 't`; DROP USER root; --'
        model['tables'][0]['columns'][0]['data_type'] = 'INT); DROP USER root; --'
        # Invalid type syntax is rejected, rather than returned for execution.
        with self.assertRaises(ValueError):
            d.export_sql(model)
        model['tables'][0]['columns'][0]['data_type'] = 'INT /* ordinary */'
        result = d.export_sql(model)['text']
        self.assertIn('`t``; DROP USER root; --`', result)
        self.assertNotIn('/* ordinary */', result)
        self.assertEqual(len(d._split(d._tokens(result), ';')), 1)

    def test_create_like_as_select_and_other_statements_warn(self):
        result = d.import_sql('CREATE TABLE a LIKE b; CREATE TABLE c AS SELECT 1;'
                              'SET @x=1; CREATE VIEW v AS SELECT 1;')
        self.assertFalse(result['tables'])
        self.assertGreaterEqual(len(result['warnings']), 3)

    def test_malformed_input_and_resource_limits(self):
        for text in ("CREATE TABLE t(x INT", "CREATE TABLE t(x VARCHAR(3) DEFAULT 'a);",
                     '/* unclosed', 'CREATE TABLE t(x INT, x INT);',
                     'CREATE TABLE t(x INT, FOREIGN KEY(x) REFERENCES y(a,b));'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                d.import_sql(text)
        with self.assertRaises(ValueError): d.import_sql('(' * 65 + ')' * 65)
        with self.assertRaises(ValueError): d.import_sql('é' * (d.MAX_BYTES // 2 + 1))
        with mock.patch.object(d, 'MAX_TOKENS', 10), self.assertRaises(ValueError):
            d.import_sql('CREATE TABLE t(a INT,b INT,c INT);')
        with mock.patch.object(d, 'MAX_STATEMENTS', 1), self.assertRaises(ValueError):
            d.import_sql('SET @a=1; SET @b=2;')
        with mock.patch.object(d, 'MAX_TABLES', 1), self.assertRaises(ValueError):
            d.import_sql('CREATE TABLE a(x INT); CREATE TABLE b(x INT);')


class TestDiagramJSON(unittest.TestCase):
    def test_lossless_model_layout_roundtrip(self):
        model = d.import_sql(SQL)
        tid = model['tables'][0]['id']
        model['layout'] = {'positions': {tid: {'x': -13.25, 'y': 50}}}
        self.assertEqual(d.import_diagram('json', json.dumps(model)), model)
        clone = d.validate_diagram(model)
        clone['tables'][0]['columns'][0]['name'] = 'changed'
        self.assertEqual(model['tables'][0]['columns'][0]['name'], 'tenant')

    def test_reject_graph_inconsistencies(self):
        base = d.import_sql(SQL)
        for modify in (
            lambda m: m['tables'].append(copy.deepcopy(m['tables'][0])),
            lambda m: m['tables'][0]['columns'].append(copy.deepcopy(m['tables'][0]['columns'][0])),
            lambda m: m['tables'][0]['columns'][0].update(nullable=1),
            lambda m: m['tables'][0]['columns'][0].update(primary_key=False),
            lambda m: m['tables'][0]['columns'][0].update(nullable=True),
            lambda m: m['tables'][0]['indexes'][0].update(columns=['absent']),
            lambda m: m['relationships'][0].update(to_table='absent'),
            lambda m: m['relationships'][0].update(from_columns=[{}]),
            lambda m: m['relationships'][0].update(on_delete='CASCADE; DROP USER root'),
            lambda m: m.update(layout={'positions': {'absent': {'x': 1, 'y': 2}}}),
        ):
            model = copy.deepcopy(base); modify(model)
            with self.assertRaises(ValueError): d.validate_diagram(model)

    def test_json_structural_limits_and_invalid_values(self):
        cycle = {}; cycle['cycle'] = cycle
        nested = []
        for _ in range(34): nested = [nested]
        for value in (cycle, nested, {'huge': 2 ** 1000}, {'float': float('nan')},
                      {'bad': object()}, {'huge': 'é' * (d.MAX_BYTES // 2 + 1)}):
            with self.assertRaises(ValueError): d.validate_diagram(value)
        for text in ('{', '[' * 2000 + ']' * 2000, '{"schema_version":"wrong"}'):
            with self.assertRaises(ValueError): d.import_diagram('json', text)

    def test_layout_bounds_match_frontend_without_clamping(self):
        model = d.import_sql('CREATE TABLE t(id INT);')
        tid = model['tables'][0]['id']
        model['layout'] = {'positions': {tid: {'x': -100000, 'y': 100000}}}
        self.assertEqual(d.validate_diagram(model)['layout'], model['layout'])
        for coordinate in (-100000.1, 100000.1):
            model['layout']['positions'][tid]['x'] = coordinate
            with self.assertRaisesRegex(ValueError, 'position'): d.validate_diagram(model)

    def test_exact_compact_utf8_model_limit_after_structural_guard(self):
        model = d.import_sql('CREATE TABLE t(' + ','.join('c%d INT NOT NULL' % i for i in range(50)) + ');')
        model['tables'][0].pop('sql_definition')
        wire = json.dumps(model, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        dumps = json.dumps
        with mock.patch.object(d, 'MAX_BYTES', len(wire) - 1):
            with mock.patch.object(d.json, 'dumps', wraps=dumps) as serialized:
                with self.assertRaisesRegex(ValueError, '2 MiB'): d.validate_diagram(model)
                self.assertTrue(serialized.called)
        with mock.patch.object(d, 'MAX_BYTES', len(wire)):
            self.assertEqual(d.validate_diagram(model), model)

    def test_completeness_and_unknown_identity_roundtrip(self):
        model = d.import_sql(SQL)
        model['incomplete'] = True
        model['relationships'][0]['identifying'] = None
        self.assertEqual(d.validate_diagram(model), model)
        model['incomplete'] = 1
        with self.assertRaises(ValueError): d.validate_diagram(model)

    def test_unknown_json_fields_cannot_reach_output(self):
        model = d.import_sql('CREATE TABLE t(id INT);')
        model['tables'][0]['html'] = '<script>evil()</script>'
        self.assertNotIn('html', d.validate_diagram(model)['tables'][0])


class TestMermaidDiagram(unittest.TestCase):
    def test_aliases_attributes_and_conceptual_relationships(self):
        model = d.import_mermaid('''erDiagram
          A["The &lt;customer&gt;"] {
            int id PK
            string? name
          }
          B { int customer_id FK, UK }
          A ||--o{ B : "owns &amp; sees"
        ''')
        self.assertEqual(model['tables'][0]['name'], 'The <customer>')
        self.assertEqual(model['tables'][0]['columns'][0]['nullable'], False)
        rel = model['relationships'][0]
        self.assertEqual(rel['from_columns'], [])
        self.assertEqual(rel['cardinality'], {'from': 'one', 'to': 'zero-or-many'})
        self.assertTrue(rel['identifying'])
        self.assertNotIn('FOREIGN KEY', d.export_sql(model)['text'])
        result = d.export_mermaid(model)
        self.assertIn('&lt;customer&gt;', result['text'])
        again = d.import_mermaid(result['text'])
        self.assertEqual(again['relationships'][0]['cardinality'], rel['cardinality'])

    def test_cardinality_endpoints_and_nonidentifying(self):
        for left, lc in d._LEFT_CARD.items():
            for right, rc in d._RIGHT_CARD.items():
                model = d.import_mermaid('erDiagram\na %s..%s b : relation' % (left, right))
                rel = model['relationships'][0]
                self.assertEqual(rel['cardinality'], {'from': lc, 'to': rc})
                self.assertFalse(rel['identifying'])
                self.assertIn(left + '..' + right, d.export_mermaid(model)['text'])

    def test_types_and_names_with_no_mermaid_equivalent_warn(self):
        model = d.import_sql("CREATE TABLE t(`two words` DECIMAL(10,2), e ENUM('x','y'));")
        result = d.export_mermaid(model)
        self.assertIn('column_1', result['text'])
        self.assertIn('string', result['text'])
        self.assertTrue(result['warnings'])
        self.assertEqual(len(d.import_mermaid(result['text'])['tables'][0]['columns']), 2)

    def test_fallback_attribute_names_do_not_collide(self):
        model = d.import_sql('CREATE TABLE t(`two words` INT, column_1 INT, column_2 INT, `more words` INT);')
        result = d.import_mermaid(d.export_mermaid(model)['text'])
        names = [c['name'] for c in result['tables'][0]['columns']]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn('column_1', names)
        self.assertIn('column_2', names)

    def test_unsupported_directives_and_empty_entity(self):
        model = d.import_mermaid('erDiagram\n%% ordinary\nEmpty {}\ndirection LR\nclassDef red fill:red;')
        self.assertEqual([t['name'] for t in model['tables']], ['Empty'])
        self.assertTrue(model['warnings'])
        self.assertEqual(d.export_sql(model)['text'], '')
        for text in ('flowchart LR\na-->b', 'erDiagram\na {\nint id'):
            with self.assertRaises(ValueError): d.import_mermaid(text)

    def test_mermaid_limits_checked_during_parse(self):
        with mock.patch.object(d, 'MAX_COLUMNS', 1), self.assertRaises(ValueError):
            d.import_mermaid('erDiagram\na {\nint x\nint y\n}')
        with mock.patch.object(d, 'MAX_RELATIONSHIPS', 1), self.assertRaises(ValueError):
            d.import_mermaid('erDiagram\na ||--|| b : x\na ||--|| b : y')

    def test_live_fk_cardinality_uses_nullable_and_known_unique_key(self):
        model = d.import_sql(SQL)
        self.assertIn('}o..o|', d.export_mermaid(model)['text'])
        child = model['tables'][1]
        child['columns'][2]['nullable'] = False
        self.assertIn('}o..||', d.export_mermaid(model)['text'])


class TestSchemaRead(unittest.TestCase):
    def answers(self):
        return {
            'TABLES': [['parent'], ['child'], ['NULL']],
            'COLUMNS': [['parent','tenant','int','NO'], ['parent','id','int','NO'],
                        ['child','tenant','int','NO'], ['child','pid','int','YES'], ['NULL','NULL','int','YES']],
            'STATISTICS': [['parent','PRIMARY','0','1','tenant',None,'BTREE'],
                           ['parent','PRIMARY','0','2','id',None,'BTREE'],
                           ['child','ix','1','1','tenant',None,'BTREE'],
                           ['child','ix','1','2','pid',None,'BTREE']],
            'KEY_COLUMN_USAGE': [['child','fk','tenant','shop','parent','tenant','1','RESTRICT','CASCADE'],
                                 ['child','fk','pid','shop','parent','id','2','RESTRICT','CASCADE']]}

    def test_four_bounded_tagged_reads_preserve_order_and_null_names(self):
        conn = Conn(self.answers())
        model = d.read_diagram(conn, 'shop')
        self.assertEqual(len(conn.calls), 4)
        self.assertEqual(model['tables'][2]['name'], 'NULL')
        self.assertEqual(model['tables'][2]['columns'][0]['name'], 'NULL')
        self.assertEqual(model['relationships'][0]['from_columns'], ['tenant', 'pid'])
        self.assertEqual(model['tables'][0]['indexes'][0]['columns'], ['tenant', 'id'])
        for sql, flags, timeout in conn.calls:
            self.assertTrue(sql.startswith(d.TAG + 'SELECT'))
            self.assertIn('LIMIT ', sql)
            self.assertIn("CONVERT(X'73686f70' USING utf8mb4)", sql)
            self.assertEqual(flags, ['--comments', '--xml'])
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 30)

    def test_cross_database_targets_and_unsupported_indexes(self):
        answers = self.answers()
        answers['KEY_COLUMN_USAGE'][0][3] = 'other'
        answers['KEY_COLUMN_USAGE'][1][3] = 'other'
        answers['STATISTICS'].append(['child','prefix','1','1','pid','3','BTREE'])
        model = d.read_diagram(Conn(answers), 'shop')
        external = [t for t in model['tables'] if t.get('external')]
        self.assertEqual(external[0]['schema'], 'other')
        self.assertFalse(external[0]['columns'])
        self.assertTrue(any('prefix' in w for w in model['warnings']))

    def test_errors_and_read_budget_never_return_credentials(self):
        conn = Conn(error=ConnectorError('private-password'))
        with self.assertRaises(ValueError) as caught: d.read_diagram(conn, 'shop')
        self.assertNotIn('private-password', str(caught.exception))
        with mock.patch.object(d.time, 'monotonic', side_effect=[0, 31]), self.assertRaises(ValueError):
            d.read_diagram(Conn(), 'shop')
        conn = Conn({'TABLES': [['t%d' % n] for n in range(201)]})
        with self.assertRaises(ValueError): d.read_diagram(conn, 'shop')
        self.assertEqual(len(conn.calls), 1)

    def test_schema_name_is_encoded_and_invalid_schema_is_early(self):
        conn = Conn()
        d.read_diagram(conn, "shop'; DROP")
        self.assertNotIn('DROP', conn.calls[0][0])
        conn = Conn()
        with self.assertRaises(ValueError): d.read_diagram(conn, 'bad\nname')
        self.assertFalse(conn.calls)


class TestDiagramAPI(unittest.TestCase):
    def test_import_and_export_do_not_open_connection_or_saved_password(self):
        from myflames.ui import api_response
        with mock.patch.object(api, 'MySQLConnection', side_effect=AssertionError('must stay offline')):
            model = api_response('/api/schema-diagram/import', {'format':'sql','text':SQL})
            result = api_response('/api/schema-diagram/export', {'format':'sql','diagram':model})
        self.assertIn('CREATE TABLE', result['text'])

    def test_read_saved_credentials_and_timeout(self):
        conn = Conn()
        context = mock.MagicMock()
        context.__enter__.return_value = conn
        saved = mock.Mock(return_value={'connection': {'host':'localhost','user':'test','password':'saved-secret'}})
        with mock.patch.object(api, 'MySQLConnection', return_value=context) as factory:
            result = api.response(api.READ_PATH, {'database':'shop','connection':{'host':'localhost'}}, saved)
        self.assertEqual(result['schema_version'], d.SCHEMA_VERSION)
        self.assertEqual(factory.call_args[1]['password'], 'saved-secret')
        self.assertEqual(factory.call_args[1]['query_timeout'], 30)
        self.assertEqual(saved.call_count, 1)

    def test_bad_request_rejected_before_saved_secret_or_connection(self):
        saved = mock.Mock(side_effect=AssertionError('invalid request reached secret lookup'))
        for payload in ({'database':'bad\nname','connection':{}}, {'database':'x','connection':{'host':{}}},
                        {'database':'x','connection':{'password':'secret'*1000}},
                        {'database':'x','connection':{'port':2**1000}}, []):
            with self.assertRaises(ValueError): api.response(api.READ_PATH, payload, saved)
        self.assertFalse(saved.called)

    def test_paths_and_bad_formats(self):
        from myflames import ui
        self.assertIn(api.READ_PATH, ui._CONNECTION_PATHS)
        self.assertNotIn('/api/schema-diagram/import', ui._CONNECTION_PATHS)
        with self.assertRaises(ValueError): api.response('/bad', {})
        with self.assertRaises(ValueError): d.import_diagram('html','<b>x</b>')
        with self.assertRaises(ValueError): d.export_diagram('html',d._blank())


if __name__ == '__main__':
    unittest.main()
