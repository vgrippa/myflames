"""Regressions found by the review of schema versions (2026-10-26).

Each test names the defect it keeps fixed: database names in literals,
trigger order clauses in bodies, foreign keys Target does not have,
signedness, backslash-escaped option values, trigger order changes in text,
delimiters, copied column defaults, the password in snapshot text, the time
budget, version lookup beyond the newest rows, and malformed imports.
"""
import copy
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import uuid
from contextlib import closing
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _schema_replay as R  # noqa: E402
from myflames import schema_ddl, schema_sync, schema_versions  # noqa: E402
from myflames.connector import ConnectorError  # noqa: E402
from myflames.workspace_store import WorkspaceStore  # noqa: E402


def obj(snapshot, kind, name):
    return next(o for o in snapshot['objects'] if o['type'] == kind and o['name'] == name)


def edited(server, which, edit):
    snapshot = copy.deepcopy(R.load('%s-%s' % (server, which)))
    edit(snapshot)
    return schema_versions.validate_snapshot(snapshot)


class LiteralsAndBodies(unittest.TestCase):
    def test_requalify_keeps_literals_and_comments(self):
        text = "select '`sv_b`.`t`' AS `x`, `sv_b`.`t`.`id` from `sv_b`.`t` /* `sv_b`.`c` */ -- `sv_b`.`d`\nx"
        self.assertEqual(schema_ddl.requalify(text, 'sv_b', 'sv_m'),
                         "select '`sv_b`.`t`' AS `x`, `sv_m`.`t`.`id` from `sv_m`.`t` /* `sv_b`.`c` */ -- `sv_b`.`d`\nx")
        self.assertEqual(schema_ddl.requalify("'it\\'s `sv_b`.' `sv_b`.y", 'sv_b', 'z'), "'it\\'s `sv_b`.' `z`.y")

    def test_views_that_return_another_database_name_differ(self):
        def returning(name):
            def edit(snapshot):
                view = obj(snapshot, 'view', 'v_orders')
                view['ddl'] = view['ddl'].replace('select ', "select '`%s`.`t`' AS `n`," % name, 1)
            return edit
        target = edited('mysql-8.4', 'before', returning('sv_m'))
        source = edited('mysql-8.4', 'before', returning('sv_b'))
        source['database'] = 'sv_b'
        obj(source, 'database', 'sv_m')['name'] = 'sv_b'
        source = schema_versions.validate_snapshot(source)
        entry = next(e for e in schema_versions.compare(source, target)['objects'] if e['name'] == 'v_orders')
        self.assertEqual(entry['status'], 'different', 'a literal is data, never requalified')

    def test_trigger_body_keeps_text_that_looks_like_an_order_clause(self):
        def edit(snapshot):
            trigger = obj(snapshot, 'trigger', 'trg_bi')
            trigger['ddl'] = trigger['ddl'].replace('SET NEW.total = ROUND(NEW.total, 2)',
                                                    "SET NEW.note = ' FOR EACH ROW FOLLOWS `foo`'")
        target, source = R.snapshot('mysql-8.4', 'before'), edited('mysql-8.4', 'before', edit)
        sql = [s['sql'] for s in schema_versions.compare(source, target)['sync']['statements']
               if s['sql'].startswith('CREATE DEFINER') and 'trg_bi`' in s['sql']]
        self.assertEqual(len(sql), 1)
        self.assertIn("' FOR EACH ROW FOLLOWS `foo`'", sql[0])

    def test_trigger_header_order_clause_is_removed(self):
        def edit(snapshot):
            trigger = obj(snapshot, 'trigger', 'trg_bi')
            trigger['ddl'] = trigger['ddl'].replace('FOR EACH ROW ', 'FOR EACH ROW PRECEDES `trg_bi2` ', 1)
        target, source = R.snapshot('mysql-8.4', 'before'), edited('mysql-8.4', 'before', edit)
        sql = [s['sql'] for s in schema_versions.compare(source, target)['sync']['statements']
               if 'TRIGGER `trg_bi`' in s['sql']]
        self.assertTrue(sql and 'PRECEDES' not in sql[0], sql)


