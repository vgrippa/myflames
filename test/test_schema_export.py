"""Export as SQL (myflames/schema_export.py): the script that recreates a
schema version, its CLI form (``myflames schema export VERSION --sql``) and
its workspace endpoint (/api/schema-versions/export-sql).

Synthetic snapshots check each rule (foreign key order, cycles, view order,
DELIMITER blocks, definers, every object kind, session settings). The
recorded snapshots in test/fixtures/schema_export (xp_src read on throwaway
MySQL 8.4.13, MariaDB 11.4.13 and MySQL 5.7.44 containers on 2026-10-31,
host names synthetic) are the database the lab round trip ran: each export
was run into an empty database on the same server and compared Identical
with Schema versions. ``capture-mysql-5.7.json`` replays capture's client
calls on 5.7.44 (its privileges read is patched out: privileges.py owns it).
No server is contacted here.
"""
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _schema_replay as replay  # noqa: E402

from myflames import schema_export, schema_versions, schema_versions_api, schema_versions_cli  # noqa: E402
from myflames.workspace_store import WorkspaceStore  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'schema_export')
COMPLETE = {k: {'state': 'complete', 'reason': ''} for k in ('tables', 'routines', 'triggers', 'events')}
STAMP = '2026-10-31T12:00:00+00:00'


def fixture(name):
    with open(os.path.join(FIXTURES, name + '.json'), encoding='utf-8') as handle:
        return json.load(handle)


def table(name, elements, options='ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin', partitioning=''):
    ddl = 'CREATE TABLE `%s` (\n  %s\n) %s' % (name, ',\n  '.join(elements), options)
    if partitioning:
        ddl += '\n' + partitioning
    return {'type': 'table', 'name': name, 'columns': {}, 'ddl': ddl}


def fk(name, column, parent, schema=None):
    ref = '`%s`.`%s`' % (schema, parent) if schema else '`%s`' % parent
    return 'CONSTRAINT `%s` FOREIGN KEY (`%s`) REFERENCES %s (`id`)' % (name, column, ref)


def view(name, body, database='shop', definer='`root`@`%`'):
    return {'type': 'view', 'name': name, 'character_set_client': 'utf8mb4', 'collation_connection': 'utf8mb4_0900_ai_ci',
            'ddl': 'CREATE ALGORITHM=UNDEFINED DEFINER=%s SQL SECURITY DEFINER VIEW `%s`.`%s` AS %s'
                   % (definer, database, name, body)}


def program(kind, name, body, sql_mode='STRICT_TRANS_TABLES', definer='`app`@`%`', **extra):
    words = {'procedure': 'PROCEDURE', 'function': 'FUNCTION', 'package': 'PACKAGE', 'package_body': 'PACKAGE BODY',
             'trigger': 'TRIGGER', 'event': 'EVENT'}
    obj = {'type': kind, 'name': name, 'sql_mode': sql_mode, 'character_set_client': 'utf8mb4',
           'collation_connection': 'utf8mb4_0900_ai_ci', 'database_collation': 'utf8mb4_bin',
           'ddl': 'CREATE DEFINER=%s %s `%s`%s' % (definer, words[kind], name, body)}
    obj.update(extra)
    return obj


def snap(objects, engine='mysql', version='8.4.11', database='shop', coverage=None, db_ddl=None, notes=None):
    product = {'mysql': 'MySQL', 'mariadb': 'MariaDB'}[engine]
    value = {
        'schema_version': 'schema-snapshot-1.0', 'myflames_version': '3.3.1', 'taken_at': STAMP, 'database': database,
        'server': {'engine': engine, 'flavor': engine, 'server_product': product, 'server_version': version,
                   'hostname': 'lab', 'host': '127.0.0.1', 'port': 3306, 'lower_case_table_names': '0'},
        'connection': {}, 'read_settings': {'database': 'information_schema', 'sql_mode': ''},
        'coverage': coverage or COMPLETE, 'notes': notes or [],
        'objects': [{'type': 'database', 'name': database, 'character_set': 'utf8mb4', 'collation': 'utf8mb4_bin',
                     'ddl': db_ddl or 'CREATE DATABASE `%s` /*!40100 DEFAULT CHARACTER SET utf8mb4 COLLATE '
                                      'utf8mb4_bin */' % database}] + objects,
    }
    return schema_versions.validate_snapshot(value)


def build(snapshot, **options):
    return schema_export.build(snapshot, generated_at=STAMP, **options)


def statements(script):
    """The statements of a script as a client runs them: comments and blank
    lines between statements skipped, DELIMITER blocks honored."""
    out, current, delimiter = [], [], ';'
    for line in script.split('\n'):
        if not current and (not line.strip() or line.startswith('--')):
            continue
        if not current and line.startswith('DELIMITER '):
            delimiter = line[len('DELIMITER '):].strip()
            continue
        if delimiter != ';' and line == delimiter:
            out.append('\n'.join(current))
            current = []
            continue
        current.append(line)
        if delimiter == ';' and line.endswith(';'):
            out.append('\n'.join(current)[:-1])
            current = []
    assert not current, current
    return out


def creates(script, kind):
    """Names created by CREATE <kind> statements, in script order."""
    pattern = re.compile(r'CREATE (?:OR REPLACE )?(?:ALGORITHM=\w+ )?(?:DEFINER=\S+ )?(?:SQL SECURITY \w+ )?%s '
                         r'(?:`[^`]*`\.)?(?:`([^`]*)`|(\w+))' % kind)
    return [m.group(1) or m.group(2) for m in (pattern.match(s) for s in statements(script)) if m]


def index_of(script, text):
    found = [i for i, s in enumerate(statements(script)) if s.startswith(text)]
    assert len(found) == 1, (text, found)
    return found[0]


