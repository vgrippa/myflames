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
        # Equal pc.weight assumed, and said so.
        self.assertEqual(report['cluster']['failures_tolerated'], 1)
        self.assertTrue(any('assumes equal pc.weight' in n for n in report['notes']))
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
            # One format: the host alone, the note explains port 0.
            self.assertEqual(m.address_text(x), x['host'])
        note = next(n for n in report['notes'] if 'port 0' in n)
        self.assertIn('wsrep_node_incoming_address', note)
        # The note lists the nodes in the members' order.
        self.assertIn(', '.join(x['name'] for x in report['members']), note)
        text = m.format_text(report)
        self.assertNotIn(':0', text)
        self.assertIn('galera-2  192.168.80.3  in the component', text)
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
        # The finding names the measured cause and does not repeat the state.
        finding = report['findings'][0]
        self.assertEqual(finding['title'], 'This node is Donor/Desynced')
        self.assertTrue(finding['text'].startswith('wsrep_desync is ON: '), finding['text'])
        self.assertNotIn('Donor/Desynced:', finding['text'])

    def test_desync_off_names_the_other_causes(self):
        """Donor/Desynced with wsrep_desync OFF and wsrep_desync_count 2 (an RSU
        schema change, Ma/mysql-test/suite/galera/r/MW-258.result): no claim
        that wsrep_desync is ON."""
        edit = F.editor((F.VARIABLES, F.pairs(wsrep_desync='OFF')), (F.WSREP, F.pairs(wsrep_desync_count='2')))
        report = m.read_status(F.recorded('galera-11.8-donor', edit=edit)[0])
        text = report['findings'][0]['text']
        self.assertNotIn('wsrep_desync is ON', text)
        for cause in ('state transfer', 'FLUSH TABLES WITH READ LOCK', 'BACKUP STAGE BLOCK_DDL', 'RSU'):
            self.assertIn(cause, text)

    def test_flow_control_threshold_boundary(self):
        for paused, tripped in (('0.0999', False), ('0.1', True), ('0.25', True)):
            with self.subTest(paused=paused):
                conn = F.recorded('galera-11.8-healthy', edit=F.editor((F.WSREP, F.pairs(wsrep_flow_control_paused=paused))))[0]
                report = m.read_status(conn)
                self.assertEqual('flow_control' in ids(report), tripped)
                if tripped:
                    flow = next(f for f in report['findings'] if f['id'] == 'flow_control')
                    self.assertIn(m.flow_percent(float(paused)), flow['text'])
                    self.assertEqual(report['status'], 'watch')
                self.assertIn('flow control paused %s' % m.flow_percent(float(paused)), m.format_text(report))

    def test_flow_percent_one_format(self):
        """One percent text for the finding, the CLI and the workspace: one
        decimal, cut so a value under the watch line never reads as it."""
        for value, text in ((0, '0%'), (0.0004, '<0.1%'), (0.004, '0.4%'), (0.0995, '9.9%'), (0.1, '10%'),
                            (0.125, '12.5%'), (0.29, '29%'), (1, '100%'), (None, '?')):
            with self.subTest(value=value):
                self.assertEqual(m.flow_percent(value), text)

    def test_lost_node_recording(self):
        """Live: galera-2 stopped, read from galera-1."""
        self.assertTrue(F.has_recording('galera-11.8-two-of-three'))
        report = F.report('galera-11.8-two-of-three')
        self.assertEqual((report['cluster']['size'], report['cluster']['expected_size']), (2, 3))
        self.assertEqual(report['cluster']['expected_source'], 'wsrep_cluster_address')
        self.assertEqual((report['status'], ids(report)), ('watch', ['size_below_configured', 'no_tolerance']))
        text = report['findings'][0]['text']
        self.assertIn('names 3 (this node included)', text)
        # The lab names nodes by container (mf-galera-N) and members are IPs and
        # node names (galera-N): the missing node is counted, never guessed.
        self.assertIn('1 configured node is not in the component and cannot be named here', text)
        self.assertEqual(report['galera']['missing'], [])
        self.assertEqual([x['name'] for x in report['members']], ['galera-1', 'galera-3'])
        self.assertEqual(report['galera']['members_source'], 'wsrep_cluster_members')
        # Two nodes left: one more failure loses the majority.
        self.assertEqual(report['cluster']['failures_tolerated'], 0)
        self.assertEqual(report['findings'][1]['title'], 'Cannot survive another failure')

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
        self.assertIsNone(report['cluster']['failures_tolerated'])
        # What still runs on both engines (Ma/sql/sql_parse.cc, PXC sql_parse.cc).
        self.assertIn('information_schema or performance_schema', report['findings'][0]['text'])
        # The node keeps its gcomm key and UUID across Primary and non-Primary.
        me = local(report)
        self.assertEqual(me['uuid'], report['galera']['gcomm_uuid'])
        self.assertEqual(me['key'], 'node:' + report['galera']['gcomm_uuid'])
        self.assertEqual(me['key'], member(F.report('galera-11.8-healthy'), 'galera-3')['key'])

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

    def test_configured_nodes_never_adds_this_node_blindly(self):
        """wsrep_cluster_address gives an expected size only when this node
        is identified: named by one entry, or proved absent (every entry is
        another member, or on this host with another port)."""
        others = [{'host': 'a', 'name': 'node-a', 'local': False}, {'host': 'b', 'name': 'node-b', 'local': False}]
        me = [{'host': '10.0.0.3', 'name': 'node-c', 'local': True}]
        cases = (({'wsrep_cluster_address': 'gcomm://a,b,c', 'hostname': 'b'}, (), 3),
                 # Lists only the others: absent only when both are proved to be others.
                 ({'wsrep_cluster_address': 'gcomm://a,b', 'hostname': 'c'}, others, 3),
                 ({'wsrep_cluster_address': 'gcomm://a,b', 'hostname': 'c'}, (), None),
                 ({'wsrep_cluster_address': 'gcomm://10.0.0.1:4567,10.0.0.2', 'wsrep_node_address': '10.0.0.1'}, (), 2),
                 ({'wsrep_cluster_address': 'gcomm://', 'hostname': 'a'}, (), None),
                 ({'wsrep_cluster_address': 'gcomm://a,b?pc.wait_prim=no', 'wsrep_node_name': 'A'}, (), 2),
                 ({'wsrep_cluster_address': '', 'hostname': 'a'}, (), None),
                 # IPs with wsrep_node_address unset (MariaDB's default): this
                 # node is found by its own incoming address.
                 ({'wsrep_cluster_address': 'gcomm://10.0.0.1,10.0.0.2,10.0.0.3', 'hostname': 'db3',
                   'wsrep_node_address': ''}, me, 3),
                 ({'wsrep_cluster_address': 'gcomm://10.0.0.1,10.0.0.2,10.0.0.3', 'hostname': 'db3'}, (), None),
                 # FQDN entries against the short host name.
                 ({'wsrep_cluster_address': 'gcomm://db1.example.com,db2.example.com,db3.example.com',
                   'hostname': 'db1'}, (), 3),
                 # Codex review: two entries share the short name: not identified.
                 ({'wsrep_cluster_address': 'gcomm://db1.red.example,db1.blue.example,db2.example',
                   'hostname': 'db1'}, (), None),
                 # Different domains never match each other.
                 ({'wsrep_cluster_address': 'gcomm://db1.red.example,db2.red.example',
                   'hostname': 'db1.blue.example'}, (), None),
                 # Three instances on one host are three nodes; the port decides.
                 ({'wsrep_cluster_address': 'gcomm://127.0.0.1:4567,127.0.0.1:4568,127.0.0.1:4569',
                   'wsrep_node_address': '127.0.0.1:4568'}, (), 3),
                 # Codex review: seeds name the two other instances on this host.
                 ({'wsrep_cluster_address': 'gcomm://127.0.0.1:4567,127.0.0.1:4569',
                   'wsrep_node_address': '127.0.0.1:4568'}, (), 3),
                 # MTR's galera_3nodes.cnf: node 2 names node 1 only.
                 ({'wsrep_cluster_address': 'gcomm://127.0.0.1:16000', 'wsrep_node_address': '127.0.0.1:16003'}, (), 2),
                 # Several entries on this host and no port of its own: not identified.
                 ({'wsrep_cluster_address': 'gcomm://127.0.0.1:4567,127.0.0.1:4569', 'hostname': 'h',
                   'wsrep_node_address': '127.0.0.1'}, (), None),
                 ({'wsrep_cluster_address': 'gcomm://a,A', 'hostname': 'b'}, (), None))
        for variables, members, expected in cases:
            with self.subTest(variables=variables, members=members):
                self.assertEqual(m.configured_nodes(variables, {}, members), expected)
        self.assertEqual(m.cluster_address_entries('gcomm://[fe80::1]:4567,B:4568?x=1'),
                         [('fe80::1', 4567), ('b', 4568)])

    def test_address_variants_on_a_healthy_cluster(self):
        """The same healthy 3-node component with the address written other
        ways (live finding: IPs and FQDNs gave a false watch, a partial list
        gave '3 of 2')."""
        for name, expected in (('galera-11.8-ip-address', 3), ('galera-11.8-fqdn', 3),
                               ('galera-11.8-partial-seeds', None), ('galera-11.8-dns-service', None)):
            with self.subTest(scenario=name):
                report = F.report(name)
                self.assertEqual((report['status'], ids(report)), ('ok', []))
                self.assertEqual(report['cluster']['expected_size'], expected)
                text = m.format_text(report)
                self.assertNotIn(' of 1', text)
                self.assertNotIn(' of 2', text)
                if expected is None:
                    self.assertTrue(any('lower bound' in n or 'fewer than the 3' in n for n in report['notes']),
                                    report['notes'])
        # A partial list names fewer than the component: size alone, and why.
        report = F.report('galera-11.8-partial-seeds')
        self.assertIsNone(report['galera']['address_names_this_node'])
        self.assertEqual(report['galera']['address_nodes'], 1)
        # A node down with IP:port entries: the missing entry is named.
        report = F.report('galera-11.8-ip-port-two-of-three')
        self.assertEqual(report['galera']['missing'], ['192.168.80.3:4567'])
        self.assertIn('192.168.80.3:4567 is not in the component', report['findings'][0]['text'])

    def test_two_nodes_cannot_survive_another_failure(self):
        """A deliberate two-node cluster: (2 - 1) / 2 = 0 (galera_kill_nochanges.test:
        the survivor of a kill goes non-Primary)."""
        report = F.report('galera-11.8-two-nodes')
        self.assertEqual((report['cluster']['size'], report['cluster']['expected_size']), (2, 2))
        self.assertEqual((report['status'], ids(report)), ('watch', ['no_tolerance']))
        self.assertIn('assumes equal pc.weight', report['findings'][0]['text'])
        weights = F.editor((F.WSREP, F.pairs(wsrep_cluster_weight='5')))
        report = m.read_status(F.recorded('galera-11.8-healthy', edit=weights)[0])
        self.assertIsNone(report['cluster']['failures_tolerated'])
        self.assertTrue(any('pc.weight differ' in n for n in report['notes']))
        single = F.editor((F.WSREP, F.pairs(wsrep_cluster_size='1', wsrep_cluster_weight='1')), (F.VARIABLES, F.pairs(wsrep_cluster_address='gcomm://')))
        report = m.read_status(F.recorded('galera-11.8-healthy', edit=single)[0])
        self.assertIn('no_tolerance', ids(report))

    def test_incomplete_status_is_not_judged(self):
        """The completeness rule the Full analysis shares: a required name
        missing, or a flow-control fraction out of range, judges nothing."""
        for changes in ({'wsrep_cluster_size': None}, {'wsrep_ready': None}, {'wsrep_flow_control_paused': '1.5'},
                        # Codex review: invalid sizes, and incomplete plus unhealthy.
                        {'wsrep_cluster_size': 'bogus'}, {'wsrep_cluster_size': '-3'},
                        {'wsrep_cluster_size': None, 'wsrep_ready': 'OFF'}):
            with self.subTest(changes=changes):
                conn = F.recorded('galera-11.8-healthy', edit=F.editor((F.WSREP, F.pairs(**changes))))[0]
                report = m.read_status(conn)
                self.assertEqual(report['status'], 'unknown')
                self.assertTrue(report['galera']['incomplete'])
                self.assertTrue(any(n.startswith('Not judged: ') for n in report['notes']))
                # Nothing judged: no finding, no member verdict, no --fail-on trip.
                self.assertEqual(report['findings'], [])
                self.assertEqual(set(x['status'] for x in report['members']), {'unknown'})
                self.assertEqual(m.at_or_worse(report, 'watch'), [])
        # A missing provider counter alone (flow control) is not required.
        report = m.read_status(F.recorded('galera-11.8-healthy', edit=F.editor(
            (F.WSREP, F.pairs(wsrep_flow_control_paused=None))))[0])
        self.assertEqual(report['status'], 'ok')

    def test_certification_failures_one_rule_with_the_full_analysis(self):
        edit = F.editor((F.WSREP, F.pairs(wsrep_local_cert_failures='7')))
        report = m.read_status(F.recorded('galera-11.8-healthy', edit=edit)[0])
        finding = next(f for f in report['findings'] if f['id'] == 'cert_failures')
        self.assertEqual((finding['status'], finding['text']), ('watch', m.CERT_FAILURES_TEXT % 7))
        self.assertEqual(report['status'], 'watch')

    def test_wsrep_on_off_with_the_provider_joined_is_a_problem(self):
        """SET GLOBAL wsrep_on=OFF on a joined MariaDB node: still Galera, not
        'Not part of a cluster' (Ma/sql/wsrep_var.cc, mysqld.cc:8070-8085)."""
        report = F.report('galera-11.8-wsrep-off')
        self.assertEqual((report['kind'], report['status']), ('galera', 'problem'))
        self.assertEqual(ids(report), ['wsrep_off'])
        me = local(report)
        self.assertEqual((me['name'], me['status'], me['state']), ('galera-1', 'problem', 'Not replicating'))
        self.assertIn('not replicated', report['findings'][0]['text'])
        code, out, err, _ = run_cli('cluster', ['--fail-on', 'problem'], F.conn('galera-11.8-wsrep-off'))
        self.assertEqual(code, 1, err)
        # The provider not connected: not part of a cluster, said precisely.
        edit = F.editor((F.VARIABLES, F.pairs(wsrep_on='OFF')),
                        (F.WSREP, F.rows(lambda rs: [r for r in rs if r[0] in F.SERVER_WSREP_NAMES and r[0] not in (
                            'wsrep_connected', 'wsrep_cluster_size')] + [['wsrep_connected', 'OFF'],
                                                                         ['wsrep_cluster_size', '0']])))
        report = m.read_status(F.recorded('galera-11.8-healthy', edit=edit)[0])
        self.assertEqual(report['kind'], 'none')
        self.assertIn('not connected', report['message'])
        # Unreadable is not disconnected (Codex review): not available.
        edit = F.editor((F.VARIABLES, F.pairs(wsrep_on='OFF')), (F.WSREP, lambda text: F.refused(
            "mysql client exited 1: ERROR 1227 (42000) at line 1: Access denied; you need (at least one of) the "
            "PROCESS privilege(s) for this operation")))
        report = m.read_status(F.recorded('galera-11.8-healthy', edit=edit)[0])
        self.assertEqual((report['kind'], report['available'], report['status']), ('galera', False, 'unknown'))
        self.assertIn('not known', report['message'])

    def test_joining_node_is_not_called_lost_quorum(self):
        """A joiner reads non-Primary until its state transfer ends
        (Ma/sql/wsrep_server_service.cc:356-380): watch, joining."""
        report = F.report('galera-11.8-joiner')
        self.assertEqual((report['status'], ids(report)), ('watch', ['state_transfer']))
        self.assertNotIn('lost quorum', report['findings'][0]['text'])
        self.assertEqual(local(report)['status'], 'watch')
        # Not connected while non-Primary is still a problem.
        lost = F.editor((F.WSREP, F.pairs(wsrep_cluster_status='non-Primary', wsrep_connected='OFF',
                                          wsrep_local_state_comment='Joining')))
        self.assertIn('non_primary', ids(m.read_status(F.recorded('galera-11.8-healthy', edit=lost)[0])))

    def test_galera_on_mysql_is_not_called_mariadb(self):
        report = F.report('galera-mysql-8.4-healthy')
        self.assertEqual((report['engine'], report['kind']), ('mysql', 'galera'))
        self.assertEqual(m.kind_label(report), 'Galera Cluster for MySQL')
        self.assertTrue(m.format_text(report).startswith('Galera Cluster for MySQL mf_pxc'))
        self.assertEqual(m.kind_label(F.report('galera-11.8-healthy')), 'MariaDB Galera Cluster')


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
            # The node's own word; the server's raw value in pxc_status.
            self.assertEqual((x['state'], x['pxc_status'], x['state_source'], x['status']),
                             ('Synced', 'SYNCED', 'pxc_cluster_view', 'ok'))
            self.assertEqual(x['segment'], 0)
        # One casing for every node in the text output.
        self.assertNotIn('SYNCED', m.format_text(report))
        self.assertEqual(report['summary']['ok'], 3)
        self.assertEqual(report['galera']['pxc_strict_mode'], 'ENFORCING')

    def test_member_not_synced_is_watch(self):
        report = F.report('pxc-8.4-member-donor')
        donor = next(x for x in report['members'] if x.get('pxc_status') == 'DONOR')
        self.assertEqual((donor['state'], donor['status']), ('Donor/Desynced', 'watch'))
        self.assertEqual(report['status'], 'watch')
        finding = report['findings'][0]
        self.assertEqual(finding['member'], donor['key'])
        self.assertEqual(finding['title'], '%s is Donor/Desynced' % donor['name'])
        self.assertNotIn('DONOR:', finding['text'])

    def test_every_pxc_cluster_view_state_has_its_own_sentence(self):
        """The Galera provider's node states as pxc_cluster_view prints them
        (seen live: SYNCED, DONOR, JOINER, PRIMARY); PRIMARY never reads as
        the word Primary on a node."""
        sentences = set()
        for raw, word, status in (('SYNCED', 'Synced', 'ok'), ('DONOR', 'Donor/Desynced', 'watch'),
                                  ('DESYNCED', 'Donor/Desynced', 'watch'), ('JOINER', 'Joiner', 'watch'),
                                  ('JOINED', 'Joined', 'watch'), ('PRIMARY', 'Joining', 'watch'),
                                  ('NON-PRIMARY', 'Non-Primary', 'watch'), ('ODD', 'Odd', 'watch')):
            with self.subTest(raw=raw):
                got = m._pxc_state(raw)
                self.assertEqual(got[:2], (word, status))
                self.assertFalse(got[2].upper().startswith(raw + ':'))
                sentences.add(got[2])
        self.assertEqual(len(sentences), 7)
        self.assertEqual(m._pxc_state('')[1], 'unknown')
        for name, word in (('pxc-8.4-member-joiner', 'Joiner'), ('pxc-8.4-member-primary', 'Joining')):
            report = F.report(name)
            self.assertEqual([x['state'] for x in report['members'] if x['status'] == 'watch'], [word])

    def test_finding_text_never_repeats_the_state(self):
        for name in F.ALL:
            report = F.report(name)
            for f in report['findings']:
                if f['id'] in ('local_state', 'member_state'):
                    state = f['title'].rsplit(' is ', 1)[1]
                    with self.subTest(scenario=name, finding=f['title']):
                        self.assertFalse(f['text'].startswith(state + ':'), f['text'])

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
        self.assertEqual((lost['status'], ids(lost)), ('watch', ['size_below_configured', 'no_tolerance']))
        self.assertEqual([x['name'] for x in lost['members']], ['pxc-1', 'pxc-3'])
        # The node that left is named: its entry matches no member.
        self.assertEqual(lost['galera']['missing'], ['mf-pxc-2'])
        self.assertIn('mf-pxc-2 is not in the component', lost['findings'][0]['text'])
        self.assertIn('mf-pxc-2  (in wsrep_cluster_address, not in the component)', m.format_text(lost))
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
        self.assertEqual((member(report, 'pxc-3')['state'], member(report, 'pxc-3')['pxc_status']),
                         ('Donor/Desynced', 'DONOR'))
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
        # One scale: a member missing from the reachable group while quorum
        # holds is watch, like a Galera node that left.
        self.assertEqual(states['UNREACHABLE']['status'], 'watch')
        # UNREACHABLE overrides the state only; the role stays (ps_information.cc).
        self.assertIn(states['UNREACHABLE']['role'], ('PRIMARY', 'SECONDARY'))
        self.assertIs(report['cluster']['quorum'], True)
        self.assertEqual(report['cluster']['failures_tolerated'], 0)
        self.assertEqual(report['status'], 'watch')
        self.assertEqual(sorted(ids(report)), ['member_state', 'member_state', 'no_tolerance'])

    def test_no_quorum(self):
        report = F.report('ic-8.4-no-quorum')
        self.assertIs(report['cluster']['quorum'], False)
        self.assertEqual(report['findings'][0]['id'], 'no_quorum')
        self.assertIn('2 of 3 members are UNREACHABLE', report['findings'][0]['text'])
        self.assertNotIn('primary_count', ids(report))
        # No tolerance is claimed without quorum (MySQL Shell's NO_QUORUM branch).
        self.assertIsNone(report['cluster']['failures_tolerated'])
        self.assertNotIn('tolerates', m.format_text(report))
        # The majority rule of member_info.cc:1450-1464, through the module,
        # and the failures it still survives over the current view.
        for total, unreachable, quorum, tolerated in (
                (3, 1, True, 0), (3, 2, False, None), (4, 1, True, 0), (4, 2, False, None), (5, 1, True, 1),
                (5, 2, True, 0), (6, 2, True, 0), (6, 3, False, None), (7, 3, True, 0), (9, 4, True, 0),
                (2, 1, False, None), (1, 0, True, 0), (3, 0, True, 1), (5, 0, True, 2)):
            with self.subTest(total=total, unreachable=unreachable):
                report = m.read_status(F.ScenarioConn(F.group(total, unreachable)))
                self.assertIs(report['cluster']['quorum'], quorum)
                self.assertEqual('no_quorum' in ids(report), not quorum)
                self.assertEqual(report['cluster']['failures_tolerated'], tolerated)
                self.assertEqual('no_tolerance' in ids(report), tolerated == 0)

    def test_unreachable_recording(self):
        """Live: mf-ic-3 paused (expel timeout raised to 60), read from the primary."""
        report = F.report('ic-8.4-unreachable')
        gone = member(report, 'mf-ic-3:3306')
        self.assertEqual((gone['state'], gone['role'], gone['status']), ('UNREACHABLE', 'SECONDARY', 'watch'))
        self.assertIs(report['cluster']['quorum'], True)
        self.assertEqual(report['cluster']['failures_tolerated'], 0)
        self.assertEqual((report['status'], sorted(ids(report))), ('watch', ['member_state', 'no_tolerance']))
        self.assertIn('2 of 3 members are reachable',
                      next(f['text'] for f in report['findings'] if f['id'] == 'no_tolerance'))
        self.assertEqual(local(report)['role'], 'PRIMARY')
        self.assertNotIn('primary_count', ids(report))

    def test_missing_instance_recording(self):
        """Live: STOP GROUP_REPLICATION on mf-ic-3, read from the primary."""
        report = F.report('ic-8.4-member-offline')
        missing = next(x for x in report['members'] if x['state'] == 'MISSING')
        # One scale: MISSING while quorum holds is watch, like a Galera node that left.
        self.assertEqual((missing['name'], missing['status'], missing['state_source']),
                         ('mf-ic-3:3306', 'watch', 'metadata'))
        self.assertEqual((report['cluster']['size'], report['cluster']['expected_size']), (2, 3))
        self.assertNotIn('size_below_configured', ids(report))
        self.assertEqual(sorted(ids(report)), ['member_state', 'no_tolerance'])
        self.assertEqual(report['status'], 'watch')
        self.assertEqual(report['summary'], {'members': 3, 'ok': 2, 'watch': 1, 'problem': 0, 'unknown': 0})
        # The metadata instance not in the group is named.
        self.assertEqual(next(f for f in report['findings'] if f['id'] == 'member_state')['title'],
                         'mf-ic-3:3306 is MISSING')

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
        # It lists only itself (gcs_event_handlers.cc:1017-1042): no group size.
        self.assertEqual((report['cluster']['size'], report['cluster']['expected_size']), (None, 3))
        self.assertIsNone(report['cluster']['failures_tolerated'])
        text = m.format_text(report)
        self.assertNotIn('size 1', text)
        self.assertIn('group not visible from this member (3 in the metadata)', text)
        others = [x for x in report['members'] if not x['local']]
        self.assertEqual(len(others), 2)
        for x in others:
            self.assertIsNone(x['state'])
            self.assertEqual(x['status'], 'unknown')
            # About the connected member, never the instance shown.
            self.assertIn('The connected member (mf-ic-3:3306) is OFFLINE', x['status_why'][0])
            self.assertNotIn('This member is not in the group', x['status_why'][0])
        # The metadata (and the router check-in) is this member's frozen copy.
        self.assertEqual(report['metadata']['local_copy'], 'left_group')
        self.assertTrue(report['routers'][0]['from_local_copy'])
        self.assertIn("this member's metadata copy, possibly out of date", text)
        self.assertFalse(F.report('ic-8.4-healthy')['routers'][0]['from_local_copy'])

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
        # Seen from a group member, quorum was measured, so OK says so.
        self.assertTrue(report['cluster']['quorum'])
        self.assertEqual(report['status_why'], ['Every judged member is healthy and the cluster has quorum.'])

    def test_connected_to_a_read_replica(self):
        """A read replica has no group of its own (Sh add_replica_instance.cc
        never installs the plugin): found by @@server_uuid in its replicated
        metadata, judged by its read_replica_replication channel."""
        conn = F.conn('ic-8.4-read-replica-local')
        report = m.read_status(conn)
        self.assertEqual((report['kind'], report['status'], report['cluster']['name']), ('innodb_cluster', 'ok', 'mfcluster'))
        me = local(report)
        self.assertEqual((me['role'], me['state'], me['state_source'], me['instance_type']),
                         ('READ_REPLICA', 'Replicating', 'replication_channel', 'read-replica'))
        self.assertEqual((report['cluster']['size'], report['cluster']['expected_size'], report['cluster']['quorum']),
                         (None, 3, None))
        group = [x for x in report['members'] if not x['local']]
        self.assertEqual(len(group), 3)
        for x in group:
            self.assertEqual((x['state'], x['status']), (None, 'unknown'))
        self.assertEqual(report['metadata']['local_copy'], 'read_replica')
        self.assertTrue(any("WHERE CHANNEL_NAME = 'read_replica_replication'" in sql for sql, _ in conn.calls))
        self.assertIn('this server is a read replica', m.format_text(report))
        # Codex review: a read replica never claims quorum it cannot see.
        # Quorum and failures tolerated stay unknown, and OK says why.
        self.assertIsNone(report['cluster']['failures_tolerated'])
        self.assertEqual(len(report['status_why']), 1)
        self.assertNotIn('has quorum', report['status_why'][0])
        self.assertIn('This read replica is replicating', report['status_why'][0])
        self.assertIn("quorum and the failures it tolerates are not visible here", report['status_why'][0])
        self.assertIn('quorum and failures tolerated unknown (not a group member)', m.format_text(report))
        self.assertNotIn('quorum yes', m.format_text(report))
        stopped = F.report('ic-8.4-read-replica-stopped')
        self.assertEqual((stopped['status'], ids(stopped)), ('problem', ['read_replica']))
        self.assertIn('not running (receiver OFF, applier ON)', stopped['findings'][0]['text'])
        self.assertIn('2003', stopped['findings'][0]['text'])
        # Retrying (CONNECTING with its last error) is watch, never Error.
        connecting = F.report('ic-8.4-read-replica-connecting')
        self.assertEqual((connecting['status'], local(connecting)['state']), ('watch', 'Connecting'))
        # A table that cannot be read is unknown, never "stopped".
        denied = F.report('ic-8.4-read-replica-applier-denied')
        self.assertEqual((denied['status'], local(denied)['status'], local(denied)['state']), ('unknown', 'unknown', None))
        self.assertEqual(ids(denied), [])
        # Not in the metadata: still not part of a cluster.
        self.assertEqual(F.report('mysql-8.4-standalone')['kind'], 'none')

    def test_ipv6_member_address(self):
        """Codex review: MEMBER_HOST 2001:db8::1 and MEMBER_PORT 3306 stay
        separate; the address brackets the host."""
        report = F.report('gr-8.4-ipv6')
        me = local(report)
        self.assertEqual((me['host'], me['port'], me['address']), ('2001:db8::1', 3306, '[2001:db8::1]:3306'))
        self.assertIn('[2001:db8::2]:3306', m.format_text(report))

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
        # A name that equals the address is printed once; this connection in words.
        self.assertNotIn('mf-ic-1:3306  mf-ic-1:3306', out)
        self.assertIn('(this connection)', out)
        # Router check-in: its age and UTC, never a bare server-time stamp.
        self.assertRegex(out, r'last check-in \d+ s ago \(2026-10-20 \d\d:\d\d:\d\d UTC\)')
        self.assertEqual(m.age_text(-3), '0 s ago')
        self.assertEqual(m.age_text(150), '2 min ago')
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



