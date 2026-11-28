"""Failover proves the old primary is down on every review and send.

The local replica uses the synthetic ClusterSet SQL replay; group peers are
saved profiles with recording connectors. No database or service is started.
"""
import json
import os
import tempfile
import types
import unittest
from collections import Counter
from unittest import mock

import _cluster_fixtures as F
from myflames import cluster, cluster_shell, connection_groups, reviewed_writes, ui
from myflames.connector import ConnectorError
from myflames.workspace_store import ENDPOINT_FIELDS, WorkspaceStore
from test_cluster import install_clusterset_batch_replay


install_clusterset_batch_replay()

SHELL = {'path': '/opt/mysqlsh/bin/mysqlsh', 'realpath': '/opt/mysqlsh/bin/mysqlsh',
         'version': '8.4.6', 'version_tuple': [8, 4, 6]}
ACTION = 'force_primary_cluster:mfcluster'
INSTANCE_TABLE = 'mysql_innodb_cluster_metadata.v2_instances'
PRIMARY_ID = 'cid-0'


def primary_rows():
    rows = F._cs_instances(PRIMARY_ID, 'mf-main', 20)
    for index in (3, 4):
        number = 20 + index
        address = 'mf-main-%d:3306' % (index + 1)
        rows.append([number, PRIMARY_ID, address, '%08d-2222-11f0-8a00-0242ac130002' % number,
                     address, address, 'group-member'])
    return rows


class GroupFixture(object):
    """A local replica, five primary members and a consent-scoped opener."""
    def __init__(self, scenario='ic-8.4-clusterset-replica'):
        self.source = F.conn(scenario)
        tables = self.source.scenario['tables']
        tables[INSTANCE_TABLE] = [row for row in tables[INSTANCE_TABLE] if row[1] != PRIMARY_ID] + primary_rows()
        tables['performance_schema.replication_group_member_actions'] = [[0]]
        self.current = {'id': 'local', 'name': 'Local', 'group': 'production',
                        'host': 'mf-ic-1', 'port': 3306, 'user': 'admin', 'remember_password': True}
        self.primary = [
            {'id': 'p%d' % i, 'name': 'Primary %d' % i, 'group': 'production',
             'host': 'mf-main-%d' % (i + 1), 'port': 3306, 'user': 'admin',
             'remember_password': True, 'topology_identity': {'server_uuid': row[3]}}
            for i, row in enumerate(primary_rows())
        ]
        # Both profiles are tempting to a search, but neither is authorized as
        # a probe of an old-primary member in the active group.
        self.foreign = dict(self.primary[4], id='foreign', group='other')
        self.spare = {'id': 'spare', 'name': 'Unclassified spare', 'group': 'production',
                      'host': 'unrelated', 'port': 3306, 'user': 'admin', 'remember_password': True}
        self.profiles = [self.current] + self.primary + [self.foreign, self.spare]
        self.outcomes = {}
        self.opens = []
        self.events = []

    def open(self, profile):
        pid = profile['id']
        self.opens.append(pid)
        self.events.append(('open', pid))
        outcome = self.outcomes.get(pid, 'down')
        if isinstance(outcome, Exception):
            raise outcome
        if outcome == 'down':
            raise ConnectorError('mysql client exited 1: ERROR 2003 (HY000): connection refused')
        if outcome == 'unknown_host':
            raise ConnectorError("mysql client exited 1: ERROR 2005 (HY000): Unknown MySQL server host 'mf-main' (-2)")
        if outcome == 'denied':
            raise ConnectorError('mysql client exited 1: ERROR 1045 (28000): Access denied')
        if outcome == 'timeout':
            raise ConnectorError('mysql client timed out after 5 s')
        if outcome == 'timeout_code':
            raise ConnectorError('mysql client exited 1: ERROR 2003 (HY000): connection timed out')
        if outcome in ('query_error', 'query_unknown_host'):
            peer = F.conn('ic-8.4-clusterset-replica')
            code = 2005 if outcome == 'query_unknown_host' else 2003

            def fail_query(sql, extra_flags=None, timeout=None):
                raise ConnectorError('mysql client exited 1: ERROR %d (HY000) at line 1: reported failure' % code)

            peer.run = fail_query
            return peer
        if outcome in ('up', 'different_uuid'):
            peer = F.conn('ic-8.4-clusterset-replica')
            uuid = (profile.get('_fixture_uuid') or (profile.get('topology_identity') or {}).get('server_uuid')
                    if outcome == 'up' else primary_rows()[2][3])
            peer.scenario['variables']['server_uuid'] = uuid
            return peer
        raise AssertionError('Unexpected profile: %s (%s)' % (pid, outcome))

    def group_checks(self):
        scope = connection_groups.GroupScope(self.profiles, self.current)
        return {'group_name': 'production', 'group_profiles': self.profiles,
                'current_profile': self.current,
                'read_options': {'profiles': scope.profiles, 'open_profile': scope.opener(self.open),
                                 'current_profile': self.current, 'current_profile_id': self.current['id']}}

    def prepare(self):
        with mock.patch.object(cluster, 'action_checks', return_value={'shell': SHELL}):
            return cluster.prepare_action(self.source, ACTION, group_checks=self.group_checks)


