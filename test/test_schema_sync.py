"""Unit tests for schema_sync.py: the SQL that would turn Target into Source.

Expectations are derived from the recorded fixture DDL (test/fixtures/
schema_versions): statement text, order, phases, risks and warnings. The
synthetic cases edit copies of the fixture snapshots and compare again.
"""
import copy
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _schema_replay as replay  # noqa: E402

from test_chart_data import validate as validate_json_schema  # noqa: E402

from myflames import alter_algorithm, schema_sync, schema_versions, schema_versions_cli  # noqa: E402
from myflames.workspace_store import WorkspaceStore  # noqa: E402

MYSQL_CUSTOMERS_ALTER = (
    "ALTER TABLE `customers`\n"
    "  DROP INDEX `idx_name`,\n"
    "  DROP COLUMN `score`,\n"
    "  MODIFY COLUMN `status` enum('a','b','c') CHARACTER SET utf8mb4 "
    "COLLATE utf8mb4_0900_ai_ci NOT NULL DEFAULT 'a' AFTER `id`,\n"
    "  MODIFY COLUMN `email` varchar(255) CHARACTER SET utf8mb4 "
    "COLLATE utf8mb4_0900_ai_ci NOT NULL COMMENT 'login',\n"
    "  ADD COLUMN `phone` varchar(20) CHARACTER SET utf8mb4 "
    "COLLATE utf8mb4_0900_ai_ci DEFAULT NULL AFTER `email`,\n"
    "  ADD KEY `idx_name` (`name`(20)),\n"
    "  ADD KEY `idx_status` (`status`),\n"
    "  COMMENT='Customer accounts'"
)

MARIADB_CUSTOMERS_ALTER = (
    "ALTER TABLE `customers`\n"
    "  DROP INDEX `idx_name`,\n"
    "  DROP COLUMN `score`,\n"
    "  MODIFY COLUMN `status` enum('a','b','c') CHARACTER SET utf8mb4 "
    "COLLATE utf8mb4_uca1400_ai_ci NOT NULL DEFAULT 'a' AFTER `id`,\n"
    "  MODIFY COLUMN `email` varchar(255) CHARACTER SET utf8mb4 "
    "COLLATE utf8mb4_uca1400_ai_ci NOT NULL COMMENT 'login',\n"
    "  ADD COLUMN `phone` varchar(20) CHARACTER SET utf8mb4 "
    "COLLATE utf8mb4_uca1400_ai_ci DEFAULT NULL AFTER `email`,\n"
    "  ADD KEY `idx_name` (`name`(20)),\n"
    "  ADD KEY `idx_status` (`status`),\n"
    "  COMMENT='Customer accounts'"
)

NOTES_ALTER = "ALTER TABLE `notes`\n  DEFAULT CHARSET=utf8mb4,\n  COLLATE=utf8mb4_bin"


def snap(server, which):
    return replay.snapshot(server, which)


def obj(snapshot, type_, name):
    return next(o for o in snapshot['objects']
                if o['type'] == type_ and o['name'] == name)


def compare_all(source, target):
    """Legacy full-script cases deliberately opt into target-only removals."""
    report = schema_versions.compare(source, target, sync=False)
    keys = ['%s:%s' % (schema_versions.NAMESPACE.get(item['type'], item['type']),
                        item['name']) for item in report['objects']]
    return schema_versions.compare(source, target, chosen_keys=keys)


def sync(server):
    return compare_all(snap(server, 'after'), snap(server, 'before'))['sync']


def phase_statements(sync_, phase, name=None):
    return [s for s in sync_['statements']
            if s['phase'] == phase and (name is None or s['object']['name'] == name)]


def validate(value):
    return schema_versions.validate_snapshot(value)


