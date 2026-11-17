"""Group member writes use their own saved login and a reviewed node identity.

These are synthetic clients over the existing cluster fixtures, with the real
store, consent, SQL classification, review tokens, guards and audit code. No
network service is started and no database write is performed.
"""
import copy
import json
import os
import tempfile
import types
import unittest
from email.message import Message
from unittest import mock

import _cluster_fixtures as F
from myflames import cluster, connection_groups, dbops, secret_store, ui
from myflames.connector import ConnectorError
from myflames.workspace_store import ENDPOINT_FIELDS, WorkspaceStore
from test_cluster_actions import Writes


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
        self.assertEqual(script.splitlines(), plan['session_guard'] + plan['statements'])
        self.assertIn(F.SERVER_UUIDS[1], script.splitlines()[0])
        self.assertNotIn(F.SERVER_UUIDS[0], script.splitlines()[0])
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
        with self.assertRaisesRegex(ValueError, 'selected cluster member'):
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
        self.clients['db-2'].fail = ConnectorError('ERROR 1242 (21000) at line 1: Subquery returns more than 1 row')
        result = self.apply(plan)
        self.assertEqual((result['status'], result['outcome'], result['sent']), ('refused', 'identity_mismatch', []))
        self.assertIn(F.SERVER_UUIDS[1], self.clients['db-2'].writes[0].splitlines()[0])
        self.assertEqual(self.records()[0]['outcome'], 'identity_mismatch')

    def test_f2_member_local_flag_does_not_replace_own_server_uuid(self):
        report = cluster.read_status(self.clients['db-2'])
        report['server']['server_uuid'] = F.SERVER_UUIDS[2]
        with mock.patch.object(cluster, 'read_status', return_value=report), self.assertRaises(ValueError):
            self.plan()
        self.assert_no_writes()

    def test_f2_catalog_uses_uuid_and_never_a_metadata_address_for_route(self):
        report = cluster.read_status(self.clients['db-1'])
        profiles = self.store.list('profile')
        report = cluster.with_actions(report, group_profiles=profiles, current_profile=self.active, group_name='G')
        target = next(m for m in report['members'] if m['uuid'] == F.SERVER_UUIDS[1])
        stop = next(a for a in target['actions'] if a['id'] == 'stop_member')
        self.assertTrue(stop['enabled'], stop)
        self.assertEqual(stop['target']['profile_id'], self.target['id'])
        target['uuid'] = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
        # The metadata address still equals db-2's saved endpoint.
        action = next(a for a in cluster.available_actions(report, target['key'],
                         dict(group_profiles=profiles, current_profile=self.active, group_name='G'))
                      if a['id'] == 'stop_member')
        self.assertFalse(action['enabled'])
        self.assertIsNone(action['target']['profile_id'])
        self.assertEqual(action['why_not'], 'Add a saved connection for db-2 to this group.')
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
        plan = self.plan()
        self.store.assign_profile_group([self.target['id']], '')
        self.opened.clear()
        with mock.patch.object(secret_store, 'get_secret') as lookup, self.assertRaises(ValueError):
            self.apply(plan)
        lookup.assert_not_called()
        self.assertEqual(self.opened, [])

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

    def test_group_catalog_uses_remote_ssh_instead_of_active_ssh_for_shell_actions(self):
        report = F.report('ic-8.4-healthy')
        member = next(m for m in report['members'] if not m['local'] and m['role'] == 'SECONDARY')
        member['state'] = 'MISSING'
        member['state_source'] = 'metadata'
        member['in_metadata'] = True
        profile = dict(self.target, topology_identity={'server_uuid': member['uuid']}, ssh_host='bastion')
        shell = {'path': '/fixture/mysqlsh', 'version': '8.4.11', 'version_tuple': (8, 4, 11)}
        checks = dict(group_name='G', group_profiles=[profile], current_profile=self.active, shell=shell, ssh=False)
        action = next(a for a in cluster.available_actions(report, member['key'], checks) if a['id'] == 'rejoin_instance')
        self.assertFalse(action['enabled'])
        self.assertIn('SSH', action['why_not'])

    def test_f10_topology_label_cannot_name_the_saved_target_in_review(self):
        for label in ('prod-db-2</b>', 'prod-db-1'):
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
                self.assertNotIn(label + '?', plan['title'])

    def test_galera_binding_uses_wsrep_node_identity_and_guard(self):
        client = MemberClient(F.conn('galera-11.8-healthy'))
        report = cluster.read_status(client)
        local = next(m for m in report['members'] if m['local'])
        self.clients['db-2'] = client
        plan = self.plan(action='desync_node', member_key=local['key'])
        self.assertEqual(plan['expect']['target']['server_identity'], {'wsrep_node_uuid': local['uuid']})
        result = self.apply(plan)
        self.assertEqual(result['status'], 'done', result)
        self.assertIn('@@global.wsrep_node_name', client.writes[0].splitlines()[0])
        self.assertEqual(self.records()[0]['connection']['profile_name'], 'prod-db-2')

    def test_galera_local_flag_and_node_name_do_not_replace_wsrep_uuid(self):
        report = cluster.read_status(MemberClient(F.conn('galera-11.8-healthy')))
        local = next(m for m in report['members'] if m['local'])
        report['galera']['gcomm_uuid'] = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
        with mock.patch.object(cluster, 'read_status', return_value=report), self.assertRaises(ValueError):
            self.plan(action='desync_node', member_key=local['key'])
        self.assert_no_writes()

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