class TestFailoverPreflight(unittest.TestCase):
    def test_failover_rechecks_stored_routing_in_the_fresh_metadata_snapshot(self):
        for change in ('defaults', 'router', 'default_tag_type', 'router_tag_type', 'added', 'removed', 'unreadable'):
            with self.subTest(change=change):
                group = GroupFixture()
                source = group.source.scenario
                rows = source['tables']['mysql_innodb_cluster_metadata.v2_cs_members']
                defaults = {'target_cluster': 'primary', 'tags': {'active': False}}
                override = {'target_cluster': 'primary', 'tags': {'active': False}}
                for row in rows:
                    row[7] = json.dumps(defaults)
                source['clusterset_routers'] = [[1, 'cs-1', None, 'rw', 'router', '8.4.10', json.dumps(override)]]
                report, checks = group.prepare()
                plan = cluster.plan_action(report, ACTION, checks=checks)
                view_id = report['clusterset']['view_id']
                snapshot = plan['expect']['routing_configuration']
                self.assertEqual(snapshot['defaults']['options'], defaults)
                self.assertEqual(snapshot['routers'][0]['options'], override)
                if change == 'defaults':
                    defaults['target_cluster'] = 'mfcluster'
                elif change == 'default_tag_type':
                    defaults['tags']['active'] = 0
                elif change == 'router':
                    override['target_cluster'] = 'mfcluster'
                elif change == 'router_tag_type':
                    override['tags']['active'] = 0
                elif change == 'added':
                    source['clusterset_routers'].append([2, 'cs-1', None, 'rw', 'another', '8.4.10', '{}'])
                elif change == 'removed':
                    source['clusterset_routers'].clear()
                else:
                    source['clusterset_routers'][0][6] = '{invalid'
                for row in rows:
                    row[7] = json.dumps(defaults)
                    self.assertEqual(row[6], view_id)
                if change not in ('removed', 'unreadable'):
                    source['clusterset_routers'][0][6] = json.dumps(override)
                self.assertIs(snapshot['defaults']['options']['tags']['active'], False)
                self.assertIs(snapshot['routers'][0]['options']['tags']['active'], False)
                with mock.patch.object(cluster, 'action_checks', return_value={'shell': SHELL}), \
                        mock.patch.object(cluster_shell.ShellWriter, 'run_write', autospec=True) as sender:
                    result = cluster.execute_action(group.source, plan, typed=plan['typed_guard'],
                                                    group_checks=group.group_checks)
                sender.assert_not_called()
                self.assertEqual(result['status'], 'refused', result)
                reason = ('Router routing could not be read after review' if change == 'unreadable' else
                          'Router routing changed after review')
                self.assertIn(reason, result['message'])
                self.assertIn('Nothing was sent', result['message'])
                self.assertEqual(result['sent'], [])
                self.assertEqual(result['not_sent'], plan['statements'])
                self.assertEqual(result['states'], ['not_sent'])

    def test_catalog_uses_bounded_read_and_review_probes_every_member(self):
        group = GroupFixture()
        options = group.group_checks()['read_options']
        report = cluster.read_status(group.source, **options)
        ordinary = list(group.opens)
        self.assertEqual(ordinary, ['p0', 'p1', 'p2'])
        cluster.with_actions(report, shell=SHELL, group_profiles=group.profiles,
                             current_profile=group.current, group_name='production')
        self.assertEqual(group.opens, ordinary, 'Attaching actions must not open saved profiles again.')
        action = next(a for a in report['actions'] if a['id'] == ACTION)
        self.assertTrue(action['enabled'], action)
        self.assertIn('check reachability, then review', action['label'])
        self.assertIn('Check reachability, then review', action['description'])
        # Offering the preflight is not enough to produce a reviewed write.
        with self.assertRaisesRegex(ValueError, '[Uu]nreachable'):
            cluster.plan_action(report, ACTION, checks=dict(group.group_checks(), shell=SHELL,
                                                            clusterset_catalog=True))

        group.opens.clear()
        report, checks = group.prepare()
        # A new ordinary read and its own preflight have separate budgets.
        self.assertEqual(group.opens[:3], ['p0', 'p1', 'p2'])
        self.assertEqual(Counter(group.opens[3:]), Counter(['p0', 'p1', 'p2', 'p3', 'p4']))
        plan = cluster.plan_action(report, ACTION, checks=checks)
        observations = plan['expect']['clusterset_reachability']['maincluster']
        self.assertEqual({item['server_uuid'] for item in observations}, {r[3] for r in primary_rows()})
        self.assertTrue(all(item['reachable'] is False for item in observations))
        self.assertNotIn('foreign', group.opens)
        self.assertNotIn('spare', group.opens)

    def test_catalog_and_review_do_not_inherit_the_display_member_or_time_budget(self):
        for count in (4, 9):
            with self.subTest(primary_members=count):
                group = GroupFixture()
                rows = primary_rows()[:count]
                for index in range(len(rows), count):
                    address = 'mf-main-%d:3306' % (index + 1)
                    rows.append([20 + index, PRIMARY_ID, address,
                                 '%08d-2222-11f0-8a00-0242ac130002' % (20 + index),
                                 address, address, 'read-replica' if index > 6 else 'group-member'])
                table = group.source.scenario['tables'][INSTANCE_TABLE]
                group.source.scenario['tables'][INSTANCE_TABLE] = [r for r in table if r[1] != PRIMARY_ID] + rows
                group.primary = [dict(group.primary[0], id='p%d' % i, host='mf-main-%d' % (i + 1),
                                      topology_identity={'server_uuid': row[3]}) for i, row in enumerate(rows)]
                group.profiles = [group.current] + group.primary
                clock = [0.0]
                original = group.open

                def delayed(profile):
                    clock[0] += 2.0
                    return original(profile)

                group.open = delayed
                with mock.patch.object(cluster.time, 'monotonic', side_effect=lambda: clock[0]):
                    options = group.group_checks()['read_options']
                    report = cluster.read_status(group.source, **options)
                    self.assertEqual(len(group.opens), cluster.CLUSTERSET_ROUTE_MAX_OPENS)
                    ordinary = list(group.opens)
                    cluster.with_actions(report, shell=SHELL, group_profiles=group.profiles,
                                         current_profile=group.current, group_name='production')
                    self.assertEqual(group.opens, ordinary)
                    self.assertTrue(next(a for a in report['actions'] if a['id'] == ACTION)['enabled'])
                    group.opens.clear()
                    report, checks = group.prepare()
                    plan = cluster.plan_action(report, ACTION, checks=checks)
                self.assertEqual(Counter(group.opens[3:]), Counter(p['id'] for p in group.primary))
                observations = plan['expect']['clusterset_reachability']['maincluster']
                self.assertEqual({item['server_uuid'] for item in observations}, {row[3] for row in rows})
                self.assertTrue(all(item['reachable'] is False for item in observations))

    def test_catalog_refuses_confirmed_primary_reachability_but_unknown_can_request_check(self):
        for outcome, enabled in (('up', False), ('denied', True), ('timeout', True)):
            with self.subTest(outcome=outcome):
                group = GroupFixture()
                group.outcomes['p0'] = outcome
                report = cluster.read_status(group.source, **group.group_checks()['read_options'])
                ordinary = list(group.opens)
                cluster.with_actions(report, shell=SHELL, group_profiles=group.profiles,
                                     current_profile=group.current, group_name='production')
                self.assertEqual(group.opens, ordinary)
                entry = next(a for a in report['actions'] if a['id'] == ACTION)
                self.assertEqual(entry['enabled'], enabled, entry)

    def test_unknown_host_offers_review_and_still_requires_all_members_before_send(self):
        group = GroupFixture()
        group.outcomes.update({profile['id']: 'unknown_host' for profile in group.primary})
        options = group.group_checks()['read_options']
        report = cluster.read_status(group.source, **options)
        group.opens.clear()
        cluster.with_actions(report, shell=SHELL, group_profiles=group.profiles,
                             current_profile=group.current, group_name='production')
        self.assertTrue(next(a for a in report['actions'] if a['id'] == ACTION)['enabled'])
        expected_opens = Counter(profile['id'] for profile in group.primary)
        self.assertEqual(group.opens, [])

        group.opens.clear()
        report, checks = group.prepare()
        plan = cluster.plan_action(report, ACTION, checks=checks)
        expected = [{'server_uuid': row[3], 'reachable': False} for row in primary_rows()]
        self.assertCountEqual(plan['expect']['clusterset_reachability']['maincluster'], expected)
        group.opens.clear()
        group.events.clear()

        def refuse_sender(*args, **kwargs):
            group.events.append(('sender', None))
            raise reviewed_writes.Refusal('Test stopped before transport.', 'changed')

        # Reaching this transport boundary proves the send performed its own
        # complete preflight. The stub prevents an actual Shell write.
        with mock.patch.object(cluster, 'action_checks', return_value={'shell': SHELL}), \
                mock.patch.object(cluster_shell.ShellWriter, 'run_write', autospec=True,
                                  side_effect=refuse_sender) as sender:
            result = cluster.execute_action(group.source, plan, typed=plan['typed_guard'],
                                            group_checks=group.group_checks)
        self.assertEqual(result['status'], 'refused', result)
        sender.assert_called_once()
        self.assertEqual(group.opens[:3], ['p0', 'p1', 'p2'])
        self.assertEqual(Counter(group.opens[3:]), expected_opens)
        self.assertEqual(group.events[-1], ('sender', None))
        self.assertNotIn('foreign', group.opens)
        self.assertNotIn('spare', group.opens)

    def test_catalog_enables_failover_when_ordinary_read_probed_all_primary_members(self):
        group = GroupFixture()
        group.primary = group.primary[:3]
        group.profiles = [group.current] + group.primary
        rows = group.source.scenario['tables'][INSTANCE_TABLE]
        allowed = {profile['topology_identity']['server_uuid'] for profile in group.primary}
        group.source.scenario['tables'][INSTANCE_TABLE] = [row for row in rows
                                                          if row[1] != PRIMARY_ID or row[3] in allowed]
        group.outcomes.update({profile['id']: 'unknown_host' for profile in group.primary})
        options = group.group_checks()['read_options']
        report = cluster.read_status(group.source, **options)
        ordinary = list(group.opens)
        self.assertEqual(ordinary, ['p0', 'p1', 'p2'])
        cluster.with_actions(report, shell=SHELL, group_profiles=group.profiles,
                             current_profile=group.current, group_name='production')
        self.assertEqual(group.opens, ordinary)
        action = next(a for a in report['actions'] if a['id'] == ACTION)
        self.assertTrue(action['enabled'], action)

    def test_reachable_denied_and_timed_out_member_each_refuse_review(self):
        for outcome in ('up', 'different_uuid', 'denied', 'timeout', 'timeout_code'):
            with self.subTest(outcome=outcome):
                group = GroupFixture()
                # This member is outside the ordinary three-open budget.
                group.outcomes['p4'] = outcome
                report, checks = group.prepare()
                self.assertIn('p4', group.opens)
                with self.assertRaisesRegex(ValueError, '[Uu]nreachable'):
                    cluster.plan_action(report, ACTION, checks=checks)

    def test_authentication_lost_connection_and_other_errors_remain_unknown(self):
        for message in (
                'ERROR 1044 (42000): Access denied for database',
                'ERROR 1045 (28000): Access denied',
                'ERROR 1130 (HY000): Host is not allowed to connect',
                'ERROR 1698 (28000): Access denied',
                'ERROR 2006 (HY000): MySQL server has gone away',
                'ERROR 2013 (HY000): Lost connection to MySQL server during query',
                'ERROR 2026 (HY000): SSL connection error',
                'ERROR 2059 (HY000): Authentication plugin cannot be loaded',
                'ERROR 2061 (HY000): Authentication plugin reported error',
                'Connection refused without a client error number'):
            with self.subTest(message=message):
                group = GroupFixture()
                group.outcomes['p4'] = ConnectorError(message)
                report, checks = group.prepare()
                missing = primary_rows()[4][3]
                self.assertFalse(any(item['server_uuid'] == missing and item['reachable'] is False
                                     for item in checks['clusterset_reachability']['maincluster']))
                with self.assertRaisesRegex(ValueError, '[Uu]nreachable'):
                    cluster.plan_action(report, ACTION, checks=checks)

    def test_timeout_errors_remain_unknown_even_with_connection_error_codes(self):
        for message in (
                'ERROR 2003 (HY000): connection timed out',
                'ERROR 2003 (HY000): connection timeout',
                'ERROR 2005 (HY000): hostname lookup timed out',
                'ERROR 2005 (HY000): hostname lookup timeout',
                "ERROR 2003 (HY000): Can't connect to MySQL server on 'mf-main:3306' (110)",
                "ERROR 2003 (HY000): Can't connect to MySQL server on 'mf-main:3306' (60)",
                "ERROR 2003 (HY000): Can't connect to MySQL server on 'mf-main:3306' (10060)"):
            with self.subTest(message=message):
                group = GroupFixture()
                group.outcomes['p4'] = ConnectorError(message)
                report, checks = group.prepare()
                with self.assertRaisesRegex(ValueError, '[Uu]nreachable'):
                    cluster.plan_action(report, ACTION, checks=checks)

    def test_timeout_in_either_read_pass_cannot_be_overridden_by_a_refusal(self):
        for first, second in (('timeout_code', 'down'), ('down', 'timeout_code')):
            with self.subTest(ordinary=first, preflight=second):
                group = GroupFixture()
                group.outcomes['p0'] = first
                options = group.group_checks()['read_options']
                report = cluster.read_status(group.source, **options)
                group.outcomes['p0'] = second
                evidence = cluster._clusterset_failover_preflight(
                    report, options['profiles'], options['open_profile'])
                observations = evidence['maincluster']
                uncertain = primary_rows()[0][3]
                self.assertFalse(any(item['server_uuid'] == uncertain and item['reachable'] is False
                                     for item in observations))
                self.assertTrue(all(item['reachable'] is False for item in observations
                                    if item['server_uuid'] != uncertain))
                primary = next(c for c in report['clusterset']['clusters'] if c['role'] == 'PRIMARY')
                self.assertFalse(cluster._clusterset_unreachable(
                    report, primary, {'clusterset_reachability': evidence}))

    def test_query_errors_are_not_connection_failures_in_either_pass(self):
        for outcome in ('query_error', 'query_unknown_host'):
            with self.subTest(outcome=outcome):
                group = GroupFixture()
                group.outcomes['p0'] = outcome
                report, checks = group.prepare()
                uuid = primary_rows()[0][3]
                # A line-specific query error proves a prior login, even when
                # its numeric code resembles an initial connection failure.
                for observations in (report._clusterset_reachability, checks['clusterset_reachability']):
                    self.assertFalse(any(item['server_uuid'] == uuid and item['reachable'] is False
                                         for item in observations.get('maincluster', [])))
                self.assertEqual(Counter(group.opens)['p0'], 2)
                with self.assertRaisesRegex(ValueError, '[Uu]nreachable'):
                    cluster.plan_action(report, ACTION, checks=checks)

    def test_unknown_host_does_not_certify_an_alias_or_ambiguous_endpoint_down(self):
        for case in ('alias', 'duplicate_endpoint', 'ssh'):
            with self.subTest(case=case):
                group = GroupFixture()
                group.outcomes['p4'] = 'unknown_host'
                if case == 'alias':
                    # A cached UUID cannot bind a failed hostname lookup to a
                    # metadata member whose actual endpoint was never tried.
                    group.primary[4]['host'] = 'saved-alias'
                elif case == 'duplicate_endpoint':
                    rows = group.source.scenario['tables'][INSTANCE_TABLE]
                    other = next(row for row in rows if row[3] == primary_rows()[3][3])
                    other[2] = other[4] = other[5] = group.primary[4]['host'] + ':3306'
                else:
                    group.primary[4]['ssh_host'] = 'bastion.example'
                report, checks = group.prepare()
                with self.assertRaisesRegex(ValueError, '[Uu]nreachable'):
                    cluster.plan_action(report, ACTION, checks=checks)

    def test_primary_preflight_preserves_independent_replica_outage_evidence(self):
        group = GroupFixture('ic-8.4-clusterset-three')
        dr_rows = F._cs_instances('cid-2', 'mf-dr', 30)
        for index, row in enumerate(dr_rows):
            group.profiles.append({'id': 'dr%d' % index, 'name': 'DR %d' % index,
                                   'host': 'mf-dr-%d' % (index + 1), 'port': 3306,
                                   'user': 'admin', 'group': 'production', 'remember_password': True,
                                   'topology_identity': {'server_uuid': row[3]}})
        report, checks = group.prepare()
        evidence = checks['clusterset_reachability']
        self.assertEqual({item['server_uuid'] for item in evidence['maincluster']}, {r[3] for r in primary_rows()})
        self.assertEqual({item['server_uuid'] for item in evidence['drcluster']}, {r[3] for r in dr_rows})
        self.assertTrue(all(item['reachable'] is False for members in evidence.values() for item in members))
        plan = cluster.plan_action(report, ACTION, checks=checks,
                                   parameters={'invalidateReplicaClusters': ['drcluster']})
        self.assertEqual(plan['expect']['clusterset_reachability'], evidence)
        self.assertIn('invalidateReplicaClusters', plan['statements'][0])
        self.assertEqual(Counter(group.opens), Counter({'p0': 2, 'p1': 2, 'p2': 2, 'p3': 1, 'p4': 1,
                                                       'dr0': 1, 'dr1': 1, 'dr2': 1}))

    def test_reachable_saved_alias_vetoes_an_exact_endpoint_connection_failure(self):
        group = GroupFixture()
        # The exact endpoint refuses, but another saved group profile known
        # as this member answers with its current UUID. Cached identity only
        # selects the alias for probing; the successful UUID read is proof.
        alias = dict(group.primary[4], id='alias', name='Primary alias', host='other-endpoint')
        group.profiles.append(alias)
        group.outcomes['alias'] = 'up'
        report, checks = group.prepare()
        self.assertIn('alias', group.opens)
        with self.assertRaisesRegex(ValueError, '[Uu]nreachable'):
            cluster.plan_action(report, ACTION, checks=checks)

    def test_fresh_group_identity_and_prior_success_veto_negative_preflight(self):
        for case in ('unclassified_alias', 'conflicting_saved_identity', 'prior_reachability'):
            with self.subTest(case=case):
                group = GroupFixture()
                primary_uuid = primary_rows()[4][3]
                alias = dict(group.primary[4], id='alias', host='other-endpoint', _fixture_uuid=primary_uuid)
                if case == 'unclassified_alias':
                    alias.pop('topology_identity')
                elif case == 'conflicting_saved_identity':
                    alias['topology_identity'] = {'server_uuid': primary_rows()[3][3]}
                if case != 'prior_reachability':
                    group.profiles.append(alias)
                options = group.group_checks()['read_options']
                report = cluster.read_status(group.source, **options)
                group.opens.clear()
                if case == 'prior_reachability':
                    report._clusterset_reachability.setdefault('maincluster', []).append(
                        {'server_uuid': primary_uuid, 'reachable': True})
                else:
                    # The scope's freshly recorded identity is evidence from
                    # this pass, not the saved topology hint on the profile.
                    options['open_profile'].group_scope.record(alias, 'read', identity={'server_uuid': primary_uuid})
                    if case == 'unclassified_alias':
                        group.outcomes['alias'] = 'up'
                evidence = cluster._clusterset_failover_preflight(report, options['profiles'], options['open_profile'])
                self.assertIn({'server_uuid': primary_uuid, 'reachable': True}, evidence['maincluster'])
                if case != 'prior_reachability':
                    self.assertIn('alias', group.opens)
                primary = next(c for c in report['clusterset']['clusters'] if c['role'] == 'PRIMARY')
                self.assertFalse(cluster._clusterset_unreachable(report, primary, {'clusterset_reachability': evidence}))

    def test_a_reachable_member_does_not_skip_the_other_required_checks(self):
        group = GroupFixture()
        options = group.group_checks()['read_options']
        report = cluster.read_status(group.source, **options)
        group.opens.clear()
        group.outcomes['p0'] = 'up'
        observations = cluster._clusterset_failover_preflight(report, options['profiles'], options['open_profile'])
        self.assertEqual(Counter(group.opens), Counter(['p0', 'p1', 'p2', 'p3', 'p4']))
        primary = next(c for c in report['clusterset']['clusters'] if c['role'] == 'PRIMARY')
        self.assertFalse(cluster._clusterset_unreachable(report, primary, {'clusterset_reachability': observations}))

    def test_preflight_open_limit_and_deadline_fail_shut(self):
        group = GroupFixture()
        with mock.patch.object(cluster, 'CLUSTERSET_FAILOVER_MAX_OPENS', 3):
            report, checks = group.prepare()
        # The ordinary pass and the preflight each get their own cap.
        self.assertEqual(group.opens[:3], ['p0', 'p1', 'p2'])
        self.assertEqual(Counter(group.opens[3:]), Counter(['p0', 'p1', 'p2']))
        with self.assertRaisesRegex(ValueError, '[Uu]nreachable'):
            cluster.plan_action(report, ACTION, checks=checks)

        group = GroupFixture()
        options = group.group_checks()['read_options']
        report = cluster.read_status(group.source, **options)
        group.opens.clear()
        clock = [0.0]

        def delayed(profile):
            clock[0] += 4.0
            return options['open_profile'](profile)

        delayed.group_scope = options['open_profile'].group_scope
        with mock.patch.object(cluster, 'CLUSTERSET_FAILOVER_TIMEOUT', 7.0), \
                mock.patch.object(cluster.time, 'monotonic', side_effect=lambda: clock[0]):
            evidence = cluster._clusterset_failover_preflight(report, options['profiles'], delayed)
        self.assertEqual(group.opens, ['p0', 'p1'])
        primary = next(c for c in report['clusterset']['clusters'] if c['role'] == 'PRIMARY')
        self.assertFalse(cluster._clusterset_unreachable(report, primary, {'clusterset_reachability': evidence}))

    def test_missing_or_ambiguous_exact_routes_cannot_certify_a_member_down(self):
        for case in ('missing', 'foreign_only', 'host_only', 'other_host_only', 'duplicate_endpoint',
                     'duplicate_uuid', 'conflicting_cache', 'ssh'):
            with self.subTest(case=case):
                group = GroupFixture()
                rows = group.source.scenario['tables'][INSTANCE_TABLE]
                final = next(row for row in rows if row[3] == primary_rows()[4][3])
                if case == 'missing':
                    group.profiles.remove(group.primary[4])
                elif case == 'foreign_only':
                    group.profiles.remove(group.primary[4])
                elif case == 'host_only':
                    final[2] = final[4] = final[5] = group.primary[4]['host']
                elif case == 'other_host_only':
                    other = next(row for row in rows if row[3] == primary_rows()[3][3])
                    other[4] = group.primary[4]['host']
                elif case == 'duplicate_endpoint':
                    final[2] = final[4] = final[5] = 'mf-main-4:3306'
                elif case == 'duplicate_uuid':
                    final[3] = primary_rows()[3][3]
                elif case == 'conflicting_cache':
                    group.primary[4]['topology_identity'] = {'server_uuid': primary_rows()[3][3]}
                else:
                    group.primary[4]['ssh_host'] = 'bastion.example'
                report, checks = group.prepare()
                with self.assertRaisesRegex(ValueError, '[Uu]nreachable'):
                    cluster.plan_action(report, ACTION, checks=checks)
                self.assertNotIn('foreign', group.opens)

    def test_send_repeats_a_full_preflight_and_refuses_if_a_member_recovers(self):
        group = GroupFixture()
        report, checks = group.prepare()
        plan = cluster.plan_action(report, ACTION, checks=checks)
        group.opens.clear()
        group.events.clear()
        group.outcomes['p4'] = 'up'
        shell = mock.Mock()
        before_send = mock.Mock()
        with mock.patch.object(cluster, 'action_checks', return_value={'shell': SHELL}), \
                mock.patch.object(cluster_shell.ShellWriter, 'run_write', autospec=True, side_effect=shell):
            result = cluster.execute_action(group.source, plan, typed=plan['typed_guard'],
                                            before_send=before_send, group_checks=group.group_checks)
        self.assertEqual(result['status'], 'refused', result)
        self.assertIn('p4', group.opens)
        shell.assert_not_called()
        before_send.assert_not_called()

    def test_send_repeats_preflight_before_the_sender_runs(self):
        group = GroupFixture()
        report, checks = group.prepare()
        plan = cluster.plan_action(report, ACTION, checks=checks)
        group.opens.clear()
        group.events.clear()

        def refuse_sender(*args, **kwargs):
            group.events.append(('sender', None))
            raise reviewed_writes.Refusal('Test stopped before transport.', 'changed')

        with mock.patch.object(cluster, 'action_checks', return_value={'shell': SHELL}), \
                mock.patch.object(cluster_shell.ShellWriter, 'run_write', autospec=True, side_effect=refuse_sender):
            result = cluster.execute_action(group.source, plan, typed=plan['typed_guard'],
                                            group_checks=group.group_checks)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(group.opens[:3], ['p0', 'p1', 'p2'])
        self.assertEqual(Counter(group.opens[3:]), Counter(['p0', 'p1', 'p2', 'p3', 'p4']))
        self.assertEqual(group.events[-1], ('sender', None))


