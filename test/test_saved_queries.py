"""Saved queries of the Query editor (myflames/editor_session.py saved_query_entry
and the /api/editor/saved/* endpoints over workspace_store records), and the
list values the parameter form writes for a collapsed IN (...) list."""
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from myflames import dbops
from myflames import editor_session as ed
from myflames.live import bind_query
from myflames.workspace_store import WorkspaceStore
from test_chart_data import validate  # noqa: E402

SCHEMA = json.loads((Path(__file__).resolve().parent.parent / 'docs' / 'schemas' / 'saved-query-1.0.json')
                    .read_text(encoding='utf-8'))


class SavedQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.dict(os.environ, {'HOME': self.temp.name, 'USERPROFILE': self.temp.name,
                                          'MYFLAMES_SECRET_BACKEND': 'vault'})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        self.path = Path(self.temp.name) / 'private' / 'workspace.sqlite3'
        self.store = WorkspaceStore(self.path)
        self.workspace = types.SimpleNamespace(store=self.store)

    def call(self, path, **payload):
        return ed.api_response(path, payload, self.workspace, lambda v: v)

    def save(self, name='Top queries', sql='SELECT 1', folder='', note='', database=None):
        entry = ed.saved_query_entry(name, folder, sql, note, database)
        entry['id'] = self.store.saved_query_save(entry, ed.SAVED_QUERY_MAX_ENTRIES,
                                                  ed.SAVED_QUERY_MAX_BYTES)
        return entry

    def stored_payloads(self):
        with self.store._connect() as conn:
            return [row[0] for row in conn.execute('SELECT payload FROM records WHERE kind = ?',
                                                   ('saved_query',))]

    def test_save_and_list_round_trip(self):
        saved = self.save(name='Daily check', sql='SELECT * FROM t', folder='ops',
                          note='runs every morning', database='shop')
        items = self.store.saved_query_list()
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item['id'], saved['id'])
        self.assertEqual(item['schema_version'], 'saved-query-1.0')
        self.assertEqual(item['name'], 'Daily check')
        self.assertEqual(item['folder'], 'ops')
        self.assertEqual(item['sql'], 'SELECT * FROM t')
        self.assertEqual(item['note'], 'runs every morning')
        self.assertEqual(item['database'], 'shop')
        self.assertEqual(item['created'], item['updated'])
        # The record id is not part of the stored payload.
        self.assertNotIn('"id"', self.stored_payloads()[0])

    def test_sql_is_redacted_with_the_history_function(self):
        sql = "CREATE USER 'u'@'%' IDENTIFIED BY 'hunter2';\nSET PASSWORD FOR x = 'swordfish'"
        saved = self.save(sql=sql, note="pw: IDENTIFIED BY 'hunter2'")
        # Exactly what the run history stores for the same text.
        self.assertEqual(saved['sql'], dbops.redact(sql))
        for secret in ('hunter2', 'swordfish'):
            self.assertNotIn(secret, saved['sql'])
            self.assertNotIn(secret, json.dumps(self.store.saved_query_list()))
            self.assertNotIn(secret, ''.join(self.stored_payloads()))
        self.assertIn('[redacted]', saved['sql'])
        self.assertIn('[redacted]', saved['note'])

    def test_api_says_when_it_redacted(self):
        plain = self.call('/api/editor/saved/save', name='Plain', sql='SELECT 1')
        self.assertIs(plain['redacted'], False)
        secret = self.call('/api/editor/saved/save', name='User',
                           sql="ALTER USER app IDENTIFIED BY 'hunter2'")
        self.assertIs(secret['redacted'], True)
        self.assertNotIn('hunter2', json.dumps(secret))

    def test_secret_keys_are_never_stored(self):
        entry = ed.saved_query_entry('n', '', 'SELECT 1', '', None)
        entry['password'] = 'hunter2'
        key = self.store.saved_query_save(entry, 10, 100000)
        with self.store._connect() as conn:
            row = conn.execute('SELECT payload FROM records WHERE id = ?', (key,)).fetchone()
        self.assertNotIn('hunter2', row[0])
        self.assertNotIn('password', json.loads(row[0]))

    def test_validation_bounds(self):
        bad = (
            ({'name': ''}, 'empty name'),
            ({'name': '  \n '}, 'blank name'),
            ({'name': None}, 'missing name'),
            ({'name': 7}, 'name not text'),
            ({'name': 'x' * 201}, 'long name'),
            ({'folder': 'x' * 201}, 'long folder'),
            ({'folder': ['a']}, 'folder not text'),
            ({'note': 'x' * 2001}, 'long note'),
            ({'database': 'x' * 256}, 'long database'),
            ({'sql': ''}, 'empty sql'),
            ({'sql': '   '}, 'blank sql'),
            ({'sql': None}, 'missing sql'),
            ({'sql': 'x' * (ed.SAVED_QUERY_SQL_CAP + 1)}, 'sql over the cap'),
        )
        for overrides, label in bad:
            with self.subTest(label=label):
                fields = {'name': 'n', 'folder': '', 'sql': 'SELECT 1', 'note': '', 'database': None}
                fields.update(overrides)
                with self.assertRaises(ValueError):
                    ed.saved_query_entry(fields['name'], fields['folder'], fields['sql'],
                                         fields['note'], fields['database'])
        with self.assertRaises(ValueError) as caught:
            ed.saved_query_entry('n', '', 'x' * (ed.SAVED_QUERY_SQL_CAP + 1), '', None)
        self.assertIn('{:,}'.format(ed.SAVED_QUERY_SQL_CAP), str(caught.exception))
        # The SQL cap is the history's; exactly at the cap is accepted.
        self.assertEqual(ed.SAVED_QUERY_SQL_CAP, ed.HISTORY_SQL_CAP)
        ed.saved_query_entry('n', '', 'x' * ed.SAVED_QUERY_SQL_CAP, '', None)

    def test_names_and_folders_are_one_line(self):
        entry = ed.saved_query_entry('  Daily\n  check ', ' ops\tteam ', 'SELECT 1', 'line one\nline two', '')
        self.assertEqual(entry['name'], 'Daily check')
        self.assertEqual(entry['folder'], 'ops team')
        self.assertEqual(entry['note'], 'line one\nline two')
        self.assertIsNone(entry['database'])
        self.assertEqual(ed.saved_query_entry('n', None, 'SELECT 1', None, None)['folder'], '')

    def test_full_store_refuses_instead_of_dropping(self):
        for i in range(3):
            self.store.saved_query_save(ed.saved_query_entry('q%d' % i, '', 'SELECT %d' % i, '', None), 3,
                                        100000)
        with self.assertRaises(ValueError) as caught:
            self.store.saved_query_save(ed.saved_query_entry('q3', '', 'SELECT 3', '', None), 3, 100000)
        self.assertIn('at most 3 saved queries', str(caught.exception))
        self.assertEqual(sorted(i['name'] for i in self.store.saved_query_list()), ['q0', 'q1', 'q2'])

    def test_byte_bound_refuses(self):
        first = ed.saved_query_entry('big', '', 'SELECT ' + 'x' * 900, '', None)
        self.store.saved_query_save(first, 10, 2000)
        with self.assertRaises(ValueError) as caught:
            self.store.saved_query_save(ed.saved_query_entry('more', '', 'SELECT ' + 'y' * 900, '', None),
                                        10, 2000)
        self.assertIn('Delete some', str(caught.exception))
        self.assertEqual([i['name'] for i in self.store.saved_query_list()], ['big'])

    def test_rename(self):
        saved = self.save(sql='SELECT 42', folder='ops')
        renamed = self.call('/api/editor/saved/rename', id=saved['id'], name=' New\nname ')['saved']
        self.assertEqual(renamed['name'], 'New name')
        self.assertEqual(renamed['sql'], 'SELECT 42')
        self.assertEqual(renamed['folder'], 'ops')
        self.assertEqual(renamed['created'], saved['created'])
        self.assertGreaterEqual(renamed['updated'], saved['updated'])
        self.assertEqual(self.store.saved_query_list()[0]['name'], 'New name')
        hidden = self.call('/api/editor/saved/rename', id=saved['id'], name="IDENTIFIED BY 'hunter2'")['saved']
        self.assertNotIn('hunter2', hidden['name'])
        for bad_id in ('0' * 32, 'not-an-id', None, 5):
            with self.subTest(bad_id=bad_id), self.assertRaises(ValueError):
                self.call('/api/editor/saved/rename', id=bad_id, name='x')
        with self.assertRaises(ValueError):
            self.call('/api/editor/saved/rename', id=saved['id'], name='')

    def test_move(self):
        saved = self.save(folder='')
        moved = self.call('/api/editor/saved/move', id=saved['id'], folder=' archive ')['saved']
        self.assertEqual(moved['folder'], 'archive')
        self.assertEqual(self.store.saved_query_list()[0]['folder'], 'archive')
        # No folder again: an empty name or null.
        self.assertEqual(self.call('/api/editor/saved/move', id=saved['id'], folder=None)['saved']['folder'], '')
        with self.assertRaises(ValueError):
            self.call('/api/editor/saved/move', id='0' * 32, folder='x')
        with self.assertRaises(ValueError):
            self.call('/api/editor/saved/move', id=saved['id'], folder='x' * 201)

    def test_delete(self):
        saved = self.save()
        other = self.save(name='Other')
        self.assertEqual(self.call('/api/editor/saved/delete', id=saved['id']), {'deleted': 1})
        self.assertEqual([i['id'] for i in self.store.saved_query_list()], [other['id']])
        self.assertEqual(self.store.saved_query_delete(saved['id']), 0)
        with self.assertRaises(ValueError):
            self.call('/api/editor/saved/delete', id=saved['id'])
        with self.assertRaises(ValueError):
            self.store.saved_query_delete('not hex at all')

    def test_history_is_separate(self):
        self.save()
        self.assertEqual(self.store.history_list(), [])
        self.assertEqual(self.store.history_clear(), 0)
        self.assertEqual(len(self.store.saved_query_list()), 1)

    def test_api_dispatch(self):
        saved = self.call('/api/editor/saved/save', name='One', folder='f', sql='SELECT 1',
                          note='n', database='db')['saved']
        self.assertRegex(saved['id'], '^[0-9a-f]{32}$')
        listed = self.call('/api/editor/saved/list')
        self.assertEqual([i['id'] for i in listed['items']], [saved['id']])
        self.assertEqual(listed['max_entries'], ed.SAVED_QUERY_MAX_ENTRIES)
        self.assertEqual(listed['max_bytes'], ed.SAVED_QUERY_MAX_BYTES)
        self.assertEqual(listed['max_sql'], ed.SAVED_QUERY_SQL_CAP)
        with self.assertRaises(ValueError):
            self.call('/api/editor/saved/save', name='', sql='SELECT 1')
        with self.assertRaises(ValueError):
            self.call('/api/editor/saved/list', limit=0)

    def test_records_match_the_published_schema(self):
        saved = self.call('/api/editor/saved/save', name='Daily', folder='ops', sql="SELECT 1 -- IDENTIFIED BY 'x'",
                          note='n', database='shop')['saved']
        validate(saved, SCHEMA)
        validate(self.call('/api/editor/saved/save', name='Plain', sql='SELECT 2')['saved'], SCHEMA)
        for item in self.call('/api/editor/saved/list')['items']:
            validate(item, SCHEMA)
        validate(self.call('/api/editor/saved/rename', id=saved['id'], name='Renamed')['saved'], SCHEMA)
        validate(self.call('/api/editor/saved/move', id=saved['id'], folder='')['saved'], SCHEMA)
        with self.assertRaises(AssertionError):
            validate(dict(saved, extra=1), SCHEMA)

    def test_list_without_a_store_file(self):
        self.assertFalse(self.path.exists())
        self.assertEqual(self.call('/api/editor/saved/list')['items'], [])

    def test_endpoints_are_editor_paths(self):
        for name in ('list', 'save', 'rename', 'move', 'delete'):
            path = '/api/editor/saved/' + name
            self.assertIn(path, ed.PATHS)
            self.assertNotIn(path, ed.WRITE_PATHS)
            self.assertNotIn(path, ed.CONNECTION_PATHS)


