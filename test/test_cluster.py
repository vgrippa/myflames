"""Cluster status (myflames/cluster.py, ``myflames cluster``, /api/live/cluster,
the MCP tool cluster_status).

Replays live recordings in fixtures/cluster/cluster-*.json (2026-10-20,
throwaway Docker clusters: MariaDB 11.8.9 Galera, Percona XtraDB Cluster
8.4.11, MySQL 8.4.11 InnoDB Cluster with MySQL Router 8.4.10), states derived
from them by editing the recorded answers (a donor, a lost node, no quorum, a
MISSING instance, this member OFFLINE), and synthetic servers for what the
matrix does not run (standalone servers, Performance Schema off, a denied
account, multi-primary, a read replica, metadata 2.4.0). See
test/_cluster_fixtures.py for where each comes from.
"""
import ast
import contextlib
import inspect
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _cluster_fixtures as F
from _perf_replay import FakeFactory, assert_read_only, no_nan, run_cli, run_main
from myflames import cluster as m, ui
from myflames.connector import ConnectorError

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / 'docs' / 'schemas' / 'cluster-1.0.json').read_text(encoding='utf-8'))


def member(report, name):
    return next(x for x in report['members'] if x['name'] == name)


def local(report):
    return next(x for x in report['members'] if x['local'])


def ids(report):
    return [f['id'] for f in report['findings']]


class TestEveryScenario(unittest.TestCase):
    def test_schema_read_only_and_no_nan(self):
        from test_chart_data import validate
        self.assertGreaterEqual(len(F.ALL), 25)
        for name in F.ALL:
            with self.subTest(scenario=name):
                conn = F.conn(name)
                report = m.read_status(conn)
                validate(report, SCHEMA)
                no_nan(self, report)
                assert_read_only(self, conn, m.CLUSTER_TAG)
                self.assertIn(report['kind'], m.KINDS)
                self.assertEqual(report['summary']['members'], len(report['members']))
                self.assertEqual(sum(report['summary'][s] for s in m.STATUSES), len(report['members']))
                self.assertLessEqual(sum(1 for x in report['members'] if x['local']), 1)
                if report['local']:
                    self.assertEqual(local(report)['key'], report['local'])
                # A member whose state this server cannot know is never judged.
                for x in report['members']:
                    if x['state'] is None:
                        self.assertEqual(x['status'], 'unknown', x)
                    self.assertTrue(x['status_why'], x)
                text = m.format_text(report)
                self.assertTrue(text.strip())

    def test_schema_rejects_unknown_fields(self):
        from test_chart_data import validate
        report = F.report('ic-8.4-healthy')
        with self.assertRaises(AssertionError):
            validate(dict(report, extra=1), SCHEMA)
        with self.assertRaises(AssertionError):
            validate(dict(report, kind='ndb'), SCHEMA)

    def test_recordings_replay_to_their_recorded_report(self):
        names = F.recordings()
        for required in ('galera-11.8-healthy', 'pxc-8.4-healthy', 'ic-8.4-healthy'):
            self.assertIn(required, names)
        volatile = ('captured_at',)
        for name in names:
            with self.subTest(recording=name):
                conn, data = F.recorded(name)
                report = m.read_status(conn)
                expected = data['report']
                for key in volatile:
                    report.pop(key)
                    expected.pop(key)
                self.assertEqual(report, expected)
                self.assertEqual(data['source'], 'live')
                for call in data['calls']:
                    self.assertNotIn('password', call[0].lower())

    def test_status_is_the_worst_finding(self):
        rank = {'ok': 0, 'unknown': 1, 'watch': 2, 'problem': 3}
        for name in F.ALL:
            with self.subTest(scenario=name):
                report = F.report(name)
                if report['findings']:
                    worst = max(report['findings'], key=lambda f: rank[f['status']])['status']
                    self.assertEqual(report['status'], worst)
                    self.assertEqual([f['status'] for f in report['findings']],
                                     sorted((f['status'] for f in report['findings']), key=lambda s: -rank[s]))
                    for f in report['findings']:
                        self.assertIn(f['text'], report['status_why'])


