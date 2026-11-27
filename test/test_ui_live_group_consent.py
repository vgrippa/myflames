"""Last-login revocation on live reads and ProxySQL/Readyset reviewed sends.

Replay only the client process boundary. The connection, saved credentials,
group store, policy, review tokens and audit records are real.
"""
import contextlib
import copy
import json
import threading
import types
from unittest import mock

import test_connection_groups as groups
import _cluster_fixtures as cluster_fixtures
from test_proxysql import FakeAdmin
from test_readyset import FakeReadyset
from myflames import connection_groups, connector, dbops, live, ui
from myflames.accounts_api import _group_login_guard
from myflames.connection_groups import ProfileReadError


class LiveConsentTests(groups.GroupCase):
    def setUp(self):
        super(LiveConsentTests, self).setUp()
        self.profile = self.save('active', 'G', host='127.0.0.1', user='root', type='development')
        self.connection = dict(self.request(self.profile), group_name='G', password='typed-password')
        self.workspace = types.SimpleNamespace(store=self.store)
        self.invocations = []

    def remove_member(self):
        self.store.assign_profile_group(['active'], '')

    @contextlib.contextmanager
    def client(self, server=None, after_login=None):
        def invoke(argv, **options):
            sql = options['input'].decode() if 'input' in options else argv[argv.index('-e') + 1]
            self.invocations.append(sql)
            if server is None:
                text = ''
            elif 'input' in options:
                text = server.run_write(sql)
            else:
                text = server.run(sql, extra_flags=argv)
            if after_login is not None:
                after_login(sql)
            return types.SimpleNamespace(returncode=0, stdout=(text or '').encode(), stderr=b'')

        with mock.patch.object(connector.MySQLConnection, '_resolve_binary', return_value='/nonexistent/mysql'), \
                mock.patch.object(connector, '_run_bounded', side_effect=invoke):
            yield

    def plan(self, kind, server):
        action = 'server_offline_soft' if kind == 'proxysql' else 'enter_maintenance_mode'
        target = {'config': [30, 'mf-ic-2', 3306]} if kind == 'proxysql' else None
        with self.client(server):
            return ui.api_response('/api/live/%s/plan' % kind,
                                   dict(connection=self.connection, action=action, target=target), self.workspace)

    def apply(self, kind, server, plan):
        reviewed = dict(plan)
        token = reviewed.pop('review_token')
        with self.client(server):
            return ui.api_response('/api/live/%s/action' % kind,
                                   dict(connection=self.connection, plan=reviewed, review_token=token,
                                        confirmed=True, typed=reviewed.get('typed_guard')), self.workspace)

    def servers(self):
        proxy = FakeAdmin()
        self.addCleanup(proxy.db.close)
        return [('proxysql', proxy), ('readyset', FakeReadyset())]

    def test_reviewed_proxy_and_readyset_sends_stop_after_pending_audit_revokes_member(self):
        attempt = dbops.AuditTrail.attempt

        def revoke(audit, *args, **kwargs):
            result = attempt(audit, *args, **kwargs)
            self.remove_member()
            return result

        for kind, server in self.servers():
            with self.subTest(kind=kind):
                self.store.assign_profile_group(['active'], 'G')
                plan = self.plan(kind, server)
                self.invocations.clear()
                with mock.patch.object(dbops.AuditTrail, 'attempt', revoke):
                    result = self.apply(kind, server, plan)
                self.assertEqual((result['status'], result['sent'], result['states']),
                                 ('refused', [], ['not_sent'] * len(plan['statements'])), result)
                self.assertEqual(server.writes, [])
                self.assertTrue(self.invocations, 'the fresh plan read happened before audit preparation')
                record = next(r for r in self.store.audit_records() if r['id'] == result['audit_id'])
                self.assertEqual((record['status'], record['outcome']), ('refused', 'changed'), record)
                self.assertEqual(record['statements'], list(plan.get('session_guard') or []) + plan['statements'])

    def test_reviewed_proxy_and_readyset_refuse_membership_change_during_secret_lookup(self):
        fill = self.store.connection_with_saved_password

        def revoke(connection):
            filled = fill(connection)
            self.remove_member()
            return filled

        for kind, server in self.servers():
            with self.subTest(kind=kind):
                self.store.assign_profile_group(['active'], 'G')
                plan = self.plan(kind, server)
                self.invocations.clear()
                with mock.patch.object(self.store, 'connection_with_saved_password', side_effect=revoke):
                    result = self.apply(kind, server, plan)
                self.assertEqual((result['status'], result['sent']), ('refused', []), result)
                self.assertEqual(self.invocations, [])
                self.assertEqual(server.writes, [])
                record = next(r for r in self.store.audit_records() if r['id'] == result['audit_id'])
                self.assertEqual((record['status'], record['outcome']), ('refused', 'changed'))

    def test_proxy_and_readyset_plans_stop_before_second_login(self):
        for kind, server in self.servers():
            with self.subTest(kind=kind):
                self.store.assign_profile_group(['active'], 'G')
                self.invocations.clear()
                with self.client(server, after_login=lambda sql: self.remove_member()):
                    with self.assertRaises(ProfileReadError):
                        ui.api_response('/api/live/%s/plan' % kind,
                                        dict(connection=self.connection, action='enter_maintenance_mode'), self.workspace)
                self.assertEqual(len(self.invocations), 1)
                self.assertEqual(server.writes, [])

    def test_grouped_proxy_and_readyset_actions_still_send_when_consent_is_current(self):
        for kind, server in self.servers():
            with self.subTest(kind=kind):
                plan = self.plan(kind, server)
                result = self.apply(kind, server, plan)
                self.assertEqual(result['status'], 'done', result)
                self.assertEqual(server.writes, ['\n'.join(list(plan.get('session_guard') or []) + plan['statements'])])

    def test_every_synchronous_live_reader_stops_before_a_second_client_login(self):
        # Each collector seam attempts two real connector invocations. This
        # table checks dispatch coverage, including all server-view paths.
        collectors = {
            'index-suggestion': 'index_hint.read_suggestion', 'top': 'discover.top_report',
            'slow-log': 'slow_queries.slow_log_table_report', 'digest-tables': 'discover.digest_tables',
            'digest-histogram': 'discover.digest_histogram', 'running': 'discover.running_report',
            'ddl-progress': 'ddl_progress.ddl_progress_report', 'sessions/history': 'sessions.sessions_history',
            'sessions': 'sessions.sessions_report', 'innodb': 'innodb_status.innodb_report',
            'error-log': 'error_log.error_log_report', 'cluster': 'cluster.read_status',
            'proxysql': 'proxysql.read_status', 'readyset': 'readyset.read_status',
            'readyset/explain-cache': 'readyset.explain_cache', 'readyset/export-caches': 'readyset.export_caches',
            'proxysql/digests': 'proxysql_digests.read_digests', 'locks': 'locks.lock_report',
            'health': 'health.health_report', 'watch': 'watch.watch_report', 'watch/capture': 'watch.capture_evidence',
            'memory': 'memory.memory_report', 'waits': 'waits.waits_report', 'indexes': 'indexes.index_report',
            'tables': 'schema_health.table_report', 'schemas': 'perf_common.schema_catalog',
            'io': 'io_stats.io_report', 'config': 'config_review.config_report',
            'instruments': 'ps_instruments.instruments_report', 'extensions': 'extensions.extensions_report',
            'report': 'report.analysis_report',
            'explain-connection': 'discover.explain_connection', 'replication': 'replication.read_status',
            'catalog': 'schema_browser.catalog_report', 'table-structure': 'schema_browser.table_structure',
            'object-search': 'schema_browser.object_search', 'variable': 'variables.read_variable',
            'variable/plan': 'variables.plan_change', 'readiness': 'readiness.read_readiness',
        }
        self.assertTrue(set(ui._SERVER_VIEW_PATHS).issubset({'/api/live/' + name for name in collectors}))

        def two_reads(conn, *args, **kwargs):
            conn.query_rows("SELECT 'first'")
            conn.query_rows("SELECT 'second'")
            self.fail('the second login must be refused')

        for name, collector in collectors.items():
            with self.subTest(path=name):
                self.store.assign_profile_group(['active'], 'G')
                self.invocations.clear()
                payload = dict(connection=self.connection, schema='shop', table='orders', connection_ids=[1],
                               id=1, text='orders', name='max_connections', value=100)
                with self.client(after_login=lambda sql: self.remove_member()), \
                        mock.patch('myflames.' + collector, side_effect=two_reads):
                    with self.assertRaises(ProfileReadError):
                        ui.api_response('/api/live/' + name, payload, self.workspace)
                self.assertEqual(self.invocations, ["SELECT 'first'"])

    def test_table_columns_and_connection_test_recheck_consent_after_setup(self):
        build = connector.MySQLConnection._build_argv

        def revoke(conn, *args, **kwargs):
            result = build(conn, *args, **kwargs)
            self.remove_member()
            return result

        for path in ('/api/live/table-columns', '/api/connection/test'):
            with self.subTest(path=path):
                self.store.assign_profile_group(['active'], 'G')
                self.invocations.clear()
                with self.client(), mock.patch.object(connector.MySQLConnection, '_build_argv', revoke):
                    with self.assertRaises(ProfileReadError):
                        ui.api_response(path, dict(connection=self.connection, schema='shop', table='orders'),
                                        self.workspace)
                self.assertEqual(self.invocations, [])

    def test_read_consent_survives_policy_change_but_not_group_recreation(self):
        guard = _group_login_guard(self.store, self.connection)
        self.store.set_profile_safety('active', read_only=True)
        guard()  # Read consent does not bind a write policy.
        self.store.delete_connection_group('G')
        self.store.save_connection_group('G', ['active'])
        with self.assertRaises(ProfileReadError):
            guard()

    def test_legacy_capture_start_installs_guard_and_refuses_first_revoked_login(self):
        self.workspace.jobs = live.LiveJobs()
        with mock.patch.object(threading, 'Thread') as thread:
            started = ui.api_response('/api/live/start', dict(connection=self.connection, query='SELECT 1'),
                                      self.workspace)
            thread.return_value.start.assert_called_once_with()
        job = self.workspace.jobs.jobs[started['id']]
        self.remove_member()
        with self.client():
            job.run()
        self.assertEqual(job.state, 'failed')
        self.assertEqual(self.invocations, [])
        self.assertNotIn('password', job.options)

    def test_legacy_capture_checks_script_spawn_and_cancellation_helper(self):
        guard = _group_login_guard(self.store, self.connection)
        job = live.LiveJob(dict(connection=self.connection, query='SELECT 1'), login_guard=guard)
        self.remove_member()
        with self.client(), mock.patch.object(live.subprocess, 'Popen') as spawn:
            conn = job._open_connection()
            with self.assertRaises(ProfileReadError):
                job._run_script_isolated(conn, 'SELECT 1', 'marker', {}, '/unused-defaults')
            job.connection_id = 41
            job.cancel()
        spawn.assert_not_called()
        self.assertEqual(self.invocations, [])
        self.assertTrue(job.cancelled.is_set())