class TestTableOrder(unittest.TestCase):
    def test_parents_come_before_the_tables_that_refer_to_them(self):
        s = snap([
            table('child_a', ['`id` int NOT NULL', '`p` int', 'KEY `k` (`p`)', fk('fk_a', 'p', 'parent_z')]),
            table('leaf', ['`id` int NOT NULL', '`m` int', 'KEY `k` (`m`)', fk('fk_leaf', 'm', 'mid')]),
            table('mid', ['`id` int NOT NULL', '`p` int', 'KEY `k` (`p`)', fk('fk_mid', 'p', 'parent_z')]),
            table('parent_z', ['`id` int NOT NULL', 'PRIMARY KEY (`id`)']),
        ])
        out = build(s)
        self.assertEqual(creates(out['script'], 'TABLE'), ['parent_z', 'child_a', 'mid', 'leaf'])
        # No cycle: every foreign key stays inside its CREATE TABLE.
        self.assertFalse([x for x in statements(out['script']) if x.startswith('ALTER TABLE')])
        self.assertEqual(out['summary']['objects'], {'table': 4})

    def test_a_self_reference_stays_inline(self):
        out = build(snap([table('node', ['`id` int NOT NULL', '`up` int', 'PRIMARY KEY (`id`)', 'KEY `k` (`up`)',
                                          fk('fk_up', 'up', 'node')])]))
        create = statements(out['script'])[[i for i, x in enumerate(statements(out['script']))
                                             if x.startswith('CREATE TABLE')][0]]
        self.assertIn('CONSTRAINT `fk_up` FOREIGN KEY', create)
        self.assertNotIn('ALTER TABLE', out['script'])

    def test_a_cycle_is_created_without_its_keys_then_altered(self):
        s = snap([
            table('a', ['`id` int NOT NULL', '`b` int', 'PRIMARY KEY (`id`)', 'KEY `k` (`b`)', fk('fk_ab', 'b', 'b')]),
            table('b', ['`id` int NOT NULL', '`a` int', 'PRIMARY KEY (`id`)', 'KEY `k` (`a`)', fk('fk_ba', 'a', 'a')]),
            table('c', ['`id` int NOT NULL', '`a` int', 'KEY `k` (`a`)', fk('fk_ca', 'a', 'a')]),
        ])
        out = build(s)
        script = out['script']
        self.assertEqual(creates(script, 'TABLE'), ['a', 'b', 'c'])
        for name in ('a', 'b'):
            create = statements(script)[index_of(script, 'CREATE TABLE `%s`' % name)]
            self.assertNotIn('FOREIGN KEY', create)
            self.assertTrue(create.endswith(') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin'))
        # c refers to a only: its key stays inline, after the cycle exists.
        self.assertIn('CONSTRAINT `fk_ca`', statements(script)[index_of(script, 'CREATE TABLE `c`')])
        last_create = index_of(script, 'CREATE TABLE `c`')
        self.assertGreater(index_of(script, 'ALTER TABLE `a` ADD CONSTRAINT `fk_ab` FOREIGN KEY (`b`) REFERENCES `b`'),
                           last_create)
        self.assertGreater(index_of(script, 'ALTER TABLE `b` ADD CONSTRAINT `fk_ba`'), last_create)
        self.assertIn('--   a and b refer to each other, so this foreign key is added once both exist.', script)
        self.assertEqual(out['warnings'], [])

    def test_a_longer_cycle_and_a_table_after_it(self):
        s = snap([
            table('x', ['`id` int NOT NULL', '`y` int', 'KEY `k` (`y`)', fk('fk_xy', 'y', 'y')]),
            table('y', ['`id` int NOT NULL', '`z` int', 'KEY `k` (`z`)', fk('fk_yz', 'z', 'z')]),
            table('z', ['`id` int NOT NULL', '`x` int', 'KEY `k` (`x`)', fk('fk_zx', 'x', 'x')]),
            table('after', ['`id` int NOT NULL', '`z` int', 'KEY `k` (`z`)', fk('fk_after', 'z', 'z')]),
        ])
        script = build(s)['script']
        self.assertEqual(creates(script, 'TABLE'), ['x', 'y', 'z', 'after'])
        adds = [x for x in statements(script) if x.startswith('ALTER TABLE')]
        self.assertEqual(len(adds), 3)

    def test_keys_to_another_database_or_a_missing_table_are_added_at_the_end(self):
        unreadable = {'type': 'table', 'name': 'secret', 'ddl': None, 'columns': {},
                      'reason': 'The account cannot read it (ERROR 1142: denied).'}
        s = snap([
            table('t', ['`id` int NOT NULL', '`o` int', '`s` int', '`g` int', 'KEY `k1` (`o`)', 'KEY `k2` (`s`)',
                        'KEY `k3` (`g`)', fk('fk_other', 'o', 'parent', schema='other_db'),
                        fk('fk_secret', 's', 'secret'), fk('fk_gone', 'g', 'gone')]),
            unreadable,
        ])
        out = build(s)
        script = out['script']
        self.assertNotIn('FOREIGN KEY', statements(script)[index_of(script, 'CREATE TABLE `t`')])
        self.assertIn('ALTER TABLE `t` ADD CONSTRAINT `fk_other` FOREIGN KEY (`o`) REFERENCES `other_db`.`parent` (`id`)',
                      statements(script))
        self.assertIn('-- WARNING: Foreign key fk_other on t refers to other_db.parent in another database: that table '
                      'must exist first, so the key is added at the end.', script)
        self.assertIn('-- WARNING: Foreign key fk_secret on t refers to table secret, which could not be read: the key '
                      'is added at the end and fails unless that table exists.', script)
        self.assertIn('fk_gone on t refers to table gone, which is not in the schema version', script)
        self.assertEqual(out['not_exported'], [{'type': 'table', 'name': 'secret',
                                                'reason': 'The account cannot read it (ERROR 1142: denied).'}])
        self.assertIn('-- WARNING: Table secret is not in this script: The account cannot read it (ERROR 1142: denied).',
                      script)
        self.assertEqual(out['summary']['warnings'], 4)
        self.assertNotIn('CREATE TABLE `secret`', script)

    def test_a_definition_that_cannot_be_split_is_created_as_captured(self):
        odd = {'type': 'table', 'name': 'odd', 'columns': {}, 'ddl': 'CREATE TABLE `odd` (`id` int) ENGINE=InnoDB'}
        script = build(snap([odd]))['script']
        self.assertIn('CREATE TABLE `odd` (`id` int) ENGINE=InnoDB;', script)
        self.assertIn('could not be split into lines', script)

    def test_partitioning_is_kept_when_a_key_moves_out(self):
        s = snap([
            table('a', ['`id` int NOT NULL', '`b` int', 'PRIMARY KEY (`id`)', 'KEY `k` (`b`)', fk('fk_ab', 'b', 'b')],
                  partitioning='/*!50100 PARTITION BY HASH (`id`)\nPARTITIONS 4 */'),
            table('b', ['`id` int NOT NULL', '`a` int', 'PRIMARY KEY (`id`)', 'KEY `k` (`a`)', fk('fk_ba', 'a', 'a')]),
        ])
        create = statements(build(s)['script'])[1 + 2]  # USE, SET sql_mode, CREATE a
        self.assertTrue(create.startswith('CREATE TABLE `a`'), create)
        self.assertTrue(create.endswith('COLLATE=utf8mb4_bin\n/*!50100 PARTITION BY HASH (`id`)\nPARTITIONS 4 */'))


