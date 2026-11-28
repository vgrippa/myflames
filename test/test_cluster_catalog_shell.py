"""ClusterSet transport eligibility and retained Shell result regressions.

Only server reads and the external Shell transport are synthetic. Planning,
review matching and outcome handling use the production implementations.
"""
import copy
import unittest
from unittest import mock

import _cluster_fixtures as F
import test_cluster_actions as catalog_fixtures
import test_cluster_shell as shell_fixtures
import test_clusterset_execution as execution_fixtures
from myflames import cluster, cluster_shell
from myflames.connector import ConnectorError


class TestClusterSetTransportEligibility(unittest.TestCase):
    def inputs(self, remote=False):
        if remote:
            report = shell_fixtures.action_report('ic-8.4-clusterset-replica')
            primary = cluster._clusterset_primary(report)
            route = primary['route']
            active = {'id': 'active', 'name': 'Active replica', 'group': 'G',
                      'topology_identity': {'server_uuid': report['server']['server_uuid']}}
            routed = {'id': route['profile_id'], 'name': 'Primary route', 'group': 'G',
                      'remember_password': True,
                      'topology_identity': {'server_uuid': route['server_uuid']}}
            checks = {'shell': execution_fixtures.SHELL, 'group_name': 'G', 'current_profile': active,
                      'group_profiles': [active, routed], 'ssh': False}
            return report, checks, routed
        report = shell_fixtures.action_report('ic-8.4-clusterset-primary')
        checks = catalog_fixtures.clusterset_checks(report)
        for profile in checks['group_profiles']:
            profile.update(group=checks['group_name'], remember_password=True)
        return report, checks, checks['current_profile']

    def entry(self, report, checks, aid='check_clusterset'):
        return next(a for a in cluster.overview_actions(report, checks) if a['id'] == aid)

    def assert_disabled_plan(self, report, checks, phrase, aid='check_clusterset', parameters=None):
        entry = self.entry(report, checks, aid)
        self.assertFalse(entry['enabled'], entry)
        self.assertIn(phrase.lower(), entry['why_not'].lower())
        self.assertTrue(entry['fix'])
        with self.assertRaises(ValueError) as caught:
            cluster.plan_action(report, aid, checks=checks, parameters=parameters)
        self.assertIn(entry['why_not'], str(caught.exception))

    def test_tls_rejection_uses_the_shell_route_and_matches_catalog_and_plan(self):
        for remote in (False, True):
            for mode in ('REQUIRED', 'VERIFY_IDENTITY', 'required', 'verify_identity'):
                with self.subTest(remote=remote, mode=mode):
                    report, checks, profile = self.inputs(remote)
                    profile['ssl_mode'] = mode
                    self.assert_disabled_plan(report, checks, mode.upper())

    def test_unsaved_direct_connection_uses_its_actual_tls_mode_in_catalog_and_plan(self):
        for mode in ('REQUIRED', 'VERIFY_IDENTITY'):
            with self.subTest(mode=mode):
                report = shell_fixtures.action_report('ic-8.4-clusterset-primary')
                report._action_conn.ssl_mode = mode
                checks = {'shell': execution_fixtures.SHELL}
                self.assertNotIn('current_profile', checks)
                self.assertNotIn('ssl_mode', checks)
                self.assert_disabled_plan(report, checks, mode)

    def test_actual_local_session_tls_takes_precedence_over_the_saved_profile(self):
        report, checks, profile = self.inputs()
        profile['ssl_mode'] = 'VERIFY_CA'
        report._action_conn.ssl_mode = 'REQUIRED'
        self.assert_disabled_plan(report, checks, 'REQUIRED')

    def test_direct_remote_shell_route_is_eligible_when_active_profile_uses_ssh(self):
        report, checks, profile = self.inputs(remote=True)
        checks['ssh'] = True
        checks['current_profile']['ssh_host'] = 'active-bastion'
        self.assertNotIn('ssh_host', profile)
        entry = self.entry(report, checks)
        self.assertTrue(entry['enabled'], entry)
        self.assertEqual(entry['target']['profile_id'], profile['id'])
        cluster.plan_action(report, 'check_clusterset', checks=checks)

    def test_remote_shell_route_cannot_use_ssh_or_an_unsaved_password(self):
        for field, value, phrase in (('ssh_host', 'remote-bastion', 'SSH'),
                                     ('remember_password', False, 'password')):
            with self.subTest(field=field):
                report, checks, profile = self.inputs(remote=True)
                profile[field] = value
                self.assert_disabled_plan(report, checks, phrase)

    def test_unsupported_tls_on_active_profile_does_not_disable_an_eligible_remote_route(self):
        report, checks, profile = self.inputs(remote=True)
        checks['current_profile']['ssl_mode'] = 'VERIFY_IDENTITY'
        profile['ssl_mode'] = 'VERIFY_CA'
        self.assertTrue(self.entry(report, checks)['enabled'])
        cluster.plan_action(report, 'check_clusterset', checks=checks)

    def test_supported_tls_modes_keep_a_direct_shell_route_eligible(self):
        for mode in ('PREFERRED', 'DISABLED', 'VERIFY_CA'):
            with self.subTest(mode=mode):
                report, checks, profile = self.inputs()
                profile['ssl_mode'] = mode
                self.assertTrue(self.entry(report, checks)['enabled'])

    def test_local_shell_route_can_use_the_current_request_password(self):
        report, checks, profile = self.inputs()
        profile['remember_password'] = False
        self.assertTrue(self.entry(report, checks)['enabled'])
        cluster.plan_action(report, 'check_clusterset', checks=checks)

    def test_spare_catalog_checks_transport_and_password_before_any_preflight(self):
        for field, value, phrase in (('ssh_host', 'spare-bastion', 'SSH'),
                                     ('remember_password', False, 'password'),
                                     ('ssl_mode', 'REQUIRED', 'REQUIRED'),
                                     ('ssl_mode', 'VERIFY_IDENTITY', 'VERIFY_IDENTITY')):
            with self.subTest(field=field, value=value):
                report, checks, _ = self.inputs()
                checks['replica_candidates'] = []
                spare = next(p for p in checks['group_profiles'] if p['id'] == 'spare')
                self.assertTrue(self.entry(report, checks, 'create_replica_cluster:spare')['enabled'])
                spare[field] = value
                with mock.patch.object(cluster, 'prepare_action', side_effect=AssertionError('catalog login')):
                    self.assert_disabled_plan(
                        report, checks, phrase, 'create_replica_cluster:spare',
                        {'name': 'newcluster', 'recoveryMethod': 'incremental'})

    def test_set_level_failover_and_rejoin_labels_name_the_target_cluster(self):
        report = catalog_fixtures.clusterset_report()
        checks = catalog_fixtures.clusterset_checks(report)
        for aid, label in (('force_primary_cluster:drcluster', 'Fail over to drcluster…'),
                           ('rejoin_cluster:oldcluster', 'Rejoin oldcluster to the ClusterSet…')):
            with self.subTest(action=aid):
                self.assertEqual(self.entry(report, checks, aid)['label'], label)