class ForeignKeysAndTypes(unittest.TestCase):
    def test_new_foreign_key_on_changed_columns_is_added_never_dropped(self):
        def edit(snapshot):
            items, tags = obj(snapshot, 'table', 'items'), obj(snapshot, 'table', 'item_tags')
            tags['ddl'] = tags['ddl'].replace(',\n  CONSTRAINT `fk_tag_item`',
                                              ',\n  CONSTRAINT `brand_new` FOREIGN KEY (`item_id`) REFERENCES `items` '
                                              '(`id`),\n  CONSTRAINT `fk_tag_item`')
            self.assertIn('brand_new', tags['ddl'])
            self.assertIn('bigint', items['ddl'])
        source = edited('mysql-8.4', 'after', edit)
        sync = schema_versions.compare(source, R.snapshot('mysql-8.4', 'before'))['sync']
        sqls = [s['sql'] for s in sync['statements']]
        self.assertNotIn('ALTER TABLE `item_tags` DROP FOREIGN KEY `brand_new`', sqls)
        self.assertIn('ALTER TABLE `item_tags` ADD CONSTRAINT `brand_new` FOREIGN KEY (`item_id`) REFERENCES '
                      '`items` (`id`)', sqls)

    def test_signedness_change_is_a_type_change(self):
        def unsigned(snapshot):
            for name, column in (('items', '`id` int unsigned'), ('item_tags', '`item_id` int unsigned')):
                table = obj(snapshot, 'table', name)
                table['ddl'] = table['ddl'].replace(column, column.replace(' unsigned', ''))
        target = edited('mysql-8.4', 'before', unsigned)
        sync = schema_versions.compare(R.snapshot('mysql-8.4', 'before'), target)['sync']
        sqls = [s['sql'] for s in sync['statements']]
        self.assertIn('ALTER TABLE `item_tags` DROP FOREIGN KEY `fk_tag_item`', sqls)
        modify = next(s for s in sync['statements'] if s['object'] and s['object']['name'] == 'items'
                      and s['phase'] == 'alter_tables')
        self.assertIn('may_change_data', modify['risks'])

    def test_option_value_with_a_backslash_escaped_quote_stays_whole(self):
        options = schema_ddl.table_options("COMMENT='it\\'s text' ROW_FORMAT=DYNAMIC")
        self.assertEqual(options, {'COMMENT': "COMMENT='it\\'s text'", 'ROW_FORMAT': 'ROW_FORMAT=DYNAMIC'})


class TextAndScript(unittest.TestCase):
    def test_trigger_order_change_has_text(self):
        def swap(snapshot):
            a, b = obj(snapshot, 'trigger', 'trg_bi'), obj(snapshot, 'trigger', 'trg_bi2')
            a['order'], b['order'] = b['order'], a['order']
        report = schema_versions.compare(edited('mysql-8.4', 'before', swap), R.snapshot('mysql-8.4', 'before'))
        changes = [c for e in report['objects'] for c in e.get('changes', []) if c['part'] == 'order']
        self.assertTrue(changes)
        self.assertTrue(all(c['text'].startswith('order: ') for c in changes))
        self.assertIn('order', schema_versions.format_text(report))

    def test_delimiter_when_the_body_holds_every_candidate(self):
        body = "CREATE PROCEDURE p() SELECT '$$ ;; // $$$$ $$1'"
        self.assertEqual(schema_sync._delimiter(body), '$$2')

    def test_copied_column_default_uses_befores_database(self):
        def edit(snapshot):
            snapshot['objects'].append({'type': 'table', 'name': 'uses_seq', 'columns': {}, 'ddl':
                                        'CREATE TABLE `uses_seq` (\n  `id` bigint NOT NULL DEFAULT nextval(`sv_b`.`seq`)'
                                        ',\n  PRIMARY KEY (`id`)\n) ENGINE=InnoDB'})
        sync = schema_versions.compare(edited('mariadb-11.4', 'after', edit),
                                            R.snapshot('mariadb-11.4', 'before'))['sync']
        create = next(s['sql'] for s in sync['statements'] if s['sql'].startswith('CREATE TABLE `uses_seq`'))
        self.assertIn('nextval(`sv_m`.`seq`)', create)


