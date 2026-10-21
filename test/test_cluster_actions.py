"""Reviewed Cluster changes (myflames/cluster.py "reviewed actions",
myflames/reviewed_writes.py, /api/live/cluster/plan and
/api/live/cluster/action), over the same live recordings and scenarios as
test_cluster.py (test/_cluster_fixtures.py). Writes go to a recorder; every
test runs with a temporary HOME and the vault backend.
"""
import copy
import json
import os
import tempfile
import unittest
from unittest import mock

import _cluster_fixtures as F
from myflames import cluster as m, dbops, ui
from myflames.connector import ConnectorError

UUID = '8f1a3b2c-0d4e-11ef-9a3b-0242ac120002'


def member(report, name):
    return next(x for x in report['members'] if x['name'] == name)


def local(report):
    return next(x for x in report['members'] if x['local'])


def offered(report, name):
    return [a['id'] for a in m.available_actions(report, member(report, name)['key'])]


class Writes(object):
    """A fixture connection that records writes (one run_write per send) and
    answers group_replication_bootstrap_group."""

    def __init__(self, conn, bootstrap='OFF', fail=None):
        self.conn, self.bootstrap, self.fail = conn, bootstrap, fail
        self.writes = []

    def run(self, sql, extra_flags=None, timeout=None):
        if 'group_replication_bootstrap_group' in sql:
            if self.bootstrap is None:
                raise ConnectorError('mysql client exited 1: ERROR 1193 (HY000) at line 1: Unknown system variable')
            return '%s\n' % self.bootstrap
        return self.conn.run(sql, extra_flags)

    def run_write(self, sql, extra_flags=None, timeout=None):
        self.writes.append(sql)
        if self.fail is not None:
            raise self.fail
        return ''

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


class Home(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {'HOME': self.tmp.name, 'USERPROFILE': self.tmp.name,
                                               'MYFLAMES_SECRET_BACKEND': 'vault'})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)


class TestOffered(Home):
    def test_galera_desync_resync_and_bootstrap(self):
        healthy = F.report('galera-11.8-healthy')
        self.assertEqual(offered(healthy, local(healthy)['name']), ['desync_node'])
        for x in healthy['members']:
            if not x['local']:
                self.assertEqual(m.available_actions(healthy, x['key']), [], 'never on a node this server is not')
        donor = F.report('galera-11.8-donor')
        self.assertEqual((donor['galera']['desync'], offered(donor, local(donor)['name'])), ('ON', ['resync_node']))
        non_primary = F.report('galera-11.8-non-primary')
        self.assertEqual(offered(non_primary, local(non_primary)['name']), ['bootstrap_pc'])
        self.assertEqual(offered(F.report('galera-11.8-joiner'), local(F.report('galera-11.8-joiner'))['name']), [],
                         'a joining node is never bootstrapped')
        self.assertEqual(m.available_actions(F.report('galera-11.8-wsrep-off'),
                                             local(F.report('galera-11.8-wsrep-off'))['key']), [])

    def test_donor_without_wsrep_desync_offers_neither(self):
        report = F.report('galera-11.8-healthy')
        report['galera']['metrics']['local_state_comment'] = 'Donor/Desynced'
        self.assertEqual(offered(report, local(report)['name']), [], 'a state transfer or backup lock ends on its own')

    def test_component_not_read_or_disconnected_is_not_bootstrapped(self):
        report = F.report('galera-11.8-non-primary')
        for component, connected in ((None, True), ('Disconnected', True), ('non-Primary', False)):
            r = copy.deepcopy(report)
            r['cluster']['component'] = component
            r['galera']['metrics']['connected'] = connected
            self.assertNotIn('bootstrap_pc', offered(r, local(r)['name']), (component, connected))

    def test_no_wsrep_on_actions_anywhere(self):
        for name in F.ALL:
            report = F.report(name)
            for x in report['members']:
                for a in m.available_actions(report, x['key']):
                    self.assertNotIn(a['id'], ('stop_node', 'start_node'), name)
                    self.assertNotIn('GCache', a['label'] + a['description'], name)

    def test_pxc_and_galera_for_mysql_offer_nothing(self):
        for name in ('pxc-8.4-healthy', 'pxc-8.4-donor', 'galera-mysql-8.4-healthy'):
            report = F.report(name)
            self.assertEqual([a for x in report['members'] for a in m.available_actions(report, x['key'])], [], name)

    def test_group_replication(self):
        ic = F.report('ic-8.4-healthy')
        primary = next(x for x in ic['members'] if x['role'] == 'PRIMARY')
        self.assertEqual(m.available_actions(ic, primary['key']), [], 'the primary is not switched to itself')
        for x in ic['members']:
            ids = [a['id'] for a in m.available_actions(ic, x['key'])]
            if x['role'] == 'SECONDARY' and x['state'] == 'ONLINE':
                self.assertIn('switchover', ids)
            self.assertEqual('stop_member' in ids, x['local'])
            self.assertNotIn('switch_to_multi_primary', ids, 'InnoDB Cluster mode switches belong to MySQL Shell')
        offline = F.report('ic-8.4-local-offline')
        self.assertEqual(offered(offline, local(offline)['name']), ['start_member'])
        degraded = F.report('ic-8.4-degraded')
        self.assertNotIn('switchover', [a['id'] for x in degraded['members'] for a in m.available_actions(degraded, x['key'])],
                         'no switchover while a member is recovering or unreachable')
        plain = F.report('gr-8.4-multi-primary')
        self.assertIn('switch_to_single_primary', offered(plain, local(plain)['name']))

    def test_a_member_in_error_offers_stop_not_start(self):
        report = F.report('ic-8.4-local-offline')
        local(report)['state'] = 'ERROR'
        self.assertEqual(offered(report, local(report)['name']), ['stop_member'])

    def test_switchover_needs_a_uuid(self):
        report = F.report('ic-8.4-healthy')
        target = next(x for x in report['members'] if x['role'] == 'SECONDARY' and x['state'] == 'ONLINE')
        target['uuid'] = "x') OR ('1"
        self.assertNotIn('switchover', offered(report, target['name']))
        target['uuid'] = None
        self.assertNotIn('switchover', offered(report, target['name']))

    def test_old_mysql_offers_no_group_change(self):
        report = F.report('ic-8.4-healthy')
        report['server_version'] = '8.0.12'
        self.assertEqual([a for x in report['members'] for a in m.available_actions(report, x['key'])], [])

    def test_workspace_reports_match_the_schema(self):
        from pathlib import Path
        from test_chart_data import validate
        schema = json.loads((Path(__file__).resolve().parent.parent / 'docs' / 'schemas' / 'cluster-1.0.json')
                            .read_text(encoding='utf-8'))
        for name in F.ALL:
            validate(m.with_actions(F.report(name)), schema)

    def test_with_actions_is_only_for_the_workspace(self):
        report = F.report('galera-11.8-healthy')
        self.assertNotIn('actions', local(report), 'the CLI and MCP report has no actions')
        self.assertEqual([a['id'] for a in local(m.with_actions(report))['actions']], ['desync_node'])


