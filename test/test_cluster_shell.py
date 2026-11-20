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
import re
import shutil
import stat
import subprocess
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

# Public Amazon Root CA 3, used only as deterministic test data.
SHELL_ROOT_PEM = """-----BEGIN CERTIFICATE-----
MIIBtjCCAVugAwIBAgITBmyf1XSXNmY/Owua2eiedgPySjAKBggqhkjOPQQDAjA5
MQswCQYDVQQGEwJVUzEPMA0GA1UEChMGQW1hem9uMRkwFwYDVQQDExBBbWF6b24g
Um9vdCBDQSAzMB4XDTE1MDUyNjAwMDAwMFoXDTQwMDUyNjAwMDAwMFowOTELMAkG
A1UEBhMCVVMxDzANBgNVBAoTBkFtYXpvbjEZMBcGA1UEAxMQQW1hem9uIFJvb3Qg
Q0EgMzBZMBMGByqGSM49AgEGCCqGSM49AwEHA0IABCmXp8ZBf8ANm+gBG1bG8lKl
ui2yEujSLtf6ycXYqm0fc4E7O5hrOXwzpcVOho6AF2hiRVd9RFgdszflZwjrZt6j
QjBAMA8GA1UdEwEB/wQFMAMBAf8wDgYDVR0PAQH/BAQDAgGGMB0GA1UdDgQWBBSr
ttvXBp43rDCGB5Fwx5zEGbF4wDAKBggqhkjOPQQDAgNJADBGAiEA4IWSoxe3jfkr
BqWTrBqYaGFy+uGh0PsceGCmQ5nFuMQCIQCcAu/xlJyzlvnrxir4tiz+OpAUFteM
YyRIHN8wfdVoOw==
-----END CERTIFICATE-----
"""


def member(report, name):
    return next(x for x in report['members'] if x['name'] == name)


def local(report):
    return next(x for x in report['members'] if x['local'])


def offered(report, key, checks=CHECKS):
    actions = m.available_actions(report, key, checks)
    if key == local(report)['key']:
        actions += m.overview_actions(report, checks)
    return [a['id'] for a in actions if a['enabled']]


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

    def test_clusterset_group_identity_is_from_metadata_and_not_public_json(self):
        conn = F.conn('ic-8.4-clusterset-primary')
        report = m.read_status(conn)
        rows = conn.scenario['tables']['mysql_innodb_cluster_metadata.v2_gr_clusters']
        self.assertEqual(report._clusterset_groups, {row[1]: row[3] for row in rows})
        self.assertEqual(set(report._clusterset_groups), {'mfcluster', 'drcluster'})
        self.assertNotIn('_clusterset_groups', json.dumps(report))
        plan = m.plan_action(report, 'set_primary_cluster:drcluster', local(report)['key'], CHECKS)
        self.assertEqual(plan['expect']['server_identity']['adminapi'],
                         {'kind': 'clusterset', 'name': 'mfcs', 'groups': report._clusterset_groups})

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

    def test_read_replica_reads_the_clusterset_from_its_local_copy(self):
        """Connected to a read replica of the ClusterSet's primary cluster:
        the band comes from its replicated copy of the metadata."""
        report = F.report('ic-8.4-read-replica-clusterset')
        cs = report['clusterset']
        self.assertEqual((cs['name'], cs['local_cluster'], cs['local_role'], cs['primary_cluster']),
                         ('mfcs', 'mfcluster', 'PRIMARY', 'mfcluster'))
        self.assertTrue(cs['from_local_copy'])
        self.assertEqual([(c['name'], c['role'], c['local']) for c in cs['clusters']],
                         [('mfcluster', 'PRIMARY', True), ('drcluster', 'REPLICA', False)])
        self.assertEqual(cs['clusters'][0]['instances'], [])
        self.assertEqual([i['address'] for i in cs['clusters'][1]['instances']],
                         ['mf-dr-1:3306', 'mf-dr-2:3306', 'mf-dr-3:3306'])
        self.assertEqual(local(report)['instance_type'], 'read-replica')
        self.assertEqual(report['metadata']['local_copy'], 'read_replica')
        notes = [n for n in report['notes'] if 'ClusterSet' in n]
        self.assertEqual(len(notes), 1, notes)
        self.assertIn('read replica', notes[0])
        self.assertIn('replicated copy', notes[0])
        self.assertEqual(json.loads(json.dumps(report))['clusterset'], cs)

    def test_read_replica_clusterset_metadata_2_2_on_8_0_and_8_4(self):
        for version in ('8.0.44', '8.4.11'):
            with self.subTest(version=version):
                scenario = F.SCENARIOS['ic-8.4-read-replica-clusterset']
                conn = F.ScenarioConn(dict(scenario, variables=dict(scenario['variables'], version=version)))
                report = m.read_status(conn)
                self.assertEqual(report['server_version'], version)
                cs = report['clusterset']
                self.assertTrue(cs['from_local_copy'])
                self.assertEqual((cs['local_cluster'], cs['primary_cluster']), ('mfcluster', 'mfcluster'))

    def test_read_replica_outside_a_clusterset_is_unchanged(self):
        conn = F.conn('ic-8.4-read-replica-local')
        report = m.read_status(conn)
        self.assertNotIn('clusterset', report)
        self.assertFalse(any('ClusterSet' in n for n in report['notes']))
        self.assertEqual(local(report)['state'], 'Replicating')


