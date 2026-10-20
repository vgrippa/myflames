"""investigations/list returns bounded summary pages; investigations/get one bundle."""
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from myflames import workspace_store
from myflames.ui import api_response
from myflames.workspace_store import WorkspaceStore

TEST = Path(__file__).resolve().parent


class InvestigationSummaries(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env = patch.dict(os.environ, {'HOME': self.directory.name, 'USERPROFILE': self.directory.name,
                                     'MYFLAMES_SECRET_BACKEND': 'vault'})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        self.store = WorkspaceStore(Path(self.directory.name) / 'private' / 'workspace.sqlite3')
        self.workspace = SimpleNamespace(store=self.store)

    def save(self, key, engine='mysql', padding=0, **fields):
        # Summaries never depend on the plan's engine; both are real parser inputs.
        name = 'mysql-explain-json-sample.json' if engine == 'mysql' else 'mariadb-explain-block-nl-join.json'
        raw = (TEST / name).read_text()
        if padding:
            raw = json.dumps(dict(json.loads(raw), padding='x' * padding))
        value = {'schema_version': 'investigation-1.0', 'name': 'Experiment ' + key,
                 'notes': 'Note ' + key, 'tags': ['café'], 'baseline_id': 'plan',
                 'plans': [{'id': 'plan', 'name': 'Original', 'raw': raw, 'capture': {'mode': 'explain'}},
                           {'id': 'second', 'name': 'Second', 'raw': raw}]}
        value.update(fields)
        return self.store.save('investigation', value, key)

    def records(self, sql, args=()):
        with contextlib.closing(sqlite3.connect(str(self.store.path))) as conn, conn:
            return conn.execute(sql, args).fetchall()

    def test_list_pages_summaries_without_reading_bundles_for_both_engines(self):
        """The reproduction: Home received every complete bundle (O(saved bytes))."""
        saved = [self.save('%02d' % i, 'mysql' if i % 2 else 'mariadb', padding=200000) for i in range(12)]
        decoded = []
        loads = json.loads

        def count(value, *args, **kwargs):
            decoded.append(value)
            return loads(value, *args, **kwargs)

        with patch.object(workspace_store.json, 'loads', side_effect=count):
            page = api_response('/api/investigations/list', {'limit': 6}, self.workspace)
        self.assertEqual(page['schema_version'], 'investigation-list-1.0')
        self.assertEqual([item['id'] for item in page['items']], ['11', '10', '09', '08', '07', '06'])
        self.assertEqual(len(decoded), 6, 'one summary decoded per row, never a bundle')
        self.assertTrue(all('"raw"' not in value for value in decoded))
        encoded = json.dumps(page)
        self.assertNotIn('"raw"', encoded)
        self.assertLess(len(encoded), 4096, 'a page of six summaries, not 12 x 200 KB of plans')
        self.assertEqual(page['next_cursor'], {'updated_at': page['items'][-1]['updated_at'], 'id': '06'})
        summary = page['items'][0]
        self.assertEqual(summary, {
            'id': '11', 'updated_at': saved[11]['updated_at'], 'schema_version': 'investigation-summary-1.0',
            'name': 'Experiment 11', 'notes': 'Note 11', 'tags': ['café'], 'baseline_id': 'plan',
            'plans': [{'id': 'plan', 'name': 'Original', 'capture': {'mode': 'explain'}},
                      {'id': 'second', 'name': 'Second'}]})
        # The complete bundle, exactly as list returned it before (Export writes it).
        for original in saved:
            self.assertEqual(api_response('/api/investigations/get', {'id': original['id']}, self.workspace),
                             original)
        self.assertEqual(api_response('/api/investigations/list', {}, self.workspace)['items'][0]['id'], '11')

    def test_keyset_pagination_ties_deletes_and_search(self):
        with patch.object(workspace_store, '_now', return_value='2026-10-19T00:00:00+00:00'):
            for i in range(7):
                self.save(str(i))
        page = self.store.list_investigations(limit=3)
        self.assertEqual([r['id'] for r in page['items']], ['6', '5', '4'])
        self.store.delete('investigation', '5')
        next_page = self.store.list_investigations(limit=3, cursor=page['next_cursor'])
        self.assertEqual([r['id'] for r in next_page['items']], ['3', '2', '1'])
        last = self.store.list_investigations(limit=3, cursor=next_page['next_cursor'])
        self.assertEqual([r['id'] for r in last['items']], ['0'])
        self.assertIsNone(last['next_cursor'])
        # Library's filter: name, tags and notes, ignoring case; never a pattern.
        self.assertEqual([r['id'] for r in self.store.list_investigations(search='  NOTE 0 ')['items']], ['0'])
        self.assertEqual(len(self.store.list_investigations(search='CAFÉ')['items']), 6)
        self.assertEqual(len(self.store.list_investigations(search='experiment')['items']), 6)
        self.assertEqual(self.store.list_investigations(search='%')['items'], [])
        self.assertEqual(self.store.list_investigations(search='Original')['items'], [], 'plan names are not searched')

    def test_records_written_by_an_older_version_are_listed(self):
        """An older myflames on the same file writes records only; the cache follows."""
        for key in ('kept', 'changed', 'deleted'):
            self.save(key)
        older = json.loads(self.records("SELECT payload FROM records WHERE id = 'kept'")[0][0])
        older['name'] = 'Renamed by an older version'
        self.records("UPDATE records SET payload = ?, updated = '2030-01-01T00:00:00+00:00' WHERE id = 'changed'",
                     (json.dumps(older),))
        self.records("DELETE FROM records WHERE id = 'deleted'")
        older['name'] = 'Saved by an older version'
        self.records("INSERT INTO records VALUES ('new', 'investigation', '2031-01-01T00:00:00+00:00', ?)",
                     (json.dumps(older),))
        self.records("INSERT INTO records VALUES ('broken', 'investigation', '2032-01-01T00:00:00+00:00', 'not json')")
        page = self.store.list_investigations()
        self.assertEqual([(r['id'], r['name']) for r in page['items']], [
            ('new', 'Saved by an older version'), ('changed', 'Renamed by an older version'),
            ('kept', 'Experiment kept')])
        self.assertEqual(self.records('PRAGMA user_version')[0][0], workspace_store.STORE_VERSION)
        # Up to date now: the next list reads summaries only, the broken record included.
        with patch.object(workspace_store.json, 'loads', side_effect=json.loads) as loads:
            self.assertEqual(self.store.list_investigations(), page)
        self.assertEqual(loads.call_count, 3)
        self.assertIsNone(self.store.get('investigation', 'deleted'))

    def test_save_and_delete_keep_summaries_in_one_transaction(self):
        saved = self.save('one')
        saved['name'] = 'Changed'
        self.store.save('investigation', saved, saved['id'])
        self.assertEqual(self.store.list_investigations()['items'][0]['name'], 'Changed')
        self.store.delete('profile', saved['id'])
        self.assertEqual(len(self.store.list_investigations()['items']), 1)
        self.store.delete('investigation', saved['id'])
        self.assertEqual(self.records('SELECT COUNT(*) FROM investigation_summaries')[0][0], 0)
        self.assertEqual(self.store.list_investigations()['items'], [])

    def test_invalid_requests_do_not_create_storage(self):
        for kwargs in ({'limit': 0}, {'limit': 51}, {'limit': True}, {'limit': '6'},
                       {'cursor': []}, {'cursor': {'id': 'x'}}, {'cursor': {'id': 'x', 'updated_at': 1}},
                       {'search': []}, {'search': 'x' * 513}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.list_investigations(**kwargs)
        self.assertFalse(self.store.path.exists())
        self.assertEqual(api_response('/api/investigations/list', {}, self.workspace),
                         {'schema_version': 'investigation-list-1.0', 'items': [], 'next_cursor': None})
        with self.assertRaisesRegex(ValueError, 'no longer exists'):
            api_response('/api/investigations/get', {'id': 'missing'}, self.workspace)
        with self.assertRaisesRegex(ValueError, 'no longer exists'):
            api_response('/api/investigations/get', {'id': ['x']}, self.workspace)
        self.assertFalse(self.store.path.exists())


if __name__ == '__main__':
    unittest.main()