class TestGalera(unittest.TestCase):
    def test_healthy_recording(self):
        conn, data = F.recorded('galera-11.8-healthy')
        report = m.read_status(conn)
        self.assertEqual((report['kind'], report['status'], report['engine']), ('galera', 'ok', 'mariadb'))
        self.assertEqual(report['cluster']['name'], 'mf_galera')
        self.assertEqual(report['cluster']['component'], 'Primary')
        self.assertIs(report['cluster']['quorum'], True)
        self.assertEqual((report['cluster']['size'], report['cluster']['expected_size']), (3, 3))
        self.assertEqual(report['galera']['members_source'], 'wsrep_cluster_members')
        self.assertEqual(len(report['members']), 3)
        me = local(report)
        self.assertEqual((me['name'], me['state'], me['status']), ('galera-1', 'Synced', 'ok'))
        self.assertEqual(me['uuid'], report['galera']['gcomm_uuid'])
        self.assertEqual(me['metrics']['local_state_comment'], 'Synced')
        self.assertIs(me['metrics']['flow_control_active'], False)
        for other in report['members']:
            if not other['local']:
                # MariaDB exposes only this node's state.
                self.assertIsNone(other['state'])
                self.assertEqual(other['status'], 'unknown')
                self.assertIn('connect to it', other['status_why'][0])
        self.assertEqual(report['findings'], [])
        self.assertTrue(any('only this node' in n for n in report['notes']))

    def test_port_zero_is_not_reported_and_explained(self):
        report = F.report('galera-11.8-healthy')
        for x in report['members']:
            self.assertTrue(x['address'].endswith(':0'))
            self.assertIsNone(x['port'])
        note = next(n for n in report['notes'] if 'port 0' in n)
        self.assertIn('wsrep_node_incoming_address', note)
        self.assertEqual(m._split_address('[fe80::1]:0'), ('fe80::1', None))
        self.assertEqual(m._split_address('db1:3306'), ('db1', 3306))
        self.assertEqual(m._split_address('db1'), ('db1', None))

    def test_donor_recording(self):
        """Live: SET GLOBAL wsrep_desync=ON on galera-1."""
        self.assertTrue(F.has_recording('galera-11.8-donor'))
        report = F.report('galera-11.8-donor')
        me = local(report)
        self.assertEqual((me['name'], me['state'], me['status']), ('galera-1', 'Donor/Desynced', 'watch'))
        self.assertEqual(me['metrics']['local_state'], 2)
        self.assertEqual(me['metrics']['desync_count'], 1)
        self.assertEqual(report['galera']['desync'], 'ON')
        self.assertEqual((report['status'], ids(report)), ('watch', ['local_state']))
        self.assertIs(report['cluster']['quorum'], True)

    def test_flow_control_threshold_boundary(self):
        for paused, tripped in (('0.0999', False), ('0.1', True), ('0.25', True)):
            with self.subTest(paused=paused):
                conn = F.recorded('galera-11.8-healthy', edit=F.editor((F.WSREP, F.pairs(wsrep_flow_control_paused=paused))))[0]
                report = m.read_status(conn)
                self.assertEqual('flow_control' in ids(report), tripped)
                if tripped:
                    flow = next(f for f in report['findings'] if f['id'] == 'flow_control')
                    self.assertIn('%d%%' % round(float(paused) * 100), flow['text'])
                    self.assertEqual(report['status'], 'watch')

    def test_lost_node_recording(self):
        """Live: galera-2 stopped, read from galera-1."""
        self.assertTrue(F.has_recording('galera-11.8-two-of-three'))
        report = F.report('galera-11.8-two-of-three')
        self.assertEqual((report['cluster']['size'], report['cluster']['expected_size']), (2, 3))
        self.assertEqual(report['cluster']['expected_source'], 'wsrep_cluster_address')
        self.assertEqual((report['status'], ids(report)), ('watch', ['size_below_configured']))
        self.assertIn('names 3, counting this one', report['findings'][0]['text'])
        self.assertEqual([x['name'] for x in report['members']], ['galera-1', 'galera-3'])
        self.assertEqual(report['galera']['members_source'], 'wsrep_cluster_members')

    def test_non_primary_recording(self):
        """Live: galera-3 cut off from the others, read from galera-3."""
        self.assertTrue(F.has_recording('galera-11.8-non-primary'))
        report = F.report('galera-11.8-non-primary')
        self.assertEqual(report['cluster']['component'], 'non-Primary')
        self.assertEqual(report['cluster']['size'], 1)
        self.assertIs(report['cluster']['quorum'], False)
        self.assertEqual((report['status'], ids(report)), ('problem', ['non_primary']))
        # The members table keeps the last Primary view: members come from
        # wsrep_incoming_addresses instead (MDEV-21479).
        self.assertEqual(report['galera']['members_source'], 'wsrep_incoming_addresses')
        self.assertEqual([(x['name'], x['state'], x['status'], x['local']) for x in report['members']],
                         [('galera-3', 'Initialized', 'problem', True)])
        self.assertIs(report['galera']['metrics']['ready'], False)
        self.assertTrue(any('last Primary view' in n for n in report['notes']))
        # Not judged against the configured size while not Primary.
        self.assertNotIn('size_below_configured', ids(report))

    def test_not_ready_in_primary_component(self):
        joined = F.editor((F.WSREP, F.pairs(wsrep_ready='OFF', wsrep_local_state='3',
                                            wsrep_local_state_comment='Joined')))
        report = m.read_status(F.recorded('galera-11.8-healthy', edit=joined)[0])
        finding = next(f for f in report['findings'] if f['id'] == 'not_ready')
        self.assertEqual(finding['status'], 'watch')
        self.assertIn('until it is Synced', finding['text'])
        odd = F.editor((F.WSREP, F.pairs(wsrep_ready='OFF')))
        report = m.read_status(F.recorded('galera-11.8-healthy', edit=odd)[0])
        self.assertEqual(next(f for f in report['findings'] if f['id'] == 'not_ready')['status'], 'problem')

    def test_denied_status_is_unavailable(self):
        report = F.report('galera-11.8-denied')
        self.assertFalse(report['available'])
        self.assertEqual(report['kind'], 'galera')
        self.assertIn('PROCESS', report['message'])
        self.assertEqual(report['status'], 'unknown')

    def test_connection_failure_raises(self):
        conn = F.conn('galera-11.8-healthy')
        conn.error = ConnectorError('mysql client exited 1: ERROR 2003 (HY000): Can\'t connect')
        with self.assertRaises(ConnectorError):
            m.read_status(conn)

    def test_mariadb_without_wsrep_is_not_a_cluster(self):
        report = F.report('mariadb-11.8-standalone')
        self.assertEqual((report['kind'], report['status']), ('none', 'unknown'))
        self.assertIn('wsrep_on is OFF', report['message'])
        self.assertEqual(report['members'], [])
        conn = F.conn('mariadb-11.8-standalone')
        m.read_status(conn)
        self.assertFalse(any('replication_group_members' in sql for sql, _ in conn.calls))

    def test_configured_nodes_counts_this_node_once(self):
        cases = (({'wsrep_cluster_address': 'gcomm://a,b,c', 'hostname': 'b'}, 3),
                 ({'wsrep_cluster_address': 'gcomm://a,b', 'hostname': 'c'}, 3),
                 ({'wsrep_cluster_address': 'gcomm://10.0.0.1:4567,10.0.0.2', 'wsrep_node_address': '10.0.0.1'}, 2),
                 ({'wsrep_cluster_address': 'gcomm://', 'hostname': 'a'}, None),
                 ({'wsrep_cluster_address': 'gcomm://a,b?pc.wait_prim=no', 'wsrep_node_name': 'A'}, 2),
                 ({'wsrep_cluster_address': '', 'hostname': 'a'}, None),
                 # Three instances on one host are three nodes.
                 ({'wsrep_cluster_address': 'gcomm://127.0.0.1:4567,127.0.0.1:4568,127.0.0.1:4569',
                   'wsrep_node_address': '127.0.0.1:4568'}, 3),
                 ({'wsrep_cluster_address': 'gcomm://a,A', 'hostname': 'b'}, 2))
        for variables, expected in cases:
            with self.subTest(variables=variables):
                self.assertEqual(m.configured_nodes(variables), expected)