class TestOffered(unittest.TestCase):
    def test_discovery_keeps_an_old_shell_for_the_versioned_refusal(self):
        old = dict(SHELL, version='8.0.31', version_tuple=[8, 0, 31])
        with mock.patch('myflames.client_tools.discover', return_value={'shells': [old]}):
            found = cluster_shell.find_shell()
        self.assertEqual(found, old)
        report = F.report('ic-8.4-healthy')
        action = next(a for a in m.overview_actions(report, {'shell': found})
                      if a['id'] == 'switch_to_multi_primary')
        self.assertFalse(action['enabled'])
        self.assertIn('found 8.0.31', action['why_not'])
        self.assertIn('Get MySQL Shell', action['fix'])

    def test_shell_action_version_thresholds(self):
        cases = (
            ('ic-8.4-healthy', 'switch_to_multi_primary', 'local', (8, 0, 32), (8, 0, 31)),
            ('ic-8.4-healthy', 'switch_to_single_primary', 'multi', (8, 0, 32), (8, 0, 31)),
            ('ic-8.4-member-offline', 'rejoin_instance', 'missing', (8, 1, 0), (8, 0, 34)),
            ('ic-8.4-clusterset-primary', 'set_primary_cluster:drcluster', 'local', (8, 0, 32), (8, 0, 31)),
            ('ic-8.4-clusterset-replica', 'force_primary_cluster', 'local', (8, 0, 32), (8, 0, 31)),
            ('ic-8.4-clusterset-invalidated', 'rejoin_cluster:drcluster', 'local', (8, 0, 32), (8, 0, 31)),
            ('ic-8.4-clusterset-replica', 'switchover', 'secondary', (8, 0, 32), (8, 0, 31)),
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

    def test_ssh_offers_and_plans_no_shell_action_on_any_member(self):
        checks = dict(CHECKS, ssh=True)
        for scenario in ('ic-8.4-healthy', 'ic-8.4-member-offline', 'ic-8.4-clusterset-primary',
                         'ic-8.4-clusterset-replica', 'ic-8.4-clusterset-invalidated'):
            for topology in ('single_primary', 'multi_primary'):
                report = F.report(scenario)
                report['cluster']['topology'] = topology
                direct_shell_actions = set()
                for target in report['members']:
                    direct_shell_actions.update(a for a in offered(report, target['key']) if m._via_shell(report, a))
                self.assertTrue(direct_shell_actions, (scenario, topology))
                for target in report['members']:
                    with self.subTest(scenario=scenario, topology=topology, member=target['key']):
                        self.assertFalse(any(m._via_shell(report, a)
                                             for a in offered(report, target['key'], checks)))
                        for action in direct_shell_actions:
                            with self.assertRaises(ValueError):
                                m.plan_action(report, action, target['key'], checks)

    def test_ssh_keeps_sql_cluster_actions(self):
        for scenario in ('ic-8.4-healthy', 'ic-8.4-clusterset-primary', 'gr-8.4-two-members'):
            report = F.report(scenario)
            secondary = next(x for x in report['members'] if x['role'] == 'SECONDARY')
            for action, target in (('stop_member', local(report)), ('switchover', secondary)):
                with self.subTest(scenario=scenario, action=action):
                    checks = dict(CHECKS, ssh=True)
                    self.assertIn(action, offered(report, target['key'], checks))
                    self.assertNotIn('via', m.plan_action(report, action, target['key'], checks))

    def test_ssh_report_note_appears_once_only_with_shell_on_innodb_cluster(self):
        note = ("MySQL Shell changes are not offered over an SSH connection: MySQL Shell connects to the other "
                "members directly, outside the tunnel, with this connection's user and password. Run them in "
                "MySQL Shell from a host that reaches the members.")
        report = F.report('ic-8.4-healthy')
        for _ in range(2):
            m.with_actions(report, shell=SHELL, ssh=True)
            self.assertEqual(report['notes'].count(note), 1)
            self.assertFalse(any(m._via_shell(report, a['id']) for x in report['members'] for a in x['actions'] if a['enabled']))
        for shell, ssh, scenario in ((SHELL, False, 'ic-8.4-healthy'),
                                     (None, True, 'ic-8.4-healthy'),
                                     (SHELL, True, 'gr-8.4-two-members')):
            with self.subTest(shell=shell, ssh=ssh, scenario=scenario):
                with mock.patch.object(cluster_shell, 'find_shell', return_value=None):
                    report = m.with_actions(F.report(scenario), shell=shell, ssh=ssh)
                self.assertNotIn(note, report['notes'])

    def test_invalid_shell_addresses_are_not_offered_or_planned(self):
        for address in ('mf-a:3306\n', 'mf-a:٣٣٠٦', 'mf-a:３３０６'):
            for scenario, action in (('ic-8.4-member-offline', 'rejoin_instance'),
                                     ('ic-8.4-clusterset-replica', 'switchover')):
                with self.subTest(address=address, action=action):
                    report = F.report(scenario)
                    target = (missing(report) if action == 'rejoin_instance' else
                              next(x for x in report['members'] if x['role'] == 'SECONDARY'))
                    self.assertIn(action, offered(report, target['key']))
                    target['address'] = address
                    self.assertNotIn(action, offered(report, target['key']))
                    with self.assertRaises(ValueError):
                        m.plan_action(report, action, target['key'], CHECKS)

    def test_newline_clusterset_names_are_not_offered_or_planned(self):
        for scenario, action in (('ic-8.4-clusterset-primary', 'set_primary_cluster:drcluster'),
                                 ('ic-8.4-clusterset-invalidated', 'rejoin_cluster:drcluster'),
                                 ('ic-8.4-clusterset-replica', 'force_primary_cluster')):
            with self.subTest(action=action):
                report = F.report(scenario)
                key = local(report)['key']
                self.assertIn(action, offered(report, key))
                if action == 'force_primary_cluster':
                    report['clusterset']['local_cluster'] += '\n'
                else:
                    next(c for c in report['clusterset']['clusters'] if c['name'] == 'drcluster')['name'] += '\n'
                    action += '\n'
                self.assertNotIn(action, offered(report, key))
                with self.assertRaises(ValueError):
                    m.plan_action(report, action, key, CHECKS)

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

    def test_rejoin_needs_metadata_2_and_exact_clusterset_ok(self):
        for version in (None, '0.0.0', '1.0.1', '2.0.0', '2.2.0'):
            with self.subTest(metadata=version):
                report = F.report('ic-8.4-member-offline')
                report['metadata']['schema_version'] = version
                action = next(a for a in m.available_actions(report, missing(report)['key'], CHECKS)
                              if a['id'] == 'rejoin_instance')
                self.assertEqual(action['enabled'], version in ('2.0.0', '2.2.0'))
                if not action['enabled']:
                    self.assertIn('metadata 2.0.0', action['why_not'])
                    with self.assertRaisesRegex(m.ActionUnavailable, 'metadata 2.0.0'):
                        m.plan_action(report, 'rejoin_instance', missing(report)['key'], CHECKS)
        for status in ('OK', 'OK_NOT_REPLICATING', 'OK_NOT_CONSISTENT', 'OK_MISCONFIGURED', 'NOT_OK',
                       'UNKNOWN', 'INVALIDATED', None):
            with self.subTest(status=status):
                report = F.report('ic-8.4-member-offline')
                report['clusterset'] = {'global_status': status, 'invalidated': False}
                action = next(a for a in m.available_actions(report, missing(report)['key'], CHECKS)
                              if a['id'] == 'rejoin_instance')
                self.assertEqual(action['enabled'], status == 'OK')
                if status != 'OK':
                    self.assertIn(status or 'could not be verified', action['why_not'])
                    with self.assertRaisesRegex(m.ActionUnavailable, 'ClusterSet status'):
                        m.plan_action(report, 'rejoin_instance', missing(report)['key'], CHECKS)

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
        self.assertEqual(plan['statements'], [TAG + 'myflamesCluster.switchToMultiPrimaryMode();'])
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
                         [TAG + 'myflamesCluster.rejoinInstance("%s", {recoveryMethod: "incremental"});'
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
        self.assertNotIn('clone', next(a for a in m.available_actions(report, target['key'], CHECKS)
                                        if a['id'] == 'rejoin_instance')['description'])

    def test_action_checks_records_ssh_only_for_shell_actions(self):
        report = F.report('ic-8.4-healthy')
        grants = {'checked': True, 'account': 'mf_admin@localhost', 'grants': F.ADMIN_GRANTS}
        expected_grants = m._grant_model(report, grants)
        with mock.patch.object(cluster_shell, 'find_shell', return_value=SHELL):
            for host in (None, '', 'bastion.example'):
                conn = type('Conn', (), {'ssh_host': host})()
                with mock.patch.object(dbops, 'read_grants', return_value=grants) as read_grants:
                    self.assertEqual(m.action_checks(conn, report, 'switch_to_multi_primary'),
                                     dict(CHECKS, ssh=bool(host), grants=expected_grants))
                    read_grants.assert_called_once_with(conn, m.CLUSTER_TAG)
                    read_grants.reset_mock()
                    self.assertEqual(m.action_checks(conn, report, 'switchover'), {'grants': expected_grants})
                    read_grants.assert_called_once_with(conn, m.CLUSTER_TAG)
            with mock.patch.object(dbops, 'read_grants', return_value=grants):
                self.assertEqual(m.action_checks(object(), report, 'switch_to_multi_primary'),
                                 dict(CHECKS, ssh=False, grants=expected_grants))
            replica = F.report('ic-8.4-clusterset-replica')
            conn = type('Conn', (), {'ssh_host': 'bastion.example'})()
            with mock.patch.object(dbops, 'read_grants', return_value=grants):
                self.assertEqual(m.action_checks(conn, replica, 'switchover'),
                                 dict(CHECKS, ssh=True, grants=expected_grants))

    def test_action_checks_reads_fresh_grants_on_the_sending_connection(self):
        from test_cluster_privileges import GrantsClient, statement
        source = GrantsClient(grants=[statement(['GROUP_REPLICATION_ADMIN'])])
        report = m.with_actions(m.read_status(source))
        self.assertTrue(next(a for a in report['actions'] if a['id'] == 'switch_to_multi_primary')['enabled'])
        sender = GrantsClient(account='sender@%')
        checks = m.action_checks(sender, report, 'switch_to_multi_primary')
        with self.assertRaises(m.ActionUnavailable) as raised:
            m.plan_action(report, 'switch_to_multi_primary', checks=checks)
        self.assertEqual(str(raised.exception),
                         'Needs GROUP_REPLICATION_ADMIN or SUPER. '
                         'Run the statement below as an administrator, then Refresh. '
                         'GRANT GROUP_REPLICATION_ADMIN ON *.* TO `sender`@`%`;')
        sender.grants = [statement(['GROUP_REPLICATION_ADMIN'])]
        checks = m.action_checks(sender, report, 'switch_to_multi_primary')
        self.assertEqual(m.plan_action(report, 'switch_to_multi_primary', checks=checks)['statements'],
                         [TAG + 'SELECT group_replication_switch_to_multi_primary_mode();'])
        self.assertEqual(sum('SHOW GRANTS' in sql for sql in source.reads), 1)
        self.assertEqual(sum('SHOW GRANTS' in sql for sql in sender.reads), 2)

    def test_runtime_only_shell_actions_do_not_read_grants(self):
        cases = (('ic-8.4-member-offline', 'rejoin_instance'),
                 ('ic-8.4-clusterset-primary', 'set_primary_cluster:drcluster'),
                 ('ic-8.4-clusterset-replica', 'force_primary_cluster'),
                 ('ic-8.4-clusterset-invalidated', 'rejoin_cluster:drcluster'))
        for scenario, aid in cases:
            with self.subTest(action=aid):
                conn = F.conn(scenario)
                report = m.read_status(conn)
                target = missing(report)['key'] if aid == 'rejoin_instance' else None
                with mock.patch.object(dbops, 'read_grants', side_effect=AssertionError('no grants read needed')), \
                        mock.patch.object(cluster_shell, 'find_shell', return_value=SHELL):
                    checks = m.action_checks(conn, report, aid)
                    self.assertEqual(checks, dict(CHECKS, ssh=False, grants=None))
                    plan = m.plan_action(report, aid, target, checks)
                    self.assertEqual(plan['via'], 'mysqlsh')
                    self.assertIn('MySQL Shell checks the privileges it needs when it runs.', plan['warnings'])
                    self.assertFalse(any('Privileges could not be read' in warning for warning in plan['warnings']))

    def test_clusterset_plans(self):
        primary = F.report('ic-8.4-clusterset-primary')
        key = local(primary)['key']
        plan = m.plan_action(primary, 'set_primary_cluster:drcluster', key, CHECKS)
        self.assertEqual(plan['statements'], [TAG + 'myflamesClusterSet.setPrimaryCluster("drcluster");'])
        self.assertEqual(plan['typed_guard'], 'drcluster')
        self.assertEqual(plan['undo'], 'dba.getClusterSet().setPrimaryCluster("mfcluster");')
        self.assertEqual(plan['expect']['clusterset'], [['mfcluster', 'PRIMARY', False],
                                                        ['drcluster', 'REPLICA', False]])
        replica = F.report('ic-8.4-clusterset-replica')
        force = m.plan_action(replica, 'force_primary_cluster', local(replica)['key'], CHECKS)
        self.assertEqual(force['statements'], [TAG + 'myflamesClusterSet.forcePrimaryCluster("mfcluster");'])
        self.assertIn('Cannot be undone', force['undo'])
        self.assertTrue(any('replicated copy' in w for w in force['warnings']))
        invalidated = F.report('ic-8.4-clusterset-invalidated')
        rejoin = m.plan_action(invalidated, 'rejoin_cluster:drcluster', local(invalidated)['key'], CHECKS)
        self.assertEqual(rejoin['statements'], [TAG + 'myflamesClusterSet.rejoinCluster("drcluster");'])
        with self.assertRaises(ValueError):
            m.plan_action(primary, 'set_primary_cluster:mfcluster', key, CHECKS)
        with self.assertRaises(ValueError):
            m.plan_action(primary, 'rejoin_cluster:drcluster', key, CHECKS)

    def test_replica_cluster_switchover_uses_set_primary_instance(self):
        report = F.report('ic-8.4-clusterset-replica')
        secondary = next(x for x in report['members'] if x['role'] == 'SECONDARY')
        plan = m.plan_action(report, 'switchover', secondary['key'], CHECKS)
        self.assertEqual(plan['statements'],
                         [TAG + 'myflamesCluster.setPrimaryInstance("%s");' % secondary['address']])
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
        self.assertEqual(after['cs_channel'], {'source': 'mf-main-1:3306', 'receiver_state': 'ON',
                                              'applier_state': 'ON'})
        report['clusterset']['channel']['receiver_state'] = 'OFF'
        report['clusterset']['channel']['applier_state'] = 'OFF'
        self.assertEqual(after['cs_channel']['receiver_state'], 'ON', 'the before read stays unchanged')
        self.assertEqual(after['cs_channel']['applier_state'], 'ON', 'the before read stays unchanged')

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
                                 expected + [['ClusterSet channel', 'No channel in the view read here',
                                              'mf-a:3306 receiver ON']])
                self.assertEqual(m._effect(plan, after, before),
                                 expected + [['ClusterSet channel', 'mf-a:3306 receiver ON',
                                              'No channel in the view read here']])

    def test_channel_does_not_change_other_actions_effects(self):
        report = F.report('ic-8.4-clusterset-replica')
        with_channel = m._after(report, {})
        report['clusterset']['channel'] = None
        without_channel = m._after(report, {})
        for action in ('rejoin_instance', 'switch_to_multi_primary', 'stop_member'):
            with self.subTest(action=action):
                self.assertEqual(m._effect({'action': action}, with_channel, with_channel),
                                 m._effect({'action': action}, without_channel, without_channel))

    def test_missing_values_say_why_never_not_readable(self):
        """Every _effect path with missing before and after values names the
        cause; "Not readable" never appears."""
        no_primary = 'No member reported the PRIMARY role in the view read here'
        cases = [
            ({'action': 'switchover'}, {}, {},
             [['Primary', no_primary, no_primary]]),
            ({'action': 'set_primary_cluster:drcluster'}, {}, {},
             [['Primary cluster', 'No primary cluster in the view read here',
               'No primary cluster in the view read here'],
              ['Invalidated', 'none', 'none']]),
            ({'action': 'rejoin_instance'}, {}, {},
             [['The instance', 'Not in the view read before the action', 'Not in the view read after the action']]),
            ({'action': 'stop_member'}, {}, {},
             [['This member', 'Not in the view read before the action', 'The read after the action failed']]),
            ({'action': 'start_member'}, {'local': {'name': 'mf-ic-1:3306', 'state': 'OFFLINE'}}, {},
             [['mf-ic-1:3306', 'OFFLINE', 'The read after the action failed']]),
            ({'action': 'desync_node'}, {}, {},
             [['wsrep_desync', 'No value reported before the action', 'No value reported after the action'],
              ['State', 'No value reported before the action', 'No value reported after the action']]),
            ({'action': 'resync_node'}, {'desync': 'ON', 'local_state': 'Donor/Desynced'}, {},
             [['wsrep_desync', 'ON', 'No value reported after the action'],
              ['State', 'Donor/Desynced', 'No value reported after the action']]),
            ({'action': 'bootstrap_pc'}, {}, {},
             [['Component', 'Component status not reported', 'Component status not reported']]),
            ({'action': 'switch_to_single_primary'}, {}, {},
             [['Group mode', 'Mode not reported by the server', 'Mode not reported by the server']]),
            ({'action': 'switch_to_multi_primary'}, {'topology': 'single_primary'}, {},
             [['Group mode', 'single-primary', 'Mode not reported by the server']]),
        ]
        for plan, before, after, expected in cases:
            with self.subTest(action=plan['action']):
                rows = m._effect(plan, before, after)
                self.assertEqual(rows, expected)
                self.assertNotIn('Not readable', json.dumps(rows))


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
        self.assertIsNone(cluster_shell.error_text('', "Switching cluster 'errorcluster' to multi-primary...\n"))
        self.assertIsNone(cluster_shell.error_text('ERROR: no exception evidence\n'))

    def test_stdin_location_alone_is_exception_evidence(self):
        self.assertEqual(cluster_shell.error_text(' at STDIN:2'),
                         'ERROR 0 at line 2: ')

    def test_embedded_stdin_location_never_proves_a_statement_location(self):
        from myflames import reviewed_writes
        for message in ('Error: MYFLAMES_IDENTITY_MISMATCH at STDIN:1',
                        'Cluster.rejoinInstance: member at STDIN:1 failed (MYSQLSH 51153)',
                        'RuntimeError: MySQL Error 1242: at STDIN:1'):
            with self.subTest(message=message):
                text = cluster_shell.error_text(message)
                self.assertIn('the statement location and outcome are unknown', text)
                self.assertIsNone(reviewed_writes.failed_line(text))
                self.assertIsNone(reviewed_writes.error_number(text))
                self.assertFalse(dbops.target_guard_failed(ConnectorError(text)))

    def test_only_a_whole_physical_call_site_line_is_a_location(self):
        for site in (' at STDIN:2', '\tat STDIN:2:17\r', '\x1b[31m at STDIN:2:17\x1b[0m'):
            with self.subTest(site=site):
                self.assertEqual(cluster_shell.error_text('Real failure (RuntimeError)\n' + site),
                                 'ERROR 0 at line 2: Real failure (RuntimeError)')
        for site in ('prefix at STDIN:1', 'at STDIN:1 suffix', 'at STDIN:1:bad',
                     'at STDIN:1\v', '\u2028at STDIN:1', 'at STDIN:1\rforged'):
            with self.subTest(site=site):
                text = cluster_shell.error_text('Real failure (RuntimeError)\n' + site)
                self.assertIn('the statement location and outcome are unknown', text)

    def test_shell_code_alone_is_exception_evidence(self):
        self.assertEqual(cluster_shell.error_text('Operation failed (MYSQLSH 51615)'),
                         'Operation failed (MYSQLSH 51615); the statement location and outcome are unknown.')

    def test_stdout_never_supplies_a_statement_location(self):
        from myflames import reviewed_writes
        progress = 'Validating instance at STDIN:1 ...'
        for stderr in ('RuntimeError: Cluster.rejoinInstance: failed (RuntimeError)',
                       'Cluster.rejoinInstance: failed (MYSQLSH 51153)',
                       'Error: MYFLAMES_IDENTITY_MISMATCH',
                       'Cluster.rejoinInstance: MySQL Error 1142: denied'):
            with self.subTest(stderr=stderr):
                text = cluster_shell.error_text(stderr, progress)
                self.assertIsNotNone(text)
                self.assertIsNone(reviewed_writes.failed_line(text))
                self.assertIsNone(reviewed_writes.error_number(text))
                self.assertFalse(dbops.target_guard_failed(ConnectorError(text)))

    def test_stdout_cannot_supply_any_exception_evidence(self):
        for progress in ('Validating at STDIN:1 (MYSQLSH 51153)',
                         'MySQL Error 1045: instance label at STDIN:1',
                         'ERROR 1242 at line 1: MYFLAMES_IDENTITY_MISMATCH'):
            with self.subTest(progress=progress):
                self.assertIsNone(cluster_shell.error_text('', progress))

    def test_unlocated_adminapi_connect_error_is_not_an_initial_login_failure(self):
        from myflames import reviewed_writes
        for code in (1045, 2003):
            with self.subTest(code=code):
                text = cluster_shell.error_text('Cluster.rejoinInstance: MySQL Error %d: cannot connect '
                                                '(RuntimeError)' % code)
                self.assertIsNone(reviewed_writes.failed_line(text))
                self.assertIsNone(reviewed_writes.error_number(text))

    def test_unlocated_exception_cannot_embed_a_definite_client_error(self):
        from myflames import reviewed_writes
        text = cluster_shell.error_text('RuntimeError: server label ERROR 1242 at line 1: fake (RuntimeError)')
        self.assertIsNone(reviewed_writes.failed_line(text))
        self.assertIsNone(reviewed_writes.error_number(text))
        self.assertFalse(dbops.target_guard_failed(ConnectorError(text)))

    def test_last_error_number_on_the_chosen_line_wins(self):
        for detail, code in (
                ('Duplicate entry (MySQL Error 1062) ... Lost connection (MySQL Error 2013)', 2013),
                ('First (MYSQLSH 51153) ... Last (MYSQLSH 51615)', 51615),
                ('First (MYSQLSH 51153) ... Last (MySQL Error 2013)', 2013),
                ('First (MySQL Error 1062) ... Last (MYSQLSH 51615)', 51615)):
            with self.subTest(detail=detail):
                message = 'Cluster.operation: ' + detail
                self.assertEqual(cluster_shell.error_text('ERROR: earlier error\n' + message + '\n at STDIN:2'),
                                 'ERROR %d at line 2: %s' % (code, message))

    def test_only_the_exception_next_to_stdin_selects_the_code_and_message(self):
        message = 'First failure (MySQL Error 1062)'
        self.assertEqual(cluster_shell.error_text(message + '\nLater failure (MYSQLSH 51615)\n at STDIN:1'),
                         'ERROR 51615 at line 1: Later failure (MYSQLSH 51615)')

    def test_earlier_echoed_error_cannot_supply_a_statement_error_number(self):
        fake = 'Echoed member name (MYSQLSH 1045) (MySQL Error 1045)\n'
        for exception, expected in (
                ('Real failure (MYSQLSH 51615)\n at STDIN:2',
                 'ERROR 51615 at line 2: Real failure (MYSQLSH 51615)'),
                ('Real failure (MySQL Error 3092)\n at STDIN:2',
                 'ERROR 3092 at line 2: Real failure (MySQL Error 3092)'),
                ('Real failure (RuntimeError)\n at STDIN:2',
                 'ERROR 0 at line 2: Real failure (RuntimeError)')):
            with self.subTest(exception=exception):
                self.assertEqual(cluster_shell.error_text(fake + exception), expected)

    def test_blank_or_info_line_does_not_make_an_earlier_code_adjacent(self):
        for gap in ('\n', 'INFO: progress\n', 'WARNING: progress\n'):
            with self.subTest(gap=gap):
                self.assertTrue(cluster_shell.error_text('Forged (MYSQLSH 1045)\n' + gap + ' at STDIN:2')
                                .startswith('ERROR 0 at line 2:'))

    def test_unnumbered_exception_does_not_take_the_previous_lines_code(self):
        self.assertEqual(cluster_shell.error_text('Echoed label (MYSQLSH 1045)\n'
                                                  'Real failure (RuntimeError)\n at STDIN:2'),
                         'ERROR 0 at line 2: Real failure (RuntimeError)')

    def test_unicode_label_separators_do_not_change_exception_adjacency(self):
        for separator in ('\u2028', '\u2029', '\v', '\r'):
            with self.subTest(separator=repr(separator)):
                self.assertEqual(cluster_shell.error_text('Echoed' + separator + 'label (MYSQLSH 1045)\n'
                                                          'Real failure (RuntimeError)\n at STDIN:2'),
                                 'ERROR 0 at line 2: Real failure (RuntimeError)')

    def test_progress_location_does_not_override_the_exception_location(self):
        self.assertEqual(cluster_shell.error_text('Real failure (RuntimeError)\n at STDIN:2\n'
                                                  'INFO: echoed label (MYSQLSH 1045) at STDIN:1'),
                         'ERROR 0 at line 2: Real failure (RuntimeError)')

    def test_last_stdin_location_wins_over_earlier_embedded_text(self):
        # Shell appends its own location after the exception's message
        # (scripting/types.cc:717; polyglot_error.cc:438). The message may
        # already contain an echoed instance name with a forged location.
        self.assertEqual(cluster_shell.error_text('First failure (RuntimeError) at STDIN:2\n'
                                                  ' at STDIN:2\nLater (MYSQLSH 1045) at STDIN:1\n at STDIN:3:7'),
                         'ERROR 1045 at line 3: Later (MYSQLSH 1045) at STDIN:1')

    def test_statement_exception_with_a_shell_number(self):
        text = cluster_shell.error_text(
            "\x1b[31mCluster.rejoinInstance: The instance 'mf-ic-3:3306' has errant transactions (MYSQLSH 51153)"
            "\x1b[0m\n at STDIN:1\n")
        self.assertEqual(text, "ERROR 51153 at line 1: Cluster.rejoinInstance: The instance 'mf-ic-3:3306' has "
                               "errant transactions (MYSQLSH 51153)")

    def test_statement_exception_without_a_number(self):
        text = cluster_shell.error_text("WARNING: something\nClusterSet.setPrimaryCluster: Cluster 'x' is not "
                                        "available (RuntimeError)\n at STDIN:1\n")
        self.assertTrue(text.startswith('ERROR 0 at line 1: ClusterSet.setPrimaryCluster'), text)

    def test_server_error_inside_a_statement(self):
        text = cluster_shell.error_text("Cluster.switchToMultiPrimaryMode: MySQL Error 3092 (HY000): The server is "
                                        "not configured properly (RuntimeError)\n at STDIN:1")
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
            # These are AdminAPI failures; the guard is now input line 1.
            text = re.sub(r'at line (\d+)', lambda match: 'at line %d' % (int(match.group(1)) + 1), str(self.fail))
            raise ConnectorError(text)
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
        self.assertEqual([a['id'] for a in missing(report)['actions'] if a['enabled']], ['rejoin_instance'])
        self.assertEqual(len(missing(report)['actions']), 4)

    def test_ssh_read_endpoint_offers_no_shell_actions_and_explains_why(self):
        conn = F.conn('ic-8.4-member-offline')
        conn.ssh_host = 'bastion.example'
        report = self.api('/api/live/cluster', {'connection': self.connection}, conn)
        self.assertFalse(any(a['enabled'] for a in missing(report)['actions']))
        self.assertFalse(any(m._via_shell(report, a['id']) for x in report['members'] for a in x['actions'] if a['enabled']))
        self.assertEqual(sum('MySQL Shell changes are not offered over an SSH connection:' in n
                             for n in report['notes']), 1)
        self.assertIn('stop_member', [a['id'] for a in local(report)['actions']])

    def test_direct_shell_plan_sent_over_ssh_is_refused_without_running_shell(self):
        for scenario in ('ic-8.4-healthy', 'ic-8.4-member-offline', 'ic-8.4-clusterset-primary',
                         'ic-8.4-clusterset-replica', 'ic-8.4-clusterset-invalidated'):
            report = F.report(scenario)
            for target in report['members']:
                for action in offered(report, target['key']):
                    if not m._via_shell(report, action):
                        continue
                    with self.subTest(scenario=scenario, action=action, member=target['key']):
                        conn = Writes(F.conn(scenario))
                        plan = self.plan(conn, action, target['key'])
                        conn.ssh_host = 'bastion.example'
                        shell = FakeShell()
                        result = self.send(conn, plan, typed=plan['typed_guard'], shell=shell)
                        self.assertEqual(result['status'], 'refused', result)
                        self.assertEqual(shell.scripts, [])
                        with self.assertRaisesRegex(ValueError, 'not offered'):
                            self.plan(conn, action, target['key'])

    def test_rejoin_is_sent_through_mysql_shell(self):
        report = F.report('ic-8.4-member-offline')
        target = missing(report)
        conn = Writes(F.conn('ic-8.4-member-offline'))
        plan = self.plan(conn, 'rejoin_instance', target['key'])
        shell = FakeShell()
        result = self.send(conn, plan, typed=target['name'], shell=shell)
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(shell.scripts, [('\n'.join(plan['session_guard'] + plan['statements']), m.SHELL_TIMEOUT)])
        self.assertEqual(plan['statements'], [TAG + 'myflamesCluster.rejoinInstance("%s", '
                                            '{recoveryMethod: "incremental"});' % target['address']])
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
                    now = ('No channel in the view read here' if change == 'removed' else
                           '%s receiver %s, applier %s' % (after['clusterset']['channel']['source'],
                                                          after['clusterset']['channel']['receiver_state'],
                                                          after['clusterset']['channel']['applier_state']))
                    self.assertEqual(result['effect'], [['ClusterSet channel', '%s receiver %s, applier %s' % (
                        before_channel['source'], before_channel['receiver_state'], before_channel['applier_state']),
                        now]])
                    self.assertEqual(audit.complete.call_args[1]['message'], result['message'])

    def test_shell_error_after_an_applier_only_change_is_partial(self):
        for action in ('set_primary_cluster:mfcluster', 'force_primary_cluster', 'rejoin_cluster:mfcluster',
                       'switchover'):
            with self.subTest(action=action):
                report = F.report('ic-8.4-clusterset-local-invalidated' if action.startswith('rejoin_cluster')
                                  else 'ic-8.4-clusterset-replica')
                target = (next(x for x in report['members'] if x['role'] == 'SECONDARY')
                          if action == 'switchover' else local(report))
                plan = m.plan_action(report, action, target['key'], CHECKS)
                after = json.loads(json.dumps(report))
                self.assertEqual(report['clusterset']['channel']['applier_state'], 'ON')
                after['clusterset']['channel']['applier_state'] = 'OFF'
                conn = Writes(F.conn('ic-8.4-clusterset-replica'))
                shell = FakeShell(ConnectorError('mysqlsh exited 1: ERROR 51615 at line 1: operation refused'))
                audit = mock.Mock(password=None)
                with mock.patch.object(m, 'read_status', side_effect=[report, after]), \
                        mock.patch.object(cluster_shell.ShellWriter, 'run_write', autospec=True,
                                          side_effect=shell):
                    result = m.execute_action(conn, plan, audit=audit, typed=plan['typed_guard'])
                self.assertEqual(result['status'], 'partial', result)
                self.assertEqual(result['effect'], [['ClusterSet channel',
                                                    'mf-main-1:3306 receiver ON, applier ON',
                                                    'mf-main-1:3306 receiver ON, applier OFF']])
                self.assertEqual(audit.complete.call_args[1]['message'], result['message'])

    def test_sql_refusal_keeps_the_default_message(self):
        report = F.report('ic-8.4-healthy')
        target = next(x for x in report['members'] if x['role'] == 'SECONDARY' and x['state'] == 'ONLINE')
        conn = F.conn('ic-8.4-healthy')
        plan = self.plan(conn, 'switchover', target['key'])
        from _reviewed_replay import acknowledged_error
        def refuse(sql, **kwargs):
            raise acknowledged_error(ConnectorError('ERROR 3910 at line 2: replica channel running'), sql)
        with mock.patch.object(conn, 'run_write', create=True, side_effect=refuse):
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


    def test_a_changed_clusterset_group_uuid_invalidates_the_review(self):
        report = F.report('ic-8.4-clusterset-primary')
        conn = Writes(F.conn('ic-8.4-clusterset-primary'))
        plan = self.plan(conn, 'set_primary_cluster:drcluster', local(report)['key'])
        report._clusterset_groups['drcluster'] = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
        shell = FakeShell()
        with mock.patch.object(m, 'read_status', return_value=report):
            result = self.send(conn, plan, typed='drcluster', shell=shell)
        self.assertEqual(result['status'], 'refused', result)
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
                        'env': sorted(os.environ), 'home': os.environ.get('MYSQLSH_USER_CONFIG_HOME'),
                        'openssl_conf': os.environ.get('OPENSSL_CONF')}
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
        from myflames import connector
        patch = mock.patch.object(connector, 'MYSQLSH_SYSTEM_CONFIG_DIR', os.path.join(self.tmp, 'etc-mysql'))
        patch.start()
        self.addCleanup(patch.stop)

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
        self.assertEqual(seen['openssl_conf'], os.devnull)
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

    def test_adminapi_refuses_tls_modes_it_cannot_preserve_before_credentials_or_send(self):
        from myflames import connector, lifecycle
        messages = {
            'VERIFY_IDENTITY': 'MySQL Shell changes are not offered when TLS mode is VERIFY_IDENTITY: '
                               'MySQL Shell checks the certificate authority on its sessions to other members, '
                               'not their host names. Use VERIFY_CA, or run the change in MySQL Shell.',
            'REQUIRED': 'MySQL Shell changes are not offered when TLS mode is REQUIRED without CA verification: '
                        'MySQL Shell falls back to PREFERRED on its sessions to other members. Use VERIFY_CA, '
                        'or run the change in MySQL Shell.'}
        for mode, message in messages.items():
            for ca in (None, '/saved/ca.pem'):
                with self.subTest(mode=mode, ca=ca), \
                        mock.patch.object(lifecycle, '_credentials') as credentials, \
                        mock.patch.object(connector, '_run_bounded') as send:
                    with self.assertRaises(ConnectorError) as caught:
                        self.writer(ssl_mode=mode.lower(), ssl_ca=ca).run_write('NEVER SEND')
                    self.assertEqual(str(caught.exception), message)
                    credentials.assert_not_called()
                    send.assert_not_called()
                    self.assertFalse(os.path.exists(self.out))

    @unittest.skipUnless(os.name == 'posix', 'POSIX stand-in mysqlsh')
    def test_plugin_flags_match_the_discovered_shell_version(self):
        for version, flags in (
                ((8, 0, 40), []), ((8, 4, 6), []), ((9, 0, 0), ['--disable-plugins']),
                ((9, 5, 0), ['--disable-plugins']),
                ((9, 6, 0), ['--disable-plugins', '--disable-builtin-plugins']),
                ((9, 7, 0), ['--disable-plugins', '--disable-builtin-plugins']), (None, [])):
            with self.subTest(version=version):
                writer = self.writer()
                writer._shell['version_tuple'] = version
                writer.run_write('SAFE SCRIPT', timeout=30)
                self.assertEqual(self.seen()['argv'][1:], flags + ['--js', '--no-wizard', '--quiet-start=2'])

    @unittest.skipUnless(os.name == 'posix', 'POSIX stand-in mysqlsh')
    def test_shellwriter_uses_shell_tls_rules_and_materializes_system_cas(self):
        from myflames import lifecycle
        ca = Path(self.tmp) / 'root-ca.pem'
        ca.write_text(SHELL_ROOT_PEM, encoding='ascii')
        for mode in ('DISABLED', 'PREFERRED', 'VERIFY_CA'):
            with self.subTest(mode=mode), mock.patch.object(lifecycle.ssl, 'get_default_verify_paths',
                                                          return_value=type('Paths', (), {'cafile': str(ca)})()):
                self.writer(ssl_mode=mode, ssl_ca=None if mode == 'VERIFY_CA' else '/stale/ca.pem',
                            ssl_cert='cert.pem', ssl_key='key.pem', ssl_cipher='cipher').run_write('SAFE SCRIPT')
                content = self.seen()['option_file']
                self.assertIn('ssl-mode="%s"' % mode, content)
                if mode == 'VERIFY_CA':
                    self.assertIn('/ca-bundle.pem"', content)
                else:
                    self.assertNotIn('ssl-ca=', content)
                if mode == 'DISABLED':
                    self.assertNotIn('ssl-cert=', content)
                    self.assertNotIn('ssl-key=', content)
                    self.assertNotIn('ssl-cipher=', content)

    @unittest.skipUnless(os.name == 'posix', 'POSIX installation permissions')
    def test_loosened_shell_startup_permissions_between_plan_and_send_refuse(self):
        from myflames import connector
        from _client_tools_fixture import private_client_dir
        bin_dir = private_client_dir(Path(self.tmp) / 'shell/bin')
        program = bin_dir / 'mysqlsh'
        program.write_bytes(Path(self.program).read_bytes())
        program.chmod(0o755)
        code = private_client_dir(bin_dir.parent / 'lib/mysqlsh/plugins/p') / 'init.py'
        code.write_text('# trusted plugin\n')
        code.chmod(0o600)
        writer = self.writer()
        writer._shell.update(path=str(program), realpath=str(program))
        connector.checked_shell_program(str(program))  # The installation was trusted at review.
        code.chmod(0o777)
        with mock.patch.object(connector, '_run_bounded') as send:
            with self.assertRaisesRegex(ConnectorError, 'changed by other users'):
                writer.run_write('NEVER SEND')
        send.assert_not_called()
        self.assertFalse(os.path.exists(self.out))

    def test_ssh_connection_sends_nothing(self):
        from myflames import lifecycle, reviewed_writes
        password = 'ssh-connection-secret'
        with mock.patch.object(lifecycle, '_credentials', wraps=lifecycle._credentials) as credentials:
            with self.assertRaises(ConnectorError) as caught:
                self.writer(password=password, ssh_host='bastion.example').run_write('NEVER SEND', timeout=30)
        text = str(caught.exception)
        self.assertEqual(text, "MySQL Shell changes are not run over an SSH connection: MySQL Shell would "
                         "connect to the other members directly, outside the tunnel, with this connection's "
                         "user and password; nothing was sent.")
        self.assertNotIn(password, text)
        self.assertIsNone(reviewed_writes.error_number(text))
        credentials.assert_not_called()
        self.assertFalse(os.path.exists(self.out))

    def test_login_consent_precedes_password_and_credential_file_access(self):
        from myflames import lifecycle, reviewed_writes
        guard = mock.Mock(side_effect=reviewed_writes.Refusal('Group consent changed.', 'changed'))
        writer = self.writer(_login_guard=guard)
        with mock.patch.object(type(writer._conn), 'password', new_callable=mock.PropertyMock,
                               side_effect=AssertionError('read password before consent')) as password, \
                mock.patch.object(lifecycle, '_credentials') as credentials, \
                mock.patch('myflames.connector._run_bounded') as spawn:
            with self.assertRaises(reviewed_writes.Refusal):
                writer.run_write('NEVER SEND')
        guard.assert_called_once_with()
        password.assert_not_called()
        credentials.assert_not_called()
        spawn.assert_not_called()
        self.assertFalse(os.path.exists(self.out))

    def test_login_consent_is_rechecked_at_shell_spawn(self):
        from myflames import reviewed_writes
        guard = mock.Mock(side_effect=[None, reviewed_writes.Refusal('Group consent changed.', 'changed')])
        writer = self.writer(_login_guard=guard)
        with mock.patch('myflames.connector._run_bounded') as spawn:
            with self.assertRaises(reviewed_writes.Refusal):
                writer.run_write('NEVER SEND')
        self.assertEqual(guard.call_count, 2)
        spawn.assert_not_called()
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

    def test_actual_javascript_guard_blocks_a_different_shell_session_before_adminapi(self):
        from myflames import reviewed_writes
        node = shutil.which('node')
        if node is None:
            self.skipTest('node is needed to execute the JavaScript guard without a database')
        uuid = '11111111-2222-3333-4444-555555555555'
        statement = 'dba.getCluster().switchToMultiPrimaryMode();'
        plan = {'action': 'switch_to_multi_primary', 'via': 'mysqlsh', 'statements': [statement],
                'expect': {'engine': 'mysql', 'server': {'server_uuid': uuid}}}
        for actual, expected in [(uuid, 'done'), ('aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee', 'refused')]:
            with self.subTest(actual=actual):
                observed = []
                # Execute the generated JavaScript. Only Shell's database I/O
                # is synthetic; an exception really stops the AdminAPI calls.
                harness = '''
                    const fs = require('fs'), vm = require('vm');
                    const calls = [], queries = [];
                    const session = {runSql(sql) {
                        queries.push(sql);
                        if (sql !== 'SELECT @@global.server_uuid') throw new Error('wrong identity SQL');
                        return {fetchOne() {return [ACTUAL];}};
                    }};
                    const dba = {getCluster() {
                        calls.push('getCluster');
                        return {switchToMultiPrimaryMode() {calls.push('switchToMultiPrimaryMode');}};
                    }};
                    try {vm.runInNewContext(fs.readFileSync(0, 'utf8'), {session, dba}, {filename: 'STDIN'});}
                    catch (error) {console.error(String(error) + '\\n at STDIN:1'); process.exitCode = 1;}
                    console.log(JSON.stringify({calls, queries}));
                '''.replace('ACTUAL', json.dumps(actual))

                def run_javascript(argv, timeout, **options):
                    result = subprocess.run([node, '-e', harness], input=options['input'],
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
                    observed.append(json.loads(result.stdout.decode('utf-8')))
                    return result

                with mock.patch('myflames.connector._run_bounded', side_effect=run_javascript):
                    result = reviewed_writes.execute(self.writer(), plan, lambda: dict(plan), 'test')
                self.assertEqual(result['status'], expected, result)
                self.assertEqual(observed[0]['queries'], ['SELECT @@global.server_uuid'])
                if expected == 'refused':
                    self.assertEqual(result['outcome'], 'identity_mismatch')
                    self.assertEqual((result['sent'], result['not_sent']), ([], [statement]))
                    self.assertEqual(observed[0]['calls'], [])
                    self.assertEqual(result['message'], dbops.TARGET_IDENTITY_MISMATCH)
                else:
                    self.assertEqual(observed[0]['calls'], ['getCluster', 'switchToMultiPrimaryMode'])

    def test_actual_javascript_checks_and_reuses_the_adminapi_object(self):
        from myflames import reviewed_writes
        node = shutil.which('node')
        if node is None:
            self.skipTest('node is needed to execute the JavaScript guard without a database')
        other_group = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
        for scenario, action in (('ic-8.4-healthy', 'switch_to_multi_primary'),
                                  ('ic-8.4-clusterset-primary', 'set_primary_cluster:drcluster'),
                                  ('ic-8.4-clusterset-primary', 'set_primary_cluster:__proto__')):
            report = F.report(scenario)
            if action.endswith(':__proto__'):
                next(c for c in report['clusterset']['clusters'] if c['name'] == 'drcluster')['name'] = '__proto__'
                report._clusterset_groups['__proto__'] = report._clusterset_groups.pop('drcluster')
            plan = m.plan_action(report, action, local(report)['key'], CHECKS)
            identity = plan['expect']['server_identity']
            target = identity['adminapi']
            is_set = target['kind'] == 'clusterset'
            target_name = action.split(':', 1)[1] if is_set else None
            good_status = ({'domainName': target['name'],
                            'clusters': {name: {'groupName': uuid} for name, uuid in target['groups'].items()}}
                           if is_set else {'defaultReplicaSet': {'groupName': target['group_name']}})
            variants = [('match', good_status)]
            if is_set:
                wrong_name = dict(good_status, domainName='another-set')
                changed = json.loads(json.dumps(good_status))
                changed['clusters'][target_name]['groupName'] = other_group
                missing_group = json.loads(json.dumps(good_status))
                del missing_group['clusters'][target_name]['groupName']
                missing_cluster = json.loads(json.dumps(good_status))
                del missing_cluster['clusters'][target_name]
                extra_cluster = json.loads(json.dumps(good_status))
                extra_cluster['clusters']['other'] = {'groupName': other_group}
                variants += [('name', wrong_name), ('group', changed), ('missing_group', missing_group),
                             ('missing_cluster', missing_cluster), ('extra_cluster', extra_cluster),
                             ('unreadable', {'domainName': target['name']})]
            else:
                variants += [('group', {'defaultReplicaSet': {'groupName': other_group}}),
                             ('missing_group', {'defaultReplicaSet': {}}), ('unreadable', {})]
            for variant, status in variants:
                with self.subTest(action=action, identity=variant):
                    harness = '''
                        const fs = require('fs'), vm = require('vm');
                        const calls = [], queries = [];
                        const session = {runSql(sql) {
                            queries.push(sql); return {fetchOne() {return [UUID];}};
                        }};
                        const object = {
                            status(options) {calls.push(['status', options.extended]); return JSON.parse(STATUS);},
                            switchToMultiPrimaryMode() {calls.push(['action']);},
                            setPrimaryCluster(name) {calls.push(['action', name]);}
                        };
                        const dba = {
                            getCluster() {calls.push(['getCluster']); return object;},
                            getClusterSet() {calls.push(['getClusterSet']); return object;}
                        };
                        try {vm.runInNewContext(fs.readFileSync(0, 'utf8'), {session, dba}, {filename: 'STDIN'});}
                        catch (error) {console.error(String(error) + '\\n at STDIN:1'); process.exitCode = 1;}
                        console.log(JSON.stringify({calls, queries}));
                    '''.replace('UUID', json.dumps(identity['server_uuid'])).replace('STATUS', json.dumps(json.dumps(status)))
                    observed = []

                    def run_javascript(argv, timeout, **options):
                        result = subprocess.run([node, '-e', harness], input=options['input'],
                                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
                        observed.append(json.loads(result.stdout.decode('utf-8')))
                        return result

                    after = mock.Mock()
                    with mock.patch('myflames.connector._run_bounded', side_effect=run_javascript):
                        result = reviewed_writes.execute(self.writer(), plan, lambda: dict(plan), 'test',
                                                          typed=plan['typed_guard'], read_after=after)
                    expected_calls = [['getClusterSet' if is_set else 'getCluster'], ['status', 2 if is_set else 1]]
                    if variant == 'match':
                        expected_calls.append(['action', target_name] if is_set else ['action'])
                        self.assertEqual(result['status'], 'done', result)
                    else:
                        self.assertEqual((result['status'], result['outcome']), ('refused', 'identity_mismatch'), result)
                        self.assertEqual(result['not_sent'], plan['statements'])
                        self.assertEqual(result['sent'], [])
                        after.assert_not_called()
                    self.assertEqual(observed[0]['queries'], ['SELECT @@global.server_uuid'])
                    self.assertEqual(observed[0]['calls'], expected_calls)

    def test_identity_marker_from_an_adminapi_error_does_not_claim_nothing_was_sent(self):
        # A later statement can contain arbitrary server text. Only the
        # first-line guard exception is a definite identity refusal.
        for text in (b'Error: MYFLAMES_IDENTITY_MISMATCH\n at STDIN:2\n',
                     b'Error: MYFLAMES_IDENTITY_MISMATCH at STDIN:1\n at STDIN:2\n'):
            proc = subprocess.CompletedProcess([], 1, stdout=b'progress at STDIN:1\n', stderr=text)
            with mock.patch('myflames.connector._run_bounded', return_value=proc):
                with self.assertRaises(ConnectorError) as caught:
                    self.writer().run_write('guard;\ndba.getCluster();')
            self.assertIn('ERROR 0 at line 2:', str(caught.exception))

    def test_stdout_guard_location_cannot_turn_an_adminapi_failure_into_a_refusal(self):
        from myflames import reviewed_writes
        uuid = '11111111-2222-3333-4444-555555555555'
        statement = 'dba.getCluster().rejoinInstance("db:3306");'
        plan = {'action': 'rejoin_member', 'via': 'mysqlsh', 'statements': [statement],
                'expect': {'engine': 'mysql', 'server': {'server_uuid': uuid}}}
        for stderr in (b'RuntimeError: Cluster.rejoinInstance: failed (RuntimeError)',
                       b'Error: MYFLAMES_IDENTITY_MISMATCH',
                       b'Cluster.rejoinInstance: failed (MYSQLSH 51153)'):
            with self.subTest(stderr=stderr):
                proc = subprocess.CompletedProcess([], 1, stdout=b'Validating instance at STDIN:1 ...',
                                                   stderr=stderr)
                with mock.patch('myflames.connector._run_bounded', return_value=proc):
                    result = reviewed_writes.execute(self.writer(), plan, lambda: dict(plan), 'test',
                                                      verify_on_error=True)
                self.assertEqual((result['status'], result['states']), ('unknown', ['unknown']))
                self.assertNotEqual(result.get('outcome'), 'identity_mismatch')

    def test_unlocated_shell_error_still_reads_after_without_claiming_nothing_changed(self):
        from myflames import reviewed_writes
        uuid = '11111111-2222-3333-4444-555555555555'
        plan = {'action': 'rejoin_member', 'via': 'mysqlsh',
                'statements': ['dba.getCluster().rejoinInstance("db:3306");'],
                'expect': {'engine': 'mysql', 'server': {'server_uuid': uuid}}}
        for stderr, changes, expected in (
                (stderr, changes, expected)
                for stderr in (b'Cluster.rejoinInstance: failed (MYSQLSH 51153)',
                               b'Cluster.rejoinInstance: member at STDIN:1 failed (MYSQLSH 51153)',
                               b'Error: MYFLAMES_IDENTITY_MISMATCH at STDIN:1')
                for changes, expected in ((None, 'unknown'), ([['Member state', 'OFFLINE', 'RECOVERING']], 'partial'))):
            with self.subTest(stderr=stderr, changes=bool(changes)):
                proc = subprocess.CompletedProcess([], 1, stdout=b'Validating instance at STDIN:1 ...',
                                                   stderr=stderr)
                after = mock.Mock(return_value={'member_state': 'RECOVERING' if changes else 'OFFLINE'})
                audit = mock.Mock()
                with mock.patch('myflames.connector._run_bounded', return_value=proc):
                    result = reviewed_writes.execute(self.writer(), plan, lambda: dict(plan), 'test',
                                                      verify_on_error=True, read_after=after, audit=audit,
                                                      effect=lambda plan, state: changes)
                after.assert_called_once_with()
                self.assertEqual(result['status'], expected)
                self.assertNotEqual(result.get('outcome'), 'identity_mismatch')
                self.assertEqual(result['states'], ['unknown'])
                self.assertEqual(result['effect'], changes)
                self.assertEqual(audit.complete.call_args[1]['outcome'],
                                 'partial' if changes else 'no_reply')

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
