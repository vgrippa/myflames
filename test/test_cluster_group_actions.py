"""Group member writes use their own saved login and a reviewed node identity.

These are synthetic clients over the existing cluster fixtures, with the real
store, consent, SQL classification, review tokens, guards and audit code. No
network service is started and no database write is performed.
"""
import copy
import json
import os
import sqlite3
import tempfile
import types
import unittest
from email.message import Message
from unittest import mock

import _cluster_fixtures as F
from myflames import cluster, connection_groups, dbops, reviewed_writes, secret_store, ui
from myflames.connector import ConnectorError
from myflames.workspace_store import ENDPOINT_FIELDS, WorkspaceStore
from test_cluster_actions import Writes
from _reviewed_replay import guarded_packets


class MemberClient(Writes):
    """Model connector consent before each login, including the write login."""
    def check_consent(self):
        guard = getattr(self, '_login_guard', None)
        if guard is not None:
            guard()

    def run(self, sql, extra_flags=None, timeout=None):
        self.check_consent()
        return super().run(sql, extra_flags, timeout)

    def run_write(self, sql, extra_flags=None, timeout=None):
        self.check_consent()
        return super().run_write(sql, extra_flags, timeout)


def mysql_client(index):
    scenario = copy.deepcopy(F.SCENARIOS['gr-8.4-two-members'])
    scenario['variables'].update(server_uuid=F.SERVER_UUIDS[index], hostname='db-%d' % (index + 1),
                                 report_host='db-%d' % (index + 1))
    scenario['tables']['performance_schema.replication_group_members'] = [
        F._gr_member(i, host='db-%d' % (i + 1)) for i in range(3)]
    scenario['tables']['performance_schema.replication_group_member_stats'] = [F._gr_stats(i) for i in range(3)]
    return MemberClient(F.ScenarioConn(scenario))


# Member and overview action families, including concrete ClusterSet targets.
CLUSTER_ACTION_IDS = (
    'switchover', 'stop_member', 'start_member', 'rejoin_instance',
    'switch_to_single_primary', 'switch_to_multi_primary',
    'set_primary_cluster', 'set_primary_cluster:east', 'force_primary_cluster',
    'rejoin_cluster', 'rejoin_cluster:east',
    'desync_node', 'resync_node', 'bootstrap_pc',
)


class GroupActionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.dict(os.environ, HOME=self.temp.name, USERPROFILE=self.temp.name,
                                  MYFLAMES_SECRET_BACKEND='vault')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = WorkspaceStore()
        self.workspace = types.SimpleNamespace(store=self.store)
        self.profiles = []
        self.clients = {}
        for index in range(3):
            host = 'db-%d' % (index + 1)
            profile = self.store.save('profile', dict(name='prod-' + host, host=host, port=3306,
                                      user='dba-%d' % (index + 1), group='G', type='development'),
                                      'prof-' + host, password='secret-' + host, remember=True)
            self.store.record_topology_identity(profile['id'], {'server_uuid': F.SERVER_UUIDS[index]})
            self.profiles.append(self.store.get('profile', profile['id']))
            self.clients[host] = mysql_client(index)
        self.active = self.profiles[0]
        self.target = self.profiles[1]
        self.connection = dict({k: self.active.get(k) for k in ENDPOINT_FIELDS},
                               profile_id=self.active['id'], group_name='G', password='active-request-secret')
        report = cluster.read_status(self.clients['db-1'])
        self.member_key = next(m['key'] for m in report['members'] if m['uuid'] == F.SERVER_UUIDS[1])
        self.opened = []

    def api(self, path, payload):
        def factory(**options):
            self.opened.append(dict(options))
            conn = self.clients[options['host']]
            # As in the real connector, action_checks sees the target's SSH.
            conn.ssh_host = options.get('ssh_host')
            return conn
        with mock.patch('myflames.connector.MySQLConnection', side_effect=factory):
            return ui.api_response(path, payload, self.workspace)

    def payload(self, **changes):
        value = dict(connection=self.connection, action='stop_member', member_key=self.member_key,
                     target_profile_id=self.target['id'])
        value.update(changes)
        return value

    def plan(self, **changes):
        return self.api('/api/live/cluster/plan', self.payload(**changes))

    def apply(self, plan, **changes):
        reviewed = dict(plan)
        token = reviewed.pop('review_token')
        payload = self.payload(plan=reviewed, review_token=token, confirmed=True, typed=plan.get('typed_guard'))
        payload.update(changes)
        return self.api('/api/live/cluster/action', payload)

    def records(self):
        return dbops.audit_report(self.store)['records']

    def assert_no_writes(self):
        self.assertFalse(any(c.writes for c in self.clients.values()))

    def pxc_group(self):
        """Three saved members with distinct local identities over PXC recordings."""
        members = F.report('pxc-8.4-healthy')['members']
        self.clients = {}
        profiles = []
        for index, profile in enumerate(self.profiles):
            node = next(m for m in members if m['name'] == 'pxc-%d' % (index + 1))

            def node_read(sql, text, count, node=node, index=index):
                if sql.startswith(cluster.CLUSTER_TAG + 'SHOW GLOBAL VARIABLES WHERE'):
                    return F.set_pairs(text, {'hostname': node['host'], 'report_host': node['host'],
                        'server_uuid': F.SERVER_UUIDS[index], 'wsrep_node_name': node['name'],
                        'wsrep_node_incoming_address': node['address']})
                if sql.startswith(cluster.CLUSTER_TAG + 'SHOW GLOBAL STATUS WHERE Variable_name LIKE'):
                    return F.set_pairs(text, {'wsrep_gcomm_uuid': node['uuid']})
                return text

            client = MemberClient(F.recorded('pxc-8.4-healthy', edit=node_read)[0])
            self.clients[node['host']] = client
            self.store.save('profile', dict(profile, host=node['host']), profile['id'],
                            password='secret-db-%d' % (index + 1), remember=True)
            self.store.record_topology_identity(profile['id'], {'wsrep_node_uuid': node['uuid']})
            profiles.append(self.store.get('profile', profile['id']))
            report = cluster.read_status(client)
            self.assertEqual(report['kind'], 'pxc')
            self.assertEqual(next(m['uuid'] for m in report['members'] if m['local']), node['uuid'])
        self.profiles = profiles
        self.active, self.target = profiles[:2]
        self.connection = dict({k: self.active.get(k) for k in ENDPOINT_FIELDS},
                               profile_id=self.active['id'], group_name='G', password='active-request-secret')
        report = cluster.read_status(self.clients[self.active['host']])
        self.assertEqual(len(report['members']), 3)
        return report

    def test_pxc_group_refuses_every_action_member_and_target_without_recording_identity(self):
        report = self.pxc_group()
        refusal = 'That change is not offered for this member now. Refresh and try again.'
        for action in CLUSTER_ACTION_IDS:
            for member in report['members']:
                for target in (None, self.active, self.target):
                    with self.subTest(action=action, member=member['name'],
                                      target=target['id'] if target else 'omitted'):
                        payload = dict(connection=self.connection, action=action, member_key=member['key'])
                        if target is not None:
                            payload['target_profile_id'] = target['id']
                        self.opened.clear()
                        with mock.patch.object(self.store, 'record_topology_identity',
                                               wraps=self.store.record_topology_identity) as record:
                            with self.assertRaises(ValueError) as caught:
                                self.api('/api/live/cluster/plan', payload)
                        self.assertEqual(str(caught.exception), refusal)
                        record.assert_not_called()
                        self.assertEqual([o['host'] for o in self.opened],
                                         [(target or self.active)['host']])
                        self.assertEqual(len(ui._review_tokens(self.workspace).entries), 0)
                        self.assert_no_writes()

    def test_pxc_group_forged_actions_refuse_before_login_or_identity_recording(self):
        report = self.pxc_group()
        for action in CLUSTER_ACTION_IDS:
            for member in report['members']:
                for target in (None, self.active, self.target):
                    with self.subTest(action=action, member=member['name'],
                                      target=target['id'] if target else 'omitted'):
                        route = target or self.active
                        own_report = cluster.read_status(self.clients[route['host']])
                        expect = cluster._server_identity(own_report)
                        expect['target'] = ui._action_target_binding(route, self.connection, 'pxc')
                        forged = dict(schema_version=cluster.ACTION_SCHEMA_VERSION, action=action,
                                      member_key=member['key'], expect=expect,
                                      statements=[cluster.CLUSTER_ACTION_TAG + 'SET GLOBAL wsrep_desync = ON'])
                        payload = dict(connection=self.connection, plan=forged,
                                       review_token='forged-no-review-token', confirmed=True)
                        if target is not None:
                            payload['target_profile_id'] = target['id']
                        self.opened.clear()
                        with mock.patch.object(self.store, 'record_topology_identity',
                                               wraps=self.store.record_topology_identity) as record, \
                                mock.patch.object(secret_store, 'get_secret') as lookup:
                            result = self.api('/api/live/cluster/action', payload)
                        self.assertEqual((result['status'], result['outcome'], result['sent']),
                                         ('refused', 'review', []))
                        record.assert_not_called()
                        lookup.assert_not_called()
                        self.assertEqual(self.opened, [])
                        self.assert_no_writes()

    def test_group_on_db1_stops_db2_with_db2_password_uuid_and_audit(self):
        plan = self.plan()
        target = plan['expect']['target']
        self.assertEqual(target['profile_id'], self.target['id'])
        self.assertEqual(target['server_identity'], {'server_uuid': F.SERVER_UUIDS[1]})
        self.assertEqual([target['endpoint'][k] for k in ('host', 'port', 'user')], ['db-2', 3306, 'dba-2'])
        self.assertEqual(plan['server_name'], 'prod-db-2 (db-2:3306)')
        self.assertEqual(dict(plan['facts'])['Runs on'], plan['server_name'])
        result = self.apply(plan)
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual([c['host'] for c in self.opened], ['db-2', 'db-2'])
        self.assertEqual([c['password'] for c in self.opened], ['secret-db-2', 'secret-db-2'])
        self.assertEqual(self.clients['db-1'].writes, [])
        (script,) = self.clients['db-2'].writes
        self.assertEqual([(g, s) for g, s, _ in guarded_packets(script)],
                         [(plan['session_guard'][0], dbops.split_statements(s)[0]['text']) for s in plan['statements']])
        self.assertIn(F.SERVER_UUIDS[1], guarded_packets(script)[0][0])
        self.assertNotIn(F.SERVER_UUIDS[0], guarded_packets(script)[0][0])
        (record,) = self.records()
        self.assertEqual((record['connection']['profile_id'], record['connection']['profile_name'],
                          record['connection']['host']), ('prof-db-2', 'prod-db-2', 'db-2'))
        self.assertEqual(record['target']['target'], target)
        self.assertNotIn('secret-db-', json.dumps(record))

    def test_action_endpoint_has_the_group_route(self):
        self.assertIn('/api/live/cluster/action', ui._WRITE_PATHS)
        result = self.apply(self.plan())
        self.assertEqual(result['status'], 'done')
        self.assertEqual([c['host'] for c in self.opened], ['db-2', 'db-2'])

    def test_apply_endpoint_is_unknown_and_post_is_refused(self):
        self.assertNotIn('/api/live/cluster/apply', ui._ACTION_PATHS)
        # Exercise the real POST allowlist and authentication without a listener.
        server = types.SimpleNamespace(origin='http://127.0.0.1:8765', token='test-token',
                                       cookie_name='myflames_test', session_secret='test-secret')
        body = json.dumps(self.payload()).encode('utf-8')
        for path in ('/api/live/cluster/apply', '/api/unknown'):
            with self.subTest(path=path):
                handler = object.__new__(ui.WorkspaceHandler)
                handler.path, handler.command, handler.server = path, 'POST', server
                handler.headers = Message()
                for key, value in {'Host': '127.0.0.1:8765', 'Origin': server.origin,
                                   'Cookie': 'myflames_test=test-secret', 'X-Myflames-Token': server.token,
                                   'Content-Type': 'application/json', 'Content-Length': str(len(body))}.items():
                    handler.headers[key] = value
                handler._read_body = mock.Mock(return_value=body)
                handler._send = mock.Mock()
                with mock.patch.object(ui, 'api_response') as dispatch:
                    handler.do_POST()
                handler._send.assert_called_once_with(404, {'error': 'Not found.'})
                handler._read_body.assert_not_called()
                dispatch.assert_not_called()

    def test_f2_wrong_backend_on_plan_refuses_without_a_token(self):
        self.clients['db-2'] = mysql_client(2)
        with self.assertRaisesRegex(ValueError, 'it reports a different server'):
            self.plan()
        self.assertEqual(len(ui._review_tokens(self.workspace).entries), 0)
        self.assert_no_writes()

    def test_f2_same_hostname_and_port_with_wrong_uuid_cannot_authorize_member(self):
        wrong = mysql_client(2)
        wrong.conn.scenario['variables'].update(hostname='db-2', report_host='db-2')
        self.clients['db-2'] = wrong
        with self.assertRaises(ValueError):
            self.plan()
        self.assert_no_writes()

    def test_f2_backend_flips_after_plan_refuses_without_a_write(self):
        plan = self.plan()
        self.clients['db-2'] = mysql_client(2)
        result = self.apply(plan)
        self.assertEqual((result['status'], result['sent']), ('refused', []))
        self.assert_no_writes()

    def test_f2_last_login_flip_is_refused_by_the_reviewed_uuid_guard(self):
        plan = self.plan()
        self.clients['db-2'].fail = ConnectorError('ERROR 1242 (21000) at line 2: Subquery returns more than 1 row')
        result = self.apply(plan)
        self.assertEqual((result['status'], result['outcome'], result['sent']), ('refused', 'identity_mismatch', []))
        self.assertIn(F.SERVER_UUIDS[1], guarded_packets(self.clients['db-2'].writes[0])[0][0])
        self.assertEqual(self.records()[0]['outcome'], 'identity_mismatch')

    def test_f2_member_local_flag_does_not_replace_own_server_uuid(self):
        report = cluster.read_status(self.clients['db-2'])
        report['server']['server_uuid'] = F.SERVER_UUIDS[2]
        with mock.patch.object(cluster, 'read_status', return_value=report), self.assertRaises(ValueError):
            self.plan()
        self.assert_no_writes()

    def test_unconfirmed_address_match_reviews_records_and_writes_only_the_proven_member(self):
        self.store.record_topology_identity(self.target['id'], {'server_uuid': None})
        before = self.store.get('profile', self.target['id'])
        report = cluster.read_status(self.clients['db-1'])
        checks = dict(group_profiles=self.store.list('profile'), current_profile=self.active, group_name='G')
        with mock.patch.object(secret_store, 'get_secret') as lookup:
            action = next(a for a in cluster.available_actions(report, self.member_key, checks)
                          if a['id'] == 'stop_member')
        lookup.assert_not_called()
        self.assertEqual(self.opened, [])
        self.assertTrue(action['enabled'], action)
        self.assertEqual(action['target']['profile_id'], self.target['id'])
        self.assertIs(action['target']['confirm'], True)
        self.assertEqual(action['target']['note'], 'Review checks that prod-db-2 (db-2:3306) is this member.')
        with mock.patch.object(self.store, 'record_topology_identity', wraps=self.store.record_topology_identity) as record:
            plan = self.plan(target_profile_id=action['target']['profile_id'])
        record.assert_called_once_with(self.target['id'], {'server_uuid': F.SERVER_UUIDS[1]},
                                       expected=before, observation=mock.ANY)
        self.assertIsNotNone(record.call_args[1]['observation'])
        self.assertEqual(self.store.get('profile', self.target['id'])['topology_identity']['server_uuid'],
                         F.SERVER_UUIDS[1])
        self.assertEqual(len(ui._review_tokens(self.workspace).entries), 1)
        self.assertEqual(plan['expect']['target']['server_identity'], {'server_uuid': F.SERVER_UUIDS[1]})
        self.assertEqual(self.apply(plan)['status'], 'done')
        self.assertEqual([c['host'] for c in self.opened], ['db-2', 'db-2'])
        self.assertEqual([c['password'] for c in self.opened], ['secret-db-2', 'secret-db-2'])
        self.assertEqual(self.clients['db-1'].writes, [])
        self.assertEqual(self.clients['db-3'].writes, [])
        (script,) = self.clients['db-2'].writes
        self.assertIn(F.SERVER_UUIDS[1], guarded_packets(script)[0][0])
        self.assertNotIn(F.SERVER_UUIDS[0], guarded_packets(script)[0][0])
        self.assertEqual([(g, s) for g, s, _ in guarded_packets(script)],
                         [(plan['session_guard'][0], dbops.split_statements(s)[0]['text']) for s in plan['statements']])

    def test_address_suggestions_use_topology_endpoint_normalization_and_loopback_scope(self):
        report = cluster.read_status(self.clients['db-1'])
        member = next(m for m in report['members'] if m['key'] == self.member_key)
        for host, port, saved_host, saved_port, observer, ssh, matches in (
                (' DB-2 ', 3306, 'db-2', '3306', 'db-1', None, True),
                ('[2001:DB8::2]', 3306, '2001:db8::2', 3306, 'db-1', None, True),
                ('db-2', 3307, 'db-2', 3306, 'db-1', None, False),
                ('db-2', 3306, 'db-20', 3306, 'db-1', None, False),
                ('localhost', 3306, '127.0.0.1', 3306, '127.0.0.1', None, True),
                ('localhost', 3306, 'localhost', 3306, 'db-1', None, False),
                ('localhost', 3306, 'localhost', 3306, '127.0.0.1', 'bastion', False)):
            with self.subTest(host=host, port=port, saved_host=saved_host, observer=observer, ssh=ssh):
                member.update(host=host, port=port)
                profile = dict(self.target, host=saved_host, port=saved_port, ssh_host=ssh, topology_identity={})
                checks = dict(group_profiles=[profile], current_profile=dict(self.active, host=observer), group_name='G')
                action = next(a for a in cluster.available_actions(report, self.member_key, checks)
                              if a['id'] == 'stop_member')
                self.assertEqual(action['enabled'], matches, action)
                self.assertEqual(action['target']['profile_id'], profile['id'] if matches else None)
                if matches:
                    self.assertIs(action['target']['confirm'], True)

    def test_unconfirmed_address_requires_current_membership_and_a_saved_password(self):
        report = cluster.read_status(self.clients['db-1'])
        for changes in ({'group': 'H'}, {'remember_password': False}):
            with self.subTest(changes=changes):
                profile = dict(self.target, topology_identity={}, **changes)
                checks = dict(group_profiles=[profile], current_profile=self.active, group_name='G')
                action = next(a for a in cluster.available_actions(report, self.member_key, checks)
                              if a['id'] == 'stop_member')
                self.assertFalse(action['enabled'])
                self.assertIsNone(action['target']['profile_id'])

    def test_unconfirmed_address_reaching_another_server_records_nothing_and_issues_no_token(self):
        self.store.record_topology_identity(self.target['id'], {'server_uuid': None})
        before = self.store.get('profile', self.target['id'])
        self.clients['db-2'] = mysql_client(2)
        with mock.patch.object(self.store, 'record_topology_identity') as record:
            with self.assertRaises(ValueError) as error:
                self.plan()
        self.assertEqual(str(error.exception),
                         'prod-db-2 (db-2:3306) does not reach db-2: it reports a different server.')
        record.assert_not_called()
        self.assertEqual(self.store.get('profile', self.target['id']), before)
        self.assertEqual(self.records(), [])
        self.assertEqual(len(ui._review_tokens(self.workspace).entries), 0)
        self.assert_no_writes()

    def test_duplicate_address_matches_disable_review_with_both_saved_names(self):
        report = cluster.read_status(self.clients['db-1'])
        target = dict(self.target, topology_identity={})
        duplicate = dict(self.profiles[2], host=' DB-2 ', topology_identity={})
        for profiles in ([duplicate, target, self.active], [self.active, target, duplicate],
                         [target, dict(duplicate, remember_password=False), self.active]):
            with self.subTest(profiles=[p['id'] for p in profiles]):
                checks = dict(group_profiles=profiles, current_profile=self.active, group_name='G')
                action = next(a for a in cluster.available_actions(report, self.member_key, checks)
                              if a['id'] == 'stop_member')
                self.assertEqual((action['enabled'], action['target']['profile_id']), (False, None))
                self.assertEqual(action['why_not'], "prod-db-2 and prod-db-3 both use this member's address.")
                self.assertEqual(action['fix'],
                                 'Keep one saved connection for this member in the group, or correct their addresses.')
                self.assertIsNone(cluster._member_profile(report, next(m for m in report['members']
                                                    if m['key'] == self.member_key), checks))
                with self.assertRaisesRegex(ValueError, "both use this member's address"):
                    cluster.plan_action(report, 'stop_member', self.member_key, checks)
        self.assertEqual(self.opened, [])

    def test_stored_identity_wins_over_a_different_address_match(self):
        report = cluster.read_status(self.clients['db-1'])
        confirmed = dict(self.target, host='private-db-2')
        suggested = dict(self.profiles[2], host='db-2', topology_identity={})
        for profiles in ([suggested, confirmed, self.active], [self.active, confirmed, suggested]):
            checks = dict(group_profiles=profiles, current_profile=self.active, group_name='G')
            action = next(a for a in cluster.available_actions(report, self.member_key, checks)
                          if a['id'] == 'stop_member')
            self.assertTrue(action['enabled'], action)
            self.assertEqual(action['target']['profile_id'], confirmed['id'])
            self.assertNotIn('confirm', action['target'])
            self.assertNotIn('note', action['target'])
            # Missing credentials on the proven route cannot promote the address.
            confirmed['remember_password'] = False
            action = next(a for a in cluster.available_actions(report, self.member_key, checks)
                          if a['id'] == 'stop_member')
            self.assertFalse(action['enabled'])
            self.assertIsNone(action['target']['profile_id'])
            self.assertEqual(action['why_not'], 'The saved connection prod-db-2 has no saved password.')
            self.assertEqual(action['fix'], 'Save its password before reviewing a cluster action.')
            confirmed['remember_password'] = True

    def test_no_identity_or_address_match_names_unconfirmed_connections_without_refresh_advice(self):
        report = cluster.read_status(self.clients['db-1'])
        member = next(m for m in report['members'] if m['key'] == self.member_key)
        member.update(name='mf-ic-2', host='mf-ic-2', address='mf-ic-2:3306')
        profiles = [dict(p, name='cluster-ic-%d' % (index + 1), host='127.0.0.1', port=34301 + index,
                         topology_identity={}) for index, p in enumerate(self.profiles)]
        checks = dict(group_profiles=list(reversed(profiles)), current_profile=profiles[0], group_name='G')
        # Tier probes do not log in to cluster endpoints. Their failure or
        # absence cannot imply that Refresh would confirm this member.
        for reads in ([], [dict(id=self.target['id'], outcome='failed', message='Access denied: SELECT secret')],
                      [dict(id=p['id'], outcome='read', message='') for p in profiles]):
            with self.subTest(reads=reads):
                report['connection_group'] = {'read': reads}
                action = next(a for a in cluster.available_actions(report, member['key'], checks)
                              if a['id'] == 'stop_member')
                self.assertEqual((action['enabled'], action['target']['profile_id']), (False, None))
                self.assertEqual(action['why_not'], 'No saved connection in this group is confirmed as mf-ic-2 yet.')
                self.assertEqual(action['fix'], 'Open the one that reaches mf-ic-2 (cluster-ic-2 or cluster-ic-3) '
                                 'on its own and view Cluster once to confirm it.')
                with self.assertRaises(cluster.ActionUnavailable) as caught:
                    cluster.plan_action(report, 'stop_member', member['key'], checks)
                self.assertEqual(str(caught.exception), action['why_not'] + ' ' + action['fix'])
        self.assertEqual(self.opened, [])
        self.assert_no_writes()

    def test_unconfirmed_choices_include_unknown_and_unrelated_identities_only_in_this_group(self):
        report = cluster.read_status(self.clients['db-1'])
        unrelated_uuid = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
        for identity in ({}, {'server_uuid': None}, {'server_uuid': unrelated_uuid},
                         {'wsrep_node_uuid': F.SERVER_UUIDS[1]}):
            with self.subTest(identity=identity):
                profile = dict(self.target, name='', host='127.0.0.1', port=34302,
                               topology_identity=identity, remember_password=False)
                outside = dict(profile, id='outside', name='outside-group', group='H')
                checks = dict(group_profiles=[None, {}, outside, self.profiles[2], profile, profile, self.active],
                              current_profile=self.active, group_name='G')
                action = next(a for a in cluster.available_actions(report, self.member_key, checks)
                              if a['id'] == 'stop_member')
                self.assertEqual((action['enabled'], action['target']['profile_id']), (False, None))
                self.assertEqual(action['why_not'], 'No saved connection in this group is confirmed as db-2 yet.')
                self.assertEqual(action['fix'], 'Open the one that reaches db-2 (prof-db-2) on its own '
                                 'and view Cluster once to confirm it.')

    def test_unconfirmed_choices_show_at_most_three_saved_names_in_sorted_order(self):
        report = cluster.read_status(self.clients['db-1'])
        profiles = [dict(self.target, id='choice-%d' % index, name='saved-%d' % index,
                         host='127.0.0.1', port=34302 + index, topology_identity={}) for index in range(5)]
        for count, choices in ((1, 'saved-0'), (2, 'saved-0 or saved-1'),
                               (3, 'saved-0 or saved-1 or saved-2'),
                               (4, 'saved-0 or saved-1 or saved-2 and 1 more'),
                               (5, 'saved-0 or saved-1 or saved-2 and 2 more')):
            with self.subTest(count=count):
                checks = dict(group_profiles=list(reversed(profiles[:count])) + [self.active],
                              current_profile=self.active, group_name='G')
                action = next(a for a in cluster.available_actions(report, self.member_key, checks)
                              if a['id'] == 'stop_member')
                self.assertEqual((action['enabled'], action['target']['profile_id']), (False, None))
                self.assertEqual(action['fix'], 'Open the one that reaches db-2 (%s) on its own '
                                 'and view Cluster once to confirm it.' % choices)

    def test_no_unconfirmed_or_matching_connections_still_requests_a_saved_connection(self):
        report = cluster.read_status(self.clients['db-1'])
        active = dict(self.active, topology_identity={})
        other_member = dict(self.target, host='127.0.0.1', port=34302,
                            topology_identity={'server_uuid': F.SERVER_UUIDS[2]})
        outside = dict(self.target, group='H', topology_identity={})
        for profiles in ([], [active], [active, self.profiles[2], other_member, outside]):
            with self.subTest(profiles=[p['id'] for p in profiles]):
                checks = dict(group_profiles=profiles, current_profile=active, group_name='G')
                action = next(a for a in cluster.available_actions(report, self.member_key, checks)
                              if a['id'] == 'stop_member')
                self.assertEqual((action['enabled'], action['target']['profile_id']), (False, None))
                self.assertEqual(action['why_not'], 'Add a saved connection for db-2 to this group.')
                self.assertEqual(action['fix'], action['why_not'])

    def test_duplicate_uuid_first_in_store_order_never_selects_a_route_or_plans(self):
        self.store.record_topology_identity('prof-db-3', {'server_uuid': F.SERVER_UUIDS[1]})
        duplicate = self.store.get('profile', 'prof-db-3')
        profiles = [duplicate, self.target, self.active]
        reason = 'prod-db-2 and prod-db-3 both reach this member.'
        fix = ('A MySQL Router or ProxySQL connection reaches whichever server is primary. '
               'Connect to prod-db-2 to act on this member, or keep only one of them in the group.')
        report = cluster.read_status(self.clients['db-1'])
        checks = dict(group_profiles=profiles, current_profile=self.active, group_name='G')
        member = next(m for m in report['members'] if m['key'] == self.member_key)
        self.assertIsNone(cluster._member_profile(report, member, checks))
        action = next(a for a in cluster.available_actions(report, self.member_key, checks) if a['id'] == 'stop_member')
        self.assertEqual((action['enabled'], action['target']['profile_id']), (False, None))
        self.assertEqual((action['why_not'], action['fix']), (reason, fix))
        with self.assertRaisesRegex(ValueError, reason):
            cluster.plan_action(report, 'stop_member', self.member_key, checks)
        # The HTTP planner reads the target directly, but must retain the same
        # ambiguity refusal even though that fresh report marks it local.
        original = self.store.list
        with mock.patch.object(self.store, 'list', side_effect=lambda kind: profiles if kind == 'profile' else original(kind)):
            with self.assertRaisesRegex(ValueError, reason):
                self.plan()
        self.assert_no_writes()
        self.assertEqual(len(ui._review_tokens(self.workspace).entries), 0)

    def test_direct_and_router_identity_claims_fail_closed_and_name_both_without_blame(self):
        report = cluster.read_status(self.clients['db-1'])
        direct = dict(self.target, name='cluster-ic-1')
        router = dict(self.profiles[2], name='cluster-ic-router-rw', host='127.0.0.1', port=6446,
                      topology_identity=self.target['topology_identity'])
        extra = dict(router, id='extra', name='cluster-ic-router-other')
        for profiles, names, quantifier in (
                ([router, direct, self.active], 'cluster-ic-1 and cluster-ic-router-rw', 'both'),
                ([self.active, direct, router], 'cluster-ic-1 and cluster-ic-router-rw', 'both'),
                ([dict(router, remember_password=False), direct], 'cluster-ic-1 and cluster-ic-router-rw', 'both'),
                ([router, dict(direct, remember_password=False)], 'cluster-ic-1 and cluster-ic-router-rw', 'both'),
                ([router, extra, direct], 'cluster-ic-1 and cluster-ic-router-other and cluster-ic-router-rw', 'all')):
            with self.subTest(names=names, profiles=[p['id'] for p in profiles]):
                checks = dict(group_profiles=profiles, current_profile=self.active, group_name='G')
                action = next(a for a in cluster.available_actions(report, self.member_key, checks)
                              if a['id'] == 'stop_member')
                self.assertEqual((action['enabled'], action['target']['profile_id']), (False, None))
                self.assertEqual(action['why_not'], '%s %s reach this member.' % (names, quantifier))
                self.assertEqual(action['fix'], 'A MySQL Router or ProxySQL connection reaches whichever server '
                                 'is primary. Connect to cluster-ic-1 to act on this member, or keep only one '
                                 'of them in the group.')
                with self.assertRaises(cluster.ActionUnavailable) as caught:
                    cluster.plan_action(report, 'stop_member', self.member_key, checks)
                self.assertEqual(str(caught.exception), action['why_not'] + ' ' + action['fix'])
        self.assertEqual(self.opened, [])
        self.assert_no_writes()

    def test_duplicate_uuid_added_after_review_refuses_before_secret_lookup(self):
        plan = self.plan()
        self.store.record_topology_identity('prof-db-3', {'server_uuid': F.SERVER_UUIDS[1]})
        self.opened.clear()
        with mock.patch.object(secret_store, 'get_secret') as lookup:
            result = self.apply(plan)
        self.assertEqual((result['status'], result['sent']), ('refused', []))
        self.assertIn('prod-db-2 and prod-db-3 both reach this member.', result['message'])
        self.assertIn('Connect to prod-db-2 to act on this member, or keep only one of them in the group.',
                      result['message'])
        lookup.assert_not_called()
        self.assertEqual(self.opened, [])
        self.assert_no_writes()

    def test_restarted_galera_node_offers_review_to_confirm_its_saved_address(self):
        report = cluster.read_status(MemberClient(F.conn('galera-11.8-healthy')))
        member = next(m for m in report['members'] if not m['local'])
        member.update(host='db-2', port=3306, address='db-2:3306')
        # A stale gcomm UUID cannot prove identity after a node restart.
        profile = dict(self.target, topology_identity={'wsrep_node_uuid': 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'})
        checks = dict(group_profiles=[profile], current_profile=self.active, group_name='G')
        action = next(a for a in cluster.available_actions(report, member['key'], checks)
                      if a['id'] == 'desync_node')
        self.assertTrue(action['enabled'], action)
        self.assertEqual(action['target']['profile_id'], profile['id'])
        self.assertIs(action['target']['confirm'], True)
        self.assertEqual(action['target']['note'], 'Review checks that prod-db-2 (db-2:3306) is this member.')
        # A catalog suggestion cannot itself produce a write plan on db-1.
        with self.assertRaises(ValueError):
            cluster.plan_action(report, 'desync_node', member['key'], checks)
        checks['group_profiles'] = []
        action = next(a for a in cluster.available_actions(report, member['key'], checks)
                      if a['id'] == 'desync_node')
        self.assertEqual((action['enabled'], action['target']['profile_id']), (False, None))
        self.assertEqual(action['why_not'], 'Add a saved connection for %s to this group.' % member['name'])
        self.assertEqual(action['fix'], action['why_not'])

    def test_catalog_cannot_use_another_group_or_an_unsaved_password(self):
        report = cluster.read_status(self.clients['db-1'])
        for change in ({'group': 'H'}, {'remember_password': False}):
            profiles = [dict(p, **change) if p['id'] == self.target['id'] else p for p in self.profiles]
            action = next(a for a in cluster.available_actions(report, self.member_key,
                          dict(group_profiles=profiles, group_name='G', current_profile=self.active))
                          if a['id'] == 'stop_member')
            self.assertFalse(action['enabled'])
            self.assertIsNone(action['target']['profile_id'])

    def test_f6_endpoint_edit_refuses_before_any_password_or_connection(self):
        plan = self.plan()
        ui.api_response('/api/profiles/save', {'id': self.target['id'],
                         'value': dict(self.target, host='changed-db-2'), 'password': 'secret-new-endpoint'}, self.workspace)
        self.opened.clear()
        with mock.patch.object(secret_store, 'get_secret') as lookup:
            result = self.apply(plan)
        self.assertEqual((result['status'], result['sent']), ('refused', []))
        self.assertEqual(self.opened, [])
        lookup.assert_not_called()
        self.assert_no_writes()

    def test_f6_every_stored_endpoint_field_and_identity_is_bound(self):
        plan = self.plan()
        binding = plan['expect']['target']
        for key, value in [('host', 'changed'), ('port', 3307), ('user', 'other'), ('ssl_mode', 'REQUIRED'),
                            ('ssl_ca', '/different.pem'), ('ssh_host', 'bastion'), ('ssh_user', 'other'),
                            ('ssh_port', 2222), ('name', 'different-name')]:
            changed = dict(self.target, **{key: value})
            self.assertNotEqual(ui._action_target_binding(changed, self.connection), binding, key)
        self.store.record_topology_identity(self.target['id'], {'server_uuid': F.SERVER_UUIDS[2]})
        self.opened.clear()
        with mock.patch.object(secret_store, 'get_secret') as lookup:
            result = self.apply(plan)
        self.assertEqual(result['status'], 'refused')
        lookup.assert_not_called()
        self.assertEqual(self.opened, [])

    def test_f6_replaying_for_another_profile_or_group_sends_nothing(self):
        plan = self.plan()
        self.opened.clear()
        with mock.patch.object(secret_store, 'get_secret') as lookup:
            result = self.apply(plan, target_profile_id='prof-db-3')
        self.assertEqual(result['status'], 'refused')
        lookup.assert_not_called()
        self.assertEqual(self.opened, [])
        self.assert_no_writes()
        self.store.save_connection_group('H', [p['id'] for p in self.profiles], previous_name='G')
        with mock.patch.object(secret_store, 'get_secret') as lookup:
            result = self.apply(plan, connection=dict(self.connection, group_name='H'))
        self.assertEqual(result['status'], 'refused')
        lookup.assert_not_called()

    def test_plan_records_a_previously_unknown_target_identity_before_issuing_token(self):
        self.store.record_topology_identity(self.target['id'], {'server_uuid': None})
        plan = self.plan()
        stored = self.store.get('profile', self.target['id'])
        self.assertEqual(stored['topology_identity']['server_uuid'], F.SERVER_UUIDS[1])
        self.assertEqual(plan['expect']['target'], ui._action_target_binding(stored, self.connection))
        self.assertEqual(self.apply(plan)['status'], 'done')

    def test_f6_omitting_target_does_not_turn_db2_review_into_db1_write(self):
        plan = self.plan()
        reviewed = dict(plan)
        token = reviewed.pop('review_token')
        with mock.patch.object(secret_store, 'get_secret') as lookup:
            result = self.api('/api/live/cluster/action', dict(connection=self.connection, plan=reviewed,
                              review_token=token, confirmed=True))
        self.assertEqual(result['status'], 'refused')
        lookup.assert_not_called()
        self.assert_no_writes()

    def test_f9_bad_targets_and_edited_active_member_never_lookup_passwords(self):
        self.store.save('profile', dict(name='outside', host='other', port=3306, user='dba', group='H'),
                        'outside', password='outside-secret', remember=True)
        self.store.save('profile', dict(name='no-password', host='nopass', port=3306, user='dba', group='G'),
                        'no-password', remember=False)
        from test_workspace_store import bundle
        self.store.save('investigation', bundle(), 'investigation')
        invalid = [None, True, 1, [], {}, '', 'x' * 201, 'outside', 'investigation', 'no-password', 'missing']
        with mock.patch.object(secret_store, 'get_secret') as lookup:
            for target in invalid:
                with self.subTest(target=target), self.assertRaises(ValueError):
                    self.plan(target_profile_id=target)
            for connection in (dict(self.connection, host='edited'), dict(self.connection, user='edited'),
                               dict(self.connection, group_name='H'), dict(self.connection, profile_id='missing')):
                with self.subTest(connection=connection), self.assertRaises(ValueError):
                    self.plan(connection=connection)
            ungrouped = dict(self.connection)
            ungrouped.pop('group_name')
            with self.assertRaises(ValueError):
                self.plan(connection=ungrouped)
        lookup.assert_not_called()
        self.assertEqual(self.opened, [])

    def test_f9_noncluster_handlers_refuse_even_null_targets_before_password_lookup(self):
        with mock.patch.object(secret_store, 'get_secret') as lookup:
            for kind in ('proxysql', 'readyset'):
                for operation in ('plan', 'action'):
                    for target in (None, self.target['id']):
                        with self.subTest(kind=kind, operation=operation, target=target), self.assertRaises(ValueError):
                            self.api('/api/live/%s/%s' % (kind, operation), self.payload(target_profile_id=target))
        lookup.assert_not_called()
        self.assertEqual(self.opened, [])

    def test_f9_group_removal_during_secret_lookup_never_opens_target(self):
        original = self.store.connection_with_saved_password
        def remove_after_lookup(connection):
            filled = original(connection)
            self.store.assign_profile_group([self.target['id']], '')
            return filled
        with mock.patch.object(self.store, 'connection_with_saved_password', side_effect=remove_after_lookup), \
                self.assertRaises(ValueError):
            self.plan()
        self.assertEqual(self.opened, [])

    def test_f9_member_removed_after_review_refuses_before_lookup(self):
        self.store.set_profile_safety(self.profiles[2]['id'], connection_type='production')
        plan = self.plan()
        self.assertEqual(plan['policy']['connection_type'], 'production')
        self.store.assign_profile_group([self.target['id']], '')
        self.opened.clear()
        with mock.patch.object(secret_store, 'get_secret') as lookup:
            result = self.apply(plan)
        self.assertEqual((result['status'], result['sent']), ('refused', []))
        (record,) = self.records()
        self.assertEqual((record['status'], record['outcome']), ('refused', 'changed'))
        self.assertEqual(record['target'], plan['expect'])
        self.assertEqual(record['connection']['profile_name'], 'prod-db-2')
        self.assertEqual(record['policy']['connection_type'], plan['policy']['connection_type'])
        lookup.assert_not_called()
        self.assertEqual(self.opened, [])

    def test_password_forgotten_after_review_is_audited_before_any_lookup(self):
        plan = self.plan()
        self.store.forget_password(self.target['id'])
        self.opened.clear()
        with mock.patch.object(secret_store, 'get_secret') as lookup:
            result = self.apply(plan)
        self.assertEqual((result['status'], result['sent']), ('refused', []))
        self.assertIn('no saved password', result['message'])
        (record,) = self.records()
        self.assertEqual((record['status'], record['outcome']), ('refused', 'changed'))
        self.assertEqual(record['target'], plan['expect'])
        self.assertEqual(record['connection']['profile_name'], 'prod-db-2')
        lookup.assert_not_called()
        self.assertEqual(self.opened, [])
        self.assert_no_writes()

    def test_active_member_without_saved_password_uses_request_password(self):
        self.store.forget_password(self.active['id'])
        report = cluster.read_status(self.clients['db-1'])
        local = next(m for m in report['members'] if m['local'])
        checks = dict(group_profiles=self.store.list('profile'), current_profile=self.store.get('profile', self.active['id']),
                      group_name='G')
        action = next(a for a in cluster.available_actions(report, local['key'], checks) if a['id'] == 'stop_member')
        self.assertTrue(action['enabled'], action)
        self.assertEqual(action['target']['profile_id'], self.active['id'])
        payload = dict(connection=self.connection, action='stop_member', member_key=local['key'])
        with mock.patch.object(secret_store, 'get_secret') as lookup:
            plan = self.api('/api/live/cluster/plan', payload)
            reviewed = dict(plan)
            token = reviewed.pop('review_token')
            result = self.api('/api/live/cluster/action', dict(payload, plan=reviewed, review_token=token,
                              confirmed=True, typed=plan.get('typed_guard')))
        self.assertEqual((result['status'], result['sent']), ('done', plan['statements']))
        self.assertEqual([o['password'] for o in self.opened], ['active-request-secret'] * 2)
        self.assertEqual(plan['server_name'], 'prod-db-1 (db-1:3306)')
        self.assertEqual(plan['expect']['target']['server_identity'], {'server_uuid': F.SERVER_UUIDS[0]})
        lookup.assert_not_called()

    def test_remote_member_without_saved_password_refuses_before_any_lookup(self):
        self.store.forget_password(self.target['id'])
        with mock.patch.object(secret_store, 'get_secret') as lookup, self.assertRaisesRegex(ValueError, 'no saved password'):
            self.plan()
        lookup.assert_not_called()
        self.assertEqual(self.opened, [])
        self.assert_no_writes()

    def test_identity_record_failure_is_plain_and_issues_no_token(self):
        with mock.patch.object(self.store, 'record_topology_identity',
                               side_effect=sqlite3.OperationalError('database is locked: private-store-detail')):
            with self.assertRaises(ValueError) as caught:
                self.plan()
        self.assertEqual(str(caught.exception), "Could not save the target server's identity. Nothing was sent. "
                         'Try reviewing the action again.')
        self.assertEqual(len(ui._review_tokens(self.workspace).entries), 0)
        self.assert_no_writes()

    def test_consent_revoked_at_write_login_finishes_pending_audit_as_refused(self):
        plan = self.plan()
        client = self.clients['db-2']
        original = client.run_write
        def remove_at_login(*args, **kwargs):
            self.store.assign_profile_group([self.target['id']], '')
            return original(*args, **kwargs)
        with mock.patch.object(client, 'run_write', side_effect=remove_at_login):
            result = self.apply(plan)
        self.assertEqual((result['status'], result['sent'], result['states']), ('refused', [], ['not_sent']))
        (record,) = self.records()
        self.assertEqual((record['id'], record['status'], record['outcome']),
                         (result['audit_id'], 'refused', 'changed'))
        self.assertEqual(record['target'], plan['expect'])
        self.assert_no_writes()

    def test_consent_is_rechecked_between_two_target_read_logins(self):
        client = self.clients['db-2']
        original = client.conn.run
        calls = []
        def remove_on_first_read(*args, **kwargs):
            result = original(*args, **kwargs)
            calls.append(args[0])
            self.store.assign_profile_group([self.target['id']], '')
            return result
        with mock.patch.object(client.conn, 'run', side_effect=remove_on_first_read), self.assertRaises(ValueError):
            self.plan()
        self.assertEqual(len(calls), 1)
        self.assert_no_writes()

    def test_target_password_and_tls_and_ssh_come_only_from_stored_fields(self):
        profile = self.store.save('profile', dict(self.target, ssl_mode='REQUIRED', ssl_ca='/saved-ca.pem',
                                  ssh_host='saved-bastion', ssh_user='saved-ssh', ssh_port=2222),
                                  self.target['id'], password='new-db2-secret', remember=True)
        plan = self.plan()
        options = self.opened[0]
        self.assertEqual({k: options[k] for k in ('host', 'port', 'user', 'ssl_mode', 'ssl_ca', 'ssh_host', 'ssh_user', 'ssh_port')},
                         {k: profile[k] for k in ('host', 'port', 'user', 'ssl_mode', 'ssl_ca', 'ssh_host', 'ssh_user', 'ssh_port')})
        self.assertEqual(options['password'], 'new-db2-secret')
        self.assertEqual(plan['expect']['target']['profile_id'], self.target['id'])

    def test_target_read_only_and_group_production_policy_are_enforced(self):
        self.store.set_profile_safety('prof-db-3', connection_type='production')
        plan = self.plan()
        self.assertEqual(plan['policy']['connection_type'], 'production')
        self.store.set_profile_safety(self.target['id'], read_only=True)
        self.opened.clear()
        with mock.patch.object(secret_store, 'get_secret') as lookup:
            result = self.apply(plan)
        self.assertEqual(result['status'], 'refused')
        lookup.assert_not_called()
        self.assertEqual(self.opened, [])
        self.assert_no_writes()

    def test_group_policy_change_during_fresh_apply_read_refuses_before_send(self):
        plan = self.plan()
        original = cluster.read_status
        def protect_target(*args, **kwargs):
            report = original(*args, **kwargs)
            self.store.set_profile_safety(self.target['id'], read_only=True)
            return report
        with mock.patch.object(cluster, 'read_status', side_effect=protect_target):
            result = self.apply(plan)
        self.assertEqual((result['status'], result['sent']), ('refused', []))
        self.assert_no_writes()

    def test_rejoin_routes_from_the_online_active_member_and_uses_its_transport(self):
        report = F.report('ic-8.4-healthy')
        member = next(m for m in report['members'] if not m['local'] and m['role'] == 'SECONDARY')
        member['state'] = 'MISSING'
        member['state_source'] = 'metadata'
        member['in_metadata'] = True
        profile = dict(self.target, topology_identity={'server_uuid': member['uuid']}, ssh_host='bastion')
        shell = {'path': '/fixture/mysqlsh', 'version': '8.4.11', 'version_tuple': (8, 4, 11)}
        active = dict(self.active, topology_identity={'server_uuid': report['server']['server_uuid']})
        checks = dict(group_name='G', group_profiles=[active, profile], current_profile=active, shell=shell, ssh=False)
        action = next(a for a in cluster.available_actions(report, member['key'], checks) if a['id'] == 'rejoin_instance')
        self.assertTrue(action['enabled'], action)
        self.assertEqual(action['target']['profile_id'], active['id'])
        self.assertTrue(action['target']['self'])
        checks['ssh'] = True
        action = next(a for a in cluster.available_actions(report, member['key'], checks) if a['id'] == 'rejoin_instance')
        self.assertFalse(action['enabled'], action)
        self.assertIn('SSH', action['why_not'])

    def test_group_rejoins_a_missing_member_through_the_online_member_not_the_missing_login(self):
        from myflames import cluster_shell
        shell = {'path': '/fixture/mysqlsh', 'version': '8.4.11', 'version_tuple': (8, 4, 11)}
        client = MemberClient(F.conn('ic-8.4-member-offline'))
        report = cluster.read_status(client)
        missing = next(m for m in report['members'] if m['state'] == 'MISSING')
        self.clients['db-1'] = client
        self.store.record_topology_identity(self.active['id'], cluster.target_server_identity(report))
        active = self.store.get('profile', self.active['id'])
        checks = dict(group_name='G', group_profiles=[active], current_profile=active, shell=shell)
        offered = next(a for a in cluster.available_actions(report, missing['key'], checks)
                       if a['id'] == 'rejoin_instance')
        self.assertTrue(offered['enabled'], offered)
        self.assertEqual(offered['target']['profile_id'], active['id'])
        with mock.patch.object(cluster_shell, 'find_shell', return_value=shell), \
                mock.patch.object(cluster_shell.ShellWriter, 'run_write', return_value='') as send:
            plan = self.plan(action='rejoin_instance', member_key=missing['key'], target_profile_id=active['id'])
            result = self.apply(plan, action='rejoin_instance', member_key=missing['key'], target_profile_id=active['id'])
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual([options['host'] for options in self.opened], ['db-1', 'db-1'])
        self.assertEqual(plan['expect']['member']['key'], missing['key'])
        self.assertEqual(plan['expect']['target']['profile_id'], active['id'])
        self.assertEqual(plan['expect']['target']['server_identity'], cluster.target_server_identity(report))
        self.assertIn('rejoinInstance("%s", {recoveryMethod: "incremental"})' % missing['address'],
                      plan['statements'][0])
        self.assertEqual(plan['typed_guard'], missing['name'])
        self.assertEqual(plan['target_text'], missing['name'])
        self.assertEqual(dict(plan['facts'])['Listed in the cluster as'],
                         next(m['name'] for m in report['members'] if m['local']))
        self.assertEqual(dict(plan['facts'])['Instance'], '%s (%s)' % (missing['name'], missing['address']))
        send.assert_called_once_with('\n'.join(plan['session_guard'] + plan['statements']), timeout=cluster.SHELL_TIMEOUT)
        self.assertIn(report['server']['server_uuid'], plan['session_guard'][0])
        self.assertIn(missing['uuid'], plan['session_guard'][0])

    def test_remote_galera_desync_and_resync_review_the_peers_own_state_in_both_directions(self):
        for active_desync, peer_desync, applicable, refused in [
                ('OFF', 'ON', 'resync_node', 'desync_node'),
                ('ON', 'OFF', 'desync_node', 'resync_node')]:
            with self.subTest(active=active_desync, peer=peer_desync):
                report = F.report('galera-11.8-healthy')
                report['galera']['desync'] = active_desync
                report['galera']['metrics']['local_state_comment'] = ('Synced' if active_desync == 'OFF'
                                                                      else 'Donor/Desynced')
                peer = next(m for m in report['members'] if not m['local'])
                self.store.record_topology_identity(self.target['id'], {'wsrep_node_uuid': peer['uuid']})
                checks = dict(group_name='G', group_profiles=self.store.list('profile'), current_profile=self.active)
                actions = {a['id']: a for a in cluster.available_actions(report, peer['key'], checks)}
                for aid in ('desync_node', 'resync_node'):
                    self.assertTrue(actions[aid]['enabled'], actions[aid])
                    self.assertIn('Review checks wsrep_desync', actions[aid]['why_not'])
                    self.assertEqual(actions[aid]['target']['profile_id'], self.target['id'])
                    with self.assertRaisesRegex(ValueError, 'node.s saved connection'):
                        cluster.plan_action(report, aid, peer['key'], checks)

                def peer_read(sql, text, count):
                    if sql.startswith(cluster.CLUSTER_TAG + 'SHOW GLOBAL VARIABLES WHERE'):
                        return F.set_pairs(text, {'wsrep_node_name': peer['name'], 'wsrep_desync': peer_desync})
                    if sql.startswith(cluster.CLUSTER_TAG + 'SHOW GLOBAL STATUS WHERE Variable_name LIKE'):
                        return F.set_pairs(text, {'wsrep_gcomm_uuid': peer['uuid'], 'wsrep_local_index': '1',
                            'wsrep_local_state_comment': 'Synced' if peer_desync == 'OFF' else 'Donor/Desynced'})
                    return text

                self.clients['db-2'] = MemberClient(F.recorded('galera-11.8-healthy', edit=peer_read)[0])
                plan = self.plan(action=applicable, member_key=peer['key'])
                self.assertEqual(plan['expect']['desync'], peer_desync)
                self.assertEqual(plan['expect']['server']['wsrep_gcomm_uuid'], peer['uuid'])
                self.assertEqual(plan['expect']['target']['profile_id'], self.target['id'])
                with self.assertRaises(cluster.ActionUnavailable):
                    self.plan(action=refused, member_key=peer['key'])

    def test_f10_topology_label_cannot_name_the_saved_target_in_review(self):
        for label in ('prod-db-2</b>', 'prod-db-1', 'e', 'Stop Group Replication'):
            with self.subTest(label=label):
                report = cluster.read_status(self.clients['db-2'])
                member = next(m for m in report['members'] if m['uuid'] == F.SERVER_UUIDS[1])
                member['name'] = label
                with mock.patch.object(cluster, 'read_status', return_value=report):
                    plan = self.plan()
                self.assertEqual(plan['server_name'], 'prod-db-2 (db-2:3306)')
                self.assertEqual(plan['target_text'], plan['server_name'])
                self.assertEqual(dict(plan['facts'])['Runs on'], plan['server_name'])
                self.assertEqual(dict(plan['facts'])['Listed in the cluster as'], label)
                self.assertEqual(dict(plan['facts'])['Server UUID'], F.SERVER_UUIDS[1])
                self.assertEqual(plan['title'], 'Stop Group Replication on prod-db-2 (db-2:3306)?')
                self.assertEqual(plan['confirm_label'], 'Stop Group Replication')
                self.assertNotIn(label + '?', plan['title'])

    def test_galera_binding_uses_wsrep_node_identity_and_guard(self):
        client = MemberClient(F.conn('galera-11.8-healthy'))
        report = cluster.read_status(client)
        local = next(m for m in report['members'] if m['local'])
        self.clients['db-2'] = client
        plan = self.plan(action='desync_node', member_key=local['key'])
        self.assertEqual(plan['expect']['target']['server_identity'], {'wsrep_node_uuid': local['uuid']})
        stored = self.store.get('profile', self.target['id'])
        self.assertEqual(stored['topology_identity']['server_uuid'], F.SERVER_UUIDS[1])
        # Another reader records an unrelated key after review. It cannot
        # invalidate the node UUID selected by the Galera plan.
        self.store.record_topology_identity(self.target['id'], {'server_uuid': F.SERVER_UUIDS[1]})
        result = self.apply(plan)
        self.assertEqual(result['status'], 'done', result)
        self.assertIn('@@global.wsrep_node_name', guarded_packets(client.writes[0])[0][0])
        self.assertEqual(self.records()[0]['connection']['profile_name'], 'prod-db-2')

    def test_galera_local_flag_and_node_name_do_not_replace_wsrep_uuid(self):
        report = cluster.read_status(MemberClient(F.conn('galera-11.8-healthy')))
        local = next(m for m in report['members'] if m['local'])
        report['galera']['gcomm_uuid'] = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
        with mock.patch.object(cluster, 'read_status', return_value=report), self.assertRaises(ValueError):
            self.plan(action='desync_node', member_key=local['key'])
        self.assert_no_writes()

    def test_first_server_uuid_record_after_galera_review_still_sends(self):
        self.store.record_topology_identity(self.target['id'], {'server_uuid': None})
        client = self.clients['db-2'] = MemberClient(F.conn('galera-11.8-healthy'))
        report = cluster.read_status(client)
        local = next(m for m in report['members'] if m['local'])
        plan = self.plan(action='desync_node', member_key=local['key'])
        self.store.record_topology_identity(self.target['id'], {'server_uuid': F.SERVER_UUIDS[1]})
        result = self.apply(plan)
        self.assertEqual((result['status'], result['sent']), ('done', plan['statements']))
        self.assertEqual(len(client.writes), 1)
        self.assertEqual(self.store.get('profile', self.target['id'])['topology_identity']['wsrep_node_uuid'], local['uuid'])

    def test_galera_catalog_uses_node_uuid_evidence_and_never_just_node_address(self):
        report = cluster.read_status(MemberClient(F.conn('galera-11.8-healthy')))
        member = next(m for m in report['members'] if not m['local'])
        profile = dict(self.target, topology_identity={'wsrep_node_uuid': member['uuid']})
        checks = dict(group_name='G', group_profiles=[profile], current_profile=self.active)
        action = next(a for a in cluster.available_actions(report, member['key'], checks) if a['id'] == 'desync_node')
        self.assertEqual(action['target']['profile_id'], profile['id'])
        self.assertTrue(action['enabled'], action)
        profile['topology_identity'] = {'wsrep_cluster_uuid': report['cluster']['uuid']}
        action = next(a for a in cluster.available_actions(report, member['key'], checks) if a['id'] == 'desync_node')
        self.assertIsNone(action['target']['profile_id'])
        self.assertFalse(action['enabled'])

    def test_f10_active_saved_member_needs_no_cached_identity_to_offer_its_route(self):
        report = cluster.read_status(self.clients['db-1'])
        active = dict(self.active, topology_identity={})
        report = cluster.with_actions(report, group_profiles=[active], current_profile=active, group_name='G')
        local = next(m for m in report['members'] if m['local'])
        stop = next(a for a in local['actions'] if a['id'] == 'stop_member')
        self.assertTrue(stop['enabled'], stop)
        self.assertEqual(stop['target']['profile_id'], active['id'])


if __name__ == '__main__':
    unittest.main()