class TestViewsAndPrograms(unittest.TestCase):
    def test_views_come_after_routines_in_dependency_order(self):
        s = snap([
            table('t', ['`id` int NOT NULL', 'PRIMARY KEY (`id`)']),
            view('v_a', "select `shop`.`v_b`.`id` AS `id`, 'see `shop`.`v_c`.' AS `note` from `shop`.`v_b`"),
            view('v_b', 'select `shop`.`t`.`id` AS `id`,`shop`.`f`(1) AS `f` from `shop`.`t`'),
            view('v_c', 'select 1 AS `one`'),
            program('function', 'f', '(x INT) RETURNS int\n    DETERMINISTIC\nRETURN x'),
        ])
        script = build(s)['script']
        self.assertEqual(creates(script, 'VIEW'), ['v_b', 'v_a', 'v_c'])
        self.assertLess(index_of(script, 'CREATE FUNCTION'), index_of(script, 'CREATE ALGORITHM=UNDEFINED SQL SECURITY '
                                                                             'DEFINER VIEW `shop`.`v_b`'))

    def test_a_view_on_an_unreadable_view_is_warned(self):
        missing = {'type': 'view', 'name': 'hidden', 'ddl': None, 'reason': 'ERROR 1356: invalid'}
        out = build(snap([view('top', 'select 1 AS `x` from `shop`.`hidden`'), missing]))
        self.assertIn('-- WARNING: View top uses view hidden, which is not in this script: create it first.', out['script'])

    def test_a_view_calling_an_unreadable_function_is_warned(self):
        hidden = {'type': 'function', 'name': 'f_secret', 'ddl': None, 'reason': 'The account cannot read it.',
                  'sql_mode': '', 'character_set_client': None, 'collation_connection': None, 'database_collation': None}
        s = snap([view('v1', 'select `f_secret`(1) AS `x`'), view('v2', 'select `shop`.`f_secret`(2) AS `x`'),
                  view('v3', "select 'f_secret(3)' AS `x`, `f_secret_two`(1) AS `y`"), hidden],
                 coverage=dict(COMPLETE, routines={'state': 'unknown', 'reason': 'SHOW GRANTS does not show EXECUTE.'}))
        out = build(s)
        self.assertIn('-- WARNING: View v1 calls function f_secret, which is not in this script: create it first.',
                      out['script'])
        self.assertIn('-- WARNING: View v2 calls function f_secret', out['script'])
        self.assertNotIn('View v3 calls', out['script'])
        self.assertIn('The account that read the source may not see every routine: SHOW GRANTS does not show EXECUTE. '
                      'A view that calls a function it could not see fails to be created.', out['warnings'])

    def test_delimiter_blocks_choose_a_mark_the_body_does_not_use(self):
        s = snap([
            program('procedure', 'p1', '()\nBEGIN\n  SELECT \'$$ and ;;\';\nEND'),
            program('procedure', 'p2', '()\nBEGIN\n  SELECT 1; -- ends in a comment\nEND'),
            program('procedure', 'p3', '()\nBEGIN\n  SELECT \'$$ ;; // $$$$\';\nEND'),
        ])
        script = build(s)['script']
        self.assertIn("DELIMITER //\nCREATE PROCEDURE `p1`()\nBEGIN\n  SELECT '$$ and ;;';\nEND\n//\nDELIMITER ;", script)
        self.assertIn('DELIMITER $$\nCREATE PROCEDURE `p2`()\nBEGIN\n  SELECT 1; -- ends in a comment\nEND\n$$\nDELIMITER ;',
                      script)
        self.assertIn('DELIMITER $$1\nCREATE PROCEDURE `p3`', script)
        self.assertEqual(creates(script, 'PROCEDURE'), ['p1', 'p2', 'p3'])

    def test_definers_are_left_out_by_default_and_kept_on_request(self):
        s = snap([view('v', 'select 1 AS `x`', definer="`dba`@`10.%`"),
                  program('procedure', 'p', '()\nSELECT 1', definer='`app`@`%`'),
                  program('trigger', 'trg', ' BEFORE INSERT ON `t` FOR EACH ROW SET NEW.id = 1', table='t',
                          timing='BEFORE', event='INSERT', order=1),
                  program('event', 'e', ' ON SCHEDULE EVERY 1 DAY DO SELECT 1', time_zone='SYSTEM'),
                  table('t', ['`id` int NOT NULL'])])
        plain = build(s)
        self.assertNotIn('DEFINER=', plain['script'].replace('becomes the DEFINER', ''))
        self.assertIn('CREATE ALGORITHM=UNDEFINED SQL SECURITY DEFINER VIEW `shop`.`v`', plain['script'])
        self.assertIn('Definers are left out: the account that runs the script becomes the DEFINER of every view, '
                      'routine, trigger and event.', plain['notes'])
        kept = build(s, keep_definers=True)
        for definer in ("DEFINER=`dba`@`10.%` SQL SECURITY", 'CREATE DEFINER=`app`@`%` PROCEDURE `p`',
                        'CREATE DEFINER=`app`@`%` TRIGGER', 'CREATE DEFINER=`app`@`%` EVENT'):
            self.assertIn(definer, kept['script'])
        self.assertIn('Views, routines, triggers and events keep their DEFINER: the account that runs the script needs '
                      'the privilege to name another definer, and each definer account should exist on the server. '
                      'MySQL 8.2 and later refuse a missing definer without ALLOW_NONEXISTENT_DEFINER (or SUPER); '
                      'elsewhere a trigger, an event or an SQL SECURITY DEFINER view or routine whose definer is missing '
                      'fails when it runs.', kept['notes'])
        self.assertEqual(kept['options'], {'database': 'shop', 'create_database': False, 'keep_definers': True})

    def test_programs_run_under_their_own_settings_and_the_session_is_restored(self):
        s = snap([
            table('t', ['`id` int NOT NULL']),
            program('procedure', 'p', '()\nSELECT 1', sql_mode='ANSI_QUOTES'),
            program('function', 'f', '() RETURNS int\nRETURN 1', sql_mode=''),
            program('event', 'e', " ON SCHEDULE EVERY 1 DAY STARTS '2030-01-01 00:00:00' DO SELECT 1",
                    sql_mode='STRICT_ALL_TABLES', time_zone='+02:00'),
        ])
        script = build(s)['script']
        self.assertIn("SET @myflames_sql_mode = @@SESSION.sql_mode, @myflames_character_set_client = "
                      "@@SESSION.character_set_client, @myflames_collation_connection = @@SESSION.collation_connection, "
                      "@myflames_time_zone = @@SESSION.time_zone;", script)
        steps = statements(script)
        self.assertEqual(steps[index_of(script, 'CREATE TABLE') - 1], "SET SESSION sql_mode = 'NO_ENGINE_SUBSTITUTION'")
        self.assertEqual(steps[index_of(script, 'CREATE PROCEDURE') - 1],
                         "SET SESSION character_set_client = 'utf8mb4', collation_connection = 'utf8mb4_0900_ai_ci', "
                         "sql_mode = 'ANSI_QUOTES'")
        self.assertEqual(steps[index_of(script, 'CREATE FUNCTION') - 1], "SET SESSION sql_mode = ''")
        self.assertEqual(steps[index_of(script, 'CREATE EVENT') - 1],
                         "SET SESSION sql_mode = 'STRICT_ALL_TABLES', time_zone = '+02:00'")
        self.assertEqual(steps[-1], 'SET SESSION sql_mode = @myflames_sql_mode, character_set_client = '
                                    '@myflames_character_set_client, collation_connection = '
                                    '@myflames_collation_connection, time_zone = @myflames_time_zone')
        # ASCII only: no SET NAMES.
        self.assertNotIn('SET NAMES', script)

    def test_text_outside_ascii_reads_as_utf8mb4(self):
        latin = view('v', "select 'café' AS `x`")
        latin['character_set_client'] = 'latin1'
        latin['collation_connection'] = 'latin1_swedish_ci'
        s = snap([table('t', ["`id` int NOT NULL COMMENT 'ação'"]), latin, program('procedure', 'p', '()\nSELECT 1')])
        script = build(s)['script']
        self.assertIn('SET NAMES utf8mb4;', script)
        steps = statements(script)
        self.assertEqual(steps[0], 'SET @myflames_character_set_client = @@SESSION.character_set_client, '
                                   '@myflames_character_set_results = @@SESSION.character_set_results, '
                                   '@myflames_collation_connection = @@SESSION.collation_connection, '
                                   '@myflames_sql_mode = @@SESSION.sql_mode')
        # The latin1 view's text holds é: it is sent as UTF-8 with its own
        # collation (after the procedure, which ran under its own sql_mode).
        self.assertEqual(steps[index_of(script, 'CREATE ALGORITHM') - 1],
                         "SET SESSION collation_connection = 'latin1_swedish_ci', sql_mode = 'NO_ENGINE_SUBSTITUTION'")

    def test_triggers_follow_the_source_order_without_order_clauses(self):
        s = snap([
            table('orders', ['`id` int NOT NULL']),
            program('trigger', 'second', ' BEFORE INSERT ON `orders` FOR EACH ROW FOLLOWS `first` SET NEW.id = 2',
                    table='orders', timing='BEFORE', event='INSERT', order=2),
            program('trigger', 'first', ' BEFORE INSERT ON `orders` FOR EACH ROW SET NEW.id = 1',
                    table='orders', timing='BEFORE', event='INSERT', order=1),
            program('trigger', 'after_upd', ' AFTER UPDATE ON `orders` FOR EACH ROW SET @x = 1',
                    table='orders', timing='AFTER', event='UPDATE', order=1),
            program('trigger', 'orphan', ' BEFORE DELETE ON `gone` FOR EACH ROW SET @x = 1',
                    table='gone', timing='BEFORE', event='DELETE', order=1),
        ])
        script = build(s)['script']
        # Groups by table, timing and event; each group in the source's order.
        self.assertEqual(creates(script, 'TRIGGER'), ['orphan', 'after_upd', 'first', 'second'])
        self.assertIn('CREATE TRIGGER `second` BEFORE INSERT ON `orders` FOR EACH ROW SET NEW.id = 2', script)
        self.assertNotIn('FOLLOWS', script)
        self.assertIn('-- WARNING: Trigger orphan is on table gone, which is not in this script: create the table first.',
                      script)

    def test_mariadb_sequences_packages_and_versioning(self):
        s = snap([
            {'type': 'sequence', 'name': 'seq', 'ddl': 'CREATE SEQUENCE `seq` start with 100 minvalue 1 maxvalue '
                                                       '9223372036854775806 increment by 10 cache 1000 nocycle ENGINE=InnoDB'},
            table('ticket', ['`id` bigint(20) NOT NULL DEFAULT nextval(`shop`.`seq`)', 'PRIMARY KEY (`id`)']),
            table('hist', ['`id` int(11) NOT NULL', 'PRIMARY KEY (`id`)'],
                  options='ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin WITH SYSTEM VERSIONING'),
            program('package_body', 'pkg', ' AS FUNCTION f1 RETURN INT AS BEGIN RETURN 2; END; END',
                    sql_mode='PIPES_AS_CONCAT,ANSI_QUOTES,ORACLE', definer='"root"@"%"'),
            program('package', 'pkg', ' AS FUNCTION f1 RETURN INT; END', sql_mode='PIPES_AS_CONCAT,ANSI_QUOTES,ORACLE',
                    definer='"root"@"%"'),
        ], engine='mariadb', version='11.4.13-MariaDB', database='shop')
        out = build(s, database='copy')
        script = out['script']
        self.assertLess(index_of(script, 'CREATE SEQUENCE `seq`'), index_of(script, 'CREATE TABLE `hist`'))
        self.assertIn('DEFAULT nextval(`copy`.`seq`)', script)
        self.assertLess(index_of(script, 'CREATE PACKAGE `pkg`'), index_of(script, 'CREATE PACKAGE BODY `pkg`'))
        self.assertEqual(out['warnings'], [
            'Sequence seq starts again at its START WITH value: a schema version keeps definitions, not the current '
            'value. Set it with SETVAL after the script if needed.',
            "Table hist uses system versioning: it is created with versioning and without the source's history rows, "
            'which are data and not part of a schema version.'])
        self.assertEqual(out['summary']['objects'], {'sequence': 1, 'table': 2, 'package': 1, 'package_body': 1})

    def test_events_with_fixed_times_replicas_and_named_zones(self):
        s = snap([
            program('event', 'once', " ON SCHEDULE AT '2020-01-01 00:00:00' ON COMPLETION PRESERVE DISABLE ON REPLICA DO "
                                     "DELETE FROM t WHERE x = ' DO '", time_zone='Europe/Lisbon'),
        ])
        out = build(s)
        self.assertEqual(out['warnings'], [
            'Event once has a fixed AT or ENDS time: if it has passed when the script runs, the server does not create '
            'the event, or creates it disabled.',
            'Event once is marked DISABLE ON REPLICA (a replica marks replicated events this way; it can also be set by '
            'hand) and is created that way; enable it with ALTER EVENT ... ENABLE where it should run. DISABLE ON '
            'REPLICA needs MySQL 8.2 or later; older MySQL and MariaDB accept DISABLE ON SLAVE.'])
        self.assertIn('--   Event once uses the time zone Europe/Lisbon, which needs the time zone tables on the server.',
                      out['script'])
        self.assertIn("time_zone = 'Europe/Lisbon'", out['script'])

    def test_files_tablespaces_and_encryption_are_warned(self):
        s = snap([
            table('files', ['`id` int NOT NULL'], options="ENGINE=InnoDB DATA DIRECTORY='/data/x/'"),
            table('spaced', ['`id` int NOT NULL'], options='/*!50100 TABLESPACE `ts1` */ ENGINE=InnoDB'),
            table('system', ['`id` int NOT NULL'], options='/*!50100 TABLESPACE `innodb_system` */ ENGINE=InnoDB'),
            table('secret', ['`id` int NOT NULL'], options="ENGINE=InnoDB ENCRYPTION='Y'"),
        ])
        warnings = build(s)['warnings']
        self.assertEqual(warnings, [
            "Table files keeps its files in '/data/x/': the server must be able to create files there, and on MySQL "
            "8.0.21 or later an InnoDB table's directory must be under the data directory, innodb_data_home_dir or a "
            'directory in innodb_directories.',
            'Table secret is encrypted: the server needs a keyring (MySQL) or an encryption plugin (MariaDB) for the '
            'CREATE TABLE to succeed.',
            'Table spaced is stored in tablespace ts1, which must exist on the server first.'])


