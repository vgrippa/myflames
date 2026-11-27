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
import copy
import importlib.util
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
from _perf_replay import FakeFactory, assert_read_only as _assert_read_only, no_nan, run_cli, run_main
from myflames import cluster as m, ui
from myflames.connector import ConnectorError

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / 'docs' / 'schemas' / 'cluster-1.0.json').read_text(encoding='utf-8'))


def install_clusterset_batch_replay():
    """Replay multiple result sets from a single synthetic mysql login.

    Archived cluster fixtures predate the same-login ClusterSet identity and
    fencing reads. Keep those recordings unchanged, while making the synthetic
    connection honor the statements in the batch and fail as the client does
    when any of those statements fails.
    """
    original = F.ScenarioConn._answer
    if getattr(original, '_clusterset_batch_replay', False):
        return

    def answer(conn, body):
        markers = ('__myflames_clusterset_version__', '__myflames_clusterset_membership__',
                   '__myflames_clusterset_instances__', '__myflames_clusterset_groups__',
                   '__myflames_clusterset_routers__')
        for marker in markers:
            if body.strip() == "SELECT '%s'" % marker:
                return F.batch([[marker]])
        if body.strip().lower() == 'select @@server_uuid':
            return F.batch([[conn.scenario['variables'].get('server_uuid')]])
        identity, sep, rest = body.partition(';')
        if sep and identity.strip().lower() == 'select @@global.server_uuid':
            uuid = conn.scenario['variables'].get('server_uuid')
            return F.batch([[uuid]]) + answer(conn, rest.strip())
        statements = [statement.strip() for statement in body.split(';')]
        if (statements[0].upper() == 'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'
                and statements[1].upper() == 'START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY'
                and statements[-1].upper() == 'COMMIT'):
            # START and COMMIT produce no result rows. Each SELECT still uses
            # the individual fixture answer, in statement order.
            output = []
            for index, statement in enumerate(statements[2:-1], start=2):
                try:
                    output.append(answer(conn, statement))
                except ConnectorError as exc:
                    if statement == m._CS_ROUTERS_SQL and 'ERROR 1142' in str(exc) and 'at line' not in str(exc):
                        raise ConnectorError('%s at line %d' % (exc, index + 1))
                    raise
            return ''.join(output)
        if statements == [m._MD_VERSION_SQL, m._MD_CLUSTERSET_SQL, m._MD_INSTANCES_SQL, m._MD_CLUSTERS_SQL]:
            return ''.join(original(conn, statement) for statement in statements)
        variables, sep, action = body.partition(';')
        if (sep and variables.strip() == "SHOW GLOBAL VARIABLES WHERE Variable_name IN ('super_read_only')"
                and action.strip() == "SELECT enabled FROM performance_schema.replication_group_member_actions "
                                      "WHERE name = 'mysql_disable_super_read_only_if_primary'"):
            return original(conn, variables.strip()) + original(conn, action.strip())
        return original(conn, body)

    answer._clusterset_batch_replay = True
    F.ScenarioConn._answer = answer


install_clusterset_batch_replay()


def assert_cluster_read_only(case, conn, tag):
    """Permit the bound identity and read-only snapshot while checking facts."""
    prefix = tag + 'SELECT @@GLOBAL.server_uuid; '
    checked = copy.copy(conn)
    checked.calls = []
    for sql, flags in conn.calls:
        case.assertTrue(sql.startswith(tag), sql)
        body = sql[len(prefix):] if sql.startswith(prefix) else sql[len(tag):]
        statements = [statement.strip() for statement in body.split(';')]
        if statements[0].upper() == 'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ':
            case.assertEqual(statements[1].upper(), 'START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY', sql)
            case.assertEqual(statements[-1].upper(), 'COMMIT', sql)
            statements = statements[2:-1]
        checked.calls.extend((statement if statement.startswith(tag) else tag + statement, flags)
                             for statement in statements if statement)
    _assert_read_only(case, checked, tag)


assert_read_only = assert_cluster_read_only


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
                # Historical recordings retain their original envelope version.
                expected['schema_version'] = m.SCHEMA_VERSION
                expected['connection_group'] = {'name': '', 'read': [], 'suggestions': [], 'join': [],
                    'suggested_name': expected['cluster']['name'] if expected['kind'] in ('innodb_cluster', 'galera', 'pxc') else ''}
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


class TestClusterSetBrowserFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = ROOT / 'apps' / 'workspace' / 'test' / 'cluster-fixtures.py'
        spec = importlib.util.spec_from_file_location('cluster_browser_fixtures', str(path))
        cls.fixtures = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.fixtures)

    def test_fenced_primary_fixture_reads_fenced_writes(self):
        name = 'ic-8.4-clusterset-primary-fenced'
        source = self.fixtures.clusterset_source(name)
        self.assertEqual(source.scenario['variables']['super_read_only'], 'ON')
        self.assertEqual(source.scenario['tables'][
            'performance_schema.replication_group_member_actions'], [[0]])
        self.assertEqual(source.scenario['tables'][
            'performance_schema.replication_connection_status'], [])
        report = self.fixtures.clusterset_report(name)
        primary, replica = report['clusterset']['clusters']
        self.assertEqual(primary['role'], 'PRIMARY')
        self.assertEqual(primary['status'], 'FENCED_WRITES')
        self.assertTrue(primary['fenced_writes'])
        self.assertTrue(primary['quorum'])
        self.assertIn('super_read_only ON', primary['status_reason'])
        self.assertEqual(replica['status'], 'OK')
        self.assertEqual(report['clusterset']['status'], 'UNAVAILABLE')
        self.assertEqual(report['clusterset']['status_reason'], 'Primary Cluster is fenced from write traffic.')

    def test_no_quorum_primary_fixture_reads_not_ok(self):
        name = 'ic-8.4-clusterset-primary-no-quorum'
        source = self.fixtures.clusterset_source(name)
        rows = source.scenario['tables']['performance_schema.replication_group_members']
        self.assertEqual([row[4] for row in rows], ['ONLINE', 'UNREACHABLE', 'UNREACHABLE'])
        report = self.fixtures.clusterset_report(name)
        primary, replica = report['clusterset']['clusters']
        self.assertEqual(primary['role'], 'PRIMARY')
        self.assertEqual(primary['status'], 'NOT_OK')
        self.assertFalse(primary['quorum'])
        self.assertIn('reachable majority', primary['status_reason'])
        self.assertEqual(replica['status'], 'OK')
        self.assertEqual(report['clusterset']['status'], 'UNAVAILABLE')


