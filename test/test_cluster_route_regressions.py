"""ClusterSet route, fencing and metadata regressions.

Synthetic mysql-client batch replies exercise the public status read and the
bounded failover preflight. No live database or network connection is used.
"""
import copy
import unittest

import _cluster_fixtures as F
import test_clusterset_execution as execution_fixtures
from myflames import cluster, connection_groups
from myflames.connector import ConnectorError


MEMBER_ACTIONS = 'performance_schema.replication_group_member_actions'
MEMBERSHIP = 'mysql_innodb_cluster_metadata.v2_cs_members'
INSTANCES = 'mysql_innodb_cluster_metadata.v2_instances'
GROUPS = 'mysql_innodb_cluster_metadata.v2_gr_clusters'
SHELL = {'path': '/usr/bin/mysqlsh', 'version': '9.7.1', 'version_tuple': [9, 7, 1]}


def primary_read():
    source = F.conn('ic-8.4-clusterset-primary')
    source.scenario['tables'][MEMBER_ACTIONS] = [[0]]
    source.scenario['tables']['performance_schema.replication_connection_status'] = []
    return source


def unavailable_primary(messages, duplicate_first=False):
    """Read a local replica while its primary endpoints return client errors."""
    source = F.conn('ic-8.4-clusterset-replica')
    source.scenario['tables'][MEMBER_ACTIONS] = [[0]]
    tables = source.scenario['tables']
    primary_rows = [r for r in tables[INSTANCES] if r[1] == 'cid-0']
    if duplicate_first:
        # One actual metadata member, contacted through two saved profiles.
        # This isolates whether a timeout vetoes a refusal for that member.
        first_uuid = primary_rows[0][3]
        tables[INSTANCES] = [r for r in tables[INSTANCES]
                             if r[1] != 'cid-0' or r[3] == first_uuid]
        primary_rows = primary_rows[:1] * len(messages)
    current = {'id': 'local', 'name': 'Local', 'group': 'production',
               'host': 'mf-ic-1', 'port': 3306}
    profiles = [current]
    errors = {}
    for index, (row, message) in enumerate(zip(primary_rows, messages)):
        host, port = cluster._split_address(row[5])
        pid = 'main-%d' % index
        profiles.append({'id': pid, 'name': pid, 'group': 'production',
                         'host': host, 'port': port,
                         'topology_identity': {'server_uuid': row[3]}})
        errors[pid] = message

    opened = []

    def opener(profile):
        opened.append(profile['id'])
        raise ConnectorError(errors[profile['id']])

    scope = connection_groups.GroupScope(profiles, current)
    report = cluster.read_status(source, profiles=scope.profiles, open_profile=scope.opener(opener),
                                 current_profile=current, current_profile_id=current['id'])
    primary = next(c for c in report['clusterset']['clusters'] if c['role'] == 'PRIMARY')
    return report, primary, opened


