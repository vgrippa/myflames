"""InnoDB Cluster and InnoDB ClusterSet changes run through MySQL Shell
(myflames/cluster_shell.py and the Shell half of myflames/cluster.py's
reviewed actions), and the ClusterSet read (cluster.py _read_clusterset).

The ClusterSet servers are synthetic (test/_cluster_fixtures.py, "InnoDB
ClusterSet"); the MISSING member is the live recording ic-8.4-member-offline.
MySQL Shell itself is replaced: by a recorder of what would be sent, and,
for the command line and the option file, by a stand-in ``mysqlsh`` program.
"""
import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import _cluster_fixtures as F
from myflames import cluster as m, cluster_shell, dbops, ui
from myflames.connector import ConnectorError

SHELL = {'path': '/opt/mysqlsh/bin/mysqlsh', 'realpath': '/opt/mysqlsh/bin/mysqlsh', 'version': '8.4.6',
         'version_tuple': [8, 4, 6]}
CHECKS = {'shell': SHELL}
TAG = '/*myflames:cluster_op*/ '


def member(report, name):
    return next(x for x in report['members'] if x['name'] == name)


def local(report):
    return next(x for x in report['members'] if x['local'])


def offered(report, key, checks=CHECKS):
    return [a['id'] for a in m.available_actions(report, key, checks)]


def missing(report):
    return next(x for x in report['members'] if x['state'] == 'MISSING')


class Home(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {'HOME': self.tmp.name, 'USERPROFILE': self.tmp.name,
                                               'MYFLAMES_SECRET_BACKEND': 'vault'})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)


class TestClusterSetRead(unittest.TestCase):
    def test_outside_a_clusterset_there_is_no_clusterset(self):
        for name in ('ic-8.4-healthy', 'ic-8.4-member-offline', 'ic-8.4-metadata-2.4', 'gr-8.4-multi-primary'):
            self.assertNotIn('clusterset', F.report(name), name)

    def test_metadata_before_2_1_is_not_asked(self):
        conn = F.ScenarioConn(F._ic([F._gr_member(i) for i in range(3)], [F._gr_stats(i) for i in range(3)],
                                    version=(2, 0, 0)))
        report = m.read_status(conn)
        self.assertNotIn('clusterset', report)
        self.assertFalse(any('v2_cs_members' in sql for sql, _ in conn.calls))

    def test_primary_cluster(self):
        report = F.report('ic-8.4-clusterset-primary')
        cs = report['clusterset']
        self.assertEqual((cs['name'], cs['local_cluster'], cs['local_role'], cs['primary_cluster']),
                         ('mfcs', 'mfcluster', 'PRIMARY', 'mfcluster'))
        self.assertEqual([(c['name'], c['role'], c['invalidated'], c['local']) for c in cs['clusters']],
                         [('mfcluster', 'PRIMARY', False, True), ('drcluster', 'REPLICA', False, False)])
        # The other cluster's instances come from the shared metadata; this
        # cluster's members are the report's members.
        self.assertEqual(cs['clusters'][0]['instances'], [])
        self.assertEqual([i['address'] for i in cs['clusters'][1]['instances']],
                         ['mf-dr-1:3306', 'mf-dr-2:3306', 'mf-dr-3:3306'])
        self.assertEqual(len(report['members']), 3, 'other clusters are never drawn as members')
        self.assertFalse(cs['from_local_copy'])
        self.assertIsNone(cs['channel'], 'the primary cluster has no clusterset_replication channel')
        self.assertEqual(report['status'], 'ok')
        text = m.format_text(report)
        self.assertNotIn('\nClusterSet ', text)
        self.assertNotIn('drcluster  REPLICA', text)
        self.assertEqual(json.loads(json.dumps(report))['clusterset'], cs)

    def test_replica_cluster_reads_its_channel_on_its_primary(self):
        report = F.report('ic-8.4-clusterset-replica')
        cs = report['clusterset']
        self.assertEqual((cs['local_role'], cs['primary_cluster'], cs['from_local_copy']),
                         ('REPLICA', 'maincluster', True))
        self.assertEqual((cs['channel']['receiver_state'], cs['channel']['applier_state'], cs['channel']['source']),
                         ('ON', 'ON', 'mf-main-1:3306'))
        main = cs['clusters'][0]
        self.assertEqual([i['address'] for i in main['instances'] if i['source']], ['mf-main-1:3306'])
        self.assertNotIn('(clusterset_replication source)', m.format_text(report))
        self.assertEqual(report['status'], 'ok')
        self.assertTrue(any('replicated copy' in n for n in report['notes']))
        self.assertNotIn('clusterset_replication from mf-main-1:3306: receiver ON, applier ON', m.format_text(report))

    def test_channel_off_is_a_problem(self):
        report = F.report('ic-8.4-clusterset-replica-channel-off')
        (finding,) = [f for f in report['findings'] if f['id'] == 'clusterset_channel']
        self.assertEqual(finding['status'], 'problem')
        self.assertIn('2003', finding['text'])
        self.assertEqual(finding['member'], local(report)['key'])
        self.assertEqual(report['status'], 'problem')

    def test_channel_is_read_only_on_the_primary(self):
        scenario = F.SCENARIOS['ic-8.4-clusterset-replica']
        conn = F.ScenarioConn(dict(scenario, variables=F._mysql_vars(server_uuid=F.SERVER_UUIDS[1])))
        report = m.read_status(conn)
        self.assertIsNone(report['clusterset']['channel'])
        self.assertFalse(any('clusterset_replication' in sql for sql, _ in conn.calls))

    def test_invalidated_clusters(self):
        seen = F.report('ic-8.4-clusterset-invalidated')
        (finding,) = [f for f in seen['findings'] if f['id'] == 'clusterset_invalidated']
        self.assertEqual(finding['status'], 'watch')
        self.assertIn('drcluster', finding['text'])
        self.assertTrue(next(c for c in seen['clusterset']['clusters'] if c['name'] == 'drcluster')['invalidated'])
        self.assertNotIn('\nClusterSet ', m.format_text(seen))
        mine = F.report('ic-8.4-clusterset-local-invalidated')
        (finding,) = [f for f in mine['findings'] if f['id'] == 'clusterset_invalidated']
        self.assertEqual((finding['status'], mine['clusterset']['invalidated']), ('problem', True))
        self.assertIn('rejoinCluster', finding['text'])

    def test_read_is_read_only(self):
        from _perf_replay import assert_read_only
        for name in ('ic-8.4-clusterset-primary', 'ic-8.4-clusterset-replica-channel-off'):
            conn = F.conn(name)
            m.read_status(conn)
            assert_read_only(self, conn, m.CLUSTER_TAG)

    def test_denied_metadata_view_is_a_note(self):
        scenario = dict(F.SCENARIOS['ic-8.4-clusterset-primary'])
        scenario['tables'] = dict(scenario['tables'])
        scenario['tables']['mysql_innodb_cluster_metadata.v2_cs_members'] = ConnectorError(
            "mysql client exited 1: ERROR 1142 (42000) at line 1: SELECT command denied to user 'app'@'%' for "
            "table 'v2_cs_members'")
        report = m.read_status(F.ScenarioConn(scenario))
        self.assertNotIn('clusterset', report)
        self.assertTrue(any('v2_cs_members' in n for n in report['notes']))


