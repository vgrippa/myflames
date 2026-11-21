"""Rolling group changes use member-owned logins and one exact review.

The clients below are synthetic transport boundaries. Store membership,
secrets, policy, review tokens, variable validation, identity packets and
audit records are the production implementations, never patched checks.
"""
import copy
import json
import re
import types
import unittest
from email.message import Message
from pathlib import Path
from unittest import mock
from xml.sax.saxutils import escape, quoteattr

from myflames import connector, dbops, topology_change, ui
from _perf_replay import batch
from _reviewed_replay import (acknowledged_error, answer_evidence, evidence_options,
                              guarded_packets, read_statements)
from test_chart_data import validate
from test_connection_groups import GroupCase


ROOT = Path(__file__).resolve().parent.parent
PLAN_PATH = '/api/live/topology/variable/plan'
APPLY_PATH = '/api/live/topology/variable/apply'
STOP_PATH = '/api/live/topology/variable/stop'
PROGRESS_PATH = '/api/live/action/progress'
UUIDS = ['%08x-1111-4222-8333-%012x' % (n, n) for n in range(1, 8)]


class MemberClient(object):
    """A member's own mutable state behind a guarded synthetic connector."""

    def __init__(self, index, engine='mysql', role=None, node_name=None):
        self.variables = dict(version='11.4.9-MariaDB' if engine == 'mariadb' else '8.4.11',
            version_comment='MariaDB Server' if engine == 'mariadb' else 'MySQL Community Server (GPL)',
            server_id=str(index + 1), hostname='node-%d' % (index + 1), port='3306',
            read_only='OFF', max_connections='151', version_compile_os='Linux')
        if engine == 'mysql':
            self.variables.update(server_uuid=UUIDS[index], super_read_only='OFF',
                                  gtid_mode='ON', gtid_executed='', persisted_globals_load='ON')
        else:
            self.variables['gtid_current_pos'] = ''
        if role:
            self.variables.update(group_replication_group_name=UUIDS[6],
                                  group_replication_single_primary_mode='ON')
        self.role, self.state = role, 'ONLINE'
        self.channels, self.replicas = [], []
        self.status = {}
        if node_name is not None:
            self.variables.update(wsrep_on='ON', wsrep_node_name=node_name,
                                  wsrep_provider='/synthetic/libgalera_smm.so',
                                  wsrep_cluster_name='test-galera')
            self.status.update(wsrep_gcomm_uuid=UUIDS[index + 3],
                               wsrep_local_state_comment='Synced', wsrep_local_recv_queue='0')
        self.persisted = {}
        self.calls, self.writes, self.options = [], [], []
        self.before_read = self.before_write = self.after_write = None
        self.read_error = self.write_error = self.gtid_error = None
        self.noop_write = False
        self.adjusted_value = None
        self.password = None
        self.query_timeout = 30
        self.closed = 0
        self.metadata = {'source': 'COMPILED', 'path': '', 'min': '1', 'max': '100000'}
        self.catalog_read_only = False
        self.proxy = None

    def _guard(self):
        callback = getattr(self, '_login_guard', None)
        if callback:
            callback()

    def __enter__(self):
        self._guard()
        return self

    def __exit__(self, *unused):
        self.closed += 1

    def server_version(self):
        return self.variables['version']

    def is_mariadb(self):
        return 'MariaDB' in self.variables['version']

    def identity(self):
        keys = ('server_id', 'hostname', 'port', 'version') if self.is_mariadb() else ('server_uuid',)
        result = {key: self.variables[key] for key in keys}
        if self.is_mariadb() and self.variables.get('wsrep_node_name'):
            result['wsrep_node_name'] = self.variables['wsrep_node_name']
        if self.variables.get('wsrep_on') == 'ON':
            result.update(wsrep_on='ON', wsrep_gcomm_uuid=self.status['wsrep_gcomm_uuid'],
                          wsrep_provider=self.variables['wsrep_provider'])
        return result

    @evidence_options
    def run(self, sql, extra_flags=None, timeout=None):
        if self.before_read:
            self.before_read(sql)
        self._guard()
        self.calls.append(sql)
        if self.read_error:
            raise connector.ConnectorError(self.read_error)
        values = dict(self.variables, **self.status)
        evidence = answer_evidence(sql, extra_flags, timeout, self._plain, values)
        return evidence if evidence is not None else self._plain(sql, extra_flags, timeout)

    def _plain(self, sql, extra_flags=None, timeout=None):
        answers = []
        for unit in read_statements(sql):
            text = re.sub(r'^/\*myflames:[^*]*\*/\s*', '', unit).strip().rstrip(';')
            columns, rows = self._rows(text)
            if '--xml' in (extra_flags or []):
                parts = ['<resultset xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">']
                for row in rows:
                    parts.append('<row>')
                    for column, value in zip(columns, row):
                        parts.append('<field name=%s%s' % (quoteattr(column),
                            ' xsi:nil="true"/>' if value is None else '>' + escape(str(value)) + '</field>'))
                    parts.append('</row>')
                parts.append('</resultset>\n')
                answers.append(''.join(parts))
            else:
                answers.append(batch(rows))
        return ''.join(answers)

    def _rows(self, text):
        lower = text.lower()
        if lower == 'select @@version_comment':
            return ['@@version_comment'], [[self.variables['version_comment']]]
        if lower == 'select @@version':
            return ['@@version'], [[self.variables['version']]]
        if lower in ('select @@global.gtid_executed', 'select @@global.gtid_current_pos'):
            if self.gtid_error:
                raise connector.ConnectorError(self.gtid_error)
            name = lower.split('.')[-1]
            return ['@@GLOBAL.' + name], [[self.variables[name]]]
        if "variable_name = 'admin-version'" in lower:
            return ['variable_value'], [[self.variables['version']]] if self.proxy == 'proxysql' else []
        if lower == 'show readyset version':
            return ['name', 'value'], [['release version', 'stable-test']]
        if lower.startswith('show global variables') or lower.startswith('show session variables'):
            names = re.findall(r"'([^']+)'", text)
            return ['Variable_name', 'Value'], [[name, self.variables[name]] for name in names
                if name in self.variables and name not in ('gtid_executed', 'gtid_current_pos')]
        if lower.startswith('show global status'):
            names = re.findall(r"'([^']+)'", text)
            return ['Variable_name', 'Value'], [[name, self.status[name]] for name in names if name in self.status]
        if lower.startswith('select member_role'):
            return ['MEMBER_ROLE', 'MEMBER_STATE'], [[self.role.upper(), self.state]] if self.role else []
        if lower.startswith('select member_state'):
            return ['MEMBER_STATE'], [[self.state]] if self.role else []
        if 'from performance_schema.replication_group_member_stats' in lower:
            return ['COUNT_TRANSACTIONS_REMOTE_IN_APPLIER_QUEUE'], [['0']]
        if lower in ('show replica status', 'show slave status', 'show all slaves status'):
            columns = list(self.channels[0]) if self.channels else []
            return columns, [[row.get(key) for key in columns] for row in self.channels]
        if lower in ('show replicas', 'show slave hosts'):
            return ['Server_id', 'Host', 'Port'], self.replicas
        if 'from performance_schema.variables_info' in lower:
            return ['VARIABLE_SOURCE', 'VARIABLE_PATH', 'MIN_VALUE', 'MAX_VALUE', 'SET_TIME', 'SET_USER', 'SET_HOST'], [
                [self.metadata[key] for key in ('source', 'path', 'min', 'max')] + [None, None, None]]
        if 'from performance_schema.persisted_variables' in lower:
            name = re.search(r"VARIABLE_NAME = '([^']+)'", text).group(1)
            value = self.persisted.get(name)
            return ['is_null', 'hex_value'], ([] if name not in self.persisted else
                [['1' if value is None else '0', None if value is None else value.encode('utf-8').hex().upper()]])
        if 'from information_schema.system_variables' in lower:
            name = re.search(r"VARIABLE_NAME = '([^']+)'", text).group(1).lower()
            if name not in self.variables:
                return [], []
            return [], [[name.upper(), None, self.variables[name], 'COMPILE-TIME', '151',
                'GLOBAL', 'BIGINT UNSIGNED', '1', '100000', '1', None,
                'YES' if self.catalog_read_only else 'NO', None, '', 'REQUIRED']]
        if lower == 'show grants':
            return ['Grants'], [["GRANT ALL PRIVILEGES ON *.* TO 'dba'@'%'"]]
        marker = re.fullmatch(r"SELECT '([^']+)'", text, flags=re.I)
        if marker:
            return ['marker'], [[marker.group(1)]]
        if lower == 'select 1':
            return ['1'], [['1']]
        raise AssertionError('Unexpected synthetic SQL: ' + text)

    def run_write(self, sql, extra_flags=None, timeout=None):
        if self.before_write:
            self.before_write(sql)
        self._guard()
        packets = guarded_packets(sql)
        expected = dbops.target_session_guard('mariadb' if self.is_mariadb() else 'mysql', self.identity())[0]
        for guard, body, line in packets:
            if guard != expected:
                raise connector.ConnectorError('ERROR 1242 (21000) at line %d: Subquery returns more than 1 row' % line)
            self.writes.append(sql)
            if self.write_error:
                raise acknowledged_error(connector.ConnectorError(self.write_error), sql)
            text = re.sub(r'^/\*myflames:[^*]*\*/\s*', '', body).strip().rstrip(';')
            match = re.fullmatch(r'SET (GLOBAL|PERSIST|PERSIST_ONLY) ([a-z_]+) = ([0-9]+)', text)
            if not match:
                raise AssertionError('Unexpected synthetic write: ' + text)
            mode, name, value = match.groups()
            if self.noop_write:
                continue
            if self.adjusted_value is not None:
                value = self.adjusted_value
            if mode != 'PERSIST_ONLY':
                self.variables[name] = value
            if mode != 'GLOBAL':
                self.persisted[name] = value
        if self.after_write:
            self.after_write(sql)
        return ''