class TestPerconaXtraDBCluster(unittest.TestCase):
    def test_detected_although_wsrep_on_is_session_only(self):
        conn, data = F.recorded('pxc-8.4-healthy')
        variables = F.parse([c for c in data['calls'] if 'SHOW GLOBAL VARIABLES' in c[0]][0][1])
        self.assertNotIn('wsrep_on', [row[0] for row in variables])
        report = m.read_status(conn)
        self.assertEqual((report['kind'], report['status'], report['engine']), ('pxc', 'ok', 'mysql'))
        self.assertTrue(m.is_galera_node({'wsrep_provider': '/usr/lib64/galera4/libgalera_smm.so'}))
        self.assertFalse(m.is_galera_node({'wsrep_provider': 'none'}))
        self.assertFalse(m.is_galera_node({}))
        self.assertFalse(m.is_galera_node({'wsrep_on': 'OFF', 'wsrep_provider': '/x.so'}))
        self.assertTrue(m.is_galera_node({'wsrep_on': 'ON'}))

    def test_every_node_state_from_pxc_cluster_view(self):
        report = F.report('pxc-8.4-healthy')
        self.assertIn('SELECT performance_schema.pxc_cluster_view', report['statements'])
        me = local(report)
        self.assertEqual((me['state'], me['state_source'], me['pxc_status']),
                         ('Synced', 'wsrep_local_state_comment', 'SYNCED'))
        others = [x for x in report['members'] if not x['local']]
        self.assertEqual(len(others), 2)
        for x in others:
            self.assertEqual((x['state'], x['state_source'], x['status']), ('SYNCED', 'pxc_cluster_view', 'ok'))
            self.assertEqual(x['segment'], 0)
        self.assertEqual(report['summary']['ok'], 3)
        self.assertEqual(report['galera']['pxc_strict_mode'], 'ENFORCING')

    def test_member_not_synced_is_watch(self):
        report = F.report('pxc-8.4-member-donor')
        donor = next(x for x in report['members'] if x['state'] == 'DONOR')
        self.assertEqual(donor['status'], 'watch')
        self.assertEqual(report['status'], 'watch')
        self.assertEqual(report['findings'][0]['member'], donor['key'])
        self.assertEqual(m._pxc_state('SYNCED')[0], 'ok')
        self.assertEqual(m._pxc_state('JOINER')[0], 'watch')
        self.assertEqual(m._pxc_state('')[0], 'unknown')

    def test_maintenance_mode(self):
        report = F.report('pxc-8.4-maintenance')
        self.assertEqual(ids(report), ['pxc_maint_mode'])
        self.assertIn('MAINTENANCE', report['findings'][0]['text'])

    def test_donor_and_lost_node_recordings(self):
        """Live: wsrep_desync=ON on pxc-1; pxc-2 stopped, read from pxc-1."""
        donor = F.report('pxc-8.4-donor')
        me = local(donor)
        self.assertEqual((me['name'], me['state'], me['status']), ('pxc-1', 'Donor/Desynced', 'watch'))
        self.assertTrue(me['pxc_status'])
        self.assertEqual((donor['status'], ids(donor)), ('watch', ['local_state']))
        self.assertEqual([x['status'] for x in donor['members'] if not x['local']], ['ok', 'ok'])
        lost = F.report('pxc-8.4-two-of-three')
        self.assertEqual((lost['cluster']['size'], lost['cluster']['expected_size']), (2, 3))
        self.assertEqual((lost['status'], ids(lost)), ('watch', ['size_below_configured']))
        self.assertEqual([x['name'] for x in lost['members']], ['pxc-1', 'pxc-3'])
        self.assertEqual(lost['galera']['cluster_address'], 'gcomm://mf-pxc-1,mf-pxc-2,mf-pxc-3')

    def test_states_kept_when_the_members_table_is_not_readable(self):
        """Without mysql.wsrep_cluster_members the members come from
        wsrep_incoming_addresses; pxc_cluster_view rows still match by
        LOCAL_INDEX (the same view order), so a node not Synced is judged."""
        def donor(text):
            rows = F.parse(text)
            for row in rows:
                if row[0] == 'pxc-3':
                    row[2] = 'DONOR'
            return F.batch(rows)
        edit = F.editor((F.WSREP_MEMBERS, lambda text: F.refused(
            "mysql client exited 1: ERROR 1142 (42000) at line 1: SELECT command denied to user 'app'@'%' for "
            "table 'wsrep_cluster_members'")), (F.PXC_VIEW, donor))
        conn, data = F.recorded('pxc-8.4-healthy', edit=edit)
        report = m.read_status(conn)
        self.assertEqual(report['galera']['members_source'], 'wsrep_incoming_addresses')
        self.assertEqual([x['name'] for x in report['members']], ['pxc-1', 'pxc-2', 'pxc-3'])
        self.assertEqual(local(report)['name'], 'pxc-1')
        self.assertEqual(member(report, 'pxc-3')['state'], 'DONOR')
        self.assertEqual((report['status'], ids(report)), ('watch', ['member_state']))
        self.assertTrue(all(x['uuid'] for x in report['members']))

    def test_pxc_without_provider_is_not_a_cluster(self):
        report = F.report('pxc-8.4-standalone')
        self.assertEqual(report['kind'], 'none')
        self.assertIn('wsrep_provider is none', report['message'])


