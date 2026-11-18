"""Group revocation through real apply endpoints and connector login checks.

Only the client process boundary is replayed. SQL classification, saved
credentials, policy, token claims, per-login guards and audits stay real.
"""
import contextlib
import types
from unittest import mock

import test_cluster_group_actions as group_actions
import test_connection_groups as groups
import test_locks as lock_tests
import test_replication as replication_tests
import test_variables as variable_tests
from myflames import connector, dbops, editor_session as editor, lifecycle, replication, table_data, ui
from myflames.connection_groups import ProfileReadError
from myflames.live import connection_options
import test_lifecycle_cli as lifecycle_tests


class GroupConsentTests(groups.GroupCase):
    def setUp(self):
        super(GroupConsentTests, self).setUp()
        self.workspace = types.SimpleNamespace(store=self.store)
        self.profile = self.save('active', 'G', host='127.0.0.1', user='root', type='development')
        self.connection = dict(self.request(self.profile), group_name='G')
        self.invocations = []

    @contextlib.contextmanager
    def client(self, replay, revoke_at_write=False):
        """Exercise MySQLConnection itself, including the last spawn check."""
        original = connector.MySQLConnection._run_write_with_defaults

        def invoke(argv, **options):
            sql = options['input'].decode() if 'input' in options else argv[argv.index('-e') + 1]
            self.invocations.append(sql)
            if sql == 'SELECT @@version_comment':
                text = 'MySQL Community Server (GPL)\n'
            elif sql.startswith("/*myflames:privileges*/ SELECT 'myflames_who'"):
                # Explicit synthetic login evidence. Parse it through the real
                # privileges reader instead of replacing the privilege gate.
                text = ('myflames_who\troot@localhost\troot@localhost\tNONE\t8.4.11\n'
                        'GRANT SUPER, SYSTEM_VARIABLES_ADMIN, SESSION_VARIABLES_ADMIN, '
                        'REPLICATION_SLAVE_ADMIN ON *.* TO `root`@`localhost`\n'
                        'server_uuid\t11111111-2222-3333-4444-555555555555\n'
                        'server_id\t1\nhostname\tfixture-db\nport\t3306\n')
            elif 'information_schema.APPLICABLE_ROLES' in sql:
                text = ''
            elif 'input' in options:
                text = replay.run_write(sql, extra_flags=list(dbops.WRITE_CLIENT_FLAGS))
            else:
                text = replay.run(sql, extra_flags=['--comments'] + (['--xml'] if '--xml' in argv else []))
            return types.SimpleNamespace(returncode=0, stdout=(text or '').encode(), stderr=b'')

        def write(conn, sql, flags, timeout, defaults):
            if revoke_at_write:
                # Defaults and credentials have already been prepared. The
                # final connector login check must still revoke this send.
                self.store.delete_connection_group('G')
            return original(conn, sql, flags, timeout, defaults)

        with mock.patch.object(connector.MySQLConnection, '_resolve_binary', return_value='/nonexistent/mysql'), \
                mock.patch.object(connector, '_run_bounded', side_effect=invoke), \
                mock.patch.object(connector.MySQLConnection, '_run_write_with_defaults', write):
            yield

    def kill_payload(self, scenario):
        safety = dbops.connection_safety(self.profile, self.connection, store=self.store)
        expect = lock_tests.bound_kill_expect(scenario.conn, connection=self.connection, safety=safety)
        payload = dict(connection=self.connection, connection_id=41, mode='connection', expect=expect)
        review = ui.api_response('/api/live/kill/prepare', payload, self.workspace)
        return dict(payload, review_token=review['review_token'], confirmed=True)

    def test_consent_recreated_group_refuses_the_original_kill_review(self):
        scenario = lock_tests.KillScenario()
        payload = self.kill_payload(scenario)
        self.store.delete_connection_group('G')
        self.store.save_connection_group('G', [self.profile['id']])
        with self.client(scenario.conn):
            result = ui.api_response('/api/live/kill', payload, self.workspace)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(self.invocations, [])
        self.assertFalse(scenario.killed)

    def test_consent_group_deleted_inside_kill_login_sends_nothing(self):
        scenario = lock_tests.KillScenario()
        payload = self.kill_payload(scenario)
        with self.client(scenario.conn, revoke_at_write=True):
            result = ui.api_response('/api/live/kill', payload, self.workspace)
        self.assertEqual(result['status'], 'refused', result)
        self.assertTrue(self.invocations, 'the endpoint performed its real preflight reads')
        self.assertFalse(scenario.killed)
        self.assertFalse(any('KILL CONNECTION' in sql for sql in self.invocations))
        record = self.store.audit_records()[0]
        self.assertEqual(record['status'], 'refused')

    def test_consent_group_deleted_inside_persist_login_sends_nothing(self):
        replay, data = variable_tests.replay('mysql-8.4', 'persist')
        args = data['args']
        payload = dict(connection=self.connection, name=args['name'], value=args['value'], mode=args['mode'])
        with self.client(replay):
            plan = ui.api_response('/api/live/variable/plan', payload, self.workspace)
        self.assertEqual(plan['status'], 'ready', plan)
        self.invocations.clear()
        with self.client(replay, revoke_at_write=True):
            result = ui.api_response('/api/live/variable/apply',
                                     dict(payload, expect=plan['expect'], confirmed=True), self.workspace)
        self.assertEqual(result['status'], 'refused', result)
        self.assertTrue(self.invocations)
        self.assertFalse(any('SET PERSIST ' in sql for sql in self.invocations))
        self.assertEqual(self.store.audit_records()[0]['status'], 'refused')

    def test_consent_group_deleted_inside_replication_skip_login_sends_nothing(self):
        report, _ = replication_tests.status('mysql-8.4-gtid-error')
        plan = replication.plan_skip(report, '')
        details = dict(channel='', expect=plan['identity'], statements=plan['statements'])
        identity = ui._review_identity(self.connection, self.workspace)
        token = ui._review_tokens(self.workspace).issue('replication_skip', identity, details)
        replay, _ = replication_tests.replay('replication', 'mysql-8.4-gtid-skip')
        with self.client(replay, revoke_at_write=True):
            result = ui.api_response('/api/live/replication/skip',
                dict(details, connection=self.connection, review_token=token, confirmed=True), self.workspace)
        self.assertEqual(result['status'], 'refused', result)
        self.assertFalse(result.get('sent'))
        self.assertTrue(self.invocations)
        self.assertFalse(any('SET GTID_NEXT' in sql or 'START REPLICA SQL_THREAD' in sql for sql in self.invocations))
        self.assertEqual(self.store.audit_records()[0]['status'], 'refused')

    def discovery_race(self, change):
        self.store.assign_profile_group(['active'], 'G')
        self.save('peer', 'G')
        store, contacts = self.store, []

        class Client(groups._FakeMemberConn):
            def __init__(self, **options):
                super(Client, self).__init__(**options)
                if self.host == 'peer.example':
                    change()

            def __enter__(self):
                self._login_guard()
                contacts.append(self.host)
                return super(Client, self).__enter__()

            def server_version(self):
                self._login_guard()
                return super(Client, self).server_version()

            def server_version_comment(self):
                self._login_guard()
                return super(Client, self).server_version_comment()

            def query_rows(self, sql):
                self._login_guard()
                return super(Client, self).query_rows(sql)

        groups._FakeMemberConn.reset()
        with mock.patch.object(connector, 'MySQLConnection', Client):
            result = ui.api_response('/api/connect', dict(group_name='G', active_member_profile_id='active',
                                                         probe_members=True), self.workspace)
        peer = next(p for p in result['group_connection']['members'] if p['profile_id'] == 'peer')
        self.assertFalse(peer['reachable'], peer)
        self.assertNotIn('peer.example', contacts)

    def test_consent_active_member_removed_during_discovery_stops_peer_logins(self):
        self.discovery_race(lambda: self.store.assign_profile_group(['active'], ''))

    def test_consent_peer_endpoint_edited_during_discovery_stops_old_endpoint_login(self):
        def edit():
            value = dict(self.store.get('profile', 'peer'), host='edited-peer.example')
            self.store.save('profile', value, 'peer', password='new-test-password', remember=True)
        self.discovery_race(edit)

    def test_consent_recreated_group_refuses_the_original_cluster_review(self):
        # Reuse the synthetic three-member scenario while executing the actual
        # plan and action endpoint, registry and saved target checks.
        case = group_actions.GroupActionTests(methodName='runTest')
        case.setUp()
        self.addCleanup(case.doCleanups)
        plan = case.plan()
        case.store.delete_connection_group('G')
        case.store.save_connection_group('G', [p['id'] for p in case.profiles])
        case.opened.clear()
        result = case.apply(plan)
        self.assertEqual((result['status'], result['sent']), ('refused', []))
        self.assertEqual(case.opened, [])
        case.assert_no_writes()


