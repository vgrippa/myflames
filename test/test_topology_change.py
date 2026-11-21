"""Rolling group changes use member-owned logins and one exact review.

The clients below are synthetic transport boundaries. Store membership,
secrets, policy, review tokens, variable validation, identity packets and
audit records are the production implementations, never patched checks.
"""
import base64
import copy
import json
import re
import threading
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
            self.variables['server_uid'] = base64.b64encode(bytes([index + 1]) * 20).decode('ascii')
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
        self.catalog_type = 'BIGINT UNSIGNED'
        self.show_hidden_variables = set()
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

    def rolling_identity(self):
        identity = dict(self.identity(), version=self.variables['version'])
        if self.is_mariadb():
            identity['server_uid'] = self.variables['server_uid']
        return identity

    @evidence_options
    def run(self, sql, extra_flags=None, timeout=None):
        if self.before_read:
            self.before_read(sql)
        self._guard()
        self.calls.append(sql)
        if self.read_error:
            raise connector.ConnectorError(self.read_error)
        # Rolling reads keep their identity predicate in the same packet as
        # the evidence prefix. A changed server refuses before any body read.
        prefixed = re.match(r'^(DELIMITER [^\n]+\n)(SET @myflames_target = [^\n]+?;)[ \n]+', sql)
        if prefixed:
            identity = {key: self.variables[key] for key in
                        ('server_uid', 'server_id', 'hostname', 'port', 'version')}
            expected = {topology_change._session_guard('mariadb', item)[0]
                        for item in (identity, self.rolling_identity())}
            if prefixed.group(2) not in expected:
                raise connector.ConnectorError('ERROR 1242 (21000) at line 2: Subquery returns more than 1 row')
            sql = prefixed.group(1) + sql[prefixed.end():]
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
        if lower == 'select @@global.server_uid':
            return ['@@global.server_uid'], [[self.variables['server_uid']]]
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
                if name in self.variables and name not in ('gtid_executed', 'gtid_current_pos') and
                (not lower.startswith('show global variables') or name not in self.show_hidden_variables)]
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
                'GLOBAL', self.catalog_type, '1', '100000', '1', None,
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
        engine = 'mariadb' if self.is_mariadb() else 'mysql'
        expected = topology_change._session_guard(engine, self.rolling_identity())[0]
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

    def test_unchanged_member_value_changed_before_its_turn_refuses(self):
        self.client(1).variables['max_connections'] = '300'
        plan = self.plan()
        self.assertEqual(plan['members'][1]['status'], 'unchanged')
        self.client(0).after_write = lambda sql: self.client(1).variables.update(max_connections='301')
        result = self.apply(plan)
        self.assertEqual(result['status'], 'partial', result)
        self.assertEqual(self.statuses(result), {'member-1': 'applied', 'member-2': 'refused', 'member-3': 'not_sent'})
        self.assertEqual(self.written_ids(), ['member-1'])
        self.assertNotIn('already set', result['members'][1]['message'])

    def test_unchanged_persist_member_rereads_persisted_scope(self):
        self.client(0).variables['max_connections'] = '300'
        self.client(0).persisted['max_connections'] = '300'
        plan = self.plan(mode='persist', member_ids=['member-1'])
        self.assertEqual(plan['members'][0]['status'], 'unchanged')
        self.client(0).persisted['max_connections'] = '301'
        result = self.apply(plan)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(result['members'][0]['status'], 'refused')
        self.assertEqual(self.written_ids(), [])

    def test_unchanged_member_identity_and_role_still_bound(self):
        for field in ('identity', 'role'):
            with self.subTest(field=field):
                self.client(0).variables['server_uuid'] = UUIDS[0]
                self.client(0).role = None
                self.client(0).variables['max_connections'] = '300'
                plan = self.plan(member_ids=['member-1'])
                self.assertEqual(plan['members'][0]['status'], 'unchanged')
                if field == 'identity':
                    self.client(0).variables['server_uuid'] = UUIDS[4]
                else:
                    self.client(0).role = 'primary'
                    self.client(0).variables.update(group_replication_group_name=UUIDS[6],
                                                   group_replication_single_primary_mode='ON')
                result = self.apply(plan)
                self.assertEqual(result['status'], 'refused', result)
                self.assertEqual(result['members'][0]['status'], 'refused')
                self.assertEqual(self.written_ids(), [])

    def test_default_refused_before_password_lookup(self):
        for value in ('DEFAULT', 'default', 'DeFaUlT'):
            with self.subTest(value=value), mock.patch.object(self.store, 'connection_with_saved_password',
                    wraps=self.store.connection_with_saved_password) as password:
                with self.assertRaisesRegex(ValueError,
                        'DEFAULT cannot be verified across servers; set an explicit value.'):
                    self.plan(value=value)
                password.assert_not_called()
        self.assertEqual(self.opened, [])

    def test_flagset_defaults_refused_in_running_value_modes(self):
        self.client(0).variables['optimizer_switch'] = 'index_merge=on,index_merge_union=on'
        for mode in ('global', 'persist'):
            for value in ('index_merge=default', 'index_merge = DeFaUlT,index_merge_union=off'):
                with self.subTest(mode=mode, value=value):
                    plan = self.plan(name='optimizer_switch', value=value, mode=mode, member_ids=['member-1'])
                    member = plan['members'][0]
                    self.assertEqual(member['status'], 'refused', plan)
                    self.assertIsNone(member['statement'])
                    self.assertEqual(member['reason'],
                                     'DEFAULT values cannot be verified across servers; set explicit values.')
                    self.assertEqual(self.apply(plan)['status'], 'refused')
        self.assertEqual(self.written_ids(), [])

    def test_flagset_explicit_values_and_persist_only_defaults_can_be_planned(self):
        self.client(0).variables['optimizer_switch'] = 'index_merge=on,index_merge_union=on'
        for mode, value in (('global', 'index_merge=off'), ('persist', 'index_merge=off'),
                            ('persist_only', 'index_merge=default')):
            with self.subTest(mode=mode, value=value):
                plan = self.plan(name='optimizer_switch', value=value, mode=mode, member_ids=['member-1'])
                self.assertEqual(plan['members'][0]['status'], 'planned', plan)
                self.assertIsNotNone(plan['members'][0]['statement'])
        self.assertEqual(self.written_ids(), [])

    def test_mariadb_set_and_flagset_defaults_refuse_global_fallback(self):
        self.clients[self.profiles[0]['host']] = MemberClient(0, engine='mariadb')
        client = self.client(0)
        for name, kind, current, value in (
                ('optimizer_switch', 'FLAGSET', 'index_merge=on', 'index_merge=default'),
                ('sql_mode', 'SET', 'STRICT_TRANS_TABLES', 'STRICT_TRANS_TABLES,default')):
            client.catalog_type = kind
            client.variables[name] = current
            for mode in ('global', 'persist', 'persist_only'):
                with self.subTest(name=name, mode=mode):
                    plan = self.plan(name=name, value=value, mode=mode, member_ids=['member-1'])
                    self.assertEqual(plan['members'][0]['status'], 'refused', plan)
                    self.assertEqual(plan['members'][0]['reason'],
                                     'DEFAULT values cannot be verified across servers; set explicit values.')
                    self.assertIsNone(plan['members'][0]['statement'])
        self.assertEqual(self.written_ids(), [])

    def test_secret_variables_refused_before_password_lookup(self):
        from myflames.param_compare import sensitive
        for name in ('wsrep_sst_auth', 'report_password', 's3_secret_key'):
            with self.subTest(name=name), mock.patch.object(self.store, 'connection_with_saved_password',
                    wraps=self.store.connection_with_saved_password) as password:
                self.assertTrue(sensitive(name))
                with self.assertRaisesRegex(ValueError, '(?i)secret|sensitive|credential') as refused:
                    self.plan(name=name, value='never-store-this-secret')
                self.assertNotIn('never-store-this-secret', str(refused.exception))
                password.assert_not_called()
        self.assertEqual(self.opened, [])
        self.assertEqual(self.records(), [])
        self.assertFalse(topology_change._registry(self.store).plans)

    def test_source_width_value_refused_with_truncation_reason(self):
        for engine, cap in (('mysql', 1024), ('mariadb', 4096)):
            with self.subTest(engine=engine):
                self.clients[self.profiles[0]['host']] = MemberClient(0, engine)
                self.client(0).variables['max_connections'] = '1' * cap
                plan = self.plan(member_ids=['member-1'])
                member = plan['members'][0]
                self.assertEqual(member['status'], 'refused', plan)
                self.assertRegex(member['reason'].lower(), 'truncat|source.*(cap|limit)|length.*(cap|limit)')
                self.assertIsNone(member['statement'])
                self.assertEqual(self.apply(plan)['status'], 'refused')
                self.assertEqual(self.written_ids(), [])

    def test_mariadb_catalog_fallback_at_2048_characters_refuses_review(self):
        self.clients[self.profiles[0]['host']] = MemberClient(0, 'mariadb')
        self.client(0).catalog_type = 'VARCHAR'
        self.client(0).variables['init_connect'] = 'x' * 2048
        self.client(0).show_hidden_variables.add('init_connect')
        plan = self.plan(name='init_connect', value='SELECT 1', member_ids=['member-1'])
        self.assertEqual(plan['members'][0]['status'], 'refused', plan)
        self.assertIn('truncat', plan['members'][0]['reason'].lower())
        self.assertIsNone(plan['members'][0]['statement'])
        self.assertEqual(self.written_ids(), [])

    def test_mariadb_complete_show_value_at_2048_characters_can_be_reviewed(self):
        self.clients[self.profiles[0]['host']] = MemberClient(0, 'mariadb')
        self.client(0).catalog_type = 'VARCHAR'
        self.client(0).variables['init_connect'] = 'x' * 2048
        plan = self.plan(name='init_connect', value='SELECT 1', member_ids=['member-1'])
        self.assertEqual(plan['members'][0]['status'], 'planned', plan)
        self.assertEqual(plan['members'][0]['statement'], "SET GLOBAL init_connect = 'SELECT 1'")
        self.assertEqual(plan['members'][0]['after'], {'global': 'SELECT 1'})
        self.assertEqual(self.written_ids(), [])

    def test_plan_deadline_includes_saved_password_lookup(self):
        now = [1000.0]
        original = self.store.connection_with_saved_password

        def slow_password(connection, **kwargs):
            value = original(connection, **kwargs)
            now[0] += topology_change.topology_group.REQUEST_SECONDS
            return value

        with mock.patch.object(topology_change.time, 'monotonic', side_effect=lambda: now[0]), \
                mock.patch.object(self.store, 'connection_with_saved_password', side_effect=slow_password):
            plan = self.plan(member_ids=['member-1'])
        self.assertEqual(plan['members'][0]['status'], 'refused', plan)
        self.assertRegex(plan['members'][0]['reason'].lower(), 'time|budget|deadline')
        self.assertEqual(self.client(0).calls, [], 'No query may log in after the password lookup uses the budget.')
        self.assertEqual(self.written_ids(), [])

    def test_plan_requires_a_full_query_window_after_transport_setup(self):
        now = [1000.0]
        original_enter = MemberClient.__enter__

        def slow_transport(client):
            now[0] += topology_change.topology_group.REQUEST_SECONDS - topology_change.topology_group.QUERY_SECONDS + 0.1
            return original_enter(client)

        with mock.patch.object(topology_change.time, 'monotonic', side_effect=lambda: now[0]), \
                mock.patch.object(MemberClient, '__enter__', slow_transport):
            plan = self.plan(member_ids=['member-1'])
        self.assertEqual(plan['members'][0]['status'], 'refused', plan)
        self.assertRegex(plan['members'][0]['reason'].lower(), 'time|budget|deadline')
        self.assertEqual(self.client(0).calls, [])

    def test_validated_plan_consent_snapshot_cannot_be_replaced_before_planner(self):
        original = topology_change.plan
        for changed in ('endpoint', 'generation'):
            with self.subTest(changed=changed):
                self.connection = dict(self.request(self.store.get('profile', 'member-1')), group_name='G')

                def change_after_request_validation(*args, **kwargs):
                    if changed == 'endpoint':
                        self.change_profile(0, host='unreviewed.example')
                    else:
                        self.store.delete_connection_group('G')
                        self.store.save_connection_group('G', [p['id'] for p in self.profiles])
                    return original(*args, **kwargs)

                with mock.patch.object(topology_change, 'plan', side_effect=change_after_request_validation), \
                        mock.patch.object(self.store, 'connection_with_saved_password',
                                          wraps=self.store.connection_with_saved_password) as password:
                    with self.assertRaisesRegex(ValueError, '(?i)changed|connection|group'):
                        self.plan()
                    password.assert_not_called()
                self.change_profile(0, host=self.profiles[0]['host'])
        self.assertEqual(self.opened, [])

    def test_plan_consent_rechecked_after_password_lookup_before_login(self):
        original = self.store.connection_with_saved_password
        changed = [False]

        def recreate_group_after_password(connection, **kwargs):
            result = original(connection, **kwargs)
            if not changed[0]:
                changed[0] = True
                self.store.delete_connection_group('G')
                self.store.save_connection_group('G', [p['id'] for p in self.profiles])
            return result

        with mock.patch.object(self.store, 'connection_with_saved_password', side_effect=recreate_group_after_password):
            with self.assertRaisesRegex(ValueError, '(?i)changed|connection|group'):
                self.plan()
        self.assertTrue(changed[0])
        self.assertTrue(all(client.calls == [] for client in self.clients.values()))
        self.assertEqual(self.written_ids(), [])

    def test_effective_modes_and_scopes_for_mixed_engines(self):
        self.clients[self.profiles[0]['host']] = MemberClient(0, 'mariadb')
        for mode in ('global', 'persist', 'persist_only'):
            with self.subTest(mode=mode):
                plan = self.plan(mode=mode, value={'global': '301', 'persist': '302', 'persist_only': '303'}[mode])
                expected = {'member-1': ('global', {'global'}),
                            'member-2': (mode, {'global', 'persisted'} if mode == 'persist' else
                                         {'persisted'} if mode == 'persist_only' else {'global'}),
                            'member-3': (mode, {'global', 'persisted'} if mode == 'persist' else
                                         {'persisted'} if mode == 'persist_only' else {'global'})}
                result = self.apply(plan)
                self.assertEqual(result['status'], 'applied', result)
                for document in (plan, result):
                    for member in document['members']:
                        effective, scopes = expected[member['profile_id']]
                        self.assertEqual(member['effective_mode'], effective)
                        self.assertEqual(set(member['after']), scopes)
                        self.assertIsInstance(member['effect'], dict)
                        self.assertTrue(member['effect']['text'])
                for value, schema in ((plan, 'topology-change-plan-1.0.json'), (result, 'topology-change-1.0.json')):
                    validate(value, json.loads((ROOT / 'docs' / 'schemas' / schema).read_text(encoding='utf-8')))

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

    def test_mariadb_duplicates_require_uid_version_and_local_identity(self):
        for index in range(3):
            self.clients[self.profiles[index]['host']] = MemberClient(index, 'mariadb')
        original = self.client(0).variables
        for key in ('server_id', 'hostname', 'port'):
            self.client(1).variables[key] = original[key]
        self.client(2).variables['server_id'] = original['server_id']
        plan = self.plan()
        self.assertTrue(all(m['status'] == 'planned' for m in plan['members']), plan)
        # Containers can have the same server_id, hostname and port. Their
        # independent network identities still distinguish the two servers.
        self.client(1).variables['server_uid'] = original['server_uid']
        plan = self.plan()
        self.assertEqual([m['status'] for m in plan['members']], ['planned', 'refused', 'planned'], plan)
        self.assertEqual(plan['members'][1]['duplicate_of'], 'member-1')
        self.assertEqual(self.apply(plan)['status'], 'refused')
        self.assertEqual(self.written_ids(), [])
        self.client(1).variables['version'] = '11.4.8-MariaDB'
        plan = self.plan()
        self.assertTrue(all(m['status'] == 'planned' for m in plan['members']), plan)
        self.assertEqual(self.apply(plan)['status'], 'applied')

    def test_mariadb_uid_and_version_bound_in_review_and_write_packet(self):
        self.clients[self.profiles[0]['host']] = MemberClient(0, 'mariadb')
        plan = self.plan(member_ids=['member-1'])
        identity = plan['members'][0]['identity']
        self.assertEqual(identity['server_uid'], self.client(0).variables['server_uid'])
        self.assertEqual(identity['version'], self.client(0).variables['version'])
        self.assertEqual(self.apply(plan)['status'], 'applied')
        guard = guarded_packets(self.client(0).writes[0])[0][0]
        self.assertIn('@@global.server_uid', guard.lower())
        self.assertIn(identity['server_uid'].encode('utf-8').hex(), guard)
        self.assertIn('version', guard.lower())
        self.assertIn(identity['version'].encode('utf-8').hex(), guard)

    def test_mariadb_uid_or_version_change_after_review_refuses(self):
        for field, changed in (('server_uid', base64.b64encode(b'z' * 20).decode('ascii')),
                               ('version', '11.4.10-MariaDB')):
            with self.subTest(field=field):
                self.clients[self.profiles[0]['host']] = MemberClient(0, 'mariadb')
                plan = self.plan(member_ids=['member-1'])
                self.client(0).variables[field] = changed
                result = self.apply(plan)
                self.assertEqual(result['status'], 'refused', result)
                self.assertEqual(self.written_ids(), [])

    def test_mariadb_uid_change_at_write_login_is_refused_by_packet_guard(self):
        self.clients[self.profiles[0]['host']] = MemberClient(0, 'mariadb')
        plan = self.plan(member_ids=['member-1'])
        self.client(0).before_write = lambda sql: self.client(0).variables.update(
            server_uid=base64.b64encode(b'z' * 20).decode('ascii'))
        result = self.apply(plan)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(self.written_ids(), [])
        self.assertEqual(self.records()[0]['status'], 'refused')

    def test_mariadb_server_id_change_verifies_after_with_reviewed_new_identity(self):
        self.clients[self.profiles[0]['host']] = MemberClient(0, 'mariadb')
        plan = self.plan(name='server_id', value='11', member_ids=['member-1'])
        self.assertEqual(plan['members'][0]['status'], 'planned', plan)
        self.assertEqual(plan['members'][0]['identity']['server_id'], '1')
        result = self.apply(plan)
        self.assertEqual(result['status'], 'applied', result)
        self.assertEqual(result['members'][0]['status'], 'applied')
        self.assertEqual(result['members'][0]['after'], {'global': '11'})
        self.assertEqual(self.client(0).variables['server_id'], '11')
        self.assertEqual(len(self.client(0).writes), 1)
        guard, body, _line = guarded_packets(self.client(0).writes[0])[0]
        self.assertIn('@@global.server_id <> 1', guard)
        self.assertNotIn('@@global.server_id <> 11', guard)
        self.assertIn('SET GLOBAL server_id = 11', body)
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['status'], 'ok')
        self.assertEqual(records[0]['target']['identity']['server_id'], '1')
        self.assertTrue(any('@@global.server_id <> 1' in statement for statement in records[0]['statements']))

    def test_missing_or_unknown_mariadb_uid_refuses_review(self):
        for uid in (None, '', 'unknown', 'not-a-base64-sha1'):
            with self.subTest(uid=uid):
                self.clients[self.profiles[0]['host']] = MemberClient(0, 'mariadb')
                if uid is None:
                    self.client(0).variables.pop('server_uid')
                else:
                    self.client(0).variables['server_uid'] = uid
                plan = self.plan(member_ids=['member-1'])
                self.assertEqual(plan['members'][0]['status'], 'refused', plan)
                self.assertIsNone(plan['members'][0]['statement'])
                self.assertEqual(self.written_ids(), [])

    def test_mixed_gr_and_chained_async_constraints_share_one_order(self):
        for index, role in ((0, 'primary'), (1, 'secondary')):
            self.client(index).role = role
            self.client(index).variables.update(group_replication_group_name=UUIDS[6],
                                               group_replication_single_primary_mode='ON')
        profile = self.save('member-4', 'G', type='development')
        self.profiles.append(profile)
        self.clients[profile['host']] = MemberClient(3)
        for index, source in ((2, 0), (3, 2)):
            self.client(index).channels = [dict(Channel_Name='', Source_Host=self.profiles[source]['host'],
                Source_Port='3306', Source_UUID=UUIDS[source], Source_Server_Id=str(source + 1),
                Replica_IO_Running='Yes', Replica_SQL_Running='Yes', Seconds_Behind_Source='0')]
        self.client(0).replicas = [['3', self.profiles[2]['host'], '3306']]
        self.client(2).replicas = [['4', self.profiles[3]['host'], '3306']]
        plan = self.plan()
        order = [member['profile_id'] for member in plan['members']]
        self.assertLess(order.index('member-4'), order.index('member-3'))
        self.assertLess(order.index('member-3'), order.index('member-1'))
        self.assertLess(order.index('member-2'), order.index('member-1'))
        self.assertIn('replica', plan['order_rule'].lower())
        self.assertIn('primary', plan['order_rule'].lower())
        self.assertEqual(self.apply(plan)['status'], 'applied')

    def test_conflicting_gr_and_async_constraints_refuse_review(self):
        self.make_gr()
        self.client(0).channels = [dict(Channel_Name='external', Source_Host=self.profiles[1]['host'],
            Source_Port='3306', Source_UUID=UUIDS[1], Source_Server_Id='2',
            Replica_IO_Running='Yes', Replica_SQL_Running='Yes', Seconds_Behind_Source='0')]
        with self.assertRaisesRegex(ValueError, '(?i)order|cycle|conflict'):
            self.plan()
        self.assertEqual(self.written_ids(), [])

    def test_mariadb_server_id_order_evidence_is_explicit(self):
        for index in range(3):
            self.clients[self.profiles[index]['host']] = MemberClient(index, 'mariadb')
        self.client(1).channels = [dict(Connection_name='', Master_Host='reported-source-alias',
            Master_Port='3306', Master_Server_Id='1', Slave_IO_Running='Yes',
            Slave_SQL_Running='Yes', Seconds_Behind_Master='0')]
        self.client(0).replicas = [['2', self.profiles[1]['host'], '3306']]
        plan = self.plan(member_ids=['member-1', 'member-2'])
        self.assertEqual([m['profile_id'] for m in plan['members']], ['member-2', 'member-1'])
        self.assertIn('MariaDB sources matched by server ID', plan['order_rule'])
        self.assertEqual(self.apply(plan)['status'], 'applied')

    def test_galera_duplicate_identity_includes_node_uuid_and_instance(self):
        for index, node in enumerate(('alpha', 'beta', 'gamma')):
            self.clients[self.profiles[index]['host']] = MemberClient(index, 'mariadb', node_name=node)
        self.client(1).status['wsrep_gcomm_uuid'] = self.client(0).status['wsrep_gcomm_uuid'].upper()
        plan = self.plan()
        self.assertTrue(all(m['status'] == 'planned' for m in plan['members']), plan)
        for key in ('server_uid', 'server_id', 'hostname', 'port', 'version'):
            self.client(1).variables[key] = self.client(0).variables[key]
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
        result = self.apply(plan)
        self.assertEqual(result['status'], 'stale', result)
        self.assertEqual(sum(len(client.writes) for client in self.clients.values()), written)

    def test_refusal_before_first_send_still_uses_review(self):
        plan = self.plan()
        self.client(0).variables['max_connections'] = '200'
        result = self.apply(plan)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(self.written_ids(), [])
        self.assertEqual(result['members'][0]['message'],
                         'The variable changed after the review. Nothing was sent to this member.')
        record = next(row for row in self.records() if row['id'] == result['audit_ids'][0])
        self.assertEqual(record['outcome'], 'changed_variable')
        registry = topology_change._registry(self.store)
        self.assertNotIn(plan['plan_id'], registry.plans)
        self.assertNotIn(plan['plan_id'], registry.tokens.entries)
        self.client(0).variables['max_connections'] = '151'
        self.assertEqual(self.apply(plan)['status'], 'stale')
        self.assertEqual(self.written_ids(), [])

    def test_concurrent_apply_cannot_start_second_run_for_one_review(self):
        plan = self.plan()
        checking, release, finished = threading.Event(), threading.Event(), threading.Event()
        answers, errors = {}, []

        def hold_first_read(sql):
            if not checking.is_set():
                checking.set()
                if not release.wait(5):
                    raise AssertionError('Concurrent apply did not release its first read.')

        def applying(label, progress_id):
            try:
                answers[label] = self.apply(plan, progress_id=progress_id)
            except BaseException as exc:
                errors.append(exc)
            finally:
                if label == 'second':
                    finished.set()

        self.client(0).before_read = hold_first_read
        first = threading.Thread(target=applying, args=('first', 'concurrent-first-progress'))
        second = threading.Thread(target=applying, args=('second', 'concurrent-second-progress'))
        first.start()
        try:
            self.assertTrue(checking.wait(5), 'The first apply did not reach its member read.')
            second.start()
            self.assertTrue(finished.wait(5), 'The replay waited for a member write instead of refusing.')
        finally:
            release.set()
            first.join(5)
            if second.ident is not None:
                second.join(5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(answers['second']['status'], 'stale', answers)
        self.assertEqual(answers['first']['status'], 'applied', answers)
        self.assertTrue(all(len(client.writes) == 1 for client in self.clients.values()))

    def test_policy_changed_outcome_survives_preflight_refusal(self):
        plan = self.plan()
        self.change_profile(2, type='production')
        opened = len(self.opened)
        result = self.apply(plan)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(len(self.opened), opened)
        self.assertEqual({row['outcome'] for row in self.records()}, {'policy_changed'})
        self.assertEqual(self.written_ids(), [])

    def test_progress_is_shared_before_any_topology_endpoint(self):
        registry = ui._action_progress(self.workspace)
        self.assertIs(registry, topology_change._registry(self.store).progress)
        progress_id = 'ordinary-before-topology-01'
        ordinary = registry.start(progress_id, 1)
        self.assertIsNotNone(ordinary)
        plan = topology_change.plan(self.store, 'G', 'member-1', 'max_connections', '300', 'global', ['member-1'])
        audit = dbops.AuditTrail(self.store, topology_change.OPERATION, 'ui')
        result = topology_change.apply(self.store, audit, plan['plan_id'], plan['expect'], True, 'G', progress_id)
        self.assertEqual(result['status'], 'refused', result)
        self.assertIs(topology_change.share_progress(self.store, registry), registry)
        self.assertEqual(self.written_ids(), [])

    def test_64_early_failing_actions_do_not_block_rolling_start(self):
        for index in range(64):
            progress_id = 'early-failing-action-%04d' % index
            with self.assertRaises(ValueError):
                ui.api_response('/api/live/proxysql/action', {
                    'connection': {'profile_id': 42},
                    'plan': {'action': 'load', 'statements': ['LOAD MYSQL SERVERS TO RUNTIME'], 'expect': {}},
                    'progress_id': progress_id, 'confirmed': True}, self.workspace)
            snapshot = ui.api_response(PROGRESS_PATH, {'progress_id': progress_id}, self.workspace)
            self.assertEqual(snapshot['phase'], 'finished')
            self.assertEqual(snapshot['states'], ['not_sent'])
        plan = self.plan(member_ids=['member-1'])
        self.assertEqual(self.apply(plan)['status'], 'applied')
        self.assertEqual(self.written_ids(), ['member-1'])

    def test_expired_received_entries_pruned_without_evicting_active_roll(self):
        registry = ui._action_progress(self.workspace)
        registry.LIMIT = 2
        abandoned = registry.start('abandoned-ordinary-action', 1)
        active = registry.start_member('active-received-rolling', 1)
        owner = topology_change._registry(self.store)
        owner.runs['active-received-rolling'] = topology_change._Run(
            {'group_name': 'G', 'expect': {'group_generation': 'test-generation'}}, active)
        abandoned.touched -= registry.TTL + 1
        active.touched -= registry.TTL + 1
        self.assertIsNotNone(registry.start('replacement-ordinary-action', 1))
        self.assertEqual(registry.get('abandoned-ordinary-action')['phase'], 'unknown')
        self.assertEqual(registry.get('active-received-rolling')['phase'], 'received')
        self.assertIsNone(registry.start('no-room-ordinary-action', 1))

    def test_action_owned_progress_id_refuses_roll_without_consuming_review(self):
        plan = self.plan()
        progress_id = 'action-owned-progress-0001'
        ordinary = ui._action_progress(self.workspace).start(progress_id, 2)
        ordinary.set('checking', ['waiting', 'waiting'])
        opened = len(self.opened)
        result = self.apply(plan, progress_id=progress_id)
        self.assertIn(result['status'], ('stale', 'refused'), result)
        self.assertEqual(len(self.opened), opened)
        self.assertEqual(self.written_ids(), [])
        snapshot = ui.api_response(PROGRESS_PATH, {'progress_id': progress_id}, self.workspace)
        self.assertEqual(snapshot['states'], ['waiting', 'waiting'])
        self.assertEqual(self.apply(plan)['status'], 'applied')

    def test_progress_is_published_atomically_with_stop_handle(self):
        plan = self.plan()
        progress_id = 'atomic-published-progress-01'
        registry = topology_change._registry(self.store)
        started, observed = threading.Event(), threading.Event()
        answers, failures, observers = [], [], []
        original = registry.progress.start_member

        def observe_then_stop():
            started.set()
            try:
                answers.append(ui.api_response(PROGRESS_PATH, {'progress_id': progress_id}, self.workspace))
                answers.append(ui.api_response(STOP_PATH,
                    dict(connection=self.connection, progress_id=progress_id), self.workspace))
            except BaseException as exc:
                failures.append(exc)
            finally:
                observed.set()

        def publish(progress_id, count, timeout=None):
            progress = original(progress_id, count, timeout)
            observer = threading.Thread(target=observe_then_stop)
            observers.append(observer)
            observer.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(observed.wait(0.1), 'Progress escaped before its Stop handle was published.')
            return progress

        try:
            with mock.patch.object(registry.progress, 'start_member', side_effect=publish):
                self.apply(plan, progress_id=progress_id)
        finally:
            for observer in observers:
                observer.join(5)
        self.assertTrue(all(not observer.is_alive() for observer in observers))
        self.assertEqual(failures, [])
        self.assertTrue(observed.is_set())
        self.assertNotEqual(answers[0]['phase'], 'unknown')
        self.assertIn(answers[1]['status'], ('stopping', 'finished'))

    def test_rolling_progress_id_cannot_be_reused_by_ordinary_action(self):
        plan = self.plan()
        progress_id = 'rolling-owned-progress-01'
        observations = []

        def try_action_reuse(sql):
            observations.append(ui._action_progress(self.workspace).start(progress_id, 8))
            observations.append(ui.api_response(PROGRESS_PATH, {'progress_id': progress_id}, self.workspace))

        self.client(0).before_write = try_action_reuse
        self.assertEqual(self.apply(plan, progress_id=progress_id)['status'], 'applied')
        self.assertIsNone(observations[0])
        self.assertEqual(observations[1]['profile_id'], 'member-1')
        self.assertEqual(observations[1]['states'], ['sent', 'waiting', 'waiting'])

    def test_active_progress_and_stop_handle_survive_finished_run_eviction(self):
        plan = self.plan(member_ids=['member-1'])
        progress_id = 'active-rolling-progress-0001'
        registry = topology_change._registry(self.store)
        registry.progress.LIMIT = 2
        observed = []

        def fill_finished_runs(sql):
            for value in ('301', '302', '303'):
                nested = self.plan(value=value, member_ids=['member-2'])
                self.assertEqual(self.apply(nested)['status'], 'applied')
            observed.append(ui.api_response(PROGRESS_PATH, {'progress_id': progress_id}, self.workspace))
            observed.append(ui.api_response(STOP_PATH,
                dict(connection=self.connection, progress_id=progress_id), self.workspace))

        self.client(0).before_write = fill_finished_runs
        self.apply(plan, progress_id=progress_id)
        self.assertEqual(observed[0]['phase'], 'sending')
        self.assertEqual(observed[0]['profile_id'], 'member-1')
        self.assertEqual(observed[1]['status'], 'stopping')
        self.assertEqual(len(self.client(0).writes), 1)

    def test_run_owned_progress_id_cannot_be_reused_when_snapshot_is_missing(self):
        plan = self.plan(member_ids=['member-1'])
        progress_id = 'retained-run-progress-0001'
        registry = topology_change._registry(self.store)
        observed = []

        def try_reuse_while_active(sql):
            with registry.progress._lock:
                registry.progress._items.pop(progress_id, None)
            another = self.plan(value='301', member_ids=['member-2'])
            observed.append(another)
            observed.append(self.apply(another, progress_id=progress_id))

        self.client(0).before_write = try_reuse_while_active
        self.assertEqual(self.apply(plan, progress_id=progress_id)['status'], 'applied')
        self.assertIn(observed[1]['status'], ('stale', 'refused'), observed)
        self.assertEqual(self.client(1).writes, [])
        self.assertEqual(self.apply(observed[0])['status'], 'applied')

    def test_stop_from_another_current_member_of_same_group(self):
        plan = self.plan()
        progress_id = 'refreshed-group-progress-01'
        refreshed = dict(self.request(self.profiles[2]), group_name='G')
        answers = []

        def stop_from_refreshed_connection(sql):
            answers.append(ui.api_response(STOP_PATH,
                dict(connection=refreshed, progress_id=progress_id), self.workspace))

        self.client(0).before_write = stop_from_refreshed_connection
        result = self.apply(plan, progress_id=progress_id)
        self.assertEqual(answers[0]['status'], 'stopping')
        self.assertEqual(result['status'], 'stopped', result)
        self.assertEqual(self.written_ids(), ['member-1'])

    def test_pre_send_recheck_timeout_is_one_unsent_refusal(self):
        plan = self.plan()
        progress_id = 'recheck-timeout-progress-01'

        def timeout_after_pending(sql):
            if any(record['status'] == 'pending' for record in self.records()):
                raise connector.ConnectorError('The query timed out after 10 seconds.')

        self.client(0).before_read = timeout_after_pending
        result = self.apply(plan, progress_id=progress_id)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(result['members'][0]['status'], 'refused')
        self.assertEqual(self.written_ids(), [])
        records = self.records()
        self.assertEqual(len(records), 3)
        self.assertTrue(all(record['status'] == 'refused' for record in records), records)
        self.assertNotIn('may have run', json.dumps([result, records]))
        self.assertEqual(ui.api_response(PROGRESS_PATH, {'progress_id': progress_id}, self.workspace)['states'],
                         ['not_sent', 'not_sent', 'not_sent'])

    def test_dependency_login_refusal_at_send_boundary_is_one_unsent_record(self):
        self.make_async()
        plan = self.plan(member_ids=['member-3'])

        def refuse_dependency(**options):
            if options['host'] == self.profiles[1]['host'] and any(
                    record['status'] == 'pending' for record in self.records()):
                raise connector.ConnectorError('ERROR 1045 (28000): synthetic login denied')
            return self.factory(**options)

        with mock.patch('myflames.connector.MySQLConnection', side_effect=refuse_dependency):
            result = self.apply(plan)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(result['members'][0]['status'], 'refused')
        self.assertEqual(self.written_ids(), [])
        records = self.records()
        self.assertEqual(len(records), 1, records)
        self.assertEqual(records[0]['status'], 'refused')
        self.assertNotIn('may have run', json.dumps([result, records]))

    def test_identity_packet_refusal_is_unsent_in_progress_and_audit(self):
        plan = self.plan(member_ids=['member-1'])
        self.client(0).before_write = lambda sql: self.client(0).variables.update(server_uuid=UUIDS[4])
        progress_id = 'identity-refusal-progress-01'
        result = self.apply(plan, progress_id=progress_id)
        message = 'The server identity changed. Nothing was sent to this member.'
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(result['members'][0]['message'], message)
        self.assertEqual(self.records()[0]['message'], message)
        self.assertEqual(self.records()[0]['outcome'], 'identity_mismatch')
        self.assertEqual(ui.api_response(PROGRESS_PATH, {'progress_id': progress_id}, self.workspace)['states'],
                         ['not_sent'])
        self.assertEqual(self.written_ids(), [])

    def test_identity_change_during_final_variable_read_is_unsent_identity_refusal(self):
        plan = self.plan(member_ids=['member-1'])
        changed = []

        def switch_after_role_checks(sql):
            if (not changed and '/*myflames:variable*/' in sql and
                    any(row['status'] == 'pending' for row in self.records())):
                self.client(0).variables['server_uuid'] = UUIDS[4]
                changed.append(True)

        self.client(0).before_read = switch_after_role_checks
        result = self.apply(plan)
        self.assertEqual(changed, [True])
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(result['members'][0]['message'],
                         'The server identity changed. Nothing was sent to this member.')
        self.assertEqual(self.records()[0]['outcome'], 'identity_mismatch')
        self.assertEqual(self.written_ids(), [])

    def test_pre_send_server_read_error_is_not_a_write_error(self):
        plan = self.plan(member_ids=['member-1'])

        def denied_after_pending(sql):
            if any(row['status'] == 'pending' for row in self.records()):
                raise connector.ConnectorError('ERROR 1142: synthetic read denied')

        self.client(0).before_read = denied_after_pending
        result = self.apply(plan)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(result['members'][0]['message'],
                         'The member could not be rechecked. Nothing was sent to this member.')
        self.assertNotIn('server returned error', json.dumps([result, self.records()]))
        self.assertEqual(self.written_ids(), [])

    def test_server_write_rejections_keep_error_code_and_failed_progress(self):
        for code in (1227, 1238, 1231):
            with self.subTest(code=code):
                plan = self.plan(member_ids=['member-1'])
                self.client(0).write_error = 'ERROR %d (42000) at line 2: synthetic write denied' % code
                progress_id = 'server-write-error-%04d' % code
                result = self.apply(plan, progress_id=progress_id)
                message = 'The server returned error %d. No later member was sent.' % code
                self.assertEqual(result['members'][0]['message'], message)
                record = next(row for row in self.records() if row['id'] == result['audit_ids'][0])
                self.assertEqual(record['message'], message)
                self.assertEqual(record['status'], 'error')
                self.assertEqual(ui.api_response(PROGRESS_PATH, {'progress_id': progress_id}, self.workspace)['states'],
                                 ['failed'])

    def test_write_login_failure_is_refused_without_unknown_wording(self):
        for code in (2002, 2003, 1045):
            with self.subTest(code=code):
                plan = self.plan(member_ids=['member-1'])

                def fail_login(sql):
                    raise connector.ConnectorError('ERROR %d: synthetic login failure' % code)

                self.client(0).before_write = fail_login
                before = {record['id'] for record in self.records()}
                result = self.apply(plan)
                self.assertEqual(result['status'], 'refused', result)
                self.assertEqual(result['members'][0]['status'], 'refused')
                records = [record for record in self.records() if record['id'] not in before]
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]['status'], 'refused', records)
                self.assertNotIn('may have run', json.dumps([result, records]))
                self.assertNotIn('server returned error', json.dumps([result, records]))
                self.assertEqual(self.written_ids(), [])

    def test_lost_connection_after_send_says_statement_may_have_run(self):
        for error in ('ERROR 2013 (HY000): Lost connection during query',
                      'The query timed out after 30 seconds.'):
            with self.subTest(error=error):
                plan = self.plan(member_ids=['member-1'])
                self.client(0).write_error = error
                before = {record['id'] for record in self.records()}
                result = self.apply(plan)
                self.assertEqual(result['status'], 'partial', result)
                self.assertEqual(result['members'][0]['status'], 'unknown', result)
                self.assertIn('may have run', result['members'][0]['message'])
                self.assertNotIn('server returned error', result['members'][0]['message'])
                records = [record for record in self.records() if record['id'] not in before]
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]['status'], 'unknown')
                self.assertIn('may have run', records[0]['message'])

    def test_parameter_audit_history_finds_rolling_record_by_name(self):
        plan = self.plan(mode='persist', member_ids=['member-1'])
        result = self.apply(plan)
        self.assertEqual(result['status'], 'applied')
        server = '%s:%s' % (self.profiles[0]['host'], self.profiles[0]['port'])
        records = dbops.audit_object_report(self.store, server, 'parameter', 'max_connections')['records']
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['id'], result['audit_ids'][0])
        self.assertEqual(records[0]['operation'], 'topology.variable')
        self.assertEqual(records[0]['target']['name'], 'max_connections')
        self.assertEqual(records[0]['target']['mode'], 'persist')
        self.assertEqual(dbops.audit_object_report(self.store, server, 'parameter', 'max_connect_errors')['records'], [])

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

    def test_saved_endpoint_drift_reports_target_not_variable_change(self):
        plan = self.plan(member_ids=['member-2'])
        self.change_profile(1, host='unreviewed.example')
        result = self.apply(plan)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(result['members'][0]['message'],
                         'The saved member, group or endpoint changed. Review again.')
        self.assertEqual(self.records()[0]['outcome'], 'changed_target')
        self.assertEqual(self.written_ids(), [])

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

    def test_async_removed_selected_relay_refuses_before_first_leaf(self):
        self.make_async_with_two_chains()
        plan = self.plan(member_ids=['member-3', 'member-6', 'member-2', 'member-5'])
        self.assertEqual([m['profile_id'] for m in plan['members']],
                         ['member-3', 'member-6', 'member-2', 'member-5'])
        self.store.assign_profile_group(['member-2'], '')
        result = self.apply(plan)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(self.written_ids(), [])
        self.assertEqual(self.statuses(result), {'member-3': 'refused', 'member-6': 'not_sent',
                                               'member-2': 'not_sent', 'member-5': 'not_sent'})
        self.assertEqual(result['members'][0]['message'],
                         'A server the rolling order depends on changed. Review again.')

    def test_later_selected_dependency_endpoint_change_refuses_before_first_leaf(self):
        self.make_async_with_two_chains()
        plan = self.plan(member_ids=['member-3', 'member-6', 'member-2', 'member-5'])
        self.change_profile(1, host='unreviewed.example')
        result = self.apply(plan)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(self.written_ids(), [])
        self.assertEqual(result['members'][0]['message'],
                         'A server the rolling order depends on changed. Review again.')
        self.assertFalse(any(options['host'] == 'unreviewed.example' for options in self.opened))

    def test_later_selected_dependency_unreadable_facts_refuse_before_first_leaf(self):
        self.make_async_with_two_chains()
        plan = self.plan(member_ids=['member-3', 'member-6', 'member-2', 'member-5'])
        self.client(1).read_error = 'ERROR 1142: synthetic role permission lost'
        result = self.apply(plan)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(self.written_ids(), [])
        self.assertEqual(result['members'][0]['message'],
                         'A server the rolling order depends on changed. Review again.')

    def test_final_scope_recheck_catches_drift_during_dependency_reads(self):
        self.make_async()
        current = self.client(2)
        for mode, scope in (('global', 'global'), ('persist', 'global'),
                            ('persist', 'persisted'), ('persist_only', 'persisted')):
            with self.subTest(mode=mode, scope=scope):
                current.variables['max_connections'] = '151'
                current.persisted.clear()
                plan = self.plan(mode=mode, member_ids=['member-3'])
                mutated = []

                def drift_after_initial_variable_read(sql):
                    if not mutated and any(row['status'] == 'pending' for row in self.records()):
                        # The audit becomes pending after apply_change's first
                        # variable read, before its final dependency scan.
                        target = current.persisted if scope == 'persisted' else current.variables
                        target['max_connections'] = '300'
                        mutated.append(True)

                self.client(1).before_read = drift_after_initial_variable_read
                progress_id = 'final-recheck-%s-%s' % (mode, scope)
                result = self.apply(plan, progress_id=progress_id)
                self.assertEqual(mutated, [True])
                self.assertEqual(result['status'], 'refused', result)
                self.assertEqual(result['members'][0]['message'],
                                 'The variable changed after the review. Nothing was sent to this member.')
                record = next(row for row in self.records() if row['id'] == result['audit_ids'][0])
                self.assertEqual((record['status'], record['outcome']), ('refused', 'changed_variable'))
                self.assertEqual(ui.api_response(PROGRESS_PATH, {'progress_id': progress_id}, self.workspace)['states'],
                                 ['not_sent'])
                self.assertEqual(self.written_ids(), [])
                self.client(1).before_read = None

    def test_unchanged_member_drift_reports_variable_not_saved_connection(self):
        plan = self.plan(value='151', member_ids=['member-1'])
        self.assertEqual(plan['members'][0]['status'], 'unchanged')
        self.client(0).variables['max_connections'] = '200'
        result = self.apply(plan)
        self.assertEqual(result['members'][0]['message'],
                         'The variable changed after the review. Nothing was sent to this member.')
        record = next(row for row in self.records() if row['id'] == result['audit_ids'][0])
        self.assertEqual(record['outcome'], 'changed_variable')
        self.assertEqual(self.written_ids(), [])

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
        self.assertEqual(result['members'][0]['message'],
                         'The variable changed after the review. Nothing was sent to this member.')
        record = next(row for row in self.records() if row['id'] == result['audit_ids'][0])
        self.assertEqual(record['outcome'], 'changed_variable')

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
