"""Authenticated workspace persistence and capture routes over real HTTP."""
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, patch

from myflames.connector import ConnectorError
from myflames.ui import ASSET_DIR, WorkspaceServer, api_response

PLAN = (Path(__file__).parent / 'mysql-explain-json-sample.json').read_text(encoding="utf-8")


class TestWorkspaceAPI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {'HOME': self.temp.name, 'USERPROFILE': self.temp.name,
                                     'MYFLAMES_SECRET_BACKEND': 'vault'})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        self.path = Path(self.temp.name) / 'private' / 'workspace.sqlite3'
        self.server = WorkspaceServer(0, store_path=self.path)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path, payload=None, headers=None, method='POST', raw_body=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        default = {'Content-Type': 'application/json', 'X-Myflames-Token': self.server.token,
                    'Origin': self.server.origin,
                    'Cookie': self.server.cookie_name + '=' + self.server.session_secret}
        default.update(headers or {})
        try:
            body = raw_body if raw_body is not None else json.dumps(payload or {}) if method == 'POST' else None
            conn.request(method, path, body, default)
            response = conn.getresponse()
            raw = response.read()
            body = json.loads(raw) if response.getheader('Content-Type').startswith('application/json') else raw
            return response.status, body, dict(response.getheaders())
        finally:
            conn.close()

    def test_empty_lists_do_not_create_storage(self):
        for kind in ('investigations', 'profiles'):
            status, body, headers = self.request('/api/' + kind + '/list')
            self.assertEqual(status, 200)
            self.assertEqual(body['items'], [])
            if kind == 'profiles':
                # Where a remembered password would go; never a secret.
                self.assertEqual(set(body['secret_store']) - {'error'}, {'backend', 'label', 'available'})
            self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertFalse(self.path.exists())

    def test_request_shape_errors_are_logged_redacted_400s(self):
        from myflames import ui
        secret = 'request-password-for-redaction'
        for error_type in (TypeError, KeyError, AttributeError, RecursionError, OverflowError):
            with self.subTest(error_type=error_type.__name__):
                log = io.StringIO()
                with patch.object(ui, 'api_response', side_effect=error_type('bug with ' + secret)), \
                        patch.object(ui.sys, 'stderr', log):
                    status, body, unused = self.request('/api/profiles/list',
                        {'connection': {'password': secret}})
                self.assertEqual(status, 400)
                self.assertEqual(body, {'error': ui._unexpected_error_message('/api/profiles/list')})
                self.assertIn('unexpected error in POST /api/profiles/list', log.getvalue())
                self.assertIn(error_type.__name__, log.getvalue())
                self.assertIn('[redacted]', log.getvalue())
                self.assertNotIn(secret, log.getvalue())
                self.assertNotIn(secret, json.dumps(body))

    def test_other_backend_errors_remain_logged_redacted_500s(self):
        from myflames import ui
        secret = 'unexpected-request-password'
        log = io.StringIO()
        with patch.object(ui, 'api_response', side_effect=RuntimeError(secret)), \
                patch.object(ui.sys, 'stderr', log):
            status, body, unused = self.request('/api/profiles/list',
                {'connection': {'password': secret}})
        self.assertEqual(status, 500)
        self.assertEqual(body, {'error': ui._unexpected_error_message('/api/profiles/list')})
        self.assertIn('RuntimeError', log.getvalue())
        self.assertIn('[redacted]', log.getvalue())
        self.assertNotIn(secret, log.getvalue())
        self.assertNotIn(secret, json.dumps(body))

    def test_write_internal_errors_after_send_are_logged_redacted_500s(self):
        from myflames import ui
        secret = 'write-readback-redaction-secret'
        # Exercise every SQL-sending route, including reviewed actions, kill,
        # editor, lifecycle and topology changes. A read-back bug cannot prove
        # that the statement which preceded it was refused.
        paths = ui._WRITE_PATHS + ('/api/table-data/apply', '/api/table-data/cancel', '/api/table-data/close',
                                  '/api/editor/rollback', '/api/editor/stop', '/api/editor/close', '/api/editor/use',
                                  '/api/lifecycle/cancel', '/api/live/cancel', '/api/import/csv/stop')
        self.assertEqual(set(paths), set(ui._SQL_OUTCOME_PATHS))
        for path in paths:
            for error_type in (TypeError, KeyError, AttributeError, RecursionError,
                               OverflowError, RuntimeError, OSError):
                with self.subTest(path=path, error_type=error_type.__name__):
                    sent = []
                    def execute_then_read_back(*args):
                        sent.append(path)
                        raise error_type('read-back failed with ' + secret)
                    log = io.StringIO()
                    with patch.object(ui, 'api_response', side_effect=execute_then_read_back), \
                            patch.object(ui.sys, 'stderr', log):
                        status, body, unused = self.request(path, {'connection': {'password': secret}})
                    self.assertEqual(sent, [path])
                    self.assertEqual(status, 500)
                    self.assertEqual(body, {'error': ui._unexpected_error_message(path)})
                    self.assertIn('outcome is unknown', body['error'])
                    self.assertIn(error_type.__name__, log.getvalue())
                    self.assertIn('[redacted]', log.getvalue())
                    self.assertNotIn(secret, log.getvalue())
                    self.assertNotIn(secret, json.dumps(body))

    def test_reviewed_executor_readback_error_after_transport_send_is_500(self):
        from myflames import reviewed_writes, ui
        secret = 'reviewed-readback-redaction-secret'
        sent = []
        transport = types.SimpleNamespace(run_write=lambda sql, **kwargs: sent.append(sql))
        plan = {'action': 'servers_load', 'statements': ['LOAD MYSQL SERVERS TO RUNTIME;'],
                'expect': {'engine': 'proxysql'}}

        def read_back():
            self.assertTrue(sent, 'the transport has already sent SQL')
            raise KeyError(secret)

        def handle(*args):
            return reviewed_writes.execute(transport, plan, lambda: plan, 'proxysql-action-1.0',
                                           read_after=read_back)

        log = io.StringIO()
        with patch.object(ui, 'api_response', side_effect=handle), patch.object(ui.sys, 'stderr', log):
            status, body, unused = self.request('/api/live/proxysql/action',
                {'connection': {'password': secret}})
        self.assertEqual(sent, plan['statements'])
        self.assertEqual(status, 500)
        self.assertIn('outcome is unknown', body['error'])
        self.assertIn('KeyError', log.getvalue())
        self.assertNotIn(secret, log.getvalue())
        self.assertNotIn(secret, json.dumps(body))

    def test_read_with_malformed_field_keeps_logged_400(self):
        from myflames import ui
        log = io.StringIO()
        with patch.object(ui.sys, 'stderr', log):
            status, body, unused = self.request('/api/report/export',
                {'report': {'schema_version': 'report-1.0'}, 'format': 'text'})
        self.assertEqual(status, 400)
        self.assertIn('error', body)
        self.assertIn('KeyError', log.getvalue())
    def test_handler_error_log_redacts_sql_and_url_credentials(self):
        from myflames import ui
        messages = (
            "ALTER USER app IDENTIFIED BY 'secret1'",
            'failed to read mysql://app:secret2@db.example/schema',
            "IDENTIFIED BY 'secret1'; mysql://app:secret2@db.example/schema",
        )
        for message in messages:
            with self.subTest(message=message):
                log = io.StringIO()
                with patch.object(ui, 'api_response', side_effect=KeyError(message)), \
                        patch.object(ui.sys, 'stderr', log):
                    status, body, unused = self.request('/api/profiles/list')
                self.assertIn(status, (400, 500))
                self.assertEqual(body, {'error': ui._unexpected_error_message('/api/profiles/list')})
                logged = log.getvalue()
                self.assertIn('KeyError', logged)
                self.assertIn('[redacted]', logged)
                for secret in ('secret1', 'secret2'):
                    self.assertNotIn(secret, logged)
                    self.assertNotIn(secret, json.dumps(body))

    def test_handler_error_traceback_is_bounded_after_redaction(self):
        from myflames import ui
        messages = (
            "IDENTIFIED BY 'secret1'; " + 'x' * (128 * 1024),
            # The credential begins before the cap and ends well beyond it.
            # Truncating first would leave an open literal with secret bytes.
            'x' * 62000 + " IDENTIFIED BY '" + 'boundary-secret' * 1000 + "'",
        )
        for message in messages:
            with self.subTest(length=len(message)):
                log = io.StringIO()
                with patch.object(ui, 'api_response', side_effect=KeyError(message)), \
                        patch.object(ui.sys, 'stderr', log):
                    unused, body, unused_headers = self.request('/api/profiles/list')
                logged = log.getvalue()
                self.assertTrue('secret1' not in logged and 'boundary-secret' not in logged,
                                'the complete secret must be redacted before truncating the log')
                self.assertIn('[redacted]', logged)
                self.assertLessEqual(len(logged), 64 * 1024 + 128)

    def test_incomplete_report_and_wrong_plan_field_types_are_400s(self):
        from myflames import ui
        cases = (
            ('/api/report/export', {'report': {'schema_version': 'report-1.0'}, 'format': 'text'}),
            ('/api/report/export', {'report': {'schema_version': 'report-1.0', 'summary': {}},
                                    'format': 'text'}),
            ('/api/analyze', {'plan': json.dumps({'operation': 'Table scan on t',
                                                'actual_last_row_ms': 1,
                                                'actual_loops': {'invalid': 1}})}),
        )
        for path, payload in cases:
            with self.subTest(path=path, payload=payload), patch.object(ui.sys, 'stderr', io.StringIO()):
                status, body, unused = self.request(path, payload)
                self.assertEqual(status, 400)
                if path == '/api/report/export':
                    self.assertEqual(body, {'error': ui._unexpected_error_message(path)})
                else:
                    # The parser already validates numeric plan fields and
                    # keeps its useful ValueError message at this boundary.
                    self.assertIn('actual_loops', body['error'])

    def test_malformed_and_overdeep_request_json_stay_400(self):
        from myflames import ui
        for raw in ('{"broken":', '[' * 1000):
            with self.subTest(raw_length=len(raw)):
                log = io.StringIO()
                with patch.object(ui, 'api_response') as endpoint, patch.object(ui.sys, 'stderr', log):
                    status, body, unused = self.request('/api/profiles/list', raw_body=raw)
                self.assertEqual(status, 400)
                self.assertIn('error', body)
                endpoint.assert_not_called()
                self.assertEqual(log.getvalue(), '')

        loads = json.loads
        def decode(raw):
            if raw == '{"deep":true}':
                raise RecursionError('decoder stack exhausted')
            return loads(raw)

        with patch.object(ui.json, 'loads', side_effect=decode), patch.object(ui, 'api_response') as endpoint:
            status, body, unused = self.request('/api/profiles/list', raw_body='{"deep":true}')
        self.assertEqual(status, 400)
        self.assertIn('too deeply nested', body['error'])
        endpoint.assert_not_called()

    def test_overdeep_plan_text_stays_a_validation_400(self):
        plan = ('{"operation":"Filter","inputs":[' * 1000 +
                '{"operation":"Table scan on t"}' + ']}' * 1000)
        status, body, unused = self.request('/api/analyze', {'plan': plan})
        self.assertEqual(status, 400)
        self.assertIn('maximum 80 levels', body['error'])

    def test_lifecycle_add_prunes_all_owners_without_jobs_polling(self):
        from myflames import ui
        for number in range(60):
            job = types.SimpleNamespace(id='finished-%d' % number, state='done', thread=None,
                created=time.monotonic(), start=lambda: None, snapshot=lambda: {}, close=lambda: None)
            ui._lifecycle_add(self.server, job, 'target',
                {'host': 'host-%d' % (number % 6), 'port': 3306, 'user': 'app'})
            self.assertLessEqual(len(self.server.lifecycle_owners), self.server.lifecycle.KEEP)
        self.assertEqual(set(self.server.lifecycle_owners), set(self.server.lifecycle.jobs))
        self.assertEqual(len(self.server.lifecycle_owners), self.server.lifecycle.KEEP)

    def test_client_settings_internal_key_error_reaches_logged_400(self):
        from myflames import client_tools, lifecycle, ui
        secret = 'client-settings-redaction-secret'
        discovered = {'installations': [], 'shells': [], 'skipped': []}
        log = io.StringIO()
        with patch.object(client_tools, 'discover_choices', return_value=discovered), \
                patch.object(client_tools, 'choice_keys', return_value=(
                    {'mysql': None, 'mariadb': None}, {'mysql': None, 'mariadb': None})), \
                patch.object(lifecycle, '_connect'), \
                patch.object(lifecycle, 'read_server', side_effect=KeyError(secret)), \
                patch.object(ui.sys, 'stderr', log):
            status, body, unused = self.request('/api/client/tools',
                {'connection': {'host': 'db', 'port': 3306, 'user': 'app', 'password': secret}})
        self.assertEqual(status, 400)
        self.assertEqual(body, {'error': ui._unexpected_error_message('/api/client/tools')})
        self.assertIn('KeyError', log.getvalue())
        self.assertIn('[redacted]', log.getvalue())
        self.assertNotIn(secret, log.getvalue())
        self.assertNotIn('server-read', json.dumps(body))

    def test_group_probe_and_topology_member_internal_errors_mark_only_that_member_failed(self):
        from myflames import topology_group, ui
        from myflames.workspace_store import ENDPOINT_FIELDS
        secret = 'saved-group-redaction-secret'
        first_uuid = '11111111-1111-4111-8111-111111111111'
        for name in ('first', 'second'):
            self.server.store.save('profile',
                {'name': name, 'host': name + '.example', 'port': 3306, 'user': 'app'},
                name, password=secret if name == 'second' else 'first-member-password', remember=True)
        self.server.store.save_connection_group('G', ['first', 'second'])
        active = self.server.store.get('profile', 'first')
        connection = dict({key: active.get(key) for key in ENDPOINT_FIELDS},
                          profile_id='first', group_name='G')

        def fake_identity(_conn, member, read_position=True):
            if member['profile_id'] == 'second':
                raise KeyError(secret)
            member.update(engine='mysql', product='MySQL', version='8.0.36',
                          role='primary', state='ONLINE', server_uuid=first_uuid)
            return {}, 'first member read'

        for path, payload, fault in (
                ('/api/connect', {'group_name': 'G', 'active_member_profile_id': 'first',
                                 'probe_members': True},
                 patch.object(ui, '_member_live_facts', side_effect=[
                     {'server_uuid': first_uuid, 'role': 'PRIMARY', 'state': 'ONLINE'},
                     KeyError(secret)])),
                ('/api/live/topology', {'connection': connection, 'section': 'members'},
                 patch.object(topology_group, '_identity', side_effect=fake_identity))):
            with self.subTest(path=path):
                log = io.StringIO()
                with fault, patch.object(ui, '_connection_test_report', return_value={}), \
                        patch.object(ui.sys, 'stderr', log):
                    status, body, unused = self.request(path, payload)
                self.assertEqual(status, 200)
                if path == '/api/connect':
                    members = {m['profile_id']: m for m in body['group_connection']['members']}
                    self.assertTrue(members['first']['reachable'])
                    self.assertEqual(members['first']['server_uuid'], first_uuid)
                    self.assertFalse(members['second']['reachable'])
                    self.assertIsNone(members['second']['server_uuid'])
                else:
                    members = {m['profile_id']: m for m in body['members']}
                    self.assertEqual(members['first']['outcome'], 'read')
                    self.assertEqual(members['first']['server_uuid'], first_uuid)
                    self.assertEqual(members['second']['outcome'], 'failed')
                    self.assertIsNone(members['second']['server_uuid'])
                    self.assertEqual(body['summary']['read'], 1)
                    self.assertEqual(body['summary']['failed'], 1)
                logged = log.getvalue()
                self.assertEqual(logged.count('myflames: unexpected error'), 1)
                self.assertIn('KeyError', logged)
                self.assertIn('[redacted]', logged)
                self.assertNotIn(secret, logged)
                self.assertNotIn(secret, json.dumps(body))

    def test_cancel_routes_unexpected_error_after_cancel_returns_500_outcome_unknown(self):
        from myflames import csv_import_api, dbops, ui
        secret = 'cancel-outcome-secret'
        connection = {'host': 'db.example', 'port': 3306, 'user': 'dba', 'password': secret}
        cancelled = []

        def fail_snapshot():
            raise KeyError('post-cancel snapshot error ' + secret)

        def lc_cancel():
            lc_job.state = 'cancelled'
            cancelled.append('/api/lifecycle/cancel')

        def lc_snapshot():
            if '/api/lifecycle/cancel' in cancelled:
                fail_snapshot()
            return {'id': 'lc-cancel-outcome', 'state': lc_job.state}

        lc_job = types.SimpleNamespace(
            id='lc-cancel-outcome', state='running', thread=None, created=time.monotonic(),
            lock=threading.Lock(), connection_id=None, cancelled=threading.Event(),
            start=lambda: None, cancel=lc_cancel, snapshot=lc_snapshot, close=lambda: None)
        ui._lifecycle_add(self.server, lc_job, 'target', connection)

        identity = dbops.identity_endpoint(dbops.connection_identity(connection))
        live_job = types.SimpleNamespace(
            id='live-cancel-outcome', identity=identity, created=time.monotonic(), state='running',
            cancel=lambda: cancelled.append('/api/live/cancel'),
            snapshot=fail_snapshot, _request_cancel=lambda: None)
        with self.server.jobs.lock:
            self.server.jobs.jobs[live_job.id] = live_job

        progress = csv_import_api.registry(self.server).start_progress('csv-stop-outcome-0001', 'apply')
        def stop_then_fail():
            cancelled.append('/api/import/csv/stop')
            raise RuntimeError('post-kill stop error ' + secret)
        progress.killer(stop_then_fail)

        cases = (
            ('/api/lifecycle/cancel', {'id': lc_job.id, 'connection': connection}, 'KeyError'),
            ('/api/live/cancel', {'id': live_job.id, 'connection': connection}, 'KeyError'),
            ('/api/import/csv/stop', {'progress_id': 'csv-stop-outcome-0001', 'connection': connection}, 'RuntimeError'),
        )
        for path, payload, error_name in cases:
            with self.subTest(path=path):
                log = io.StringIO()
                with patch.object(ui.sys, 'stderr', log):
                    status, body, _ = self.request(path, payload)
                self.assertIn(path, cancelled)
                self.assertEqual(status, 500)
                self.assertEqual(body, {'error': ui._unexpected_error_message(path)})
                self.assertIn('outcome is unknown', body['error'])
                self.assertIn(error_name, log.getvalue())
                self.assertIn('[redacted]', log.getvalue())
                self.assertNotIn(secret, log.getvalue())
                self.assertNotIn(secret, json.dumps(body))

    def test_sql_outcome_path_disconnect_during_200_send_and_error_before_send(self):
        from myflames import ui
        secret = 'editor-send-disconnect-secret'
        orig_send = ui.WorkspaceHandler._send

        for error_type in (BrokenPipeError, ConnectionResetError, TimeoutError):
            with self.subTest(phase='during_200_send', error_type=error_type.__name__):
                sends = []
                handlers = []
                def send_with_body_disconnect(handler, status, body, *args, **kwargs):
                    sends.append(status)
                    handlers.append(handler)
                    if status == 200:
                        orig_write = handler.wfile.write
                        writes = [0]
                        def failing_write(data):
                            writes[0] += 1
                            if writes[0] == 1:
                                return orig_write(data)
                            raise error_type('client closed mid-reply')
                        handler.wfile.write = failing_write
                    return orig_send(handler, status, body, *args, **kwargs)

                log = io.StringIO()
                with patch.object(ui, 'api_response', return_value={'ok': True}), \
                        patch.object(ui.WorkspaceHandler, '_send', new=send_with_body_disconnect), \
                        patch.object(ui.sys, 'stderr', log):
                    with self.assertRaises((http.client.HTTPException, OSError)):
                        self.request('/api/editor/run', {'connection': {'password': secret}})
                self.assertEqual(sends, [200])
                self.assertEqual(log.getvalue(), '')
                self.assertTrue(handlers[-1].close_connection)

        for error_type in (BrokenPipeError, ConnectionResetError, TimeoutError, RuntimeError):
            with self.subTest(phase='before_send', error_type=error_type.__name__):
                sends = []
                def spy_send(handler, status, body, *args, **kwargs):
                    sends.append(status)
                    return orig_send(handler, status, body, *args, **kwargs)

                log = io.StringIO()
                with patch.object(ui, 'api_response', side_effect=error_type('failed before send ' + secret)), \
                        patch.object(ui.WorkspaceHandler, '_send', new=spy_send), \
                        patch.object(ui.sys, 'stderr', log):
                    status, body, _ = self.request('/api/editor/run', {'connection': {'password': secret}})
                self.assertEqual(sends, [500])
                self.assertEqual(status, 500)
                self.assertEqual(body, {'error': ui._unexpected_error_message('/api/editor/run')})
                self.assertIn('outcome is unknown', body['error'])
                self.assertIn(error_type.__name__, log.getvalue())
                self.assertIn('[redacted]', log.getvalue())
                self.assertNotIn(secret, log.getvalue())
                self.assertNotIn(secret, json.dumps(body))

        with self.subTest(phase='json_encode_error_in_200_send'):
            log = io.StringIO()
            with patch.object(ui, 'api_response', return_value={'bad': object()}), \
                    patch.object(ui.sys, 'stderr', log):
                status, body, _ = self.request('/api/editor/run', {'connection': {'password': secret}})
            self.assertEqual(status, 500)
            self.assertEqual(body, {'error': ui._unexpected_error_message('/api/editor/run')})
            self.assertIn('TypeError', log.getvalue())
            self.assertNotIn(secret, log.getvalue())

    def test_investigation_save_update_list_and_delete(self):
        value = {'schema_version': 'investigation-1.0', 'name': 'Before index',
                 'plans': [{'id': 'base', 'name': 'Original', 'raw': PLAN}], 'baseline_id': 'base'}
        status, first, _ = self.request('/api/investigations/save', {'value': value})
        self.assertEqual(status, 200)
        self.assertTrue(first['id'])
        value['name'] = 'After index'
        status, changed, _ = self.request('/api/investigations/save', {'id': first['id'], 'value': value})
        self.assertEqual(status, 200)
        status, listed, _ = self.request('/api/investigations/list')
        self.assertEqual(status, 200)
        # A summary page; the complete bundle comes from investigations/get.
        self.assertEqual(listed, {'schema_version': 'investigation-list-1.0', 'next_cursor': None, 'items': [{
            'id': first['id'], 'updated_at': changed['updated_at'], 'schema_version': 'investigation-summary-1.0',
            'name': 'After index', 'notes': '', 'tags': [], 'baseline_id': 'base',
            'plans': [{'id': 'base', 'name': 'Original'}]}]})
        status, bundle, _ = self.request('/api/investigations/get', {'id': first['id']})
        self.assertEqual((status, bundle), (200, changed))
        self.assertEqual(changed['name'], 'After index')
        status, body, _ = self.request('/api/investigations/delete', {'id': first['id']})
        self.assertEqual((status, body), (200, {'deleted': True}))
        self.assertEqual(self.request('/api/investigations/list')[1],
                         {'schema_version': 'investigation-list-1.0', 'items': [], 'next_cursor': None})
        status, body, _ = self.request('/api/investigations/get', {'id': first['id']})
        self.assertEqual(status, 400)
        self.assertIn('no longer exists', body['error'])

    def test_profile_save_drops_secrets_and_kind_isolation_holds(self):
        value = {'name': 'Reporting', 'host': 'db.example', 'port': '3307', 'user': 'reader',
                 'password': 'secret-for-test', 'connection': {'password': 'secret-for-test'}}
        status, saved, _ = self.request('/api/profiles/save', {'value': value})
        self.assertEqual(status, 200)
        self.assertNotIn('secret-for-test', json.dumps(saved))
        self.assertNotIn('password', saved)
        self.assertEqual(saved['port'], 3307)
        self.assertNotIn(b'secret-for-test', self.path.read_bytes())
        self.request('/api/investigations/delete', {'id': saved['id']})
        self.assertEqual(self.request('/api/profiles/list')[1]['items'], [saved])
        self.request('/api/profiles/delete', {'id': saved['id']})
        self.assertEqual(self.request('/api/profiles/list')[1]['items'], [])

    def test_workspace_routes_require_current_token_and_same_origin(self):
        routes = ('/api/investigations/list', '/api/investigations/get', '/api/investigations/save', '/api/profiles/save',
                  '/api/live/start', '/api/live/status', '/api/live/cancel', '/api/connection/test')
        with patch.object(self.server.jobs, 'start') as start, \
                patch('myflames.connector.MySQLConnection') as connect:
            for route in routes:
                for headers in ({'X-Myflames-Token': ''}, {'Origin': 'https://example.com'}, {'Host': 'evil.test'}):
                    with self.subTest(route=route, headers=headers):
                        self.assertEqual(self.request(route, headers=headers)[0], 403)
            start.assert_not_called()
            connect.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_live_start_status_and_cancel_share_server_job_manager(self):
        job = MagicMock()
        job.snapshot.return_value = {'id': 'job-1', 'state': 'running', 'progress': 1}
        value = {'connection': {'host': 'localhost'}, 'query': 'SELECT 1'}
        with patch.object(self.server.jobs, 'start', return_value={'id': 'job-1', 'state': 'queued'}) as start, \
                patch.object(self.server.jobs, 'get', return_value=job) as get:
            status, body, _ = self.request('/api/live/start', value)
            self.assertEqual((status, body), (200, {'id': 'job-1', 'state': 'queued'}))
            start.assert_called_once_with(value)
            status, body, _ = self.request('/api/live/status', {'id': 'job-1'})
            self.assertEqual((status, body['state']), (200, 'running'))
            job.cancel.side_effect = lambda: setattr(job.snapshot, 'return_value', {'id': 'job-1', 'state': 'cancelled'})
            status, body, _ = self.request('/api/live/cancel', {'id': 'job-1'})
            self.assertEqual((status, body['state']), (200, 'cancelled'))
            job.cancel.assert_called_once_with()
            self.assertEqual(get.call_count, 2)
        self.assertFalse(self.path.exists())

    def test_invalid_live_parameters_and_missing_jobs_return_400(self):
        values = ({}, {'connection': {}, 'query': 'DELETE FROM t'},
                  {'connection': {}, 'query': 'SELECT 1', 'repeat': 0},
                  {'connection': {}, 'query': 'SELECT 1', 'parameters': []},
                  {'connection': {'port': 0}, 'query': 'SELECT 1'},
                  {'connection': {}, 'query': 'SELECT 1', 'timeout': 301})
        with patch('myflames.live.MySQLConnection') as connection:
            for value in values:
                with self.subTest(value=value):
                    status, body, _ = self.request('/api/live/start', value)
                    self.assertEqual(status, 400)
                    self.assertIn('error', body)
            connection.assert_not_called()
        for route in ('/api/live/status', '/api/live/cancel'):
            self.assertEqual(self.request(route, {'id': 'unknown'})[0], 400)

    def test_connection_test_uses_validated_options_and_returns_schemas(self):
        with patch('myflames.connector.MySQLConnection') as connection:
            conn = connection.return_value.__enter__.return_value
            conn.server_version.return_value = '9.7.0'
            conn.is_mariadb.return_value = False
            conn.server_version_comment.return_value = 'MySQL Community Server - GPL'
            conn.query_rows.return_value = [['inventory'], [], ['reporting']]
            status, body, _ = self.request('/api/connection/test', {'connection': {
                'host': 'localhost', 'port': '3307', 'password': 'secret-for-test'}})
            self.assertEqual((status, body), (200, {'version': '9.7.0', 'engine': 'mysql', 'flavor': 'mysql',
                                                    'product': 'MySQL', 'schemas': ['inventory', 'reporting'],
                                                    'databases': ['inventory', 'reporting'],
                                                    'databases_truncated': False}))
            self.assertEqual(connection.call_args[1]['port'], 3307)
            self.assertEqual(connection.call_args[1]['password'], 'secret-for-test')
            conn.query_rows.assert_called_once_with('SHOW DATABASES')
            connection.return_value.__exit__.assert_called_once()
        self.assertFalse(self.path.exists())

    def test_connection_errors_are_json_client_errors(self):
        with patch('myflames.connector.MySQLConnection', side_effect=ConnectorError('Connection refused')):
            status, body, _ = self.request('/api/connection/test', {'connection': {}})
        self.assertEqual(status, 400)
        self.assertIn('Connection refused', body['error'])

    def test_invalid_saved_records_return_400(self):
        for path, value in (('/api/investigations/save', {}),
                            ('/api/investigations/save', {'schema_version': 'investigation-1.0', 'plans': [{}]}),
                            ('/api/profiles/save', {'host': 'a\npassword=b'})):
            with self.subTest(path=path, value=value):
                status, body, _ = self.request(path, {'value': value})
                self.assertEqual(status, 400)
                self.assertIn('error', body)
        self.assertFalse(self.path.exists())

    @unittest.skipUnless((ASSET_DIR / 'index.html').is_file(),
                         'Workspace assets are not built; run `npm ci` and `npm run build` in apps/workspace')
    def test_bundled_project_logo_is_served_as_png(self):
        logos = list((ASSET_DIR / 'assets').glob('myflames-logo.png'))
        self.assertEqual(len(logos), 1)
        status, body, headers = self.request('/assets/' + logos[0].name, method='GET')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'].split(';')[0], 'image/png')
        self.assertEqual(body, logos[0].read_bytes())
        self.assertEqual(body, (Path(__file__).resolve().parents[1] / 'docs/brand/myflames-app-logo.png').read_bytes())
        self.assertTrue(body.startswith(b'\x89PNG\r\n\x1a\n'))
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')

    def test_earlier_hashed_logo_urls_are_gone(self):
        # The workspace is not GA: logo URLs of earlier bundles are not kept.
        for name in ('myflames-BpuTIrWf.jpeg', 'logo-BpuTIrWf.jpeg',
                     'myflames-icon-v2-CFba5YPm.png'):
            with self.subTest(name=name):
                status, unused, unused_headers = self.request('/assets/' + name, method='GET')
                self.assertEqual(status, 404)

    @unittest.skipUnless((ASSET_DIR / 'index.html').is_file(),
                         'Workspace assets are not built; run `npm ci` and `npm run build` in apps/workspace')
    def test_page_favicon_versions_stable_logo_url(self):
        status, body, _ = self.request('/', method='GET')
        self.assertEqual(status, 200)
        self.assertIn(b'href="/assets/myflames-logo.png?v=4"', body)
        status, icon, headers = self.request('/assets/myflames-logo.png?v=2', method='GET')
        self.assertEqual(status, 200)
        self.assertEqual(icon, (ASSET_DIR / 'assets' / 'myflames-logo.png').read_bytes())
        self.assertEqual(headers['Content-Type'].split(';')[0], 'image/png')

    def test_direct_workspace_routes_require_workspace_context(self):
        with self.assertRaisesRegex(ValueError, 'running workspace'):
            api_response('/api/investigations/list', {})

    def test_analysis_returns_normalized_imported_capture(self):
        status, body, _ = self.request('/api/analyze', {'plan': PLAN, 'capture': {
            'mode': 'analyze', 'password': 'secret-for-test', 'connection': {'password': 'secret-for-test'}}})
        self.assertEqual(status, 200)
        self.assertEqual(body['capture']['runs'], [])
        self.assertEqual(body['capture']['warnings'], [])
        self.assertEqual(body['capture']['measurements']['count'], 0)
        self.assertNotIn('secret-for-test', json.dumps(body))

    def test_bad_imported_capture_returns_400_for_analysis_and_save(self):
        for capture in ([], {'runs': 1}, {'runs': [None]}, {'variables': []},
                        {'runs': [{'total_time_ms': float('nan')}]},
                        {'runs': [{'plan': '{}'}]}, {'warnings': 'warning'}):
            with self.subTest(capture=capture):
                status, body, _ = self.request('/api/analyze', {'plan': PLAN, 'capture': capture})
                self.assertEqual(status, 400)
                self.assertIn('error', body)
                value = {'schema_version': 'investigation-1.0', 'plans': [
                    {'id': 'plan', 'raw': PLAN, 'capture': capture}]}
                status, body, _ = self.request('/api/investigations/save', {'value': value})
                self.assertEqual(status, 400)
                self.assertIn('error', body)
        self.assertFalse(self.path.exists())