class TestOffered(unittest.TestCase):
    def test_shell_action_version_thresholds(self):
        cases = (
            ('ic-8.4-healthy', 'switch_to_multi_primary', 'local', (8, 0, 0), (7, 9, 99)),
            ('ic-8.4-healthy', 'switch_to_single_primary', 'multi', (8, 0, 0), (7, 9, 99)),
            ('ic-8.4-member-offline', 'rejoin_instance', 'missing', (8, 1, 0), (8, 0, 34)),
            ('ic-8.4-clusterset-primary', 'set_primary_cluster:drcluster', 'local', (8, 0, 27), (8, 0, 26)),
            ('ic-8.4-clusterset-replica', 'force_primary_cluster', 'local', (8, 0, 27), (8, 0, 26)),
            ('ic-8.4-clusterset-invalidated', 'rejoin_cluster:drcluster', 'local', (8, 0, 27), (8, 0, 26)),
            ('ic-8.4-clusterset-replica', 'switchover', 'secondary', (8, 0, 27), (8, 0, 26)),
        )
        for scenario, action, who, threshold, below in cases:
            report = F.report(scenario)
            if who == 'multi':
                report['cluster']['topology'] = 'multi_primary'
            target = (missing(report) if who == 'missing' else
                      next(x for x in report['members'] if x['role'] == 'SECONDARY') if who == 'secondary' else
                      local(report))
            for version, present in ((threshold, True), (below, False)):
                with self.subTest(action=action, version=version):
                    checks = {'shell': dict(SHELL, version_tuple=list(version))}
                    self.assertEqual(action in offered(report, target['key'], checks), present)
                    if present:
                        self.assertEqual(m.plan_action(report, action, target['key'], checks)['via'], 'mysqlsh')
                    else:
                        with self.assertRaises(ValueError):
                            m.plan_action(report, action, target['key'], checks)

    def test_shell_without_a_version_offers_no_shell_actions(self):
        for version in (None, []):
            for scenario in ('ic-8.4-healthy', 'ic-8.4-member-offline', 'ic-8.4-clusterset-replica'):
                report = F.report(scenario)
                checks = {'shell': dict(SHELL, version_tuple=version)}
                for target in report['members']:
                    self.assertFalse(any(m._via_shell(report, a) for a in offered(report, target['key'], checks)))

    def test_nothing_from_mysql_shell_without_mysql_shell(self):
        for name in ('ic-8.4-healthy', 'ic-8.4-member-offline', 'ic-8.4-clusterset-primary',
                     'ic-8.4-clusterset-replica'):
            report = F.report(name)
            for x in report['members']:
                ids = offered(report, x['key'], None)
                for action in ids:
                    self.assertFalse(m._via_shell(report, action), (name, action))

    def test_innodb_cluster_mode_switch(self):
        report = F.report('ic-8.4-healthy')
        self.assertIn('switch_to_multi_primary', offered(report, local(report)['key']))
        for x in report['members']:
            if not x['local']:
                self.assertNotIn('switch_to_multi_primary', offered(report, x['key']))
        multi = F.report('ic-8.4-healthy')
        multi['cluster']['topology'] = 'multi_primary'
        self.assertIn('switch_to_single_primary', offered(multi, local(multi)['key']))

    def test_no_mode_switch_unless_every_member_is_online(self):
        report = F.report('ic-8.4-member-offline')
        self.assertNotIn('switch_to_multi_primary', offered(report, local(report)['key']),
                         'MySQL Shell refuses unless every member is ONLINE')
        degraded = F.report('ic-8.4-degraded')
        self.assertFalse(any(a.startswith('switch_to') for x in degraded['members']
                             for a in offered(degraded, x['key'])))

    def test_no_mode_switch_with_an_error_or_offline_group_member(self):
        healthy = F.report('ic-8.4-healthy')
        for state in ('ERROR', 'OFFLINE'):
            for topology in ('single_primary', 'multi_primary'):
                with self.subTest(state=state, topology=topology):
                    report = json.loads(json.dumps(healthy))
                    report['cluster']['topology'] = topology
                    next(x for x in report['members'] if not x['local'])['state'] = state
                    target = local(report)
                    self.assertEqual(target['state'], 'ONLINE')
                    ids = offered(report, target['key'])
                    for action in ('switch_to_multi_primary', 'switch_to_single_primary'):
                        self.assertNotIn(action, ids)
                        with self.assertRaises(ValueError):
                            m.plan_action(report, action, target['key'], CHECKS)

    def test_no_mode_switch_in_a_clusterset(self):
        for name in ('ic-8.4-clusterset-primary', 'ic-8.4-clusterset-replica'):
            report = F.report(name)
            self.assertFalse([a for a in offered(report, local(report)['key']) if a.startswith('switch_to')], name)

    def test_rejoin_on_a_missing_member_only(self):
        report = F.report('ic-8.4-member-offline')
        target = missing(report)
        self.assertEqual(offered(report, target['key']), ['rejoin_instance'])
        self.assertEqual(offered(report, target['key'], None), [], 'needs MySQL Shell')
        for x in report['members']:
            if x is not target:
                self.assertNotIn('rejoin_instance', offered(report, x['key']))

    def test_no_rejoin_from_a_member_that_is_not_online_or_has_no_address(self):
        report = F.report('ic-8.4-member-offline')
        local(report)['state'] = 'RECOVERING'
        self.assertEqual(offered(report, missing(report)['key']), [])
        report = F.report('ic-8.4-member-offline')
        missing(report)['address'] = "mf-ic-3'); dba.dropMetadataSchema(); ('"
        self.assertEqual(offered(report, missing(report)['key']), [])

    def test_clusterset_actions(self):
        primary = F.report('ic-8.4-clusterset-primary')
        self.assertIn('set_primary_cluster:drcluster', offered(primary, local(primary)['key']))
        self.assertNotIn('force_primary_cluster', offered(primary, local(primary)['key']))
        replica = F.report('ic-8.4-clusterset-replica')
        ids = offered(replica, local(replica)['key'])
        self.assertIn('set_primary_cluster:mfcluster', ids)
        self.assertIn('force_primary_cluster', ids)
        self.assertNotIn('set_primary_cluster:maincluster', ids, 'already the primary cluster')
        invalidated = F.report('ic-8.4-clusterset-invalidated')
        ids = offered(invalidated, local(invalidated)['key'])
        self.assertIn('rejoin_cluster:drcluster', ids)
        self.assertNotIn('set_primary_cluster:drcluster', ids, 'an invalidated cluster is rejoined first')
        mine = F.report('ic-8.4-clusterset-local-invalidated')
        ids = offered(mine, local(mine)['key'])
        self.assertEqual([a for a in ids if a.startswith(('set_primary', 'force'))], [])
        self.assertIn('rejoin_cluster:mfcluster', ids)

    def test_clusterset_actions_only_on_the_connected_member(self):
        report = F.report('ic-8.4-clusterset-primary')
        for x in report['members']:
            if not x['local']:
                self.assertFalse([a for a in offered(report, x['key']) if ':' in a])

    def test_switchover_in_a_replica_cluster_needs_mysql_shell(self):
        report = F.report('ic-8.4-clusterset-replica')
        secondary = next(x for x in report['members'] if x['role'] == 'SECONDARY')
        self.assertNotIn('switchover', offered(report, secondary['key'], None),
                         'the UDF refuses while the clusterset_replication channel runs')
        self.assertIn('switchover', offered(report, secondary['key']))
        primary = F.report('ic-8.4-clusterset-primary')
        secondary = next(x for x in primary['members'] if x['role'] == 'SECONDARY')
        self.assertIn('switchover', offered(primary, secondary['key'], None), 'the primary cluster has no channel')

    def test_workspace_reports_match_the_schema(self):
        from test_chart_data import validate
        schema = json.loads((Path(__file__).resolve().parent.parent / 'docs' / 'schemas' / 'cluster-1.0.json')
                            .read_text(encoding='utf-8'))
        for name in F.ALL:
            validate(m.with_actions(F.report(name), shell=SHELL), schema)