class TestClusterSetEffects(unittest.TestCase):
    def test_every_clusterset_action_ignores_local_group_mode_changes(self):
        report = catalog_fixtures.clusterset_report()
        before = cluster._after(report, {})
        report['cluster']['topology'] = 'multi_primary'
        after = cluster._after(report, {})
        for aid in cluster.CLUSTERSET_ACTIONS:
            with self.subTest(action=aid):
                rows = cluster._effect({'action': aid}, before, after)
                self.assertTrue(rows)
                self.assertNotIn('Group mode', [row[0] for row in rows])
                self.assertFalse([row for row in rows if row[1] != row[2]], rows)

    def test_membership_only_changes_are_visible_for_create_remove_and_dissolve(self):
        for aid in ('create_replica_cluster:spare', 'remove_cluster:drcluster',
                    'remove_cluster_keep:drcluster', 'dissolve_clusterset'):
            with self.subTest(action=aid):
                report = catalog_fixtures.clusterset_report()
                before = cluster._after(report, {})
                if aid == 'dissolve_clusterset':
                    report.pop('clusterset')
                else:
                    report['clusterset']['clusters'] = [
                        c for c in report['clusterset']['clusters'] if c['name'] != 'drcluster']
                after = cluster._after(report, {})
                changes = [row for row in cluster._effect({'action': aid}, before, after)
                           if row[1] != row[2]]
                self.assertTrue(changes)
                self.assertNotIn('Group mode', [row[0] for row in changes])

    def test_routing_and_remote_channel_only_changes_are_visible(self):
        for aid, field in (('set_routing_option', 'routing'), ('repair_cluster:drcluster', 'channel')):
            with self.subTest(action=aid):
                report = catalog_fixtures.clusterset_report()
                before = cluster._after(report, {})
                if field == 'routing':
                    report['clusterset']['routing_options']['options']['target_cluster'] = 'drcluster'
                else:
                    replica = next(c for c in report['clusterset']['clusters'] if c['name'] == 'drcluster')
                    replica['channel']['receiver_state'] = 'OFF'
                after = cluster._after(report, {})
                self.assertTrue([row for row in cluster._effect({'action': aid}, before, after)
                                 if row[1] != row[2]])

    def test_before_snapshot_keeps_independent_routing_and_cluster_evidence(self):
        report = catalog_fixtures.clusterset_report()
        before = cluster._after(report, {})
        independent = copy.deepcopy(before)
        report['clusterset']['routing_options']['options']['target_cluster'] = 'drcluster'
        report['clusterset']['clusters'][0]['role'] = 'REPLICA'
        report['clusterset']['clusters'][0]['channel']['receiver_state'] = 'OFF'
        self.assertEqual(before, independent)

    def test_dissolve_unread_after_report_does_not_fabricate_removed_metadata(self):
        report = catalog_fixtures.clusterset_report()
        before = cluster._after(report, {})
        unread = cluster._after({}, {})
        self.assertIsNone(unread['cs_present'])
        self.assertEqual(cluster._effect({'action': 'dissolve_clusterset'}, before, unread), [])

    def test_dissolve_partial_cleanup_uses_actual_facts_on_a_verified_route(self):
        for fact_name, actual, label in (('metadata schema present', False, 'Cluster metadata on '),
                                         ('Group Replication stopped', [], 'Group Replication on ')):
            with self.subTest(fact=fact_name):
                report = catalog_fixtures.clusterset_report()
                before = cluster._after(report, {})
                uuid = report['server']['server_uuid']
                server = 'mfcluster/member-1'
                self.assertIn(before['cs_member_states'][uuid], ('ONLINE', 'RECOVERING'))
                self.assertEqual(before['cs_metadata_server_uuid'], uuid)
                after = cluster._after({}, {})
                after['verification'] = {'facts': [
                    {'server': server, 'fact': 'route server UUID', 'status': 'verified', 'actual': uuid},
                    {'server': server, 'fact': fact_name, 'status': 'verified', 'actual': actual}]}
                changes = cluster._effect({'action': 'dissolve_clusterset'}, before, after)
                self.assertEqual(len(changes), 1, changes)
                self.assertEqual(changes[0][0], label + server)
                self.assertNotEqual(changes[0][1], changes[0][2])

    def test_dissolve_known_unchanged_cleanup_facts_do_not_claim_partial_mutation(self):
        report = catalog_fixtures.clusterset_report()
        before = cluster._after(report, {})
        uuid = report['server']['server_uuid']
        server = 'mfcluster/member-1'
        after = cluster._after({}, {})
        after['verification'] = {'facts': [
            {'server': server, 'fact': 'route server UUID', 'status': 'verified', 'actual': uuid},
            {'server': server, 'fact': 'metadata schema present', 'status': 'mismatch', 'actual': True},
            {'server': server, 'fact': 'Group Replication stopped', 'status': 'mismatch', 'actual': [uuid]}]}
        self.assertEqual(cluster._effect({'action': 'dissolve_clusterset'}, before, after), [])

    def test_dissolve_cleanup_without_matching_verified_identity_is_not_effect_evidence(self):
        for route_status, route_uuid, fact_server in (('missing', None, 'mfcluster/member-1'),
                                                      ('verified', 'unreviewed-uuid', 'mfcluster/member-1'),
                                                      ('verified', None, 'another/member')):
            with self.subTest(route_status=route_status, route_uuid=route_uuid, fact_server=fact_server):
                report = catalog_fixtures.clusterset_report()
                before = cluster._after(report, {})
                uuid = report['server']['server_uuid'] if route_uuid is None else route_uuid
                after = cluster._after({}, {})
                after['verification'] = {'facts': [
                    {'server': 'mfcluster/member-1', 'fact': 'route server UUID',
                     'status': route_status, 'actual': uuid},
                    {'server': fact_server, 'fact': 'metadata schema present', 'status': 'verified', 'actual': False},
                    {'server': fact_server, 'fact': 'Group Replication stopped', 'status': 'verified', 'actual': []}]}
                self.assertEqual(cluster._effect({'action': 'dissolve_clusterset'}, before, after), [])