class TestGroupReplication(unittest.TestCase):
    def test_innodb_cluster_recording(self):
        report = F.report('ic-8.4-healthy')
        self.assertEqual((report['kind'], report['status']), ('innodb_cluster', 'ok'))
        self.assertEqual(report['cluster']['name'], 'mfcluster')
        self.assertEqual(report['cluster']['topology'], 'single_primary')
        self.assertEqual((report['cluster']['size'], report['cluster']['expected_size'],
                          report['cluster']['failures_tolerated']), (3, 3, 1))
        self.assertIs(report['cluster']['quorum'], True)
        self.assertEqual(report['metadata']['schema_version'], '2.2.0')
        self.assertEqual([x['role'] for x in report['members']].count('PRIMARY'), 1)
        for x in report['members']:
            self.assertEqual((x['state'], x['status'], x['version']), ('ONLINE', 'ok', '8.4.11'))
            self.assertTrue(x['in_metadata'])
            self.assertEqual(x['name'], x['label'])
            self.assertEqual(x['communication_stack'], 'MySQL')
            self.assertIn('transactions_in_queue', x['metrics'])
        router = report['routers'][0]
        self.assertEqual((router['product'], router['address'], router['version']),
                         ('MySQL Router', 'mf-ic-router', '8.4.10'))
        self.assertIsNone(router['name'])
        self.assertEqual(router['ports'], {'RWEndpoint': '6446', 'ROEndpoint': '6447', 'RWSplitEndpoint': '6450',
                                           'RWXEndpoint': '6448', 'ROXEndpoint': '6449'})
        self.assertIsInstance(router['last_check_in_age_s'], int)

    def test_router_attributes_never_leave_the_server_whole(self):
        conn, data = F.recorded('ic-8.4-healthy')
        sql = next(c[0] for c in data['calls'] if 'v2_routers' in c[0])
        self.assertNotIn('r.attributes,', sql)
        self.assertNotIn('router_stats', sql)
        self.assertIn("r.attributes->>'$.RWEndpoint'", sql)
        self.assertNotIn('MetadataUser', json.dumps(data))

    def test_metadata_24_reads_router_stats_first(self):
        conn = F.conn('ic-8.4-metadata-2.4')
        report = m.read_status(conn)
        sql = next(s for s, _ in conn.calls if 'v2_routers' in s)
        self.assertIn('COALESCE(s.last_check_in, r.last_check_in)', sql)
        self.assertIn('LEFT JOIN mysql_innodb_cluster_metadata.router_stats AS s', sql)
        self.assertEqual(report['metadata']['schema_version'], '2.4.0')
        self.assertEqual(len(report['routers']), 1)

    def test_degraded_member_states(self):
        report = F.report('ic-8.4-degraded')
        states = dict((x['state'], x) for x in report['members'])
        self.assertEqual(states['RECOVERING']['status'], 'watch')
        self.assertEqual(states['RECOVERING']['role'], 'SECONDARY')
        self.assertIn('Queues: 4 in certification, 12 in the applier.', states['RECOVERING']['status_why'])
        self.assertEqual(states['UNREACHABLE']['status'], 'problem')
        # UNREACHABLE overrides the state only; the role stays (ps_information.cc).
        self.assertIn(states['UNREACHABLE']['role'], ('PRIMARY', 'SECONDARY'))
        self.assertIs(report['cluster']['quorum'], True)
        self.assertEqual(report['cluster']['failures_tolerated'], 0)
        self.assertEqual(report['status'], 'problem')
        self.assertEqual(sorted(ids(report)), ['member_state', 'member_state', 'no_tolerance'])

    def test_no_quorum(self):
        report = F.report('ic-8.4-no-quorum')
        self.assertIs(report['cluster']['quorum'], False)
        self.assertEqual(report['findings'][0]['id'], 'no_quorum')
        self.assertIn('2 of 3 members are UNREACHABLE', report['findings'][0]['text'])
        self.assertNotIn('primary_count', ids(report))
        # The majority rule of member_info.cc:1450-1464, through the module.
        for total, unreachable, quorum in ((3, 1, True), (3, 2, False), (4, 2, False), (5, 2, True), (2, 1, False),
                                           (1, 0, True)):
            with self.subTest(total=total, unreachable=unreachable):
                report = m.read_status(F.ScenarioConn(F.group(total, unreachable)))
                self.assertIs(report['cluster']['quorum'], quorum)
                self.assertEqual('no_quorum' in ids(report), not quorum)

    def test_unreachable_recording(self):
        """Live: mf-ic-3 paused (expel timeout raised to 60), read from the primary."""
        report = F.report('ic-8.4-unreachable')
        gone = member(report, 'mf-ic-3:3306')
        self.assertEqual((gone['state'], gone['role'], gone['status']), ('UNREACHABLE', 'SECONDARY', 'problem'))
        self.assertIs(report['cluster']['quorum'], True)
        self.assertEqual(report['cluster']['failures_tolerated'], 0)
        self.assertEqual((report['status'], sorted(ids(report))), ('problem', ['member_state', 'no_tolerance']))
        self.assertEqual(local(report)['role'], 'PRIMARY')
        self.assertNotIn('primary_count', ids(report))

    def test_missing_instance_recording(self):
        """Live: STOP GROUP_REPLICATION on mf-ic-3, read from the primary."""
        report = F.report('ic-8.4-member-offline')
        missing = next(x for x in report['members'] if x['state'] == 'MISSING')
        self.assertEqual((missing['name'], missing['status'], missing['state_source']),
                         ('mf-ic-3:3306', 'problem', 'metadata'))
        self.assertEqual((report['cluster']['size'], report['cluster']['expected_size']), (2, 3))
        self.assertNotIn('size_below_configured', ids(report))
        self.assertEqual(sorted(ids(report)), ['member_state', 'no_tolerance'])
        self.assertEqual(report['summary'], {'members': 3, 'ok': 2, 'watch': 0, 'problem': 1, 'unknown': 0})

    def test_this_member_offline_cannot_judge_the_group(self):
        """Live: STOP GROUP_REPLICATION on mf-ic-3, read from mf-ic-3."""
        self.assertTrue(F.has_recording('ic-8.4-local-offline'))
        report = F.report('ic-8.4-local-offline')
        me = local(report)
        self.assertEqual((me['name'], me['state'], me['status'], me['role']), ('mf-ic-3:3306', 'OFFLINE', 'problem', None))
        self.assertEqual(report['kind'], 'innodb_cluster')
        self.assertIsNone(report['cluster']['quorum'])
        self.assertEqual(ids(report), ['local_not_in_group'])
        self.assertIn('connect to an ONLINE member', report['findings'][0]['text'])
        others = [x for x in report['members'] if not x['local']]
        self.assertEqual(len(others), 2)
        for x in others:
            self.assertIsNone(x['state'])
            self.assertEqual(x['status'], 'unknown')

    def test_multi_primary_every_member_primary(self):
        report = F.report('gr-8.4-multi-primary')
        self.assertEqual((report['kind'], report['cluster']['topology']), ('group_replication', 'multi_primary'))
        self.assertEqual([x['role'] for x in report['members']], ['PRIMARY'] * 3)
        self.assertEqual(report['findings'], [])
        self.assertFalse(report['metadata']['available'])

    def test_mixed_versions_and_two_members(self):
        self.assertEqual(ids(F.report('gr-8.4-mixed-versions')), ['version_mismatch'])
        two = F.report('gr-8.4-two-members')
        self.assertEqual(ids(two), ['no_tolerance'])
        self.assertEqual(two['cluster']['failures_tolerated'], 0)

    def test_read_replica_is_not_missing(self):
        report = F.report('ic-8.4-read-replica')
        replica = next(x for x in report['members'] if x.get('instance_type') == 'read-replica')
        self.assertEqual((replica['role'], replica['state'], replica['status']), ('READ_REPLICA', None, 'unknown'))
        self.assertEqual(report['cluster']['expected_size'], 3)
        self.assertEqual(report['status'], 'ok')

    def test_denied_ps_off_standalone_and_idle_plugin(self):
        denied = F.report('gr-8.4-denied')
        self.assertFalse(denied['available'])
        self.assertIn('SELECT on performance_schema', denied['message'])
        off = F.report('gr-8.4-ps-off')
        self.assertEqual((off['kind'], off['status'], off['members']), ('group_replication', 'unknown', []))
        self.assertTrue(any('Performance Schema is off' in n for n in off['notes']))
        for name in ('mysql-8.4-standalone', 'mysql-8.4-plugin-idle'):
            report = F.report(name)
            self.assertEqual((report['kind'], report['status']), ('none', 'unknown'), name)
            self.assertTrue(report['message'].startswith('Not part of a cluster'))
            self.assertEqual(m.format_text(report).count('\n'), 1)

    def test_live_primary_mode_wins_over_metadata(self):
        conn, data = F.recorded('ic-8.4-healthy', edit=F.editor(
            (F.VARIABLES, F.pairs(group_replication_single_primary_mode='OFF'))))
        self.assertEqual(m.read_status(conn)['cluster']['topology'], 'multi_primary')


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        env = dict(os.environ, HOME=self.home.name, USERPROFILE=self.home.name, MYFLAMES_SECRET_BACKEND='vault')
        env.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def test_text_json_output_and_quiet(self):
        code, out, err, factory = run_cli('cluster', ['-h', 'db', '-u', 'app'], F.conn('ic-8.4-healthy'))
        self.assertEqual(code, 0)
        self.assertIn('MySQL InnoDB Cluster mfcluster  [ok]', out)
        self.assertIn('note:', err)
        self.assertEqual(factory.options['query_timeout'], 30)
        code, out, err, _ = run_cli('cluster', ['--json', '-q'], F.conn('galera-11.8-healthy'))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['schema_version'], 'cluster-1.0')
        self.assertEqual(err, '')
        path = os.path.join(self.home.name, 'cluster.json')
        code, out, err, _ = run_cli('cluster', ['--json', '-o', path], F.conn('pxc-8.4-healthy'))
        self.assertEqual((code, out), (0, ''))
        with open(path, encoding='utf-8') as handle:
            self.assertEqual(json.load(handle)['kind'], 'pxc')

    def test_fail_on_levels(self):
        cases = (('ic-8.4-healthy', 'problem', 0), ('ic-8.4-healthy', 'watch', 0),
                 ('galera-11.8-donor', 'problem', 0), ('galera-11.8-donor', 'watch', 1),
                 ('ic-8.4-no-quorum', 'problem', 1), ('mysql-8.4-standalone', 'watch', 0))
        for name, level, expected in cases:
            with self.subTest(scenario=name, level=level):
                code, out, err, _ = run_cli('cluster', ['--fail-on', level], F.conn(name))
                self.assertEqual(code, expected, err)
                self.assertTrue(out)
                self.assertIn('FAIL:' if expected else 'OK:', err)
        code, out, err, _ = run_cli('cluster', ['--fail-on', 'watch', '-q'], F.conn('galera-11.8-donor'))
        self.assertEqual((code, err), (1, ''))

    def test_unavailable_and_connection_failure_exit_two(self):
        code, out, err, _ = run_cli('cluster', [], F.conn('gr-8.4-denied'))
        self.assertEqual((code, out), (2, ''))
        self.assertIn('cannot read performance_schema.replication_group_members', err)
        code, out, err, _ = run_cli('cluster', ['--json'], F.conn('gr-8.4-denied'))
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(out)['available'])
        conn = F.conn('ic-8.4-healthy')
        conn.error = ConnectorError("mysql client exited 1: ERROR 2003 (HY000): Can't connect to MySQL server")
        code, out, err, _ = run_cli('cluster', ['--password=hunter2-not-real'], conn)
        self.assertEqual((code, out), (2, ''))
        self.assertIn("Can't connect", err)
        self.assertNotIn('hunter2-not-real', err)
        code, out, err, _ = run_cli('cluster', ['--fail-on', 'bad'], F.conn('ic-8.4-healthy'))
        self.assertEqual(code, 2)

    def test_not_a_cluster_is_success(self):
        code, out, err, _ = run_cli('cluster', [], F.conn('mariadb-11.8-standalone'))
        self.assertEqual(code, 0)
        self.assertIn('Not part of a cluster', out)

    def test_main_dispatch_help_and_root_listing(self):
        code, out, err = run_main(['myflames', 'cluster', '--json', '-q'], F.conn('ic-8.4-healthy'))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['kind'], 'innodb_cluster')
        code, out, err = run_main(['myflames', 'cluster', '--help'])
        self.assertEqual(code, 0)
        out = ' '.join(out.split())
        for text in ('--profile', '--json', '--output', '--quiet', '--fail-on', 'Exit codes', 'Read-only',
                     'Galera', 'Percona XtraDB Cluster', 'InnoDB Cluster', 'exits 0'):
            self.assertIn(text, out)
        code, out, err = run_main(['myflames', '--help'])
        self.assertIn('cluster', out)

    def test_python37_syntax(self):
        for path in (Path(m.__file__), Path(__file__), Path(F.__file__)):
            ast.parse(path.read_text(), **({'feature_version': 7} if sys.version_info >= (3, 8) else {}))