class TestPlans(unittest.TestCase):
    def test_mode_switch_runs_mysql_shell(self):
        report = F.report('ic-8.4-healthy')
        plan = m.plan_action(report, 'switch_to_multi_primary', local(report)['key'], CHECKS)
        self.assertEqual(plan['statements'], [TAG + 'dba.getCluster().switchToMultiPrimaryMode();'])
        self.assertEqual((plan['via'], plan['typed_guard'], plan['timeout_s']), ('mysqlsh', 'mfcluster',
                                                                                 m.SHELL_TIMEOUT))
        self.assertEqual(plan['expect']['shell'], SHELL['path'])
        self.assertIn('auto_increment', plan['description'])
        self.assertEqual(plan['undo'], 'dba.getCluster().switchToSinglePrimaryMode();')
        self.assertIn('MySQL Shell 8.4.6', dict(plan['facts'])['Runs with'])
        with self.assertRaises(ValueError):
            m.plan_action(report, 'switch_to_multi_primary', local(report)['key'])

    def test_rejoin_names_the_instance(self):
        report = F.report('ic-8.4-member-offline')
        target = missing(report)
        plan = m.plan_action(report, 'rejoin_instance', target['key'], CHECKS)
        self.assertEqual(plan['statements'],
                         [TAG + 'dba.getCluster().rejoinInstance("%s", {recoveryMethod: "incremental"});'
                          % target['address']])
        self.assertEqual(plan['typed_guard'], target['name'])
        self.assertEqual(plan['expect']['missing'], [target['key']])
        self.assertIn('incremental recovery from a donor', plan['description'])
        self.assertNotIn('clone', plan['description'])
        warnings = ' '.join(plan['warnings'])
        self.assertIn("no longer in any member's binary logs", warnings)
        self.assertIn('MySQL Shell refuses and nothing changes', warnings)
        self.assertIn('Run cluster.rejoinInstance("%s", {recoveryMethod: "clone"}) in MySQL Shell' % target['address'],
                      warnings)
        self.assertNotIn('clone', ' '.join(plan['checks']))
        self.assertNotIn('clone', m.available_actions(report, target['key'], CHECKS)[0]['description'])

    def test_ssh_warning_uses_the_connected_member_and_does_not_change_statements(self):
        cases = (('ic-8.4-healthy', 'switch_to_multi_primary', 'local'),
                 ('ic-8.4-healthy', 'switch_to_single_primary', 'multi'),
                 ('ic-8.4-member-offline', 'rejoin_instance', 'missing'),
                 ('ic-8.4-clusterset-primary', 'set_primary_cluster:drcluster', 'local'),
                 ('ic-8.4-clusterset-replica', 'force_primary_cluster', 'local'),
                 ('ic-8.4-clusterset-invalidated', 'rejoin_cluster:drcluster', 'local'),
                 ('ic-8.4-clusterset-replica', 'switchover', 'secondary'))
        for scenario, action, who in cases:
            with self.subTest(action=action):
                report = F.report(scenario)
                if who == 'multi':
                    report['cluster']['topology'] = 'multi_primary'
                target = (missing(report) if who == 'missing' else
                          next(x for x in report['members'] if x['role'] == 'SECONDARY') if who == 'secondary' else
                          local(report))
                direct = m.plan_action(report, action, target['key'], CHECKS)
                false_flag = m.plan_action(report, action, target['key'], dict(CHECKS, ssh=False))
                tunneled = m.plan_action(report, action, target['key'], dict(CHECKS, ssh=True))
                warning = ("This connection uses an SSH tunnel. MySQL Shell runs on this computer: it reaches %s "
                           "through the tunnel, but connects to the other %s at the addresses in the cluster "
                           "metadata directly, so they must be reachable from here." % (
                               local(report)['name'], 'members and clusters' if report.get('clusterset') else 'members'))
                self.assertEqual(direct, false_flag)
                self.assertEqual(tunneled['warnings'], direct['warnings'] + [warning])
                self.assertEqual(tunneled['statements'], direct['statements'])
                self.assertEqual(tunneled['expect'], direct['expect'])

    def test_action_checks_records_ssh_only_for_shell_actions(self):
        report = F.report('ic-8.4-healthy')
        with mock.patch.object(cluster_shell, 'find_shell', return_value=SHELL):
            for host in (None, '', 'bastion.example'):
                conn = type('Conn', (), {'ssh_host': host})()
                self.assertEqual(m.action_checks(conn, report, 'switch_to_multi_primary'),
                                 dict(CHECKS, ssh=bool(host)))
                self.assertEqual(m.action_checks(conn, report, 'switchover'), {})
            self.assertEqual(m.action_checks(object(), report, 'switch_to_multi_primary'), dict(CHECKS, ssh=False))
            replica = F.report('ic-8.4-clusterset-replica')
            conn = type('Conn', (), {'ssh_host': 'bastion.example'})()
            self.assertEqual(m.action_checks(conn, replica, 'switchover'), dict(CHECKS, ssh=True))

    def test_clusterset_plans(self):
        primary = F.report('ic-8.4-clusterset-primary')
        key = local(primary)['key']
        plan = m.plan_action(primary, 'set_primary_cluster:drcluster', key, CHECKS)
        self.assertEqual(plan['statements'], [TAG + 'dba.getClusterSet().setPrimaryCluster("drcluster");'])
        self.assertEqual(plan['typed_guard'], 'drcluster')
        self.assertEqual(plan['undo'], 'dba.getClusterSet().setPrimaryCluster("mfcluster");')
        self.assertEqual(plan['expect']['clusterset'], [['mfcluster', 'PRIMARY', False],
                                                        ['drcluster', 'REPLICA', False]])
        replica = F.report('ic-8.4-clusterset-replica')
        force = m.plan_action(replica, 'force_primary_cluster', local(replica)['key'], CHECKS)
        self.assertEqual(force['statements'], [TAG + 'dba.getClusterSet().forcePrimaryCluster("mfcluster");'])
        self.assertIn('Cannot be undone', force['undo'])
        self.assertTrue(any('replicated copy' in w for w in force['warnings']))
        invalidated = F.report('ic-8.4-clusterset-invalidated')
        rejoin = m.plan_action(invalidated, 'rejoin_cluster:drcluster', local(invalidated)['key'], CHECKS)
        self.assertEqual(rejoin['statements'], [TAG + 'dba.getClusterSet().rejoinCluster("drcluster");'])
        with self.assertRaises(ValueError):
            m.plan_action(primary, 'set_primary_cluster:mfcluster', key, CHECKS)
        with self.assertRaises(ValueError):
            m.plan_action(primary, 'rejoin_cluster:drcluster', key, CHECKS)

    def test_replica_cluster_switchover_uses_set_primary_instance(self):
        report = F.report('ic-8.4-clusterset-replica')
        secondary = next(x for x in report['members'] if x['role'] == 'SECONDARY')
        plan = m.plan_action(report, 'switchover', secondary['key'], CHECKS)
        self.assertEqual(plan['statements'],
                         [TAG + 'dba.getCluster().setPrimaryInstance("%s");' % secondary['address']])
        self.assertEqual(plan['via'], 'mysqlsh')
        plain = F.report('ic-8.4-healthy')
        target = next(x for x in plain['members'] if x['role'] == 'SECONDARY' and x['state'] == 'ONLINE')
        self.assertNotIn('via', m.plan_action(plain, 'switchover', target['key'], CHECKS),
                         'outside a replica cluster the UDF still does it')

    def test_every_shell_statement_is_one_line_and_risky(self):
        from myflames import reviewed_writes
        cases = (('ic-8.4-healthy', 'switch_to_multi_primary', None), ('ic-8.4-member-offline', 'rejoin_instance',
                                                                      'missing'),
                 ('ic-8.4-clusterset-primary', 'set_primary_cluster:drcluster', None),
                 ('ic-8.4-clusterset-replica', 'force_primary_cluster', None))
        for name, action, who in cases:
            report = F.report(name)
            key = missing(report)['key'] if who else local(report)['key']
            plan = m.plan_action(report, action, key, CHECKS)
            reviewed_writes.check_reviewed(plan)
            self.assertEqual(dbops.decide(reviewed_writes.worst_kind(plan['statements']), 'development')['action'],
                             'confirm', name)


