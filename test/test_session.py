"""Session restore: stored workspace snapshot, revisions, limits, and secrets."""
import http.client
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from unittest import mock

from myflames import workspace_store
from myflames.ui import WorkspaceServer, api_response, cmd_ui
from myflames.workspace_store import (SessionConflict, SessionTooLarge, WorkspaceStore,
                                      validate_session_state)

TEST_DIR = Path(__file__).resolve().parent
PLAN = (TEST_DIR / 'mysql-explain-json-sample.json').read_text()
SECRET = 'hunter2-session-secret'


def sample_state(**extra):
    state = {
        'schema_version': 'session-1.0', 'mode': 'inspect', 'view': 'tree', 'metric': 'rows',
        'active_plan_id': 'p1', 'selected_id': 'n2', 'focus_id': '', 'collapsed_ids': ['n3'],
        'plans': [{'id': 'p1', 'name': 'sample.json', 'raw': PLAN, 'capture': None, 'notes': ''}],
        'query_lab': {
            'profile_id': 'prof', 'tab_id': 't2',
            'connection': {'host': '127.0.0.1', 'port': 3306, 'user': 'app', 'database': 'shop',
                           'ssl_mode': 'REQUIRED', 'ssl_ca': '', 'password': SECRET},
            'tabs': [{'id': 't1', 'name': 'Query 1', 'sql': 'SELECT 1', 'parameters': '{}'},
                     {'id': 't2', 'name': 'Orders', 'sql': 'SELECT * FROM orders WHERE id = :id',
                      'parameters': '{"id": 42}'}],
        },
    }
    state.update(extra)
    return state


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {'HOME': self.tmp.name, 'USERPROFILE': self.tmp.name,
                                          'MYFLAMES_SECRET_BACKEND': 'vault'})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        self.db = Path(self.tmp.name) / 'workspace.sqlite3'
        self.store = WorkspaceStore(self.db)