class TestAPI(unittest.TestCase):
    def call(self, conn):
        workspace = mock.Mock()
        workspace.store.connection_with_saved_password.side_effect = lambda c: dict(c, password='saved-secret')
        self.factory = FakeFactory(conn)
        with mock.patch('myflames.connector.MySQLConnection', self.factory):
            return ui.api_response('/api/live/cluster', {'connection': {
                'host': '127.0.0.1', 'port': 3306, 'user': 'root', 'profile_id': 'saved'}}, workspace)

    def test_allowlist_dispatch_saved_password_and_timeout(self):
        report = self.call(F.conn('ic-8.4-healthy'))
        self.assertEqual(report['schema_version'], 'cluster-1.0')
        self.assertEqual(self.factory.options['password'], 'saved-secret')
        self.assertEqual(self.factory.options['query_timeout'], 30)
        self.assertIn('/api/live/cluster', ui._SERVER_VIEW_PATHS)
        self.assertIn('/api/live/cluster', ui._CONNECTION_PATHS)
        self.assertNotIn('/api/live/cluster', ui._WRITE_PATHS)
        self.assertIn('"/api/live/cluster"', inspect.getsource(ui.WorkspaceHandler.do_POST))
        with self.assertRaisesRegex(ValueError, 'requires a running workspace'):
            ui.api_response('/api/live/cluster', {})

    def test_not_a_cluster_and_unavailable_are_answers(self):
        self.assertEqual(self.call(F.conn('mysql-8.4-standalone'))['kind'], 'none')
        self.assertFalse(self.call(F.conn('gr-8.4-denied'))['available'])