class DirectTopologyStartTests(groups.GroupCase):
    def test_overlapping_direct_reads_keep_newer_start_when_older_finishes_last(self):
        for kind in ('cluster', 'replication'):
            with self.subTest(kind=kind):
                profile = self.save('active-' + kind, 'G')
                connection = dict(self.request(profile), group_name='G')
                workspace = types.SimpleNamespace(store=self.store)
                cluster_report = cluster_fixtures.report('mysql-8.4-standalone') if kind == 'cluster' else None
                started, release = threading.Event(), threading.Event()
                reports, failures = [], []
                calls = []

                def read(conn, *args, **kwargs):
                    older = not calls
                    calls.append(True)
                    if older:
                        started.set()
                        if not release.wait(5):
                            raise AssertionError('newer read did not finish')
                    uuid = 'older-node' if older else 'newer-node'
                    if kind == 'cluster':
                        report = copy.deepcopy(cluster_report)
                        report['server']['server_uuid'] = uuid
                        return report
                    return {'available': False, 'server_uuid': uuid, 'channels': []}

                def request():
                    try:
                        reports.append(ui.api_response('/api/live/' + kind, {'connection': connection}, workspace))
                    except BaseException as exc:
                        failures.append(exc)

                with mock.patch('myflames.%s.read_status' % kind, side_effect=read), \
                        mock.patch('myflames.cluster.attach_topology_tiers'):
                    older = threading.Thread(target=request)
                    older.start()
                    try:
                        self.assertTrue(started.wait(5), 'older read must start first')
                        request()
                        self.assertEqual(self.store.get('profile', profile['id'])['topology_identity']['server_uuid'],
                                         'newer-node')
                    finally:
                        release.set()
                        older.join(5)
                    self.assertFalse(older.is_alive())
                self.assertEqual(failures, [])
                self.assertEqual(len(reports), 2)
                self.assertEqual(self.store.get('profile', profile['id'])['topology_identity']['server_uuid'],
                                 'newer-node')
                self.assertNotIn('_topology_observation', json.dumps(reports))

    def test_failed_observation_reservation_cannot_redate_a_completed_read(self):
        profile = self.save('active', 'G')
        with mock.patch.object(self.store, 'begin_topology_observation', side_effect=OSError('disk unavailable')):
            identity = connection_groups.begin_read(self.store, profile)
        newer = self.store.begin_topology_observation()
        self.store.record_topology_identity('active', {'server_uuid': 'newer-node'}, observation=newer)
        report = {'server_uuid': 'older-node', 'channels': []}
        connection_groups.finish_read(self.store, report, self.request(profile), [profile], profile, identity)
        self.assertEqual(self.store.get('profile', 'active')['topology_identity']['server_uuid'], 'newer-node')

    def test_reader_without_store_token_retains_start_order_until_publication(self):
        profile = self.save('active', 'G')

        @connection_groups.grouped_read
        def read(conn, profiles=(), current_profile_id=None, _identity=None):
            return {'server_uuid': conn, 'channels': []}

        earlier, later = {}, {}
        old_report = read('older-node', profiles=[profile], current_profile_id='active', _identity=earlier)
        new_report = read('newer-node', profiles=[profile], current_profile_id='active', _identity=later)
        connection_groups.finish_read(self.store, new_report, self.request(profile), [profile], profile, later)
        connection_groups.finish_read(self.store, old_report, self.request(profile), [profile], profile, earlier)
        self.assertEqual(self.store.get('profile', 'active')['topology_identity']['server_uuid'], 'newer-node')