class TestSessionStore(StoreTestCase):
    def test_empty_store_loads_without_creating_the_database(self):
        session = self.store.load_session()
        self.assertEqual(session, {'revision': 0, 'saved_at': '', 'state': None, 'restore': True})
        self.assertFalse(self.db.exists())

    def test_save_round_trip_increments_revision(self):
        first = self.store.save_session(sample_state(), 0)
        self.assertEqual(first['revision'], 1)
        self.assertTrue(first['saved_at'])
        second = self.store.save_session(sample_state(view='flamegraph'), 1)
        self.assertEqual(second['revision'], 2)
        loaded = self.store.load_session()
        self.assertEqual(loaded['revision'], 2)
        self.assertEqual(loaded['state']['view'], 'flamegraph')
        self.assertEqual(loaded['state']['plans'][0]['raw'], PLAN)
        self.assertEqual(loaded['state']['query_lab']['tabs'][1]['parameters'], '{"id": 42}')

    def test_stale_revision_from_another_page_conflicts_and_keeps_newer_state(self):
        self.store.save_session(sample_state(view='tree'), 0, 'tab-a', 1)
        with self.assertRaises(SessionConflict) as caught:
            self.store.save_session(sample_state(view='bargraph'), 0, 'tab-b', 1)
        self.assertEqual(caught.exception.revision, 1)
        self.assertIn('another browser tab', str(caught.exception))
        self.assertEqual(self.store.load_session()['state']['view'], 'tree')
        # After reloading the newer revision, the other page saves normally.
        self.assertEqual(self.store.save_session(sample_state(view='bargraph'), 1, 'tab-b', 2)['revision'], 2)

    def test_same_page_overlapping_saves_do_not_conflict_and_late_old_save_is_dropped(self):
        self.store.save_session(sample_state(view='tree'), 0, 'tab-a', 1)
        # seq 3 lands first while seq 2 was still in flight with revision 0.
        self.assertEqual(self.store.save_session(sample_state(view='flamegraph'), 0, 'tab-a', 3)['revision'], 2)
        late = self.store.save_session(sample_state(view='bargraph'), 0, 'tab-a', 2)
        self.assertTrue(late['stale'])
        self.assertEqual(late['revision'], 2)
        self.assertEqual(self.store.load_session()['state']['view'], 'flamegraph')

    def test_clear_keeps_settings_and_bumps_revision(self):
        self.store.save_session(sample_state(), 0, 'tab-a', 1)
        cleared = self.store.clear_session('tab-b')
        self.assertEqual(cleared, {'revision': 2, 'restore': True})
        loaded = self.store.load_session()
        self.assertIsNone(loaded['state'])
        self.assertEqual(loaded['saved_at'], '')
        with self.assertRaises(SessionConflict):
            self.store.save_session(sample_state(), 1, 'tab-a', 2)

    def test_restore_off_clears_and_refuses_saves(self):
        self.store.save_session(sample_state(), 0)
        result = self.store.set_session_restore(False)
        self.assertEqual(result, {'revision': 2, 'restore': False})
        loaded = self.store.load_session()
        self.assertFalse(loaded['restore'])
        self.assertIsNone(loaded['state'])
        with self.assertRaisesRegex(ValueError, 'Restore workspace on startup is off'):
            self.store.save_session(sample_state(), 2)
        self.assertTrue(self.store.set_session_restore(True)['restore'])
        self.assertEqual(self.store.save_session(sample_state(), 3)['revision'], 4)
        with self.assertRaisesRegex(ValueError, 'true or false'):
            self.store.set_session_restore('yes')

    def test_size_cap_rejects_and_keeps_previous_session(self):
        self.store.save_session(sample_state(view='tree'), 0)
        with mock.patch.object(workspace_store, 'SESSION_MAX_BYTES', 4096):
            with self.assertRaisesRegex(SessionTooLarge, '64 MiB'):
                self.store.save_session(sample_state(padding='x' * 5000), 1)
        loaded = self.store.load_session()
        self.assertEqual(loaded['revision'], 1)
        self.assertEqual(loaded['state']['view'], 'tree')
        self.assertNotIn('padding', loaded['state'])

    def test_passwords_are_stripped_at_any_depth_and_never_written(self):
        state = sample_state(nested=[{'deep': {'Password': SECRET, 'keep': 1}}], password=SECRET)
        self.store.save_session(state, 0)
        loaded = self.store.load_session()['state']
        self.assertNotIn('password', loaded['query_lab']['connection'])
        self.assertEqual(loaded['query_lab']['connection']['user'], 'app')
        self.assertEqual(loaded['nested'], [{'deep': {'keep': 1}}])
        self.assertNotIn('password', loaded)
        # The caller's object is not modified.
        self.assertEqual(state['query_lab']['connection']['password'], SECRET)
        # Nothing on disk contains the secret: database, journal, or WAL.
        for path in Path(self.tmp.name).iterdir():
            self.assertNotIn(SECRET.encode('utf-8'), path.read_bytes(), path.name)

    def test_security_editor_sql_credentials_are_redacted_before_persistence(self):
        state = sample_state()
        tab = state['query_lab']['tabs'][0]
        tab['sql'] = "ALTER USER app IDENTIFIED BY 'restore-secret'; SELECT 1"
        tab['text'] = "SET PASSWORD = PASSWORD('text-secret')"
        state['query_lab']['tabs'][1]['sql'] = "SELECT 'password', 'IDENTIFIED BY', name FROM users"
        expected_select = state['query_lab']['tabs'][1]['sql']
        self.store.save_session(state, 0)
        loaded = self.store.load_session()['state']
        self.assertEqual(loaded['query_lab']['tabs'][0]['sql'],
                         "ALTER USER app IDENTIFIED BY '[redacted]'; SELECT 1")
        self.assertNotIn('text-secret', loaded['query_lab']['tabs'][0]['text'])
        self.assertEqual(loaded['query_lab']['tabs'][1]['sql'], expected_select)
        self.assertIn('restore-secret', tab['sql'])
        self.assertNotIn(b'restore-secret', self.db.read_bytes())
        self.assertNotIn(b'text-secret', self.db.read_bytes())

    def test_validation_rejects_bad_shapes(self):
        for bad in ([], 'text', None, 3):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, 'JSON object'):
                validate_session_state(bad)
        deep = {}
        node = deep
        for _ in range(workspace_store.SESSION_MAX_DEPTH + 5):
            node['n'] = {}
            node = node['n']
        with self.assertRaisesRegex(ValueError, 'too deeply nested'):
            validate_session_state(deep)
        with self.assertRaisesRegex(ValueError, 'finite'):
            validate_session_state({'x': [float('nan')]})
        with self.assertRaisesRegex(ValueError, 'only JSON values'):
            validate_session_state({'x': object()})
        for revision in (-1, '1', True, None, 1.5):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                self.store.save_session({}, revision)
        with self.assertRaisesRegex(ValueError, 'client ID'):
            self.store.save_session({}, 0, 'x' * 101)
        with self.assertRaisesRegex(ValueError, 'sequence'):
            self.store.save_session({}, 0, 'a', -1)

    @unittest.skipIf(sys.platform == 'win32', 'POSIX permissions')
    def test_database_and_directory_are_owner_only(self):
        directory = Path(self.tmp.name) / 'fresh' / '.myflames'
        store = WorkspaceStore(directory / 'workspace.sqlite3')
        old = os.umask(0o022)
        try:
            store.save_session(sample_state(), 0)
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((directory / 'workspace.sqlite3').stat().st_mode), 0o600)
        # A database created earlier with broad permissions is tightened.
        os.chmod(str(directory / 'workspace.sqlite3'), 0o644)
        store.load_session()
        self.assertEqual(stat.S_IMODE((directory / 'workspace.sqlite3').stat().st_mode), 0o600)

    @unittest.skipIf(sys.platform == 'win32', 'POSIX permissions')
    def test_default_directory_is_tightened_when_it_already_exists(self):
        home = Path(self.tmp.name) / 'home'
        (home / '.myflames').mkdir(parents=True, mode=0o755)
        os.chmod(str(home / '.myflames'), 0o755)
        with mock.patch.object(Path, 'home', return_value=home):
            store = WorkspaceStore()
            store.save_session({}, 0)
        self.assertEqual(stat.S_IMODE((home / '.myflames').stat().st_mode), 0o700)


