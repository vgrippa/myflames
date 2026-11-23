"""Local persistence remains explicit, bounded, and credential-free."""
import copy
import json
import os
import stat
import errno
from pathlib import Path
import contextlib
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from myflames import secret_store, workspace_store
from myflames.workspace_store import WorkspaceStore, validate_bundle

PLAN = (Path(__file__).parent / 'mysql-explain-json-sample.json').read_text(encoding="utf-8")


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
        secret_store._reset_windows_acl_cache()
        self.addCleanup(secret_store._reset_windows_acl_cache)

    def test_windows_concurrent_connections_apply_each_acl_once(self):
        # Set up SQLite before the race, then model a fresh backend process.
        # Only the Win32 ACL call is mocked; every connection is real.
        self.store._connect().close()
        secret_store._reset_windows_acl_cache()
        barrier = threading.Barrier(20, timeout=10)
        results, errors = [], []
        result_lock = threading.Lock()

        def connect():
            try:
                barrier.wait()
                with contextlib.closing(WorkspaceStore(self.path)._connect()) as conn:
                    value = conn.execute('SELECT 1').fetchone()[0]
                with result_lock:
                    results.append(value)
            except BaseException as exc:
                with result_lock:
                    errors.append(exc)

        def protect(path, directory=False):
            # A slow ACL call makes simultaneous cache misses overlap reliably.
            time.sleep(0.01)

        with patch.object(sys, 'platform', 'win32'), \
                patch.object(secret_store, '_windows_owner_only', side_effect=protect) as acl:
            threads = [threading.Thread(target=connect) for _ in range(20)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(20)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(results, [1] * 20)
            calls = [(Path(call[0][0]), bool(call[1].get('directory'))) for call in acl.call_args_list]
            self.assertCountEqual(calls, [(self.path.parent, True), (self.path, False)])
            # A warm connection must not write either ACL again.
            self.store._connect().close()
            self.assertEqual(acl.call_count, 2)

    def test_windows_replaced_database_gets_its_own_acl(self):
        self.store._connect().close()
        secret_store._reset_windows_acl_cache()
        with patch.object(sys, 'platform', 'win32'), \
                patch.object(secret_store, '_windows_owner_only') as acl:
            self.store._connect().close()
            self.store._connect().close()
            self.assertEqual(acl.call_count, 2)
            replacement = self.path.with_name('replacement.sqlite3')
            replacement.write_bytes(self.path.read_bytes())
            before, after = self.path.stat(), replacement.stat()
            self.assertNotEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
            replacement.replace(self.path)
            self.store._connect().close()
            self.store._connect().close()
            calls = [(Path(call[0][0]), bool(call[1].get('directory'))) for call in acl.call_args_list]
            self.assertEqual(calls, [(self.path.parent, True), (self.path, False), (self.path, False)])

    def test_windows_failed_acl_is_retried_before_sqlite_connect(self):
        self.store._connect().close()
        secret_store._reset_windows_acl_cache()
        fail = [True]

        def protect(path, directory=False):
            if Path(path) == self.path and fail[0]:
                fail[0] = False
                raise secret_store.SecretStoreError('Cannot enforce owner-only Windows permissions')

        with patch.object(sys, 'platform', 'win32'), \
                patch.object(secret_store, '_windows_owner_only', side_effect=protect) as acl, \
                patch.object(workspace_store.sqlite3, 'connect', wraps=sqlite3.connect) as connect:
            with self.assertRaisesRegex(secret_store.SecretStoreError, 'Cannot enforce owner-only'):
                self.store._connect()
            connect.assert_not_called()
            self.store._connect().close()
            self.store._connect().close()
            self.assertEqual(connect.call_count, 2)
            calls = [(Path(call[0][0]), bool(call[1].get('directory'))) for call in acl.call_args_list]
            self.assertEqual(calls, [(self.path.parent, True), (self.path, False), (self.path, False)])

    def test_windows_cached_acl_still_rejects_links_and_wrong_file_types(self):
        self.store._connect().close()
        secret_store._reset_windows_acl_cache()
        real_lstat = os.lstat
        with patch.object(sys, 'platform', 'win32'), \
                patch.object(secret_store, '_windows_owner_only') as acl:
            self.store._connect().close()
            acl.reset_mock()
            for target in (self.path.parent, self.path):
                for mode in (stat.S_IFLNK, stat.S_IFREG if target == self.path.parent else stat.S_IFDIR):
                    # Keep the identity unchanged, so an early cache return
                    # would incorrectly bypass the refusal. No symlink privilege
                    # is needed to exercise this on the Windows CI runner.
                    def invalid(path, *args, **kwargs):
                        info = real_lstat(path, *args, **kwargs)
                        if str(path) == str(target):
                            fields = list(info)
                            fields[0] = mode | 0o600
                            return os.stat_result(fields)
                        return info

                    with self.subTest(target=target, mode=mode), \
                            patch.object(os, 'lstat', side_effect=invalid), \
                            patch.object(workspace_store.sqlite3, 'connect') as connect:
                        with self.assertRaisesRegex(secret_store.SecretStoreError, 'symbolic link|wrong file type'):
                            self.store._connect()
                        connect.assert_not_called()
                        acl.assert_not_called()

    def test_windows_cached_acl_still_checks_the_opened_database_identity(self):
        self.store._connect().close()
        secret_store._reset_windows_acl_cache()
        identity = self.path.stat()
        real_fstat = os.fstat
        descriptors = []

        def replaced(fd):
            info = real_fstat(fd)
            if (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
                descriptors.append(fd)
                fields = list(info)
                fields[1] += 1
                return os.stat_result(fields)
            return info

        with patch.object(sys, 'platform', 'win32'), \
                patch.object(secret_store, '_windows_owner_only') as acl:
            self.store._connect().close()
            acl.reset_mock()
            with patch.object(os, 'fstat', side_effect=replaced), \
                    patch.object(workspace_store.sqlite3, 'connect') as connect:
                with self.assertRaisesRegex(secret_store.SecretStoreError, 'changed while it was being opened'):
                    self.store._connect()
                connect.assert_not_called()
                acl.assert_not_called()
        self.assertTrue(descriptors)
        for fd in set(descriptors):
            with self.assertRaises(OSError):
                real_fstat(fd)

    @unittest.skipUnless(os.name == 'posix', 'POSIX storage ownership')
    def test_windows_cached_acl_still_checks_posix_ownership(self):
        self.store._connect().close()
        secret_store._reset_windows_acl_cache()
        real_lstat = os.lstat
        with patch.object(sys, 'platform', 'win32'), \
                patch.object(secret_store, '_windows_owner_only') as acl:
            self.store._connect().close()
            acl.reset_mock()
            for target in (self.path.parent, self.path):
                def foreign(path, *args, **kwargs):
                    info = real_lstat(path, *args, **kwargs)
                    if str(path) == str(target):
                        fields = list(info)
                        fields[4] = os.geteuid() + 1
                        return os.stat_result(fields)
                    return info

                with self.subTest(target=target), patch.object(os, 'lstat', side_effect=foreign), \
                        patch.object(workspace_store.sqlite3, 'connect') as connect:
                    with self.assertRaisesRegex(secret_store.SecretStoreError, 'owned'):
                        self.store._connect()
                    connect.assert_not_called()
                    acl.assert_not_called()

    def test_gate65_session_restore_keeps_clauses_and_redacts_restarted_set(self):
        cases = (
            ("UPDATE cfg SET s3_secret_key = 'abc' WHERE id = 1",
             "UPDATE cfg SET s3_secret_key = '[redacted]' WHERE id = 1"),
            ("INSERT INTO cfg SET s3_secret_key = 'abc' ON DUPLICATE KEY UPDATE is_admin = 1",
             "INSERT INTO cfg SET s3_secret_key = '[redacted]' ON DUPLICATE KEY UPDATE is_admin = 1"),
            ("SET GLOBAL max_connections = 500\nSET GLOBAL wsrep_sst_auth = CONCAT('script-secret', 'tail')",
             'SET GLOBAL max_connections = 500\nSET GLOBAL wsrep_sst_auth = [redacted]'),
            ("DELIMITER //\nSET GLOBAL max_connections = 500//\n"
             "SET GLOBAL wsrep_sst_auth = CONCAT('script-secret', 'tail')",
             'DELIMITER //\nSET GLOBAL max_connections = 500//\nSET GLOBAL wsrep_sst_auth = [redacted]'),
        )
        state = {'tabs': [{'sql': sql} for sql, _ in cases]}
        expected = {'tabs': [{'sql': sql} for _, sql in cases]}
        self.assertEqual(workspace_store.validate_session_state(state), expected)
        self.store.save_session(state, 0)
        self.assertEqual(WorkspaceStore(self.path).load_session()['state'], expected)
        # Saving must not alter the editor's original in-memory SQL.
        self.assertEqual(state['tabs'][0]['sql'], cases[0][0])

    def test_gate65_session_restore_redacts_urls_names_echoes_and_comment_values(self):
        cases = (
            ("SELECT 'mysql://reader@h1,mysql://admin:PW@h2'",
             "SELECT 'mysql://reader@h1,mysql://admin:[redacted]@h2'"),
            ("SELECT 'mysql://app:2024/secret@db1:3306/shop'",
             "SELECT 'mysql://app:[redacted]@db1:3306/shop'"),
            ('SET GLOBAL "wsrep_sst_auth" = CONCAT(\'ansi-secret\', \'tail\')',
             'SET GLOBAL "wsrep_sst_auth" = [redacted]'),
            ('EXECUTE IMMEDIATE "SET GLOBAL wsrep_sst_auth = \'echo-secret\'"',
             'EXECUTE IMMEDIATE "SET GLOBAL wsrep_sst_auth = \'[redacted]\'"'),
            ('SET GLOBAL wsrep_sst_auth = /*!80000987654321*/',
             'SET GLOBAL wsrep_sst_auth = /*!80000[redacted]*/'),
            ("SELECT 2 */* it's */ 3; SET GLOBAL wsrep_sst_auth = CONCAT('marker-secret', 'tail')",
             "SELECT 2 */* it's */ 3; SET GLOBAL wsrep_sst_auth = [redacted]"),
        )
        # Every SQL-bearing session key uses the same storage boundary.
        fields = ('sql', 'text', 'query', 'statement', 'sql_text')
        state = {'tabs': [{key: sql for key in fields} for sql, _ in cases]}
        expected = {'tabs': [{key: sql for key in fields} for _, sql in cases]}
        self.assertEqual(workspace_store.validate_session_state(state), expected)
        self.store.save_session(state, 0)
        self.assertEqual(WorkspaceStore(self.path).load_session()['state'], expected)

    def test_topology_observations_keep_start_order_across_store_instances(self):
        profile = self.store.save('profile', {'name': 'db', 'host': 'db.example', 'user': 'monitor'}, 'db')
        older = self.store.begin_topology_observation()
        reopened = WorkspaceStore(self.path)
        newer = reopened.begin_topology_observation()
        reopened.record_topology_identity('db', {'server_uuid': 'new'}, expected=profile, observation=newer)
        before = reopened._profile('db', include_updated=True)
        self.store.record_topology_identity('db', {'server_uuid': 'old'}, expected=profile, observation=older)
        self.assertEqual(self.store._profile('db', include_updated=True), before)
        # Equal observations are idempotent, even if a caller supplies new data.
        self.store.record_topology_identity('db', {'server_uuid': 'replay'}, expected=profile, observation=newer)
        self.assertEqual(self.store._profile('db', include_updated=True), before)
        self.store.record_topology_identity('db', {'server_uuid': 'direct'}, expected=profile)
        self.assertEqual(self.store._profile('db')['topology_identity']['server_uuid'], 'direct')

    def test_topology_observation_rejects_tokens_from_another_store(self):
        self.store.save('profile', {'name': 'db', 'host': 'db.example', 'user': 'monitor'}, 'db')
        other = WorkspaceStore(self.path.parent / 'other.sqlite3')
        observation = other.begin_topology_observation()
        self.store.record_topology_identity('db', {'server_uuid': 'foreign'}, observation=observation)
        self.assertNotIn('topology_identity', self.store._profile('db'))

    def test_newer_reserved_observation_can_finish_after_an_older_probe(self):
        profile = self.store.save('profile', {'name': 'db', 'host': 'db.example', 'user': 'monitor'}, 'db')
        older = workspace_store._topology_probe_observation(profile)
        newer = self.store.begin_topology_observation()
        self.store.record_topology_identity('db', {'server_uuid': 'old', '_topology_observation': older},
                                            expected=profile)
        self.store.record_topology_identity('db', {'server_uuid': 'new'}, expected=profile, observation=newer)
        before = self.store._profile('db', include_updated=True)
        self.assertEqual(before['topology_identity']['server_uuid'], 'new')
        self.store.record_topology_identity('db', {'server_uuid': 'replay', '_topology_observation': older},
                                            expected=profile)
        self.assertEqual(self.store._profile('db', include_updated=True), before)

    def test_probe_rejects_a_changed_snapshot_from_another_process_epoch(self):
        profile = self.store.save('profile', {'name': 'db', 'host': 'db.example', 'user': 'monitor'}, 'db')
        older = workspace_store._topology_probe_observation(profile)
        # A separate workspace process has an unrelated monotonic origin.
        with patch.object(workspace_store, '_TOPOLOGY_EPOCH', 'another-process'):
            reopened = WorkspaceStore(self.path)
            reopened.record_topology_identity('db', {'server_uuid': 'new'}, expected=profile)
        before = self.store._profile('db', include_updated=True)
        self.store.record_topology_identity('db', {'server_uuid': 'old', '_topology_observation': older},
                                            expected=profile)
        self.assertEqual(self.store._profile('db', include_updated=True), before)

    def test_client_choice_read_and_clear_do_not_create_a_database(self):
        self.assertIsNone(self.store.client_choice('mysql'))
        self.assertIsNone(self.store.set_client_choice('mysql', None))
        self.assertFalse(self.path.exists())

    def test_client_choice_round_trip_and_clear(self):
        value = str(Path(self.temp.name) / 'client' / 'mysql')
        self.assertEqual(self.store.set_client_choice('mysql', value), value)
        self.assertEqual(WorkspaceStore(self.path).client_choice('mysql'), value)
        replacement = str(Path(self.temp.name) / 'mariadb')
        self.store.set_client_choice('mysql', replacement)
        self.assertEqual(WorkspaceStore(self.path).client_choice('mysql'), replacement)
        self.store.set_client_choice('mysql', None)
        self.assertIsNone(WorkspaceStore(self.path).client_choice('mysql'))

    def test_client_choice_refuses_invalid_types_without_changing_setting(self):
        self.store.set_client_choice('mysql', '/chosen/mysql')
        for value in ('', 3, True, {}, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.set_client_choice('mysql', value)
        self.assertEqual(self.store.client_choice('mysql'), '/chosen/mysql')

    def test_client_choice_does_not_allow_per_connection_binary_fields(self):
        self.store.set_client_choice('mysql', '/chosen/mysql')
        with self.assertRaises(ValueError):
            self.store.connection_with_saved_password({'host': 'localhost', 'binary': '/other/mysql'})

    def test_client_choices_are_independent_and_bundle_is_preserved(self):
        self.store.set_client_choice('mysql', 'bundled')
        self.store.set_client_choice('mariadb', '/opt/mariadb/mysql')
        self.assertEqual(self.store.client_choice('mysql'), 'bundled')
        self.assertEqual(self.store.client_choice('mariadb'), '/opt/mariadb/mysql')
        self.store.set_client_choice('mysql', None)
        self.assertIsNone(self.store.client_choice('mysql'))
        self.assertEqual(self.store.client_choice('mariadb'), '/opt/mariadb/mysql')
        for engine in ('other', None, ''):
            with self.assertRaises(ValueError):
                self.store.client_choice(engine)
            with self.assertRaises(ValueError):
                self.store.set_client_choice(engine, None)

    def test_migrate_legacy_client_choice_once_including_broken_and_bundled(self):
        from myflames import connector
        cases = [('/opt/mysql', 'mysql Ver 8.4.13', 'mysql', '/opt/mysql'),
                 ('/opt/mysql', 'mysql Distrib 11.4.2-MariaDB', 'mariadb', '/opt/mysql'),
                 ('/bundled/bin/mysql', 'mysql Ver 8.4.13', 'mysql', 'bundled'),
                 ('/gone/mariadb-old', None, 'mariadb', '/gone/mariadb-old'),
                 ('/gone/mysql', None, 'mysql', '/gone/mysql')]
        for i, (value, banner, engine, expected) in enumerate(cases):
            store = WorkspaceStore(Path(self.temp.name) / ('case%d.sqlite3' % i))
            with contextlib.closing(store._connect()) as conn, conn:
                conn.execute("INSERT INTO settings VALUES ('client_binary', ?)", (value,))
            probe = (patch.object(connector, 'probe_client', return_value={'version': banner}) if banner
                     else patch.object(connector, 'probe_client', side_effect=ValueError('unusable')))
            with probe, patch.dict(os.environ, {'MYFLAMES_BUNDLED_CLIENT_DIR': '/bundled/bin'}):
                store.migrate_client_choices()
            self.assertEqual(store.client_choice(engine), expected)
            with contextlib.closing(store._connect()) as conn:
                self.assertIsNone(conn.execute("SELECT value FROM settings WHERE key='client_binary'").fetchone())
            store.set_client_choice(engine, None)
            store.migrate_client_choices()
            self.assertIsNone(store.client_choice(engine))

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

    def test_app_update_auto_defaults_on_without_creating_storage(self):
        self.assertIs(self.store.app_update_auto(), True)
        self.assertFalse(self.path.exists())
        self.assertIs(self.store.set_app_update_auto(False), False)
        self.assertIs(WorkspaceStore(self.path).app_update_auto(), False)
        self.assertIs(self.store.set_app_update_auto(True), True)
        self.assertIs(WorkspaceStore(self.path).app_update_auto(), True)
        with contextlib.closing(sqlite3.connect(str(self.path))) as conn, conn:
            conn.execute("UPDATE settings SET value = 'unexpected' WHERE key = 'app_update_auto'")
        self.assertIs(self.store.app_update_auto(), True)

    def test_app_update_auto_rejects_non_boolean_values(self):
        for invalid in (None, 0, 1, 'on', 'off', [], {}):
            with self.subTest(value=invalid), self.assertRaisesRegex(ValueError, 'true or false'):
                self.store.set_app_update_auto(invalid)
        self.assertFalse(self.path.exists())

    def test_ui_preferences_read_never_creates_storage(self):
        self.assertEqual(self.store.ui_preferences(), workspace_store.UI_PREFERENCE_DEFAULTS)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.parent.exists())
        self.assertEqual(self.store.set_ui_preferences({}), workspace_store.UI_PREFERENCE_DEFAULTS)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.parent.exists())
        self.assertIsNone(self.store.ui_theme_for_index())
        self.assertFalse(self.path.exists())

    def test_ui_preferences_round_trip_and_partial_update(self):
        changes = dict(workspace_store.UI_PREFERENCE_DEFAULTS, theme='one-dark', sidebar_width=296,
                       inspector_width=420, explain_format='json', files_show_hidden=True,
                       files_transfers_open=False, server_nav_collapsed=False,
                       health_auto_production=30, health_auto_development=60)
        self.assertEqual(self.store.set_ui_preferences(changes), changes)
        self.assertEqual(WorkspaceStore(self.path).ui_preferences(), changes)
        self.assertEqual(self.store.set_ui_preferences({'health_auto_production': 0}), dict(changes, health_auto_production=0))
        for theme in workspace_store.UI_THEME_IDS:
            self.assertEqual(self.store.set_ui_preferences({'theme': theme})['theme'], theme)
        for width in (workspace_store.UI_SIDEBAR_MIN, workspace_store.UI_SIDEBAR_MAX):
            self.assertEqual(self.store.set_ui_preferences({'sidebar_width': width})['sidebar_width'], width)
        for key in ('health_auto_production', 'health_auto_development'):
            for auto in (-1, 0, 10, 30, 60):
                self.assertEqual(self.store.set_ui_preferences({key: auto})[key], auto)

    def test_ui_inspector_explain_and_file_choices(self):
        for width in (280, 340, 560):
            self.store.set_ui_preferences({'inspector_width': width})
            self.assertEqual(WorkspaceStore(self.path).ui_preferences()['inspector_width'], width)
        for fmt in ('table', 'tree', 'json'):
            self.assertEqual(self.store.set_ui_preferences({'explain_format': fmt})['explain_format'], fmt)
        for key in ('files_show_hidden', 'files_transfers_open'):
            for flag in (True, False):
                self.assertEqual(self.store.set_ui_preferences({key: flag})[key], flag)
        before = self.store.ui_preferences()
        for key, values in (('inspector_width', (279, 561, True, 340.0, '340', None)),
                            ('explain_format', ('JSON', 'analyze', '', None, [], {})),
                            ('files_show_hidden', (1, 'true', None)),
                            ('files_transfers_open', (0, 'false', None))):
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.store.set_ui_preferences({'theme': 'fire', key: value})
                self.assertEqual(self.store.ui_preferences(), before)

    def test_ui_preferences_validation_is_atomic_and_strict(self):
        invalid = [None, [], {'unknown': True}, {'theme': '<script>alert(1)</script>'},
                   {'theme': 'Dark'}, {'theme': None}, {'theme': ['dark']}, {'theme': {}},
                   {'sidebar_width': 207}, {'sidebar_width': 321}, {'sidebar_width': 208.0},
                   {'sidebar_width': True}, {'server_nav_collapsed': 1},
                   {'server_nav_collapsed': 'false'}, {'health_auto_production': True},
                   {'health_auto_production': 1}, {'health_auto_production': '30'}, {'health_auto_production': 30.0},
                   {'health_auto': 30}]
        invalid.extend({key: value} for key in ('health_auto_production', 'health_auto_development')
                       for value in (True, 1, '30', 30.0, {}, [], 10**400))
        invalid.extend({key: value} for key in ('theme', 'sidebar_width', 'server_nav_collapsed')
                       for value in ({}, [], 10**400, 0.5))
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.set_ui_preferences(changes)
        self.assertFalse(self.path.exists())
        self.store.set_ui_preferences({'theme': 'nord'})
        with self.assertRaises(ValueError):
            self.store.set_ui_preferences({'theme': 'light', 'health_auto_production': 99})
        self.assertEqual(self.store.ui_preferences()['theme'], 'nord')

    def test_diagram_zoom_speed_is_persisted_and_strict(self):
        self.assertEqual(self.store.ui_preferences()['diagram_zoom_speed'], 100)
        for speed in (25, 50, 100, 150, 200):
            with self.subTest(speed=speed):
                self.store.set_ui_preferences({'diagram_zoom_speed': speed})
                self.assertEqual(WorkspaceStore(self.path).ui_preferences()['diagram_zoom_speed'], speed)
        before = self.store.ui_preferences()
        for value in (0, 300, '100', True, False, 100.0, None, [], {}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.store.set_ui_preferences({'theme': 'fire', 'diagram_zoom_speed': value})
                self.assertEqual(self.store.ui_preferences(), before)
                # Invalid persisted JSON also falls back to Normal.
                with contextlib.closing(self.store._connect()) as conn, conn:
                    conn.execute('INSERT OR REPLACE INTO settings VALUES (?, ?)',
                                 ('ui.diagram_zoom_speed', json.dumps(value)))
                self.assertEqual(self.store.ui_preferences()['diagram_zoom_speed'], 100)
                self.store.set_ui_preferences({'diagram_zoom_speed': before['diagram_zoom_speed']})

    def test_ui_font_and_text_size_preferences_are_persisted_and_strict(self):
        self.assertEqual(self.store.ui_preferences()['ui_font'], 'system')
        self.assertEqual(self.store.ui_preferences()['ui_text_size'], 13)
        self.assertEqual(self.store.ui_preferences()['code_font'], 'system')
        self.assertEqual(self.store.ui_preferences()['code_text_size'], 13)
        for font in workspace_store.UI_FONT_IDS:
            with self.subTest(font=font):
                self.assertEqual(self.store.set_ui_preferences({'ui_font': font})['ui_font'], font)
        for font in workspace_store.UI_CODE_FONT_IDS:
            with self.subTest(font=font):
                self.assertEqual(self.store.set_ui_preferences({'code_font': font})['code_font'], font)
        for size in (workspace_store.UI_TEXT_SIZE_MIN, 13, workspace_store.UI_TEXT_SIZE_MAX):
            self.store.set_ui_preferences({'ui_text_size': size})
            self.assertEqual(WorkspaceStore(self.path).ui_preferences()['ui_text_size'], size)
        for size in (workspace_store.UI_CODE_TEXT_SIZE_MIN, 13, workspace_store.UI_CODE_TEXT_SIZE_MAX):
            self.store.set_ui_preferences({'code_text_size': size})
            self.assertEqual(WorkspaceStore(self.path).ui_preferences()['code_text_size'], size)
        before = self.store.ui_preferences()
        # A font id is never free text: CSS, quotes and unknown ids are
        # refused, and sizes outside the range or not integers are refused.
        bad_fonts = ('inter; } body { display: none', '"inter"', "'inter", 'Inter', 'inter\', sans-serif',
                     'unknown-font', '', 'system-ui', None, 13, True, [], {})
        bad_sizes = (0, 11, 17, 100, 13.0, True, False, '13', '13px', None, [], {})
        bad_code_sizes = (0, 10, 21, 100, 13.5, True, '13', None, [], {})
        for value in bad_fonts:
            with self.subTest(key='ui_font', value=value), self.assertRaises(ValueError):
                self.store.set_ui_preferences({'theme': 'fire', 'ui_font': value})
            with self.subTest(key='code_font', value=value), self.assertRaises(ValueError):
                self.store.set_ui_preferences({'theme': 'fire', 'code_font': value})
        for value in bad_sizes:
            with self.subTest(key='ui_text_size', value=value), self.assertRaises(ValueError):
                self.store.set_ui_preferences({'theme': 'fire', 'ui_text_size': value})
        for value in bad_code_sizes:
            with self.subTest(key='code_text_size', value=value), self.assertRaises(ValueError):
                self.store.set_ui_preferences({'theme': 'fire', 'code_text_size': value})
        self.assertEqual(self.store.ui_preferences(), before)
        # A code font id is not an interface font id, and the other way round.
        with self.assertRaises(ValueError):
            self.store.set_ui_preferences({'ui_font': 'jetbrains-mono'})
        with self.assertRaises(ValueError):
            self.store.set_ui_preferences({'code_font': 'inter'})
        self.assertEqual(self.store.ui_preferences(), before)
        # Invalid persisted JSON also falls back to the defaults.
        for key, value in (('ui_font', 'inter; } body { display: none'), ('code_font', 'comic-sans'),
                           ('ui_text_size', 99), ('code_text_size', 7)):
            with contextlib.closing(self.store._connect()) as conn, conn:
                conn.execute('INSERT OR REPLACE INTO settings VALUES (?, ?)', ('ui.' + key, json.dumps(value)))
            self.assertEqual(self.store.ui_preferences()[key], workspace_store.UI_PREFERENCE_DEFAULTS[key])
            with contextlib.closing(self.store._connect()) as conn, conn:
                conn.execute('DELETE FROM settings WHERE key = ?', ('ui.' + key,))

    def test_ui_preferences_ignore_malformed_and_unknown_rows(self):
        self.store.set_ui_preferences({'theme': 'dark', 'health_auto_production': 60})
        with contextlib.closing(self.store._connect()) as conn, conn:
            for key, value in [('theme', 'dark'), ('sidebar_width', 'true'),
                               ('server_nav_collapsed', '1'), ('health_auto_production', 'broken'), ('health_auto_development', 'true'),
                               ('unknown', '"dark"')]:
                conn.execute('INSERT OR REPLACE INTO settings VALUES (?, ?)', ('ui.' + key, value))
        self.assertEqual(self.store.ui_preferences(), workspace_store.UI_PREFERENCE_DEFAULTS)

    def test_ui_preferences_ignore_case_variant_prefixes(self):
        with contextlib.closing(self.store._connect()) as conn, conn:
            for key in ('UI.theme', 'Ui.theme', 'uI.theme', 'ui.Theme'):
                conn.execute('INSERT INTO settings VALUES (?, ?)', (key, '"dark"'))
        self.assertEqual(self.store.ui_preferences(), workspace_store.UI_PREFERENCE_DEFAULTS)
        self.assertIsNone(self.store.ui_theme_for_index())

    def test_ui_preferences_ignore_invalid_stored_json_types(self):
        for key in workspace_store.UI_PREFERENCE_DEFAULTS:
            values = [{}, [], 10**400, 0.5, 1 if key in ('server_nav_collapsed', 'files_show_hidden', 'files_transfers_open') else True]
            for value in values:
                with self.subTest(key=key, value=value):
                    with contextlib.closing(self.store._connect()) as conn, conn:
                        conn.execute('INSERT OR REPLACE INTO settings VALUES (?, ?)',
                                     ('ui.' + key, json.dumps(value)))
                    self.assertEqual(self.store.ui_preferences(), workspace_store.UI_PREFERENCE_DEFAULTS)
                    self.assertIsNone(self.store.ui_theme_for_index())

    def test_editor_settings_default_without_creating_storage(self):
        self.assertEqual(self.store.editor_settings(), workspace_store.EDITOR_SETTING_DEFAULTS)
        self.assertFalse(self.path.exists())

    def test_editor_settings_round_trip(self):
        expected = dict(workspace_store.EDITOR_SETTING_DEFAULTS, soft_wrap=True, multi_cursor=False)
        self.assertEqual(self.store.set_editor_settings({'soft_wrap': True, 'multi_cursor': False}), expected)
        self.assertEqual(WorkspaceStore(self.path).editor_settings(), expected)
        # A partial write changes only the keys it names.
        self.store.set_editor_settings({'soft_wrap': False})
        self.assertEqual(WorkspaceStore(self.path).editor_settings(),
                         dict(workspace_store.EDITOR_SETTING_DEFAULTS, multi_cursor=False))

    def test_editor_settings_rejects_unknown_keys_and_non_boolean_values(self):
        for changes in ({'unknown_key': True}, {'soft_wrap': 'on'}, {'soft_wrap': None}, {'soft_wrap': 1},
                        {'hover_info': []}, ['soft_wrap']):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.set_editor_settings(changes)
        self.assertFalse(self.path.exists())

    def test_editor_settings_ignore_unknown_and_malformed_stored_rows(self):
        self.store.set_editor_settings({'hover_info': False})
        with contextlib.closing(sqlite3.connect(str(self.path))) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO settings VALUES ('editor.unknown', 'on')")
            conn.execute("INSERT OR REPLACE INTO settings VALUES ('editor.soft_wrap', 'unexpected')")
            conn.execute("INSERT OR REPLACE INTO settings VALUES ('editor.completion_auto', 'off')")
        expected = dict(workspace_store.EDITOR_SETTING_DEFAULTS, hover_info=False, completion_auto=False)
        self.assertEqual(self.store.editor_settings(), expected)

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

    @unittest.skipUnless(os.name == 'posix', 'POSIX sidecar races')
    def test_concurrent_sidecar_unlink_and_rotation_do_not_fail_status_reads(self):
        import http.client
        import threading
        from myflames.ui import WorkspaceServer

        self.store.save('profile', {'name': 'Primary', 'host': 'db.example', 'user': 'reader'})
        journal = Path(str(self.path) + '-journal')
        real_open = os.open

        server = WorkspaceServer(store_path=self.path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        # Case 1: SQLite commits and unlinks -journal right after os.open succeeds.
        journal.write_bytes(b'rollback')
        unlinked = []

        def unlink_after_open(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if str(path) == str(journal) and not unlinked:
                unlinked.append(True)
                journal.unlink()
            return fd

        headers = {
            'Content-Type': 'application/json',
            'X-Myflames-Token': server.token,
            'Origin': server.origin,
            'Cookie': server.cookie_name + '=' + server.session_secret,
        }
        with patch.object(os, 'open', side_effect=unlink_after_open):
            conn = http.client.HTTPConnection(*server.server_address, timeout=10)
            try:
                conn.request('POST', '/api/app/update/status', body='{}', headers=headers)
                response = conn.getresponse()
                status, body = response.status, response.read().decode('utf-8')
            finally:
                conn.close()
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)['schema_version'], 'app-update-status-1.0')
        self.assertEqual(unlinked, [True])

        # Case 2: SQLite unlinks -journal and a new transaction recreates it
        # with a different inode and wide mode right after os.open succeeds.
        journal.write_bytes(b'old-journal')
        rotated = []

        def rotate_after_open(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if str(path) == str(journal) and not rotated:
                rotated.append(True)
                journal.unlink()
                journal.write_bytes(b'new-journal')
                os.chmod(str(journal), 0o666)
            return fd

        # The replacement is checked again and tightened before SQLite opens
        # the database (SQLite then discards the invalid journal, so the mode
        # is read when sqlite3.connect is called).
        modes, journal_opens = [], []
        real_connect = workspace_store.sqlite3.connect

        def spy_open(path, flags, *args, **kwargs):
            if str(path) == str(journal):
                journal_opens.append(True)
            return rotate_after_open(path, flags, *args, **kwargs)

        def spy_connect(*args, **kwargs):
            if journal.exists():
                modes.append(journal.stat().st_mode & 0o777)
            return real_connect(*args, **kwargs)

        with patch.object(os, 'open', side_effect=spy_open), \
                patch.object(workspace_store.sqlite3, 'connect', side_effect=spy_connect):
            self.assertIs(self.store.app_update_auto(), True)
        self.assertEqual(rotated, [True])
        self.assertGreaterEqual(len(journal_opens), 2)
        self.assertTrue(modes, 'the replacement journal exists when SQLite connects')
        self.assertEqual(modes[0], 0o600)

    @unittest.skipUnless(os.name == 'posix', 'POSIX sidecar races')
    def test_sidecar_replaced_on_every_open_fails_closed(self):
        self.store.save('profile', {'name': 'Primary', 'host': 'db.example', 'user': 'reader'})
        journal = Path(str(self.path) + '-journal')
        journal.write_bytes(b'journal')
        real_open = os.open
        opens = []

        def rotate_always(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if str(path) == str(journal):
                opens.append(True)
                journal.unlink()
                journal.write_bytes(b'journal')
            return fd

        try:
            with patch.object(os, 'open', side_effect=rotate_always), \
                    patch.object(workspace_store.sqlite3, 'connect') as connect:
                with self.assertRaisesRegex(ValueError, 'changed while it was being opened'):
                    self.store._connect()
                connect.assert_not_called()
            self.assertEqual(len(opens), 9)
        finally:
            journal.unlink()

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFOs')
    def test_fifo_sidecar_is_refused_without_blocking(self):
        self.store.save('profile', {'name': 'Primary', 'host': 'db.example', 'user': 'reader'})
        journal = Path(str(self.path) + '-journal')
        os.mkfifo(str(journal))
        try:
            with patch.object(workspace_store.sqlite3, 'connect') as connect:
                with self.assertRaisesRegex(ValueError, 'wrong file type'):
                    self.store._connect()
                connect.assert_not_called()
        finally:
            os.unlink(str(journal))

    @unittest.skipUnless(os.name == 'posix', 'POSIX sidecar security races')
    def test_security_sidecar_swap_to_symlink_or_foreign_owner_is_rejected(self):
        self.store.save('profile', {'name': 'Primary', 'host': 'db.example', 'user': 'reader'})
        journal = Path(str(self.path) + '-journal')
        target = Path(self.temp.name) / 'external-target'
        target.write_bytes(b'keep intact')
        os.chmod(str(target), 0o644)
        real_open = os.open
        real_lstat = os.lstat

        # Abuse 1: symlink swapped in between lstat and open (O_NOFOLLOW).
        journal.write_bytes(b'legit')
        swapped = []

        def swap_before_open(path, flags, *args, **kwargs):
            if str(path) == str(journal) and not swapped:
                swapped.append(True)
                journal.unlink()
                journal.symlink_to(target)
            return real_open(path, flags, *args, **kwargs)

        with patch.object(os, 'open', side_effect=swap_before_open), \
                patch.object(workspace_store.sqlite3, 'connect') as connect:
            with self.assertRaisesRegex(ValueError, 'link|symlink'):
                self.store._connect()
            connect.assert_not_called()
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)
        self.assertEqual(target.read_bytes(), b'keep intact')
        journal.unlink()

        # Abuse 2: symlink swapped in between open and post-open lstat.
        journal.write_bytes(b'legit')
        swapped_after = []

        def swap_after_open(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if str(path) == str(journal) and not swapped_after:
                swapped_after.append(True)
                journal.unlink()
                journal.symlink_to(target)
            return fd

        with patch.object(os, 'open', side_effect=swap_after_open), \
                patch.object(workspace_store.sqlite3, 'connect') as connect:
            with self.assertRaisesRegex(ValueError, 'link|symlink'):
                self.store._connect()
            connect.assert_not_called()
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)
        self.assertEqual(target.read_bytes(), b'keep intact')
        journal.unlink()

        # Abuse 3: foreign-owned file swapped in between open and post-open lstat.
        journal.write_bytes(b'legit')
        lstat_calls = [0]

        def foreign_after_open(path, *args, **kwargs):
            result = real_lstat(path, *args, **kwargs)
            if str(path) == str(journal):
                lstat_calls[0] += 1
                if lstat_calls[0] >= 2:
                    fields = list(result)
                    fields[4] = os.geteuid() + 1
                    return os.stat_result(fields)
            return result

        with patch.object(os, 'lstat', side_effect=foreign_after_open), \
                patch.object(workspace_store.sqlite3, 'connect') as connect:
            with self.assertRaisesRegex(ValueError, 'owner|owned'):
                self.store._connect()
            connect.assert_not_called()

    @unittest.skipUnless(os.name == 'posix', 'POSIX mode bits')
    def test_permission_failure_prevents_opening_database(self):
        self.store.save('investigation', bundle())
        os.chmod(str(self.path.parent), 0o755)
        with patch.object(workspace_store.os, 'fchmod', side_effect=PermissionError('cannot protect')), \
                patch.object(workspace_store.sqlite3, 'connect') as connect:
            with self.assertRaisesRegex(PermissionError, 'cannot protect'):
                self.store.list('investigation')
        connect.assert_not_called()

    def test_windows_sidecar_protection_is_a_noop(self):
        journal = Path(str(self.path) + '-journal')  # built before os.name changes
        with patch.object(workspace_store.os, 'name', 'nt'), patch.object(workspace_store.os, 'chmod') as chmod, \
                patch.object(workspace_store.os, 'open', side_effect=AssertionError('sidecar open on Windows')), \
                patch.object(workspace_store.os, 'lstat', side_effect=AssertionError('permission check on Windows')):
            workspace_store._protect_sidecar(journal, 0o600)
        chmod.assert_not_called()

    @unittest.skipUnless(os.name == 'posix', 'POSIX sidecar refusals')
    def test_refused_sidecar_is_never_retried_even_if_it_then_vanishes(self):
        self.store.save('profile', {'name': 'Primary', 'host': 'db.example', 'user': 'reader'})
        journal = Path(str(self.path) + '-journal')
        target = Path(self.temp.name) / 'elsewhere'
        target.write_bytes(b'keep')
        journal.symlink_to(target)
        real_lstat = os.lstat
        calls = []

        def vanish_after_refusal(path, *args, **kwargs):
            result = real_lstat(path, *args, **kwargs)
            if str(path) == str(journal):
                calls.append(True)
                if len(calls) == 1:
                    journal.unlink()
            return result

        with patch.object(os, 'lstat', side_effect=vanish_after_refusal):
            with self.assertRaisesRegex(ValueError, 'symbolic link'):
                workspace_store._protect_sidecar(journal, 0o600)
        self.assertEqual(len(calls), 1)

        # Refused at the second lstat (a link swapped in after the open), and
        # gone right after that refusal.
        journal.write_bytes(b'journal')
        real_open = os.open
        swapped, seen_link = [], []

        def swap_after_open(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if str(path) == str(journal) and not swapped:
                swapped.append(True)
                journal.unlink()
                journal.symlink_to(target)
            return fd

        def vanish_after_link(path, *args, **kwargs):
            result = real_lstat(path, *args, **kwargs)
            if str(path) == str(journal) and stat.S_ISLNK(result.st_mode) and not seen_link:
                seen_link.append(True)
                journal.unlink()
            return result

        with patch.object(os, 'open', side_effect=swap_after_open), \
                patch.object(os, 'lstat', side_effect=vanish_after_link):
            with self.assertRaisesRegex(ValueError, 'symbolic link'):
                workspace_store._protect_sidecar(journal, 0o600)
        self.assertEqual((swapped, seen_link), ([True], [True]))

        # Refused at the open (O_NOFOLLOW on a link swapped in after the first
        # lstat), and gone before anything could check again.
        journal.write_bytes(b'journal')

        def link_then_vanish(path, flags, *args, **kwargs):
            if str(path) == str(journal):
                journal.unlink()
                journal.symlink_to(target)
                journal.unlink()
                raise OSError(errno.ELOOP, 'Too many levels of symbolic links', str(path))
            return real_open(path, flags, *args, **kwargs)

        with patch.object(os, 'open', side_effect=link_then_vanish):
            with self.assertRaisesRegex(ValueError, 'symbolic link'):
                workspace_store._protect_sidecar(journal, 0o600)
        self.assertEqual(target.read_bytes(), b'keep')

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

    def test_save_and_delete_connection_group(self):
        # Deleting a group before storage exists validates the name and does not create the database.
        with self.assertRaises(ValueError):
            self.store.delete_connection_group('')
        empty_del = self.store.delete_connection_group('nonexistent')
        self.assertEqual(empty_del['groups'], [])
        self.assertEqual(empty_del['profiles'], [])
        self.assertFalse(self.path.exists())

        p1 = self.store.save('profile', {'name': 'db-1', 'host': 'db1.example', 'user': 'dba'}, 'p1',
                             password='pw-1', remember=True)
        p2 = self.store.save('profile', {'name': 'db-2', 'host': 'db2.example', 'user': 'dba'}, 'p2',
                             password='pw-2', remember=True)
        p3 = self.store.save('profile', {'name': 'db-3', 'host': 'db3.example', 'user': 'dba'}, 'p3',
                             password='pw-3', remember=True)
        inv = self.store.save('investigation', bundle(), 'inv-1')

        saved = self.store.save_connection_group('  prod-eu  ', ['p1', 'p2'])
        self.assertEqual(saved['name'], 'prod-eu')
        self.assertEqual([g['name'] for g in saved['groups']], ['prod-eu'])
        self.assertEqual(saved['groups'][0]['profile_ids'], ['p1', 'p2'])

        # Rename prod-eu -> prod-global while adding p3 and removing p2.
        renamed = self.store.save_connection_group('prod-global', ['p1', 'p3'], previous_name='prod-eu')
        self.assertEqual(renamed['name'], 'prod-global')
        self.assertEqual([g['name'] for g in renamed['groups']], ['prod-global'])
        self.assertEqual(renamed['groups'][0]['profile_ids'], ['p1', 'p3'])
        self.assertEqual(self.store.get('profile', 'p2')['group'], '')

        # Non-profile record ID is refused atomically.
        with self.assertRaises(ValueError):
            self.store.save_connection_group('prod-global', ['p1', inv['id']], previous_name='prod-global')
        self.assertEqual({p['id'] for p in self.store.list('profile') if p['group'] == 'prod-global'}, {'p1', 'p3'})

        # Delete group keeps all profiles and their saved passwords.
        deleted = self.store.delete_connection_group('prod-global')
        self.assertTrue(deleted['deleted'])
        self.assertEqual(deleted['groups'], [])
        for pid, host, pw in (('p1', 'db1.example', 'pw-1'),
                              ('p2', 'db2.example', 'pw-2'),
                              ('p3', 'db3.example', 'pw-3')):
            prof = self.store.get('profile', pid)
            self.assertEqual(prof['group'], '')
            self.assertTrue(prof['has_saved_password'])
            filled = self.store.connection_with_saved_password({'profile_id': pid, 'host': host, 'port': 3306, 'user': 'dba'})
            self.assertEqual(filled['password'], pw)

    def test_typed_password_still_checks_group_membership(self):
        # A request that types the password is checked against the group too:
        # consent is the store, not the credential's origin.
        self.store.save('profile', {'name': 'db-1', 'host': 'db1.example', 'user': 'dba', 'group': 'G'}, 'p1',
                        password='pw-1', remember=True)
        request = {'profile_id': 'p1', 'host': 'db1.example', 'port': 3306, 'user': 'dba', 'group_name': 'G'}
        filled = self.store.connection_with_saved_password(dict(request, password='typed-pw'))
        self.assertEqual(filled['password'], 'typed-pw')
        self.assertEqual(self.store.connection_with_saved_password(dict(request))['password'], 'pw-1')
        # A member that left the group is refused with or without a typed
        # password, and so is a deleted profile.
        self.store.assign_profile_group(['p1'], 'H')
        for value in (dict(request, password='typed-pw'), dict(request)):
            with self.assertRaises(ValueError):
                self.store.connection_with_saved_password(value)
        self.store.delete('profile', 'p1')
        for value in (dict(request, password='typed-pw'), dict(request)):
            with self.assertRaises(ValueError):
                self.store.connection_with_saved_password(value)