class RemainingWriteConsentTests(groups.GroupCase):
    """Real policy, review tokens and login guards; no server is contacted."""

    def setUp(self):
        super(RemainingWriteConsentTests, self).setUp()
        self.profile = self.save('member', 'G', host='127.0.0.1', user='root', type='development')
        self.connection = dict(self.request(self.profile), group_name='G')
        self.workspace = types.SimpleNamespace(store=self.store, editor=editor.EditorSessions(reap_every=0),
                                               table_data=object(), lifecycle=types.SimpleNamespace(
            runner=lambda operation, options: contextlib.nullcontext(
                lifecycle.LifecycleJob(operation, lambda job: None, options))))
        self.addCleanup(self.workspace.editor.close)

    def revoke(self, mode):
        if mode == 'removed':
            self.store.assign_profile_group([self.profile['id']], '')
        elif mode == 'read_only':
            self.store.set_profile_safety(self.profile['id'], read_only=True)
        else:
            self.save('strict-peer', 'G', type='production')

    def assert_refused(self):
        record = self.store.audit_records()[0]
        self.assertEqual((record['status'], record['outcome']), ('refused', 'changed'), record)

    @contextlib.contextmanager
    def editor_login_race(self, mode):
        """Revoke after option-file preparation and feature discovery."""
        def features(*args, **kwargs):
            self.revoke(mode)
            return {}

        with mock.patch.object(connector.MySQLConnection, '_resolve_binary', return_value='/nonexistent/mysql'), \
                mock.patch.object(editor, '_client_features', side_effect=features), \
                mock.patch.object(editor.subprocess, 'Popen') as spawn:
            yield spawn
            spawn.assert_not_called()

    def editor_apply_race(self, sql, mode):
        payload = dict(connection=self.connection, tab_id='consent', sql=sql)
        review = ui.api_response('/api/editor/run', payload, self.workspace)
        self.assertEqual(review['status'], 'needs_confirmation', review)
        payload.update(confirmed=True, confirm_token=review['confirmation']['token'])
        with self.editor_login_race(mode):
            with self.assertRaises(ProfileReadError):
                ui.api_response('/api/editor/run', payload, self.workspace)
        self.assertIsNone(self.workspace.editor.get('consent'))
        self.assert_refused()

    def test_editor_ddl_removed_at_final_login_sends_nothing(self):
        self.editor_apply_race('CREATE TABLE consent_t (id INT)', 'removed')

    def test_editor_ddl_read_only_at_final_login_sends_nothing(self):
        self.editor_apply_race('CREATE TABLE consent_t (id INT)', 'read_only')

    def test_editor_ddl_stricter_peer_at_final_login_sends_nothing(self):
        self.editor_apply_race('CREATE TABLE consent_t (id INT)', 'stricter')

    def test_editor_safe_updates_removed_at_final_login_sends_nothing(self):
        payload = dict(connection=self.connection, tab_id='safe', safe_updates=False)
        with self.editor_login_race('removed'):
            with self.assertRaises(ProfileReadError):
                ui.api_response('/api/editor/open', payload, self.workspace)
        self.assert_refused()

    def test_editor_open_refusal_does_not_audit_an_unrequested_setting(self):
        payload = dict(connection=self.connection, tab_id='open')
        with self.editor_login_race('removed'):
            with self.assertRaises(ProfileReadError):
                ui.api_response('/api/editor/open', payload, self.workspace)
        self.assert_refused()
        self.assertEqual(self.store.audit_records()[0]['statements'], [])

    def test_table_data_open_removed_at_final_login_sends_nothing(self):
        payload = dict(connection=self.connection, schema='app', table='t')
        facts = editor._connection_facts(payload, self.store)
        with self.editor_login_race('removed'):
            with self.assertRaises(ProfileReadError):
                table_data._open(payload, self.workspace, lambda value: value, *facts)
        self.assert_refused()

    def test_table_data_privilege_login_revocation_cannot_become_unknown_permission(self):
        payload = dict(connection=self.connection)
        options, identity, safety, _label = editor._connection_facts(payload, self.store)
        guarded = editor._group_options(options, payload, self.store, identity, safety,
                                         statements=['UPDATE app.t SET id=2'], operation='table_data')
        session = types.SimpleNamespace(_conn=connector.MySQLConnection(**options))
        session._conn._login_guard = guarded['_login_guard']
        self.addCleanup(session._conn.close)
        self.revoke('read_only')
        with mock.patch.object(connector.MySQLConnection, '_resolve_binary', return_value='/nonexistent/mysql'), \
                mock.patch.object(connector, '_run_bounded') as spawn:
            with self.assertRaises(ProfileReadError):
                table_data._actor(session)
        spawn.assert_not_called()
        self.assert_refused()

    def test_editor_helper_rechecks_consent_at_its_own_login(self):
        payload = dict(connection=self.connection)
        options, identity, safety, label = editor._connection_facts(payload, self.store)
        audit = dbops.AuditTrail(self.store, 'editor_run', 'ui', connection=self.connection, safety=safety)
        full = editor._group_options(options, payload, self.store, identity, safety, audit,
                                     statements=['KILL QUERY 41'])
        session = editor.EditorSession('helper', identity, options)
        session.engine = 'mysql'
        session.server_identity = {'server_uuid': '11111111-2222-3333-4444-555555555555'}
        session._kill_identity = dict(session.server_identity)
        session._conn = connector.MySQLConnection(**options)
        session._conn._login_guard = full['_login_guard']
        self.addCleanup(session._conn.close)
        completed_id = audit.outcome(['SELECT 1'], 'ok', outcome='completed')
        self.revoke('read_only')
        with mock.patch.object(connector.MySQLConnection, '_resolve_binary', return_value='/nonexistent/mysql'), \
                mock.patch.object(connector, '_run_bounded') as spawn:
            with self.assertRaises(ProfileReadError):
                session._send_kill(options, 41)
        spawn.assert_not_called()
        self.assert_refused()
        self.assertNotEqual(self.store.audit_records()[0]['id'], completed_id)
        completed = next(record for record in self.store.audit_records() if record['id'] == completed_id)
        self.assertEqual(completed['status'], 'ok')

    def test_lifecycle_start_guards_each_control_login(self):
        for operation in ('dump', 'restore', 'clone'):
            with self.subTest(operation=operation):
                self.store.assign_profile_group([self.profile['id']], 'G')
                plan = lifecycle_tests.fake_plan(operation)
                payload = dict(connection=self.connection, operation=operation, plan=plan,
                               confirmed=True, typed_target=plan['target'])
                if operation == 'clone':
                    donor = self.save('donor', 'G', host='donor.example', type='development')
                    payload['donor'] = dict(self.request(donor), password='test-password')
                    donor_options = ui._lifecycle_donor(payload, self.workspace, [])
                    safety = dbops.connection_safety(self.profile, self.connection, store=self.store)
                    recipient_options = ui._lifecycle_connection(self.connection, safety)
                    plan['expect'] = lifecycle._clone_expect(donor_options, recipient_options)
                    plan['request'] = dict(require_ssl=True, donor_key=plan['expect']['donor']['connection_key'])
                    plan['donor'] = dict(identity={'server_uuid': 'donor-fixture'})
                    plan['connection_key'] = plan['expect']['recipient']['connection_key']
                    plan['binding'] = lifecycle._plan_binding(plan, {
                        'donor_identity': plan['donor']['identity'], 'expect': plan['expect']})
                plan['review_token'] = ui._review_tokens(self.workspace).issue(
                    'lifecycle', ui._review_identity(self.connection, self.workspace), ui._lifecycle_review_details(plan))
                def run(job, target):
                    self.revoke('removed')
                    return job.run()
                self.workspace.lifecycle = types.SimpleNamespace(add=run)
                with mock.patch.object(connector.MySQLConnection, '_resolve_binary', return_value='/nonexistent/mysql'), \
                        mock.patch.object(connector, '_run_bounded') as spawn:
                    result = ui.api_response('/api/lifecycle/start', payload, self.workspace)
                self.assertEqual(result['state'], 'refused', result)
                spawn.assert_not_called()
                self.assert_refused()

    def test_lifecycle_control_rechecks_after_credential_setup(self):
        safety = dbops.connection_safety(self.profile, self.connection, store=self.store)
        audit = dbops.AuditTrail(self.store, 'dump', 'ui', connection=self.connection, safety=safety)
        options = ui._group_guarded_options(connection_options(self.connection), self.connection, self.store,
                                             dbops.connection_identity(self.connection, safety), audit, ['SELECT 1'])
        original = connector.MySQLConnection._create_defaults_file
        def credentials(conn, *args, **kwargs):
            result = original(conn, *args, **kwargs)
            self.revoke('read_only')
            return result
        with mock.patch.object(connector.MySQLConnection, '_resolve_binary', return_value='/nonexistent/mysql'), \
                mock.patch.object(connector.MySQLConnection, '_create_defaults_file', credentials), \
                mock.patch.object(connector, '_run_bounded') as spawn, \
                lifecycle._connect(lifecycle.LifecycleConnection, options) as conn:
            with self.assertRaises(ProfileReadError):
                conn.run('SELECT 1')
        spawn.assert_not_called()
        self.assert_refused()

    def test_lifecycle_tools_recheck_at_final_spawn(self):
        for name in ('mysqlsh', 'mysqldump', 'mysql', 'mariadb-dump', 'mariadb'):
            with self.subTest(tool=name):
                self.store.assign_profile_group([self.profile['id']], 'G')
                self.store.set_profile_safety(self.profile['id'], read_only=False)
                safety = dbops.connection_safety(self.profile, self.connection, store=self.store)
                audit = dbops.AuditTrail(self.store, 'restore', 'ui', connection=self.connection, safety=safety)
                options = ui._group_guarded_options(connection_options(self.connection), self.connection, self.store,
                    dbops.connection_identity(self.connection, safety), audit, [name])
                job = lifecycle.LifecycleJob('restore', lambda job: None, options)
                tool = dict(name=name, path='/nonexistent/' + name, engine='mysql')
                checks = []
                def checked(path, *args, **kwargs):
                    # First trust check precedes process_tool; the second is
                    # directly inside its spawn closure, after credentials.
                    checks.append(path)
                    if len(checks) == 2:
                        self.revoke('read_only')
                    return path
                with mock.patch.object(connector, 'checked_program', side_effect=checked), \
                        mock.patch.object(connector, 'checked_shell_program', side_effect=checked), \
                        mock.patch.object(lifecycle.subprocess, 'Popen') as spawn:
                    with self.assertRaises(ProfileReadError):
                        lifecycle._invoke(job, tool, options, [])
                spawn.assert_not_called()
                self.assertEqual(len(checks), 2)
                self.assert_refused()

    def test_clone_donor_login_requires_active_member_consent(self):
        donor = self.save('donor', 'G', host='donor.example', type='development', ssl_mode='REQUIRED')
        donor_route = self.request(donor)
        payload = dict(connection=dict(self.connection, ssl_mode='REQUIRED'), donor=donor_route)
        # The recipient TLS mode must match its saved endpoint for consent.
        self.profile = self.save('member', 'G', host='127.0.0.1', user='root', type='development', ssl_mode='REQUIRED')
        payload['connection'] = self.connection = dict(self.request(self.profile), group_name='G')
        audit = dbops.AuditTrail(self.store, 'clone', 'ui', connection=self.connection)
        options = ui._lifecycle_donor(payload, self.workspace, [], audit=audit)
        self.revoke('removed')
        with mock.patch.object(connector, '_run_bounded') as spawn, \
                lifecycle._connect(lifecycle.LifecycleConnection, options) as conn:
            with self.assertRaises(ProfileReadError):
                conn.run('SELECT 1')
        spawn.assert_not_called()
        self.assert_refused()

    def test_clone_donor_read_only_change_refuses_its_later_login(self):
        donor = self.save('donor', 'G', host='donor.example', type='development')
        payload = dict(connection=self.connection, donor=dict(self.request(donor), password='test-password'))
        audit = dbops.AuditTrail(self.store, 'clone', 'ui', connection=self.connection)
        options = ui._lifecycle_donor(payload, self.workspace, [], audit=audit)
        self.store.set_profile_safety('donor', read_only=True)
        with mock.patch.object(connector, '_run_bounded') as spawn, \
                lifecycle._connect(lifecycle.LifecycleConnection, options) as conn:
            with self.assertRaises(ProfileReadError):
                conn.run('SELECT 1')
        spawn.assert_not_called()
        self.assert_refused()

    def test_upgrade_check_guard_is_installed_before_read(self):
        def read(conn, options):
            self.revoke('removed')
            return conn.run('SELECT 1')
        with mock.patch.object(lifecycle, 'read_server', side_effect=read), \
                mock.patch.object(connector, '_run_bounded') as spawn:
            with self.assertRaises(ProfileReadError):
                ui.api_response('/api/lifecycle/upgrade-check', dict(
                    connection=self.connection, target_version='9.7.0'), self.workspace)
        spawn.assert_not_called()
        self.assert_refused()