class _Workspace:
    def __init__(self, store, no_restore=False):
        self.store = store
        self.no_restore = no_restore


class TestSessionApi(StoreTestCase):
    def test_endpoints_round_trip(self):
        ws = _Workspace(self.store)
        loaded = api_response('/api/session/load', {}, ws)
        self.assertEqual(loaded['revision'], 0)
        self.assertIsNone(loaded['state'])
        self.assertFalse(loaded['skip_restore'])
        saved = api_response('/api/session/save', {'state': sample_state(), 'revision': 0,
                                                   'client_id': 'a', 'seq': 1}, ws)
        self.assertEqual(saved['revision'], 1)
        self.assertEqual(api_response('/api/session/load', {}, ws)['state']['view'], 'tree')
        self.assertEqual(api_response('/api/session/settings', {'restore': False}, ws)['restore'], False)
        self.assertIsNone(api_response('/api/session/load', {}, ws)['state'])
        api_response('/api/session/settings', {'restore': True}, ws)
        api_response('/api/session/save', {'state': sample_state(), 'revision': 3}, ws)
        self.assertEqual(api_response('/api/session/clear', {}, ws)['revision'], 5)
        with self.assertRaisesRegex(ValueError, 'running workspace'):
            api_response('/api/session/load', {})
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            api_response('/api/session/other', {}, ws)

    def test_no_restore_skips_restore_and_autosave_but_keeps_the_session(self):
        self.store.save_session(sample_state(), 0)
        ws = _Workspace(self.store, no_restore=True)
        loaded = api_response('/api/session/load', {}, ws)
        self.assertTrue(loaded['skip_restore'])
        self.assertIsNone(loaded['state'])
        self.assertTrue(loaded['restore'])
        with self.assertRaisesRegex(ValueError, '--no-restore'):
            api_response('/api/session/save', {'state': {}, 'revision': 1}, ws)
        # The stored session is still there for the next normal launch.
        self.assertEqual(self.store.load_session()['state']['view'], 'tree')
        self.assertEqual(api_response('/api/session/load', {}, _Workspace(self.store))['state']['view'], 'tree')