class TestRecorder(unittest.TestCase):
    """scripts/record-cluster-fixtures.py, which makes the recordings above:
    committed, named by the fixture helper, password from the matrix's
    credentials file only (never argv, never printed), never the real home."""

    def load(self):
        import importlib.util
        path = ROOT / 'scripts' / 'record-cluster-fixtures.py'
        self.assertTrue(path.is_file())
        spec = importlib.util.spec_from_file_location('record_cluster_fixtures', str(path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_recorder_is_committed_and_named_by_the_fixtures(self):
        self.assertIn('scripts/record-cluster-fixtures.py', F.__doc__)
        self.assertNotIn('scratchpad', F.__doc__)
        self.assertIn('record-cluster-fixtures.py', (ROOT / 'test' / 'README.md').read_text(encoding='utf-8'))
        self.assertNotIn('--password', (ROOT / 'scripts' / 'record-cluster-fixtures.py').read_text(encoding='utf-8'))

    def test_password_comes_from_the_credentials_file(self):
        rec = self.load()
        with tempfile.TemporaryDirectory() as lab:
            creds = os.path.join(lab, 'credentials.env')
            with open(creds, 'w', encoding='utf-8') as handle:
                handle.write('ROOT_PASSWORD=root-secret\nMF_ADMIN_PASSWORD="admin-secret"\n')
            with mock.patch.dict(os.environ, {'CLUSTER_LAB': lab}):
                os.environ.pop('CREDS_FILE', None)
                self.assertEqual(rec.credentials_file(), creds)
                self.assertEqual(rec.read_password('root'), 'root-secret')
                self.assertEqual(rec.read_password('mf_admin'), 'admin-secret')
                err = io.StringIO()
                with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as stop:
                    rec.read_password('mf_read')
                self.assertEqual(stop.exception.code, 2)
                self.assertNotIn('secret', err.getvalue())
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    rec.read_password('someone')
            with mock.patch.dict(os.environ, {'CREDS_FILE': creds, 'CLUSTER_LAB': '/nonexistent'}):
                self.assertEqual(rec.credentials_file(), creds)

    def test_refuses_the_real_home(self):
        rec = self.load()
        if rec.pwd is None:
            self.skipTest('Unix only')
        real = rec.pwd.getpwuid(os.getuid()).pw_dir
        for home in (real, os.path.join(real, '.myflames'), os.path.join(real, '.myflames', 'lab')):
            with self.subTest(home=home), mock.patch.dict(os.environ, {'HOME': home}):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stop:
                    rec.refuse_real_home()
                self.assertEqual(stop.exception.code, 2)
        with tempfile.TemporaryDirectory() as isolated, mock.patch.dict(os.environ, {'HOME': isolated}):
            rec.refuse_real_home()


if __name__ == '__main__':
    unittest.main()