class TestDatabaseAndScript(unittest.TestCase):
    def test_a_setting_one_object_records_does_not_carry_over(self):
        s = snap([program('event', 'e1', ' ON SCHEDULE EVERY 1 DAY DO SELECT 1', time_zone='+02:00'),
                  program('event', 'e2', ' ON SCHEDULE EVERY 1 DAY DO SELECT 2')])
        steps = statements(build(s)['script'])
        first, second = steps.index(next(x for x in steps if 'EVENT `e1`' in x)), \
            steps.index(next(x for x in steps if 'EVENT `e2`' in x))
        self.assertTrue(any("time_zone = '+02:00'" in x for x in steps[:first]))
        self.assertTrue(any('time_zone = @myflames_time_zone' in x for x in steps[first:second]))

    def test_a_name_outside_ascii_brings_a_utf8_client(self):
        latin = view('v', 'select `shop`.`t`.`id` AS `id` from `shop`.`t`')
        latin['character_set_client'] = 'latin1'
        out = build(snap([table('t', ['`id` int NOT NULL']), latin]), database='caf\u00e9')
        steps = statements(out['script'])
        created = steps.index(next(x for x in steps if 'VIEW `caf\u00e9`.`v`' in x))
        sets = [x for x in steps[:created] if 'character_set_client' in x]
        self.assertNotIn("'latin1'", sets[-1])

    def test_a_snapshot_without_its_database_object_still_exports(self):
        s = snap([table('t', ['`id` int NOT NULL'])])
        s['objects'] = [o for o in s['objects'] if o['type'] != 'database']
        out = build(s, create_database=True)
        self.assertIn('CREATE DATABASE `shop`', out['script'])
        self.assertIn('CREATE TABLE `t`', out['script'])

    def test_a_copy_under_another_name_compares_identical(self):
        def side(db):
            return snap([table('orders', ['`id` int NOT NULL']),
                         view('v_orders', 'select `orders`.`id` AS `id` from `%s`.`orders`' % db, database=db)],
                        database=db)
        report = schema_versions.compare(side('orders'), side('orders_copy'), sync=False)
        self.assertEqual({e['status'] for e in report['objects'] if e['type'] != 'database'}, {'identical'})

    def test_views_follow_names_compared_in_lower_case(self):
        # lower_case_table_names 2 keeps V_Zone as created; a view may name it v_zone.
        s = snap([table('t', ['`id` int NOT NULL']),
                  view('A_audit', 'select `v_zone`.`id` AS `id` from `shop`.`v_zone`'),
                  view('V_Zone', 'select `t`.`id` AS `id` from `shop`.`t`')])
        s['server']['lower_case_table_names'] = '2'
        script = build(s)['script']
        self.assertLess(script.index('VIEW `shop`.`V_Zone`'), script.index('VIEW `shop`.`A_audit`'))

    def test_an_alias_named_like_the_database_keeps_its_column_references(self):
        s = snap([table('orders', ['`id` int NOT NULL']),
                  view('v', 'select `shop`.`id` AS `id` from `shop`.`orders` `shop`')])
        out = build(s, database='copy')
        self.assertIn('VIEW `copy`.`v` AS select `shop`.`id` AS `id` from `copy`.`orders` `shop`', out['script'])

    def test_a_table_named_like_the_database_keeps_its_column_references(self):
        s = snap([table('orders', ['`id` int NOT NULL']),
                  view('v_orders', 'select `orders`.`id` AS `id` from `orders`.`orders`', database='orders')],
                 database='orders')
        out = build(s, database='orders_copy')
        self.assertIn('VIEW `orders_copy`.`v_orders` AS select `orders`.`id` AS `id` from `orders_copy`.`orders`',
                      out['script'])

    def test_create_database_and_another_name(self):
        s = snap([table('t', ['`id` int NOT NULL']), view('v', 'select `shop`.`t`.`id` AS `id` from `shop`.`t`'),
                  program('procedure', 'p', '()\nSELECT * FROM shop.t')])
        out = build(s, database='shop_copy', create_database=True)
        steps = statements(out['script'])
        self.assertEqual(steps[1:3], ['CREATE DATABASE `shop_copy` /*!40100 DEFAULT CHARACTER SET utf8mb4 COLLATE '
                                      'utf8mb4_bin */', 'USE `shop_copy`'])
        self.assertIn('VIEW `shop_copy`.`v` AS select `shop_copy`.`t`.`id` AS `id` from `shop_copy`.`t`', out['script'])
        self.assertIn('Definitions use the database name shop_copy where the source says shop.', out['notes'])
        self.assertIn('A routine, trigger or event body names shop without backquotes: those references are not changed.',
                      out['notes'])
        self.assertTrue(out['script'].startswith('-- myflames schema export\n-- Recreates shop as read from shop on '
                                                 '127.0.0.1:3306 · MySQL 8.4.11, read 2026-10-31 12:00:00 UTC.\n'))
        self.assertIn('-- It creates the database shop_copy and its objects and drops nothing.', out['script'])
        self.assertEqual(out['filename'], 'schema-shop.sql')

    def test_without_create_database_the_script_switches_to_it(self):
        out = build(snap([table('t', ['`id` int NOT NULL'])]))
        self.assertNotIn('CREATE DATABASE', out['script'])
        self.assertIn('USE `shop`', statements(out['script']))
        self.assertIn('The script does not create the database: run it where shop exists and is empty. The source\'s '
                      'default character set and collation are utf8mb4 / utf8mb4_bin.', out['notes'])

    def test_a_read_only_database_is_made_read_only_again_at_the_end(self):
        ddl = ("CREATE DATABASE `shop` /*!40100 DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin */ "
               "/*!80016 DEFAULT ENCRYPTION='N' */ /* READ ONLY = 1 */")
        out = build(snap([table('t', ['`id` int NOT NULL'])], db_ddl=ddl), create_database=True)
        steps = statements(out['script'])
        self.assertEqual(steps[1], "CREATE DATABASE `shop` /*!40100 DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin */ "
                                   "/*!80016 DEFAULT ENCRYPTION='N' */")
        self.assertEqual(steps[-2], 'ALTER DATABASE `shop` READ ONLY = 1')

    def test_unreadable_objects_coverage_and_snapshot_notes_lead_the_script(self):
        s = snap([program('procedure', 'hidden', '()\nSELECT 1'), table('t', ['`id` int NOT NULL'])],
                 coverage=dict(COMPLETE, routines={'state': 'unknown', 'reason': 'SHOW GRANTS does not show EXECUTE.'}),
                 notes=['JavaScript libraries (CREATE LIBRARY) are not read.'])
        s['objects'] = [dict(o, ddl=None, reason='The account cannot read this routine\'s definition.')
                        if o['name'] == 'hidden' else o for o in s['objects']]
        out = build(s)
        head = out['script'].split('\n\n')[1].split('\n')
        self.assertEqual(head, [
            '-- WARNING: The account that read the source may not see every routine: SHOW GRANTS does not show EXECUTE.',
            '-- WARNING: JavaScript libraries (CREATE LIBRARY) are not read.',
            "-- WARNING: Procedure hidden is not in this script: The account cannot read this routine's definition."])
        self.assertIn('-- 3 warnings.', out['script'])
        self.assertNotIn('`hidden`', out['script'])

    def test_bad_options(self):
        s = snap([])
        with self.assertRaisesRegex(ValueError, 'Choose another database name'):
            build(s, database='information_schema')
        with self.assertRaisesRegex(ValueError, 'true or false'):
            build(s, create_database='yes')
        with self.assertRaisesRegex(ValueError, '1 to 64 characters'):
            build(s, database='x' * 65)
        self.assertEqual(build(s, database='')['database'], 'shop')

    def test_the_script_only_creates(self):
        for name in ('xp_src-mysql-8.4', 'xp_src-mariadb-11.4', 'xp_src-mysql-5.7'):
            with self.subTest(name=name):
                out = build(schema_versions.validate_snapshot(fixture(name)), create_database=True)
                verbs = {re.match(r'[A-Z]+(?: [A-Z]+)?', s).group(0) for s in statements(out['script'])}
                self.assertEqual(verbs - {'CREATE TABLE', 'CREATE DATABASE', 'CREATE ALGORITHM', 'CREATE PROCEDURE',
                                          'CREATE FUNCTION', 'CREATE TRIGGER', 'CREATE EVENT', 'CREATE SEQUENCE',
                                          'CREATE PACKAGE', 'ALTER TABLE', 'USE', 'SET SESSION', 'SET NAMES', 'SET'},
                                 set())
                self.assertNotIn('DROP ', '\n'.join(statements(out['script'])))