class TestEffects(unittest.TestCase):
    def test_clusterset_channel_is_copied_from_the_local_read(self):
        report = F.report('ic-8.4-clusterset-replica')
        after = m._after(report, {})
        self.assertEqual(after['cs_channel'], {'source': 'mf-main-1:3306', 'receiver_state': 'ON'})
        report['clusterset']['channel']['receiver_state'] = 'OFF'
        self.assertEqual(after['cs_channel']['receiver_state'], 'ON', 'the before read stays unchanged')

    def test_channel_row_appears_only_when_either_read_has_a_channel(self):
        report = F.report('ic-8.4-clusterset-primary')
        before = m._after(report, {})
        report['clusterset']['channel'] = {'source': 'mf-a:3306', 'receiver_state': 'ON'}
        after = m._after(report, {})
        for action in ('set_primary_cluster:drcluster', 'force_primary_cluster', 'rejoin_cluster:drcluster',
                       'switchover'):
            with self.subTest(action=action):
                plan = {'action': action}
                expected = ([['Primary', 'mf-ic-1:3306', 'mf-ic-1:3306']] if action == 'switchover' else
                            [['Primary cluster', 'mfcluster', 'mfcluster'], ['Invalidated', 'none', 'none']])
                self.assertEqual(m._effect(plan, before, before), expected)
                self.assertEqual(m._effect(plan, before, after),
                                 expected + [['ClusterSet channel', 'Not readable', 'mf-a:3306 ON']])
                self.assertEqual(m._effect(plan, after, before),
                                 expected + [['ClusterSet channel', 'mf-a:3306 ON', 'Not readable']])

    def test_channel_does_not_change_other_actions_effects(self):
        report = F.report('ic-8.4-clusterset-replica')
        with_channel = m._after(report, {})
        report['clusterset']['channel'] = None
        without_channel = m._after(report, {})
        for action in ('rejoin_instance', 'switch_to_multi_primary', 'stop_member'):
            with self.subTest(action=action):
                self.assertEqual(m._effect({'action': action}, with_channel, with_channel),
                                 m._effect({'action': action}, without_channel, without_channel))


