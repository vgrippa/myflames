"""Local persistence remains explicit, bounded, and credential-free."""
import copy
import json
import os
from pathlib import Path
import contextlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from myflames import workspace_store
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
        patcher = patch.dict(os.environ, {'HOME': self.temp.name, 'USERPROFILE': self.temp.name,
                                         'MYFLAMES_SECRET_BACKEND': 'vault'})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        self.path = Path(self.temp.name) / 'private' / 'workspace.sqlite3'
        self.store = WorkspaceStore(self.path)

    def test_app_update_last_check_survives_a_restart(self):
        self.assertIsNone(self.store.app_update_last_check())
        self.assertFalse(self.path.exists(), 'reading the last check never creates the store')
        record = {'checked_at': '2026-10-20T10:42:00Z', 'outcome': 'up_to_date', 'current_version': '3.1.0',
                  'latest_version': '3.1.0', 'reason': None}
        self.assertEqual(self.store.set_app_update_last_check(record), record)
        self.assertEqual(WorkspaceStore(self.path).app_update_last_check(), record)
        newer = dict(record, checked_at='2026-10-21T08:00:00Z', outcome='failed', reason='offline')
        self.store.set_app_update_last_check(newer)
        self.assertEqual(WorkspaceStore(self.path).app_update_last_check(), newer)
        for bad in (None, [], {'outcome': 'up_to_date'}, dict(record, reason='x' * 5000)):
            with self.subTest(bad=type(bad)), self.assertRaises(ValueError):
                self.store.set_app_update_last_check(bad)
        self.assertEqual(self.store.app_update_last_check(), newer)
        # A damaged value reads as never checked, not as an error.
        # closing(): sqlite3's context manager commits but leaves the file open,
        # and Windows cannot remove an open file when the temporary folder goes.
        with contextlib.closing(sqlite3.connect(str(self.path))) as conn, conn:
            conn.execute("UPDATE settings SET value = 'not json' WHERE key = 'app_update_last_check'")
        self.assertIsNone(self.store.app_update_last_check())

    def test_write_lock_timeout_closes_the_connection(self):
        # BEGIN IMMEDIATE raising (a busy timeout under write contention) must
        # not leak the connection, which no caller owns yet.
        self.store.save('investigation', bundle())
        opened = []
        real_connect = self.store._connect

        class Busy:
            def __init__(self, conn):
                self.conn, self.closed = conn, False
                self.isolation_level = None

            def execute(self, sql, *args):
                if sql == 'BEGIN IMMEDIATE':
                    raise sqlite3.OperationalError('database is locked')
                return self.conn.execute(sql, *args)

            def close(self):
                self.closed = True
                self.conn.close()

        def connect():
            wrapped = Busy(real_connect())
            opened.append(wrapped)
            return wrapped

        with patch.object(self.store, '_connect', connect):
            with self.assertRaises(sqlite3.OperationalError):
                self.store._transaction()
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].closed)

    @unittest.skipUnless(os.name == 'posix', 'POSIX storage safety')
    def test_security_symlinks_are_rejected_without_touching_targets(self):
        for suffix in ('directory', '', '-wal', '-shm', '-journal'):
            with self.subTest(suffix=suffix):
                folder = Path(self.temp.name) / ('case' + (suffix or 'db'))
                folder.mkdir()
                path = folder / 'workspace.sqlite3'
                target = Path(self.temp.name) / ('target' + (suffix or 'db'))
                if suffix == 'directory':
                    target.mkdir(mode=0o755)
                    folder.rmdir()
                    folder.symlink_to(target, target_is_directory=True)
                else:
                    target.write_bytes(b'do not change')
                    target.chmod(0o644)
                    Path(str(path) + suffix).symlink_to(target)
                before = target.stat().st_mode
                with patch.object(workspace_store.sqlite3, 'connect') as connect:
                    with self.assertRaises(ValueError):
                        WorkspaceStore(path)._connect()
                    connect.assert_not_called()
                self.assertEqual(target.stat().st_mode, before)
                if suffix != 'directory':
                    self.assertEqual(target.read_bytes(), b'do not change')

    @unittest.skipUnless(os.name == 'posix', 'POSIX storage ownership')
    def test_security_foreign_owner_is_rejected_before_chmod_or_sqlite(self):
        self.store._connect().close()
        real_lstat = os.lstat
        targets = [self.path.parent, self.path] + [Path(str(self.path) + suffix)
                                                  for suffix in ('-wal', '-shm', '-journal')]
        for target in targets[2:]:
            target.write_bytes(b'')
        for target in targets:
            def foreign(path, *args, **kwargs):
                result = real_lstat(path, *args, **kwargs)
                if str(path) == str(target):
                    fields = list(result)
                    fields[4] = os.geteuid() + 1
                    return os.stat_result(fields)
                return result
            with self.subTest(target=target), patch.object(os, 'lstat', side_effect=foreign), \
                    patch.object(workspace_store.sqlite3, 'connect') as connect:
                with self.assertRaisesRegex(ValueError, 'owner|owned'):
                    self.store._connect()
                connect.assert_not_called()

    @unittest.skipUnless(os.name == 'posix', 'POSIX open flags')
    def test_security_database_creation_is_exclusive_and_does_not_follow_links(self):
        real_open = os.open
        with patch.object(workspace_store.os, 'open', wraps=real_open) as opened:
            self.store._connect().close()
        flags = [call[0][1] for call in opened.call_args_list if str(call[0][0]) == str(self.path)]
        self.assertTrue(any(flag & os.O_EXCL and flag & os.O_CREAT for flag in flags))
        self.assertTrue(all(flag & os.O_NOFOLLOW for flag in flags))
        with patch.object(workspace_store.os, 'open', wraps=real_open) as opened:
            self.store._connect().close()
        flags = [call[0][1] for call in opened.call_args_list if str(call[0][0]) == str(self.path)]
        self.assertTrue(any(not flag & os.O_CREAT for flag in flags))
        self.assertTrue(all(flag & os.O_NOFOLLOW for flag in flags))

    def test_security_sqlite_setup_failures_close_the_connection(self):
        from unittest.mock import MagicMock
        for fail_at in range(1, 8):
            conn = MagicMock()
            calls = [0]
            def step(*args):
                calls[0] += 1
                if calls[0] == fail_at:
                    raise sqlite3.OperationalError('setup failed')
                return MagicMock(fetchone=lambda: (1,))
            conn.execute.side_effect = step
            conn.commit.side_effect = step
            with self.subTest(fail_at=fail_at), patch.object(workspace_store.sqlite3, 'connect', return_value=conn):
                with self.assertRaisesRegex(sqlite3.OperationalError, 'setup failed'):
                    self.store._connect()
                conn.close.assert_called_once()

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

    @unittest.skipUnless(os.name == 'posix', 'POSIX mode bits')
    def test_every_open_protects_existing_default_and_custom_storage(self):
        with patch.dict(os.environ, {'HOME': self.temp.name, 'USERPROFILE': self.temp.name,
                                     'MYFLAMES_SECRET_BACKEND': 'vault'}):
            for store in (WorkspaceStore(), self.store):
                with self.subTest(path=str(store.path)):
                    store.save('investigation', bundle())
                    for _ in range(2):
                        os.chmod(str(store.path.parent), 0o755)
                        os.chmod(str(store.path), 0o666)
                        store.list('investigation')
                        self.assertEqual(store.path.parent.stat().st_mode & 0o777, 0o700)
                        self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(os.name == 'posix', 'POSIX mode bits')
    def test_sqlite_sidecars_are_protected_before_open(self):
        with patch.dict(os.environ, {'HOME': self.temp.name, 'USERPROFILE': self.temp.name,
                                     'MYFLAMES_SECRET_BACKEND': 'vault'}):
            self.store.save('investigation', bundle())
            paths = [Path(str(self.path) + suffix) for suffix in ('-wal', '-shm', '-journal')]
            for path in paths:
                path.write_bytes(b'')
                os.chmod(str(path), 0o666)
            connect = sqlite3.connect

            def checked_connect(*args, **kwargs):
                for path in paths:
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                return connect(*args, **kwargs)
            with patch.object(workspace_store.sqlite3, 'connect', side_effect=checked_connect):
                self.store.list('investigation')
            for path in paths:
                if path.exists():
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(os.name == 'posix', 'POSIX mode bits')
    def test_live_wal_and_shm_are_protected_without_vault_access(self):
        self.store.save('investigation', bundle())
        conn = sqlite3.connect(str(self.path))
        self.addCleanup(conn.close)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute("INSERT INTO settings VALUES ('wal-test', '1')")
        conn.commit()
        paths = [Path(str(self.path) + suffix) for suffix in ('-wal', '-shm')]
        for path in paths:
            self.assertTrue(path.exists())
            os.chmod(str(path), 0o666)
        self.store.list('investigation')
        for path in paths:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(os.name == 'posix', 'POSIX mode bits')
    def test_permission_failure_prevents_opening_database(self):
        self.store.save('investigation', bundle())
        os.chmod(str(self.path.parent), 0o755)
        with patch.object(workspace_store.os, 'fchmod', side_effect=PermissionError('cannot protect')), \
                patch.object(workspace_store.sqlite3, 'connect') as connect:
            with self.assertRaisesRegex(PermissionError, 'cannot protect'):
                self.store.list('investigation')
        connect.assert_not_called()

    def test_windows_permission_protection_is_a_noop(self):
        with patch.object(workspace_store.os, 'name', 'nt'), patch.object(workspace_store.os, 'chmod') as chmod, \
                patch.object(workspace_store.os, 'stat', side_effect=AssertionError('permission check on Windows')):
            workspace_store._owner_only(self.path, 0o600)
        chmod.assert_not_called()

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
        # The value must not collide with field names such as secret_backend.
        saved = self.store.save('profile', {'name': 'Local', 'host': 'localhost',
            'port': '3307', 'user': 'reader', 'password': 'hunter2-for-test',
            'ssl_mode': 'REQUIRED', 'arbitrary': {'password': 'hunter2-for-test'}})
        self.assertEqual(saved['port'], 3307)
        self.assertEqual(saved['ssl_mode'], 'REQUIRED')
        self.assertNotIn('password', saved)
        self.assertNotIn('arbitrary', saved)
        self.assertNotIn(b'hunter2-for-test', self.path.read_bytes())
        self.assertFalse(saved['has_saved_password'])
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