class TestPlans(Home):
    def test_switchover_sends_the_member_uuid(self):
        report = F.report('ic-8.4-healthy')
        target = next(x for x in report['members'] if x['role'] == 'SECONDARY' and x['state'] == 'ONLINE')
        plan = m.plan_action(report, 'switchover', target['key'])
        self.assertEqual(plan['statements'], ["/*myflames:cluster_op*/ SELECT group_replication_set_as_primary('%s');"
                                              % target['uuid']])
        self.assertRegex(target['uuid'], r'^[0-9a-f-]{36}$')
        primary = next(x for x in report['members'] if x['role'] == 'PRIMARY')
        self.assertIn(primary['uuid'], plan['undo'])
        self.assertEqual(plan['expect']['member']['uuid'], target['uuid'])
        self.assertIsNone(plan['typed_guard'])
        self.assertEqual(dict(plan['facts'])['Server'], 'MySQL 8.4.11')

    def test_plan_refuses_what_is_not_offered(self):
        report = F.report('galera-11.8-healthy')
        other = next(x for x in report['members'] if not x['local'])
        for action, key in (('desync_node', other['key']), ('bootstrap_pc', local(report)['key']),
                            ('stop_node', local(report)['key']), ('resync_node', local(report)['key']),
                            ('desync_node', None), ('desync_node', 'member:nope')):
            with self.assertRaises(ValueError, msg=(action, key)):
                m.plan_action(report, action, key)

    def test_statements_and_guards(self):
        galera = F.report('galera-11.8-healthy')
        plan = m.plan_action(galera, 'desync_node', local(galera)['key'])
        self.assertEqual(plan['statements'], ['/*myflames:cluster_op*/ SET GLOBAL wsrep_desync = ON;'])
        self.assertNotIn('GCache', plan['title'] + plan['description'])
        resync = F.report('galera-11.8-donor')
        self.assertEqual(m.plan_action(resync, 'resync_node', local(resync)['key'])['statements'],
                         ['/*myflames:cluster_op*/ SET GLOBAL wsrep_desync = OFF;'])
        np = F.report('galera-11.8-non-primary')
        boot = m.plan_action(np, 'bootstrap_pc', local(np)['key'])
        self.assertEqual(boot['statements'], ["/*myflames:cluster_op*/ SET GLOBAL wsrep_provider_options = "
                                              "'pc.bootstrap=YES';"])
        self.assertEqual(boot['typed_guard'], local(np)['name'])
        self.assertIn('split brain', boot['description'])
        self.assertIn('wsrep_last_committed here', dict(boot['facts']))
        ic = F.report('ic-8.4-healthy')
        stop = m.plan_action(ic, 'stop_member', local(ic)['key'])
        self.assertEqual(stop['statements'], ['/*myflames:cluster_op*/ STOP GROUP_REPLICATION;'])
        self.assertIn('super_read_only', stop['description'])

    def test_stopping_the_primary_or_the_last_member_is_typed(self):
        report = F.report('gr-8.4-two-members')
        me = local(report)
        stop = m.plan_action(report, 'stop_member', me['key'])
        self.assertEqual(stop['typed_guard'] if me['role'] == 'PRIMARY' else None,
                         me['name'] if me['role'] == 'PRIMARY' else None)
        last = F.report('ic-8.4-healthy')
        for x in last['members']:
            if not x['local'] and x['state_source'] == 'replication_group_members':
                x['state'] = 'OFFLINE'
        stop = m.plan_action(last, 'stop_member', local(last)['key'])
        self.assertEqual(stop['typed_guard'], local(last)['name'])
        self.assertIn('bootstrapped again', stop['undo'])

    def test_start_refuses_with_bootstrap_group_on_or_unknown(self):
        report = F.report('ic-8.4-local-offline')
        key = local(report)['key']
        for value in ('ON', '1', None):
            with self.assertRaises(ValueError, msg=value):
                m.plan_action(report, 'start_member', key, {'bootstrap_group': value})
        plan = m.plan_action(report, 'start_member', key, {'bootstrap_group': 'OFF'})
        self.assertEqual(plan['statements'], ['/*myflames:cluster_op*/ START GROUP_REPLICATION;'])
        self.assertIn('clone', plan['description'])
        self.assertEqual(plan['expect']['bootstrap_group'], 'OFF')

    def test_mode_switch_is_typed_and_warns_about_auto_increment(self):
        report = F.report('gr-8.4-two-members')
        plan = m.plan_action(report, 'switch_to_multi_primary', local(report)['key'])
        self.assertEqual(plan['statements'], ['/*myflames:cluster_op*/ SELECT group_replication_switch_to_multi_primary_mode();'])
        self.assertEqual(plan['typed_guard'], local(report)['name'])
        self.assertTrue(any('auto_increment' in w for w in plan['warnings']))

    def test_every_cluster_statement_is_risky(self):
        from myflames import reviewed_writes
        for name, action in (('galera-11.8-healthy', 'desync_node'), ('galera-11.8-donor', 'resync_node'),
                             ('ic-8.4-healthy', 'stop_member')):
            report = F.report(name)
            plan = m.plan_action(report, action, local(report)['key'])
            self.assertEqual(dbops.decide(reviewed_writes.worst_kind(plan['statements']), 'development')['action'],
                             'confirm', name)