class TestSessionHttp(StoreTestCase):
    def start(self, **kwargs):
        server = WorkspaceServer(store_path=str(self.db), **kwargs)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def post(self, server, path, payload, token=None, body=None):
        conn = http.client.HTTPConnection(*server.server_address, timeout=30)
        try:
            data = body if body is not None else json.dumps(payload).encode('utf-8')
            conn.request('POST', path, body=data,
                         headers={'Content-Type': 'application/json',
                                  'X-Myflames-Token': server.token if token is None else token,
                                  'Origin': server.origin,
                                  'Cookie': server.cookie_name + '=' + server.session_secret})
            response = conn.getresponse()
            return response.status, json.loads(response.read().decode('utf-8'))
        finally:
            conn.close()

    def test_allowlist_token_conflict_and_too_large(self):
        import inspect
        from myflames.ui import WorkspaceHandler
        source = inspect.getsource(WorkspaceHandler.do_POST)
        for path in ('load', 'save', 'clear', 'settings'):
            self.assertIn('"/api/session/%s"' % path, source)
        server = self.start()
        status, body = self.post(server, '/api/session/load', {}, token='wrong')
        self.assertEqual(status, 403)
        status, body = self.post(server, '/api/session/load', {})
        self.assertEqual((status, body['revision'], body['state']), (200, 0, None))
        status, body = self.post(server, '/api/session/save',
                                 {'state': sample_state(), 'revision': 0, 'client_id': 'a', 'seq': 1})
        self.assertEqual((status, body['revision']), (200, 1))
        status, body = self.post(server, '/api/session/save',
                                 {'state': sample_state(), 'revision': 0, 'client_id': 'b', 'seq': 1})
        self.assertEqual(status, 409)
        self.assertEqual(body['code'], 'session_conflict')
        self.assertEqual(body['revision'], 1)
        self.assertIn('another browser tab', body['error'])
        # Larger than the general 12 MiB request limit, still accepted here.
        big = sample_state(padding='x' * (13 * 1024 * 1024))
        status, body = self.post(server, '/api/session/save', {'state': big, 'revision': 1})
        self.assertEqual(status, 200, body)
        with mock.patch.object(workspace_store, 'SESSION_MAX_BYTES', 1024):
            status, body = self.post(server, '/api/session/save', {'state': sample_state(), 'revision': 2})
        self.assertEqual(status, 413)
        self.assertEqual(body['code'], 'session_too_large')
        self.assertIn('64 MiB', body['error'])
        status, body = self.post(server, '/api/session/load', {})
        self.assertEqual(body['revision'], 2)
        self.assertIn('padding', body['state'])

    def test_restart_with_the_same_store_restores(self):
        server = self.start()
        self.post(server, '/api/session/save', {'state': sample_state(view='bargraph'), 'revision': 0})
        server.shutdown()
        server.server_close()
        again = self.start()
        status, body = self.post(again, '/api/session/load', {})
        self.assertEqual(body['state']['view'], 'bargraph')
        self.assertNotIn(SECRET, json.dumps(body))
        skipped = self.start(no_restore=True)
        status, body = self.post(skipped, '/api/session/load', {})
        self.assertTrue(body['skip_restore'])
        self.assertIsNone(body['state'])
        status, body = self.post(skipped, '/api/session/save', {'state': {}, 'revision': 1})
        self.assertEqual(status, 400)
        self.assertIn('--no-restore', body['error'])


