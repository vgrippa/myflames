"""Unit tests for schema_ddl.py: splitting and normalizing SHOW CREATE text.

The table texts under test come from the recorded schema version fixtures
(test/fixtures/schema_versions), which hold real SHOW CREATE output from
MySQL 8.4.11 and MariaDB 11.4.13.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _schema_replay as replay  # noqa: E402

from myflames import schema_ddl  # noqa: E402


def fixture_tables():
    """(server, which, name, ddl) for every table of every fixture snapshot."""
    for server in replay.SERVERS:
        for which in replay.WHICH:
            snapshot = replay.load('%s-%s' % (server, which))
            for obj in snapshot['objects']:
                if obj['type'] == 'table' and obj.get('ddl'):
                    yield server, which, obj['name'], obj['ddl']


class TestReadIdentifier(unittest.TestCase):
    def test_backquoted_with_doubled_backquote(self):
        self.assertEqual(schema_ddl.read_identifier('`a``b` rest', 0), ('a`b', 6))

    def test_double_quoted_with_doubled_quote(self):
        self.assertEqual(schema_ddl.read_identifier('"a""b" rest', 0), ('a"b', 6))

    def test_bare_word(self):
        self.assertEqual(schema_ddl.read_identifier('word more', 0), ('word', 4))

    def test_unclosed_quote(self):
        self.assertIsNone(schema_ddl.read_identifier('`never-closed', 0))
        self.assertIsNone(schema_ddl.read_identifier('"never-closed', 0))

    def test_past_the_end(self):
        self.assertIsNone(schema_ddl.read_identifier('', 0))


class TestSplitTable(unittest.TestCase):
    def test_round_trip_rebuilds_every_fixture_table(self):
        # Rebuilding "  " + text joined with ",\n" between the header line and
        # "\n)" + options reproduces the ddl, so the split loses nothing.
        # split_table strips the partitioning block, and MariaDB prints its
        # first line indented (" PARTITION BY"), so compare against the ddl
        # with that one indentation removed.
        for server, which, name, ddl in fixture_tables():
            with self.subTest(server=server, which=which, table=name):
                parts = schema_ddl.split_table(ddl)
                self.assertIsNotNone(parts, 'SHOW CREATE text must split')
                self.assertEqual(parts['name'], name)
                region = ',\n'.join('  ' + element['text'] for element in parts['elements'])
                rebuilt = 'CREATE TABLE %s (\n%s\n)%s' % (
                    schema_ddl.quote(parts['name']), region,
                    ' ' + parts['options'] if parts['options'] else '')
                if parts['partitioning']:
                    rebuilt += '\n' + parts['partitioning']
                    check = ddl.replace('\n ' + parts['partitioning'],
                                        '\n' + parts['partitioning'], 1)
                else:
                    check = ddl
                self.assertEqual(rebuilt, check)

    def test_elements_are_columns_keys_and_constraints(self):
        allowed = set(schema_ddl.KINDS)
        for server, which, name, ddl in fixture_tables():
            with self.subTest(server=server, which=which, table=name):
                parts = schema_ddl.split_table(ddl)
                kinds = [element['kind'] for element in parts['elements']]
                self.assertTrue(set(kinds) <= allowed, kinds)
                self.assertIn('column', kinds)

    def test_options_never_contain_auto_increment(self):
        for server, which, name, ddl in fixture_tables():
            with self.subTest(server=server, which=which, table=name):
                self.assertNotIn('AUTO_INCREMENT=',
                                 schema_ddl.split_table(ddl)['options'])

    def test_partitioning_kept(self):
        mysql_logs = [ddl for server, which, name, ddl in fixture_tables()
                      if server == 'mysql-8.4' and name == 'logs'][0]
        parts = schema_ddl.split_table(mysql_logs)
        self.assertTrue(parts['partitioning'].startswith('/*!50100 PARTITION BY'))
        maria_logs = [ddl for server, which, name, ddl in fixture_tables()
                      if server == 'mariadb-11.4' and name == 'logs'][0]
        parts = schema_ddl.split_table(maria_logs)
        self.assertTrue(parts['partitioning'].startswith('PARTITION BY'))
        self.assertNotIn('/*!', parts['partitioning'])

    def test_none_for_non_table_text(self):
        view = [obj for obj in replay.load('mysql-8.4-before')['objects']
                if obj['type'] == 'view'][0]
        self.assertIsNone(schema_ddl.split_table(view['ddl']))
        self.assertIsNone(schema_ddl.split_table(None))
        self.assertIsNone(schema_ddl.split_table(''))

    def test_none_without_opening_parenthesis_line(self):
        self.assertIsNone(schema_ddl.split_table('CREATE TABLE `x` (id int)'))

    def test_none_when_a_middle_line_loses_its_comma(self):
        customers = [ddl for server, which, name, ddl in fixture_tables()
                     if name == 'customers' and server == 'mysql-8.4'][0]
        broken = customers.replace('AUTO_INCREMENT,\n', 'AUTO_INCREMENT\n', 1)
        self.assertIsNone(schema_ddl.split_table(broken))


class TestStripAutoIncrement(unittest.TestCase):
    def test_removed_from_options_line(self):
        ddl = ('CREATE TABLE `t` (\n  `id` int NOT NULL\n) '
               'ENGINE=InnoDB AUTO_INCREMENT=17 DEFAULT CHARSET=utf8mb4')
        self.assertEqual(
            schema_ddl.strip_auto_increment(ddl),
            'CREATE TABLE `t` (\n  `id` int NOT NULL\n) '
            'ENGINE=InnoDB DEFAULT CHARSET=utf8mb4')

    def test_comment_literal_untouched(self):
        ddl = ("CREATE TABLE `t` (\n  `id` int NOT NULL\n) "
               "ENGINE=InnoDB AUTO_INCREMENT=17 COMMENT='x AUTO_INCREMENT=5'")
        self.assertEqual(
            schema_ddl.strip_auto_increment(ddl),
            "CREATE TABLE `t` (\n  `id` int NOT NULL\n) "
            "ENGINE=InnoDB COMMENT='x AUTO_INCREMENT=5'")

    def test_column_line_untouched(self):
        ddl = 'CREATE TABLE `t` (\n  `id` int NOT NULL AUTO_INCREMENT\n) ENGINE=InnoDB'
        self.assertEqual(schema_ddl.strip_auto_increment(ddl), ddl)

    def test_text_that_does_not_split_is_returned(self):
        self.assertEqual(schema_ddl.strip_auto_increment('no table'), 'no table')


class TestElementClassification(unittest.TestCase):
    def test_kinds(self):
        cases = [
            ('`email` varchar(100) NOT NULL', 'column', 'email'),
            ('PRIMARY KEY (`id`)', 'index', 'PRIMARY'),
            ('UNIQUE KEY `uq` (`a`)', 'index', 'uq'),
            ('KEY `k` (`a`)', 'index', 'k'),
            ('FULLTEXT KEY `ft` (`b`)', 'index', 'ft'),
            ('SPATIAL KEY `sp` (`g`)', 'index', 'sp'),
            ('CONSTRAINT `fk` FOREIGN KEY (`a`) REFERENCES `t` (`id`)',
             'foreign_key', 'fk'),
            ('CONSTRAINT `ck` CHECK (`a` > 0)', 'check', 'ck'),
            ('PERIOD FOR SYSTEM_TIME (`from`, `to`)', 'period', 'SYSTEM_TIME'),
            ('SOME FUTURE THING (`x`)', 'other', 'SOME FUTURE THING (`x`)'),
        ]
        for text, kind, name in cases:
            with self.subTest(text=text):
                item = schema_ddl.element(text)
                self.assertEqual(item['kind'], kind)
                self.assertEqual(item['name'], name)
                self.assertEqual(item['text'], text)


class TestSplitDefiner(unittest.TestCase):
    def test_mysql_view(self):
        view = [obj for obj in replay.load('mysql-8.4-before')['objects']
                if obj['type'] == 'view'][0]['ddl']
        definer, rest = schema_ddl.split_definer(view)
        self.assertEqual(definer, '`root`@`%`')
        self.assertNotIn('DEFINER=', rest)
        self.assertTrue(rest.startswith('CREATE ALGORITHM=UNDEFINED SQL SECURITY'))

    def test_procedure(self):
        proc = [obj for obj in replay.load('mysql-8.4-before')['objects']
                if obj['type'] == 'procedure'][0]['ddl']
        definer, rest = schema_ddl.split_definer(proc)
        self.assertEqual(definer, '`root`@`%`')
        self.assertTrue(rest.startswith('CREATE PROCEDURE `p_touch`'))

    def test_mariadb_package_oracle_mode(self):
        package = [obj for obj in replay.load('mariadb-11.4-before')['objects']
                   if obj['type'] == 'package'][0]['ddl']
        definer, rest = schema_ddl.split_definer(package)
        self.assertEqual(definer, '"root"@"%"')
        self.assertTrue(rest.startswith('CREATE PACKAGE "pkg_util"'))

    def test_definer_without_host(self):
        definer, rest = schema_ddl.split_definer(
            'CREATE DEFINER=`app` PROCEDURE `p`() BEGIN END')
        self.assertEqual(definer, '`app`')
        self.assertTrue(rest.startswith('CREATE PROCEDURE'))

    def test_no_definer(self):
        text = 'CREATE TABLE `x` (`i` int)'
        self.assertEqual(schema_ddl.split_definer(text), (None, text))
        self.assertEqual(schema_ddl.split_definer(''), (None, ''))


class TestRequalify(unittest.TestCase):
    def test_replaces_only_backquoted_form(self):
        text = 'select * from `db`.`t` join db.t join `db2`.`t`'
        self.assertEqual(schema_ddl.requalify(text, 'db', 'new'),
                         'select * from `new`.`t` join db.t join `db2`.`t`')

    def test_noop_when_old_equals_new(self):
        text = 'select * from `db`.`t`'
        self.assertEqual(schema_ddl.requalify(text, 'db', 'db'), text)

    def test_a_name_after_a_dot_is_never_the_database(self):
        text = 'select `x`.`db`.`id` from `x`.`db`'
        self.assertEqual(schema_ddl.requalify(text, 'db', 'new'), text)

    def test_an_alias_named_like_the_database_keeps_its_columns(self):
        text = 'select `shop`.`id` AS `id` from `shop`.`orders` `shop`'
        self.assertEqual(schema_ddl.requalify(text, 'shop', 'copy', {'orders'}),
                         'select `shop`.`id` AS `id` from `copy`.`orders` `shop`')
        # A derived table's alias counts too; a variable of that name does not.
        self.assertEqual(schema_ddl.requalify('select `shop`.`id` from (select 1 AS `id`) `shop` join `shop`.`t` `o`',
                                              'shop', 'copy', {'t'}),
                         'select `shop`.`id` from (select 1 AS `id`) `shop` join `copy`.`t` `o`')
        body = 'BEGIN DECLARE `shop` INT; INSERT INTO `shop`.`scratch` VALUES (`shop`); END'
        self.assertEqual(schema_ddl.requalify(body, 'shop', 'copy', {'t'}),
                         'BEGIN DECLARE `shop` INT; INSERT INTO `copy`.`scratch` VALUES (`shop`); END')
        # A literal or a comment naming it is not an alias.
        self.assertEqual(schema_ddl.requalify("select 'shop', `t`.`x` /* shop */ from `shop`.`t` `t`", 'shop', 'copy', {'t'}),
                         "select 'shop', `t`.`x` /* shop */ from `copy`.`t` `t`")

    def test_a_table_named_like_its_database_keeps_its_columns(self):
        # `orders`.`id` is the column id of the table orders, not the database.
        text = 'select `orders`.`id` AS `id`, `orders`.`f`(1) from `orders`.`orders` where `orders`.`orders`.`id` > 0'
        self.assertEqual(schema_ddl.requalify(text, 'orders', 'copy', {'orders', 'f'}),
                         'select `orders`.`id` AS `id`, `copy`.`f`(1) from `copy`.`orders` where `copy`.`orders`.`id` > 0')
        # Without the clash the names do not matter.
        self.assertEqual(schema_ddl.requalify('select `t`.`id` from `shop`.`t`', 'shop', 's2', {'t'}),
                         'select `t`.`id` from `s2`.`t`')


class TestTableOptions(unittest.TestCase):
    def test_default_charset_is_one_option(self):
        options = schema_ddl.table_options('DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin')
        self.assertEqual(options['DEFAULT CHARSET'], 'DEFAULT CHARSET=utf8mb4')
        self.assertEqual(options['COLLATE'], 'COLLATE=utf8mb4_bin')

    def test_quoted_comment_with_spaces_and_doubled_quotes(self):
        options = schema_ddl.table_options("COMMENT='a b ''q'' c' ENGINE=InnoDB")
        self.assertEqual(options['COMMENT'], "COMMENT='a b ''q'' c'")
        self.assertEqual(options['ENGINE'], 'ENGINE=InnoDB')

    def test_version_comment_keyed_by_inner_option(self):
        options = schema_ddl.table_options("/*!80016 ENCRYPTION='N' */")
        self.assertEqual(options['ENCRYPTION'], "/*!80016 ENCRYPTION='N' */")
        options = schema_ddl.table_options('/*!50100 TABLESPACE `ts` */')
        self.assertEqual(options['TABLESPACE'], '/*!50100 TABLESPACE `ts` */')

    def test_with_system_versioning_is_one_option(self):
        options = schema_ddl.table_options('ENGINE=InnoDB WITH SYSTEM VERSIONING')
        self.assertEqual(options['WITH SYSTEM VERSIONING'], 'WITH SYSTEM VERSIONING')
        self.assertEqual(options['ENGINE'], 'ENGINE=InnoDB')


class TestNamesAndTypes(unittest.TestCase):
    def test_foreign_key_unqualified_reference(self):
        item = schema_ddl.foreign_key(
            'CONSTRAINT `fk` FOREIGN KEY (`a`,`b`) REFERENCES `t` (`id`)')
        self.assertEqual(item['columns'], ['a', 'b'])
        self.assertIsNone(item['schema'])
        self.assertEqual(item['table'], 't')
        self.assertEqual(item['ref_columns'], ['id'])

    def test_foreign_key_schema_qualified_reference(self):
        item = schema_ddl.foreign_key(
            'CONSTRAINT `fk` FOREIGN KEY (`a`) REFERENCES `other`.`t2` (`x`)')
        self.assertEqual(item['schema'], 'other')
        self.assertEqual(item['table'], 't2')

    def test_index_columns(self):
        self.assertEqual(schema_ddl.index_columns('KEY `k` (`a`(10) DESC)'), ['a'])
        self.assertEqual(schema_ddl.index_columns('PRIMARY KEY (`a`,`b`)'), ['a', 'b'])
        # A functional part gives no column name.
        self.assertEqual(schema_ddl.index_columns('KEY `idx_func` (((`total` * 2)))'), [])

    def test_column_type_end(self):
        cases = [
            ("`e` enum('a','b c') NOT NULL", "enum('a','b c')"),
            ('`v` varchar(10) DEFAULT NULL', 'varchar(10)'),
            ('`i` int unsigned NOT NULL', 'int'),
            ('`d` decimal(10,2) DEFAULT 0', 'decimal(10,2)'),
        ]
        for line, type_text in cases:
            with self.subTest(line=line):
                end = schema_ddl.column_type_end(line)
                self.assertEqual(line[:end], line.split(' ', 1)[0] + ' ' + type_text)


class TestColumnBodyAndCharset(unittest.TestCase):
    def test_column_body_strips_charset_and_collate_after_type(self):
        line = '`name` varchar(80) CHARACTER SET latin1 COLLATE latin1_swedish_ci DEFAULT NULL'
        self.assertEqual(schema_ddl.column_body(line), '`name` varchar(80) DEFAULT NULL')

    def test_column_body_keeps_charset_clauses_elsewhere(self):
        line = '`g` varchar(80) GENERATED ALWAYS AS (convert(`x` using utf8mb4)) VIRTUAL'
        self.assertEqual(schema_ddl.column_body(line), line)

    def test_with_charset_inserts_after_type(self):
        line = '`email` varchar(255) NOT NULL'
        self.assertEqual(
            schema_ddl.with_charset(line, ['utf8mb4', 'utf8mb4_0900_ai_ci']),
            '`email` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL')

    def test_with_charset_removes_existing_first(self):
        line = '`name` varchar(80) CHARACTER SET latin1 COLLATE latin1_swedish_ci DEFAULT NULL'
        self.assertEqual(
            schema_ddl.with_charset(line, ['utf8mb4', 'utf8mb4_bin']),
            '`name` varchar(80) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL')

    def test_with_charset_without_pair_is_noop(self):
        line = '`name` varchar(80) DEFAULT NULL'
        self.assertEqual(schema_ddl.with_charset(line, None), line)
        self.assertEqual(schema_ddl.with_charset(line, ['utf8mb4', '']), line)


class TestLinesDiff(unittest.TestCase):
    def test_equal_texts_give_no_lines(self):
        self.assertEqual(schema_ddl.lines_diff('a\nb', 'a\nb'), [])
        self.assertEqual(schema_ddl.lines_diff('', ''), [])

    def test_one_line_change(self):
        diff = schema_ddl.lines_diff('a\nb\nc', 'a\nB\nc')
        ops = [op for op, _ in diff]
        self.assertEqual(ops[0], '@')
        self.assertTrue(diff[0][1].startswith('@@ -'))
        self.assertIn('-', ops)
        self.assertIn('+', ops)
        self.assertEqual([text for op, text in diff if op == '-'], ['b'])
        self.assertEqual([text for op, text in diff if op == '+'], ['B'])

    def test_context_lines_keep_their_text(self):
        diff = schema_ddl.lines_diff('a\nb\nc', 'a\nB\nc')
        context = [text for op, text in diff if op == ' ']
        self.assertEqual(context, ['a', 'c'])


class TestSourceReviewParsing(unittest.TestCase):
    def test_directory_options_are_one_option_each(self):
        options = schema_ddl.table_options("ENGINE=InnoDB DATA DIRECTORY='/d' INDEX DIRECTORY='/i' COMMENT='x'")
        self.assertEqual(options['DATA DIRECTORY'], "DATA DIRECTORY='/d'")
        self.assertEqual(options['INDEX DIRECTORY'], "INDEX DIRECTORY='/i'")
        self.assertNotIn('DATA', options)

    def test_mariadb_compressed_column_keeps_its_comment_before_the_character_set(self):
        line = '`c` varchar(10) /*M!100301 COMPRESSED*/ DEFAULT NULL'
        self.assertEqual(line[:schema_ddl.column_type_end(line)], '`c` varchar(10) /*M!100301 COMPRESSED*/')
        self.assertEqual(schema_ddl.with_charset(line, ['utf8mb4', 'utf8mb4_bin']),
                         '`c` varchar(10) /*M!100301 COMPRESSED*/ CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL')
        self.assertEqual(schema_ddl.column_body('`c` varchar(10) /*M!100301 COMPRESSED*/ CHARACTER SET latin1 '
                                                'COLLATE latin1_bin DEFAULT NULL'),
                         '`c` varchar(10) /*M!100301 COMPRESSED*/ DEFAULT NULL')


if __name__ == '__main__':
    unittest.main()