class TestErrorText(unittest.TestCase):
    def test_live_force_primary_exception_takes_priority_over_noise(self):
        stderr = ("Cannot set LC_ALL to locale en_US.UTF-8: No such file or directory\n"
                  "Failing-over primary cluster of the clusterset to 'csb'\n"
                  "* Verifying primary cluster status\n"
                  "\x1b[31mERROR: \x1b[0mThe PRIMARY cluster 'csa' is still available and has quorum.\n"
                  "ClusterSet.forcePrimaryCluster: PRIMARY cluster 'csa' is still available (MYSQLSH 51615)\n"
                  " at STDIN:1\n")
        expected = ("ERROR 51615 at line 1: ClusterSet.forcePrimaryCluster: PRIMARY cluster 'csa' is still "
                    "available (MYSQLSH 51615)")
        for locale_warning in (True, False):
            with self.subTest(locale_warning=locale_warning):
                output = stderr if locale_warning else stderr.split('\n', 1)[1]
                self.assertEqual(cluster_shell.error_text(output), expected)

    def test_coloured_error_prefix_without_a_number(self):
        self.assertEqual(cluster_shell.error_text("\x1b[31mERROR: \x1b[0mSomething failed\n at STDIN:1\n"),
                         'ERROR 0 at line 1: Something failed')

    def test_server_error_takes_priority_over_other_error_lines(self):
        self.assertEqual(cluster_shell.error_text(
            'ERROR: Operation failed\nCluster.operation: MySQL Error 3092: Not configured\n at STDIN:2\n'),
            'ERROR 3092 at line 2: Cluster.operation: MySQL Error 3092: Not configured')

    def test_progress_without_exception_evidence_is_not_an_error(self):
        self.assertIsNone(cluster_shell.error_text('INFO: Connecting\n', 'Synchronizing transactions...\n'))
        self.assertIsNone(cluster_shell.error_text('', 'MYSQLSH is working\n'))

    def test_stdin_location_alone_is_exception_evidence(self):
        self.assertEqual(cluster_shell.error_text('Operation failed at STDIN:2'),
                         'ERROR 0 at line 2: Operation failed')

    def test_shell_code_alone_is_exception_evidence(self):
        self.assertEqual(cluster_shell.error_text('Operation failed (MYSQLSH 51615)'),
                         'ERROR 51615 at line 1: Operation failed (MYSQLSH 51615)')

    def test_statement_exception_with_a_shell_number(self):
        text = cluster_shell.error_text(
            "\x1b[31mCluster.rejoinInstance: The instance 'mf-ic-3:3306' has errant transactions (MYSQLSH 51153)"
            "\x1b[0m\n at STDIN:1\n")
        self.assertEqual(text, "ERROR 51153 at line 1: Cluster.rejoinInstance: The instance 'mf-ic-3:3306' has "
                               "errant transactions (MYSQLSH 51153)")

    def test_statement_exception_without_a_number(self):
        text = cluster_shell.error_text("WARNING: something\nClusterSet.setPrimaryCluster: Cluster 'x' is not "
                                        "available (RuntimeError)\n")
        self.assertTrue(text.startswith('ERROR 0 at line 1: ClusterSet.setPrimaryCluster'), text)

    def test_server_error_inside_a_statement(self):
        text = cluster_shell.error_text("Cluster.switchToMultiPrimaryMode: MySQL Error 3092 (HY000): The server is "
                                        "not configured properly (RuntimeError) at STDIN:1")
        self.assertTrue(text.startswith('ERROR 3092 at line 1:'), text)

    def test_no_session_means_nothing_ran(self):
        text = cluster_shell.error_text("MySQL Error 1045 (28000): Access denied for user 'x'@'10.0.0.1' (using "
                                        "password: YES)\n")
        self.assertTrue(text.startswith('ERROR 1045: '), text)
        from myflames import reviewed_writes
        self.assertIsNone(reviewed_writes.failed_line(text))

    def test_empty_output(self):
        self.assertIsNone(cluster_shell.error_text('', ''))


class FakeShell(object):
    """Records what ShellWriter.run_write would send to MySQL Shell."""

    def __init__(self, fail=None):
        self.fail = fail
        self.scripts = []

    def __call__(self, writer, script, extra_flags=None, timeout=None):
        self.scripts.append((script, timeout))
        if self.fail is not None:
            raise self.fail
        return ''


class Writes(object):
    """A fixture connection that must not see a write through the mysql client."""

    def __init__(self, conn):
        self.conn = conn
        self.host, self.port, self.user, self.password = '127.0.0.1', 34101, 'mf_admin', 'pw-secret'

    def run(self, sql, extra_flags=None, timeout=None):
        return self.conn.run(sql, extra_flags)

    def run_write(self, sql, extra_flags=None, timeout=None):
        raise AssertionError('sent through the mysql client: ' + sql)

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