class CaptureAndStore(unittest.TestCase):
    def test_password_never_stays_in_snapshot_text(self):
        record = R.recording('mysql-8.4', 'before')
        secret = 'hunter-%s' % uuid.uuid4().hex[:6]

        def edit(method, sql, call):
            if "'myflames_who'" in sql:
                raise ConnectorError('mysql client exited 1: login %s failed' % secret)
            return call
        conn = R.ReplayConn(record, edit=edit)
        conn.password = secret
        snapshot = schema_versions.capture(conn, record['database'], record['source'])
        self.assertNotIn(secret, json.dumps(snapshot))
        self.assertIn('[redacted]', snapshot['coverage']['tables']['reason'])

    def test_every_read_gets_what_is_left_of_the_budget(self):
        record = R.recording('mysql-8.4', 'before')
        seen = []
        conn = R.ReplayConn(record)
        conn.query_timeout = 600
        original = conn.run

        def run(sql, extra_flags=None, timeout=None):
            seen.append((sql[:40], timeout or conn.query_timeout))
            return original(sql, extra_flags, timeout)
        conn.run = run
        # Ten seconds pass between budget checks: the privileges read (no
        # timeout of its own) may use only what is left at its turn.
        clock = iter(range(0, 10000, 10)).__next__
        schema_versions.capture(conn, record['database'], record['source'], clock=clock)
        self.assertTrue(all(limit <= schema_versions.READ_SECONDS for _, limit in seen), seen)
        privileges = [limit for sql, limit in seen if 'myflames:privileges' in sql]
        self.assertTrue(privileges and all(limit <= schema_versions.READ_SECONDS - 20 for limit in privileges), seen)
        self.assertEqual(conn.query_timeout, 600, 'the connection gets its own timeout back')

    def test_version_lookup_reaches_beyond_the_newest_rows(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {'HOME': home, 'USERPROFILE': home, 'MYFLAMES_SECRET_BACKEND': 'vault'}):
            os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
            store = WorkspaceStore()
            first = store.schema_version_save(R.snapshot('mysql-8.4', 'before'), 'oldest', '')
            summary = json.dumps(schema_versions.version_summary(R.snapshot('mysql-8.4', 'before')))
            with closing(sqlite3.connect(str(store.path))) as conn, conn:
                conn.executemany('INSERT INTO schema_versions (id, saved_at, source_key, database, number, name, '
                                 'note, summary, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                                 [(uuid.uuid4().hex, '2099-01-01T00:00:%02d' % (n % 60), 'x:1/db', 'db', n + 2, '',
                                   '', summary, b'') for n in range(1001)])
            self.assertEqual(store.schema_version_find(first['id'])['id'], first['id'])
            self.assertEqual(store.schema_version_find(first['id'][:8])['id'], first['id'])
            self.assertEqual(store.schema_version_find('oldest')['id'], first['id'])
            self.assertEqual([i['id'] for i in store.schema_version_list(database='sv_m')['items']], [first['id']])


class MalformedImports(unittest.TestCase):
    def test_wrong_collection_types_are_refused(self):
        good = R.load('mysql-8.4-before')
        for key, value in (('coverage', [1]), ('read_settings', 'x'), ('connection', [])):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    schema_versions.validate_snapshot(dict(good, **{key: value}))

    def test_a_version_holds_its_own_database_object(self):
        good = R.load('mysql-8.4-before')
        with self.assertRaises(ValueError):
            schema_versions.validate_snapshot(dict(good, objects=[]))
        other = copy.deepcopy(good)
        obj(other, 'database', 'sv_m')['name'] = 'elsewhere'
        with self.assertRaises(ValueError):
            schema_versions.validate_snapshot(other)


if __name__ == '__main__':
    unittest.main()