class TestUiCommand(unittest.TestCase):
    def test_help_documents_no_restore(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            cmd_ui(['--help'])
        self.assertEqual(caught.exception.code, 0)
        text = ' '.join(out.getvalue().split())
        self.assertIn('--no-restore', text)
        self.assertIn('Restore workspace on startup', text)

    def test_no_restore_flag_reaches_the_server(self):
        created = {}

        class FakeServer:
            origin = 'http://127.0.0.1:1'
            launch_url = origin + '/#launch=test-only'

            def __init__(self, port, no_restore=False, tool_groups_file=None, news_reader=None):
                created['no_restore'] = no_restore
                created['tool_groups_file'] = tool_groups_file
                created['news_reader'] = news_reader

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def serve_forever(self):
                raise KeyboardInterrupt

        with mock.patch('myflames.ui.WorkspaceServer', FakeServer), \
                mock.patch('sys.stderr', io.StringIO()):
            cmd_ui(['--no-browser', '--no-restore'])
        self.assertTrue(created['no_restore'])
        # Only the real launcher reads Home news feeds (myflames/news.py).
        from myflames.news import fetch_feed
        self.assertIs(created['news_reader'], fetch_feed)
        with mock.patch('myflames.ui.WorkspaceServer', FakeServer), \
                mock.patch('sys.stderr', io.StringIO()):
            cmd_ui(['--no-browser'])
        self.assertFalse(created['no_restore'])
        self.assertIsNone(created['tool_groups_file'])
        # Only the desktop app sets this: its record of tool process groups.
        with mock.patch('myflames.ui.WorkspaceServer', FakeServer), \
                mock.patch('sys.stderr', io.StringIO()), \
                mock.patch.dict('os.environ', {'MYFLAMES_TOOL_GROUPS_FILE': '/private/runtime/tool-groups-x.json'}):
            cmd_ui(['--no-browser'])
        self.assertEqual(created['tool_groups_file'], '/private/runtime/tool-groups-x.json')


class TestClearOrdering(StoreTestCase):
    def test_in_flight_save_cannot_undo_start_fresh(self):
        first = self.store.save_session({"plans": [1]}, 0, "A", 1)
        cleared = self.store.clear_session("A", 2)
        late = self.store.save_session({"plans": [1]}, first["revision"], "A", 1)
        self.assertTrue(late.get("stale"))
        self.assertEqual(late["revision"], cleared["revision"])
        self.assertIsNone(self.store.load_session()["state"])
        newer = self.store.save_session({"plans": []}, cleared["revision"], "A", 3)
        self.assertNotIn("stale", newer)

    def test_delayed_clear_cannot_delete_a_newer_save_or_lower_the_sequence(self):
        saved = self.store.save_session({"plans": [3]}, 0, "A", 3)
        late_clear = self.store.clear_session("A", 2)
        self.assertTrue(late_clear["stale"])
        self.assertEqual(late_clear["revision"], saved["revision"])
        self.assertEqual(self.store.load_session()["state"], {"plans": [3]})
        # The sequence stayed at 3, so a retried save 3 is still stale.
        retry = self.store.save_session({"plans": ["retry"]}, saved["revision"], "A", 3)
        self.assertTrue(retry["stale"])
        self.assertEqual(self.store.load_session()["state"], {"plans": [3]})
        # A newer request from the page still applies.
        self.assertNotIn("stale", self.store.clear_session("A", 4))
        self.assertIsNone(self.store.load_session()["state"])

    def test_delayed_settings_change_is_dropped_the_same_way(self):
        saved = self.store.save_session({"plans": [3]}, 0, "A", 3)
        late = self.store.set_session_restore(False, "A", 2)
        self.assertEqual(late, {"revision": saved["revision"], "restore": True, "stale": True})
        loaded = self.store.load_session()
        self.assertTrue(loaded["restore"])
        self.assertEqual(loaded["state"], {"plans": [3]})
        self.assertTrue(self.store.save_session({"plans": []}, saved["revision"], "A", 3)["stale"])
        # Another page's request is not this page's sequence: it applies.
        other = self.store.set_session_restore(False, "B", 1)
        self.assertNotIn("stale", other)
        self.assertFalse(self.store.load_session()["restore"])


if __name__ == '__main__':
    unittest.main()
