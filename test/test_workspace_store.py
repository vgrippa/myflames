"""Local persistence remains explicit, bounded, and credential-free."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from myflames.workspace_store import WorkspaceStore, validate_bundle

PLAN = (Path(__file__).parent / 'mysql-explain-json-sample.json').read_text()


def bundle():
    return {'schema_version': 'investigation-1.0', 'name': 'Index experiment',
            'notes': 'Compare with warm cache', 'tags': ['checkout'], 'baseline_id': 'base',
            'plans': [{'id': 'base', 'name': 'Original', 'raw': PLAN}]}


class TestWorkspaceStore(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'private' / 'workspace.sqlite3'
        self.store = WorkspaceStore(self.path)

    def test_read_and_delete_do_not_create_storage(self):
        self.assertEqual(self.store.list('investigation'), [])
        self.assertEqual(self.store.list('profile'), [])
        self.store.delete('investigation', 'missing')
        self.assertFalse(self.path.parent.exists())

    def test_save_update_reopen_and_delete(self):
        first = self.store.save('investigation', bundle())
        self.assertTrue(first['id'])
        self.assertTrue(first['updated_at'])
        changed = bundle()
        changed['notes'] = 'New measurement'
        second = self.store.save('investigation', changed, first['id'])
        rows = WorkspaceStore(self.path).list('investigation')
        self.assertEqual(rows, [second])
        self.assertEqual(rows[0]['notes'], 'New measurement')
        if os.name == 'posix':
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.store.delete('investigation', first['id'])
        self.assertEqual(self.store.list('investigation'), [])

    def test_record_kind_isolation(self):
        saved = self.store.save('investigation', bundle(), "id'; DROP TABLE records; --")
        self.store.delete('profile', saved['id'])
        self.assertEqual(len(self.store.list('investigation')), 1)
        self.assertEqual(self.store.list('profile'), [])
        with self.assertRaisesRegex(ValueError, 'type'):
            self.store.save('profile', {'host': 'localhost'}, saved['id'])
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            self.store.save('unknown', {})

    def test_connections_close_after_read_write_and_delete(self):
        connections = []
        connect = sqlite3.connect
        def tracking_connect(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connections.append(connection)
            return connection
        with patch('myflames.workspace_store.sqlite3.connect', side_effect=tracking_connect):
            saved = self.store.save('investigation', bundle())
            self.store.list('investigation')
            self.store.delete('investigation', saved['id'])
        self.assertEqual(len(connections), 3)
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute('SELECT 1')

    def test_profiles_normalize_and_never_persist_password(self):
        saved = self.store.save('profile', {'name': 'Local', 'host': 'localhost',
            'port': '3307', 'user': 'reader', 'password': 'secret',
            'ssl_mode': 'REQUIRED', 'arbitrary': {'password': 'secret'}})
        self.assertEqual(saved['port'], 3307)
        self.assertEqual(saved['ssl_mode'], 'REQUIRED')
        self.assertNotIn('password', saved)
        self.assertNotIn('arbitrary', saved)
        self.assertNotIn(b'secret', self.path.read_bytes())
        self.assertEqual(self.store.list('profile'), [saved])
        default = self.store.save('profile', {})
        self.assertEqual(default['host'], '127.0.0.1')
        self.assertEqual(default['port'], 3306)

    def test_bad_profile_does_not_create_database(self):
        for value in ({'port': 70000}, {'host': 'local\npassword=evil'},
                      {'ssl_mode': 'anything'}, {'name': {'password': 'secret'}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.save('profile', value)
        self.assertFalse(self.path.exists())

    def test_bundle_validates_without_mutating_and_excludes_request_credentials(self):
        value = bundle()
        value['password'] = 'secret'
        value['connection'] = {'password': 'secret'}
        value['plans'][0]['password'] = 'secret'
        value['plans'][0]['capture'] = {'mode': 'analyze', 'engine': 'mysql',
            'conditions': 'Warm cache', 'password': 'secret', 'connection': {'password': 'secret'}}
        original = copy.deepcopy(value)
        result = validate_bundle(value)
        self.assertEqual(value, original)
        self.assertNotIn('secret', json.dumps(result))
        capture = result['plans'][0]['capture']
        self.assertEqual(capture['mode'], 'analyze')
        self.assertEqual(capture['engine'], 'mysql')
        self.assertEqual(capture['conditions'], 'Warm cache')
        self.assertEqual(capture['runs'], [])
        self.assertEqual(capture['warnings'], [])
        self.assertEqual(capture['measurements']['count'], 0)
        self.assertEqual(result['baseline_id'], 'base')

    def test_malformed_bundles_and_dangling_baseline_rejected(self):
        values = [None, [], {}, {'schema_version': 'future', 'plans': []}]
        for updates in ({'plans': None}, {'plans': [{}]}, {'tags': None}, {'tags': 'one'},
                        {'tags': [1]}, {'baseline_id': 'missing'}, {'plans': bundle()['plans'] * 31}):
            value = bundle()
            value.update(updates)
            values.append(value)
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_bundle(value)

    def test_plan_ids_remain_unique_and_are_never_truncated(self):
        for plan_id in ('', 5, 'x' * 201):
            value = bundle()
            value['plans'][0]['id'] = plan_id
            with self.subTest(plan_id=plan_id), self.assertRaises(ValueError):
                validate_bundle(value)
        value = bundle()
        value['plans'].append(dict(value['plans'][0]))
        with self.assertRaises(ValueError):
            validate_bundle(value)

    def test_raw_plan_validation_reused(self):
        for raw in ('{}', '[]', 'not json', '{"operation":"Scan","actual_rows":NaN}'):
            value = bundle()
            value['plans'][0]['raw'] = raw
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                validate_bundle(value)

    def test_bundle_size_and_nonfinite_capture_are_bounded(self):
        value = bundle()
        raw = json.loads(PLAN)
        raw['metadata_padding'] = 'x' * 400000
        value['plans'] = [{'id': str(i), 'raw': json.dumps(raw)} for i in range(30)]
        value['baseline_id'] = '0'
        with self.assertRaisesRegex(ValueError, '10 MiB'):
            validate_bundle(value)
        value = bundle()
        value['plans'][0]['capture'] = {'runs': [{'total_time_ms': float('nan')}]}
        with self.assertRaises(ValueError):
            validate_bundle(value)

    def test_saved_capture_defaults_survive_round_trip(self):
        value = bundle()
        value['plans'][0]['capture'] = {'mode': 'analyze'}
        saved = self.store.save('investigation', value)
        loaded = self.store.list('investigation')[0]
        self.assertEqual(saved, loaded)
        self.assertEqual(loaded['plans'][0]['capture']['runs'], [])
        self.assertEqual(loaded['plans'][0]['capture']['measurements']['count'], 0)

    def test_bad_capture_is_rejected_before_creating_storage(self):
        for capture in ([], {'runs': 'wrong'}, {'runs': [{'total_time_ms': float('inf')}]},
                        {'runs': [{'plan': '{}'}]}, {'warnings': [{}]}):
            value = bundle()
            value['plans'][0]['capture'] = capture
            with self.subTest(capture=capture), self.assertRaises(ValueError):
                self.store.save('investigation', value)
        self.assertFalse(self.path.exists())