class TestStatementOrder(unittest.TestCase):
    def test_first_statement_uses_the_target_database(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                statements = sync(server)['statements']
                self.assertEqual(statements[0]['sql'], 'USE `sv_m`')

    def test_phases_appear_in_the_documented_order(self):
        # Checks and removed tables go before the ALTERs (a removed child's
        # foreign key blocks changes to its parent; constraint names are
        # unique per database); partitioning ends its table's ALTER.
        order = ['session', 'database', 'drop_objects', 'drop_foreign_keys',
                 'drop_checks', 'drop_tables', 'alter_tables', 'create_tables',
                 'add_foreign_keys', 'routines', 'views', 'triggers', 'events']
        for server in replay.SERVERS:
            with self.subTest(server=server):
                seen = []
                for statement in sync(server)['statements']:
                    if statement['phase'] not in seen:
                        seen.append(statement['phase'])
                self.assertEqual(seen, order)


class TestCustomersAlter(unittest.TestCase):
    def test_one_alter_with_clauses_in_order(self):
        expected = {'mysql-8.4': MYSQL_CUSTOMERS_ALTER,
                    'mariadb-11.4': MARIADB_CUSTOMERS_ALTER}
        for server in replay.SERVERS:
            with self.subTest(server=server):
                statements = phase_statements(sync(server), 'alter_tables',
                                              'customers')
                self.assertEqual(len(statements), 1)
                self.assertEqual(statements[0]['sql'], expected[server])
                self.assertEqual(statements[0]['risks'],
                                 ['drops_data', 'may_change_data'])

    def test_checks_are_dropped_in_their_own_statement_first(self):
        expected = {'mysql-8.4': 'ALTER TABLE `customers`\n  DROP CHECK `chk_score`',
                    'mariadb-11.4': 'ALTER TABLE `customers`\n  DROP CONSTRAINT `chk_score`'}
        for server in replay.SERVERS:
            with self.subTest(server=server):
                mig = sync(server)
                drops = phase_statements(mig, 'drop_checks', 'customers')
                self.assertEqual([d['sql'] for d in drops], [expected[server]])
                alter = phase_statements(mig, 'alter_tables', 'customers')[0]
                self.assertLess(mig['statements'].index(drops[0]), mig['statements'].index(alter))

    def test_engine_is_never_restated(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                statements = phase_statements(sync(server), 'alter_tables',
                                              'customers')
                self.assertNotIn('ENGINE', statements[0]['sql'])

    def test_text_columns_name_charset_and_collation(self):
        statements = phase_statements(sync('mysql-8.4'), 'alter_tables',
                                      'customers')
        sql = statements[0]['sql']
        for column in ('`status`', '`email`', '`phone`'):
            clause = next(line for line in sql.split('\n') if column in line)
            self.assertIn('CHARACTER SET utf8mb4', clause)
            self.assertIn('COLLATE utf8mb4_0900_ai_ci', clause)


class TestForeignKeys(unittest.TestCase):
    def test_orders_key_dropped_then_added(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                mig = sync(server)
                drops = phase_statements(mig, 'drop_foreign_keys', 'orders')
                self.assertEqual(
                    [s['sql'] for s in drops],
                    ['ALTER TABLE `orders` DROP FOREIGN KEY `fk_orders_customer`'])
                adds = phase_statements(mig, 'add_foreign_keys', 'orders')
                self.assertEqual(len(adds), 1)
                self.assertIn('ADD CONSTRAINT `fk_orders_customer` FOREIGN KEY',
                              adds[0]['sql'])
                self.assertIn('REFERENCES `customers` (`id`)', adds[0]['sql'])
                if server == 'mysql-8.4':
                    self.assertIn('ON DELETE RESTRICT', adds[0]['sql'])
                self.assertIn('checks_rows', adds[0]['risks'])

    def test_unchanged_key_on_a_changed_column_is_rebuilt(self):
        # items.id widens, so item_tags.fk_tag_item (which references it) is
        # dropped and added again even though its text is unchanged.
        for server in replay.SERVERS:
            with self.subTest(server=server):
                mig = sync(server)
                drops = phase_statements(mig, 'drop_foreign_keys', 'item_tags')
                self.assertEqual(
                    [s['sql'] for s in drops],
                    ['ALTER TABLE `item_tags` DROP FOREIGN KEY `fk_tag_item`'])
                self.assertEqual(
                    drops[0]['warnings'],
                    ['Dropped and added again because a column or key it uses '
                     'changes.'])
                adds = phase_statements(mig, 'add_foreign_keys', 'item_tags')
                self.assertEqual(len(adds), 1)

    def test_new_table_is_created_without_its_foreign_key(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                mig = sync(server)
                creates = phase_statements(mig, 'create_tables', 'audit')
                self.assertEqual(len(creates), 1)
                self.assertNotIn('FOREIGN KEY', creates[0]['sql'])
                adds = phase_statements(mig, 'add_foreign_keys', 'audit')
                self.assertEqual(
                    [s['sql'] for s in adds],
                    ['ALTER TABLE `audit` ADD CONSTRAINT `fk_audit_customer` '
                     'FOREIGN KEY (`customer_id`) REFERENCES `customers` (`id`)'])
                create_at = mig['statements'].index(creates[0])
                add_at = mig['statements'].index(adds[0])
                self.assertLess(create_at, add_at)


class TestPartitioning(unittest.TestCase):
    def test_logs_partitioning_ends_the_tables_alter(self):
        # MySQL drops a column the old partitioning uses only in the statement
        # that repartitions, so the clause ends the table's one ALTER TABLE.
        heads = {'mysql-8.4': 'ALTER TABLE `logs`\n/*!50100 PARTITION BY RANGE (year(`at`))',
                 'mariadb-11.4': 'ALTER TABLE `logs`\nPARTITION BY RANGE (year(`at`))'}
        for server in replay.SERVERS:
            with self.subTest(server=server):
                mig = sync(server)
                self.assertEqual(phase_statements(mig, 'partitioning', 'logs'), [])
                statements = phase_statements(mig, 'alter_tables', 'logs')
                self.assertEqual(len(statements), 1)
                sql = statements[0]['sql']
                self.assertTrue(sql.startswith(heads[server]), sql)
                self.assertIn('p2027', sql)
                self.assertEqual(
                    statements[0]['warnings'],
                    ['Repartitioning copies every row of the table.'])


class TestNotesTable(unittest.TestCase):
    def test_charset_change_only_no_column_modifies(self):
        # body and title keep their effective character set; only the new
        # table default is applied.
        for server in replay.SERVERS:
            with self.subTest(server=server):
                statements = phase_statements(sync(server), 'alter_tables',
                                              'notes')
                self.assertEqual(len(statements), 1)
                self.assertEqual(statements[0]['sql'], NOTES_ALTER)
                self.assertNotIn('MODIFY', statements[0]['sql'])
                self.assertNotIn('body', statements[0]['sql'])
                self.assertNotIn('title', statements[0]['sql'])


class TestDropTables(unittest.TestCase):
    def test_legacy_dropped_source_foreign_keys_and_target_the_alters(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                mig = sync(server)
                drops = phase_statements(mig, 'drop_tables', 'legacy')
                self.assertEqual([s['sql'] for s in drops], ['DROP TABLE `legacy`'])
                self.assertEqual(drops[0]['risks'], ['drops_data'])
                drop_at = mig['statements'].index(drops[0])
                last_fk_drop = max(mig['statements'].index(s)
                                   for s in phase_statements(mig, 'drop_foreign_keys'))
                first_alter = min(mig['statements'].index(s)
                                  for s in phase_statements(mig, 'alter_tables'))
                self.assertLess(last_fk_drop, drop_at)
                self.assertLess(drop_at, first_alter)
                self.assertTrue(any('If a table was renamed (dropped: legacy; '
                                    'new: audit)' in warning
                                    for warning in mig['warnings']),
                                mig['warnings'])


class TestRoutines(unittest.TestCase):
    def test_mysql_drops_and_recreates_the_procedure(self):
        mig = sync('mysql-8.4')
        drops = phase_statements(mig, 'drop_objects', 'p_touch')
        self.assertEqual([s['sql'] for s in drops],
                         ['DROP PROCEDURE IF EXISTS `p_touch`'])
        self.assertEqual(drops[0]['risks'], ['drops_privileges'])
        self.assertTrue(any('automatic_sp_privileges' in warning
                            for warning in drops[0]['warnings']))
        creates = [s for s in phase_statements(mig, 'routines', 'p_touch')
                   if s['sql'].startswith('CREATE')]
        self.assertEqual(len(creates), 1)
        self.assertTrue(creates[0]['sql'].startswith(
            'CREATE DEFINER=`root`@`%` PROCEDURE `p_touch`'))
        self.assertLess(mig['statements'].index(drops[0]),
                        mig['statements'].index(creates[0]))

    def test_mariadb_creates_or_replaces_without_dropping(self):
        mig = sync('mariadb-11.4')
        self.assertFalse(any('DROP PROCEDURE' in s['sql'] for s in mig['statements']))
        creates = [s for s in phase_statements(mig, 'routines', 'p_touch')
                   if s['sql'].startswith('CREATE')]
        self.assertEqual(len(creates), 1)
        self.assertTrue(creates[0]['sql'].startswith(
            'CREATE OR REPLACE DEFINER=`root`@`%` PROCEDURE `p_touch`'))
        bodies = phase_statements(mig, 'routines', 'pkg_util')
        body = [s for s in bodies if 'PACKAGE BODY' in s['sql']]
        self.assertEqual(len(body), 1)
        self.assertTrue(body[0]['sql'].startswith('CREATE OR REPLACE '))
        self.assertIn('PACKAGE BODY "pkg_util"', body[0]['sql'])

    def test_every_create_runs_under_its_own_sql_mode(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                statements = sync(server)['statements']
                saved_at = next(
                    i for i, s in enumerate(statements)
                    if s['sql'].startswith('SET @myflames_sql_mode = @@SESSION.sql_mode'))
                creates = [(i, s) for i, s in enumerate(statements)
                           if s['phase'] in ('routines', 'triggers', 'events')
                           and s['sql'].startswith('CREATE')]
                self.assertTrue(creates)
                self.assertLess(saved_at, creates[0][0])
                for index, statement in creates:
                    self.assertTrue(
                        statements[index - 1]['sql'].startswith('SET SESSION sql_mode'),
                        statements[index - 1]['sql'])
                    self.assertTrue(
                        statements[index + 1]['sql'].startswith(
                            'SET SESSION sql_mode = @myflames_sql_mode'),
                        statements[index + 1]['sql'])
                event = next((i, s) for i, s in creates if s['phase'] == 'events')
                self.assertIn("time_zone = 'SYSTEM'",
                              statements[event[0] - 1]['sql'])
                self.assertIn('time_zone = @myflames_time_zone',
                              statements[event[0] + 1]['sql'])


class TestViews(unittest.TestCase):
    def test_views_are_requalified_and_ordered_by_dependency(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                mig = sync(server)
                views = phase_statements(mig, 'views')
                names = [s['object']['name'] for s in views]
                self.assertEqual(names, ['v_orders', 'v_big'])
                v_orders, v_big = views
                self.assertTrue(v_orders['sql'].startswith('CREATE OR REPLACE '))
                self.assertIn('VIEW `sv_m`.`v_orders`', v_orders['sql'])
                self.assertNotIn('sv_b', v_orders['sql'])
                self.assertNotIn('sv_b', v_big['sql'])
                self.assertLess(mig['statements'].index(v_orders),
                                mig['statements'].index(v_big))


class TestTriggers(unittest.TestCase):
    def test_triggers_recreated_in_afters_order(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                mig = sync(server)
                drops = [s['sql'] for s in phase_statements(mig, 'drop_objects')]
                self.assertIn('DROP TRIGGER IF EXISTS `trg_bi`', drops)
                self.assertIn('DROP TRIGGER IF EXISTS `trg_bi2`', drops)
                creates = [s['object']['name']
                           for s in phase_statements(mig, 'triggers')
                           if s['sql'].startswith('CREATE')]
                self.assertEqual(creates, ['trg_bi0', 'trg_bi'])
                self.assertTrue(any(
                    'Recreates all BEFORE INSERT triggers on orders' in warning
                    for warning in mig['warnings']), mig['warnings'])


class TestSkipped(unittest.TestCase):
    def test_mariadb_sequence_change_is_not_generated(self):
        mig = sync('mariadb-11.4')
        skipped = [(s['type'], s['name']) for s in mig['skipped']]
        self.assertIn(('sequence', 'seq_ids'), skipped)
        reason = next(s['reason'] for s in mig['skipped']
                      if s['type'] == 'sequence' and s['name'] == 'seq_ids')
        self.assertIn('ALTER SEQUENCE', reason)
        self.assertIn('-- Not generated:', mig['script'])
        self.assertIn('--   sequence seq_ids:', mig['script'])


class TestScript(unittest.TestCase):
    def test_header_names_both_sides_and_the_never_runs_note(self):
        text = schema_sync.script(
            {'statements': [], 'skipped': [], 'warnings': []},
            'Target db', 'Source db', '2026-10-26T00:00:00+00:00')
        self.assertIn('-- Changes the target: Target db', text)
        self.assertIn('-- To match the source: Source db', text)
        self.assertIn('-- myflames never runs this script.', text)
        self.assertIn('-- No differences to apply.', text)

    def test_compound_statement_uses_a_delimiter_not_in_its_body(self):
        sync_ = {'statements': [
            {'phase': 'routines', 'object': {'type': 'procedure', 'name': 'p'},
             'sql': "CREATE PROCEDURE `p`()\nBEGIN\nSET @x = '$$';\nEND",
             'risks': [], 'warnings': [], 'compound': True},
        ], 'skipped': [], 'warnings': []}
        text = schema_sync.script(sync_, 'B', 'A', 'now')
        self.assertIn('DELIMITER ;;\n', text)
        # The mark on its own line: a body may end in a -- comment.
        self.assertIn('END\n;;\nDELIMITER ;', text)
        self.assertNotIn('DELIMITER $$', text)

    def test_object_comment_appears_once_for_consecutive_statements(self):
        # The p_touch recreate is one SET SESSION + CREATE + restore group, so
        # its comment line introduces the whole group once; the earlier DROP
        # is a separate group and gets its own comment.
        sync_ = {'statements': [
            {'phase': 'routines', 'object': {'type': 'procedure', 'name': 'p'},
             'sql': 'SET SESSION sql_mode = @myflames_sql_mode',
             'risks': [], 'warnings': [], 'compound': False},
            {'phase': 'routines', 'object': {'type': 'procedure', 'name': 'p'},
             'sql': 'CREATE PROCEDURE `p`() SELECT 1',
             'risks': [], 'warnings': [], 'compound': False},
            {'phase': 'routines', 'object': {'type': 'procedure', 'name': 'p'},
             'sql': 'SET SESSION sql_mode = @myflames_sql_mode',
             'risks': [], 'warnings': [], 'compound': False},
        ], 'skipped': [], 'warnings': []}
        text = schema_sync.script(sync_, 'B', 'A', 'now')
        self.assertEqual(text.count('-- procedure p\n'), 1)
        script = sync('mysql-8.4')['script']
        self.assertEqual(script.count('-- procedure p_touch'), 2)
        self.assertIn('-- table customers', script)


class TestSyntheticCases(unittest.TestCase):
    """Copies of the fixture snapshots edited into specific situations."""

    def compare_edited(self, server, edit_source=None, edit_target=None,
                       which='after'):
        target = copy.deepcopy(replay.load(server + '-before'))
        source = copy.deepcopy(replay.load(server + '-' + which))
        if edit_target:
            edit_target(target)
        if edit_source:
            edit_source(source)
        return compare_all(validate(source), validate(target))['sync']

    def test_removed_comment_option_resets_it(self):
        def edit(source):
            ddl = obj(source, 'table', 'customers')['ddl']
            obj(source, 'table', 'customers')['ddl'] = ddl.replace(
                " COMMENT='Customer accounts'", '')
        mig = self.compare_edited('mysql-8.4', edit)
        alter = phase_statements(mig, 'alter_tables', 'customers')[0]
        self.assertIn("COMMENT=''", alter['sql'])

    def test_removed_row_format_resets_it(self):
        def edit(target):
            ddl = obj(target, 'table', 'customers')['ddl']
            obj(target, 'table', 'customers')['ddl'] = ddl.replace(
                'ENGINE=InnoDB ', 'ENGINE=InnoDB ROW_FORMAT=DYNAMIC ', 1)
        mig = self.compare_edited('mysql-8.4', edit_target=edit)
        alter = phase_statements(mig, 'alter_tables', 'customers')[0]
        self.assertIn('ROW_FORMAT=DEFAULT', alter['sql'])

    def test_engine_change_warns_about_copying_rows(self):
        def edit(source):
            ddl = obj(source, 'table', 'items')['ddl']
            obj(source, 'table', 'items')['ddl'] = ddl.replace(
                'ENGINE=InnoDB', 'ENGINE=MyISAM')
        mig = self.compare_edited('mysql-8.4', edit)
        alter = phase_statements(mig, 'alter_tables', 'items')[0]
        self.assertIn('ENGINE=MyISAM', alter['sql'])
        self.assertTrue(any('Changing the storage engine copies every row' in warning
                            for warning in alter['warnings']), alter['warnings'])

    def test_dropped_and_added_column_warns_about_a_possible_rename(self):
        mig = sync('mysql-8.4')
        alter = phase_statements(mig, 'alter_tables', 'customers')[0]
        self.assertTrue(any(
            'If a column was renamed (dropped: score; added: phone), use '
            'RENAME COLUMN instead' in warning for warning in alter['warnings']),
            alter['warnings'])

    def test_changed_sequence_is_skipped(self):
        def add_sequence(start):
            def edit(snapshot):
                snapshot['objects'].append({
                    'type': 'sequence', 'name': 'seq_x',
                    'ddl': 'CREATE SEQUENCE `seq_x` start with %d' % start})
            return edit
        for server in replay.SERVERS:
            with self.subTest(server=server):
                mig = self.compare_edited(server, edit_source=add_sequence(2),
                                          edit_target=add_sequence(1))
                skipped = [s for s in mig['skipped']
                           if s['type'] == 'sequence' and s['name'] == 'seq_x']
                self.assertEqual(len(skipped), 1)
                self.assertIn('ALTER SEQUENCE', skipped[0]['reason'])

    def test_system_versioning_change_is_skipped(self):
        # MariaDB only: MySQL has no system versioning to print.
        def edit(source):
            ddl = obj(source, 'table', 'hist')['ddl']
            obj(source, 'table', 'hist')['ddl'] = ddl.replace(
                ' WITH SYSTEM VERSIONING', '')
        mig = self.compare_edited('mariadb-11.4', edit)
        skipped = [s for s in mig['skipped']
                   if s['type'] == 'table' and s['name'] == 'hist']
        self.assertEqual(len(skipped), 1)
        self.assertIn('System versioning changes', skipped[0]['reason'])

    def test_not_compared_object_is_skipped(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                report = schema_versions.compare(snap(server, 'limited'),
                                                 snap(server, 'after'))
                skipped = report['sync']['skipped']
                self.assertTrue(skipped)
                self.assertTrue(all(s['reason'].startswith('Not compared.')
                                    for s in skipped))

    def test_unsplittable_table_is_skipped(self):
        def edit(source):
            obj(source, 'table', 'items')['ddl'] = 'CREATE TABLE `items` (id int)'
        mig = self.compare_edited('mysql-8.4', edit)
        skipped = [s for s in mig['skipped']
                   if s['type'] == 'table' and s['name'] == 'items']
        self.assertEqual(len(skipped), 1)
        self.assertIn('could not be split', skipped[0]['reason'])

    def test_new_view_is_created_source_the_view_it_uses(self):
        def edit(source):
            source['objects'].append({
                'type': 'view', 'name': 'v_a',
                'ddl': 'CREATE VIEW `sv_b`.`v_a` AS select * from `sv_b`.`v_b`'})
            source['objects'].append({
                'type': 'view', 'name': 'v_b',
                'ddl': 'CREATE VIEW `sv_b`.`v_b` AS select 1'})
        mig = self.compare_edited('mysql-8.4', edit)
        views = phase_statements(mig, 'views')
        names = [s['object']['name'] for s in views]
        self.assertLess(names.index('v_b'), names.index('v_a'))
        self.assertLess(names.index('v_orders'), names.index('v_big'))


class TestSourceReviewRules(unittest.TestCase):
    """Rules from the source review (mysql-server and mariadb-server)."""

    compare_edited = TestSyntheticCases.compare_edited

    def test_generated_column_storage_change_is_not_generated(self):
        """MODIFY cannot switch STORED and VIRTUAL (sql/sql_table.cc:15741-15744)."""
        for server in replay.SERVERS:
            with self.subTest(server=server):
                def edit(source):
                    table = obj(source, 'table', 'customers')
                    table['ddl'] = table['ddl'].replace(') VIRTUAL,', ') STORED,')
                mig = self.compare_edited(server, edit, edit_target=None)
                skipped = [x for x in mig['skipped'] if x['name'] == 'customers']
                self.assertEqual(len(skipped), 1, mig['skipped'])
                self.assertIn('full_name', skipped[0]['reason'])
                self.assertIn('VIRTUAL to STORED', skipped[0]['reason'])
                # A partial table plan cannot stand in for its Source shape
                # during dependency closure. Keep the whole Target table.
                self.assertFalse(phase_statements(mig, 'alter_tables', 'customers'))
                self.assertFalse(phase_statements(mig, 'drop_checks', 'customers'))
                self.assertTrue(phase_statements(mig, 'alter_tables', 'notes'))

    def test_second_fulltext_key_is_its_own_statement_on_mysql(self):
        def edit(source):
            table = obj(source, 'table', 'orders')
            table['ddl'] = table['ddl'].replace('  FULLTEXT KEY `ft_note` (`note`),',
                                                '  FULLTEXT KEY `ft_note` (`note`),\n  FULLTEXT KEY `ft_a` (`note`),\n'
                                                '  FULLTEXT KEY `ft_b` (`note`),')
        mig = self.compare_edited('mysql-8.4', edit)
        alters = phase_statements(mig, 'alter_tables', 'orders')
        self.assertEqual(len(alters), 2)
        self.assertIn('ADD FULLTEXT KEY `ft_a`', alters[0]['sql'])
        self.assertNotIn('ft_b', alters[0]['sql'])
        self.assertEqual(alters[1]['sql'], 'ALTER TABLE `orders` ADD FULLTEXT KEY `ft_b` (`note`)')
        maria = self.compare_edited('mariadb-11.4', edit)
        self.assertEqual(len(phase_statements(maria, 'alter_tables', 'orders')), 1)

    def test_key_block_size_and_directory_options_warn(self):
        def edit_target(target):
            table = obj(target, 'table', 'notes')
            table['ddl'] = table['ddl'].replace('DEFAULT CHARSET=latin1', "KEY_BLOCK_SIZE=8 DATA DIRECTORY='/d' "
                                                'DEFAULT CHARSET=latin1')
        mig = self.compare_edited('mysql-8.4', edit_target=edit_target)
        alter = phase_statements(mig, 'alter_tables', 'notes')[0]
        self.assertIn('KEY_BLOCK_SIZE=0', alter['sql'])
        self.assertNotIn('DIRECTORY', alter['sql'])
        self.assertTrue(any('keep their old KEY_BLOCK_SIZE' in w for w in alter['warnings']), alter['warnings'])
        self.assertTrue(any('DATA DIRECTORY' in w for w in alter['warnings']), alter['warnings'])

    def test_new_sequence_comes_target_the_alters(self):
        def edit(source):
            source['objects'].append({'type': 'sequence', 'name': 'seq_new', 'ddl':
                                     'CREATE SEQUENCE `seq_new` start with 1 minvalue 1 maxvalue 9223372036854775806 '
                                     'increment by 1 cache 1000 nocycle ENGINE=InnoDB'})
        mig = self.compare_edited('mariadb-11.4', edit)
        created = phase_statements(mig, 'create_sequences', 'seq_new')
        self.assertEqual(len(created), 1)
        first_alter = min(mig['statements'].index(x) for x in phase_statements(mig, 'alter_tables'))
        self.assertLess(mig['statements'].index(created[0]), first_alter)

    def test_read_only_database_is_opened_first_and_closed_last(self):
        def mark(snapshot):
            db = next(o for o in snapshot['objects'] if o['type'] == 'database')
            db['ddl'] += ' /* READ ONLY = 1 */'
        mig = self.compare_edited('mysql-8.4', mark, mark)
        sqls = [x['sql'] for x in mig['statements']]
        self.assertEqual(sqls[1], 'ALTER DATABASE `sv_m` READ ONLY = 0')
        self.assertEqual(sqls[-1], 'ALTER DATABASE `sv_m` READ ONLY = 1')
        self.assertFalse(any('READ ONLY = 1 */' in x for x in sqls), 'the comment is never sent as an option')

    def test_event_with_a_fixed_time_and_named_zone_warns(self):
        def edit(source):
            event = obj(source, 'event', 'ev_cleanup')
            event['ddl'] = event['ddl'].replace("STARTS '2026-01-01 00:00:00'",
                                                "STARTS '2026-01-01 00:00:00' ENDS '2026-02-01 00:00:00'")
            event['time_zone'] = 'Europe/Paris'
        mig = self.compare_edited('mysql-8.4', edit)
        self.assertTrue(any('fixed AT or ENDS time' in w for w in mig['warnings']), mig['warnings'])
        self.assertTrue(any('Europe/Paris' in w and 'time zone tables' in w for w in mig['warnings']))

    def test_newer_source_server_warns(self):
        def edit(source):
            source['server']['server_version'] = '9.7.2'
        mig = self.compare_edited('mysql-8.4', edit)
        self.assertTrue(any('newer server' in w for w in mig['warnings']), mig['warnings'])


class TestSelection(unittest.TestCase):
    """A generated script may change only the objects the user selected."""

    def compare(self, server, chosen_keys=None):
        return schema_versions.compare(snap(server, 'after'), snap(server, 'before'),
                                       chosen_keys=chosen_keys)

    def test_default_omits_every_target_only_object(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                report = self.compare(server)
                mig = report['sync']
                excluded = {item['key'] for item in report['objects']
                            if item['status'] == 'only_in_target'}
                expected = {item['key'] for item in report['objects']} - excluded
                self.assertEqual(set(mig['chosen_keys']), expected)
                self.assertEqual({item['key'] for item in mig['omitted']}, excluded)
                self.assertNotIn('DROP TABLE `legacy`', mig['script'])
                self.assertNotIn('DROP TRIGGER IF EXISTS `trg_bi2`', mig['script'])
                for item in mig['omitted']:
                    self.assertTrue(item['reason'])
                self.assertIn('%d differences left out' % len(excluded), mig['script'])
                self.assertIn('only in the target are not dropped; choose them to include', mig['script'])

    def test_explicit_target_only_selection_generates_its_drop(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                mig = self.compare(server, ['relation:legacy'])['sync']
                self.assertEqual(mig['chosen_keys'], ['relation:legacy'])
                self.assertEqual([s['sql'] for s in
                                  phase_statements(mig, 'drop_tables')],
                                 ['DROP TABLE `legacy`'])
                self.assertTrue(all(s['object'] is None or
                                    s['object']['name'] == 'legacy'
                                    for s in mig['statements']))

    def test_empty_selection_has_no_sql(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                report = self.compare(server, [])
                mig = report['sync']
                self.assertEqual(mig['chosen_keys'], [])
                self.assertEqual(mig['statements'], [])
                self.assertEqual(mig['summary']['statements'], 0)
                self.assertEqual(mig['summary']['warnings'], 0)
                self.assertTrue(mig['omitted'])
                self.assertFalse(any(line.strip() and not line.startswith('--')
                                     for line in mig['script'].splitlines()))

    def test_build_and_compare_use_the_same_selection(self):
        source, target = snap('mysql-8.4', 'after'), snap('mysql-8.4', 'before')
        report = schema_versions.compare(source, target, sync=False)
        chosen = ['relation:notes']
        built = schema_sync.build(target, source, report, chosen_keys=chosen)
        compared = schema_versions.compare(source, target, chosen_keys=chosen)['sync']
        self.assertEqual(built['statements'], compared['statements'])
        self.assertEqual(built['chosen_keys'], chosen)
        self.assertEqual(built['omitted'], compared['omitted'])
        self.assertEqual([s['sql'] for s in phase_statements(built, 'alter_tables')],
                         [NOTES_ALTER])

    def test_invalid_selections_fail_before_generation(self):
        source, target = snap('mysql-8.4', 'after'), snap('mysql-8.4', 'before')
        report = schema_versions.compare(source, target, sync=False)
        invalid = ['relation:notes', 1, False, {}, ('relation:notes',),
                   [None], [1], [False], [{}], [['relation:notes']],
                   ['table:notes'], ['relation:missing'],
                   ['relation:notes', 'relation:notes']]
        for chosen in invalid:
            with self.subTest(chosen=chosen):
                with self.assertRaises(ValueError):
                    schema_versions.compare(source, target, chosen_keys=chosen)
                with self.assertRaises(ValueError):
                    schema_sync.build(target, source, report, chosen_keys=chosen)

    def test_unselected_foreign_key_owner_is_not_altered(self):
        # Widening items.id requires rebuilding item_tags' foreign key. The
        # parent change must be skipped when the dependent table is unchecked.
        for server in replay.SERVERS:
            with self.subTest(server=server):
                mig = self.compare(server, ['relation:items'])['sync']
                self.assertEqual(mig['statements'], [])
                skipped = [item for item in mig['skipped'] if item['name'] == 'items']
                self.assertTrue(skipped, mig['skipped'])
                self.assertIn('item_tags', skipped[0]['reason'])

    def test_selecting_foreign_key_owner_allows_the_rebuild(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                mig = self.compare(server, ['relation:items', 'relation:item_tags'])['sync']
                self.assertTrue(phase_statements(mig, 'alter_tables', 'items'))
                self.assertTrue(phase_statements(mig, 'drop_foreign_keys', 'item_tags'))
                self.assertTrue(phase_statements(mig, 'add_foreign_keys', 'item_tags'))
                self.assertTrue(all(s['object'] is None or
                                    s['object']['name'] in ('items', 'item_tags')
                                    for s in mig['statements']))

    def test_trigger_order_does_not_recreate_unselected_triggers(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                mig = self.compare(server, ['trigger:trg_bi0'])['sync']
                self.assertEqual(mig['statements'], [])
                skipped = [item for item in mig['skipped'] if item['name'] == 'trg_bi0']
                self.assertTrue(skipped, mig['skipped'])
                self.assertIn('trg_bi', skipped[0]['reason'])

    def test_replacing_package_does_not_recreate_unselected_body(self):
        # MariaDB has packages; MySQL has no equivalent package object.
        source = snap('mariadb-11.4', 'after')
        target = snap('mariadb-11.4', 'before')
        package = obj(source, 'package', 'pkg_util')
        package['ddl'] = package['ddl'].replace('END', 'PROCEDURE extra;\nEND', 1)
        report = schema_versions.compare(validate(source), target,
                                         chosen_keys=['package:pkg_util'])
        mig = report['sync']
        self.assertEqual(mig['statements'], [])
        skipped = [item for item in mig['skipped'] if item['name'] == 'pkg_util']
        self.assertTrue(skipped, mig['skipped'])
        self.assertIn('body', skipped[0]['reason'].lower())

    def test_trigger_group_is_not_dropped_when_a_required_definition_is_unreadable(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                source, target = snap(server, 'after'), snap(server, 'before')
                unreadable = obj(source, 'trigger', 'trg_bi')
                unreadable['ddl'] = None
                unreadable['reason'] = 'The account cannot read this trigger.'
                report = schema_versions.compare(validate(source), target,
                                                 chosen_keys=['trigger:trg_bi0', 'trigger:trg_bi'])
                self.assertEqual(report['sync']['statements'], [])
                skipped = [item for item in report['sync']['skipped'] if item['name'] == 'trg_bi0']
                self.assertTrue(skipped, report['sync']['skipped'])
                self.assertIn('trg_bi', skipped[0]['reason'])

    def test_package_is_not_replaced_when_its_selected_body_is_unreadable(self):
        target = snap('mariadb-11.4', 'before')
        source = copy.deepcopy(target)
        package = obj(source, 'package', 'pkg_util')
        package['ddl'] = package['ddl'].replace('END', 'PROCEDURE extra;\nEND', 1)
        body = obj(source, 'package_body', 'pkg_util')
        body['ddl'] = None
        body['reason'] = 'The account cannot read this package body.'
        report = schema_versions.compare(validate(source), target,
                                         chosen_keys=['package:pkg_util', 'package_body:pkg_util'])
        self.assertEqual(report['sync']['statements'], [])
        skipped = [item for item in report['sync']['skipped'] if item['type'] == 'package']
        self.assertTrue(skipped, report['sync']['skipped'])
        self.assertIn('body', skipped[0]['reason'].lower())

    def test_target_only_package_does_not_implicitly_drop_unselected_body(self):
        target = snap('mariadb-11.4', 'before')
        source = copy.deepcopy(target)
        source['objects'] = [item for item in source['objects']
                             if item['type'] not in ('package', 'package_body')]
        report = schema_versions.compare(validate(source), target,
                                         chosen_keys=['package:pkg_util'])
        self.assertEqual(report['sync']['statements'], [])
        skipped = [item for item in report['sync']['skipped'] if item['type'] == 'package']
        self.assertTrue(skipped, report['sync']['skipped'])
        self.assertIn('body', skipped[0]['reason'].lower())

    def test_replacing_package_preserves_body_that_exists_only_in_target(self):
        target = snap('mariadb-11.4', 'before')
        source = copy.deepcopy(target)
        source['objects'] = [item for item in source['objects'] if item['type'] != 'package_body']
        package = obj(source, 'package', 'pkg_util')
        package['ddl'] = package['ddl'].replace('END', 'PROCEDURE extra;\nEND', 1)
        report = schema_versions.compare(validate(source), target,
                                         chosen_keys=['package:pkg_util'])
        self.assertEqual(report['sync']['statements'], [])
        self.assertTrue(any('body' in item['reason'].lower()
                            for item in report['sync']['skipped']))

    def test_unselected_table_keeps_its_target_foreign_key_not_its_source_definition(self):
        # Unchecking item_tags keeps Target's FK even if Source removed it.
        for server in replay.SERVERS:
            with self.subTest(server=server):
                source, target = snap(server, 'after'), snap(server, 'before')
                table = obj(source, 'table', 'item_tags')
                table['ddl'] = table['ddl'].replace(',\n  CONSTRAINT `fk_tag_item` FOREIGN KEY '
                                                   '(`item_id`) REFERENCES `items` (`id`)', '')
                self.assertNotIn('FOREIGN KEY', table['ddl'])
                report = schema_versions.compare(validate(source), target, chosen_keys=['relation:items'])
                self.assertEqual(report['sync']['statements'], [])
                self.assertTrue(any('item_tags' in item['reason']
                                    for item in report['sync']['skipped']))

    def test_dropping_table_does_not_implicitly_drop_unselected_triggers(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                target = snap(server, 'before')
                source = copy.deepcopy(target)
                source['objects'] = [item for item in source['objects']
                                     if item['name'] != 'orders' and item['type'] != 'trigger']
                report = schema_versions.compare(validate(source), target, chosen_keys=['relation:orders'])
                self.assertEqual(report['sync']['statements'], [])
                self.assertTrue(any('trigger' in item['reason'].lower()
                                    for item in report['sync']['skipped']))

    def test_parent_drop_is_skipped_when_a_target_child_is_unselected(self):
        # Source removed the dependency, but unchecking item_tags retains
        # Target's FK. Selecting the parent DROP cannot erase that decision.
        for server in replay.SERVERS:
            with self.subTest(server=server):
                target = snap(server, 'before')
                source = copy.deepcopy(target)
                source['objects'] = [item for item in source['objects'] if item['name'] != 'items']
                child = obj(source, 'table', 'item_tags')
                child['ddl'] = child['ddl'].replace(',\n  CONSTRAINT `fk_tag_item` FOREIGN KEY '
                                                   '(`item_id`) REFERENCES `items` (`id`)', '')
                self.assertNotIn('FOREIGN KEY', child['ddl'])
                report = schema_versions.compare(validate(source), target, chosen_keys=['relation:items'])
                self.assertEqual(report['sync']['statements'], [])
                skipped = [item for item in report['sync']['skipped'] if item['name'] == 'items']
                self.assertTrue(skipped, report['sync']['skipped'])
                self.assertIn('item_tags', skipped[0]['reason'])
                self.assertIn('foreign key', skipped[0]['reason'].lower())

    def test_read_only_database_is_not_opened_while_unselected(self):
        target = snap('mysql-8.4', 'before')
        obj(target, 'database', target['database'])['ddl'] += ' /* READ ONLY = 1 */'
        report = schema_versions.compare(snap('mysql-8.4', 'after'), validate(target),
                                         chosen_keys=['relation:notes'])
        self.assertEqual(report['sync']['statements'], [])
        self.assertTrue(any('read only' in item['reason'].lower()
                            for item in report['sync']['skipped']))


class TestKeptTargetDependencies(unittest.TestCase):
    def entry(self, report, name):
        return next(e for e in report['objects'] if e['name'] == name)

    def skipped(self, report, name):
        return ' '.join(s['reason'] for s in report['sync']['skipped'] if s['name'] == name)

    def test_unreadable_or_skipped_fk_owner_is_retained_before_closure(self):
        for server in replay.SERVERS:
            for failure in ('unreadable', 'definition', 'generated_storage'):
                with self.subTest(server=server, failure=failure):
                    source, target = replay.retained_child_pair(server)
                    child = copy.deepcopy(obj(target, 'table', 'child2'))
                    child['ddl'] = child['ddl'].replace(' int ', ' bigint ')
                    source['objects'].append(child)
                    obj(source, 'table', 'child3')['ddl'] = obj(source, 'table', 'child3')['ddl'].replace('`fk_c2`', '`new_fk`')
                    if failure == 'unreadable':
                        child['ddl'], child['reason'] = None, 'Access denied'
                    elif failure == 'definition':
                        child['ddl'] = 'CREATE TABLE `child2` (pid bigint)'
                    else:
                        old = obj(target, 'table', 'child2')
                        old['ddl'] = old['ddl'].replace('  KEY ', '  `g` int GENERATED ALWAYS AS (`pid` + 1) VIRTUAL,\n  KEY ')
                        child['ddl'] = child['ddl'].replace('  KEY ', '  `g` int GENERATED ALWAYS AS (`pid` + 1) STORED,\n  KEY ')
                    report = schema_versions.compare(validate(source), validate(target))
                    for name in ('parent2', 'child3'):
                        self.assertEqual(self.entry(report, name)['blocked_by'], ['relation:child2'])
                        self.assertIn('Choose child2', self.skipped(report, name))
                        self.assertFalse(any(s['object'] and s['object']['name'] == name
                                             for s in report['sync']['statements']))
                    self.assertTrue(self.skipped(report, 'child2'))
                    self.assertFalse(phase_statements(report['sync'], 'alter_tables', 'child2'))
                    self.assertFalse(phase_statements(report['sync'], 'drop_foreign_keys', 'child2'))
                    self.assertTrue(phase_statements(report['sync'], 'alter_tables', 'customers'))

    def test_fk_lookup_preserves_case_sensitive_parent_names(self):
        for setting in ('0', None):
            with self.subTest(lower_case_table_names=setting):
                source, target = replay.retained_child_pair('mysql-8.4')
                for snapshot_ in (source, target):
                    snapshot_['server']['lower_case_table_names'] = setting
                    snapshot_['objects'] = [o for o in snapshot_['objects'] if o['name'] != 'child2']
                    parent = obj(snapshot_, 'table', 'parent2')
                    parent['name'], parent['ddl'] = 'P', parent['ddl'].replace('`parent2`', '`P`')
                    other = copy.deepcopy(parent)
                    other['name'], other['ddl'] = 'p', parent['ddl'].replace('`P`', '`p`').replace(' int ', ' bigint ')
                    snapshot_['objects'].append(other)
                child = obj(source, 'table', 'child3')
                child['ddl'] = child['ddl'].replace('`parent2`', '`P`')
                report = schema_versions.compare(validate(source), validate(target), chosen_keys=['relation:child3'])
                self.assertEqual(self.entry(report, 'child3')['blocked_by'], ['relation:P'])
                self.assertIn('Choose P', self.skipped(report, 'child3'))
                self.assertEqual(report['sync']['statements'], [])
                if setting is None:
                    self.assertTrue(any('case-sensitive' in w for w in report['sync']['warnings']))

    def test_fk_lookup_folds_names_when_snapshot_requires_it(self):
        for setting in ('1', '2'):
            with self.subTest(lower_case_table_names=setting):
                source, target = replay.retained_child_pair('mysql-8.4')
                for snapshot_ in (source, target):
                    snapshot_['server']['lower_case_table_names'] = setting
                child = obj(source, 'table', 'child3')
                child['ddl'] = child['ddl'].replace('`parent2`', '`PARENT2`').replace('`fk_c2`', '`new_fk`')
                report = schema_versions.compare(validate(source), validate(target), chosen_keys=['relation:child3'])
                self.assertEqual(self.entry(report, 'child3')['blocked_by'], ['relation:parent2'])
                self.assertEqual(report['sync']['statements'], [])

    def test_case_distinct_retained_child_does_not_block_or_rebuild(self):
        source, target = replay.retained_child_pair('mysql-8.4')
        for snapshot_ in (source, target):
            snapshot_['server']['lower_case_table_names'] = '0'
            parent = obj(snapshot_, 'table', 'parent2')
            parent['name'], parent['ddl'] = 'P', parent['ddl'].replace('`parent2`', '`P`')
            other = copy.deepcopy(parent)
            other['name'], other['ddl'] = 'p', parent['ddl'].replace('`P`', '`p`').replace(' bigint ', ' int ')
            snapshot_['objects'].append(other)
        child = obj(target, 'table', 'child2')
        child['ddl'] = child['ddl'].replace('`parent2`', '`p`')
        source['objects'].append(copy.deepcopy(child))
        source['objects'] = [o for o in source['objects'] if o['name'] != 'child3']
        for keys in (['relation:P'], ['relation:P', 'relation:child2']):
            report = schema_versions.compare(validate(source), validate(target), chosen_keys=keys)
            self.assertTrue(phase_statements(report['sync'], 'alter_tables', 'P'))
            self.assertFalse(phase_statements(report['sync'], 'drop_foreign_keys', 'child2'))
            self.assertFalse(phase_statements(report['sync'], 'add_foreign_keys', 'child2'))
        source['objects'] = [o for o in source['objects'] if o['name'] != 'P']
        report = schema_versions.compare(validate(source), validate(target), chosen_keys=['relation:P'])
        self.assertEqual([s['sql'] for s in phase_statements(report['sync'], 'drop_tables')], ['DROP TABLE `P`'])
        self.assertEqual(self.entry(report, 'P')['blocked_by'], [])

    def test_retained_parent_key_rule_depends_on_target_engine_and_version(self):
        for server, version, standard in (('mysql-8.4', '8.0.40', False),
                                           ('mysql-8.4', '8.3.0', False),
                                           ('mysql-8.4', '8.4.0', True),
                                           ('mysql-8.4', '9.2.0', True),
                                           ('mariadb-11.4', '11.4.13', False)):
            for index in ('KEY `k` (`id`)', 'UNIQUE KEY `k` (`id`,`extra`)',
                          'PRIMARY KEY (`id`,`extra`)', 'UNIQUE KEY `k` (`id`)'):
                with self.subTest(server=server, version=version, index=index):
                    source, target = replay.retained_child_pair(server)
                    target['server']['server_version'] = version
                    child = obj(source, 'table', 'child3')
                    child['ddl'] = child['ddl'].replace('`fk_c2`', '`new_fk`')
                    parent = obj(target, 'table', 'parent2')
                    parent['ddl'] = 'CREATE TABLE `parent2` (\n  `id` bigint NOT NULL,\n  `extra` int NOT NULL,\n  %s\n) ENGINE=InnoDB' % index
                    report = schema_versions.compare(source, validate(target), chosen_keys=['relation:child3'])
                    blocked = standard and index != 'UNIQUE KEY `k` (`id`)'
                    self.assertEqual(self.entry(report, 'child3')['blocked_by'], ['relation:parent2'] if blocked else [])
                    if blocked:
                        self.assertIn('PRIMARY KEY or UNIQUE', self.skipped(report, 'child3'))
                        self.assertIn('Choose parent2', self.skipped(report, 'child3'))
                        self.assertEqual(report['sync']['statements'], [])
                    else:
                        self.assertTrue(phase_statements(report['sync'], 'add_foreign_keys', 'child3'))

    def test_retained_parent_must_use_innodb_for_an_enforced_fk(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                source, target = replay.retained_child_pair(server)
                parent = obj(target, 'table', 'parent2')
                parent['ddl'] = parent['ddl'].replace(' int ', ' bigint ').replace('ENGINE=InnoDB', 'ENGINE=MyISAM')
                child = obj(source, 'table', 'child3')
                child['ddl'] = child['ddl'].replace('`fk_c2`', '`new_fk`')
                report = schema_versions.compare(source, validate(target), chosen_keys=['relation:child3'])
                self.assertEqual(self.entry(report, 'child3')['blocked_by'], ['relation:parent2'])
                self.assertIn('InnoDB', self.skipped(report, 'child3'))
                self.assertIn('Choose parent2', self.skipped(report, 'child3'))
                self.assertEqual(report['sync']['statements'], [])

    def test_default_keeps_child_and_skips_parent_and_new_child(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                source, target = replay.retained_child_pair(server)
                report = schema_versions.compare(source, target)
                self.assertNotIn('relation:child2', report['sync']['chosen_keys'])
                self.assertIn('parent2.id', self.skipped(report, 'parent2'))
                self.assertIn('BIGINT', self.skipped(report, 'parent2'))
                self.assertIn('INT', self.skipped(report, 'parent2'))
                self.assertIn('fk_c2', self.skipped(report, 'child3'))
                for name in ('parent2', 'child3'):
                    self.assertIn('Choose child2 to include its drop.', self.skipped(report, name))
                    self.assertEqual(self.entry(report, name)['blocked_by'], ['relation:child2'])
                    self.assertFalse(any(s['object'] and s['object']['name'] == name for s in report['sync']['statements']))
                # A blocked parent must not discard unrelated alterations.
                self.assertTrue(phase_statements(report['sync'], 'alter_tables', 'customers'))
                self.assertNotIn('foreign_key_checks', report['sync']['script'])

    def test_choosing_kept_child_restores_parent_and_new_child(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                source, target = replay.retained_child_pair(server)
                defaults = schema_versions.compare(source, target)['sync']['chosen_keys']
                report = schema_versions.compare(source, target, chosen_keys=defaults + ['relation:child2'])
                self.assertTrue(phase_statements(report['sync'], 'alter_tables', 'parent2'))
                self.assertTrue(phase_statements(report['sync'], 'create_tables', 'child3'))
                self.assertTrue(phase_statements(report['sync'], 'add_foreign_keys', 'child3'))
                self.assertFalse(self.skipped(report, 'parent2'))
                self.assertFalse(self.skipped(report, 'child3'))
                for name in ('parent2', 'child3'):
                    self.assertEqual(self.entry(report, name)['blocked_by'], [])
                all_report = compare_all(source, target)
                self.assertFalse(self.skipped(all_report, 'parent2'))
                self.assertFalse(self.skipped(all_report, 'child3'))

    def test_constraint_name_collision_without_type_change(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                source, target = replay.retained_child_pair(server)
                obj(source, 'table', 'parent2')['ddl'] = obj(target, 'table', 'parent2')['ddl']
                table = obj(source, 'table', 'child3')
                table['ddl'] = table['ddl'].replace('bigint', 'int')
                report = schema_versions.compare(validate(source), target)
                self.assertFalse(self.skipped(report, 'parent2'))
                self.assertIn('already uses that foreign key name', self.skipped(report, 'child3'))
                self.assertEqual(self.entry(report, 'child3')['blocked_by'], ['relation:child2'])

    def test_transitive_foreign_keys_need_the_original_blocker(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                source, target = replay.retained_child_pair(server)
                obj(source, 'table', 'child3')['ddl'] = obj(source, 'table', 'child3')['ddl'].replace('`fk_c2`', '`new_fk`')
                dependent = copy.deepcopy(obj(source, 'table', 'child3'))
                dependent['name'] = 'grandchild'
                dependent['ddl'] = dependent['ddl'].replace('CREATE TABLE `child3`', 'CREATE TABLE `grandchild`').replace(
                    '`new_fk`', '`grand_fk`').replace('`parent2` (`id`)', '`child3` (`pid`)')
                source['objects'].append(dependent)
                report = schema_versions.compare(validate(source), target)
                for name in ('child3', 'grandchild'):
                    self.assertTrue(self.skipped(report, name))
                    self.assertEqual(self.entry(report, name)['blocked_by'], ['relation:child2'])
                    self.assertFalse(phase_statements(report['sync'], 'create_tables', name))
                self.assertIn('parent2', self.skipped(report, 'child3'))
                self.assertIn('child3', self.skipped(report, 'grandchild'))

    def test_unselected_parent_must_match_selected_child(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                source, target = replay.retained_child_pair(server)
                report = schema_versions.compare(source, target, chosen_keys=['relation:child3'])
                self.assertIn('does not match', self.skipped(report, 'child3'))
                self.assertIn('relation:parent2', self.entry(report, 'child3')['blocked_by'])
                self.assertEqual(report['sync']['statements'], [])

    def test_transitive_views_are_not_created_against_a_skipped_table(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                source, target = replay.retained_child_pair(server)
                source['objects'].extend([
                    {'type': 'view', 'name': 'v_child3', 'ddl': 'CREATE VIEW `v_child3` AS SELECT `pid` FROM `%s`.`child3`' % source['database']},
                    {'type': 'view', 'name': 'v_outer', 'ddl': 'CREATE VIEW `v_outer` AS SELECT `pid` FROM `%s`.`v_child3`' % source['database']}])
                report = schema_versions.compare(validate(source), target)
                for name in ('v_child3', 'v_outer'):
                    self.assertEqual(self.entry(report, name)['blocked_by'], ['relation:child2'])
                    self.assertIn('Choose child2', self.skipped(report, name))
                    self.assertFalse(phase_statements(report['sync'], 'views', name))
                report = schema_versions.compare(validate(source), target, chosen_keys=report['sync']['chosen_keys'] + ['relation:child2'])
                self.assertTrue(phase_statements(report['sync'], 'views', 'v_outer'))

    def test_dependency_scan_ignores_literals_comments_and_other_schemas(self):
        source, target = replay.retained_child_pair('mysql-8.4')
        source['objects'].append({'type': 'view', 'name': 'v_literal', 'ddl':
            "CREATE VIEW `v_literal` AS SELECT 'FROM `sv_b`.`child3`' AS `v` /* `sv_b`.`child3` */ FROM `other`.`child3`"})
        report = schema_versions.compare(validate(source), target)
        self.assertEqual(self.entry(report, 'v_literal')['blocked_by'], [])
        self.assertTrue(phase_statements(report['sync'], 'views', 'v_literal'))

    def test_kept_parent_needs_its_source_index_for_a_new_fk(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                source, target = replay.retained_child_pair(server)
                obj(target, 'table', 'parent2')['ddl'] = 'CREATE TABLE `parent2` (\n  `id` bigint NOT NULL\n) ENGINE=InnoDB'
                obj(source, 'table', 'child3')['ddl'] = obj(source, 'table', 'child3')['ddl'].replace('`fk_c2`', '`new_fk`')
                report = schema_versions.compare(source, validate(target), chosen_keys=['relation:child3'])
                self.assertIn('no index', self.skipped(report, 'child3'))
                self.assertEqual(self.entry(report, 'child3')['blocked_by'], ['relation:parent2'])

    def test_compatible_integer_display_widths_and_string_lengths_can_stay(self):
        for server in replay.SERVERS:
            for old_type, new_type in (('int(11)', 'int'), ('varchar(10)', 'varchar(20)')):
                with self.subTest(server=server, old_type=old_type):
                    source, target = replay.retained_child_pair(server)
                    target['objects'] = [o for o in target['objects'] if o['name'] != 'child2']
                    parent = obj(target, 'table', 'parent2')
                    parent['ddl'] = parent['ddl'].replace(' int ', ' %s ' % old_type)
                    child = obj(source, 'table', 'child3')
                    child['ddl'] = child['ddl'].replace('bigint', new_type)
                    report = schema_versions.compare(validate(source), validate(target), chosen_keys=['relation:child3'])
                    self.assertEqual(self.entry(report, 'child3')['blocked_by'], [])
                    self.assertTrue(phase_statements(report['sync'], 'add_foreign_keys', 'child3'))

    def test_kept_parent_prefix_expression_and_fulltext_keys_do_not_support_a_new_fk(self):
        for index in ('KEY `k` (`id`(5))', 'KEY `k` ((lower(`id`)),`id`)', 'FULLTEXT KEY `k` (`id`)'):
            with self.subTest(index=index):
                source, target = replay.retained_child_pair('mysql-8.4')
                parent, child = obj(target, 'table', 'parent2'), obj(source, 'table', 'child3')
                parent['ddl'] = 'CREATE TABLE `parent2` (\n  `id` varchar(20) NOT NULL,\n  %s\n) ENGINE=InnoDB' % index
                child['ddl'] = child['ddl'].replace('bigint', 'varchar(20)').replace('`fk_c2`', '`new_fk`')
                report = schema_versions.compare(validate(source), validate(target), chosen_keys=['relation:child3'])
                self.assertIn('no index', self.skipped(report, 'child3'))
                self.assertEqual(report['sync']['statements'], [])

    def test_kept_parent_charset_and_collation_are_checked(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                source, target = replay.retained_child_pair(server)
                for snapshot_ in (source, target):
                    for name in ('parent2', 'child2', 'child3'):
                        tables = [o for o in snapshot_['objects'] if o['name'] == name]
                        for table in tables:
                            table['ddl'] = table['ddl'].replace('bigint', 'varchar(20)').replace(' int ', ' varchar(20) ')
                            table['columns'] = {'id' if name == 'parent2' else 'pid': ['utf8mb4', 'utf8mb4_bin']}
                obj(source, 'table', 'parent2')['columns']['id'] = ['latin1', 'latin1_bin']
                obj(source, 'table', 'child3')['columns']['pid'] = ['latin1', 'latin1_bin']
                obj(source, 'table', 'child3')['ddl'] = obj(source, 'table', 'child3')['ddl'].replace('`fk_c2`', '`new_fk`')
                report = schema_versions.compare(validate(source), validate(target))
                self.assertIn('child2', self.skipped(report, 'parent2'))
                self.assertIn('collation', self.skipped(report, 'child3'))
                self.assertEqual(self.entry(report, 'child3')['blocked_by'], ['relation:child2'])

    def test_kept_child_blocks_removing_its_parent_column_or_index(self):
        for server in replay.SERVERS:
            for remove in ('column', 'index'):
                with self.subTest(server=server, remove=remove):
                    source, target = replay.retained_child_pair(server)
                    parent = obj(source, 'table', 'parent2')
                    parent['ddl'] = ('CREATE TABLE `parent2` (\n  `other` int NOT NULL\n) ENGINE=InnoDB' if remove == 'column'
                                     else 'CREATE TABLE `parent2` (\n  `id` int NOT NULL\n) ENGINE=InnoDB')
                    report = schema_versions.compare(validate(source), target)
                    self.assertIn('foreign key fk_c2', self.skipped(report, 'parent2'))
                    self.assertEqual(self.entry(report, 'parent2')['blocked_by'], ['relation:child2'])

    def test_mysql_check_name_scope_and_mariadb_table_scope(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                target = snap(server, 'before')
                base = {'type': 'table', 'columns': {}}
                old = dict(base, name='kept_check', ddl='CREATE TABLE `kept_check` (\n  `v` int,\n  CONSTRAINT `chk_same` CHECK (`v` > 0)\n) ENGINE=InnoDB')
                target['objects'].append(old)
                source = copy.deepcopy(target)
                source['objects'].remove(old)
                source['objects'].append(dict(base, name='new_check', ddl=old['ddl'].replace('`kept_check`', '`new_check`')))
                report = schema_versions.compare(validate(source), validate(target))
                if server == 'mysql-8.4':
                    self.assertIn('CHECK name', self.skipped(report, 'new_check'))
                    self.assertEqual(self.entry(report, 'new_check')['blocked_by'], ['relation:kept_check'])
                    keys = report['sync']['chosen_keys'] + ['relation:kept_check']
                    selected = schema_versions.compare(validate(source), validate(target), chosen_keys=keys)
                    self.assertTrue(phase_statements(selected['sync'], 'create_tables', 'new_check'))
                else:
                    self.assertFalse(self.skipped(report, 'new_check'))
                    self.assertTrue(phase_statements(report['sync'], 'create_tables', 'new_check'))


class TestRiskProjection(unittest.TestCase):
    def test_script_reuses_each_statement_risk_sentence_verbatim(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                mig = sync(server)
                count = 0
                for statement in mig['statements']:
                    details = statement['risk_details']
                    self.assertEqual(set(details), {'destructive', 'data_dependent', 'rebuild'})
                    if details['rebuild']:
                        self.assertIn('-- WARNING: ' + details['rebuild'], mig['script'])
                        count += 1
                    for kind, prefix in [('destructive', '-- WARNING: '),
                                         ('data_dependent', '-- CHECK: ')]:
                        for sentence in details[kind]:
                            self.assertIn(prefix + sentence, mig['script'])
                            count += 1
                self.assertGreater(count, 0)
                self.assertEqual(mig['summary']['warnings'], count)
                self.assertIn('%d warnings' % count, mig['script'])

    def test_omitted_drop_warns_in_comparison_but_not_the_script(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                report = schema_versions.compare(snap(server, 'after'), snap(server, 'before'))
                legacy = next(item for item in report['objects'] if item['name'] == 'legacy')
                self.assertTrue(legacy['risks']['destructive'])
                for sentence in legacy['risks']['destructive']:
                    self.assertNotIn('-- WARNING: ' + sentence, report['sync']['script'])


class TestAlgorithmPrediction(unittest.TestCase):
    BASE = ('CREATE TABLE `probe` (\n  `id` int NOT NULL,\n'
            "  `state` enum('a','c'),\n  PRIMARY KEY (`id`)\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin ROW_FORMAT=DYNAMIC")

    def pair(self, server, target_ddl=None, source_ddl=None):
        target = snap(server, 'before')
        target['objects'] = [o for o in target['objects'] if o['type'] == 'database'] + [
            {'type': 'table', 'name': 'probe', 'ddl': target_ddl or self.BASE, 'columns': {}}]
        source = copy.deepcopy(target)
        next(o for o in source['objects'] if o['name'] == 'probe')['ddl'] = source_ddl or self.BASE
        # The source can be newer: prediction must use the target version.
        source['server']['server_version'] = '99.0.0'
        return validate(source), validate(target)

    def report(self, server, source_ddl, target_ddl=None):
        source, target = self.pair(server, target_ddl, source_ddl)
        return schema_versions.compare(source, target)

    def check(self, report, algorithm, rebuild, concurrent):
        statements = [s for s in report['sync']['statements'] if s['sql'].startswith('ALTER TABLE')]
        self.assertEqual(len(statements), 1)
        statement = statements[0]
        prediction = statement['algorithm']
        self.assertEqual((prediction['predicted'], prediction['algorithm'], prediction['rebuild'],
                          prediction['concurrent_dml']), (True, algorithm, rebuild, concurrent))
        self.assertTrue(prediction['reason'])
        self.assertTrue(prediction['source'])
        self.assertTrue(prediction['clauses'])
        self.assertEqual(prediction['needs'], [])
        self.assertNotRegex(statement['sql'], r'(?i)\b(?:ALGORITHM|LOCK)\s*=')
        self.assertIn('-- ALGORITHM: ', report['sync']['script'])
        entry = next(e for e in report['objects'] if e['name'] == 'probe')
        risk = statement['risk_details']['rebuild']
        self.assertEqual(entry['risks']['rebuild'], risk)
        self.assertEqual(bool(risk), rebuild or algorithm == 'COPY')
        for change in entry['changes']:
            self.assertEqual(change['risks']['rebuild'], risk)
        return statement

    def test_virtual_add_column_is_instant_without_hidden_facts(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                ddl = self.BASE.replace('  PRIMARY KEY', '  `v` int GENERATED ALWAYS AS (`id` + 1) VIRTUAL,\n  PRIMARY KEY')
                report = self.report(server, ddl)
                self.check(report, 'INSTANT', False, True)
                self.assertIn('-- ALGORITHM: INSTANT\nALTER TABLE', report['sync']['script'])

    def test_enum_type_change_copies_with_target_engine_concurrency(self):
        # Changing ENUM storage order needs COPY, without external FK facts.
        # INT -> BIGINT instead needs referenced_columns and stays unknown.
        for server in replay.SERVERS:
            with self.subTest(server=server):
                report = self.report(server, self.BASE.replace("enum('a','c')", "enum('a','b','c')"))
                concurrent = server.startswith('mariadb')
                statement = self.check(report, 'COPY', True, concurrent)
                self.assertIn('Copies the table (COPY)', statement['risk_details']['rebuild'])
                suffix = 'writes continue' if concurrent else 'writes wait'
                self.assertIn('-- ALGORITHM: COPY, ' + suffix + '\nALTER TABLE', report['sync']['script'])
                self.assertIn(suffix, statement['risk_details']['rebuild'])

    def test_add_index_is_inplace_without_rebuild(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                report = self.report(server, self.BASE.replace('  PRIMARY KEY', '  KEY `k` (`state`),\n  PRIMARY KEY'))
                self.check(report, 'INPLACE', False, True)
                self.assertIn('-- ALGORITHM: INPLACE, no rebuild\nALTER TABLE', report['sync']['script'])

    def test_row_format_rebuilds_and_warning_precedes_algorithm(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                report = self.report(server, self.BASE.replace('DYNAMIC', 'COMPACT'))
                statement = self.check(report, 'INPLACE', True, True)
                self.assertIn('-- WARNING: ' + statement['risk_details']['rebuild'] +
                              '\n-- ALGORITHM: INPLACE, rebuilds the table\nALTER TABLE', report['sync']['script'])

    def test_partitioned_target_is_not_predicted(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                target = self.BASE + '\nPARTITION BY HASH (`id`) PARTITIONS 2'
                report = self.report(server, target.replace('DYNAMIC', 'COMPACT'), target)
                statement = next(s for s in report['sync']['statements'] if 'algorithm' in s)
                self.assertFalse(statement['algorithm']['predicted'])
                self.assertIn('partition', statement['algorithm']['reason'].lower())
                self.assertIsNone(statement['risk_details']['rebuild'])
                self.assertIsNone(next(e for e in report['objects'] if e['name'] == 'probe')['risks']['rebuild'])
                self.assertIn('-- ALGORITHM: not predicted (' + statement['algorithm']['reason'] +
                              ')\nALTER TABLE', report['sync']['script'])

    def test_hidden_facts_stay_unknown_for_stored_add_and_numeric_type_change(self):
        cases = [(self.BASE.replace('  PRIMARY KEY', '  `v` int,\n  PRIMARY KEY'), 'fts_doc_id_index'),
                 (self.BASE.replace('`id` int NOT NULL', '`id` bigint NOT NULL'), 'referenced_columns')]
        for server in replay.SERVERS:
            for ddl, fact in cases:
                with self.subTest(server=server, fact=fact):
                    report = self.report(server, ddl)
                    statement = next(s for s in report['sync']['statements'] if 'algorithm' in s)
                    self.assertFalse(statement['algorithm']['predicted'])
                    self.assertIn(fact, statement['algorithm']['needs'])
                    self.assertIsNone(statement['risk_details']['rebuild'])
                    self.assertIsNone(next(e for e in report['objects'] if e['name'] == 'probe')['risks']['rebuild'])
                    if server == 'mysql-8.4' and fact == 'fts_doc_id_index':
                        self.assertIn('total_row_versions', statement['algorithm']['needs'])

    def test_each_emitted_alter_uses_captured_target_without_facts_or_reads(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                source, target = snap(server, 'after'), snap(server, 'before')
                with mock.patch.object(alter_algorithm, 'predict', wraps=alter_algorithm.predict) as predict, \
                        mock.patch('myflames.connector.MySQLConnection', side_effect=AssertionError('offline')):
                    report = schema_versions.compare(source, target)
                alters = [s for s in report['sync']['statements'] if s['sql'].startswith('ALTER TABLE')]
                self.assertEqual(predict.call_count, len(alters))
                for statement, call in zip(alters, predict.call_args_list):
                    old = next((o for o in target['objects'] if o['type'] == statement['object']['type']
                                and o['name'] == statement['object']['name']), {})
                    self.assertEqual(call[0], (statement['sql'], old.get('ddl'), target['server']['engine'],
                                               target['server']['server_version']))
                    self.assertEqual(call[1], {})
                self.assertTrue(all(('algorithm' in s) == s['sql'].startswith('ALTER TABLE')
                                    for s in report['sync']['statements']))
                for statement in alters:
                    self.assertNotRegex(statement['sql'], r'(?i)\b(?:ALGORITHM|LOCK)\s*=')

    def test_multi_alter_change_risks_follow_their_own_statement(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                source = self.BASE.replace('DYNAMIC', 'COMPACT').replace(
                    '  PRIMARY KEY', '  CONSTRAINT `fk_probe` FOREIGN KEY (`id`) REFERENCES `probe` (`id`),\n  PRIMARY KEY')
                report = self.report(server, source)
                entry = next(e for e in report['objects'] if e['name'] == 'probe')
                changes = {c['part']: c for c in entry['changes']}
                statements = {s['phase']: s for s in report['sync']['statements'] if 'algorithm' in s}
                self.assertEqual(set(statements), {'alter_tables', 'add_foreign_keys'})
                self.assertEqual(changes['options']['risks']['rebuild'], statements['alter_tables']['risk_details']['rebuild'])
                self.assertIn('(INPLACE)', changes['options']['risks']['rebuild'])
                self.assertEqual(changes['foreign_key']['risks']['rebuild'], statements['add_foreign_keys']['risk_details']['rebuild'])
                self.assertIn('(COPY)', changes['foreign_key']['risks']['rebuild'])
                for statement in statements.values():
                    self.assertIn(statement['risk_details']['rebuild'], entry['risks']['rebuild'])

    def test_replaced_check_has_no_single_change_algorithm(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                target = self.BASE.replace('  PRIMARY KEY', '  CONSTRAINT `ck_probe` CHECK (`id` > 0),\n  PRIMARY KEY')
                source = target.replace('`id` > 0', '`id` > 1')
                report = self.report(server, source, target)
                entry = next(e for e in report['objects'] if e['name'] == 'probe')
                change = next(c for c in entry['changes'] if c['part'] == 'check')
                self.assertIsNone(change['risks']['rebuild'])
                self.assertIn('Copies the table', entry['risks']['rebuild'])
                self.assertEqual(len([s for s in report['sync']['statements'] if 'algorithm' in s]), 2)

    def test_reselection_clears_entry_and_change_rebuild_risks(self):
        source, target = self.pair('mysql-8.4', source_ddl=self.BASE.replace('DYNAMIC', 'COMPACT'))
        report = schema_versions.compare(source, target)
        self.assertTrue(next(e for e in report['objects'] if e['name'] == 'probe')['risks']['rebuild'])
        schema_sync.build(target, source, report, chosen_keys=[])
        self.assertIsNone(next(e for e in report['objects'] if e['name'] == 'probe')['risks']['rebuild'])
        self.assertTrue(all(c['risks']['rebuild'] is None for c in next(e for e in report['objects'] if e['name'] == 'probe')['changes']))

    def test_comparison_schema_and_json_roundtrip_with_predictions(self):
        path = os.path.join(os.path.dirname(__file__), '..', 'docs', 'schemas', 'schema-compare-1.0.json')
        with open(path) as handle:
            schema = json.load(handle)
        for server in replay.SERVERS:
            reports = [schema_versions.compare(snap(server, 'after'), snap(server, 'before')),
                       self.report(server, self.BASE.replace('DYNAMIC', 'COMPACT')),
                       self.report(server, self.BASE.replace("enum('a','c')", "enum('a','b','c')"))]
            for report in reports:
                with self.subTest(server=server, script=report['sync']['script']):
                    restored = json.loads(json.dumps(report, allow_nan=False))
                    validate_json_schema(restored, schema)
                    self.assertEqual(restored, report)
                    bad = copy.deepcopy(restored)
                    statement = next(s for s in bad['sync']['statements'] if 'algorithm' in s)
                    statement['algorithm']['algorithm'] = 'ONLINE'
                    with self.assertRaises(AssertionError):
                        validate_json_schema(bad, schema)


class TestCliSelection(unittest.TestCase):
    """CLI selection prints scripts only, using saved fixture definitions."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.dict(os.environ, {'HOME': self.temp.name, 'USERPROFILE': self.temp.name,
                                               'MYFLAMES_SECRET_BACKEND': 'vault'})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        self.store = WorkspaceStore()
        patcher = mock.patch.object(schema_versions_cli, 'MySQLConnection',
                                    side_effect=AssertionError('Saved compare must not connect'))
        self.connect = patcher.start()
        self.addCleanup(patcher.stop)

    def command(self, arguments):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                schema_versions_cli.cmd_schema(arguments)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def sides(self, server):
        source = self.store.schema_version_save(snap(server, 'after'))
        target = self.store.schema_version_save(snap(server, 'before'))
        return ['compare', source['id'], target['id']]

    def test_default_sql_omits_target_only_drop_and_names_the_reason(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                code, out, err = self.command(self.sides(server) + ['--sql'])
                self.assertEqual(code, 0, err)
                self.assertNotIn('DROP TABLE `legacy`', out)
                self.assertIn('only in the target are not dropped', out)
                self.assertIn('-- myflames never runs this script.', out)
        self.connect.assert_not_called()

    def test_include_drops_explicitly_selects_every_comparison_key(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                code, out, err = self.command(self.sides(server) + ['--include-drops', '--json'])
                self.assertEqual(code, 0, err)
                report = json.loads(out)
                self.assertEqual(report['sync']['chosen_keys'], [item['key'] for item in report['objects']])
                self.assertEqual(report['sync']['omitted'], [])
                self.assertIn('DROP TABLE `legacy`', report['sync']['script'])
                self.assertIn('DROP TRIGGER IF EXISTS `trg_bi2`', report['sync']['script'])
        self.connect.assert_not_called()

    def test_repeated_only_selects_exact_keys_including_target_only(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                code, out, err = self.command(self.sides(server) + [
                    '--only', 'relation:notes', '--only', 'relation:legacy', '--json'])
                self.assertEqual(code, 0, err)
                report = json.loads(out)
                self.assertEqual(set(report['sync']['chosen_keys']), {'relation:notes', 'relation:legacy'})
                self.assertIn('DROP TABLE `legacy`', report['sync']['script'])
                self.assertIn(NOTES_ALTER, report['sync']['script'])
                self.assertTrue(all(statement['object'] is None or
                                    statement['object']['name'] in ('notes', 'legacy')
                                    for statement in report['sync']['statements']))

    def test_unknown_duplicate_and_invalid_keys_preserve_existing_output(self):
        sides = self.sides('mysql-8.4')
        output = os.path.join(self.temp.name, 'keep.sql')
        with open(output, 'w') as handle:
            handle.write('original')
        for flags in (['--only', 'relation:missing'], ['--only', 'table:notes'],
                      ['--only', 'relation:notes', '--only', 'relation:notes'], ['--only', '']):
            with self.subTest(flags=flags):
                code, out, err = self.command(sides + flags + ['--sql', '-o', output])
                self.assertEqual(code, 2)
                self.assertEqual(out, '')
                self.assertEqual(len(err.splitlines()), 1, err)
                self.assertIn('key', err.lower())
                with open(output) as handle:
                    self.assertEqual(handle.read(), 'original')

    def test_include_drops_and_only_are_mutually_exclusive(self):
        code, out, err = self.command(['compare', 'live', 'live', '--include-drops',
                                      '--only', 'relation:notes'])
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertIn('not allowed', err)
        self.connect.assert_not_called()

    def test_help_explains_safe_default_and_repeatable_selection(self):
        code, out, err = self.command(['compare', '--help'])
        self.assertEqual(code, 0)
        self.assertEqual(err, '')
        self.assertIn('--include-drops', out)
        self.assertIn('omitted by default', out)
        self.assertIn('--only KEY', out)
        self.assertIn('repeat', out)


if __name__ == '__main__':
    unittest.main()