class TestClusterSetRouteRegressions(unittest.TestCase):
    def test_unambiguous_client_refusals_and_unknown_hosts_match_preflight(self):
        for message in (
                "ERROR 2002 (HY000): Can't connect to MySQL server (111)",
                "ERROR 2003 (HY000): Can't connect to MySQL server (111)",
                "ERROR 2005 (HY000): Unknown MySQL server host 'mf-main-1' (-2)",
                "ERROR 2003 (HY000): Can't connect to MySQL server on '10.0.60.1:3306' (111)",
                "ERROR 2003 (HY000): Can't connect to MySQL server on 'db:10060' (111)",
                "ERROR 2005 (HY000): Unknown MySQL server host 'db-110' (-2)"):
            with self.subTest(message=message):
                report, primary, opened = unavailable_primary([message] * 3)
                self.assertEqual(len(opened), 3)
                self.assertTrue(cluster._clusterset_unreachable(report, primary, {}))
                evidence = cluster._clusterset_failover_preflight(
                    report, report._clusterset_profiles, report._clusterset_opener)
                self.assertTrue(cluster._clusterset_unreachable(
                    report, primary, {'clusterset_reachability': evidence}))

    def test_timeouts_auth_and_query_failures_never_prove_unreachable(self):
        messages = [
            'ERROR 2003 (HY000): Connection timed out',
            'ERROR 2005 (HY000): hostname lookup timeout',
            'ERROR 2003 (HY000): connect failed: ETIMEDOUT',
            'ERROR 2005 (HY000): lookup failed: WSAETIMEDOUT',
            'ERROR 2003 (HY000): system error code 110',
            'ERROR 2005 (HY000): resolver error 10060',
            'ERROR 1045 (28000): Access denied',
            'ERROR 2013 (HY000): Lost connection during query',
            'ERROR 2003 (HY000) at line 1: connection failed',
        ]
        for code in (2002, 2003, 2005):
            for timeout_number in (60, 110, 10060):
                messages.append('ERROR %s (HY000): connection failed (%s)' % (code, timeout_number))
        for message in messages:
            with self.subTest(message=message):
                report, primary, _ = unavailable_primary([message] * 3)
                self.assertFalse(cluster._clusterset_unreachable(report, primary, {}))
                actions = {a['id']: a for a in cluster.overview_actions(report, {'shell': SHELL})}
                self.assertFalse(actions['force_primary_cluster:mfcluster']['enabled'])
                evidence = cluster._clusterset_failover_preflight(
                    report, report._clusterset_profiles, report._clusterset_opener)
                self.assertFalse(cluster._clusterset_unreachable(
                    report, primary, {'clusterset_reachability': evidence}))

    def test_timed_out_replica_cannot_enable_forced_removal(self):
        source = primary_read()
        current = {'id': 'local', 'name': 'Local', 'group': 'production'}
        instances = [r for r in source.scenario['tables'][INSTANCES] if r[1] == 'cid-2']
        profiles = [current]
        for index, row in enumerate(instances):
            host, port = cluster._split_address(row[5])
            profiles.append({'id': 'dr-%d' % index, 'name': 'DR %d' % index,
                             'group': 'production', 'host': host, 'port': port,
                             'topology_identity': {'server_uuid': row[3]}})

        def opener(profile):
            raise ConnectorError('ERROR 2003 (HY000): connection failed (110)')

        report = cluster.read_status(source, profiles=profiles, open_profile=opener,
                                     current_profile_id=current['id'])
        replica = next(c for c in report['clusterset']['clusters'] if c['name'] == 'drcluster')
        self.assertFalse(cluster._clusterset_unreachable(report, replica, {}))
        actions = {a['id']: a for a in cluster.overview_actions(report, {'shell': SHELL})}
        self.assertFalse(actions['remove_cluster:drcluster']['enabled'])
        self.assertFalse(actions['remove_cluster_keep:drcluster']['enabled'])

    def test_timeout_vetoes_refusal_for_the_same_member_in_either_order(self):
        refusal = 'ERROR 2003 (HY000): connection refused (111)'
        timeout = 'ERROR 2003 (HY000): connection failed (110)'
        for messages in ((refusal, timeout), (timeout, refusal)):
            with self.subTest(messages=messages):
                report, primary, opened = unavailable_primary(messages, duplicate_first=True)
                self.assertEqual(len(opened), 2)
                self.assertFalse(cluster._clusterset_unreachable(report, primary, {}))
                evidence = cluster._clusterset_failover_preflight(
                    report, report._clusterset_profiles, report._clusterset_opener)
                self.assertFalse(cluster._clusterset_unreachable(
                    report, primary, {'clusterset_reachability': evidence}))

    def test_mismatched_or_missing_identity_vetoes_an_endpoint_refusal(self):
        for result in ('different', 'missing'):
            for first in ('refused', 'answered'):
                with self.subTest(identity=result, first=first):
                    source = F.conn('ic-8.4-clusterset-replica')
                    source.scenario['tables'][MEMBER_ACTIONS] = [[0]]
                    rows = source.scenario['tables'][INSTANCES]
                    member = next(row for row in rows if row[1] == 'cid-0')
                    rows[:] = [row for row in rows if row[1] != 'cid-0' or row[3] == member[3]]
                    current = {'id': 'local', 'name': 'Local', 'group': 'production'}
                    host, port = cluster._split_address(member[5])
                    profiles = [current] + [
                        {'id': pid, 'name': pid, 'group': 'production', 'host': host, 'port': port,
                         'topology_identity': {'server_uuid': member[3]}}
                        for pid in (first, 'answered' if first == 'refused' else 'refused')]

                    def opener(profile):
                        if profile['id'] == 'refused':
                            raise ConnectorError('ERROR 2003 (HY000): connection refused (111)')
                        peer = F.conn('ic-8.4-clusterset-replica')
                        peer.scenario['variables']['server_uuid'] = (
                            'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee' if result == 'different' else None)
                        return peer

                    report = cluster.read_status(source, profiles=profiles, open_profile=opener,
                                                 current_profile_id=current['id'])
                    primary = next(c for c in report['clusterset']['clusters'] if c['role'] == 'PRIMARY')
                    self.assertFalse(cluster._clusterset_unreachable(report, primary, {}))
                    evidence = report._clusterset_reachability['maincluster']
                    self.assertEqual(evidence, [{'server_uuid': member[3], 'reachable': None}])
                    actions = {a['id']: a for a in cluster.overview_actions(report, {'shell': SHELL})}
                    self.assertFalse(actions['force_primary_cluster:mfcluster']['enabled'])

    def test_denied_member_action_preserves_raw_sro_without_inventing_fencing(self):
        for sro in ('ON', 'OFF'):
            with self.subTest(super_read_only=sro):
                source = primary_read()
                source.scenario['variables']['super_read_only'] = sro
                source.scenario['tables'][MEMBER_ACTIONS] = ConnectorError(
                    'ERROR 1142 (42000): SELECT command denied; password=private-test-value')
                cs = cluster.read_status(source)['clusterset']
                primary = next(c for c in cs['clusters'] if c['role'] == 'PRIMARY')
                self.assertIs(primary['super_read_only'], sro == 'ON')
                self.assertIsNone(primary['disable_super_read_only_if_primary'])
                self.assertIsNone(primary['fenced_writes'])
                self.assertIn('fencing', primary['status_reason'].lower())
                self.assertIn('replication_group_member_actions', primary['status_reason'])
                self.assertNotIn('private-test-value', primary['status_reason'])
                if sro == 'ON':
                    self.assertEqual(primary['status'], 'NOT_OK')
                    self.assertEqual(cs['status'], 'UNAVAILABLE')

    def test_unreadable_sro_keeps_fencing_unknown_with_a_reason(self):
        for mode in ('missing', 'denied'):
            with self.subTest(mode=mode):
                source = primary_read()
                if mode == 'missing':
                    source.scenario['variables'].pop('super_read_only', None)
                else:
                    source.scenario['query_errors'] = {
                        "SHOW GLOBAL VARIABLES WHERE Variable_name IN ('super_read_only')":
                            ConnectorError('ERROR 1142 (42000): SELECT command denied')}
                cs = cluster.read_status(source)['clusterset']
                primary = next(c for c in cs['clusters'] if c['role'] == 'PRIMARY')
                self.assertIsNone(primary['super_read_only'])
                self.assertIsNone(primary['fenced_writes'])
                self.assertIn('super_read_only', primary['status_reason'])
                self.assertEqual(primary['status'], 'NOT_READ')
                self.assertEqual(cs['status'], 'UNKNOWN')

    def test_missing_sro_must_not_establish_primary_writability(self):
        source = primary_read()
        source.scenario['variables'].pop('super_read_only', None)
        cs = cluster.read_status(source)['clusterset']
        primary = next(c for c in cs['clusters'] if c['role'] == 'PRIMARY')
        self.assertEqual(primary['status'], 'NOT_READ')
        self.assertEqual(cs['status'], 'UNKNOWN')

    def test_missing_member_action_keeps_fencing_unknown(self):
        source = primary_read()
        source.scenario['tables'][MEMBER_ACTIONS] = []
        primary = next(c for c in cluster.read_status(source)['clusterset']['clusters']
                       if c['role'] == 'PRIMARY')
        self.assertIs(primary['super_read_only'], False)
        self.assertIsNone(primary['disable_super_read_only_if_primary'])
        self.assertIsNone(primary['fenced_writes'])
        self.assertIn('replication_group_member_actions', primary['status_reason'])

    def test_missing_or_denied_fencing_reads_refuse_routing_review(self):
        for evidence in ('missing_sro', 'denied_sro', 'missing_action', 'denied_action'):
            with self.subTest(evidence=evidence):
                source = primary_read()
                if evidence == 'missing_sro':
                    source.scenario['variables'].pop('super_read_only')
                elif evidence == 'denied_sro':
                    source.scenario['query_errors'] = {
                        "SHOW GLOBAL VARIABLES WHERE Variable_name IN ('super_read_only')":
                            ConnectorError('ERROR 1142 (42000): SELECT command denied')}
                else:
                    source.scenario['tables'][MEMBER_ACTIONS] = (
                        [] if evidence == 'missing_action' else
                        ConnectorError('ERROR 1142 (42000): SELECT command denied'))
                report = cluster.read_status(source)
                primary = next(c for c in report['clusterset']['clusters'] if c['role'] == 'PRIMARY')
                self.assertIsNone(primary['fenced_writes'])
                entry = next(a for a in cluster.overview_actions(report, {'shell': SHELL})
                             if a['id'] == 'set_routing_option')
                self.assertFalse(entry['enabled'], entry)
                if evidence != 'denied_sro':
                    self.assertIn('Cannot confirm', entry['why_not'])
                else:
                    # The denied combined group/SRO read cannot establish
                    # the ONLINE-primary route needed before checking fencing.
                    self.assertIn('primary cluster was not read', entry['why_not'])
                with self.assertRaises(cluster.ActionUnavailable):
                    cluster.plan_action(report, 'set_routing_option', checks={'shell': SHELL},
                                        parameters={'option': 'target_cluster', 'value': 'primary'})

    def test_unknown_or_absent_primary_fencing_disables_each_dependent_action(self):
        from test_cluster_actions import clusterset_checks, clusterset_report

        actions = ('set_primary_cluster:drcluster', 'rejoin_cluster:oldcluster',
                   'repair_cluster:drcluster', 'remove_cluster:drcluster',
                   'remove_cluster_keep:drcluster', 'set_routing_option',
                   'set_routing_option:router-host::rw', 'create_replica_cluster:spare')
        for aid in actions:
            for evidence in ('unknown', 'absent'):
                with self.subTest(action=aid, fencing=evidence):
                    report = clusterset_report()
                    checks = clusterset_checks(report)
                    if aid.startswith('repair_cluster:'):
                        replica = next(c for c in report['clusterset']['clusters']
                                       if c['name'] == 'drcluster')
                        replica['status'] = 'NOT_REPLICATING'
                        replica['channel']['receiver_state'] = 'OFF'
                    # Prove the refusal is caused by fencing evidence alone.
                    before = {a['id']: a for a in cluster.overview_actions(report, checks)}
                    self.assertTrue(before[aid]['enabled'], before[aid])
                    primary = next(c for c in report['clusterset']['clusters'] if c['role'] == 'PRIMARY')
                    if evidence == 'unknown':
                        primary['fenced_writes'] = None
                    else:
                        primary.pop('fenced_writes')
                    entry = next(a for a in cluster.overview_actions(report, checks) if a['id'] == aid)
                    self.assertFalse(entry['enabled'], entry)
                    self.assertIn('Cannot confirm', entry['why_not'])
                    self.assertIn('fenced', entry['why_not'])
                    self.assertTrue(entry['fix'])
                    with self.assertRaisesRegex(cluster.ActionUnavailable, 'Cannot confirm'):
                        cluster.plan_action(report, aid, checks=checks)

    def test_unknown_primary_fencing_preserves_diagnostic_dissolve_and_confirmed_failover(self):
        from test_cluster_actions import clusterset_checks, clusterset_report, clusterset_unreachable

        report = clusterset_report()
        checks = clusterset_checks(report)
        primary = next(c for c in report['clusterset']['clusters'] if c['role'] == 'PRIMARY')
        primary['fenced_writes'] = None
        actions = {a['id']: a for a in cluster.overview_actions(report, checks)}
        for aid in ('check_clusterset', 'dissolve_clusterset'):
            self.assertTrue(actions[aid]['enabled'], actions[aid])
        self.assertFalse(actions['force_primary_cluster:drcluster']['enabled'])
        clusterset_unreachable(report, checks, primary['name'])
        actions = {a['id']: a for a in cluster.overview_actions(report, checks)}
        self.assertTrue(actions['force_primary_cluster:drcluster']['enabled'], actions)

    def test_denied_member_action_on_remote_primary_keeps_route_and_sro(self):
        source = F.conn('ic-8.4-clusterset-replica')
        source.scenario['tables'][MEMBER_ACTIONS] = [[0]]
        peer = F.clusterset_peer('cid-0', 'mf-main', 20, 'ic-8.4-clusterset-replica')
        peer.scenario['variables']['super_read_only'] = 'ON'
        peer.scenario['tables'][MEMBER_ACTIONS] = ConnectorError('ERROR 1142: SELECT denied')
        member = F._cs_instances('cid-0', 'mf-main', 20)[0]
        profiles = [{'id': 'local', 'name': 'Local', 'group': 'production'},
                    {'id': 'main', 'name': 'Main', 'group': 'production',
                     'host': 'mf-main-1', 'port': 3306,
                     'topology_identity': {'server_uuid': member[3]}}]
        opened = []

        def opener(profile):
            opened.append(profile['id'])
            return peer

        cs = cluster.read_status(source, profiles=profiles, open_profile=opener,
                                 current_profile_id='local')['clusterset']
        primary = next(c for c in cs['clusters'] if c['role'] == 'PRIMARY')
        self.assertEqual(opened, ['main'])
        self.assertIs(primary['super_read_only'], True)
        self.assertIsNone(primary['fenced_writes'])
        self.assertEqual(primary['status'], 'NOT_OK')
        self.assertEqual(cs['status'], 'UNAVAILABLE')
        self.assertIn('replication_group_member_actions', primary['status_reason'])

    def test_literal_null_cluster_name_survives_metadata_snapshot(self):
        for cid in ('cid-1', 'cid-2'):
            with self.subTest(cluster_id=cid):
                source = primary_read()
                for row in source.scenario['tables'][MEMBERSHIP]:
                    if row[1] == cid:
                        row[2] = 'NULL'
                    row[5] = 'NULL'
                    row[7] = None
                    if row[1] == 'cid-2':
                        # A SQL NULL role must remain None even as the name
                        # and domain with literal text NULL survive.
                        row[3], row[4] = None, '1'
                for row in source.scenario['tables'][GROUPS]:
                    if row[0] == cid:
                        row[1] = 'NULL'
                report = cluster.read_status(source)
                cs = report['clusterset']
                self.assertEqual(cs['name'], 'NULL')
                self.assertEqual(cs['routing_options']['options'], {})
                named = next(c for c in cs['clusters'] if c['name'] == 'NULL')
                self.assertEqual(named['local'], cid == 'cid-1')
                if cid == 'cid-1':
                    self.assertEqual(cs['local_cluster'], 'NULL')
                    self.assertEqual(report['cluster']['name'], 'NULL')
                invalid = next(c for c in cs['clusters'] if c['invalidated'])
                self.assertIsNone(invalid['role'])


class TestClusterSetFencingBeforeSend(execution_fixtures.EndpointHarness):
    def test_lost_fencing_evidence_after_review_refuses_without_sending(self):
        # The real endpoint rechecks eligibility under a genuine review token.
        # Only the SQL read and Shell transport are supplied by EndpointHarness.
        for aid in ('set_primary_cluster:drcluster', 'remove_cluster:drcluster',
                    'set_routing_option:router::rw'):
            for evidence in ('unknown', 'absent'):
                with self.subTest(action=aid, fencing=evidence):
                    self.report = copy.deepcopy(self.baseline)
                    plan = self.plan(aid)
                    primary = next(c for c in self.report['clusterset']['clusters'] if c['role'] == 'PRIMARY')
                    if evidence == 'unknown':
                        primary['fenced_writes'] = None
                    else:
                        primary.pop('fenced_writes')
                    result = self.apply(plan)
                    self.assertEqual(result['status'], 'refused', result)
                    self.assertIn('Cannot confirm', result['message'])
                    self.assertEqual(result['sent'], [])
                    self.assertEqual(result['not_sent'], plan['statements'])
                    self.assertEqual(result['states'], ['not_sent'])
                    self.assertEqual(self.sent, [])


if __name__ == '__main__':
    unittest.main()