class Endpoints(Home):
    def setUp(self):
        super().setUp()
        from myflames.workspace_store import WorkspaceStore
        self.workspace = type('Workspace', (), {'store': WorkspaceStore(os.path.join(self.tmp.name, 'ws.sqlite3'))})()
        self.connection = {'host': '127.0.0.1', 'port': 34101, 'user': 'mf_admin', 'password': 'pw-secret'}
        patcher = mock.patch.object(cluster_shell, 'find_shell', return_value=SHELL)
        patcher.start()
        self.addCleanup(patcher.stop)

    def api(self, path, body, conn, shell=None):
        factory = lambda **options: conn  # noqa: E731
        with mock.patch('myflames.connector.MySQLConnection', factory), \
                mock.patch.object(cluster_shell.ShellWriter, 'run_write', autospec=True,
                                  side_effect=shell or FakeShell()):
            return ui.api_response(path, body, self.workspace)

    def plan(self, conn, action, member_key):
        return self.api('/api/live/cluster/plan', {'connection': self.connection, 'action': action,
                                                    'member_key': member_key}, conn)

    def send(self, conn, plan, typed=None, shell=None):
        plan = dict(plan)
        token = plan.pop('review_token', None)
        return self.api('/api/live/cluster/action', {'connection': self.connection, 'plan': plan,
                                                      'review_token': token, 'typed': typed, 'confirmed': True},
                        conn, shell)

    def test_read_endpoint_offers_the_shell_actions(self):
        report = self.api('/api/live/cluster', {'connection': self.connection}, F.conn('ic-8.4-member-offline'))
        self.assertEqual([a['id'] for a in missing(report)['actions']], ['rejoin_instance'])

    def test_rejoin_is_sent_through_mysql_shell(self):
        report = F.report('ic-8.4-member-offline')
        target = missing(report)
        conn = Writes(F.conn('ic-8.4-member-offline'))
        plan = self.plan(conn, 'rejoin_instance', target['key'])
        shell = FakeShell()
        result = self.send(conn, plan, typed=target['name'], shell=shell)
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(shell.scripts, [(TAG + 'dba.getCluster().rejoinInstance("%s", {recoveryMethod: "incremental"});'
                                          % target['address'],
                                          m.SHELL_TIMEOUT)])
        (record,) = dbops.audit_report(self.workspace.store)['records']
        self.assertEqual((record['operation'], record['status']), ('cluster_action', 'ok'))
        self.assertNotIn('pw-secret', json.dumps(record))

    def test_changing_incremental_recovery_to_clone_sends_nothing(self):
        report = F.report('ic-8.4-member-offline')
        conn = Writes(F.conn('ic-8.4-member-offline'))
        target = missing(report)
        plan = self.plan(conn, 'rejoin_instance', target['key'])
        plan['statements'] = [s.replace('"incremental"', '"clone"') for s in plan['statements']]
        shell = FakeShell()
        self.assertEqual(self.send(conn, plan, typed=target['name'], shell=shell)['status'], 'refused')
        self.assertEqual(shell.scripts, [])

    def test_shell_downgraded_after_review_sends_nothing(self):
        report = F.report('ic-8.4-member-offline')
        conn = Writes(F.conn('ic-8.4-member-offline'))
        target = missing(report)
        plan = self.plan(conn, 'rejoin_instance', target['key'])
        shell = FakeShell()
        with mock.patch.object(cluster_shell, 'find_shell', return_value=dict(SHELL, version_tuple=[8, 0, 34])):
            self.assertEqual(self.send(conn, plan, typed=target['name'], shell=shell)['status'], 'refused')
        self.assertEqual(shell.scripts, [])

    def test_without_the_typed_name_nothing_is_sent(self):
        report = F.report('ic-8.4-member-offline')
        conn = Writes(F.conn('ic-8.4-member-offline'))
        plan = self.plan(conn, 'rejoin_instance', missing(report)['key'])
        shell = FakeShell()
        self.assertEqual(self.send(conn, plan, typed='nope', shell=shell)['status'], 'refused')
        self.assertEqual(shell.scripts, [])

    def test_mysql_shell_gone_at_send_time_sends_nothing(self):
        report = F.report('ic-8.4-member-offline')
        conn = Writes(F.conn('ic-8.4-member-offline'))
        plan = self.plan(conn, 'rejoin_instance', missing(report)['key'])
        shell = FakeShell()
        with mock.patch.object(cluster_shell, 'find_shell', return_value=None):
            result = self.send(conn, plan, typed=missing(report)['name'], shell=shell)
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(shell.scripts, [])

    def test_shell_refusal_with_nothing_changed_is_failed(self):
        report = F.report('ic-8.4-member-offline')
        target = missing(report)
        conn = Writes(F.conn('ic-8.4-member-offline'))
        plan = self.plan(conn, 'rejoin_instance', target['key'])
        shell = FakeShell(ConnectorError("mysqlsh exited 1: ERROR 51153 at line 1: Cluster.rejoinInstance: The "
                                         "instance has errant transactions (MYSQLSH 51153)"))
        result = self.send(conn, plan, typed=target['name'], shell=shell)
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(result['message'],
                         'MySQL Shell refused %s: %s. No change is visible here; check dba.getCluster().status() '
                         'in MySQL Shell before you try again.' % (plan['statements'][0], result['error']))
        (record,) = dbops.audit_report(self.workspace.store)['records']
        self.assertEqual(record['message'], result['message'])

    def test_shell_refusals_name_the_matching_status_call(self):
        cases = (('ic-8.4-clusterset-primary', 'set_primary_cluster:drcluster', 'local', 'ClusterSet'),
                 ('ic-8.4-clusterset-replica', 'force_primary_cluster', 'local', 'ClusterSet'),
                 ('ic-8.4-clusterset-invalidated', 'rejoin_cluster:drcluster', 'local', 'ClusterSet'),
                 ('ic-8.4-clusterset-replica', 'switchover', 'secondary', 'ClusterSet'),
                 ('ic-8.4-healthy', 'switch_to_multi_primary', 'local', 'Cluster'))
        for scenario, action, who, status in cases:
            with self.subTest(action=action):
                report = F.report(scenario)
                target = (local(report) if who == 'local' else
                          next(x for x in report['members'] if x['role'] == 'SECONDARY'))
                conn = Writes(F.conn(scenario))
                plan = self.plan(conn, action, target['key'])
                shell = FakeShell(ConnectorError('mysqlsh exited 1: ERROR 51615 at line 1: operation refused'))
                result = self.send(conn, plan, typed=plan['typed_guard'], shell=shell)
                self.assertEqual((result['status'], result['effect']), ('failed', None), result)
                self.assertEqual(result['message'],
                                 'MySQL Shell refused %s: operation refused. No change is visible here; check '
                                 'dba.get%s().status() in MySQL Shell before you try again.'
                                 % (plan['statements'][0], status))
                record = next(r for r in dbops.audit_report(self.workspace.store)['records']
                              if r['id'] == result['audit_id'])
                self.assertEqual(record['message'], result['message'])

    def test_shell_error_after_a_channel_only_change_is_partial(self):
        for action in ('set_primary_cluster:mfcluster', 'force_primary_cluster', 'rejoin_cluster:mfcluster',
                       'switchover'):
            for change in ('receiver_state', 'source', 'removed'):
                with self.subTest(action=action, change=change):
                    report = F.report('ic-8.4-clusterset-local-invalidated' if action.startswith('rejoin_cluster')
                                      else 'ic-8.4-clusterset-replica')
                    target = (next(x for x in report['members'] if x['role'] == 'SECONDARY')
                              if action == 'switchover' else local(report))
                    plan = m.plan_action(report, action, target['key'], CHECKS)
                    after = json.loads(json.dumps(report))
                    if change == 'removed':
                        after['clusterset']['channel'] = None
                    else:
                        after['clusterset']['channel'][change] = ('OFF' if change == 'receiver_state' else
                                                                 'mf-a:3306')
                    before_channel = report['clusterset']['channel']
                    conn = Writes(F.conn('ic-8.4-clusterset-replica'))
                    shell = FakeShell(ConnectorError('mysqlsh exited 1: ERROR 51615 at line 1: operation refused'))
                    audit = mock.Mock(password=None)
                    with mock.patch.object(m, 'read_status', side_effect=[report, after]), \
                            mock.patch.object(cluster_shell.ShellWriter, 'run_write', autospec=True,
                                              side_effect=shell):
                        result = m.execute_action(conn, plan, audit=audit, typed=plan['typed_guard'])
                    self.assertEqual(result['status'], 'partial', result)
                    now = ('Not readable' if change == 'removed' else
                           '%s %s' % (after['clusterset']['channel']['source'],
                                      after['clusterset']['channel']['receiver_state']))
                    self.assertEqual(result['effect'], [['ClusterSet channel', '%s %s' % (
                        before_channel['source'], before_channel['receiver_state']), now]])
                    self.assertEqual(audit.complete.call_args[1]['message'], result['message'])

    def test_sql_refusal_keeps_the_default_message(self):
        report = F.report('ic-8.4-healthy')
        target = next(x for x in report['members'] if x['role'] == 'SECONDARY' and x['state'] == 'ONLINE')
        conn = F.conn('ic-8.4-healthy')
        plan = self.plan(conn, 'switchover', target['key'])
        with mock.patch.object(conn, 'run_write', create=True,
                               side_effect=ConnectorError('ERROR 3910 at line 1: replica channel running')):
            result = self.send(conn, plan)
        self.assertEqual((result['status'], result['message']),
                         ('failed', 'The server refused %s: replica channel running. Nothing changed.'
                          % plan['statements'][0]))
        (record,) = dbops.audit_report(self.workspace.store)['records']
        self.assertEqual(record['message'], result['message'])

    def test_shell_error_after_a_visible_change_is_partial(self):
        report = F.report('ic-8.4-member-offline')
        target = missing(report)
        conn = Writes(F.conn('ic-8.4-member-offline'))
        plan = self.plan(conn, 'rejoin_instance', target['key'])
        shell = FakeShell(ConnectorError("mysqlsh exited 1: ERROR 0 at line 1: Cluster.rejoinInstance: Timeout "
                                         "waiting for recovery (RuntimeError)"))
        rejoined = F.report('ic-8.4-healthy')
        with mock.patch.object(m, 'read_status', side_effect=[report, rejoined]):
            result = self.send(conn, plan, typed=target['name'], shell=shell)
        self.assertEqual(result['status'], 'partial', result)
        self.assertTrue(result['effect'])

    def test_no_session_is_failed_nothing_sent(self):
        report = F.report('ic-8.4-healthy')
        conn = Writes(F.conn('ic-8.4-healthy'))
        plan = self.plan(conn, 'switch_to_multi_primary', local(report)['key'])
        shell = FakeShell(ConnectorError("mysqlsh exited 1: ERROR 1045: Access denied for user 'mf_admin'"))
        result = self.send(conn, plan, typed='mfcluster', shell=shell)
        self.assertEqual(result['status'], 'failed', result)
        self.assertIn('could not connect', result['message'])

    def test_time_limit_is_unknown(self):
        report = F.report('ic-8.4-clusterset-primary')
        conn = Writes(F.conn('ic-8.4-clusterset-primary'))
        plan = self.plan(conn, 'set_primary_cluster:drcluster', local(report)['key'])
        shell = FakeShell(ConnectorError('MySQL Shell timed out after 600s; the outcome is unknown.'))
        result = self.send(conn, plan, typed='drcluster', shell=shell)
        self.assertEqual(result['status'], 'unknown', result)
        self.assertIn('dba.getClusterSet().status()', ' '.join(result['checks']))

    def test_a_changed_clusterset_sends_nothing(self):
        report = F.report('ic-8.4-clusterset-primary')
        conn = Writes(F.conn('ic-8.4-clusterset-primary'))
        plan = self.plan(conn, 'set_primary_cluster:drcluster', local(report)['key'])
        shell = FakeShell()
        changed = F.report('ic-8.4-clusterset-invalidated')
        with mock.patch.object(m, 'read_status', return_value=changed):
            result = self.send(conn, plan, typed='drcluster', shell=shell)
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(shell.scripts, [])