class TestRecordedLab(unittest.TestCase):
    """The lab databases: foreign keys with a cycle, views on views, routines,
    triggers in an order, an event, and engine extras."""

    def export(self, server, **options):
        return build(schema_versions.validate_snapshot(fixture('xp_src-' + server)), database='xp_dst',
                     create_database=True, **options)

    def test_every_engine_orders_the_lab_database(self):
        for server in ('mysql-8.4', 'mariadb-11.4', 'mysql-5.7'):
            with self.subTest(server=server):
                script = self.export(server)['script']
                tables = creates(script, 'TABLE')
                self.assertLess(tables.index('customers'), tables.index('orders'))
                self.assertLess(tables.index('orders'), tables.index('a_lines'))
                self.assertEqual(creates(script, 'VIEW'), ['v_orders', 'v_big', 'v_bigger'])
                self.assertEqual(creates(script, 'TRIGGER'), ['trg_au', 'trg_bi0', 'trg_bi'])
                adds = [s for s in statements(script) if s.startswith('ALTER TABLE')]
                self.assertEqual(adds, [
                    'ALTER TABLE `departments` ADD CONSTRAINT `fk_dept_head` FOREIGN KEY (`head_id`) REFERENCES '
                    '`employees` (`id`)',
                    'ALTER TABLE `employees` ADD CONSTRAINT `fk_emp_dept` FOREIGN KEY (`dept_id`) REFERENCES '
                    '`departments` (`id`)'])
                self.assertLess(index_of(script, 'CREATE FUNCTION `f_double`'),
                                index_of(script, 'CREATE ALGORITHM=UNDEFINED SQL SECURITY DEFINER VIEW `xp_dst`.`v_orders`'))
                self.assertIn('SET NAMES utf8mb4;', script)
                self.assertIn("COMMENT 'login ação'", script)
                # The event keeps the time zone it was created in.
                self.assertIn("time_zone = '+02:00'", script)

    def test_engine_extras(self):
        maria = self.export('mariadb-11.4')
        self.assertEqual(maria['summary']['objects'], {'sequence': 1, 'table': 10, 'view': 3, 'procedure': 1,
                                                       'function': 1, 'package': 1, 'package_body': 1, 'trigger': 3,
                                                       'event': 1})
        self.assertEqual(len(maria['warnings']), 2)
        self.assertIn('DEFAULT nextval(`xp_dst`.`seq_ids`)', maria['script'])
        mysql = self.export('mysql-8.4')
        self.assertEqual(mysql['warnings'], [])
        self.assertIn('`my_row_id` bigint unsigned NOT NULL AUTO_INCREMENT /*!80023 INVISIBLE */', mysql['script'])
        old = self.export('mysql-5.7')
        # Recorded while 5.7's privileges read failed: every kind is unknown.
        self.assertEqual(len(old['warnings']), 4)
        self.assertIn('SPATIAL KEY `sp` (`p`)', old['script'])

    def test_older_fixture_with_unreadable_routines(self):
        limited = replay.snapshot('mysql-8.4', 'limited')
        out = build(limited)
        self.assertTrue(out['not_exported'])
        for item in out['not_exported']:
            self.assertIn('-- WARNING: %s %s is not in this script:' % (
                schema_versions.TYPE_LABELS[item['type']].capitalize(), item['name']), out['script'])