class ListParameterTests(unittest.TestCase):
    """The parameter form writes a collapsed IN (...) list as a JSON array."""

    def test_bind_query_list_parameters(self):
        bound = bind_query('SELECT * FROM t WHERE id IN (:p)', {'p': [1, 2, 3]})
        self.assertEqual(bound, 'SELECT * FROM t WHERE id IN (1, 2, 3)')
        bound = bind_query('SELECT * FROM t WHERE name IN (:p)', {'p': ['a', 'b']})
        self.assertEqual(bound, "SELECT * FROM t WHERE name IN (CONVERT(X'61' USING utf8mb4), "
                                "CONVERT(X'62' USING utf8mb4))")
        bound = bind_query('SELECT * FROM t WHERE id IN (:p)', {'p': [None, True, 2.5]})
        self.assertEqual(bound, 'SELECT * FROM t WHERE id IN (NULL, 1, 2.5)')
        # MySQL's "?, ..." form: the marker stands for the whole list.
        self.assertEqual(bind_query('SELECT * FROM t WHERE id IN (:p)', {'p': [7]}),
                         'SELECT * FROM t WHERE id IN (7)')
        for value, label in (([], 'empty'), (list(range(1001)), 'over 1000'),
                             ([{'a': 1}], 'object element'), ([[1]], 'nested list'),
                             ([float('nan')], 'not finite')):
            with self.subTest(label=label):
                with self.assertRaises(ValueError) as caught:
                    bind_query('SELECT * FROM t WHERE id IN (:p)', {'p': value})
                self.assertIn('p', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