class TestShellWriter(unittest.TestCase):
    """The command line, option file and environment MySQL Shell gets, with a
    stand-in mysqlsh that reports them."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.chmod(self.tmp, 0o700)
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmp, ignore_errors=True))
        self.out = os.path.join(self.tmp, 'seen.json')
        self.program = os.path.join(self.tmp, 'mysqlsh')
        with open(self.program, 'w') as handle:
            handle.write(textwrap.dedent('''\
                #!%s
                import json, os, signal, sys
                defaults = [a for a in sys.argv if a.startswith('--defaults-file=')][0].split('=', 1)[1]
                script = sys.stdin.read()
                seen = {'argv': sys.argv[1:], 'script': script, 'option_file': open(defaults).read(),
                        'mode': oct(os.stat(defaults).st_mode & 0o777),
                        'env': sorted(os.environ), 'home': os.environ.get('MYSQLSH_USER_CONFIG_HOME')}
                json.dump(seen, open(%r, 'w'))
                if 'STOP' in script:
                    print('Synchronizing transactions...', flush=True)
                    if 'EVIDENCE' in script:
                        print('Operation refused (MYSQLSH 51615) at STDIN:1', file=sys.stderr, flush=True)
                    os.kill(os.getpid(), signal.SIGTERM)
                if 'PROGRESS' in script:
                    print('Synchronizing transactions...', flush=True)
                    sys.exit(1)
                if 'FAIL' in script:
                    sys.stderr.write("Cluster.rejoinInstance: errant transactions (MYSQLSH 51153)\\n at STDIN:1\\n")
                    sys.exit(1)
                if 'NOCONNECT' in script:
                    sys.stderr.write("MySQL Error 2003 (HY000): Can't connect to MySQL server on 'x:3306' (111)\\n")
                    sys.exit(1)
                print('ok')
            ''' % (sys.executable, self.out)))
        os.chmod(self.program, stat.S_IRWXU)

    def writer(self, password='s3cr"et', **options):
        attrs = {'host': 'mf-ic-1', 'port': 3306, 'user': 'mf_admin', 'password': password,
                 'ssl_mode': None, 'ssh_host': None}
        attrs.update(options)
        conn = type('Conn', (), attrs)()
        return cluster_shell.ShellWriter(conn, {'path': self.program, 'realpath': self.program})

    def seen(self):
        with open(self.out) as handle:
            return json.load(handle)

    def test_account_only_in_a_private_option_file(self):
        if os.name != 'posix':
            self.skipTest('POSIX permissions')
        self.writer().run_write(TAG + 'dba.getCluster().switchToMultiPrimaryMode();', timeout=30)
        seen = self.seen()
        self.assertEqual(seen['argv'][1:], ['--js', '--no-wizard', '--quiet-start=2'])
        self.assertTrue(seen['argv'][0].startswith('--defaults-file='))
        self.assertNotIn('s3cr', ' '.join(seen['argv']))
        self.assertIn('password="s3cr\\"et"', seen['option_file'])
        self.assertIn('user="mf_admin"', seen['option_file'])
        self.assertIn('host="mf-ic-1"', seen['option_file'])
        self.assertIn('port="3306"', seen['option_file'])
        self.assertNotIn('socket=', seen['option_file'])
        self.assertNotIn('protocol', seen['option_file'], 'MySQL Shell has no protocol option')
        self.assertEqual(seen['mode'], '0o600')
        self.assertEqual(seen['script'], TAG + 'dba.getCluster().switchToMultiPrimaryMode();')
        self.assertTrue(seen['home'])
        self.assertNotIn('MYSQL_PWD', seen['env'])

    def test_socket_option_file_has_no_tcp_port(self):
        if os.name != 'posix':
            self.skipTest('POSIX program')
        socket = os.path.join(self.tmp, 'mysql "local".sock')
        script = TAG + 'dba.getCluster().switchToMultiPrimaryMode();'
        self.writer(socket=socket, port=3307).run_write(script, timeout=30)
        seen = self.seen()
        self.assertIn('socket="%s"' % socket.replace('"', '\\"'), seen['option_file'])
        self.assertIn('host="localhost"', seen['option_file'])
        self.assertNotIn('port=', seen['option_file'])
        self.assertNotIn('protocol=', seen['option_file'])
        self.assertEqual(seen['mode'], '0o600')
        self.assertEqual(seen['script'], script)
        self.assertFalse(os.path.exists(seen['argv'][0].split('=', 1)[1]), 'credentials removed after sending')

    def test_invalid_socket_sends_nothing(self):
        for socket in ('/tmp/mysql.sock\nport=3307', '/tmp/mysql.sock\x00', 123):
            with self.subTest(socket=socket), self.assertRaises(ConnectorError):
                self.writer(socket=socket).run_write('NEVER SEND', timeout=30)
            self.assertFalse(os.path.exists(self.out))

    def test_errors_in_the_reviewed_writes_shape(self):
        if os.name != 'posix':
            self.skipTest('POSIX program')
        from myflames import reviewed_writes
        with self.assertRaises(ConnectorError) as caught:
            self.writer().run_write('FAIL', timeout=30)
        self.assertEqual((reviewed_writes.error_number(str(caught.exception)),
                          reviewed_writes.failed_line(str(caught.exception))), (51153, 1))
        with self.assertRaises(ConnectorError) as caught:
            self.writer().run_write('NOCONNECT', timeout=30)
        self.assertEqual((reviewed_writes.error_number(str(caught.exception)),
                          reviewed_writes.failed_line(str(caught.exception))), (2003, None))

    def test_no_mysql_shell_sends_nothing(self):
        with self.assertRaises(ConnectorError):
            cluster_shell.ShellWriter(object(), None)

    def test_progress_only_exit_is_unknown(self):
        self._assert_unknown_exit('PROGRESS',
                                  'MySQL Shell exited 1 without an error message; the outcome is unknown.')

    def test_signal_exit_is_unknown_even_with_exception_output(self):
        import signal
        for script in ('STOP', 'STOP EVIDENCE'):
            with self.subTest(script=script):
                self._assert_unknown_exit(script,
                                          'MySQL Shell was stopped (signal %d); the outcome is unknown.'
                                          % signal.SIGTERM)

    def _assert_unknown_exit(self, script, expected):
        if os.name != 'posix':
            self.skipTest('POSIX program')
        from myflames import reviewed_writes
        with self.assertRaises(ConnectorError) as caught:
            self.writer().run_write(script, timeout=30)
        self.assertEqual(str(caught.exception), expected)
        self.assertIsNone(reviewed_writes.error_number(str(caught.exception)))
        plan = {'action': 'x', 'statements': [script], 'expect': {}}
        audit = mock.Mock()
        result = reviewed_writes.execute(self.writer(), plan, lambda: plan, 'x-1.0', audit=audit, timeout=30)
        self.assertEqual((result['status'], result['states']), ('unknown', ['unknown']))
        self.assertEqual(audit.complete.call_args[0], ('unknown',))


if __name__ == '__main__':
    unittest.main()