class TopologyChangeTests(GroupCase):
    def setUp(self):
        super(TopologyChangeTests, self).setUp()
        self.workspace = types.SimpleNamespace(store=self.store)
        self.profiles = [self.save('member-%d' % (i + 1), 'G', type='development') for i in range(3)]
        self.clients = {profile['host']: MemberClient(i) for i, profile in enumerate(self.profiles)}
        self.opened = []
        self.connection = dict(self.request(self.profiles[0]), group_name='G')
        self.progress_number = 0
        patcher = mock.patch('myflames.connector.MySQLConnection', side_effect=self.factory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def factory(self, **options):
        self.opened.append(dict(options))
        client = self.clients[options['host']]
        client.password = options.get('password')
        client.options.append(dict(options))
        return client

    def client(self, index):
        return self.clients[self.profiles[index]['host']]

    def plan(self, **changes):
        payload = dict(connection=self.connection, name='max_connections', value='300', mode='global',
                       member_ids=[p['id'] for p in self.profiles])
        payload.update(changes)
        return ui.api_response(PLAN_PATH, payload, self.workspace)

    def apply(self, plan, **changes):
        self.progress_number += 1
        payload = dict(connection=self.connection, plan_id=plan['plan_id'], expect=copy.deepcopy(plan['expect']),
                       confirmed=True, typed_name='G', progress_id='topology-progress-%04d' % self.progress_number)
        payload.update(changes)
        return ui.api_response(APPLY_PATH, payload, self.workspace)

    def records(self):
        return dbops.audit_report(self.store)['records']

    def written_ids(self):
        return [p['id'] for p in self.profiles if self.clients[p['host']].writes]

    def change_profile(self, index, **updates):
        profile = self.store.get('profile', self.profiles[index]['id'])
        profile.update(updates)
        return self.store.save('profile', profile, profile['id'], password='test-password', remember=True)

    @staticmethod
    def statuses(result):
        return {member['profile_id']: member['status'] for member in result['members']}

    def make_gr(self):
        for index, role in enumerate(('primary', 'secondary', 'secondary')):
            client = self.client(index)
            client.role = role
            client.variables.update(group_replication_group_name=UUIDS[6], group_replication_single_primary_mode='ON')

    def make_async(self):
        for index in (1, 2):
            self.client(index).channels = [dict(Channel_Name='', Source_Host=self.profiles[index - 1]['host'],
                Source_Port='3306', Source_UUID=UUIDS[index - 1], Source_Server_Id=str(index),
                Replica_IO_Running='Yes', Replica_SQL_Running='Yes', Seconds_Behind_Source='0')]
        self.client(0).replicas = [['2', self.profiles[1]['host'], '3306']]
        self.client(1).replicas = [['3', self.profiles[2]['host'], '3306']]

    def make_async_with_fourth_branch(self):
        self.make_async()
        profile = self.save('member-4', 'G', type='development')
        self.profiles.append(profile)
        self.clients[profile['host']] = MemberClient(3)
        self.client(3).channels = [dict(Channel_Name='', Source_Host=self.profiles[0]['host'],
            Source_Port='3306', Source_UUID=UUIDS[0], Source_Server_Id='1',
            Replica_IO_Running='Yes', Replica_SQL_Running='Yes', Seconds_Behind_Source='0')]
        self.client(0).replicas.append(['4', profile['host'], '3306'])

    def make_async_with_two_chains(self):
        self.make_async()
        for index in range(3, 6):
            profile = self.save('member-%d' % (index + 1), 'G', type='development')
            self.profiles.append(profile)
            self.clients[profile['host']] = MemberClient(index)
        for index in (4, 5):
            self.client(index).channels = [dict(Channel_Name='', Source_Host=self.profiles[index - 1]['host'],
                Source_Port='3306', Source_UUID=UUIDS[index - 1], Source_Server_Id=str(index),
                Replica_IO_Running='Yes', Replica_SQL_Running='Yes', Seconds_Behind_Source='0')]
            self.client(index - 1).replicas = [[str(index + 1), self.profiles[index]['host'], '3306']]

    def test_saved_name_order_and_exact_own_login_statement_and_audit(self):
        self.change_profile(0, name='Zulu')
        self.change_profile(1, name='Alpha')
        self.change_profile(2, name='Beta')
        plan = self.plan()
        self.assertEqual([m['profile_id'] for m in plan['members']], ['member-2', 'member-3', 'member-1'])
        self.assertIn('name', plan['order_rule'].lower())
        self.assertEqual(plan['schema_version'], 'topology-change-plan-1.0')
        self.assertTrue(all(m['statement'] == 'SET GLOBAL max_connections = 300' for m in plan['members']))
        order = []
        for index in range(3):
            self.client(index).after_write = lambda sql, i=index: order.append('member-%d' % (i + 1))
        result = self.apply(plan)
        self.assertEqual(result['status'], 'applied', result)
        self.assertEqual(order, ['member-2', 'member-3', 'member-1'])
        self.assertTrue(all(m['status'] == 'applied' for m in result['members']))
        self.assertEqual(result['schema_version'], 'topology-change-1.0')
        for profile in self.profiles:
            client = self.clients[profile['host']]
            self.assertTrue(all(options['host'] == profile['host'] for options in client.options))
            self.assertTrue(all(options['password'] == 'test-password' for options in client.options))
            self.assertEqual(client.variables['max_connections'], '300')
            guard, body, _line = guarded_packets(client.writes[0])[0]
            self.assertIn(client.variables['server_uuid'], guard)
            self.assertIn('SET GLOBAL max_connections = 300', body)
        records = self.records()
        self.assertEqual(len(records), 3)
        for record in records:
            self.assertEqual(record['operation'], 'topology.variable')
            self.assertEqual(record['target']['change_set_id'], plan['change_set_id'])
            self.assertIn(record['target']['profile_id'], self.written_ids())
            self.assertTrue(record['target']['identity'])
            self.assertEqual(record['status'], 'ok')
        self.assertNotIn('test-password', json.dumps(records))

    def test_gr_secondaries_before_primary(self):
        self.make_gr()
        plan = self.plan()
        self.assertEqual([m['profile_id'] for m in plan['members']], ['member-2', 'member-3', 'member-1'])
        self.assertIn('primary', plan['order_rule'].lower())
        self.assertEqual(self.apply(plan)['status'], 'applied')

    def test_async_replica_of_replica_first_then_root_source(self):
        self.make_async()
        plan = self.plan()
        self.assertEqual([m['profile_id'] for m in plan['members']], ['member-3', 'member-2', 'member-1'])
        self.assertIn('replica', plan['order_rule'].lower())
        self.assertEqual(self.apply(plan)['status'], 'applied')

    def test_galera_uses_live_node_names_and_keeps_node_uuid(self):
        for index, node in enumerate(('zeta', 'alpha', 'beta')):
            self.clients[self.profiles[index]['host']] = MemberClient(index, 'mariadb', node_name=node)
        plan = self.plan()
        self.assertEqual([m['profile_id'] for m in plan['members']], ['member-2', 'member-3', 'member-1'])
        self.assertIn('node', plan['order_rule'].lower())
        for member in plan['members']:
            self.assertTrue(member['identity'].get('wsrep_gcomm_uuid'))
        self.assertEqual(self.apply(plan)['status'], 'applied')

    def test_mariadb_persist_modes_review_global_and_option_line(self):
        self.clients[self.profiles[0]['host']] = MemberClient(0, 'mariadb')
        for mode in ('persist', 'persist_only'):
            with self.subTest(mode=mode):
                plan = self.plan(mode=mode, member_ids=['member-1'])
                member = plan['members'][0]
                self.assertEqual(member['status'], 'planned')
                self.assertEqual(member['statement'], 'SET GLOBAL max_connections = 300')
                self.assertEqual(member['config_line'], 'max_connections = 300')
                self.assertIn("Not persisted: add this line to the server's option file", json.dumps(member))
                for key in ('server_id', 'hostname', 'port'):
                    self.assertTrue(member['identity'][key])
        self.assertEqual(self.apply(plan)['status'], 'applied')
        self.assertEqual(self.client(0).persisted, {})

    def test_unchanged_member_gets_no_statement_and_no_send(self):
        self.client(0).variables['max_connections'] = '300'
        plan = self.plan()
        member = next(m for m in plan['members'] if m['profile_id'] == 'member-1')
        self.assertEqual(member['status'], 'unchanged')
        self.assertIsNone(member['statement'])
        result = self.apply(plan)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(self.statuses(result)['member-1'], 'unchanged')
        self.assertEqual(self.client(0).writes, [])
        self.assertEqual(len(self.records()), 3)

    def test_persist_unchanged_requires_both_requested_scopes(self):
        self.client(0).variables['max_connections'] = '300'
        plan = self.plan(mode='persist', member_ids=['member-1'])
        self.assertEqual(plan['members'][0]['status'], 'planned')
        self.client(0).persisted['max_connections'] = '300'
        plan = self.plan(mode='persist', member_ids=['member-1'])
        self.assertEqual(plan['members'][0]['status'], 'unchanged')
        self.assertIsNone(plan['members'][0]['statement'])

    def test_invalid_member_plan_is_listed_and_stops_before_later_members(self):
        self.client(1).variables.pop('max_connections')
        plan = self.plan()
        member = next(m for m in plan['members'] if m['profile_id'] == 'member-2')
        self.assertEqual(member['status'], 'refused')
        self.assertTrue(member['reason'])
        self.assertIsNone(member['statement'])
        opened = len(self.opened)
        result = self.apply(plan)
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(result['message'], 'Untick the refused servers and review again.')
        self.assertEqual(self.statuses(result), dict.fromkeys(('member-1', 'member-2', 'member-3'), 'not_sent'))
        refused = next(m for m in result['members'] if m['profile_id'] == 'member-2')
        self.assertEqual(refused['reason'], member['reason'])
        self.assertEqual(refused['message'], member['reason'])
        self.assertEqual(len(self.opened), opened)
        self.assertEqual(self.written_ids(), [])
        self.assertEqual(len(self.records()), 3)
        self.assertTrue(all(r['status'] == 'refused' for r in self.records()))

    def test_duplicate_mysql_targets_refuse_whole_plan_in_reviewed_order(self):
        self.change_profile(0, name='Zulu')
        self.change_profile(1, name='Alpha')
        self.change_profile(2, name='Beta')
        for index in (0, 2):
            self.client(index).variables['server_uuid'] = self.client(1).variables['server_uuid'].upper()
        plan = self.plan()
        self.assertEqual([m['profile_id'] for m in plan['members']], ['member-2', 'member-3', 'member-1'])
        self.assertEqual(plan['members'][0]['status'], 'planned')
        self.assertIsNone(plan['members'][0]['duplicate_of'])
        for member in plan['members'][1:]:
            self.assertEqual(member['status'], 'refused')
            self.assertEqual(member['duplicate_of'], 'member-2')
            self.assertEqual(member['reason'], 'This saved connection reaches the same server as Alpha.')
            self.assertIsNone(member['statement'])
            self.assertIsNone(member['after'])
        opened = len(self.opened)
        result = self.apply(plan)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(result['message'], 'Untick the refused servers and review again.')
        self.assertTrue(all(m['status'] == 'not_sent' for m in result['members']))
        self.assertEqual(len(self.opened), opened)
        self.assertEqual(self.written_ids(), [])
        self.assertEqual(len(self.records()), 3)
        self.assertTrue(all(record['status'] == 'refused' for record in self.records()))
        for value, schema in ((plan, 'topology-change-plan-1.0.json'), (result, 'topology-change-1.0.json')):
            validate(value, json.loads((ROOT / 'docs' / 'schemas' / schema).read_text(encoding='utf-8')))

    def test_invalid_plan_needs_no_confirmation_even_after_policy_becomes_read_only(self):
        self.change_profile(2, type='production')
        self.client(1).variables.pop('max_connections')
        plan = self.plan()
        opened = len(self.opened)
        for read_only in (False, True):
            with self.subTest(read_only=read_only):
                self.change_profile(2, read_only=read_only)
                result = self.apply(plan, confirmed=False, typed_name='')
                self.assertEqual(result['status'], 'refused', result)
                self.assertEqual(result['message'], 'Untick the refused servers and review again.')
                self.assertTrue(all(m['status'] == 'not_sent' for m in result['members']))
                self.assertEqual(len(self.opened), opened)
                self.assertEqual(self.written_ids(), [])

    def test_unselected_alias_does_not_refuse_selected_target(self):
        self.client(1).variables['server_uuid'] = self.client(0).variables['server_uuid']
        plan = self.plan(member_ids=['member-2'])
        self.assertEqual(plan['members'][0]['status'], 'planned', plan)
        self.assertIsNone(plan['members'][0]['duplicate_of'])
        self.assertEqual(self.apply(plan)['status'], 'applied')
        self.assertEqual(self.written_ids(), ['member-2'])

    def test_unselected_source_alias_keeps_async_replica_before_source(self):
        self.make_async()
        self.client(2).variables['server_uuid'] = self.client(0).variables['server_uuid']
        self.client(2).channels = []
        self.client(2).replicas = list(self.client(0).replicas)
        self.client(1).replicas = []
        plan = self.plan(member_ids=['member-1', 'member-2'])
        self.assertEqual([m['profile_id'] for m in plan['members']], ['member-2', 'member-1'])
        self.assertEqual([m['profile_id'] for m in plan['expect']['order_dependencies']], ['member-3'])
        order = []
        for index in (0, 1):
            self.client(index).after_write = lambda sql, i=index: order.append('member-%d' % (i + 1))
        self.assertEqual(self.apply(plan)['status'], 'applied')
        self.assertEqual(order, ['member-2', 'member-1'])
        self.assertEqual(self.client(2).writes, [])

    def test_unchanged_alias_still_refuses_duplicate_selection(self):
        self.client(0).variables['max_connections'] = '300'
        self.client(1).variables['server_uuid'] = self.client(0).variables['server_uuid']
        plan = self.plan()
        self.assertEqual(plan['members'][0]['status'], 'unchanged')
        self.assertEqual(plan['members'][1]['status'], 'refused')
        self.assertEqual(plan['members'][1]['duplicate_of'], 'member-1')
        self.assertEqual(self.apply(plan)['status'], 'refused')
        self.assertEqual(self.written_ids(), [])

    def test_mariadb_duplicates_require_server_id_hostname_and_port(self):
        for index in range(3):
            self.clients[self.profiles[index]['host']] = MemberClient(index, 'mariadb')
        original = self.client(0).variables
        for key in ('server_id', 'hostname', 'port'):
            self.client(1).variables[key] = original[key]
        # Version is metadata, not a discriminator for duplicate destinations.
        self.client(1).variables['version'] = '11.4.8-MariaDB'
        self.client(2).variables['server_id'] = original['server_id']
        plan = self.plan()
        self.assertEqual([m['status'] for m in plan['members']], ['planned', 'refused', 'planned'], plan)
        self.assertEqual(plan['members'][1]['duplicate_of'], 'member-1')
        self.assertEqual(self.apply(plan)['status'], 'refused')
        self.assertEqual(self.written_ids(), [])
        self.client(1).variables['port'] = '3307'
        plan = self.plan()
        self.assertTrue(all(m['status'] == 'planned' for m in plan['members']), plan)
        self.assertEqual(self.apply(plan)['status'], 'applied')

    def test_galera_duplicate_identity_is_node_uuid(self):
        for index, node in enumerate(('alpha', 'beta', 'gamma')):
            self.clients[self.profiles[index]['host']] = MemberClient(index, 'mariadb', node_name=node)
        self.client(1).status['wsrep_gcomm_uuid'] = self.client(0).status['wsrep_gcomm_uuid'].upper()
        plan = self.plan()
        self.assertEqual([m['status'] for m in plan['members']], ['planned', 'refused', 'planned'], plan)
        self.assertEqual(plan['members'][1]['duplicate_of'], 'member-1')
        self.assertEqual(self.apply(plan)['status'], 'refused')
        self.assertEqual(self.written_ids(), [])

    def test_nil_mysql_and_galera_node_identity_refuses_before_send(self):
        self.client(0).variables['server_uuid'] = '00000000-0000-0000-0000-000000000000'
        self.clients[self.profiles[1]['host']] = MemberClient(1, 'mariadb', node_name='beta')
        self.client(1).status['wsrep_gcomm_uuid'] = '00000000-0000-0000-0000-000000000000'
        plan = self.plan()
        by_id = {member['profile_id']: member for member in plan['members']}
        for pid in ('member-1', 'member-2'):
            self.assertEqual(by_id[pid]['status'], 'refused', plan)
            self.assertIsNone(by_id[pid]['statement'])
        self.assertEqual(self.apply(plan)['status'], 'refused')
        self.assertEqual(self.written_ids(), [])

    def test_gtid_reads_are_bound_but_positions_do_not_invalidate_review(self):
        self.clients[self.profiles[1]['host']] = MemberClient(1, 'mariadb')
        plan = self.plan()
        self.assertTrue(all(m['status'] == 'planned' for m in plan['members']), plan)
        for index, name in ((0, 'gtid_executed'), (1, 'gtid_current_pos')):
            client = self.client(index)
            self.assertTrue(any(sql.startswith('DELIMITER ') and 'SELECT @@GLOBAL.' + name in sql
                                for sql in client.calls))
            client.variables[name] = UUIDS[0] + ':1-200' if index == 0 else '0-1-200'
        self.assertEqual(self.apply(plan)['status'], 'applied')

    def test_optional_gtid_server_error_does_not_poison_proven_identity(self):
        self.clients[self.profiles[1]['host']] = MemberClient(1, 'mariadb')
        for index in (0, 1):
            self.client(index).gtid_error = 'ERROR 1193 (HY000): Unknown system variable'
        plan = self.plan()
        self.assertTrue(all(m['status'] == 'planned' for m in plan['members']), plan)
        self.assertEqual(self.apply(plan)['status'], 'applied')

    def test_router_switch_during_bound_gtid_read_poisoned_even_when_optional(self):
        def switch(sql):
            if sql.startswith('DELIMITER ') and 'SELECT @@GLOBAL.gtid_executed' in sql:
                self.client(0).variables['server_uuid'] = UUIDS[4]
        self.client(0).before_read = switch
        plan = self.plan()
        self.assertEqual(plan['members'][0]['status'], 'refused', plan)
        self.assertIsNone(plan['members'][0]['statement'])
        self.assertEqual(self.apply(plan)['status'], 'refused')
        self.assertEqual(self.written_ids(), [])

    def test_offline_gr_row_uses_async_order_and_connecting_channel(self):
        self.make_async()
        self.make_gr()
        for index in range(3):
            self.client(index).state = 'OFFLINE'
        self.client(2).channels[0]['Replica_IO_Running'] = 'Connecting'
        plan = self.plan()
        self.assertEqual([m['profile_id'] for m in plan['members']], ['member-3', 'member-2', 'member-1'])
        self.assertIn('Asynchronous', plan['order_rule'])
        self.assertEqual([m['order_facts']['role'] for m in plan['expect']['members']],
                         ['replica', 'replica', 'source'])
        self.assertEqual(self.apply(plan)['status'], 'applied')

    def test_read_only_anywhere_refuses_plan_before_secret_lookup(self):
        self.change_profile(2, read_only=True)
        with mock.patch.object(self.store, 'connection_with_saved_password', wraps=self.store.connection_with_saved_password) as secret:
            with self.assertRaises(ValueError):
                self.plan(member_ids=['member-1'])
            secret.assert_not_called()
        self.assertEqual(self.opened, [])

    def test_production_needs_confirmation_and_exact_group_name(self):
        self.change_profile(2, type='production')
        plan = self.plan(member_ids=['member-1'])
        self.assertEqual(plan['effective_safety']['type'], 'production')
        self.assertTrue(plan['confirmation']['required'])
        self.assertEqual(plan['confirmation']['typed_name'], 'G')
        for changes in ({'confirmed': False}, {'typed_name': ''}, {'typed_name': 'g'}):
            with self.subTest(changes=changes):
                opened = len(self.opened)
                result = self.apply(plan, **changes)
                self.assertEqual(result['status'], 'refused', result)
                self.assertEqual(len(self.opened), opened)
                self.assertEqual(self.written_ids(), [])
        self.assertEqual(self.apply(plan)['status'], 'applied')

    def test_changed_expect_is_stale_before_login_and_never_sends(self):
        plan = self.plan()
        expect = copy.deepcopy(plan['expect'])
        def replace_statement(value):
            if isinstance(value, dict):
                if isinstance(value.get('statement'), str):
                    value['statement'] = 'SET GLOBAL max_connections = 9999'
                    return True
                return any(replace_statement(item) for item in value.values())
            if isinstance(value, list):
                return any(replace_statement(item) for item in value)
            return False
        self.assertTrue(replace_statement(expect), 'expect must bind every exact member statement')
        opened = len(self.opened)
        result = self.apply(plan, expect=expect)
        self.assertEqual(result['status'], 'stale', result)
        self.assertEqual(len(self.opened), opened)
        self.assertEqual(self.written_ids(), [])

    def test_foreign_group_cannot_replay_review(self):
        plan = self.plan()
        other = self.save('other', 'Other', type='development')
        connection = dict(self.request(other), group_name='Other')
        opened = len(self.opened)
        try:
            result = self.apply(plan, connection=connection)
        except ValueError:
            pass
        else:
            self.assertIn(result['status'], ('refused', 'stale'))
        self.assertEqual(len(self.opened), opened)
        self.assertEqual(self.written_ids(), [])

    def test_review_token_is_one_use(self):
        plan = self.plan()
        self.assertEqual(self.apply(plan)['status'], 'applied')
        written = sum(len(client.writes) for client in self.clients.values())
        try:
            result = self.apply(plan)
        except ValueError:
            pass
        else:
            self.assertIn(result['status'], ('refused', 'stale'))
        self.assertEqual(sum(len(client.writes) for client in self.clients.values()), written)

    def test_deleted_and_recreated_group_refuses_old_generation(self):
        plan = self.plan()
        before = self.store.get('profile', 'member-1')['group_generation']
        self.store.delete_connection_group('G')
        self.store.save_connection_group('G', ['member-1', 'member-2', 'member-3'])
        self.assertNotEqual(before, self.store.get('profile', 'member-1')['group_generation'])
        result = self.apply(plan)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(self.written_ids(), [])

    def test_member_removed_during_roll_stops_after_earlier_member(self):
        plan = self.plan()
        self.client(0).after_write = lambda sql: self.store.assign_profile_group(['member-2'], '')
        result = self.apply(plan)
        self.assertEqual(result['status'], 'partial', result)
        self.assertEqual(self.statuses(result), {'member-1': 'applied', 'member-2': 'refused', 'member-3': 'not_sent'})
        self.assertEqual(self.written_ids(), ['member-1'])

    def test_member_removed_before_apply_leaves_earlier_members_applied(self):
        plan = self.plan()
        self.store.assign_profile_group(['member-2'], '')
        result = self.apply(plan)
        self.assertEqual(result['status'], 'partial', result)
        self.assertEqual(self.statuses(result), {'member-1': 'applied', 'member-2': 'refused', 'member-3': 'not_sent'})
        self.assertEqual(self.written_ids(), ['member-1'])

    def test_audit_is_pending_before_each_member_statement(self):
        plan = self.plan()
        seen = []
        def check_pending(index):
            records = [record for record in self.records()
                       if record['target']['profile_id'] == self.profiles[index]['id']]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]['status'], 'pending')
            self.assertEqual(records[0]['target']['change_set_id'], plan['change_set_id'])
            seen.append(self.profiles[index]['id'])
        for index in range(3):
            self.client(index).before_write = lambda sql, index=index: check_pending(index)
        self.assertEqual(self.apply(plan)['status'], 'applied')
        self.assertEqual(seen, ['member-1', 'member-2', 'member-3'])

    def test_endpoint_edit_during_roll_never_uses_new_endpoint(self):
        plan = self.plan()
        self.client(0).after_write = lambda sql: self.change_profile(1, host='unreviewed.example')
        result = self.apply(plan)
        self.assertEqual(result['status'], 'partial', result)
        self.assertFalse(any(options['host'] == 'unreviewed.example' for options in self.opened))
        self.assertEqual(self.written_ids(), ['member-1'])

    def test_saved_endpoint_reaches_other_identity_and_stops(self):
        plan = self.plan()
        self.client(0).after_write = lambda sql: self.client(1).variables.update(server_uuid=UUIDS[4])
        result = self.apply(plan)
        self.assertEqual(result['status'], 'partial', result)
        self.assertEqual(self.statuses(result)['member-2'], 'refused')
        self.assertEqual(self.written_ids(), ['member-1'])

    def test_failover_after_review_refuses_changed_role_before_write(self):
        self.make_gr()
        plan = self.plan()
        self.client(1).role = 'primary'
        self.client(0).role = 'secondary'
        result = self.apply(plan)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(self.written_ids(), [])

    def test_async_source_change_after_review_refuses(self):
        self.make_async()
        plan = self.plan()
        self.client(2).channels[0]['Source_UUID'] = UUIDS[0]
        result = self.apply(plan)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(self.written_ids(), [])

    def test_async_unselected_relay_reparenting_invalidates_reviewed_depth(self):
        self.make_async_with_fourth_branch()
        plan = self.plan(member_ids=['member-3', 'member-4'])
        self.assertEqual([m['profile_id'] for m in plan['members']], ['member-3', 'member-4'])
        dependencies = plan['expect']['order_dependencies']
        self.assertEqual({m['profile_id'] for m in dependencies}, {'member-1', 'member-2'})
        # The selected replicas did not change their own channels. Their
        # relative depth depends on this unselected relay's live parent.
        self.client(1).channels[0].update(Source_Host=self.profiles[3]['host'],
            Source_UUID=UUIDS[3], Source_Server_Id='4')
        result = self.apply(plan)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(self.written_ids(), [])
        self.assertFalse(any(m['status'] in ('applied', 'unchanged') for m in result['members']))

    def test_async_unreadable_unselected_dependency_refuses_selected_plans(self):
        self.make_async_with_fourth_branch()
        self.client(1).read_error = 'ERROR 2013: synthetic relay unavailable'
        plan = self.plan(member_ids=['member-3', 'member-4'])
        self.assertEqual({m['profile_id'] for m in plan['members']}, {'member-3', 'member-4'})
        self.assertTrue(all(m['status'] == 'refused' for m in plan['members']), plan)
        self.assertTrue(all(m['statement'] is None for m in plan['members']), plan)
        self.assertEqual(self.written_ids(), [])

    def test_async_later_selected_relay_reparenting_refuses_before_first_leaf(self):
        self.make_async_with_two_chains()
        plan = self.plan(member_ids=['member-3', 'member-6', 'member-2', 'member-5'])
        self.assertEqual([m['profile_id'] for m in plan['members']],
                         ['member-3', 'member-6', 'member-2', 'member-5'])
        # Both leaves retain their own channels. Moving the future selected
        # relay changes their relative depth before that relay's own turn.
        self.client(4).channels[0].update(Source_Host=self.profiles[2]['host'],
            Source_UUID=UUIDS[2], Source_Server_Id='3')
        result = self.apply(plan)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(self.written_ids(), [])
        self.assertFalse(any(m['status'] in ('applied', 'unchanged') for m in result['members']))

    def test_async_removed_selected_relay_stops_at_its_reviewed_turn(self):
        self.make_async_with_two_chains()
        plan = self.plan(member_ids=['member-3', 'member-6', 'member-2', 'member-5'])
        self.assertEqual([m['profile_id'] for m in plan['members']],
                         ['member-3', 'member-6', 'member-2', 'member-5'])
        self.store.assign_profile_group(['member-2'], '')
        result = self.apply(plan)
        self.assertEqual(result['status'], 'partial', result)
        self.assertEqual(self.written_ids(), ['member-3', 'member-6'])
        self.assertEqual(self.statuses(result), {'member-3': 'applied', 'member-6': 'applied',
                                               'member-2': 'refused', 'member-5': 'not_sent'})

    def test_member_becomes_read_only_mid_run(self):
        plan = self.plan()
        self.client(0).after_write = lambda sql: self.change_profile(1, read_only=True)
        result = self.apply(plan)
        self.assertEqual(result['status'], 'partial', result)
        self.assertEqual(self.written_ids(), ['member-1'])
        self.assertTrue(any(r['status'] == 'refused' for r in self.records()))

    def test_read_only_change_at_write_login_finishes_unsent_audit(self):
        plan = self.plan()
        self.client(0).before_write = lambda sql: self.change_profile(0, read_only=True)
        result = self.apply(plan)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(self.written_ids(), [])
        self.assertFalse(any(r['status'] == 'pending' for r in self.records()))

    def test_variable_drift_after_review_refuses_and_stops(self):
        plan = self.plan()
        self.client(0).variables['max_connections'] = '200'
        result = self.apply(plan)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(self.written_ids(), [])

    def test_warning_only_noop_stops_without_success_messages(self):
        plan = self.plan()
        self.client(0).noop_write = True
        result = self.apply(plan)
        self.assertIn(result['status'], ('refused', 'partial'), result)
        self.assertEqual(self.written_ids(), ['member-1'])
        self.assertEqual(self.client(0).variables['max_connections'], '151')
        self.assertNotIn(self.statuses(result)['member-1'], ('applied', 'unchanged'))
        self.assertEqual(self.statuses(result)['member-2'], 'not_sent')
        self.assertEqual(self.statuses(result)['member-3'], 'not_sent')
        messages = [result.get('message')] + [m.get('message') for m in result['members']]
        messages += [record.get('message') for record in self.records()]
        self.assertNotIn('already set', json.dumps(messages).lower())
        self.assertNotIn('was applied', json.dumps(messages).lower())

    def test_adjusted_value_stops_without_success_messages(self):
        plan = self.plan()
        self.client(0).adjusted_value = '299'
        result = self.apply(plan)
        self.assertIn(result['status'], ('refused', 'partial'), result)
        self.assertEqual(self.written_ids(), ['member-1'])
        self.assertEqual(self.client(0).variables['max_connections'], '299')
        self.assertNotIn(self.statuses(result)['member-1'], ('applied', 'unchanged'))
        self.assertEqual(self.statuses(result)['member-2'], 'not_sent')
        self.assertEqual(self.statuses(result)['member-3'], 'not_sent')
        messages = [result.get('message')] + [m.get('message') for m in result['members']]
        messages += [record.get('message') for record in self.records()]
        self.assertNotIn('already set', json.dumps(messages).lower())
        self.assertNotIn('was applied', json.dumps(messages).lower())

    def test_stop_during_statement_does_not_interrupt_it_or_start_next(self):
        plan = self.plan()
        progress_id = 'rolling-stop-progress-0001'
        stop_responses, progress = [], []
        def stopping(sql):
            progress.append(ui.api_response(PROGRESS_PATH, {'progress_id': progress_id}, self.workspace))
            stop_responses.append(ui.api_response(STOP_PATH,
                dict(connection=self.connection, progress_id=progress_id), self.workspace))
        self.client(0).before_write = stopping
        result = self.apply(plan, progress_id=progress_id)
        self.assertEqual(result['status'], 'stopped', result)
        self.assertEqual(self.written_ids(), ['member-1'])
        self.assertEqual(self.client(0).variables['max_connections'], '300')
        self.assertEqual(self.statuses(result), {'member-1': 'applied', 'member-2': 'not_sent', 'member-3': 'not_sent'})
        self.assertIn('interrupt', json.dumps(stop_responses).lower())
        self.assertIn('member-1', json.dumps(progress))
        final = ui.api_response(PROGRESS_PATH, {'progress_id': progress_id}, self.workspace)
        self.assertEqual(final['phase'], 'finished')
        self.assertEqual(final['status'], 'stopped')

    def test_server_error_never_echoes_sql_or_password(self):
        plan = self.plan()
        self.client(1).write_error = ('ERROR 1231 (42000) at line 2: test-password '
                                     'SELECT secret_server_statement FROM credentials')
        result = self.apply(plan)
        self.assertEqual(result['status'], 'partial', result)
        self.assertEqual(self.client(2).writes, [])
        text = json.dumps([result, self.records()])
        self.assertNotIn('test-password', text)
        self.assertNotIn('secret_server_statement', text)
        self.assertNotIn('credentials', text)
        self.assertEqual(len(self.records()), 3)

    def test_plan_read_error_is_sanitized_and_member_refused(self):
        self.client(1).read_error = 'ERROR 1045: test-password SELECT secret_server_statement'
        plan = self.plan()
        member = next(m for m in plan['members'] if m['profile_id'] == 'member-2')
        self.assertEqual(member['status'], 'refused')
        self.assertIsNone(member['statement'])
        self.assertNotIn('test-password', json.dumps(plan))
        self.assertNotIn('secret_server_statement', json.dumps(plan))

    def test_member_ids_validation_precedes_login(self):
        self.save('outside', 'Other', type='development')
        for ids in ([], ['unknown'], ['outside'], ['member-1', 'member-1'], [1],
                    ['member-1'] * 17, 'member-1'):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.plan(member_ids=ids)
        self.assertEqual(self.opened, [])

    def test_proxy_member_cannot_get_a_variable_statement(self):
        self.client(1).variables['version_comment'] = 'Readyset'
        self.client(1).proxy = 'readyset'
        plan = self.plan()
        member = next(m for m in plan['members'] if m['profile_id'] == 'member-2')
        self.assertEqual(member['status'], 'refused')
        self.assertIsNone(member['statement'])
        self.assertEqual(self.client(1).writes, [])

    def test_unknown_keys_and_wrong_types_are_rejected(self):
        for extra in ({'unexpected': True}, {'mode': 'session'}, {'value': {}},
                      {'connection': dict(self.connection, group_generation='untrusted')},
                      {'connection': dict(self.connection, password='untrusted-password')}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.plan(**extra)
        plan = self.plan()
        for extra in ({'statement': 'SET GLOBAL max_connections = 9999'}, {'confirmed': 'true'},
                      {'typed_name': 42}, {'expect': []}, {'progress_id': 'short'}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.apply(plan, **extra)
        for extra in ({'unknown': True}, {'progress_id': 'short'}):
            payload = dict(connection=self.connection, progress_id='not-running-progress-1')
            payload.update(extra)
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                ui.api_response(STOP_PATH, payload, self.workspace)
        self.assertEqual(self.written_ids(), [])

    def test_every_endpoint_requires_a_current_group_connection(self):
        plain = self.request(self.profiles[0])
        for path, payload in ((PLAN_PATH, dict(name='max_connections', value='300', mode='global', member_ids=['member-1'])),
                              (APPLY_PATH, dict(plan_id='missing-plan', expect={}, confirmed=True,
                                                progress_id='unknown-progress-id')),
                              (STOP_PATH, dict(progress_id='unknown-progress-id'))):
            for connection in (plain, dict(self.connection, profile_id='missing')):
                with self.subTest(path=path, connection=connection), self.assertRaises(ValueError):
                    ui.api_response(path, dict(payload, connection=connection), self.workspace)
        self.assertEqual(self.opened, [])

    def test_routes_are_post_allowed_and_apply_is_a_write(self):
        self.assertIn(APPLY_PATH, ui._WRITE_PATHS)
        self.assertNotIn(PLAN_PATH, ui._SERVER_VIEW_PATHS)
        self.assertNotIn(APPLY_PATH, ui._SERVER_VIEW_PATHS)
        self.assertNotIn(STOP_PATH, ui._SERVER_VIEW_PATHS)
        server = types.SimpleNamespace(origin='http://127.0.0.1:8765', token='test-token',
                                       cookie_name='myflames_test', session_secret='test-secret', store=self.store)
        for path in (PLAN_PATH, APPLY_PATH, STOP_PATH):
            with self.subTest(path=path):
                body = b'{}'
                handler = object.__new__(ui.WorkspaceHandler)
                handler.path, handler.command, handler.server = path, 'POST', server
                handler.headers = Message()
                for key, value in {'Host': '127.0.0.1:8765', 'Origin': server.origin,
                    'Cookie': 'myflames_test=test-secret', 'X-Myflames-Token': server.token,
                    'Content-Type': 'application/json', 'Content-Length': str(len(body))}.items():
                    handler.headers[key] = value
                replies = []
                handler._read_body = lambda size: body
                handler._send = lambda code, value, **kwargs: replies.append((code, value))
                handler.do_POST()
                self.assertEqual(replies[0][0], 400, replies)
                self.assertNotEqual(replies[0][1].get('error'), 'Not found.')

    def test_plan_and_result_match_published_schemas(self):
        plan = self.plan()
        result = self.apply(plan)
        for value, schema in ((plan, 'topology-change-plan-1.0.json'), (result, 'topology-change-1.0.json')):
            with self.subTest(schema=schema):
                validate(value, json.loads((ROOT / 'docs' / 'schemas' / schema).read_text(encoding='utf-8')))


if __name__ == '__main__':
    unittest.main()