class TestContracts(unittest.TestCase):
    def test_the_recorded_lab_snapshots_are_schema_snapshots(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from test_chart_data import validate
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'docs', 'schemas', 'schema-snapshot-1.0.json')
        with open(path, encoding='utf-8') as handle:
            contract = json.load(handle)
        for name in ('xp_src-mysql-8.4', 'xp_src-mariadb-11.4', 'xp_src-mysql-5.7'):
            with self.subTest(name=name):
                validate(schema_versions.validate_snapshot(fixture(name)), contract)


class TestCapture57(unittest.TestCase):
    def test_capture_reads_mysql_57(self):
        record = fixture('capture-mysql-5.7')
        conn = replay.ReplayConn(record)
        conn.query_timeout = 120
        with mock.patch.object(schema_versions, 'coverage', return_value=COMPLETE):
            snapshot = schema_versions.capture(conn, 'xp_src', {'host': '127.0.0.1', 'port': 3306})
        self.assertEqual(snapshot['server']['server_version'], '5.7.44')
        self.assertEqual(snapshot['counts'], {'database': 1, 'table': 9, 'view': 3, 'procedure': 1, 'function': 1,
                                              'trigger': 3, 'event': 1, 'unreadable': 0})
        # 5.7 has show_create_table_verbosity but neither the GIPK nor the
        # secondary engine variables (8.0.30 and 8.0.18): they are not set.
        self.assertEqual(snapshot['read_settings'], {'database': 'information_schema', 'sql_mode': '',
                                                     'sql_quote_show_create': '1', 'show_create_table_verbosity': 'OFF'})
        orders = next(o for o in snapshot['objects'] if o['name'] == 'trg_bi0')
        self.assertEqual(orders['order'], 1)