class TestAllShellResults(shell_fixtures.Home):
    def inputs(self, scenario, action):
        report = shell_fixtures.action_report(scenario)
        if action == 'switch_to_single_primary':
            report['cluster']['topology'] = 'multi_primary'
        if action == 'rejoin_instance':
            member = shell_fixtures.missing(report)
        elif action == 'switchover':
            member = next(m for m in report['members'] if m['role'] == 'SECONDARY')
        else:
            member = shell_fixtures.local(report)
        return report, member, shell_fixtures.Writes(F.conn(scenario))

    def execute(self, scenario, action, stdout='', stderr='', error=None, constructor_error=None):
        report, member, conn = self.inputs(scenario, action)

        def send(writer, script, **kwargs):
            writer.stdout, writer.stderr = stdout, stderr
            if error is not None:
                raise ConnectorError(error)
            return stdout

        with mock.patch.object(cluster, 'read_status', side_effect=lambda *a, **kw: copy.deepcopy(report)), \
                mock.patch.object(cluster_shell, 'find_shell', return_value=execution_fixtures.SHELL):
            fresh_report, checks = cluster.prepare_action(conn, action)
            plan = cluster.plan_action(fresh_report, action, member['key'], checks)
            if constructor_error is None:
                with mock.patch.object(cluster_shell.ShellWriter, 'run_write', autospec=True, side_effect=send):
                    result = cluster.execute_action(conn, plan, typed=plan.get('typed_guard'))
            else:
                with mock.patch.object(cluster_shell, 'ShellWriter', side_effect=ConnectorError(constructor_error)):
                    result = cluster.execute_action(conn, plan, typed=plan.get('typed_guard'))
        return plan, result

    def test_mode_switch_make_primary_and_rejoin_retain_small_output_on_success_and_error(self):
        cases = (('ic-8.4-healthy', 'switch_to_multi_primary'),
                 ('ic-8.4-healthy', 'switch_to_single_primary'),
                 ('ic-8.4-clusterset-replica', 'switchover'),
                 ('ic-8.4-member-offline', 'rejoin_instance'))
        for scenario, aid in cases:
            for error in (None, 'MySQL Shell timed out; the outcome is unknown.'):
                with self.subTest(action=aid, error=error):
                    _, result = self.execute(scenario, aid, 'Recovery progress\n', 'WARNING: retained\n', error)
                    self.assertEqual(result['shell_output'], 'Recovery progress\n')
                    self.assertEqual(result['shell_stderr'], 'WARNING: retained\n')
                    self.assertFalse(result['shell_output_truncated'])
                    self.assertFalse(result['shell_stderr_truncated'])
                    self.assertEqual(result['status'], 'done' if error is None else 'unknown')

    def test_legacy_shell_results_are_bounded_utf8_tails_including_error_output(self):
        limit = 1024 * 1024
        stdout = 'discarded prefix\n' + '\N{SNOWMAN}' * (limit // 3 + 20) + 'output tail'
        stderr = 'discarded warning\n' + 'w' * limit + 'stderr tail'
        for error in (None, 'MySQL Shell timed out; the outcome is unknown.'):
            with self.subTest(error=error):
                _, result = self.execute('ic-8.4-healthy', 'switch_to_multi_primary', stdout, stderr, error)
                for key, original in (('shell_output', stdout), ('shell_stderr', stderr)):
                    self.assertLessEqual(len(result[key].encode('utf-8')), limit)
                    self.assertEqual(result[key], original.encode('utf-8')[-limit:].decode('utf-8', 'ignore'))
                    self.assertTrue(result[key + '_truncated'])

    def test_proven_constructor_failure_is_an_unsent_refusal(self):
        plan, result = self.execute('ic-8.4-healthy', 'switch_to_multi_primary',
                                    constructor_error='The selected mysqlsh cannot be used before spawn.')
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(result['sent'], [])
        self.assertEqual(result['not_sent'], plan['statements'])
        self.assertEqual(result['states'], ['not_sent'])
        self.assertEqual(result['shell_output'], '')
        self.assertEqual(result['shell_stderr'], '')
        self.assertFalse(result['shell_output_truncated'])
        self.assertFalse(result['shell_stderr_truncated'])


class TestClusterSetRetainedOutput(execution_fixtures.EndpointHarness):
    def test_existing_clusterset_retention_keeps_the_shared_output_contract(self):
        self.warning = 'WARNING: retained ClusterSet warning\n'
        result = self.apply(self.plan('check_clusterset'))
        self.assertEqual(result['shell_output'], self.output)
        self.assertEqual(result['shell_stderr'], self.warning)
        self.assertFalse(result['shell_output_truncated'])
        self.assertFalse(result['shell_stderr_truncated'])

    def test_proven_constructor_failure_is_unsent_and_finishes_the_same_audit(self):
        plan = self.plan('check_clusterset')
        with mock.patch.object(cluster_shell, 'ShellWriter',
                               side_effect=ConnectorError('MySQL Shell cannot be used before spawn.')):
            result = self.apply(plan)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(result['outcome_class'], 'failed, nothing changed')
        self.assertEqual(result['sent'], [])
        self.assertEqual(result['not_sent'], plan['statements'])
        self.assertEqual(self.sent, [])
        self.assertEqual(result['shell_output'], '')
        self.assertEqual(result['shell_stderr'], '')
        records = execution_fixtures.dbops.audit_report(self.store)['records']
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['id'], result['audit_id'])
        self.assertEqual(records[0]['status'], 'refused')

    def test_tls_mode_changed_after_review_refuses_before_shell(self):
        plan = self.plan('check_clusterset')
        self.conn.ssl_mode = 'REQUIRED'
        result = self.apply(plan)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(result['outcome_class'], 'failed, nothing changed')
        self.assertIn('REQUIRED', result['message'])
        self.assertEqual(result['sent'], [])
        self.assertEqual(result['not_sent'], plan['statements'])
        self.assertEqual(self.sent, [])