class TestFailoverGroupAPI(unittest.TestCase):
    def test_review_token_cannot_send_after_primary_member_recovers(self):
        # The real UI path supplies a store-backed opener and re-plans the
        # review token. No write or external mysql client is invoked.
        group = GroupFixture()
        directory = tempfile.mkdtemp(prefix='cluster-failover-', dir=os.environ.get('TMPDIR'))
        store = WorkspaceStore(os.path.join(directory, 'workspace.sqlite3'))
        for profile in group.profiles:
            store.save('profile', dict(profile, type='development'), profile['id'], password='secret', remember=True)
        current = store.get('profile', group.current['id'])
        connection = dict({key: current.get(key) for key in ENDPOINT_FIELDS},
                          profile_id=current['id'], group_name='production', password='secret')
        workspace = types.SimpleNamespace(store=store)
        hosts = {p['host']: p for p in group.primary}
        opened = []

        class Peer(object):
            def __init__(self, profile):
                self.profile = profile

            def __enter__(self):
                return self

            def __exit__(self, *unused):
                return False

            def run(self, sql, extra_flags=None, timeout=None):
                guard = getattr(self, '_login_guard', None)
                if guard:
                    guard()
                if group.outcomes.get(self.profile['id']) == 'up':
                    peer = F.conn('ic-8.4-clusterset-replica')
                    peer.scenario['variables']['server_uuid'] = self.profile['topology_identity']['server_uuid']
                    return peer.run(sql, extra_flags=extra_flags, timeout=timeout)
                raise ConnectorError('mysql client exited 1: ERROR 2003 (HY000): connection refused')

        def factory(**options):
            host = options['host']
            if host == group.current['host']:
                return group.source
            opened.append(host)
            if host not in hosts:
                raise AssertionError('Opened an unclassified or out-of-group endpoint: %s' % host)
            return Peer(hosts[host])

        payload = {'connection': connection, 'action': ACTION, 'target_profile_id': current['id']}
        sender = mock.Mock()
        with mock.patch('myflames.connector.MySQLConnection', side_effect=factory), \
                mock.patch.object(cluster_shell, 'find_shell', return_value=SHELL), \
                mock.patch.object(cluster_shell.ShellWriter, 'run_write', autospec=True, side_effect=sender):
            report = ui.api_response('/api/live/cluster', {'connection': connection}, workspace)
            self.assertEqual(len(opened), 3, opened)
            self.assertEqual(len(set(opened)), 3)
            self.assertTrue(set(opened).issubset(hosts))
            self.assertTrue(next(item for item in report['actions'] if item['id'] == ACTION)['enabled'])
            opened.clear()
            plan = ui.api_response('/api/live/cluster/plan', payload, workspace)
            self.assertEqual(len(set(opened[:3])), 3)
            self.assertTrue(set(opened[:3]).issubset(hosts))
            self.assertEqual(Counter(opened[3:]), Counter(hosts.keys()))
            token = plan.pop('review_token')
            opened.clear()
            group.outcomes['p4'] = 'up'
            result = ui.api_response('/api/live/cluster/action',
                                     dict(payload, plan=plan, review_token=token, confirmed=True,
                                          typed=plan['typed_guard']), workspace)
        self.assertEqual(result['status'], 'refused', result)
        self.assertIn('mf-main-5', opened)
        sender.assert_not_called()


if __name__ == '__main__':
    unittest.main()