class Isolated(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.dict(os.environ, {'HOME': self.temp.name, 'USERPROFILE': self.temp.name,
                                               'MYFLAMES_SECRET_BACKEND': 'vault'})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        self.store = WorkspaceStore()
        self.snapshot = schema_versions.validate_snapshot(fixture('xp_src-mysql-8.4'))


class TestCli(Isolated):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                schema_versions_cli.cmd_schema(argv)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_export_sql_from_a_saved_version_and_a_file(self):
        self.store.schema_version_save(self.snapshot, 'v', '')
        code, out, err = self.run_cli(['export', 'latest', '--sql', '--create-database', '--keep-definers'])
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith('-- myflames schema export\n'))
        self.assertIn('version 1 "v"', out.split('\n')[1])
        self.assertIn('CREATE DATABASE `xp_src`', out)
        self.assertIn('DEFINER=`root`@`%`', out)
        self.assertRegex(err, r'note: \d+ statements for xp_src: 11 tables, 3 views, .* myflames never runs it\.')
        path = os.path.join(self.temp.name, 'snap.json')
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(self.snapshot, handle)
        target = os.path.join(self.temp.name, 'out.sql')
        code, out, err = self.run_cli(['export', 'file:' + path, '--sql', '--target-database', 'copy', '-o', target, '-q'])
        self.assertEqual((code, out, err), (0, '', ''))
        with open(target, encoding='utf-8') as handle:
            text = handle.read()
        self.assertIn('USE `copy`;', text)
        self.assertNotIn('CREATE DATABASE', text)
        self.assertNotIn('DEFINER=`root`', text)

    def test_export_without_sql_keeps_the_snapshot_file(self):
        self.store.schema_version_save(self.snapshot, '', '')
        code, out, err = self.run_cli(['export', 'latest'])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['schema_version'], 'schema-snapshot-1.0')

    def test_sql_options_and_live_sides_need_sql(self):
        self.store.schema_version_save(self.snapshot, '', '')
        for argv, message in ((['export', 'latest', '--keep-definers'], '--keep-definers needs --sql'),
                              (['export', 'latest', '--target-database', 'x'], '--target-database needs --sql'),
                              (['export', 'live'], 'a live side needs --sql'),
                              (['export', 'profile:prod/shop'], 'a live side needs --sql'),
                              (['export', 'latest', '-h', 'db1'], 'a connection option needs --sql')):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(argv)
                self.assertEqual((code, out), (2, ''))
                self.assertIn(message, err)

    def test_export_sql_of_a_live_database(self):
        conn = replay.ReplayConn(fixture('capture-mysql-5.7'))
        conn.query_timeout = 120
        with mock.patch.object(schema_versions_cli, 'MySQLConnection', return_value=conn), \
                mock.patch.object(schema_versions, 'coverage', return_value=COMPLETE):
            code, out, err = self.run_cli(['export', 'live', '--sql', '-h', '127.0.0.1', '-u', 'root', '-D', 'xp_src'])
        self.assertEqual(code, 0, err)
        self.assertIn('MySQL 5.7.44', out.split('\n')[1])
        self.assertIn('CREATE TABLE `customers`', out)