class TestMCP(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        env = dict(os.environ, HOME=self.home.name, USERPROFILE=self.home.name, MYFLAMES_SECRET_BACKEND='vault')
        env.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def run_tool(self, name, detail='core'):
        from myflames import mcp_server as mcp
        conn = F.conn(name)
        factory = FakeFactory(conn)
        with mock.patch('myflames.connector.MySQLConnection', factory), \
                mock.patch('myflames.workspace_cli._resolve_connection', return_value={
                    'host': 'localhost', 'port': 3306, 'user': 'app', 'password': 'SAVED-SECRET'}) as resolve:
            result = mcp.cluster_status_tool('fixture', detail=detail)
        self.assertEqual(resolve.call_args[0][0].profile, 'fixture')
        self.assertIsNone(resolve.call_args[0][0].password)
        assert_read_only(self, conn, m.CLUSTER_TAG)
        return result

    def test_core_text_and_full_json_for_each_kind(self):
        for name, kind in (('galera-11.8-healthy', 'galera'), ('pxc-8.4-healthy', 'pxc'),
                           ('ic-8.4-degraded', 'innodb_cluster'), ('mysql-8.4-standalone', 'none')):
            with self.subTest(scenario=name):
                core = self.run_tool(name)
                full = self.run_tool(name, 'full')
                self.assertIsInstance(core, str)
                self.assertEqual((full['schema_version'], full['kind']), ('cluster-1.0', kind))
                self.assertLess(len(core), len(json.dumps(full)))
                self.assertNotIn('SAVED-SECRET', core + json.dumps(full))
                for f in full['findings']:
                    self.assertIn(f['text'], core)

    def test_tool_is_read_only_and_takes_no_password(self):
        from myflames import mcp_server as mcp
        fn = mcp.cluster_status_tool
        self.assertIn('Read-only', fn.__doc__)
        self.assertNotIn('password', inspect.signature(fn).parameters)
        for kwargs in ({'detail': 'digest'}, {'profile': ''}, {'profile': None}):
            with self.subTest(kwargs=kwargs), mock.patch('myflames.workspace_cli._resolve_connection') as resolve:
                values = dict({'profile': 'fixture'}, **kwargs)
                with self.assertRaises(ValueError):
                    fn(**values)
                resolve.assert_not_called()


if __name__ == '__main__':
    unittest.main()