class Endpoints(Home):
    def setUp(self):
        super().setUp()
        from myflames.workspace_store import WorkspaceStore
        self.workspace = type('Workspace', (), {'store': WorkspaceStore(os.path.join(self.tmp.name, 'ws.sqlite3'))})()
        self.connection = {'host': '127.0.0.1', 'port': 34101, 'user': 'mf_admin', 'password': 'pw-secret'}

    def api(self, path, body, conn):
        factory = lambda **options: conn  # noqa: E731
        with mock.patch('myflames.connector.MySQLConnection', factory):
            return ui.api_response(path, body, self.workspace)

    def plan(self, conn, action, member_key):
        return self.api('/api/live/cluster/plan', {'connection': self.connection, 'action': action,
                                                    'member_key': member_key}, conn)

    def send(self, conn, plan, typed=None, token=None):
        plan = dict(plan)
        own = plan.pop('review_token', None)
        return self.api('/api/live/cluster/action', {'connection': self.connection, 'plan': plan,
                                                      'review_token': own if token is None else token,
                                                      'typed': typed, 'confirmed': True}, conn)

    def audit(self):
        return dbops.audit_report(self.workspace.store)['records']

    def test_read_endpoint_adds_actions(self):
        report = self.api('/api/live/cluster', {'connection': self.connection}, F.conn('galera-11.8-healthy'))
        self.assertEqual([a['id'] for a in local(report)['actions']], ['desync_node'])
        self.assertIn('/api/live/cluster/action', ui._WRITE_PATHS)
        self.assertNotIn('/api/live/cluster/action', ui._SERVER_VIEW_PATHS)

    def test_desync_is_sent_once_after_a_fresh_read_and_audited(self):
        conn = Writes(F.conn('galera-11.8-healthy'))
        report = F.report('galera-11.8-healthy')
        plan = self.plan(conn, 'desync_node', local(report)['key'])
        result = self.send(conn, plan)
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(conn.writes, ['/*myflames:cluster_op*/ SET GLOBAL wsrep_desync = ON;'])
        (record,) = self.audit()
        self.assertEqual((record['operation'], record['status'], record['connection']['engine']),
                         ('cluster_action', 'ok', 'mariadb'))
        self.assertNotIn('pw-secret', json.dumps(record))

    def test_another_member_or_a_changed_state_sends_nothing(self):
        report = F.report('galera-11.8-healthy')
        other = next(x for x in report['members'] if not x['local'])
        with self.assertRaises(ValueError):
            self.plan(Writes(F.conn('galera-11.8-healthy')), 'desync_node', other['key'])
        # Reviewed on a Synced node, sent after it became a donor.
        plan = self.plan(Writes(F.conn('galera-11.8-healthy')), 'desync_node', local(report)['key'])
        donor = Writes(F.conn('galera-11.8-donor'))
        result = self.send(donor, plan)
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(donor.writes, [])
        (record,) = self.audit()
        self.assertEqual((record['status'], record['outcome']), ('refused', 'changed'))

    def test_bootstrap_needs_the_typed_name(self):
        report = F.report('galera-11.8-non-primary')
        name, key = local(report)['name'], local(report)['key']
        conn = Writes(F.conn('galera-11.8-non-primary'))
        plan = self.plan(conn, 'bootstrap_pc', key)
        self.assertEqual(self.send(conn, plan, typed='galera')['status'], 'refused')
        self.assertEqual(conn.writes, [])
        plan = self.plan(conn, 'bootstrap_pc', key)
        self.assertEqual(self.send(conn, plan, typed=name)['status'], 'done')
        self.assertEqual(len(conn.writes), 1)

    def test_bootstrap_refuses_when_the_component_changed(self):
        # Codex review: a node joining the non-Primary component needs another review.
        report = F.report('galera-11.8-non-primary')
        name, key = local(report)['name'], local(report)['key']
        conn = Writes(F.conn('galera-11.8-non-primary'))
        plan = self.plan(conn, 'bootstrap_pc', key)
        self.assertIn('component_view', plan['expect'])
        changed = copy.deepcopy(report)
        changed['cluster']['size'] = (changed['cluster']['size'] or 1) + 1
        changed['galera']['metrics']['cluster_conf_id'] = (changed['galera']['metrics'].get('cluster_conf_id') or 0) + 1
        with mock.patch.object(m, 'read_status', return_value=changed):
            result = self.send(conn, plan, typed=name)
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(conn.writes, [])

    def test_start_reads_bootstrap_group_at_send_time(self):
        report = F.report('ic-8.4-local-offline')
        key = local(report)['key']
        conn = Writes(F.conn('ic-8.4-local-offline'))
        plan = self.plan(conn, 'start_member', key)
        conn.bootstrap = 'ON'
        result = self.send(conn, plan)
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(conn.writes, [])

    def test_lost_reply_is_unknown(self):
        report = F.report('ic-8.4-healthy')
        conn = Writes(F.conn('ic-8.4-healthy'), fail=ConnectorError('Query timed out after 120s; the outcome is unknown.'))
        plan = self.plan(conn, 'stop_member', local(report)['key'])
        result = self.send(conn, plan, typed=plan['typed_guard'])
        self.assertEqual(result['status'], 'unknown')
        (record,) = self.audit()
        self.assertEqual(record['status'], 'unknown')

    def test_server_refusal_is_failed(self):
        report = F.report('ic-8.4-healthy')
        target = next(x for x in report['members'] if x['role'] == 'SECONDARY' and x['state'] == 'ONLINE')
        conn = Writes(F.conn('ic-8.4-healthy'), fail=ConnectorError(
            "mysql client exited 1: ERROR 3910 (HY000) at line 1: The function 'group_replication_set_as_primary' "
            "failed. There is a replica channel running in the group's current primary member."))
        result = self.send(conn, self.plan(conn, 'switchover', target['key']))
        self.assertEqual(result['status'], 'failed')
        self.assertIn('replica channel', result['message'])


if __name__ == '__main__':
    unittest.main()