class TestApi(Isolated):
    def call(self, payload, fill=None):
        return schema_versions_api.response(schema_versions_api.EXPORT_SQL_PATH, payload, self.store,
                                            fill or (lambda value: value))

    def test_a_saved_version(self):
        item = self.store.schema_version_save(self.snapshot, 'release', '')
        out = self.call({'id': item['id'], 'database': 'copy', 'create_database': True, 'keep_definers': False})
        self.assertEqual(out['filename'], 'schema-xp_src-v1.sql')
        self.assertEqual((out['kind'], out['database'], out['source_database']), ('version', 'copy', 'xp_src'))
        self.assertNotIn('statements', out)
        self.assertNotIn('export_id', out)
        self.assertIn('CREATE DATABASE `copy`', out['script'])
        self.assertEqual(out['server']['server_version'], '8.4.13')
        self.assertIn(schema_versions_api.EXPORT_SQL_PATH, schema_versions_api.PATHS)
        self.assertIn(schema_versions_api.EXPORT_SQL_PATH, schema_versions_api.LIVE_PATHS)

    def test_a_live_read_is_kept_for_option_changes(self):
        with mock.patch.object(schema_versions_api, 'read_live', return_value=self.snapshot) as read:
            first = self.call({'live': {'connection': {'host': 'h'}, 'database': 'xp_src'}})
            again = self.call({'export_id': first['export_id'], 'keep_definers': True})
        self.assertEqual(read.call_count, 1)
        self.assertEqual(first['kind'], 'live')
        self.assertEqual(again['export_id'], first['export_id'])
        self.assertNotIn('DEFINER=`root`', first['script'])
        self.assertIn('DEFINER=`root`', again['script'])
        with self.assertRaisesRegex(ValueError, 'read the database again'):
            self.call({'export_id': 'f' * 32})
        with self.assertRaisesRegex(ValueError, 'true or false'):
            self.call({'export_id': first['export_id'], 'create_database': 'yes'})


if __name__ == '__main__':
    unittest.main()