class TestClusterSetReadModel(unittest.TestCase):
    CURRENT = {'id': 'local', 'name': 'Local member', 'group': 'production'}
    REMOTE = {'id': 'remote', 'name': 'DR member', 'group': 'production'}
    MEMBER_ACTIONS = 'performance_schema.replication_group_member_actions'

    def with_member_action(self, conn):
        """Supply the new SQL answer without rewriting archived fixtures."""
        if isinstance(conn, F.ScenarioConn):
            conn.scenario['tables'].setdefault(self.MEMBER_ACTIONS, [[0]])
            uuid = conn.scenario.get('variables', {}).get('server_uuid')
            cs_rows = conn.scenario.get('tables', {}).get('mysql_innodb_cluster_metadata.v2_cs_members')
            instances = conn.scenario.get('tables', {}).get('mysql_innodb_cluster_metadata.v2_instances')
            if isinstance(cs_rows, list) and isinstance(instances, list):
                primary_cids = {row[1] for row in cs_rows if len(row) > 4 and row[3] == 'PRIMARY' and row[4] == '0'}
                if any(len(inst) > 3 and inst[1] in primary_cids and inst[3] == uuid for inst in instances):
                    extra = conn.scenario.get('extra')
                    if isinstance(extra, dict) and extra.get('performance_schema.replication_connection_status') == [
                            ['ON', '0', '']]:
                        extra['performance_schema.replication_connection_status'] = []
                    elif extra is None:
                        conn.scenario['tables'].setdefault('performance_schema.replication_connection_status', [])
        return conn

    def read_group(self, peer, profiles=None, source=None):
        opened = []
        self.with_member_action(peer)
        source = self.with_member_action(source or F.conn('ic-8.4-clusterset-primary'))
        if profiles is None:
            # Ordinary routing is intentionally limited to preclassified
            # group profiles. Most route tests exercise what happens after a
            # route is selected, so give this fixture a prior UUID hint for
            # the intended peer. Explicit-profile tests control their own
            # classification and test the boundary separately.
            instances = source.scenario['tables']['mysql_innodb_cluster_metadata.v2_instances']
            cs_rows = source.scenario['tables']['mysql_innodb_cluster_metadata.v2_cs_members']
            local_uuid = source.scenario['variables'].get('server_uuid')
            local_cid = next((row[1] for row in instances if row[3] == local_uuid), None)
            remote_cids = {row[1] for row in cs_rows if row[1] != local_cid}
            remote_instances = [row for row in instances if row[1] in remote_cids]
            peer_uuid = (peer.scenario['variables'].get('server_uuid')
                         if isinstance(peer, F.ScenarioConn) else None)
            hint = next((row for row in remote_instances if row[3] == peer_uuid), None)
            hint = hint or (remote_instances[0] if remote_instances else None)
            remote = dict(self.REMOTE)
            if hint:
                remote['topology_identity'] = {'server_uuid': hint[3]}
                remote['host'], remote['port'] = m._split_address(hint[5])
            profiles = [self.CURRENT, remote]

        def opener(profile):
            opened.append(profile['id'])
            if isinstance(peer, Exception):
                raise peer
            return peer

        report = m.read_status(source,
                               profiles=profiles, open_profile=opener,
                               current_profile_id='local')
        return report, opened

    def read_peers(self, peers, profiles, source=None):
        """Give each saved connection its own identity and query answers."""
        opened = []
        source = self.with_member_action(source or F.conn('ic-8.4-clusterset-primary'))
        for peer in peers.values():
            self.with_member_action(peer)

        def opener(profile):
            opened.append(profile['id'])
            peer = peers[profile['id']]
            if isinstance(peer, Exception):
                raise peer
            return peer

        report = m.read_status(source, profiles=profiles,
                               open_profile=opener, current_profile_id='local')
        return report, opened

    def dr_profile(self, profile_id, member_index=None, **kwargs):
        profile = dict(self.REMOTE, id=profile_id, name='DR ' + profile_id, **kwargs)
        if member_index is not None:
            profile['topology_identity'] = {
                'server_uuid': F._cs_instances('cid-2', 'mf-dr', 30)[member_index][3]}
        return profile

    def test_routes_require_uuid_read_now_and_close_the_connection(self):
        peer = F.clusterset_peer()
        report, opened = self.read_group(peer)
        clusters = {c['name']: c for c in report['clusterset']['clusters']}
        self.assertEqual(clusters['mfcluster']['route'],
                         {'profile_id': 'local', 'profile_name': 'Local member', 'self': True,
                          'server_uuid': F.SERVER_UUIDS[0]})
        self.assertEqual(clusters['drcluster']['route'],
                         {'profile_id': 'remote', 'profile_name': 'DR member', 'self': False,
                          'server_uuid': F._cs_instances('cid-2', 'mf-dr', 30)[0][3]})
        self.assertEqual(opened, ['remote'])
        self.assertTrue(peer.closed)
        self.assertEqual(sum("Variable_name IN ('server_uuid')" in sql for sql, _ in peer.calls), 1)
        self.assertTrue(all(t is not None and 0 < t <= m.CLUSTERSET_ROUTE_TIMEOUT for t in peer.timeouts))
        assert_read_only(self, peer, m.CLUSTER_TAG)

    def test_initial_metadata_shares_one_uuid_guarded_client_login(self):
        source = F.conn('ic-8.4-clusterset-primary')
        report = m.read_status(source)
        self.assertTrue(report.get('clusterset'))
        prefix = m.CLUSTER_TAG + 'SELECT @@GLOBAL.server_uuid; '
        metadata_calls = [sql for sql, _ in source.calls
                          if sql.startswith(prefix) and m._MD_VERSION_SQL in sql]
        self.assertEqual(len(metadata_calls), 1)
        statements = [statement.strip() for statement in metadata_calls[0][len(prefix):].split(';')]
        self.assertEqual(statements[:2], [
            'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ',
            'START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY'])
        self.assertEqual(statements[-1], 'COMMIT')
        for _, projection, _ in m._CLUSTERSET_SNAPSHOT_QUERIES:
            self.assertEqual(statements.count(projection), 1, projection)

    def test_discovery_does_not_depend_on_an_earlier_unguarded_empty_view(self):
        source = F.conn('ic-8.4-clusterset-primary')
        run_original = source.run
        early_reads = []

        def run(sql, **kwargs):
            if m._MD_CLUSTERSET_SQL in sql and '@@GLOBAL.server_uuid' not in sql:
                # An earlier session can see an older metadata view.
                early_reads.append(sql)
                source.calls.append((sql, list(kwargs.get('extra_flags') or ())))
                return ''
            return run_original(sql, **kwargs)

        source.run = run
        report = m.read_status(source)
        self.assertEqual(report['clusterset']['name'], 'mfcs')
        guarded = [sql for sql, _ in source.calls
                   if '@@GLOBAL.server_uuid' in sql and m._MD_CLUSTERSET_SQL in sql]
        self.assertEqual(len(guarded), 1)

    def test_discovery_recovers_when_an_earlier_read_misses_local_metadata(self):
        cases = (('ic-8.4-clusterset-primary', m._MD_CLUSTERS_SQL),
                 ('ic-8.4-read-replica-clusterset', m._MD_INSTANCES_SQL),
                 ('ic-8.4-read-replica-clusterset', m._MD_CLUSTERS_SQL))
        for scenario, missing_sql in cases:
            with self.subTest(scenario=scenario, missing=missing_sql):
                source = F.conn(scenario)
                run_original = source.run

                def run(sql, **kwargs):
                    if sql == m.CLUSTER_TAG + missing_sql:
                        source.calls.append((sql, list(kwargs.get('extra_flags') or ())))
                        return ''
                    return run_original(sql, **kwargs)

                source.run = run
                report = m.read_status(source)
                self.assertEqual(report['kind'], 'innodb_cluster')
                self.assertEqual(report['clusterset']['name'], 'mfcs')
                guarded = [sql for sql, _ in source.calls
                           if '@@GLOBAL.server_uuid' in sql and m._MD_CLUSTERSET_SQL in sql]
                self.assertEqual(len(guarded), 1)
                if scenario == 'ic-8.4-read-replica-clusterset':
                    local_member = next(member for member in report['members'] if member['local'])
                    self.assertEqual(local_member['uuid'], F.RR_UUID)
                    self.assertEqual(local_member['instance_type'], 'read-replica')

    def test_discovery_with_performance_schema_off_does_not_invent_no_quorum(self):
        source = F.conn('ic-8.4-clusterset-primary')
        source.scenario['variables']['performance_schema'] = 'OFF'
        source.scenario['tables']['performance_schema.replication_group_members'] = []
        report = m.read_status(source)
        self.assertEqual(report['kind'], 'innodb_cluster')
        self.assertEqual(report['clusterset']['name'], 'mfcs')
        self.assertIsNone(report['cluster']['quorum'])
        self.assertNotIn('no_quorum', [finding['id'] for finding in report['findings']])
        local = next(item for item in report['clusterset']['clusters'] if item['local'])
        self.assertIsNone(local['quorum'])

    def test_discovery_recovers_a_schema_hidden_from_information_schema(self):
        source = F.conn('ic-8.4-clusterset-primary')
        source.scenario['tables']['information_schema.SCHEMATA'] = []
        report = m.read_status(source)
        self.assertEqual(report['kind'], 'innodb_cluster')
        self.assertEqual(report['clusterset']['name'], 'mfcs')
        version_reads = [sql for sql, _ in source.calls if '@@GLOBAL.server_uuid' in sql and m._MD_VERSION_SQL in sql]
        self.assertTrue(version_reads)

    def test_earlier_local_inventory_must_agree_with_guarded_snapshot(self):
        for corruption in ('missing', 'extra', 'label', 'endpoint', 'instance_type'):
            with self.subTest(corruption=corruption):
                source = F.conn('ic-8.4-clusterset-primary')
                run_original = source.run
                changed = []

                def run(sql, **kwargs):
                    result = run_original(sql, **kwargs)
                    if sql == m.CLUSTER_TAG + m._MD_INSTANCES_SQL:
                        rows = F.parse(result)
                        index = next(i for i, row in enumerate(rows) if row[3] == F.SERVER_UUIDS[1])
                        if corruption == 'missing':
                            del rows[index]
                        elif corruption == 'extra':
                            row = list(rows[index])
                            row[0] = '999'
                            row[3] = 'aaaaaaaa-0000-4000-8000-000000000099'
                            rows.append(row)
                        elif corruption == 'label':
                            rows[index][2] = 'untrusted-label:3306'
                        elif corruption == 'endpoint':
                            rows[index][5] = 'untrusted-host:3306'
                        else:
                            rows[index][6] = 'read-replica'
                        changed.append(True)
                        return F.batch(rows)
                    return result

                source.run = run
                with self.assertRaisesRegex(ValueError, 'ClusterSet metadata.*verified'):
                    m.read_status(source)
                self.assertEqual(changed, [True])

    def test_unreadable_clusterset_membership_disables_cluster_changes(self):
        for denied in ('membership', 'hidden_schema_version'):
            with self.subTest(denied=denied):
                source = F.conn('ic-8.4-clusterset-primary')
                if denied == 'membership':
                    source.scenario['tables']['mysql_innodb_cluster_metadata.v2_cs_members'] = ConnectorError(
                        'ERROR 1142 (42000): SELECT denied for v2_cs_members')
                else:
                    source.scenario['tables']['information_schema.SCHEMATA'] = []
                    source.scenario['tables']['mysql_innodb_cluster_metadata.schema_version'] = ConnectorError(
                        'ERROR 1142 (42000): SELECT denied for schema_version')
                report = m.read_status(source)
                self.assertFalse(report.get('clusterset'))
                self.assertTrue(getattr(report, '_clusterset_discovery_error', False))
                own = next(member for member in report['members'] if member['local'])
                actions = {action['id']: action for action in m.available_actions(report, own['key'])}
                self.assertFalse(actions['stop_member']['enabled'])
                self.assertIn('ClusterSet membership could not be verified', actions['stop_member']['why_not'])
                with self.assertRaisesRegex(ValueError, 'ClusterSet membership could not be verified'):
                    m.plan_action(report, 'stop_member', own['key'])

    def test_primary_refresh_reads_related_metadata_in_one_read_only_snapshot(self):
        source = F.conn('ic-8.4-clusterset-replica')
        primary = F.clusterset_peer('cid-0', 'mf-main', 20, 'ic-8.4-clusterset-replica')
        profile = dict(self.REMOTE, topology_identity={
            'server_uuid': primary.scenario['variables']['server_uuid']})
        report, opened = self.read_group(primary, [self.CURRENT, profile], source=source)
        self.assertEqual(opened, ['remote'])
        self.assertFalse(report['clusterset']['from_local_copy'])
        metadata_calls = [sql for sql, _ in primary.calls if m._MD_CLUSTERSET_SQL in sql]
        self.assertEqual(len(metadata_calls), 1)
        sql = metadata_calls[0]
        self.assertTrue(sql.startswith(m.CLUSTER_TAG + 'SELECT @@GLOBAL.server_uuid; '), sql)
        statements = [statement.strip() for statement in sql.split(';')]
        self.assertEqual(statements[1], 'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
        self.assertEqual(statements[2], 'START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
        self.assertEqual(statements[-1], 'COMMIT')
        for _, projection, _ in m._CLUSTERSET_SNAPSHOT_QUERIES[1:]:
            self.assertEqual(statements[3:-1].count(projection), 1, projection)

    def test_cluster_group_uuids_map_router_targets_without_guessing(self):
        table = 'mysql_innodb_cluster_metadata.v2_gr_clusters'
        membership = 'mysql_innodb_cluster_metadata.v2_cs_members'
        source = F.conn('ic-8.4-clusterset-primary')
        rows = source.scenario['tables'][table]
        expected = {row[1]: next(group[3] for group in rows if group[0] == row[1])
                    for row in source.scenario['tables'][membership]}
        report = m.read_status(source)
        for item in report['clusterset']['clusters']:
            self.assertEqual(item['uuid'], expected[report._clusterset_ids[item['name']]])

        for problem in ('missing', 'duplicate', 'malformed'):
            with self.subTest(problem=problem):
                source = F.conn('ic-8.4-clusterset-primary')
                rows = source.scenario['tables'][table]
                remote = next(row for row in rows if row[0] == 'cid-2')
                if problem == 'missing':
                    rows.remove(remote)
                elif problem == 'duplicate':
                    remote[3] = next(row[3] for row in rows if row[0] == 'cid-1')
                else:
                    remote[3] = 'not-a-uuid'
                clusters = m.read_status(source)['clusterset']['clusters']
                self.assertIsNone(next(item for item in clusters if item['name'] == 'drcluster')['uuid'])
                if problem == 'duplicate':
                    self.assertIsNone(next(item for item in clusters if item['local'])['uuid'])

    def test_primary_refresh_publishes_the_new_group_uuid(self):
        source = F.conn('ic-8.4-clusterset-three')
        primary = F.clusterset_peer('cid-0', 'mf-main', 20, 'ic-8.4-clusterset-three')
        groups = primary.scenario['tables']['mysql_innodb_cluster_metadata.v2_gr_clusters']
        group = next(row for row in groups if row[0] == 'cid-2')
        group[3] = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
        report, opened = self.read_group(primary, source=source)
        self.assertEqual(opened, ['remote'])
        self.assertFalse(report['clusterset']['from_local_copy'])
        self.assertEqual(next(c for c in report['clusterset']['clusters'] if c['name'] == 'drcluster')['uuid'],
                         group[3])

    def test_primary_refresh_reconciles_member_inventory_and_live_evidence(self):
        table = 'mysql_innodb_cluster_metadata.v2_instances'
        group_table = 'performance_schema.replication_group_members'
        for removed_live in (False, True):
            with self.subTest(removed_live=removed_live):
                source = F.conn('ic-8.4-clusterset-replica')
                primary = F.clusterset_peer('cid-0', 'mf-main', 20, 'ic-8.4-clusterset-replica')
                if not removed_live:
                    primary.scenario['tables'][group_table] = primary.scenario['tables'][group_table][:1]
                rows = primary.scenario['tables'][table]
                removed = F._cs_instances('cid-0', 'mf-main', 20)[2]
                rows[:] = [row for row in rows if row[3] != removed[3]]
                added = F._cs_instances('cid-0', 'mf-new', 23)[0]
                rows.append(added)
                current = next(row for row in rows if row[3] == primary.scenario['variables']['server_uuid'])
                current[2] = 'new-label'
                current[4] = current[5] = 'new-primary.example:3311'
                report, opened = self.read_group(primary, source=source)
                self.assertEqual(opened, ['remote'])
                item = next(c for c in report['clusterset']['clusters'] if c['role'] == 'PRIMARY')
                by_uuid = {member['server_uuid']: member for member in item['members']}
                self.assertNotIn(removed[3], by_uuid)
                self.assertIn(added[3], by_uuid)
                self.assertIsNone(by_uuid[added[3]]['state'])
                self.assertEqual(by_uuid[current[3]]['label'], 'new-label')
                self.assertEqual(by_uuid[current[3]]['address'], 'new-primary.example:3311')
                if removed_live:
                    self.assertEqual(item['read'], 'failed')
                    self.assertIsNone(item['primary'])
                    self.assertIsNone(item['quorum'])
                    self.assertIn('inventory changed', item['read_error'])
                else:
                    self.assertEqual(item['primary']['server_uuid'], current[3])
                    self.assertEqual(item['primary']['address'], 'new-primary.example:3311')
                    self.assertTrue(item['quorum'])

    def test_primary_refresh_clears_a_previously_read_removed_primary(self):
        source = self.with_member_action(F.conn('ic-8.4-clusterset-primary'))
        identity = {}
        report = m.read_status(source, _identity=identity)
        cs = report['clusterset']
        # Exercise the internal refresh before grouped_read strips this ID.
        cs['id'] = identity['clusterset_id']
        primary = next(item for item in cs['clusters'] if item['local'])
        replica = next(item for item in cs['clusters'] if not item['local'])
        members = replica['members']
        members[0].update(state='ONLINE', role='PRIMARY')
        members[1].update(state='ONLINE', role='SECONDARY')
        replica.update(route={'self': False, 'profile_id': 'dr', 'server_uuid': members[1]['server_uuid']},
                       read='route', quorum=True, primary=members[0], super_read_only=True,
                       fenced_writes=True, channel={'present': True})
        old_primary = members[0]['server_uuid']
        route_uuid = members[1]['server_uuid']
        rows = source.scenario['tables']['mysql_innodb_cluster_metadata.v2_instances']
        rows[:] = [row for row in rows if row[3] != old_primary]
        reader = m._ClusterSetReader(source, server_uuid=report['server']['server_uuid'])
        m._refresh_clusterset_metadata(reader, report, primary, report['server']['server_uuid'],
                                       observed={'cid-2': route_uuid})
        refreshed = next(item for item in report['clusterset']['clusters'] if not item['local'])
        self.assertNotIn(old_primary, {member['server_uuid'] for member in refreshed['members']})
        self.assertEqual(refreshed['read'], 'failed')
        self.assertIsNone(refreshed['primary'])
        self.assertIsNone(refreshed['quorum'])
        self.assertIsNone(refreshed['fenced_writes'])
        self.assertIsNone(refreshed['channel'])

    def test_primary_refresh_retains_unknown_and_positive_reachability(self):
        source = self.with_member_action(F.conn('ic-8.4-clusterset-primary'))
        identity = {}
        report = m.read_status(source, _identity=identity)
        cs = report['clusterset']
        cs['id'] = identity['clusterset_id']
        primary = next(item for item in cs['clusters'] if item['local'])
        uuids = [row[3] for row in F._cs_instances('cid-2', 'mf-dr', 30)]
        report._clusterset_reachability = {'drcluster': [
            {'server_uuid': uuids[0], 'reachable': False},
            {'server_uuid': uuids[1], 'reachable': None},
            {'server_uuid': uuids[2], 'reachable': True}]}
        # Metadata UUID spelling can change case without changing identity.
        for row in source.scenario['tables']['mysql_innodb_cluster_metadata.v2_instances']:
            if row[3] in (uuids[1], uuids[2]):
                row[3] = row[3].upper()
        reader = m._ClusterSetReader(source, server_uuid=report['server']['server_uuid'])
        m._refresh_clusterset_metadata(reader, report, primary, report['server']['server_uuid'])
        self.assertCountEqual(report._clusterset_reachability['drcluster'], [
            {'server_uuid': uuids[1].upper(), 'reachable': None},
            {'server_uuid': uuids[2].upper(), 'reachable': True}])

    def test_primary_refresh_cannot_mix_membership_and_instance_versions(self):
        source = F.conn('ic-8.4-clusterset-replica')
        primary = F.clusterset_peer('cid-0', 'mf-main', 20, 'ic-8.4-clusterset-replica')
        memberships = primary.scenario['tables']['mysql_innodb_cluster_metadata.v2_cs_members']
        for row in memberships:
            row[6] = '4294967298'
        primary_uuid = primary.scenario['variables']['server_uuid']
        instance = next(row for row in primary.scenario['tables'][
            'mysql_innodb_cluster_metadata.v2_instances'] if row[3] == primary_uuid)
        profile = dict(self.REMOTE, topology_identity={'server_uuid': primary_uuid})
        run_original = primary.run
        updates = []

        def run(sql, **kwargs):
            result = run_original(sql, **kwargs)
            if m._MD_CLUSTERSET_SQL in sql and not updates:
                # Simulate a metadata transaction committing after this client
                # invocation. A single snapshot keeps the entire old view;
                # separate invocations would combine old roles and new hosts.
                for row in memberships:
                    row[6] = '4294967299'
                instance[4] = instance[5] = 'new-main.example:3311'
                updates.append(True)
            return result

        primary.run = run
        report, opened = self.read_group(primary, [self.CURRENT, profile], source=source)
        self.assertEqual(opened, ['remote'])
        self.assertEqual(updates, [True])
        self.assertEqual(report['clusterset']['view_id'], '4294967298')
        selected = next(item for item in report._clusterset_instances['cid-0']
                        if item['server_uuid'] == primary_uuid)
        self.assertEqual(selected['endpoint'], 'mf-main-1:3306')

    def test_primary_refresh_rejects_a_conflicting_local_inventory_or_group(self):
        cases = ('missing', 'extra', 'label', 'endpoint', 'instance_type', 'local_group', 'route_group')
        for corruption in cases:
            with self.subTest(corruption=corruption):
                source = F.conn('ic-8.4-clusterset-replica')
                primary = F.clusterset_peer('cid-0', 'mf-main', 20, 'ic-8.4-clusterset-replica')
                tables = primary.scenario['tables']
                for row in tables['mysql_innodb_cluster_metadata.v2_cs_members']:
                    row[6] = '4294967298'
                instances = tables['mysql_innodb_cluster_metadata.v2_instances']
                index = next(i for i, row in enumerate(instances) if row[3] == F.SERVER_UUIDS[1])
                if corruption == 'missing':
                    del instances[index]
                elif corruption == 'extra':
                    row = list(instances[index])
                    row[0] = 999
                    row[3] = 'aaaaaaaa-0000-4000-8000-000000000099'
                    instances.append(row)
                elif corruption == 'label':
                    instances[index][2] = 'untrusted-label:3306'
                elif corruption == 'endpoint':
                    instances[index][5] = 'untrusted-host:3306'
                elif corruption == 'instance_type':
                    instances[index][6] = 'read-replica'
                else:
                    cid = 'cid-1' if corruption == 'local_group' else 'cid-0'
                    group = next(row for row in tables['mysql_innodb_cluster_metadata.v2_gr_clusters']
                                 if row[0] == cid)
                    group[3] = 'ffffffff-ffff-4fff-afff-ffffffffffff'
                profile = dict(self.REMOTE, topology_identity={
                    'server_uuid': primary.scenario['variables']['server_uuid']})
                report, opened = self.read_group(primary, [self.CURRENT, profile], source=source)
                self.assertEqual(opened, ['remote'])
                self.assertTrue(any(m._MD_CLUSTERSET_SQL in sql for sql, _ in primary.calls))
                self.assertEqual(report['clusterset']['view_id'], '4294967297')
                self.assertTrue(report['clusterset']['from_local_copy'])
                self.assertTrue(report['clusterset']['metadata_read_on']['self'])
                self.assertTrue(any('replicated copy' in note for note in report['notes']))

    def test_unreadable_routers_retry_the_required_snapshot_without_another_open(self):
        source = F.conn('ic-8.4-clusterset-replica')
        primary = F.clusterset_peer('cid-0', 'mf-main', 20, 'ic-8.4-clusterset-replica')
        primary.scenario['clusterset_routers'] = ConnectorError('ERROR 1142: denied')
        for row in primary.scenario['tables']['mysql_innodb_cluster_metadata.v2_cs_members']:
            row[6] = '4294967298'
        profile = dict(self.REMOTE, topology_identity={
            'server_uuid': primary.scenario['variables']['server_uuid']})
        report, opened = self.read_group(primary, [self.CURRENT, profile], source=source)
        self.assertEqual(opened, ['remote'])
        self.assertEqual(report['clusterset']['view_id'], '4294967298')
        self.assertFalse(report['clusterset']['from_local_copy'])
        self.assertIsNone(report['clusterset']['routers'])
        reads = [sql for sql, _ in primary.calls if m._MD_CLUSTERSET_SQL in sql]
        self.assertEqual(len(reads), 2)
        for sql in reads:
            self.assertIn('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ', sql)
            self.assertIn('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY', sql)
            self.assertIn(m._MD_INSTANCES_SQL, sql)
            self.assertIn(m._MD_CLUSTERS_SQL, sql)
            self.assertTrue(sql.endswith('COMMIT'), sql)
        self.assertIn(m._CS_ROUTERS_SQL, reads[0])
        self.assertNotIn(m._CS_ROUTERS_SQL, reads[1])

    def test_other_snapshot_errors_do_not_retry_as_if_routers_were_denied(self):
        tables = (('mysql_innodb_cluster_metadata.schema_version', 4),
                  ('mysql_innodb_cluster_metadata.v2_cs_members', 6),
                  ('mysql_innodb_cluster_metadata.v2_instances', 8),
                  ('mysql_innodb_cluster_metadata.v2_gr_clusters', 10))
        for table, line in tables:
            with self.subTest(table=table):
                source = F.conn('ic-8.4-clusterset-replica')
                primary = F.clusterset_peer('cid-0', 'mf-main', 20, 'ic-8.4-clusterset-replica')
                primary.scenario['tables'][table] = ConnectorError(
                    'ERROR 1142 (42000) at line %d: SELECT denied' % line)
                profile = dict(self.REMOTE, topology_identity={
                    'server_uuid': primary.scenario['variables']['server_uuid']})
                report, opened = self.read_group(primary, [self.CURRENT, profile], source=source)
                self.assertEqual(opened, ['remote'])
                self.assertTrue(report['clusterset']['from_local_copy'])
                self.assertEqual(report['clusterset']['view_id'], '4294967297')
                snapshots = [sql for sql, _ in primary.calls
                             if 'START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY' in sql]
                self.assertEqual(len(snapshots), 1)
                self.assertIn(m._CS_ROUTERS_SQL, snapshots[0])

    def test_incomplete_or_out_of_order_snapshot_markers_are_refused(self):
        for corruption in ('omitted', 'out_of_order'):
            with self.subTest(corruption=corruption):
                source = F.conn('ic-8.4-clusterset-primary')
                run_original = source.run
                changed = []

                def run(sql, **kwargs):
                    result = run_original(sql, **kwargs)
                    if m._MD_CLUSTERSET_SQL in sql and '@@GLOBAL.server_uuid' in sql:
                        marker = '__myflames_clusterset_instances__\n'
                        self.assertIn(marker, result)
                        if corruption == 'omitted':
                            result = result.replace(marker, '', 1)
                        else:
                            result = result.replace(marker, '__myflames_clusterset_groups__\n', 1)
                        changed.append(True)
                    return result

                source.run = run
                with self.assertRaisesRegex(ValueError, 'ClusterSet metadata.*verified'):
                    m.read_status(source)
                self.assertEqual(changed, [True])

    def test_ambiguous_membership_is_refused_at_bootstrap_and_primary_refresh(self):
        cases = ('invalidation_2', 'invalidation_garbage', 'invalidation_null', 'two_primaries')
        for stage in ('bootstrap', 'refresh'):
            for case in cases:
                with self.subTest(stage=stage, case=case):
                    source = F.conn('ic-8.4-clusterset-primary' if stage == 'bootstrap'
                                    else 'ic-8.4-clusterset-replica')
                    selected = (source if stage == 'bootstrap' else
                                F.clusterset_peer('cid-0', 'mf-main', 20, 'ic-8.4-clusterset-replica'))
                    rows = selected.scenario['tables']['mysql_innodb_cluster_metadata.v2_cs_members']
                    if stage == 'refresh':
                        for row in rows:
                            row[6] = '4294967298'
                    replica = next(row for row in rows if row[3] == 'REPLICA')
                    if case == 'two_primaries':
                        replica[3] = 'PRIMARY'
                    else:
                        replica[4] = {'invalidation_2': '2', 'invalidation_garbage': 'garbage',
                                      'invalidation_null': None}[case]
                    if stage == 'bootstrap':
                        with self.assertRaisesRegex(ValueError, 'ClusterSet metadata.*verified'):
                            m.read_status(source)
                    else:
                        profile = dict(self.REMOTE, topology_identity={
                            'server_uuid': selected.scenario['variables']['server_uuid']})
                        report, opened = self.read_group(selected, [self.CURRENT, profile], source=source)
                        self.assertEqual(opened, ['remote'])
                        self.assertEqual(report['clusterset']['view_id'], '4294967297')
                        self.assertTrue(report['clusterset']['from_local_copy'])
                        self.assertTrue(report['clusterset']['metadata_read_on']['self'])

    def test_clusterset_route_logins_disable_automatic_reconnect(self):
        source, peer = F.conn('ic-8.4-clusterset-primary'), F.clusterset_peer()
        report, opened = self.read_group(peer, source=source)
        self.assertEqual(opened, ['remote'])
        self.assertEqual(report['clusterset']['clusters'][1]['read'], 'route')
        prefix = m.CLUSTER_TAG + 'SELECT @@GLOBAL.server_uuid; '
        identity_sql = m.CLUSTER_TAG + "SHOW GLOBAL VARIABLES WHERE Variable_name IN ('server_uuid')"
        identity = [(sql, flags) for sql, flags in peer.calls if sql == identity_sql]
        self.assertEqual(len(identity), 1, 'A route must start with its own identity-only login.')
        peer_facts = [(sql, flags) for sql, flags in peer.calls if sql.startswith(prefix)]
        source_facts = [(sql, flags) for sql, flags in source.calls if sql.startswith(prefix)]
        self.assertTrue(any('replication_group_members' in sql for sql, _ in peer_facts))
        self.assertTrue(any('replication_connection_status' in sql for sql, _ in peer_facts))
        self.assertTrue(any(m._MD_VERSION_SQL in sql and m._MD_CLUSTERSET_SQL in sql
                            for sql, _ in source_facts))
        for sql, flags in identity + peer_facts + source_facts:
            self.assertIn('--comments', flags, sql)
            self.assertIn('--skip-reconnect', flags, sql)

    def test_cleanup_reader_binds_uuid_and_stays_failed_after_server_switch(self):
        peer = F.clusterset_peer()
        reader = m._ClusterSetReader(peer)
        original_uuid = peer.scenario['variables']['server_uuid']
        self.assertEqual(reader.rows('SELECT @@server_uuid'), [[original_uuid]])
        peer.scenario['variables']['server_uuid'] = F._cs_instances('cid-2', 'mf-dr', 30)[1][3]
        sql = 'SELECT MEMBER_ID FROM performance_schema.replication_group_members'
        with self.assertRaises(ValueError):
            reader.rows(sql)
        peer.scenario['variables']['server_uuid'] = original_uuid
        with self.assertRaises(ValueError):
            reader.rows(sql)
        self.assertEqual(len(peer.calls), 2, 'A mixed reader must reject subsequent facts without another login.')
        for sql, flags in peer.calls:
            self.assertIn('--comments', flags, sql)
            self.assertIn('--skip-reconnect', flags, sql)

    def test_load_balanced_route_refuses_facts_from_another_server(self):
        # Every mysql client invocation is a new login. The identity probe and
        # group, fencing, and channel queries can therefore hit different
        # backends even when they use the same saved connection object.
        cases = (
            ('performance_schema.replication_group_members', 'group'),
            (self.MEMBER_ACTIONS, 'fencing'),
            ('performance_schema.replication_connection_status', 'channel'),
            ('performance_schema.replication_applier_status_by_worker', 'workers'),
        )
        for fragment, fact in cases:
            with self.subTest(fact=fact):
                peer = F.clusterset_peer()
                original = peer.run
                switched = []

                def run(sql, **kwargs):
                    if fragment in sql:
                        saved = peer.scenario['variables']['server_uuid']
                        peer.scenario['variables']['server_uuid'] = F._cs_instances('cid-2', 'mf-dr', 30)[1][3]
                        switched.append(sql)
                        try:
                            return original(sql, **kwargs)
                        finally:
                            peer.scenario['variables']['server_uuid'] = saved
                    return original(sql, **kwargs)

                peer.run = run
                report, opened = self.read_group(peer)
                dr = next(c for c in report['clusterset']['clusters'] if c['name'] == 'drcluster')
                self.assertEqual(opened, ['remote'])
                self.assertTrue(switched, 'The targeted fact query must actually execute.')
                self.assertTrue(all('@@GLOBAL.server_uuid' in sql for sql in switched))
                if fact == 'group':
                    self.assertIsNone(dr['quorum'])
                    self.assertIsNone(dr['primary'])
                elif fact == 'fencing':
                    self.assertIsNone(dr['super_read_only'])
                    self.assertIsNone(dr['fenced_writes'])
                elif fact == 'channel':
                    self.assertNotEqual((dr.get('channel') or {}).get('receiver_state'), 'ON')
                else:
                    self.assertNotIn('ON', [w.get('state') for w in (dr.get('channel') or {}).get('workers', [])])
                self.assertTrue(any('identity' in n.lower() or 'server_uuid' in n.lower()
                                    for n in report['notes']), report['notes'])

    def test_changed_identity_during_initial_clusterset_metadata_refuses_set_read(self):
        for table in ('schema_version', 'v2_cs_members', 'v2_instances', 'v2_gr_clusters'):
            with self.subTest(table=table):
                source = F.conn('ic-8.4-clusterset-primary')
                original = source.run
                switched = []

                def run(sql, **kwargs):
                    if '@@GLOBAL.server_uuid' in sql and 'mysql_innodb_cluster_metadata.' + table in sql:
                        saved = source.scenario['variables']['server_uuid']
                        source.scenario['variables']['server_uuid'] = F.SERVER_UUIDS[1]
                        switched.append(sql)
                        try:
                            return original(sql, **kwargs)
                        finally:
                            source.scenario['variables']['server_uuid'] = saved
                    return original(sql, **kwargs)

                source.run = run
                with self.assertRaisesRegex(ValueError, 'ClusterSet metadata.*verified'):
                    m.read_status(source)
                self.assertTrue(switched, 'The initial metadata must be checked in a UUID-bound login.')

    def test_bootstrap_rejects_invalid_schema_or_group_identity(self):
        for field in ('schema_version', 'group_uuid'):
            with self.subTest(field=field):
                source = F.conn('ic-8.4-clusterset-primary')
                original = source.run
                changed = []
                table = ('mysql_innodb_cluster_metadata.schema_version' if field == 'schema_version'
                         else 'mysql_innodb_cluster_metadata.v2_gr_clusters')

                def run(sql, **kwargs):
                    if '@@GLOBAL.server_uuid' in sql and table in sql:
                        saved = source.scenario['tables'][table]
                        rows = copy.deepcopy(saved)
                        if field == 'schema_version':
                            rows[0] = [0, 0, 0]
                        else:
                            local = next(row for row in rows if row[0] == 'cid-1')
                            local[3] = 'ffffffff-ffff-4fff-afff-ffffffffffff'
                        source.scenario['tables'][table] = rows
                        changed.append(sql)
                        try:
                            return original(sql, **kwargs)
                        finally:
                            source.scenario['tables'][table] = saved
                    return original(sql, **kwargs)

                source.run = run
                with self.assertRaisesRegex(ValueError, 'ClusterSet metadata.*verified'):
                    m.read_status(source)
                self.assertTrue(changed, 'The changed bootstrap fact must be read in a UUID-bound login.')

    def test_changed_identity_during_local_group_read_never_attributes_membership(self):
        source = F.conn('ic-8.4-clusterset-primary')
        original = source.run
        switched = []

        def run(sql, **kwargs):
            if '@@GLOBAL.server_uuid' in sql and 'performance_schema.replication_group_members' in sql:
                saved = source.scenario['variables']['server_uuid']
                source.scenario['variables']['server_uuid'] = F.SERVER_UUIDS[1]
                switched.append(sql)
                try:
                    return original(sql, **kwargs)
                finally:
                    source.scenario['variables']['server_uuid'] = saved
            return original(sql, **kwargs)

        source.run = run
        report = m.read_status(source)
        self.assertTrue(switched, 'Local group membership must be read in a UUID-bound login.')
        cs = report.get('clusterset')
        if cs:
            local = next(c for c in cs['clusters'] if c['local'])
            self.assertIsNone(local['quorum'])
            self.assertIsNone(local['primary'])
            self.assertIsNone(local['super_read_only'])
            self.assertTrue(cs['from_local_copy'])

    def test_wrong_uuid_with_an_empty_channel_result_is_not_a_missing_channel(self):
        peer = F.clusterset_peer()
        peer.scenario['extra']['performance_schema.replication_connection_status'] = []
        original = peer.run
        switched = []

        def run(sql, **kwargs):
            if 'performance_schema.replication_connection_status' in sql:
                saved = peer.scenario['variables']['server_uuid']
                peer.scenario['variables']['server_uuid'] = F._cs_instances('cid-2', 'mf-dr', 30)[1][3]
                switched.append(sql)
                try:
                    return original(sql, **kwargs)
                finally:
                    peer.scenario['variables']['server_uuid'] = saved
            return original(sql, **kwargs)

        peer.run = run
        report, _ = self.read_group(peer)
        self.assertTrue(switched)
        dr = next(c for c in report['clusterset']['clusters'] if c['name'] == 'drcluster')
        self.assertIsNot((dr.get('channel') or {}).get('present'), False)

    def test_changed_identity_during_primary_refresh_or_router_read_is_refused(self):
        for stage in ('metadata', 'routers'):
            with self.subTest(stage=stage):
                source = F.conn('ic-8.4-clusterset-replica')
                primary = F.clusterset_peer('cid-0', 'mf-main', 20, 'ic-8.4-clusterset-replica')
                for row in primary.scenario['tables']['mysql_innodb_cluster_metadata.v2_cs_members']:
                    row[6] = '4294967298'
                primary.scenario['clusterset_routers'] = [
                    [1, 'cs-1', None, 'wrong-backend', 'untrusted-router', '8.4.11', '{}']]
                original = primary.run
                switched = []
                fragment = ('mysql_innodb_cluster_metadata.v2_cs_members' if stage == 'metadata'
                            else 'SELECT r.router_id, r.clusterset_id')

                def run(sql, **kwargs):
                    if fragment in sql:
                        saved = primary.scenario['variables']['server_uuid']
                        primary.scenario['variables']['server_uuid'] = F._cs_instances('cid-0', 'mf-main', 20)[1][3]
                        switched.append(sql)
                        try:
                            return original(sql, **kwargs)
                        finally:
                            primary.scenario['variables']['server_uuid'] = saved
                    return original(sql, **kwargs)

                primary.run = run
                report, opened = self.read_group(primary, source=source)
                self.assertEqual(opened, ['remote'])
                self.assertTrue(switched)
                cs = report['clusterset']
                if stage == 'metadata':
                    self.assertEqual(cs['view_id'], '4294967297')
                    self.assertTrue(cs['from_local_copy'])
                    self.assertTrue(cs['metadata_read_on']['self'])
                else:
                    # The remote snapshot, including Routers, is staged
                    # before publication. Keep the prior local snapshot when
                    # its Router result comes from a different server.
                    self.assertEqual(cs['routers'], [])
                    self.assertEqual(cs['view_id'], '4294967297')
                    self.assertTrue(cs['from_local_copy'])
                    self.assertTrue(cs['metadata_read_on']['self'])

    def test_uuid_mismatch_does_not_trust_saved_identity_or_address(self):
        peer = F.clusterset_peer()
        peer.scenario['variables']['server_uuid'] = 'not-a-metadata-instance'
        remote = dict(self.REMOTE, host='mf-dr-1', topology_identity={
            'server_uuid': F._cs_instances('cid-2', 'mf-dr', 30)[0][3]})
        report, opened = self.read_group(peer, [self.CURRENT, remote])
        self.assertIsNone(report['clusterset']['clusters'][1]['route'])
        self.assertEqual(opened, ['remote'])

    def test_unreachable_group_member_is_not_a_route(self):
        report, opened = self.read_group(ConnectorError('ERROR 2003: connection refused'))
        self.assertIsNone(report['clusterset']['clusters'][1]['route'])
        self.assertEqual(opened, ['remote'])
        self.assertIn('could not be read', ' '.join(report['notes']))

    def test_spare_group_member_note_names_its_absence_from_clusterset(self):
        peer = F.clusterset_peer()
        peer.scenario['variables']['server_uuid'] = '00000099-2222-11f0-8a00-0242ac130002'
        # A stale prior identity can classify the route; the fresh read still
        # detects that it now points to a standalone server.
        spare = dict(self.REMOTE, name='cs-c1', topology_identity={
            'server_uuid': F._cs_instances('cid-2', 'mf-dr', 30)[0][3]})
        report, opened = self.read_group(peer, [self.CURRENT, spare])
        self.assertEqual(opened, ['remote'])
        self.assertIsNone(report['clusterset']['clusters'][1]['route'])
        self.assertIn('cs-c1 is not a member of any cluster in this ClusterSet (mfcs), '
                      'according to the metadata read here.', report['notes'])
        self.assertFalse(any('cs-c1 could not verify a route' in note for note in report['notes']))
        self.assertEqual(len(peer.calls), 1, 'A spare needs only the fresh identity read.')
        self.assertIn("Variable_name IN ('server_uuid')", peer.calls[0][0])
        self.assertTrue(peer.closed)
        assert_read_only(self, peer, m.CLUSTER_TAG)

    def test_missing_route_uuid_does_not_claim_non_membership(self):
        peer = F.clusterset_peer()
        peer.scenario['variables'].pop('server_uuid')
        report, opened = self.read_group(peer)
        self.assertEqual(opened, ['remote'])
        self.assertIsNone(report['clusterset']['clusters'][1]['route'])
        self.assertTrue(any('DR member could not verify a route' in note for note in report['notes']))
        self.assertFalse(any('is not a member of any cluster' in note for note in report['notes']))
        self.assertTrue(peer.closed)

    def test_ambiguous_metadata_uuid_does_not_claim_non_membership(self):
        source, peer = F.conn('ic-8.4-clusterset-primary'), F.clusterset_peer()
        duplicate = list(F._cs_instances('cid-2', 'mf-dr', 30)[0])
        duplicate[0], duplicate[1] = 99, 'cid-1'
        source.scenario['tables']['mysql_innodb_cluster_metadata.v2_instances'].append(duplicate)
        report, opened = self.read_group(peer, source=source)
        self.assertEqual(opened, ['remote'])
        self.assertIsNone(report['clusterset']['clusters'][1]['route'])
        self.assertTrue(any('a unique eligible metadata member' in note for note in report['notes']))
        self.assertFalse(any('is not a member of any cluster' in note for note in report['notes']))
        self.assertTrue(peer.closed)

    def test_uuid_duplicated_outside_the_set_cannot_authorize_a_route(self):
        for alternate_case in (False, True):
            with self.subTest(alternate_case=alternate_case):
                source, peer = F.conn('ic-8.4-clusterset-primary'), F.clusterset_peer()
                duplicate = list(F._cs_instances('cid-2', 'mf-dr', 30)[0])
                duplicate[0], duplicate[1] = 99, 'cid-outside-set'
                if alternate_case:
                    duplicate[3] = duplicate[3].upper()
                source.scenario['tables']['mysql_innodb_cluster_metadata.v2_instances'].append(duplicate)
                report, opened = self.read_group(peer, source=source)
                dr = next(c for c in report['clusterset']['clusters'] if c['name'] == 'drcluster')
                self.assertEqual(opened, ['remote'])
                self.assertIsNone(dr['route'])
                self.assertTrue(any('a unique eligible metadata member' in note for note in report['notes']))
                self.assertFalse(any('is not a member of any cluster' in note for note in report['notes']))
                self.assertTrue(peer.closed)

    def test_non_member_is_never_opened_and_single_connection_has_only_self(self):
        outsider = dict(self.REMOTE, group='another group')
        report, opened = self.read_group(F.clusterset_peer(), [self.CURRENT, outsider])
        self.assertEqual(opened, [])
        self.assertIsNone(report['clusterset']['clusters'][1]['route'])
        single = m.read_status(F.conn('ic-8.4-clusterset-primary'))['clusterset']['clusters']
        self.assertTrue(single[0]['route']['self'])
        self.assertIsNone(single[1]['route'])

    def test_ordinary_routes_skip_unclassified_profiles_on_every_pass(self):
        profiles = [self.CURRENT,
                    {'id': 'unknown', 'name': 'Unknown server', 'group': 'production',
                     'host': 'unrelated.example', 'port': 3306},
                    {'id': 'spare', 'name': 'Standalone spare', 'group': 'production',
                     'topology_identity': {'server_uuid': 'aaaaaaaa-0000-4000-8000-000000000009'}}]
        for _ in range(2):
            opened = []

            def opener(profile):
                opened.append(profile['id'])
                raise AssertionError('An unclassified profile must never be opened.')

            report = m.read_status(F.conn('ic-8.4-clusterset-primary'), profiles=profiles,
                                   open_profile=opener, current_profile_id='local')
            self.assertEqual(opened, [])
            self.assertIsNone(report['clusterset']['clusters'][1]['route'])

    def test_route_classification_requires_a_full_metadata_endpoint_or_known_uuid(self):
        for classification in ('exact_endpoint', 'known_uuid', 'known_uuid_uppercase',
                               'host_only', 'different_port'):
            with self.subTest(classification=classification):
                source = F.conn('ic-8.4-clusterset-primary')
                peer = F.clusterset_peer()
                profile = {'id': 'remote', 'name': 'Remote', 'group': 'production',
                           'host': 'mf-dr-1', 'port': 3306}
                if classification in ('known_uuid', 'known_uuid_uppercase'):
                    profile['host'] = 'forwarded.example'
                    uuid = peer.scenario['variables']['server_uuid']
                    profile['topology_identity'] = {'server_uuid': uuid.upper() if classification.endswith('uppercase') else uuid}
                elif classification == 'host_only':
                    instance = next(row for row in source.scenario['tables'][
                        'mysql_innodb_cluster_metadata.v2_instances'] if row[3] == peer.scenario['variables']['server_uuid'])
                    instance[4] = instance[5] = 'mf-dr-1'
                elif classification == 'different_port':
                    profile['port'] = 3307
                report, opened = self.read_group(peer, [self.CURRENT, profile], source)
                valid = classification in ('exact_endpoint', 'known_uuid', 'known_uuid_uppercase')
                self.assertEqual(opened, ['remote'] if valid else [])
                self.assertEqual(report['clusterset']['clusters'][1]['route'] is not None, valid)

    def test_cached_identity_then_endpoint_skips_remaining_unclassified_candidates(self):
        uuid = F._cs_instances('cid-2', 'mf-dr', 30)[0][3]
        profiles = [self.CURRENT, dict(self.REMOTE, id='remaining'),
                    dict(self.REMOTE, id='endpoint', host='mf-dr-1', port=3306),
                    dict(self.REMOTE, id='cached', topology_identity={'server_uuid': uuid})]
        opened = []

        def opener(profile):
            opened.append(profile['id'])
            peer = F.clusterset_peer()
            if profile['id'] == 'cached':
                peer.scenario['variables']['server_uuid'] = 'changed-uuid'
            elif profile['id'] == 'endpoint':
                raise ConnectorError('ERROR 2003: unreachable')
            return peer

        report = m.read_status(F.conn('ic-8.4-clusterset-primary'), profiles=profiles,
                               open_profile=opener, current_profile_id='local')
        self.assertEqual(opened, ['cached', 'endpoint'])
        self.assertIsNone(report['clusterset']['clusters'][1]['route'])

    def test_first_verified_route_stops_and_local_cluster_is_never_opened(self):
        uuid = F._cs_instances('cid-2', 'mf-dr', 30)[0][3]
        profiles = [self.CURRENT, dict(self.REMOTE, id='same-cluster',
                    topology_identity={'server_uuid': F.SERVER_UUIDS[1]}),
                    dict(self.REMOTE, id='endpoint', host='mf-dr-1'),
                    dict(self.REMOTE, id='cached', topology_identity={'server_uuid': uuid})]
        report, opened = self.read_group(F.clusterset_peer(), profiles)
        self.assertEqual(opened, ['cached'])
        self.assertEqual(report['clusterset']['clusters'][1]['route']['profile_id'], 'cached')

    def test_three_open_cap(self):
        profiles = [self.CURRENT] + [self.dr_profile('peer-%d' % i, 0) for i in range(5)]
        report, opened = self.read_group(ConnectorError('ERROR 2003: unreachable'), profiles)
        self.assertEqual(opened, ['peer-0', 'peer-1', 'peer-2'])
        self.assertIsNone(report['clusterset']['clusters'][1]['route'])

    def test_group_read_observations_take_precedence_over_saved_identity(self):
        from myflames.connection_groups import GroupScope
        profiles = [self.CURRENT, dict(self.REMOTE, id='local-peer'), self.REMOTE]
        scope = GroupScope(profiles, self.CURRENT)
        scope.record(profiles[1], 'read', identity={'server_uuid': F.SERVER_UUIDS[1]})
        scope.record(self.REMOTE, 'read', identity={
            'server_uuid': F._cs_instances('cid-2', 'mf-dr', 30)[0][3]})
        opened = []

        def opener(profile):
            opened.append(profile['id'])
            return F.clusterset_peer()

        m.read_status(F.conn('ic-8.4-clusterset-primary'), profiles=profiles,
                      open_profile=scope.opener(opener), current_profile_id='local')
        self.assertEqual(opened, ['remote'])

    def test_live_read_keeps_clusters_separate_and_primary_facts_on_primary(self):
        peer = F.clusterset_peer()
        report, opened = self.read_group(peer)
        main, dr = report['clusterset']['clusters']
        self.assertEqual((main['read'], dr['read']), ('this_connection', 'route'))
        self.assertEqual((main['quorum'], dr['quorum']), (True, True))
        self.assertEqual((main['super_read_only'], dr['super_read_only']), (False, True))
        self.assertEqual((main['fenced_writes'], dr['fenced_writes']), (False, True))
        serialized = json.loads(json.dumps(report))['clusterset']['clusters']
        self.assertIs(serialized[0]['super_read_only'], False)
        self.assertIs(serialized[1]['super_read_only'], True)
        self.assertEqual(dr['primary']['server_uuid'], peer.scenario['variables']['server_uuid'])
        self.assertTrue(all(member['state'] == 'ONLINE' for member in dr['members']))
        self.assertEqual(dr['channel']['receiver_state'], 'ON')
        self.assertEqual(dr['channel']['workers'][0]['state'], 'ON')
        self.assertEqual(opened, ['remote'])
        self.assertFalse(any(m['uuid'] == dr['primary']['server_uuid'] for m in report['members']))

    def test_secondary_route_does_not_read_primary_fencing_or_channel(self):
        peer = F.clusterset_peer()
        peer.scenario['variables']['server_uuid'] = F._cs_instances('cid-2', 'mf-dr', 30)[1][3]
        report, _ = self.read_group(peer)
        dr = report['clusterset']['clusters'][1]
        self.assertTrue(dr['quorum'])
        self.assertIsNotNone(dr['primary'])
        self.assertIsNone(dr['super_read_only'])
        self.assertIsNone(dr['fenced_writes'])
        self.assertIsNone(dr['channel'])
        self.assertIn('secondary', dr['read_error'])
        self.assertFalse(any(self.MEMBER_ACTIONS in sql or 'clusterset_replication' in sql for sql, _ in peer.calls))

    def test_secondary_route_follows_primary_cached_identity_before_endpoint(self):
        secondary, primary = F.clusterset_peer(member_index=1), F.clusterset_peer()
        profiles = [self.CURRENT, self.dr_profile('secondary', 1),
                    self.dr_profile('endpoint', host='mf-dr-1', port=3306), self.dr_profile('primary', 0)]
        report, opened = self.read_peers({'secondary': secondary, 'primary': primary}, profiles)
        dr = report['clusterset']['clusters'][1]
        self.assertEqual(opened, ['secondary', 'primary'])
        self.assertEqual(dr['route']['profile_id'], 'primary')
        self.assertEqual(dr['status'], 'OK')
        self.assertTrue(dr['fenced_writes'])
        self.assertEqual(dr['channel']['receiver_state'], 'ON')
        self.assertIsNone(dr['read_error'])
        self.assertFalse(any(self.MEMBER_ACTIONS in sql or 'clusterset_replication' in sql
                             for sql, _ in secondary.calls))
        self.assertEqual(sum("Variable_name IN ('server_uuid')" in sql for sql, _ in primary.calls), 1)
        self.assertTrue(secondary.closed and primary.closed)
        assert_read_only(self, secondary, m.CLUSTER_TAG)
        assert_read_only(self, primary, m.CLUSTER_TAG)

    def test_secondary_route_uses_primary_metadata_endpoint_without_cached_identity(self):
        for stale_uuid in (None, F.SERVER_UUIDS[0]):
            with self.subTest(stale_uuid=stale_uuid):
                secondary, primary = F.clusterset_peer(member_index=1), F.clusterset_peer()
                saved = self.dr_profile('primary', host='MF-DR-1', port='3306')
                if stale_uuid:
                    saved['topology_identity'] = {'server_uuid': stale_uuid}
                profiles = [self.CURRENT, self.dr_profile('secondary', 1), saved]
                report, opened = self.read_peers({'secondary': secondary, 'primary': primary}, profiles)
                self.assertEqual(opened, ['secondary', 'primary'])
                self.assertEqual(report['clusterset']['clusters'][1]['route']['profile_id'], 'primary')
                self.assertEqual(report['clusterset']['clusters'][1]['status'], 'OK')

    def test_failed_cached_primary_can_fall_back_to_its_saved_endpoint(self):
        secondary, stale, primary = F.clusterset_peer(member_index=1), F.clusterset_peer(), F.clusterset_peer()
        stale.scenario['variables']['server_uuid'] = 'different-server'
        profiles = [self.CURRENT, self.dr_profile('secondary', 1),
                    self.dr_profile('endpoint', host='mf-dr-1'), self.dr_profile('stale', 0)]
        report, opened = self.read_peers({'secondary': secondary, 'stale': stale, 'endpoint': primary}, profiles)
        self.assertEqual(opened, ['secondary', 'stale', 'endpoint'])
        self.assertEqual(report['clusterset']['clusters'][1]['route']['profile_id'], 'endpoint')
        self.assertEqual(report['clusterset']['clusters'][1]['status'], 'OK')
        self.assertEqual(len(stale.calls), 1, 'An identity mismatch cannot authorize primary fact reads.')

    def test_unavailable_or_wrong_primary_keeps_secondary_evidence(self):
        for failure in ('open', 'identity', 'members', 'offline', 'not_in_group', 'now_secondary'):
            with self.subTest(failure=failure):
                secondary, primary = F.clusterset_peer(member_index=1), F.clusterset_peer()
                if failure == 'open':
                    primary = ConnectorError('ERROR 2003: connection refused')
                elif failure == 'identity':
                    primary.scenario['variables']['server_uuid'] = 'different-server'
                elif failure == 'members':
                    primary.scenario['tables']['performance_schema.replication_group_members'] = (
                        ConnectorError('ERROR 1142: denied'))
                else:
                    rows = primary.scenario['tables']['performance_schema.replication_group_members']
                    if failure == 'offline':
                        rows[0][4], rows[0][5] = 'OFFLINE', ''
                    elif failure == 'not_in_group':
                        del rows[0]
                    else:
                        rows[0][5], rows[1][5] = 'SECONDARY', 'PRIMARY'
                profiles = [self.CURRENT, self.dr_profile('secondary', 1), self.dr_profile('primary', 0)]
                report, opened = self.read_peers({'secondary': secondary, 'primary': primary}, profiles)
                dr = report['clusterset']['clusters'][1]
                self.assertEqual(opened, ['secondary', 'primary'])
                self.assertEqual(dr['route']['profile_id'], 'secondary')
                self.assertEqual(dr['read'], 'route')
                self.assertTrue(dr['quorum'])
                self.assertTrue(all(member['state'] == 'ONLINE' for member in dr['members']))
                self.assertIsNotNone(dr['primary'])
                self.assertIsNone(dr['fenced_writes'])
                self.assertIsNone(dr['channel'])
                self.assertEqual(dr['status'], 'NOT_READ')
                self.assertIn('fencing', dr['status_reason'])
                self.assertIn('channel', dr['status_reason'])
                self.assertTrue(secondary.closed)
                if failure != 'open':
                    self.assertTrue(primary.closed)
                    self.assertFalse(any(self.MEMBER_ACTIONS in sql or 'clusterset_replication' in sql
                                         for sql, _ in primary.calls))

    def _mfcs_lab(self):
        """Lab ClusterSet mfcs: container metadata names, localhost forwarded ports."""
        cs_rows = F._cs(('cid-a', 'csa', 'PRIMARY', False), ('cid-b', 'csb', 'REPLICA', False),
                        ('cid-c', 'csc', 'REPLICA', False))
        all_instances = (F._cs_instances('cid-a', 'mf-cs-a', 10) +
                         F._cs_instances('cid-b', 'mf-cs-b', 20) +
                         F._cs_instances('cid-c', 'mf-cs-c', 30))

        def node(cid, prefix, start, role, member_index=0):
            instances = F._cs_instances(cid, prefix, start)
            scenario = F._ic([], [], version=(2, 2, 0), instances=all_instances,
                             variables=F._mysql_vars(
                                 server_uuid=instances[member_index][3],
                                 hostname='%s-%d' % (prefix, member_index + 1),
                                 report_host='%s-%d' % (prefix, member_index + 1),
                                 group_replication_group_name='11111111-3333-4444-5555-%012d' % start,
                                 super_read_only='OFF' if role == 'PRIMARY' and member_index == 0 else 'ON'))
            scenario['tables']['performance_schema.replication_group_members'] = [
                ['group_replication_applier', inst[3], '%s-%d' % (prefix, idx + 1), '3306', 'ONLINE',
                 'PRIMARY' if idx == 0 else 'SECONDARY', '8.4.11', 'MySQL']
                for idx, inst in enumerate(instances)]
            scenario['tables']['mysql_innodb_cluster_metadata.v2_gr_clusters'] = [
                ['cid-a', 'csa', 'pm', '11111111-3333-4444-5555-%012d' % 10],
                ['cid-b', 'csb', 'pm', '11111111-3333-4444-5555-%012d' % 20],
                ['cid-c', 'csc', 'pm', '11111111-3333-4444-5555-%012d' % 30]]
            scenario['tables']['mysql_innodb_cluster_metadata.v2_cs_members'] = copy.deepcopy(cs_rows)
            scenario['tables']['performance_schema.replication_applier_status_by_worker'] = [[1, 'ON', 0, '']]
            scenario['tables'][self.MEMBER_ACTIONS] = [[0]]
            if role == 'REPLICA':
                scenario['extra'] = {
                    'performance_schema.replication_connection_status': [['ON', '0', '']],
                    'performance_schema.replication_applier_status': [['ON']],
                    'performance_schema.replication_connection_configuration': [['mf-cs-a-1', '3306']]}
            return F.ScenarioConn(scenario)

        peers = {
            'cs-a1': node('cid-a', 'mf-cs-a', 10, 'PRIMARY', 0),
            'cs-a2': node('cid-a', 'mf-cs-a', 10, 'PRIMARY', 1),
            'cs-b1': node('cid-b', 'mf-cs-b', 20, 'REPLICA', 0),
            'cs-b2': node('cid-b', 'mf-cs-b', 20, 'REPLICA', 1),
            'cs-b3': node('cid-b', 'mf-cs-b', 20, 'REPLICA', 2),
            'cs-c1': node('cid-c', 'mf-cs-c', 30, 'REPLICA', 0),
        }
        profiles = {
            pid: {'id': pid, 'name': pid, 'group': 'production', 'host': '127.0.0.1', 'port': port,
                  'topology_identity': {'server_uuid': peers[pid].scenario['variables']['server_uuid']}}
            for pid, port in (('cs-a1', 34601), ('cs-a2', 34602), ('cs-b1', 34603),
                              ('cs-b2', 34604), ('cs-c1', 34605), ('cs-b3', 34606))
        }
        return peers, profiles

    def test_tier_attachment_does_not_reopen_routes_or_probe_a_spare(self):
        from myflames import topology_links
        topology_links._reset_cache_for_tests()
        peers, by_id = self._mfcs_lab()
        profiles = [dict(by_id[pid], remember_password=True) for pid in ('cs-a1', 'cs-b1', 'cs-c1')]
        profiles.append({'id': 'spare', 'name': 'Standalone spare', 'group': 'production',
                         'host': '127.0.0.1', 'port': 34607, 'remember_password': True,
                         'topology_identity': {'server_uuid': 'aaaaaaaa-0000-4000-8000-000000000009'}})
        profiles.append({'id': 'outsider', 'name': 'Outside group', 'group': 'another',
                         'host': '127.0.0.1', 'port': 34608, 'remember_password': True})
        peers['spare'] = F.ScenarioConn(F._ic([], []))
        peers['outsider'] = F.ScenarioConn(F._ic([], []))
        opened = []

        def opener(profile):
            opened.append(profile['id'])
            return peers[profile['id']]

        report = m.read_status(peers['cs-a1'], profiles=profiles, open_profile=opener,
                               current_profile_id='cs-a1')
        self.assertEqual(opened, ['cs-b1', 'cs-c1'])
        m.attach_topology_tiers(peers['cs-a1'], report, profiles=profiles, open_profile=opener,
                                current_profile_id='cs-a1')
        self.assertEqual(opened, ['cs-b1', 'cs-c1'], 'A topology pass must not add saved logins.')
        self.assertEqual(len(opened), len(set(opened)))

    def test_secondary_route_falls_back_to_remaining_group_members_for_primary(self):
        from myflames.connection_groups import GroupScope
        for order in (('cs-a1', 'cs-b2', 'cs-b1', 'cs-a2', 'cs-c1'),
                      ('cs-a1', 'cs-b2', 'cs-c1', 'cs-b1', 'cs-a2')):
            with self.subTest(order=order):
                peers, by_id = self._mfcs_lab()
                profiles = [dict(by_id[pid]) for pid in order]
                scope = GroupScope(profiles, profiles[0])
                opened = []

                def opener(profile):
                    opened.append(profile['id'])
                    return peers[profile['id']]

                report = m.read_status(peers['cs-a1'], profiles=profiles,
                                       open_profile=scope.opener(opener), current_profile_id='cs-a1')
                clusters = {c['name']: c for c in report['clusterset']['clusters']}
                csb = clusters['csb']
                self.assertEqual(csb['route']['profile_id'], 'cs-b1')
                self.assertEqual(csb['status'], 'OK')
                self.assertTrue(csb['fenced_writes'])
                self.assertEqual(csb['channel']['receiver_state'], 'ON')
                self.assertEqual(csb['channel']['applier_state'], 'ON')
                self.assertIsNone(csb['read_error'])
                self.assertEqual(clusters['csc']['status'], 'OK')
                self.assertEqual(report['clusterset']['status'], 'HEALTHY')
                self.assertIn('cs-b1', opened)
                for pid in opened:
                    self.assertTrue(peers[pid].closed)
                    assert_read_only(self, peers[pid], m.CLUSTER_TAG)
                recorded = {r['id']: r.identity.get('server_uuid') for r in scope.read}
                self.assertEqual(recorded['cs-b1'], F._cs_instances('cid-b', 'mf-cs-b', 20)[0][3])

    def test_secondary_route_followup_stops_at_three_open_cap(self):
        peers, by_id = self._mfcs_lab()
        profiles = [by_id['cs-a1'], by_id['cs-b2'],
                    dict(by_id['cs-b1'], id='missing-1'), dict(by_id['cs-b1'], id='missing-2'),
                    by_id['cs-b1'], by_id['cs-c1']]
        opened = []

        def opener(profile):
            opened.append(profile['id'])
            if profile['id'].startswith('missing-'):
                raise ConnectorError('ERROR 2003: unreachable')
            return peers[profile['id']]

        report = m.read_status(peers['cs-a1'], profiles=profiles,
                               open_profile=opener, current_profile_id='cs-a1')
        csb = next(c for c in report['clusterset']['clusters'] if c['name'] == 'csb')
        self.assertNotIn('cs-b1', opened)
        self.assertEqual(opened, ['cs-b2', 'missing-1', 'missing-2', 'cs-c1'])
        self.assertEqual(csb['route']['profile_id'], 'cs-b2')
        self.assertEqual(csb['read'], 'route')
        self.assertTrue(csb['quorum'])
        self.assertIsNone(csb['fenced_writes'])
        self.assertIsNone(csb['channel'])
        self.assertEqual(csb['status'], 'NOT_READ')
        self.assertEqual(csb['read_error'],
                         "The route is a secondary member; the primary's fencing and channel were not read.")
        self.assertEqual(csb['status_reason'], csb['read_error'])

    def test_primary_followup_skips_local_and_proven_and_prefers_identified_primary(self):
        from myflames.connection_groups import GroupScope
        peers, by_id = self._mfcs_lab()
        b_instances = F._cs_instances('cid-b', 'mf-cs-b', 20)
        a_instances = F._cs_instances('cid-a', 'mf-cs-a', 10)
        c_instances = F._cs_instances('cid-c', 'mf-cs-c', 30)
        profiles = [
            dict(by_id['cs-a1']),
            dict(by_id['cs-a2'], topology_identity={'server_uuid': a_instances[1][3]}),
            dict(by_id['cs-c1']),
            dict(by_id['cs-b2'], topology_identity={'server_uuid': b_instances[1][3]}),
            dict(by_id['cs-b3'], topology_identity={'server_uuid': b_instances[2][3]}),
            dict(by_id['cs-b1']),
        ]
        # cs-c1 was already proven in this pass by an earlier grouped read.
        scope = GroupScope(profiles, profiles[0])
        scope.record(profiles[2], 'read', identity={'server_uuid': c_instances[0][3]})
        opened = []

        def opener(profile):
            opened.append(profile['id'])
            return peers[profile['id']]

        report = m.read_status(peers['cs-a1'], profiles=profiles,
                               open_profile=scope.opener(opener), current_profile_id='cs-a1')
        csb = next(c for c in report['clusterset']['clusters'] if c['name'] == 'csb')
        self.assertEqual(opened, ['cs-b2', 'cs-b1', 'cs-c1'])
        self.assertEqual(csb['route']['profile_id'], 'cs-b1')
        self.assertEqual(csb['status'], 'OK')
        self.assertTrue(csb['fenced_writes'])
        self.assertEqual(csb['channel']['receiver_state'], 'ON')

    def test_secondary_route_remaining_fallback_stops_at_time_budget(self):
        peers, by_id = self._mfcs_lab()
        c_instances = F._cs_instances('cid-c', 'mf-cs-c', 30)
        profiles = [
            dict(by_id['cs-a1']),
            dict(by_id['cs-b2']),
            dict(by_id['cs-b1']),
            dict(by_id['cs-c1'], topology_identity={'server_uuid': c_instances[0][3]}),
        ]
        clock, opened = [0.0], []

        def opener(profile):
            opened.append(profile['id'])
            peer = peers[profile['id']]
            original = peer.run

            def run(sql, **kwargs):
                result = original(sql, **kwargs)
                clock[0] += 3.0
                return result

            peer.run = run
            return peer

        with mock.patch.object(m.time, 'monotonic', side_effect=lambda: clock[0]):
            report = m.read_status(peers['cs-a1'], profiles=profiles,
                                   open_profile=opener, current_profile_id='cs-a1')
        csb = next(c for c in report['clusterset']['clusters'] if c['name'] == 'csb')
        self.assertEqual(opened, ['cs-b2', 'cs-c1'])
        self.assertNotIn('cs-b1', opened)
        self.assertEqual(csb['route']['profile_id'], 'cs-b2')
        self.assertEqual(csb['status'], 'NOT_READ')
        self.assertEqual(csb['read_error'],
                         "The route is a secondary member; the primary's fencing and channel were not read.")

    def test_primary_followup_counts_toward_the_three_open_cap(self):
        secondary = F.clusterset_peer(member_index=1)
        profiles = [self.CURRENT, self.dr_profile('secondary', 1), self.dr_profile('old-one', 0),
                    self.dr_profile('old-two', 0), self.dr_profile('endpoint', host='mf-dr-1')]
        peers = {'secondary': secondary, 'old-one': ConnectorError('ERROR 2003: unreachable'),
                 'old-two': ConnectorError('ERROR 2003: unreachable')}
        report, opened = self.read_peers(peers, profiles)
        self.assertEqual(opened, ['secondary', 'old-one', 'old-two'])
        dr = report['clusterset']['clusters'][1]
        self.assertEqual(dr['route']['profile_id'], 'secondary')
        self.assertTrue(dr['quorum'])
        self.assertIsNone(dr['fenced_writes'])
        self.assertIsNone(dr['channel'])

    def test_primary_followup_shares_the_cluster_read_deadline(self):
        secondary, primary = F.clusterset_peer(member_index=1), F.clusterset_peer()
        clock = [0.0]

        def timed_run(peer):
            original = peer.run

            def run(sql, **kwargs):
                result = original(sql, **kwargs)
                clock[0] += 2.0
                return result

            return run

        secondary.run, primary.run = timed_run(secondary), timed_run(primary)
        profiles = [self.CURRENT, self.dr_profile('secondary', 1), self.dr_profile('primary', 0)]
        with mock.patch.object(m.time, 'monotonic', side_effect=lambda: clock[0]):
            report, opened = self.read_peers({'secondary': secondary, 'primary': primary}, profiles)
        self.assertEqual(opened, ['secondary', 'primary'])
        self.assertEqual(len(primary.calls), 1, 'Only the identity fits the remaining cluster budget.')
        self.assertLess(primary.timeouts[0], secondary.timeouts[0])
        dr = report['clusterset']['clusters'][1]
        self.assertEqual(dr['route']['profile_id'], 'secondary')
        self.assertTrue(dr['quorum'])
        self.assertIsNone(dr['fenced_writes'])
        self.assertTrue(any('timed out' in note for note in report['notes']))
        self.assertTrue(secondary.closed and primary.closed)

    def test_primary_followup_never_opens_a_saved_connection_outside_the_group(self):
        secondary = F.clusterset_peer(member_index=1)
        outsider = self.dr_profile('primary', 0)
        outsider['group'] = 'different group'
        profiles = [self.CURRENT, self.dr_profile('secondary', 1), outsider]
        report, opened = self.read_peers({'secondary': secondary}, profiles)
        self.assertEqual(opened, ['secondary'])
        self.assertIsNone(report['clusterset']['clusters'][1]['fenced_writes'])

    def test_primary_cluster_seen_through_secondary_keeps_writability_unknown(self):
        source = F.conn('ic-8.4-clusterset-replica')
        secondary = F.clusterset_peer('cid-0', 'mf-main', 20, 'ic-8.4-clusterset-replica', member_index=1)
        report, opened = self.read_group(secondary, source=source)
        primary = report['clusterset']['clusters'][0]
        self.assertEqual(opened, ['remote'])
        self.assertEqual(primary['status'], 'NOT_READ')
        self.assertEqual(report['clusterset']['status'], 'UNKNOWN')
        self.assertIsNone(primary['super_read_only'])
        self.assertTrue(primary['quorum'])
        self.assertIsNone(primary['fenced_writes'])
        self.assertIn('fencing', primary['status_reason'])
        self.assertIn('not read', primary['status_reason'])

    def test_local_primary_cluster_secondary_does_not_claim_fencing_was_read(self):
        source = F.conn('ic-8.4-clusterset-primary')
        source.scenario['variables'].update(server_uuid=F.SERVER_UUIDS[1], super_read_only='ON')
        cs = m.read_status(source)['clusterset']
        primary = cs['clusters'][0]
        self.assertEqual(primary['status'], 'NOT_READ')
        self.assertEqual(cs['status'], 'UNKNOWN')
        self.assertIsNone(primary['super_read_only'])
        self.assertIsNone(primary['fenced_writes'])
        self.assertIn('fencing', primary['status_reason'])
        self.assertIn('not read', primary['status_reason'])

    def test_current_secondary_can_read_its_primary_through_a_saved_group_member(self):
        for scenario in ('ic-8.4-clusterset-primary', 'ic-8.4-clusterset-replica'):
            with self.subTest(scenario=scenario):
                source, primary = F.conn(scenario), F.conn(scenario)
                source.scenario['variables'].update(server_uuid=F.SERVER_UUIDS[1], super_read_only='ON')
                profiles = [self.CURRENT, dict(self.REMOTE, id='primary', name='Local primary',
                            topology_identity={'server_uuid': F.SERVER_UUIDS[0]})]
                report, opened = self.read_peers({'primary': primary}, profiles, source)
                local = next(c for c in report['clusterset']['clusters'] if c['local'])
                self.assertEqual(opened, ['primary'])
                self.assertEqual(local['route'], {'profile_id': 'primary', 'profile_name': 'Local primary',
                                                 'self': False, 'server_uuid': F.SERVER_UUIDS[0]})
                self.assertEqual(local['primary']['server_uuid'], F.SERVER_UUIDS[0])
                self.assertEqual(local['status'], 'OK')
                self.assertEqual(local['fenced_writes'], local['role'] == 'REPLICA')
                self.assertEqual(report['server']['server_uuid'], F.SERVER_UUIDS[1])
                if local['role'] == 'REPLICA':
                    self.assertEqual(local['channel']['receiver_state'], 'ON')
                else:
                    self.assertEqual(report['clusterset']['metadata_read_on'], local['route'])
                self.assertEqual(sum("Variable_name IN ('server_uuid')" in sql for sql, _ in primary.calls), 1)
                self.assertTrue(primary.closed)
                assert_read_only(self, primary, m.CLUSTER_TAG)

    def test_identified_local_primary_is_read_once_after_remote_cluster_search(self):
        source, primary, dr = (F.conn('ic-8.4-clusterset-primary'),
                               F.conn('ic-8.4-clusterset-primary'), F.clusterset_peer())
        source.scenario['variables']['server_uuid'] = F.SERVER_UUIDS[1]
        profiles = [self.CURRENT,
                    dict(self.REMOTE, id='local-primary', topology_identity={'server_uuid': F.SERVER_UUIDS[0]}),
                    self.dr_profile('dr', 0)]
        report, opened = self.read_peers({'local-primary': primary, 'dr': dr}, profiles, source)
        local = next(c for c in report['clusterset']['clusters'] if c['local'])
        self.assertEqual(local['route']['profile_id'], 'local-primary')
        self.assertEqual(local['route']['server_uuid'], F.SERVER_UUIDS[0])
        self.assertEqual(local['primary']['server_uuid'], F.SERVER_UUIDS[0])
        self.assertIs(local['fenced_writes'], False)
        self.assertEqual(opened.count('local-primary'), 1)
        self.assertEqual(opened.count('dr'), 1)
        self.assertEqual(len(opened), len(set(opened)))

    def test_read_replica_or_offline_local_member_can_read_an_online_peer(self):
        for state in ('read_replica', 'offline'):
            with self.subTest(state=state):
                if state == 'read_replica':
                    source = F.conn('ic-8.4-read-replica-clusterset')
                    source_uuid = F.RR_UUID
                else:
                    source = F.conn('ic-8.4-clusterset-primary')
                    source_uuid = F.SERVER_UUIDS[1]
                    source.scenario['variables']['server_uuid'] = source_uuid
                    row = list(source.scenario['tables']['performance_schema.replication_group_members'][1])
                    row[4], row[5] = 'OFFLINE', None
                    source.scenario['tables']['performance_schema.replication_group_members'] = [row]
                primary = F.conn('ic-8.4-clusterset-primary')
                profile = dict(self.REMOTE, id='online-primary',
                               topology_identity={'server_uuid': F.SERVER_UUIDS[0]})
                report, opened = self.read_peers({'online-primary': primary}, [self.CURRENT, profile], source)
                local = next(c for c in report['clusterset']['clusters'] if c['local'])
                self.assertEqual(report['server']['server_uuid'], source_uuid)
                self.assertEqual(opened, ['online-primary'])
                self.assertEqual(local['route']['profile_id'], 'online-primary')
                self.assertEqual(local['route']['server_uuid'], F.SERVER_UUIDS[0])
                self.assertEqual(local['primary']['server_uuid'], F.SERVER_UUIDS[0])
                self.assertIs(local['quorum'], True)
                self.assertIs(local['fenced_writes'], False)

    def test_no_route_uses_metadata_members_with_unknown_states(self):
        dr = F.report('ic-8.4-clusterset-primary')['clusterset']['clusters'][1]
        self.assertEqual(dr['read'], 'metadata_only')
        self.assertEqual(len(dr['members']), 3)
        self.assertTrue(all(m['state'] is None for m in dr['members']))

    def test_member_read_failure_keeps_route_and_metadata_members(self):
        peer = F.clusterset_peer()
        peer.scenario['tables']['performance_schema.replication_group_members'] = ConnectorError('ERROR 1142: denied')
        report, _ = self.read_group(peer)
        dr = report['clusterset']['clusters'][1]
        self.assertEqual(dr['read'], 'failed')
        self.assertIn('denied', dr['read_error'])
        self.assertEqual(dr['route']['profile_id'], 'remote')
        self.assertTrue(all(m['state'] is None for m in dr['members']))
        self.assertTrue(peer.closed)

    def test_route_budget_is_shared_by_identity_members_and_primary_reads(self):
        peer = F.clusterset_peer()
        clock = [0.0]
        original = peer.run

        def run(sql, **kwargs):
            result = original(sql, **kwargs)
            clock[0] += 2.6
            return result

        peer.run = run
        with mock.patch.object(m.time, 'monotonic', side_effect=lambda: clock[0]):
            report, opened = self.read_group(peer)
        dr = report['clusterset']['clusters'][1]
        self.assertEqual(opened, ['remote'])
        self.assertEqual(dr['read'], 'failed')
        self.assertIn('timed out', dr['read_error'])
        self.assertEqual(len(peer.calls), 2)
        self.assertLess(peer.timeouts[1], peer.timeouts[0])
        self.assertTrue(peer.closed)

    def test_status_words_name_the_evidence_and_gtids_are_not_checked(self):
        cases = [('ok', 'OK', 'receiver and applier ON'),
                 ('missing', 'NOT_REPLICATING', 'no clusterset_replication'),
                 ('stopped', 'NOT_REPLICATING', 'receiver OFF'),
                 ('worker_error', 'NOT_REPLICATING', 'applier ERROR'),
                 ('connecting', 'NOT_REPLICATING', 'receiver CONNECTING'),
                 ('no_quorum', 'NOT_OK', 'majority'),
                 ('no_primary', 'NOT_OK', 'no unique ONLINE primary'),
                 ('denied', 'NOT_READ', 'could not be read in full')]
        for case, status, evidence in cases:
            with self.subTest(case=case):
                peer = F.clusterset_peer()
                tables, extra = peer.scenario['tables'], peer.scenario['extra']
                if case == 'missing':
                    extra['performance_schema.replication_connection_status'] = []
                if case in ('stopped', 'connecting'):
                    extra['performance_schema.replication_connection_status'][0][0] = (
                        'OFF' if case == 'stopped' else 'CONNECTING')
                if case == 'worker_error':
                    tables['performance_schema.replication_applier_status_by_worker'] = [[1, 'OFF', 1062, 'duplicate']]
                if case == 'denied':
                    tables['performance_schema.replication_applier_status_by_worker'] = ConnectorError('ERROR 1142: denied')
                if case == 'no_quorum':
                    for row in tables['performance_schema.replication_group_members'][1:]:
                        row[4] = 'UNREACHABLE'
                if case == 'no_primary':
                    tables['performance_schema.replication_group_members'][0][5] = 'SECONDARY'
                report, _ = self.read_group(peer)
                cluster = report['clusterset']['clusters'][1]
                self.assertEqual(cluster['status'], status)
                self.assertIn(evidence, cluster['status_reason'])
                self.assertIsNone(cluster['gtid_consistency'])
                self.assertIsNone(report['clusterset']['gtid_consistency'])

    def test_fencing_is_judged_on_primary_cluster_only(self):
        source = F.conn('ic-8.4-clusterset-primary')
        source.scenario['variables']['super_read_only'] = 'ON'
        report, _ = self.read_group(F.clusterset_peer(), source=source)
        primary, replica = report['clusterset']['clusters']
        self.assertEqual(primary['status'], 'FENCED_WRITES')
        self.assertIn('super_read_only ON', primary['status_reason'])
        self.assertTrue(replica['fenced_writes'])
        self.assertEqual(replica['status'], 'OK')
        self.assertEqual(report['clusterset']['status'], 'UNAVAILABLE')
        self.assertEqual(report['clusterset']['status_reason'], 'Primary Cluster is fenced from write traffic.')

    def test_primary_fencing_requires_sro_and_disabled_member_action(self):
        for sro, enabled, fenced in (('ON', 0, True), ('ON', 1, False),
                                     ('OFF', 0, False), ('OFF', 1, False)):
            with self.subTest(super_read_only=sro, action_enabled=enabled):
                source = F.conn('ic-8.4-clusterset-primary')
                source.scenario['variables']['super_read_only'] = sro
                source.scenario['tables'][self.MEMBER_ACTIONS] = [[enabled]]
                source.scenario['tables']['performance_schema.replication_connection_status'] = []
                report = m.read_status(source)
                primary = report['clusterset']['clusters'][0]
                self.assertIs(primary['super_read_only'], sro == 'ON')
                self.assertIs(primary['disable_super_read_only_if_primary'], bool(enabled))
                self.assertIs(primary['fenced_writes'], fenced)
                expected_status = 'FENCED_WRITES' if fenced else ('NOT_OK' if sro == 'ON' else 'OK')
                self.assertEqual(primary['status'], expected_status)
                if sro == 'ON':
                    self.assertEqual(report['clusterset']['status'], 'UNAVAILABLE')
                    self.assertIn('super_read_only ON', primary['status_reason'])
                queries = [sql for sql, _ in source.calls if self.MEMBER_ACTIONS in sql]
                self.assertEqual(len(queries), 1)
                self.assertIn("SELECT enabled FROM " + self.MEMBER_ACTIONS, queries[0])
                self.assertIn("WHERE name = 'mysql_disable_super_read_only_if_primary'", queries[0])
                sro_query = next(sql for sql, _ in source.calls
                                 if "Variable_name IN ('super_read_only')" in sql)
                self.assertIn('performance_schema.replication_group_members', sro_query)
                if fenced:
                    self.assertIn('mysql_disable_super_read_only_if_primary', primary['status_reason'])
                    self.assertIn('disabled', primary['status_reason'])
                    self.assertEqual(report['clusterset']['status_reason'],
                                     'Primary Cluster is fenced from write traffic.')
                elif sro == 'ON':
                    self.assertIn('writes are currently blocked', primary['status_reason'])
                    self.assertIn('not write-fenced', primary['status_reason'])
                    self.assertIn('super_read_only ON', report['clusterset']['status_reason'])
                assert_read_only(self, source, m.CLUSTER_TAG)

    def test_unreadable_member_action_keeps_fencing_unknown_with_reason(self):
        cases = (('denied', ConnectorError('ERROR 1142: denied; password=private-test-value'), True),
                 ('missing_table', ConnectorError('ERROR 1146: table does not exist'), True),
                 ('missing_row', [], True), ('null_value', [[None]], True), ('not_in_fixture', None, True))
        for name, answer, expected_sro in cases:
            with self.subTest(case=name):
                source = F.conn('ic-8.4-clusterset-primary')
                source.scenario['variables']['super_read_only'] = 'ON'
                if answer is not None:
                    source.scenario['tables'][self.MEMBER_ACTIONS] = answer
                report = m.read_status(source)
                primary = report['clusterset']['clusters'][0]
                self.assertIs(primary['super_read_only'], expected_sro)
                self.assertIsNone(primary['disable_super_read_only_if_primary'])
                self.assertIsNone(primary['fenced_writes'])
                self.assertIn('fencing', primary['status_reason'])
                self.assertIn('replication_group_member_actions', primary['status_reason'])
                self.assertNotIn('private-test-value', primary['status_reason'])
                if expected_sro is True:
                    self.assertEqual(primary['status'], 'NOT_OK')
                    self.assertEqual(report['clusterset']['status'], 'UNAVAILABLE')
                    self.assertIn('super_read_only ON', primary['status_reason'])

    def test_unreadable_sro_is_not_inferred_from_the_member_action(self):
        for sro in (None, 'missing'):
            for enabled in (0, 1):
                with self.subTest(super_read_only=sro, action_enabled=enabled):
                    source = F.conn('ic-8.4-clusterset-primary')
                    if sro == 'missing':
                        source.scenario['variables'].pop('super_read_only')
                    else:
                        source.scenario['variables']['super_read_only'] = sro
                    source.scenario['tables'][self.MEMBER_ACTIONS] = [[enabled]]
                    cs = m.read_status(source)['clusterset']
                    primary = cs['clusters'][0]
                    self.assertIsNone(primary['super_read_only'])
                    self.assertIs(primary['disable_super_read_only_if_primary'], bool(enabled))
                    self.assertIsNone(primary['fenced_writes'])
                    self.assertEqual(primary['status'], 'NOT_READ')
                    self.assertEqual(cs['status'], 'UNKNOWN')
                    self.assertIn('super_read_only', primary['status_reason'])

    def test_remote_primary_reads_member_action_in_its_existing_login(self):
        source = F.conn('ic-8.4-clusterset-replica')
        peer = F.clusterset_peer('cid-0', 'mf-main', 20, 'ic-8.4-clusterset-replica')
        peer.scenario['variables']['super_read_only'] = 'ON'
        peer.scenario['tables'][self.MEMBER_ACTIONS] = [[1]]
        report, opened = self.read_group(peer, source=source)
        primary = report['clusterset']['clusters'][0]
        self.assertIs(primary['super_read_only'], True)
        self.assertIs(primary['disable_super_read_only_if_primary'], True)
        self.assertIs(primary['fenced_writes'], False)
        self.assertEqual(primary['status'], 'NOT_OK')
        self.assertEqual(report['clusterset']['status'], 'UNAVAILABLE')
        self.assertIn('super_read_only ON', primary['status_reason'])
        self.assertEqual(opened, ['remote'])
        self.assertTrue(peer.closed)
        queries = [sql for sql, _ in peer.calls]
        self.assertEqual(sum(self.MEMBER_ACTIONS in sql for sql in queries), 1)
        sro_reads = [sql for sql in queries if "Variable_name IN ('super_read_only')" in sql]
        self.assertEqual(len(sro_reads), 1)
        self.assertIn('performance_schema.replication_group_members', sro_reads[0])
        self.assertNotIn(self.MEMBER_ACTIONS, sro_reads[0])
        assert_read_only(self, peer, m.CLUSTER_TAG)

    def test_replica_sro_is_expected_even_with_disabled_member_action(self):
        for enabled in (0, 1):
            with self.subTest(action_enabled=enabled):
                peer = F.clusterset_peer()
                peer.scenario['tables'][self.MEMBER_ACTIONS] = [[enabled]]
                report, opened = self.read_group(peer)
                replica = report['clusterset']['clusters'][1]
                self.assertEqual(replica['role'], 'REPLICA')
                self.assertEqual(replica['status'], 'OK')
                self.assertIs(replica['super_read_only'], True)
                self.assertEqual(replica['fenced_writes'], not bool(enabled))
                self.assertEqual(opened, ['remote'])
                self.assertEqual(report['clusterset']['status'], 'HEALTHY')
                # Action-enabled plus SRO ON is not proof of a stale role.
                self.assertFalse(any('v2_cs_members' in sql for sql, _ in peer.calls))

    def test_primary_sro_blocks_removal_and_switchover_with_or_without_fencing(self):
        from test_cluster_actions import clusterset_checks
        for enabled in (0, 1):
            with self.subTest(action_enabled=enabled):
                source = F.conn('ic-8.4-clusterset-primary')
                source.scenario['variables']['super_read_only'] = 'ON'
                source.scenario['tables'][self.MEMBER_ACTIONS] = [[enabled]]
                report, _ = self.read_group(F.clusterset_peer(), source=source)
                checks = clusterset_checks(report)
                for aid in ('set_primary_cluster:drcluster', 'remove_cluster:drcluster'):
                    reason = 'super_read_only ON' if enabled else 'writes are fenced'
                    with self.assertRaisesRegex(ValueError, reason):
                        m.plan_action(report, aid, checks=checks)

    def test_set_status_requires_evidence_from_this_pass(self):
        report, _ = self.read_group(F.clusterset_peer())
        self.assertEqual(report['clusterset']['status'], 'HEALTHY')
        cs = F.report('ic-8.4-clusterset-primary')['clusterset']
        self.assertEqual(cs['status'], 'AVAILABLE')
        self.assertEqual(cs['clusters'][1]['status'], 'NOT_READ')
        source = F.conn('ic-8.4-clusterset-primary')
        for row in source.scenario['tables']['performance_schema.replication_group_members'][1:]:
            row[4] = 'UNREACHABLE'
        self.assertEqual(m.read_status(source)['clusterset']['status'], 'UNAVAILABLE')
        self.assertEqual(F.report('ic-8.4-clusterset-replica')['clusterset']['status'], 'UNKNOWN')
        source = F.conn('ic-8.4-clusterset-primary')
        source.scenario['tables']['mysql_innodb_cluster_metadata.v2_cs_members'][0][3] = 'REPLICA'
        self.assertEqual(m.read_status(source)['clusterset']['status'], 'UNAVAILABLE')

    def test_invalidated_status_overrides_unread_live_state(self):
        cs = F.report('ic-8.4-clusterset-invalidated')['clusterset']
        cluster = cs['clusters'][1]
        self.assertEqual(cluster['status'], 'INVALIDATED')
        self.assertIn('metadata', cluster['status_reason'])
        self.assertEqual(cs['status'], 'AVAILABLE')

    def test_primary_route_replaces_replicated_metadata_and_keeps_route(self):
        source = F.conn('ic-8.4-clusterset-replica')
        peer = F.clusterset_peer('cid-0', 'mf-main', 20, 'ic-8.4-clusterset-replica')
        for row in peer.scenario['tables']['mysql_innodb_cluster_metadata.v2_cs_members']:
            row[6] = '9007199254740993'
            if row[1] == 'cid-0':
                row[2] = 'renamed-primary'
        report, opened = self.read_group(peer, source=source)
        cs = report['clusterset']
        self.assertEqual(opened, ['remote'])
        self.assertEqual(cs['view_id'], '9007199254740993')
        self.assertEqual(cs['primary_cluster'], 'renamed-primary')
        self.assertFalse(cs['from_local_copy'])
        self.assertEqual(cs['metadata_read_on'], cs['clusters'][0]['route'])
        self.assertFalse(cs['metadata_read_on']['self'])
        self.assertTrue(cs['clusters'][0]['quorum'])
        self.assertEqual(cs['clusters'][1]['read'], 'this_connection')
        self.assertFalse(any('replicated copy' in n and 'ClusterSet' in n for n in report['notes']))
        self.assertEqual(sum('v2_cs_members' in sql for sql, _ in peer.calls), 1)
        self.assertTrue(peer.closed)

    def test_failed_primary_metadata_keeps_local_view_and_copy_warning(self):
        source = F.conn('ic-8.4-clusterset-replica')
        peer = F.clusterset_peer('cid-0', 'mf-main', 20, 'ic-8.4-clusterset-replica')
        peer.scenario['tables']['mysql_innodb_cluster_metadata.v2_cs_members'] = ConnectorError('ERROR 1142: denied')
        report, _ = self.read_group(peer, source=source)
        cs = report['clusterset']
        self.assertEqual(cs['view_id'], '4294967297')
        self.assertTrue(cs['metadata_read_on']['self'])
        self.assertTrue(cs['from_local_copy'])
        self.assertTrue(any('replicated copy' in n and 'ClusterSet' in n for n in report['notes']))
        self.assertEqual(cs['clusters'][0]['route']['profile_id'], 'remote')

    def test_primary_cluster_without_quorum_keeps_replicated_copy_warning(self):
        for route in ('local', 'remote'):
            with self.subTest(route=route):
                source = F.conn('ic-8.4-clusterset-primary' if route == 'local'
                                else 'ic-8.4-clusterset-replica')
                primary = (source if route == 'local' else
                           F.clusterset_peer('cid-0', 'mf-main', 20, 'ic-8.4-clusterset-replica'))
                for row in primary.scenario['tables']['performance_schema.replication_group_members'][1:]:
                    row[4] = 'UNREACHABLE'
                report = m.read_status(source) if route == 'local' else self.read_group(primary, source=source)[0]
                cs = report['clusterset']
                self.assertFalse(cs['clusters'][0]['quorum'])
                self.assertTrue(cs['from_local_copy'])
                self.assertTrue(any('replicated copy' in n and 'may be behind' in n for n in report['notes']))

    def test_offline_primary_cluster_member_keeps_replicated_copy_warning(self):
        source = F.conn('ic-8.4-clusterset-primary')
        source.scenario['tables']['performance_schema.replication_group_members'] = [F._gr_member(0, 'OFFLINE')]
        report = m.read_status(source)
        self.assertTrue(report['clusterset']['from_local_copy'])
        self.assertTrue(any('replicated copy' in n and 'may be behind' in n for n in report['notes']))

    def test_primary_cluster_online_secondary_with_quorum_keeps_stale_copy_note(self):
        # Group Replication defaults to EVENTUAL consistency: an ONLINE secondary
        # can still lag behind unapplied metadata transactions, so only the
        # primary cluster's ONLINE primary clears from_local_copy.
        source = F.conn('ic-8.4-clusterset-replica')
        secondary = F.clusterset_peer('cid-0', 'mf-main', 20, 'ic-8.4-clusterset-replica', member_index=1)
        report, _ = self.read_group(secondary, source=source)
        self.assertTrue(report['clusterset']['from_local_copy'])
        self.assertTrue(report['clusterset']['metadata_read_on']['self'])
        self.assertTrue(any('replicated copy' in n and 'ClusterSet' in n for n in report['notes']))
        # Connected directly to a primary-cluster secondary without a saved primary route.
        local_sec = F.conn('ic-8.4-clusterset-primary')
        local_sec.scenario['variables'].update(server_uuid=F.SERVER_UUIDS[1], super_read_only='ON')
        local_report = m.read_status(local_sec)
        self.assertTrue(local_report['clusterset']['from_local_copy'])
        self.assertTrue(any('ONLINE primary' in n and 'replicated copy' in n for n in local_report['notes']))
        # Reaching the primary cluster's ONLINE primary clears from_local_copy.
        primary = F.clusterset_peer('cid-0', 'mf-main', 20, 'ic-8.4-clusterset-replica', member_index=0)
        fresh_report, _ = self.read_group(primary, source=F.conn('ic-8.4-clusterset-replica'))
        self.assertFalse(fresh_report['clusterset']['from_local_copy'])
        self.assertFalse(any('replicated copy' in n and 'ClusterSet' in n for n in fresh_report['notes']))

    def test_primary_cluster_online_secondary_keeps_metadata_copy_caveat(self):
        source = F.conn('ic-8.4-clusterset-replica')
        secondary = F.clusterset_peer('cid-0', 'mf-main', 20, 'ic-8.4-clusterset-replica', member_index=1)
        report, opened = self.read_group(secondary, source=source)
        primary_cluster = next(c for c in report['clusterset']['clusters'] if c['role'] == 'PRIMARY')
        route_uuid = secondary.scenario['variables']['server_uuid']
        route_member = next(member for member in primary_cluster['members'] if member['server_uuid'] == route_uuid)
        self.assertEqual(opened, ['remote'])
        self.assertEqual((route_member['state'], route_member['role']), ('ONLINE', 'SECONDARY'))
        self.assertTrue(primary_cluster['quorum'])
        self.assertNotEqual(primary_cluster['primary']['server_uuid'], route_uuid)
        self.assertTrue(report['clusterset']['from_local_copy'])
        self.assertTrue(any('replicated copy' in n and 'may be behind' in n for n in report['notes']))

    def test_candidate_for_another_cluster_is_reused_without_a_second_login(self):
        for primary_reachable in (False, True):
            with self.subTest(primary_reachable=primary_reachable):
                source = F.conn('ic-8.4-clusterset-three')
                unexpected = F.clusterset_peer(scenario_name='ic-8.4-clusterset-three')
                main_uuid = F._cs_instances('cid-0', 'mf-main', 20)[0][3]
                profiles = [self.CURRENT,
                            dict(self.REMOTE, id='unexpected', name='Unexpected DR',
                                 topology_identity={'server_uuid': main_uuid}),
                            dict(self.REMOTE, id='primary', name='Main primary',
                                 topology_identity={'server_uuid': main_uuid})]
                primary = (F.clusterset_peer('cid-0', 'mf-main', 20, 'ic-8.4-clusterset-three')
                           if primary_reachable else ConnectorError('ERROR 2003: unreachable'))
                report, opened = self.read_peers({'unexpected': unexpected, 'primary': primary}, profiles, source)
                clusters = {c['name']: c for c in report['clusterset']['clusters']}
                self.assertEqual(opened, ['unexpected', 'primary'])
                self.assertEqual(clusters['drcluster']['route']['profile_id'], 'unexpected')
                self.assertEqual(clusters['drcluster']['status'], 'OK')
                self.assertEqual(clusters['drcluster']['channel']['receiver_state'], 'ON')
                self.assertEqual(sum("Variable_name IN ('server_uuid')" in sql for sql, _ in unexpected.calls), 1)
                self.assertTrue(unexpected.closed)
                if primary_reachable:
                    self.assertEqual(clusters['maincluster']['route']['profile_id'], 'primary')
                    self.assertFalse(report['clusterset']['from_local_copy'])
                else:
                    self.assertIsNone(clusters['maincluster']['route'])

    def test_other_cluster_search_skips_known_aliases_of_already_read_cluster(self):
        source = F.conn('ic-8.4-clusterset-three')
        main = F.clusterset_peer('cid-0', 'mf-main', 20, 'ic-8.4-clusterset-three')
        dr = F.clusterset_peer(scenario_name='ic-8.4-clusterset-three')
        main_uuid = F._cs_instances('cid-0', 'mf-main', 20)[0][3]
        profiles = [self.CURRENT,
                    dict(self.REMOTE, id='primary', topology_identity={'server_uuid': main_uuid}),
                    dict(self.REMOTE, id='primary-alias', topology_identity={'server_uuid': main_uuid}),
                    self.dr_profile('dr', 0)]
        report, opened = self.read_peers({'primary': main, 'dr': dr}, profiles, source)
        self.assertEqual(opened, ['primary', 'dr'])
        self.assertTrue(all(c['status'] == 'OK' for c in report['clusterset']['clusters']))

    def test_other_cluster_search_skips_known_aliases_after_three_failed_opens(self):
        source = F.conn('ic-8.4-clusterset-three')
        main_uuid = F._cs_instances('cid-0', 'mf-main', 20)[0][3]
        profiles = [self.CURRENT] + [dict(self.REMOTE, id='main-%d' % i,
                    topology_identity={'server_uuid': main_uuid}) for i in range(4)] + [self.dr_profile('dr', 0)]
        peers = {'main-%d' % i: ConnectorError('ERROR 2003: unreachable') for i in range(3)}
        peers['dr'] = F.clusterset_peer(scenario_name='ic-8.4-clusterset-three')
        report, opened = self.read_peers(peers, profiles, source)
        self.assertEqual(opened, ['main-0', 'main-1', 'main-2', 'dr'])
        dr = next(c for c in report['clusterset']['clusters'] if c['name'] == 'drcluster')
        self.assertEqual(dr['status'], 'OK')

    def test_exhausted_cluster_never_probes_an_unclassified_profile(self):
        source = F.conn('ic-8.4-clusterset-three')
        main_uuid = F._cs_instances('cid-0', 'mf-main', 20)[0][3]
        profiles = [self.CURRENT] + [dict(self.REMOTE, id='main-%d' % i,
                    topology_identity={'server_uuid': main_uuid}) for i in range(3)]
        profiles += [self.dr_profile('unknown'), self.dr_profile('dr', 0)]
        main = F.clusterset_peer('cid-0', 'mf-main', 20, 'ic-8.4-clusterset-three')
        peers = {'main-%d' % i: ConnectorError('ERROR 2003: unreachable') for i in range(3)}
        peers.update(unknown=main, dr=F.clusterset_peer(scenario_name='ic-8.4-clusterset-three'))
        report, opened = self.read_peers(peers, profiles, source)
        self.assertEqual(opened, ['main-0', 'main-1', 'main-2', 'dr'])
        primary = next(c for c in report['clusterset']['clusters'] if c['role'] == 'PRIMARY')
        self.assertIsNone(primary['route'])
        self.assertEqual(primary['read'], 'failed')
        self.assertEqual(primary['status'], 'NOT_READ')
        self.assertEqual(len(main.calls), 0)
        self.assertFalse(main.closed)

    def test_expired_cluster_budget_does_not_probe_unclassified_profiles(self):
        source = F.conn('ic-8.4-clusterset-three')
        main_uuid = F._cs_instances('cid-0', 'mf-main', 20)[0][3]
        profiles = [self.CURRENT,
                    dict(self.REMOTE, id='down', topology_identity={'server_uuid': main_uuid}),
                    self.dr_profile('unknown'), self.dr_profile('dr', 0)]
        main = F.clusterset_peer('cid-0', 'mf-main', 20, 'ic-8.4-clusterset-three')
        dr = F.clusterset_peer(scenario_name='ic-8.4-clusterset-three')
        clock, opened = [0.0], []

        def opener(profile):
            opened.append(profile['id'])
            if profile['id'] == 'down':
                clock[0] = m.CLUSTERSET_ROUTE_TIMEOUT + 1.0
                raise ConnectorError('ERROR 2003: unreachable')
            return main if profile['id'] == 'unknown' else dr

        with mock.patch.object(m.time, 'monotonic', side_effect=lambda: clock[0]):
            report = m.read_status(source, profiles=profiles, open_profile=opener, current_profile_id='local')
        self.assertEqual(opened, ['down', 'dr'])
        primary = next(c for c in report['clusterset']['clusters'] if c['role'] == 'PRIMARY')
        self.assertIsNone(primary['route'])
        self.assertEqual(primary['read'], 'failed')
        self.assertEqual(len(main.calls), 0)
        self.assertFalse(main.closed)
        self.assertTrue(dr.closed)

    def test_primary_metadata_refresh_can_remove_cluster_during_its_primary_hop(self):
        source = F.conn('ic-8.4-clusterset-three')
        secondary = F.clusterset_peer(scenario_name='ic-8.4-clusterset-three', member_index=1)
        primary = F.clusterset_peer('cid-0', 'mf-main', 20, 'ic-8.4-clusterset-three')
        rows = primary.scenario['tables']['mysql_innodb_cluster_metadata.v2_cs_members']
        rows[:] = [row for row in rows if row[1] != 'cid-2']
        profiles = [self.CURRENT,
                    dict(self.REMOTE, id='down', topology_identity={
                        'server_uuid': F._cs_instances('cid-0', 'mf-main', 20)[0][3]}),
                    self.dr_profile('secondary', 1), self.dr_profile('rerouted', 0)]
        peers = {'down': ConnectorError('ERROR 2003: unreachable'), 'secondary': secondary, 'rerouted': primary}
        report, opened = self.read_peers(peers, profiles, source)
        self.assertEqual(opened, ['down', 'secondary', 'rerouted'])
        self.assertEqual({c['name'] for c in report['clusterset']['clusters']}, {'maincluster', 'mfcluster'})
        main = next(c for c in report['clusterset']['clusters'] if c['role'] == 'PRIMARY')
        self.assertEqual(main['route']['profile_id'], 'rerouted')
        self.assertEqual(main['status'], 'OK')
        self.assertFalse(report['clusterset']['from_local_copy'])
        self.assertTrue(secondary.closed and primary.closed)

    def test_failed_route_notes_identify_saved_member_and_failed_step(self):
        cases = [('opening', None, ('open', 'connection')),
                 ('identity', "Variable_name IN ('server_uuid')", ('server_uuid', 'identity')),
                 ('members', 'FROM performance_schema.replication_group_members',
                  ('members', 'replication_group_members')),
                 ('fencing', "Variable_name IN ('super_read_only')", ('fencing', 'super_read_only')),
                 ('channel', 'FROM performance_schema.replication_connection_status',
                  ('channel', 'replication_connection_status'))]
        for step, fragment, words in cases:
            with self.subTest(step=step):
                error = ConnectorError('ERROR 2003: connection refused; password=private-test-value')
                peer = F.clusterset_peer()
                if fragment is None:
                    peer = error
                else:
                    peer.scenario['query_errors'] = {fragment: error}
                report, _ = self.read_group(peer)
                notes = report['notes']
                self.assertTrue(any('DR member' in n and any(word in n.lower() for word in words)
                                    for n in notes), notes)
                self.assertNotIn('private-test-value', '\n'.join(notes))

    def test_failed_metadata_note_identifies_saved_member_and_step(self):
        source = F.conn('ic-8.4-clusterset-replica')
        primary = F.clusterset_peer('cid-0', 'mf-main', 20, 'ic-8.4-clusterset-replica')
        primary.scenario['tables']['mysql_innodb_cluster_metadata.v2_cs_members'] = (
            ConnectorError('ERROR 1142: denied; password=private-test-value'))
        report, _ = self.read_group(primary, source=source)
        self.assertTrue(any('DR member' in n and 'metadata' in n.lower() for n in report['notes']))
        self.assertNotIn('private-test-value', '\n'.join(report['notes']))

    def test_failed_optional_channel_read_note_identifies_saved_member(self):
        primary = F.clusterset_peer()
        primary.scenario['tables']['performance_schema.replication_applier_status_by_worker'] = (
            ConnectorError('ERROR 1142: denied; password=private-test-value'))
        report, _ = self.read_group(primary)
        self.assertTrue(any('DR member' in n and ('channel' in n.lower() or 'applier' in n.lower())
                            for n in report['notes']))
        self.assertNotIn('private-test-value', '\n'.join(report['notes']))

    def test_primary_metadata_can_add_a_cluster_in_the_same_pass(self):
        source = F.conn('ic-8.4-clusterset-replica')
        primary = F.clusterset_peer('cid-0', 'mf-main', 20, 'ic-8.4-clusterset-replica')
        primary.scenario['tables']['mysql_innodb_cluster_metadata.v2_cs_members'] += F._cs(
            ('cid-2', 'new-replica', 'REPLICA', False))
        profiles = [self.CURRENT, dict(self.REMOTE, id='primary', host='mf-main-1'),
                    dict(self.REMOTE, id='new-replica', host='mf-dr-1')]
        opened = []

        def opener(profile):
            opened.append(profile['id'])
            return primary if profile['id'] == 'primary' else F.clusterset_peer()

        report = m.read_status(source, profiles=profiles, open_profile=opener, current_profile_id='local')
        self.assertEqual(opened, ['primary', 'new-replica'])
        new = next(c for c in report['clusterset']['clusters'] if c['name'] == 'new-replica')
        self.assertEqual(new['route']['profile_id'], 'new-replica')
        self.assertEqual(new['status'], 'OK')

    def test_invalidated_null_role_does_not_claim_replication_or_writes_stopped(self):
        for scenario in ('ic-8.4-clusterset-invalidated', 'ic-8.4-clusterset-local-invalidated'):
            with self.subTest(scenario=scenario):
                report = F.report(scenario)
                cluster = next(c for c in report['clusterset']['clusters'] if c['invalidated'])
                self.assertIsNone(cluster['role'])
                self.assertEqual(cluster['status'], 'INVALIDATED')
                finding = next(f for f in report['findings'] if f['id'] == 'clusterset_invalidated')
                self.assertIn('does not prove that replication or writes stopped', finding['text'])
                self.assertNotIn('no longer replicates', finding['text'])

    def test_routers_and_routing_options_are_stored_configuration(self):
        source = F.conn('ic-8.4-clusterset-primary')
        source.scenario['tables']['mysql_innodb_cluster_metadata.v2_cs_members'][0][7] = (
            '{"target_cluster":"primary","invalidated_cluster_policy":"drop_all"}')
        source.scenario['clusterset_routers'] = [
            [1, 'cs-1', None, 'router-main', 'router-host', '8.4.10', '{"target_cluster":"group-id"}'],
            [2, None, 'cid-2', 'router-dr', 'dr-host', '8.4.10', '{}'],
            [3, 'another-set', 'cid-2', 'not-ours', 'other', '8.4.10', '{}']]
        cs = m.read_status(source)['clusterset']
        self.assertEqual(cs['routing_options'], {'stored_configuration': True,
            'options': {'target_cluster': 'primary', 'invalidated_cluster_policy': 'drop_all'}})
        self.assertEqual([r['name'] for r in cs['routers']], ['dr-host::router-dr', 'router-host::router-main'])
        self.assertEqual(cs['routers'][1]['options'], {'target_cluster': 'group-id'})
        self.assertTrue(all(r['stored_configuration'] for r in cs['routers']))
        self.assertFalse(any('r.attributes' in sql for sql, _ in source.calls if 'r.options' in sql))
        assert_read_only(self, source, m.CLUSTER_TAG)

    def test_router_configuration_uses_the_selected_primary_metadata_source(self):
        source = F.conn('ic-8.4-clusterset-replica')
        source.scenario['clusterset_routers'] = [[1, 'cs-1', None, 'stale-router', 'old', '8.4', '{}']]
        peer = F.clusterset_peer('cid-0', 'mf-main', 20, 'ic-8.4-clusterset-replica')
        peer.scenario['clusterset_routers'] = [[2, 'cs-1', None, 'current-router', 'new', '9.7', '{}']]
        for row in peer.scenario['tables']['mysql_innodb_cluster_metadata.v2_cs_members']:
            row[7] = '{"target_cluster":"primary"}'
        report, _ = self.read_group(peer, source=source)
        cs = report['clusterset']
        self.assertEqual([r['name'] for r in cs['routers']], ['new::current-router'])
        self.assertEqual(cs['routing_options']['options'], {'target_cluster': 'primary'})
        self.assertFalse(any(sql.startswith(m.CLUSTER_TAG + 'SELECT r.router_id, r.clusterset_id')
                             for sql, _ in source.calls))

    def test_unread_routers_are_distinct_from_an_empty_registration_list(self):
        self.assertEqual(F.report('ic-8.4-clusterset-primary')['clusterset']['routers'], [])
        source = F.conn('ic-8.4-clusterset-primary')
        source.scenario['clusterset_routers'] = ConnectorError('ERROR 1142: denied')
        report = m.read_status(source)
        self.assertIsNone(report['clusterset']['routers'])
        self.assertTrue(any('Stored Router configuration' in note for note in report['notes']))

    def test_invalid_router_json_is_unknown_with_a_note(self):
        for value in ('not-json', 'null', '[]', '{"number": NaN}', '{"number": 1e309}'):
            with self.subTest(value=value):
                source = F.conn('ic-8.4-clusterset-primary')
                source.scenario['tables']['mysql_innodb_cluster_metadata.v2_cs_members'][0][7] = value
                report = m.read_status(source)
                self.assertIsNone(report['clusterset']['routing_options']['options'])
                self.assertTrue(any('JSON object' in note for note in report['notes']))
                no_nan(self, report)

    def test_sql_null_router_options_are_empty_overrides_at_both_levels(self):
        # The metadata view uses COALESCE(options, '{}'); SQL NULL is no
        # override, unlike unreadable or malformed JSON (including JSON null).
        source = F.conn('ic-8.4-clusterset-primary')
        source.scenario['tables']['mysql_innodb_cluster_metadata.v2_cs_members'][0][7] = None
        source.scenario['clusterset_routers'] = [[1, 'cs-1', None, '', 'mf-cs-router', '9.7.0', None]]
        report = m.read_status(source)
        cs = report['clusterset']
        self.assertEqual(cs['routing_options']['options'], {})
        self.assertEqual(cs['routers'][0]['options'], {})
        self.assertFalse(any('JSON object' in note for note in report['notes']))

    def test_router_name_uses_shell_label_even_when_registered_name_is_empty(self):
        source = F.conn('ic-8.4-clusterset-primary')
        source.scenario['clusterset_routers'] = [
            [1, 'cs-1', None, '', 'mf-cs-router', '8.4.10', '{}'],
            [2, 'cs-1', None, 'reporting', 'mf-cs-router', '8.4.10', '{}']]
        routers = m.read_status(source)['clusterset']['routers']
        self.assertEqual([r['name'] for r in routers], ['mf-cs-router::', 'mf-cs-router::reporting'])
        self.assertTrue(all(r['address'] == 'mf-cs-router' for r in routers))
        self.assertTrue(all(r['version'] == '8.4.10' for r in routers))

    def test_router_options_project_only_routing_keys_at_both_levels(self):
        source = F.conn('ic-8.4-clusterset-primary')
        routing = {'target_cluster': 'primary', 'invalidated_cluster_policy': 'accept_ro',
                   'stats_updates_frequency': 0, 'use_replica_primary_as_rw': False,
                   'read_only_targets': 'secondaries', 'tags': {'region': 'west'}, 'guideline': None}
        stored = dict(routing, Configuration={'routing:bootstrap_rw': {'bind_port': 6446}},
                      bootstrapTargetType='clusterset', router_require_enforce=True)
        source.scenario['tables']['mysql_innodb_cluster_metadata.v2_cs_members'][0][7] = json.dumps(stored)
        source.scenario['clusterset_routers'] = [
            [1, 'cs-1', None, '', 'mf-cs-router', '8.4.10', json.dumps(stored)]]
        cs = m.read_status(source)['clusterset']
        self.assertEqual(cs['routing_options']['options'], routing)
        self.assertEqual(cs['routers'][0]['options'], routing)
        self.assertNotIn('Configuration', json.dumps(cs))
        self.assertNotIn('bootstrapTargetType', json.dumps(cs))
        self.assertNotIn('router_require_enforce', json.dumps(cs))

    def test_router_options_do_not_invent_absent_routing_defaults(self):
        source = F.conn('ic-8.4-clusterset-primary')
        stored = '{"Configuration":{"DEFAULT":{"name":"stored-config-only"}}}'
        source.scenario['tables']['mysql_innodb_cluster_metadata.v2_cs_members'][0][7] = stored
        source.scenario['clusterset_routers'] = [[1, 'cs-1', None, '', 'mf-cs-router', '8.4.10', stored]]
        cs = m.read_status(source)['clusterset']
        self.assertEqual(cs['routing_options']['options'], {})
        self.assertEqual(cs['routers'][0]['options'], {})

    def test_unknown_host_is_unreachable_but_auth_and_timeout_are_unknown(self):
        main_instances = F._cs_instances('cid-0', 'mf-main', 20)
        profiles = [self.CURRENT] + [
            dict(self.REMOTE, id='main-%d' % idx, name='Main %d' % idx,
                 host='mf-main-%d' % (idx + 1), port=3306)
            for idx in range(len(main_instances))]
        shell = {'path': '/usr/bin/mysqlsh', 'version': '9.7.1', 'version_tuple': [9, 7, 1]}
        for error, expected in (
            (ConnectorError("mysql client exited 1: ERROR 2005 (HY000): Unknown MySQL server host 'mf-main-1' (-2)"), True),
            (ConnectorError("mysql client exited 1: ERROR 1045 (28000): Access denied for user 'mf_admin'"), False),
            (ConnectorError("mysql client exited 1: ERROR 2026 (HY000): TLS/SSL error"), False),
            (ConnectorError("Query timed out after 5.0s: SHOW GLOBAL VARIABLES"), False),
        ):
            with self.subTest(error=str(error)):
                source = F.conn('ic-8.4-clusterset-replica')
                peers = {'main-%d' % idx: error for idx in range(len(main_instances))}
                report, _ = self.read_peers(peers, profiles, source)
                actions = {a['id']: a for a in m.overview_actions(report, {'shell': shell})}
                self.assertEqual(actions['force_primary_cluster:mfcluster']['enabled'], expected)
        # A 2005 error on a saved endpoint that does not match metadata never marks a member unreachable.
        wrong_host = [self.CURRENT] + [
            dict(self.REMOTE, id='wrong-%d' % idx, host='other-host-%d' % idx, port=3306)
            for idx in range(len(main_instances))]
        peers = {'wrong-%d' % idx: ConnectorError("ERROR 2005 (HY000): Unknown MySQL server host 'other-host'")
                 for idx in range(len(main_instances))}
        report, _ = self.read_peers(peers, wrong_host, F.conn('ic-8.4-clusterset-replica'))
        actions = {a['id']: a for a in m.overview_actions(report, {'shell': shell})}
        self.assertFalse(actions['force_primary_cluster:mfcluster']['enabled'])

    def test_connection_failure_requires_one_fully_qualified_metadata_endpoint(self):
        for metadata_address in ('shared-db', 'shared-db:3306'):
            for cached in (False, True):
                with self.subTest(address=metadata_address, cached=cached):
                    source = F.conn('ic-8.4-clusterset-replica')
                    instances = [row for row in source.scenario['tables'][
                        'mysql_innodb_cluster_metadata.v2_instances'] if row[1] == 'cid-0']
                    for row in instances[:2]:
                        row[4] = row[5] = metadata_address
                    profile = dict(self.REMOTE, id='failed', host='shared-db', port=3306)
                    if cached:
                        profile['topology_identity'] = {'server_uuid': instances[0][3]}
                    report, opened = self.read_peers({'failed': ConnectorError('ERROR 2003: connection refused')},
                                                     [self.CURRENT, profile], source)
                    self.assertEqual(opened, [] if metadata_address == 'shared-db' and not cached else ['failed'])
                    observations = getattr(report, '_clusterset_reachability', {}).get('maincluster', [])
                    for row in instances[:2]:
                        self.assertNotIn({'server_uuid': row[3], 'reachable': False}, observations)

        # One full, unique IPv6 endpoint may establish a connection refusal.
        source = F.conn('ic-8.4-clusterset-replica')
        instance = next(row for row in source.scenario['tables'][
            'mysql_innodb_cluster_metadata.v2_instances'] if row[1] == 'cid-0')
        instance[4] = instance[5] = '[2001:db8::7]:3307'
        profile = dict(self.REMOTE, id='v6', host='2001:db8::7', port=3307)
        report, opened = self.read_peers({'v6': ConnectorError('ERROR 2003: connection refused')},
                                         [self.CURRENT, profile], source)
        self.assertEqual(opened, ['v6'])
        observations = getattr(report, '_clusterset_reachability', {}).get('maincluster', [])
        self.assertIn({'server_uuid': instance[3], 'reachable': False}, observations)

    def test_tunnel_refusal_and_invalid_saved_port_do_not_prove_member_unreachable(self):
        for extra in ({'ssh_host': 'bastion.example'}, {'port': 0}):
            with self.subTest(profile=extra):
                source = F.conn('ic-8.4-clusterset-replica')
                instance = next(row for row in source.scenario['tables'][
                    'mysql_innodb_cluster_metadata.v2_instances'] if row[1] == 'cid-0')
                profile = dict(self.REMOTE, id='failed', host='mf-main-1', port=3306)
                profile.update(extra)
                profile['topology_identity'] = {'server_uuid': instance[3]}
                report, opened = self.read_peers({'failed': ConnectorError('ERROR 2003: connection refused')},
                                                 [self.CURRENT, profile], source)
                self.assertEqual(opened, ['failed'])
                observations = getattr(report, '_clusterset_reachability', {}).get('maincluster', [])
                self.assertNotIn({'server_uuid': instance[3], 'reachable': False}, observations)

    def test_malformed_endpoint_or_ambiguous_uuid_does_not_prove_unreachable(self):
        for case in ('bad_host', 'bad_ipv6', 'bad_uuid', 'case_duplicate_uuid'):
            with self.subTest(case=case):
                source = F.conn('ic-8.4-clusterset-replica')
                instances = [row for row in source.scenario['tables'][
                    'mysql_innodb_cluster_metadata.v2_instances'] if row[1] == 'cid-0']
                host = 'mf-main-1'
                if case == 'bad_host':
                    host = 'bad@host'
                    instances[0][4] = instances[0][5] = 'bad@host:3306'
                elif case == 'bad_ipv6':
                    host = '2001:db8::zz'
                    instances[0][4] = instances[0][5] = '[2001:db8::zz]:3306'
                elif case == 'bad_uuid':
                    instances[0][3] = 'not-a-uuid'
                else:
                    # UUIDs compare case-insensitively; a metadata duplicate
                    # must not become unique merely through letter casing.
                    instances[1][3] = instances[0][3].upper()
                profile = dict(self.REMOTE, id='failed', host=host, port=3306)
                if case in ('bad_host', 'bad_ipv6'):
                    profile['topology_identity'] = {'server_uuid': instances[0][3]}
                report, opened = self.read_peers({'failed': ConnectorError('ERROR 2003: connection refused')},
                                                 [self.CURRENT, profile], source)
                self.assertEqual(opened, ['failed'])
                observations = getattr(report, '_clusterset_reachability', {}).get('maincluster', [])
                self.assertFalse(any(row['reachable'] is False for row in observations), observations)

    def test_primary_metadata_refresh_invalidates_earlier_failure_evidence(self):
        for change in ('endpoint', 'view'):
            with self.subTest(change=change):
                source = F.conn('ic-8.4-clusterset-three')
                current = F.clusterset_peer('cid-0', 'mf-main', 20,
                                            'ic-8.4-clusterset-three', member_index=1)
                main_uuid = F._cs_instances('cid-0', 'mf-main', 20)[0][3]
                rows = current.scenario['tables']['performance_schema.replication_group_members']
                rows[0][5], rows[1][5] = 'SECONDARY', 'PRIMARY'
                current.scenario['variables']['super_read_only'] = 'OFF'
                for row in current.scenario['tables']['mysql_innodb_cluster_metadata.v2_cs_members']:
                    row[6] = '4294967298'
                if change == 'endpoint':
                    instance = next(row for row in current.scenario['tables'][
                        'mysql_innodb_cluster_metadata.v2_instances'] if row[3] == main_uuid)
                    instance[4] = instance[5] = 'new-main-address:3306'
                profiles = [self.CURRENT, dict(self.REMOTE, id='old', host='mf-main-1', port=3306,
                                              topology_identity={'server_uuid': main_uuid}),
                            dict(self.REMOTE, id='current', host='primary-alias', port=3306,
                                 topology_identity={'server_uuid': current.scenario['variables']['server_uuid']})]
                peers = {'old': ConnectorError('ERROR 2003: connection refused'), 'current': current}
                report, opened = self.read_peers(peers, profiles, source)
                self.assertEqual(opened[:2], ['old', 'current'])
                self.assertEqual(report['clusterset']['view_id'], '4294967298')
                observations = getattr(report, '_clusterset_reachability', {}).get('maincluster', [])
                self.assertNotIn({'server_uuid': main_uuid, 'reachable': False}, observations)

    def test_offline_route_probe_continues_to_online_member(self):
        offline = F.clusterset_peer(member_index=1)
        off_uuid = F._cs_instances('cid-2', 'mf-dr', 30)[1][3]
        offline.scenario['tables']['performance_schema.replication_group_members'] = [
            ['group-dr', off_uuid, 'mf-dr-2', '3306', 'OFFLINE', '', '8.4.13', 'MySQL']]
        online = F.clusterset_peer(member_index=0)
        profiles = [self.CURRENT, self.dr_profile('dr-offline', 1), self.dr_profile('dr-online', 0)]
        report, opened = self.read_peers({'dr-offline': offline, 'dr-online': online}, profiles)
        self.assertEqual(opened, ['dr-offline', 'dr-online'])
        dr = next(c for c in report['clusterset']['clusters'] if c['name'] == 'drcluster')
        self.assertEqual(dr['route']['profile_id'], 'dr-online')
        self.assertTrue(dr['quorum'])
        self.assertEqual(dr['status'], 'OK')

    def test_stale_old_primary_refreshes_metadata_from_higher_view_promoted_primary(self):
        source = F.conn('ic-8.4-clusterset-primary')
        for row in source.scenario['tables']['mysql_innodb_cluster_metadata.v2_cs_members']:
            row[6] = '3'
        source.scenario['variables']['super_read_only'] = 'ON'
        promoted = F.clusterset_peer()
        promoted.scenario['variables']['super_read_only'] = 'OFF'
        promoted.scenario['extra']['performance_schema.replication_connection_status'] = []
        for row in promoted.scenario['tables']['mysql_innodb_cluster_metadata.v2_cs_members']:
            row[6] = '4294967299'
            if row[1] == 'cid-2':
                row[3], row[4] = 'PRIMARY', '0'
            elif row[1] == 'cid-1':
                row[3], row[4] = None, '1'
        report, opened = self.read_group(promoted, source=source)
        self.assertEqual(opened, ['remote'])
        cs = report['clusterset']
        self.assertEqual(cs['view_id'], '4294967299')
        self.assertEqual(cs['primary_cluster'], 'drcluster')
        self.assertFalse(cs['from_local_copy'])
        clusters = {c['name']: c for c in cs['clusters']}
        self.assertEqual(clusters['drcluster']['role'], 'PRIMARY')
        self.assertIs(clusters['drcluster']['channel']['present'], False)
        self.assertEqual(clusters['drcluster']['status'], 'OK')
        self.assertTrue(clusters['mfcluster']['invalidated'])
        self.assertEqual(clusters['mfcluster']['status'], 'INVALIDATED')
        self.assertTrue(any(f['id'] == 'clusterset_invalidated' for f in report['findings']))

    def test_raw_primary_sro_on_prevents_healthy_even_with_healthy_replicas(self):
        from test_chart_data import validate
        source = F.conn('ic-8.4-clusterset-primary')
        source.scenario['variables']['super_read_only'] = 'ON'
        source.scenario['tables'][self.MEMBER_ACTIONS] = [[1]]
        peer = F.clusterset_peer()
        peer.scenario['extra']['performance_schema.replication_connection_status'] = [
            ['ON', '0', '', F.SERVER_UUIDS[0]]]
        report, opened = self.read_group(peer, source=source)
        primary, replica = report['clusterset']['clusters']
        self.assertEqual(opened, ['remote'])
        self.assertIs(primary['super_read_only'], True)
        self.assertIs(primary['disable_super_read_only_if_primary'], True)
        self.assertIs(primary['fenced_writes'], False)
        self.assertEqual(primary['status'], 'NOT_OK')
        self.assertIn('super_read_only ON', primary['status_reason'])
        self.assertIn('writes are currently blocked', primary['status_reason'])
        self.assertEqual(replica['status'], 'OK')
        self.assertEqual(report['clusterset']['status'], 'UNAVAILABLE')
        self.assertIn('super_read_only ON', report['clusterset']['status_reason'])
        validate(report, SCHEMA)

    def test_replica_source_uuid_mismatch_is_misconfigured_and_repair_eligible(self):
        from test_chart_data import validate
        from test_cluster_actions import clusterset_checks
        primary_uuid = F.SERVER_UUIDS[0]
        wrong_uuid = F.SERVER_UUIDS[1]
        for case, source_uuid, expected_status, repair_enabled in (
            ('matching', primary_uuid, 'OK', False),
            ('matching_upper', primary_uuid.upper(), 'OK', False),
            ('wrong_uuid', wrong_uuid, 'OK_MISCONFIGURED', True),
            ('empty_uuid', '', 'OK_MISCONFIGURED', True),
        ):
            with self.subTest(case=case):
                source = F.conn('ic-8.4-clusterset-primary')
                source.scenario['tables'][self.MEMBER_ACTIONS] = [[1]]
                peer = F.clusterset_peer()
                # Configured host:port matches the primary, so only SOURCE_UUID exposes the mismatch.
                peer.scenario['extra']['performance_schema.replication_connection_configuration'] = [['mf-ic-1', '3306']]
                peer.scenario['extra']['performance_schema.replication_connection_status'] = [
                    ['ON', '0', '', source_uuid]]
                report, opened = self.read_group(peer, source=source)
                self.assertEqual(opened, ['remote'])
                replica = report['clusterset']['clusters'][1]
                self.assertEqual(replica['status'], expected_status)
                self.assertEqual(replica['channel']['source_uuid'], source_uuid)
                self.assertEqual(replica['channel']['status'], 'OK' if expected_status == 'OK' else 'MISCONFIGURED')
                self.assertEqual(report['clusterset']['status'], 'HEALTHY' if expected_status == 'OK' else 'AVAILABLE')
                self.assertTrue(any('SOURCE_UUID' in sql for sql, _ in peer.calls))
                checks = clusterset_checks(report)
                actions = {a['id']: a for a in m.overview_actions(report, checks)}
                self.assertEqual(actions['repair_cluster:drcluster']['enabled'], repair_enabled)
                self.assertEqual(actions['set_primary_cluster:drcluster']['enabled'], not repair_enabled)
                if repair_enabled:
                    self.assertIn('source UUID', replica['status_reason'])
                    self.assertIn(primary_uuid, replica['status_reason'])
                    plan = m.plan_action(report, 'repair_cluster:drcluster', checks=checks)
                    self.assertIn('.rejoinCluster("drcluster")', plan['statements'][0])
                validate(report, SCHEMA)
                assert_read_only(self, peer, m.CLUSTER_TAG)

    def test_primary_clusterset_replication_channel_must_be_absent_or_stopped(self):
        from test_chart_data import validate
        cases = (
            ('absent', [], [], [], 'OK', 'HEALTHY', 'MISSING'),
            ('stopped', [['OFF', '0', '', F.SERVER_UUIDS[1]]], [['OFF']], [[1, 'OFF', 0, '']],
             'OK', 'HEALTHY', 'STOPPED'),
            ('running', [['ON', '0', '', F.SERVER_UUIDS[1]]], [['ON']], [[1, 'ON', 0, '']],
             'OK_MISCONFIGURED', 'AVAILABLE', 'MISCONFIGURED'),
            ('connecting', [['CONNECTING', '0', '', F.SERVER_UUIDS[1]]], [['ON']], [[1, 'ON', 0, '']],
             'OK_MISCONFIGURED', 'AVAILABLE', 'MISCONFIGURED'),
            ('applier_running', [['OFF', '0', '', F.SERVER_UUIDS[1]]], [['ON']], [[1, 'ON', 0, '']],
             'OK_MISCONFIGURED', 'AVAILABLE', 'MISCONFIGURED'),
            ('receiver_error', [['OFF', '2003', 'connect failed', F.SERVER_UUIDS[1]]], [['OFF']], [[1, 'OFF', 0, '']],
             'OK_MISCONFIGURED', 'AVAILABLE', 'MISCONFIGURED'),
            ('worker_error', [['OFF', '0', '', F.SERVER_UUIDS[1]]], [['OFF']], [[1, 'OFF', 1062, 'dup']],
             'OK_MISCONFIGURED', 'AVAILABLE', 'MISCONFIGURED'),
        )
        for name, conn_rows, applier_rows, worker_rows, p_status, set_status, ch_status in cases:
            with self.subTest(case=name):
                source = F.conn('ic-8.4-clusterset-primary')
                source.scenario['tables'][self.MEMBER_ACTIONS] = [[1]]
                source.scenario['tables']['performance_schema.replication_connection_status'] = conn_rows
                source.scenario['tables']['performance_schema.replication_applier_status'] = applier_rows
                source.scenario['tables']['performance_schema.replication_connection_configuration'] = [
                    ['old-primary', '3306']] if conn_rows else []
                source.scenario['tables']['performance_schema.replication_applier_status_by_worker'] = worker_rows
                peer = F.clusterset_peer()
                peer.scenario['extra']['performance_schema.replication_connection_status'] = [
                    ['ON', '0', '', F.SERVER_UUIDS[0]]]
                report, opened = self.read_group(peer, source=source)
                self.assertEqual(opened, ['remote'])
                primary = report['clusterset']['clusters'][0]
                self.assertEqual(primary['status'], p_status)
                self.assertEqual(primary['channel']['status'], ch_status)
                self.assertIs(primary['channel']['present'], bool(conn_rows))
                self.assertIsNone(report['clusterset']['channel'])
                self.assertEqual(report['clusterset']['status'], set_status)
                if p_status == 'OK_MISCONFIGURED':
                    self.assertIn('Unexpected clusterset_replication channel', primary['status_reason'])
                    self.assertEqual(report['clusterset']['status_reason'],
                                     'Primary Cluster available but requires attention.')
                validate(report, SCHEMA)
                assert_read_only(self, source, m.CLUSTER_TAG)

    def test_remote_primary_unexpected_channel_survives_metadata_refresh(self):
        from test_chart_data import validate
        source = F.conn('ic-8.4-clusterset-replica')
        main_uuid = F._cs_instances('cid-0', 'mf-main', 20)[0][3]
        source.scenario['extra']['performance_schema.replication_connection_status'] = [['ON', '0', '', main_uuid]]
        peer = F.clusterset_peer('cid-0', 'mf-main', 20, 'ic-8.4-clusterset-replica')
        peer.scenario['tables'][self.MEMBER_ACTIONS] = [[1]]
        peer.scenario['extra']['performance_schema.replication_connection_status'] = [
            ['ON', '0', '', '99999999-2222-11f0-8a00-0242ac130002']]
        report, opened = self.read_group(peer, source=source)
        self.assertEqual(opened, ['remote'])
        primary = report['clusterset']['clusters'][0]
        self.assertFalse(report['clusterset']['from_local_copy'])
        self.assertIs(primary['channel']['present'], True)
        self.assertEqual(primary['channel']['status'], 'MISCONFIGURED')
        self.assertEqual(primary['status'], 'OK_MISCONFIGURED')
        self.assertEqual(report['clusterset']['status'], 'AVAILABLE')
        self.assertEqual(report['clusterset']['status_reason'],
                         'Primary Cluster available but requires attention.')
        validate(report, SCHEMA)


class TestActionCatalog(unittest.TestCase):
    """Catalog identities never disappear when state, tools or connection change."""
    SHELL = {'path': '/opt/mysqlsh/bin/mysqlsh', 'version': '8.4.6', 'version_tuple': [8, 4, 6]}
    MEMBER_IDS = {
        'group_replication': {'switchover', 'stop_member', 'start_member'},
        'innodb_cluster': {'switchover', 'stop_member', 'start_member', 'rejoin_instance'},
        'galera': {'desync_node', 'resync_node', 'bootstrap_pc'},
    }
    MODES = {'switch_to_single_primary', 'switch_to_multi_primary'}
    CLUSTERSET = {'set_primary_cluster', 'force_primary_cluster', 'rejoin_cluster', 'repair_cluster',
                  'remove_cluster', 'remove_cluster_keep', 'set_routing_option', 'create_replica_cluster',
                  'dissolve_clusterset', 'check_clusterset'}

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = mock.patch.dict(os.environ, {'HOME': temp.name, 'USERPROFILE': temp.name,
                                          'MYFLAMES_SECRET_BACKEND': 'vault'})
        env.start()
        self.addCleanup(env.stop)

    def actions(self, report, target=None, checks=None):
        checks = {'shell': self.SHELL} if checks is None else checks
        actions = (m.overview_actions(report, checks) if target is None else
                   m.available_actions(report, target['key'], checks))
        return {a['id']: a for a in actions}

    def assert_refused(self, report, action, target=None, checks=None):
        checks = {'shell': self.SHELL} if checks is None else checks
        with self.assertRaises(m.ActionUnavailable) as error:
            m.plan_action(report, action['id'], target['key'] if target else None, checks)
        self.assertEqual(str(error.exception), action['why_not'] + ' ' + action['fix'])

    def test_every_kind_and_state_has_the_complete_catalog_and_shape(self):
        from myflames import dbops, reviewed_writes
        from test_chart_data import validate
        for scenario in F.ALL:
            report = m.with_actions(F.report(scenario), shell=self.SHELL)
            with self.subTest(scenario=scenario):
                validate(report, SCHEMA)
                kind = report['kind']
                expected = self.MEMBER_IDS.get(kind, set())
                if kind == 'galera' and report['engine'] != 'mariadb':
                    expected = set()
                overview = self.MODES | self.CLUSTERSET if kind == 'innodb_cluster' else (
                    self.MODES if kind == 'group_replication' else set())
                self.assertEqual({a['id'].split(':')[0] for a in report['actions']}, overview)
                if 'clusterset' in report:
                    self.assertNotIn('actions', report['clusterset'], 'one home per action scope')
                all_actions = list(report['actions'])
                for target in report['members']:
                    self.assertEqual({a['id'] for a in target['actions']}, expected)
                    for action in target['actions']:
                        self.assertEqual(action['scope'], 'member')
                        self.assertEqual((action['member_key'], action['member_name'], action['member_uuid']),
                                         (target['key'], target['name'], target['uuid']))
                    all_actions.extend(target['actions'])
                for action in report['actions']:
                    base = action['id'].split(':', 1)[0]
                    self.assertEqual(action['scope'], 'clusterset' if base in self.CLUSTERSET else 'overview')
                    self.assertIsNone(action['member_key'])
                    self.assertIsNone(action['member_name'])
                    self.assertIsNone(action['member_uuid'])
                for action in all_actions:
                    base = action['id'].split(':', 1)[0]
                    extra = {'options'} if base == 'set_routing_option' else set()
                    self.assertEqual(set(action), set(SCHEMA['$defs']['action']['required']) | extra)
                    self.assertEqual(action['schema_version'], 'cluster-action-1.1')
                    self.assertIn(action['kind'], dbops.KINDS)
                    self.assertIn(action['via'], ('sql', 'udf', 'mysqlsh'))
                    target_keys = {'self', 'member_name'}
                    if action['scope'] == 'clusterset':
                        target_keys |= {'profile_id', 'server_uuid'}
                    self.assertEqual(set(action['target']), target_keys)
                    self.assertEqual((action['missing_privileges'], action['grant_sql'],
                                      action['privileges_unreadable']), ([], None, False))
                    if action['enabled']:
                        self.assertIn(action['why_not'], (None, m._SHELL_RUNTIME_PRIVILEGES))
                        self.assertIsNone(action['fix'])
                        parameters = ({'option': 'target_cluster', 'value': 'primary'}
                                      if base == 'set_routing_option' else None)
                        plan = m.plan_action(report, action['id'], action['member_key'], {'shell': self.SHELL},
                                             parameters=parameters)
                        if base == 'check_clusterset':
                            # The read-only Shell lane consumes this explicit
                            # plan kind; the SQL classifier does not parse JS.
                            self.assertEqual((action['kind'], plan['kind']), ('read', 'read'))
                            self.assertTrue(plan['read_only'])
                            self.assertIsNone(plan['typed_guard'])
                            self.assertNotIn(m.CLUSTER_ACTION_TAG, plan['statements'][0])
                        else:
                            self.assertEqual(action['kind'], reviewed_writes.worst_kind(plan['statements'])['kind'])
                    else:
                        self.assertTrue(action['why_not'])
                        self.assertTrue(action['fix'])
                        self.assertNotIn(chr(0x2014), action['why_not'] + action['fix'])

    def test_every_disabled_action_refuses_with_the_catalog_text(self):
        for scenario in F.ALL:
            report = F.report(scenario)
            for target in [None] + report['members']:
                for action in self.actions(report, target).values():
                    if not action['enabled']:
                        with self.subTest(scenario=scenario, member=(target or {}).get('key'), action=action['id']):
                            self.assert_refused(report, action, target)

    def test_primary_and_secondary_connections_keep_identical_member_buttons(self):
        primary = F.report('ic-8.4-healthy')
        primary_member = next(x for x in primary['members'] if x['role'] == 'PRIMARY' and x['state'] == 'ONLINE')
        for target in primary['members']:
            target['local'] = target is primary_member
        primary['local'] = primary_member['key']
        primary['server'].update(server_uuid=primary_member['uuid'], hostname=primary_member['host'])
        secondary = copy.deepcopy(primary)
        new_local = next(x for x in secondary['members'] if x['role'] == 'SECONDARY' and x['state'] == 'ONLINE')
        for target in secondary['members']:
            target['local'] = target is new_local
        secondary['local'] = new_local['key']
        secondary['server'].update(server_uuid=new_local['uuid'], hostname=new_local['host'])
        for target in primary['members']:
            other = next(x for x in secondary['members'] if x['key'] == target['key'])
            before, after = self.actions(primary, target), self.actions(secondary, other)
            self.assertEqual(set(before), set(after))
            self.assertEqual(before['switchover']['enabled'], after['switchover']['enabled'])
            self.assertEqual(before['stop_member']['enabled'], target['local'])
            self.assertEqual(after['stop_member']['enabled'], other['local'])
            action = before['stop_member'] if not target['local'] else after['stop_member']
            connection_name = local(primary)['name'] if not target['local'] else local(secondary)['name']
            self.assertEqual(action['why_not'],
                'Stop Group Replication runs on the server it stops, %s; this connection is to %s.' %
                (target['name'], connection_name))
            self.assertEqual(action['fix'], 'Connect to %s to stop it.' % target['name'])
            self.assertEqual(action['target'], {'self': False, 'member_name': target['name']})
            self.assertEqual(before['switchover']['target'], {'self': True, 'member_name': local(primary)['name']})
        self.assertEqual(set(self.actions(primary)), set(self.actions(secondary)))
        self.assertEqual({aid: a['enabled'] for aid, a in self.actions(primary).items()},
                         {aid: a['enabled'] for aid, a in self.actions(secondary).items()})

    def test_local_states_keep_stop_and_start_visible(self):
        for state in ('ONLINE', 'RECOVERING', 'OFFLINE', 'ERROR', 'UNREACHABLE'):
            report = F.report('ic-8.4-healthy')
            target = local(report)
            target['state'] = state
            checks = {'shell': self.SHELL, 'bootstrap_group': 'OFF'}
            actions = self.actions(report, target, checks)
            self.assertEqual(set(actions), self.MEMBER_IDS['innodb_cluster'])
            self.assertEqual(actions['stop_member']['enabled'], state in ('ONLINE', 'RECOVERING', 'ERROR'))
            self.assertEqual(actions['start_member']['enabled'], state == 'OFFLINE')
            if state == 'ERROR':
                self.assertEqual(actions['start_member']['fix'], 'Stop Group Replication on this member first.')
                self.assert_refused(report, actions['start_member'], target, checks)

    def test_all_local_node_actions_name_both_servers_when_remote(self):
        for scenario, action_ids in (
                ('ic-8.4-healthy', ('stop_member', 'start_member')),
                ('galera-11.8-healthy', ('desync_node', 'resync_node', 'bootstrap_pc'))):
            report = F.report(scenario)
            target = next(x for x in report['members'] if not x['local'])
            actions = self.actions(report, target)
            for aid in action_ids:
                action = actions[aid]
                self.assertFalse(action['enabled'])
                self.assertIn(target['name'], action['why_not'])
                self.assertIn(local(report)['name'], action['why_not'])
                self.assertTrue(action['fix'].startswith('Connect to %s to ' % target['name']))
                self.assertEqual(action['target'], {'self': False, 'member_name': target['name']})
                self.assert_refused(report, action, target)

    def test_recovering_member_and_lost_quorum_block_group_changes(self):
        for state in ('RECOVERING', 'UNREACHABLE'):
            report = F.report('ic-8.4-healthy')
            secondary = next(x for x in report['members'] if not x['local'])
            secondary['state'] = state
            self.assertFalse(self.actions(report)['switch_to_multi_primary']['enabled'])
            self.assertFalse(self.actions(report, secondary)['switchover']['enabled'])
            self.assertTrue(self.actions(report, local(report))['stop_member']['enabled'])
        report = F.report('gr-8.4-two-members')
        report['cluster']['quorum'] = False
        self.assertFalse(any(a['enabled'] for a in self.actions(report).values()))

    def test_shell_missing_old_unknown_and_ssh_are_distinct(self):
        report = F.report('ic-8.4-healthy')
        cases = (
            ({}, 'MySQL Shell was not found on this computer.', 'Install MySQL Shell 8.0.32 or later, then Refresh.'),
            ({'shell': dict(self.SHELL, version='8.0.31', version_tuple=[8, 0, 31])},
             'This action needs MySQL Shell 8.0.32 or later; found 8.0.31.',
             'Install MySQL Shell 8.0.32 or later, then Refresh. Get MySQL Shell.'),
            ({'shell': dict(self.SHELL, version_tuple=[])},
             'The installed MySQL Shell version could not be read.', 'Install MySQL Shell 8.0.32 or later, then Refresh.'),
            ({'shell': self.SHELL, 'ssh': True}, m._SHELL_SSH_NOTE,
             'Connect directly to a group member from a host that reaches the members.'),
        )
        for checks, reason, fix in cases:
            actions = self.actions(report, checks=checks)
            action = actions['switch_to_multi_primary']
            self.assertEqual(set(actions), self.MODES | self.CLUSTERSET)
            self.assertEqual((action['enabled'], action['why_not'], action['fix']), (False, reason, fix))
            self.assert_refused(report, action, checks=checks)
        self.assertTrue(self.actions(report, checks={'shell': dict(self.SHELL, version_tuple=[8, 0, 32])})
                        ['switch_to_multi_primary']['enabled'])

    def test_topology_server_floor_checks_members_but_not_missing_metadata(self):
        for name in ('ic-8.4-healthy', 'gr-8.4-two-members'):
            report = F.report(name)
            local(report)['version'] = '8.0.12'
            self.assertFalse(self.actions(report)['switch_to_multi_primary']['enabled'])
            report = F.report(name)
            report['server_version'] = '8.0.12'
            self.assertFalse(self.actions(report)['switch_to_multi_primary']['enabled'])
        report = F.report('ic-8.4-member-offline')
        secondary = next(x for x in report['members'] if x['state'] == 'ONLINE' and x['role'] == 'SECONDARY')
        self.assertTrue(self.actions(report, secondary)['switchover']['enabled'], 'MISSING does not block switchover')
        self.assertFalse(self.actions(report)['switch_to_multi_primary']['enabled'], 'MISSING blocks mode switches')

    def test_no_clusterset_and_no_candidate_each_keep_all_families(self):
        report = F.report('ic-8.4-healthy')
        # Admit every method here so the membership refusal is observed;
        # separate boundary tests preserve tool-before-state precedence.
        checks = {'shell': dict(self.SHELL, version='9.7.0', version_tuple=[9, 7, 0])}
        actions = self.actions(report, checks=checks)
        for aid in self.CLUSTERSET:
            self.assertIn(aid, actions)
            self.assertEqual(actions[aid]['why_not'], 'This cluster is not part of an InnoDB ClusterSet.')
            self.assert_refused(report, actions[aid], checks=checks)
        report = F.report('ic-8.4-clusterset-primary')
        report['clusterset']['clusters'] = [c for c in report['clusterset']['clusters'] if c['local']]
        actions = self.actions(report)
        self.assertFalse(actions['set_primary_cluster']['enabled'])
        self.assertFalse(actions['rejoin_cluster']['enabled'])
        self.assertFalse(actions['force_primary_cluster']['enabled'])
        for name in ('ic-8.4-clusterset-primary', 'ic-8.4-clusterset-replica'):
            report = F.report(name)
            self.assertFalse(self.actions(report)['switch_to_multi_primary']['enabled'])
            report['cluster']['topology'] = 'multi_primary'
            self.assertFalse(self.actions(report)['switch_to_single_primary']['enabled'])

    def test_read_replica_keeps_changes_disabled_but_can_check_its_clusterset(self):
        report = F.report('ic-8.4-read-replica-local')
        self.assertEqual({a.split(':')[0] for a in self.actions(report)}, self.MODES | self.CLUSTERSET)
        self.assertFalse(any(a['enabled'] for a in self.actions(report).values()))
        with_set = F.report('ic-8.4-read-replica-clusterset')
        self.assertTrue(with_set['clusterset']['from_local_copy'])
        self.assertEqual({aid for aid, a in self.actions(with_set).items() if a['enabled']}, {'check_clusterset'})
        for source in (report, with_set):
            for target in source['members']:
                self.assertEqual(set(self.actions(source, target)), self.MEMBER_IDS['innodb_cluster'])
                self.assertFalse(any(a['enabled'] for a in self.actions(source, target).values()))

    def test_rejoin_accepts_missing_offline_error_with_an_online_primary(self):
        for state in ('MISSING', 'OFFLINE', 'ERROR'):
            report = F.report('ic-8.4-member-offline')
            target = next(x for x in report['members'] if x['state'] == 'MISSING')
            target['state'] = state
            if state != 'MISSING':
                target['state_source'] = 'replication_group_members'
            action = self.actions(report, target)['rejoin_instance']
            self.assertTrue(action['enabled'], (state, action))
            self.assertEqual(m.plan_action(report, action['id'], target['key'], {'shell': self.SHELL})['via'], 'mysqlsh')
            local(report)['role'] = 'SECONDARY'
            action = self.actions(report, target)['rejoin_instance']
            self.assertFalse(action['enabled'])
            self.assertIn('No ONLINE primary', action['why_not'])
            self.assert_refused(report, action, target)

    def test_optional_gc_variable_is_read_and_blocks_only_multi_primary(self):
        for name in ('gr-8.4-two-members', 'ic-8.4-metadata-2.4'):
            for version, value in ((v, setting) for v in ('8.4.11', '9.7.0') for setting in (None, 'OFF', 'ON')):
                scenario = copy.deepcopy(F.SCENARIOS[name])
                scenario['variables'].update(version=version)
                if value is not None:
                    scenario['variables']['group_replication_preemptive_garbage_collection'] = value
                conn = F.ScenarioConn(scenario)
                report = m.read_status(conn)
                self.assertTrue(any('group_replication_preemptive_garbage_collection' in sql for sql, _ in conn.calls))
                action = self.actions(report)['switch_to_multi_primary']
                self.assertEqual(action['enabled'], value != 'ON')
                if value == 'ON':
                    self.assertEqual(action['why_not'], 'group_replication_preemptive_garbage_collection is ON.')
                    self.assert_refused(report, action)
                    report['cluster']['topology'] = 'multi_primary'
                    self.assertTrue(self.actions(report)['switch_to_single_primary']['enabled'])
                elif value is None:
                    self.assertNotIn('preemptive_garbage_collection', report['group_replication'])

    def test_bootstrap_variable_is_read_and_unknown_is_not_assumed_off(self):
        for value in (None, 'ON', 'OFF'):
            edit = F.editor(('SHOW GLOBAL VARIABLES', F.pairs(group_replication_bootstrap_group=value)))
            conn, _ = F.recorded('ic-8.4-local-offline', edit=edit)
            report = m.read_status(conn)
            action = self.actions(report, local(report))['start_member']
            self.assertEqual(action['enabled'], value == 'OFF')
            if action['enabled']:
                plan = m.plan_action(report, 'start_member', local(report)['key'])
                self.assertEqual(plan['expect']['bootstrap_group'], 'OFF')
            else:
                self.assert_refused(report, action, local(report))

    def test_unreadable_cluster_keeps_actions_and_overview_plans_use_local(self):
        report = F.report('ic-8.4-healthy')
        plan = m.plan_action(report, 'switch_to_multi_primary', checks={'shell': self.SHELL})
        self.assertEqual(plan['member_key'], local(report)['key'])
        report['available'] = False
        for target in [None] + report['members']:
            for action in self.actions(report, target).values():
                self.assertFalse(action['enabled'])
                self.assert_refused(report, action, target)
        with self.assertRaisesRegex(ValueError, 'not offered'):
            m.plan_action(report, 'invented_action', local(report)['key'])


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
        self.assertIn('grant_sql', report)
        self.assertIsNone(report['grant_sql'])

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

    def test_proxysql_admin_is_not_a_cluster(self):
        from test_proxysql import FakeAdmin
        class ProxyAdminConn(FakeAdmin):
            def run(self, sql, extra_flags=None, timeout=None):
                if 'SHOW GLOBAL VARIABLES' in sql:
                    raise ConnectorError('ERROR 1045 (28000) at line 1: near "SHOW": syntax error')
                return super(ProxyAdminConn, self).run(sql, extra_flags=extra_flags, timeout=timeout)
        conn = ProxyAdminConn()
        report = m.read_status(conn)
        self.assertTrue(report['available'])
        self.assertEqual((report['kind'], report['status']), ('none', 'unknown'))
        self.assertEqual(report['server_product'], 'ProxySQL Admin')
        self.assertEqual(report['server_version'], '3.0.11-797-g7c91137')
        self.assertEqual(report['message'], 'Not part of a cluster: this connection is a ProxySQL Admin interface.')
        self.assertEqual(report['members'], [])

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
        self.assertEqual(denied['grant_sql'], 'GRANT SELECT ON `performance\\_schema`.* TO `app`@`%`;')
        off = F.report('gr-8.4-ps-off')
        self.assertEqual((off['kind'], off['status'], off['members']), ('group_replication', 'unknown', []))
        self.assertTrue(any('Performance Schema is off' in n for n in off['notes']))
        for name in ('mysql-8.4-standalone', 'mysql-8.4-plugin-idle'):
            report = F.report(name)
            self.assertEqual((report['kind'], report['status']), ('none', 'unknown'), name)
            self.assertTrue(report['message'].startswith('Not part of a cluster'))
            self.assertEqual(m.format_text(report).count('\n'), 1)

    def test_denied_read_grant_sql(self):
        from test_chart_data import validate
        err = ConnectorError("mysql client exited 1: ERROR 1142 (42000) at line 1: SELECT command denied to "
                             "user 'spoofed'@'%' for table 'mysql.user'")
        # MySQL 8.4: partial_revokes OFF escapes '_', ON keeps literal, absent leaves null.
        for partial, expected in (
            ('OFF', 'GRANT SELECT ON `performance\\_schema`.* TO `real_user`@`10.0.0.5`;'),
            ('ON', 'GRANT SELECT ON `performance_schema`.* TO `real_user`@`10.0.0.5`;'),
            (None, None),
        ):
            with self.subTest(partial_revokes=partial):
                sc = {
                    'variables': F._mysql_vars(partial_revokes=partial),
                    'account': 'real_user@10.0.0.5',
                    'tables': {'performance_schema.replication_group_members': err},
                }
                report = m.read_status(F.ScenarioConn(sc))
                validate(report, SCHEMA)
                self.assertFalse(report['available'])
                self.assertEqual(report['grant_sql'], expected)
                if expected is not None:
                    self.assertNotIn('spoofed', expected)
                    self.assertNotIn('mysql', expected)

        # MySQL 5.7 has no partial_revokes variable and always uses wildcard matching.
        sc_57 = {
            'variables': F._mysql_vars(version='5.7.44'),
            'account': 'app@localhost',
            'tables': {'performance_schema.replication_group_members': err},
        }
        report_57 = m.read_status(F.ScenarioConn(sc_57))
        validate(report_57, SCHEMA)
        self.assertEqual(report_57['grant_sql'], 'GRANT SELECT ON `performance\\_schema`.* TO `app`@`localhost`;')

        # CURRENT_USER() not read, NULL or missing '@' leaves grant_sql null.
        for extra in (
            {'omit_current_user': True},
            {'account': None},
            {'account': ''},
            {'account': 'no_host_part'},
        ):
            with self.subTest(extra=extra):
                sc = dict({
                    'variables': F._mysql_vars(partial_revokes='OFF'),
                    'tables': {'performance_schema.replication_group_members': err},
                }, **extra)
                report = m.read_status(F.ScenarioConn(sc))
                validate(report, SCHEMA)
                self.assertFalse(report['available'])
                self.assertIsNone(report['grant_sql'])
                self.assertNotIn('_current_user', report)
        # Plain dict report without private _current_user leaves grant_sql null.
        self.assertIsNone(m._denied_read_grant_sql(
            m.Server(F._mysql_vars(partial_revokes='OFF'), {}), 'performance_schema', report={}))
        # Connection failure during the initial preflight still raises ConnectorError.
        with self.assertRaises(ConnectorError):
            m.read_status(F.ScenarioConn({
                'variables': F._mysql_vars(partial_revokes='OFF'),
                'account': ConnectorError('mysql client exited 1: ERROR 2013 (HY000): Lost connection'),
                'tables': {'performance_schema.replication_group_members': err},
            }))

        # Hostile CURRENT_USER() outside printable ASCII or with a backslash returns null.
        hostile_accounts = (
            '\x01\\! touch /tmp/x #@%',
            '\u00c8' * 7 + '\\! touch /tmp/x #@%',
            'app@h\x1b[201~',
            'back\\slash@%',
            'bad\nuser@%',
            'app\x7f@%',
            'a\u200bb@%',
            'a\u202eb@%',
            'caf\u00e9@%',
        )
        for account in hostile_accounts:
            for partial in ('OFF', 'ON'):
                with self.subTest(hostile=account, partial_revokes=partial):
                    sc = {
                        'variables': F._mysql_vars(partial_revokes=partial),
                        'account': account,
                        'tables': {'performance_schema.replication_group_members': err},
                    }
                    report = m.read_status(F.ScenarioConn(sc))
                    validate(report, SCHEMA)
                    self.assertFalse(report['available'])
                    self.assertIsNone(report['grant_sql'])

        # Galera (_read_galera and _read_galera_off): SHOW GLOBAL STATUS has no table
        # schema, so grant_sql is null even when error text mimics a 1142 table error.
        galera_1142 = ConnectorError(
            "mysql client exited 1: ERROR 1142 (42000) at line 1: SELECT command denied to "
            "user 'evil'@'%' for table 'mysql.wsrep_cluster_members'"
        )
        for wsrep_on in ('ON', 'OFF'):
            with self.subTest(galera_wsrep_on=wsrep_on):
                sc = {
                    'variables': F._mariadb_vars(wsrep_on=wsrep_on,
                                                 wsrep_provider='/usr/lib/galera/libgalera_smm.so',
                                                 wsrep_cluster_address='gcomm://a,b,c'),
                    'account': 'app@%',
                    'status_error': galera_1142,
                }
                report = m.read_status(F.ScenarioConn(sc))
                validate(report, SCHEMA)
                self.assertFalse(report['available'])
                self.assertEqual(report['kind'], 'galera')
                self.assertIn('grant_sql', report)
                self.assertIsNone(report['grant_sql'])

    def test_denied_gr_scenario_uses_no_extra_invocation_for_grant_sql(self):
        conn = F.conn('gr-8.4-denied')
        report = m.read_status(conn)
        sent = [sql for sql, _ in conn.calls]
        self.assertFalse(report['available'])
        self.assertEqual(report['grant_sql'], 'GRANT SELECT ON `performance\\_schema`.* TO `app`@`%`;')
        self.assertEqual(len(sent), 3)
        self.assertIn("SELECT 'myflames_current_user', CURRENT_USER()", sent[0])
        self.assertIn('SHOW GLOBAL STATUS', sent[1])
        self.assertIn('performance_schema.replication_group_members', sent[2])
        self.assertFalse(any('SELECT USER()' in sql for sql in sent))
        self.assertEqual(sum('CURRENT_USER()' in sql for sql in sent), 1)
        self.assertNotIn('_current_user', report)
        # Private _current_user never serializes into JSON on healthy reports either.
        healthy = m.read_status(F.conn('ic-8.4-healthy'))
        self.assertNotIn('_current_user', healthy)
        self.assertNotIn('mf_admin@localhost', json.dumps(healthy))

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
        self.assertEqual(json.loads(out)['schema_version'], 'cluster-1.1')
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
            ast.parse(path.read_text(encoding="utf-8"), **({'feature_version': 7} if sys.version_info >= (3, 8) else {}))


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
        self.assertEqual(report['schema_version'], 'cluster-1.1')
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
                self.assertEqual((full['schema_version'], full['kind']), ('cluster-1.1', kind))
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



class TestAttachTopologyTiers(unittest.TestCase):
    """attach_topology_tiers forwards ssh_host in local_identity without altering direct connections."""

    def setUp(self):
        from myflames import topology_links
        topology_links._reset_cache_for_tests()

    def _report(self, hostname='shared-box', port=3306):
        return {
            'available': True,
            'kind': 'group_replication',
            'performance_schema': False,
            'server': {
                'hostname': hostname,
                'port': port,
                'report_host': None,
                'server_uuid': 'aaaaaaaa-0000-4000-8000-000000000001',
            },
            'members': [{'host': '127.0.0.1', 'port': port, 'uuid': 'aaaaaaaa-0000-4000-8000-000000000001'}],
        }

    def test_clusterset_tiers_use_only_matching_cached_probes_without_opening(self):
        from myflames import topology_links
        from test_topology_links import FakeProbeConn
        self.addCleanup(topology_links._reset_cache_for_tests)
        current = {'id': 'p-cur', 'name': 'Database', 'host': '127.0.0.1', 'port': 13306, 'group': 'lab'}
        rs = {'id': 'p-rs', 'name': 'Readyset', 'host': '127.0.0.1', 'port': 13307,
              'remember_password': True, 'group': 'lab'}
        px = {'id': 'p-px', 'name': 'ProxySQL', 'host': '127.0.0.1', 'port': 16032,
              'remember_password': True, 'group': 'lab'}
        stale = {'id': 'p-stale', 'name': 'Stale tier', 'host': '127.0.0.1', 'port': 13308,
                 'updated_at': 'before', 'remember_password': True, 'group': 'lab'}
        spare = {'id': 'p-spare', 'name': 'Spare', 'host': '127.0.0.1', 'port': 13309,
                 'remember_password': True, 'group': 'lab'}
        upstream = [{'host': 'mysql-primary', 'port': '3306', 'database': 'shop', 'user': 'rs',
                     'weight': '100', 'purpose': 'cdc', 'active_cdc': 'true', 'health': 'up', 'connections': '1'}]
        conns = {
            'p-rs': FakeProbeConn('readyset', upstreams=upstream, cdc_server='mysql-primary:3306'),
            'p-stale': FakeProbeConn('readyset', upstreams=upstream, cdc_server='mysql-primary:3306'),
            'p-spare': FakeProbeConn('database', hostname='spare'),
            'p-px': FakeProbeConn('proxysql', servers=[
                {'hostgroup_id': '10', 'hostname': 'mysql-primary', 'port': '3306', 'status': 'ONLINE',
                 'weight': '100'}]),
        }
        opened = []

        def opener(profile):
            opened.append(profile['id'])
            return conns[profile['id']]

        # Populate the real cache through the same synthetic client probes as
        # topology discovery, then start the ClusterSet pass with fresh counts.
        warm, notes = topology_links.probe_saved_profiles(
            [current, rs, px, stale], opener, skip_profile_id='p-cur', current=current)
        self.assertEqual(notes, [])
        self.assertEqual({item['profile_id'] for item in warm}, {'p-rs', 'p-px', 'p-stale'})
        self.assertEqual(opened, ['p-rs', 'p-px', 'p-stale'])
        opened[:] = []
        changed_stale = dict(stale, updated_at='after')
        conn = FakeProbeConn('database', hostname='mysql-primary', port=3306)
        conn.host, conn.port = '127.0.0.1', 13306
        report = self._report('mysql-primary')
        report['clusterset'] = {'name': 'set', 'clusters': []}
        m.attach_topology_tiers(conn, report, profiles=[current, rs, px, changed_stale, spare],
                                open_profile=opener, current_profile_id='p-cur')
        tiers = report['topology_tiers']
        self.assertEqual(opened, [])
        self.assertEqual([item['profile_id'] for item in tiers['readyset']], ['p-rs'])
        self.assertEqual([item['profile_id'] for item in tiers['proxysql']], ['p-px'])
        self.assertTrue(any('stale tier' in note.lower() for note in tiers['notes']), tiers['notes'])

    def test_ssh_connection_passes_ssh_host_and_blocks_local_loopback_tier_match(self):
        from test_topology_links import FakeProbeConn
        conn = FakeProbeConn('database', hostname='shared-box', port=3306)
        conn.host = '127.0.0.1'
        conn.port = 3306
        conn.ssh_host = 'bastion.example'
        current = {'id': 'p-cur', 'name': 'SSH Cluster', 'host': '127.0.0.1', 'port': 3306,
                   'ssh_host': 'bastion.example', 'group': 'lab'}
        local_rs = {'id': 'p-rs', 'name': 'Local Readyset', 'host': '127.0.0.1', 'port': 13307,
                    'remember_password': True, 'group': 'lab'}
        local_px = {'id': 'p-px', 'name': 'Local Proxy', 'host': '127.0.0.1', 'port': 3306,
                    'remember_password': True, 'group': 'lab'}
        rs_conn = FakeProbeConn('readyset', upstreams=[
            {'host': '127.0.0.1', 'port': '3306', 'database': 'shop', 'user': 'rs',
             'weight': '100', 'purpose': 'cdc', 'active_cdc': 'true', 'health': 'up', 'connections': '1'},
        ], cdc_server='127.0.0.1:3306')
        px_conn = FakeProbeConn('proxysql', servers=[
            {'hostgroup_id': '10', 'hostname': '127.0.0.1', 'port': '3306', 'status': 'ONLINE', 'weight': '100'},
        ])
        opened = []

        def opener(profile):
            opened.append(profile['id'])
            return rs_conn if profile['id'] == 'p-rs' else px_conn

        # Even when the SSH server shares the local hostname, ssh_host stops local
        # loopback tier matching and keeps the direct local proxy eligible as a candidate.
        with mock.patch('myflames.topology_links.socket.gethostname', return_value='shared-box.local'):
            out = m.attach_topology_tiers(
                conn, self._report('shared-box'),
                profiles=[current, local_rs, local_px], open_profile=opener,
                current_profile_id='p-cur',
            )
        with self.subTest('tier_matchers_block_local_loopback'):
            self.assertEqual(out['topology_tiers']['readyset'], [])
            self.assertEqual(out['topology_tiers']['proxysql'], [])
        with self.subTest('filter_keeps_direct_local_proxy'):
            self.assertEqual(opened, ['p-rs', 'p-px'])

        # Also check local_identity directly carries ssh_host.
        with self.subTest('local_identity_carries_ssh_host'):
            with mock.patch('myflames.topology_links.discover_database_tiers', return_value={'readyset': [], 'proxysql': []}) as disc:
                m.attach_topology_tiers(conn, self._report('shared-box'), profiles=[current], current_profile_id='p-cur')
            self.assertEqual(disc.call_args[0][1].get('ssh_host'), 'bastion.example')

    def test_direct_connection_preserves_local_tier_discovery_and_filtering(self):
        from test_topology_links import FakeProbeConn
        conn = FakeProbeConn('database', hostname='shared-box', port=3306)
        conn.host = '127.0.0.1'
        conn.port = 13306
        conn.ssh_host = None
        current = {'id': 'p-cur', 'name': 'Direct DB', 'host': '127.0.0.1', 'port': 13306, 'group': 'lab'}
        direct_alias = {'id': 'p-alias', 'name': 'Direct Alias', 'host': 'localhost', 'port': 13306,
                        'remember_password': True, 'group': 'lab'}
        local_rs = {'id': 'p-rs', 'name': 'Local Readyset', 'host': '127.0.0.1', 'port': 13307,
                    'remember_password': True, 'group': 'lab'}
        rs_conn = FakeProbeConn('readyset', upstreams=[
            {'host': '127.0.0.1', 'port': '3306', 'database': 'shop', 'user': 'rs',
             'weight': '100', 'purpose': 'cdc', 'active_cdc': 'true', 'health': 'up', 'connections': '1'},
        ], cdc_server='127.0.0.1:3306')
        opened = []

        def opener(profile):
            opened.append(profile['id'])
            return rs_conn

        with mock.patch('myflames.topology_links.socket.gethostname', return_value='shared-box.local'):
            out = m.attach_topology_tiers(
                conn, self._report('shared-box'),
                profiles=[current, direct_alias, local_rs], open_profile=opener,
                current_profile_id='p-cur',
            )
        self.assertEqual(opened, ['p-rs'])
        self.assertEqual([node['profile_id'] for node in out['topology_tiers']['readyset']], ['p-rs'])
        with mock.patch('myflames.topology_links.discover_database_tiers', return_value={'readyset': [], 'proxysql': []}) as disc:
            m.attach_topology_tiers(conn, self._report('shared-box'), profiles=[current], current_profile_id='p-cur')
        self.assertIsNone(disc.call_args[0][1]['ssh_host'])


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
