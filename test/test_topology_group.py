"""Whole-group reads preserve stored consent and compare credential-free facts.

The fixtures are synthetic. Saved profiles, generation checks and dbops stay
real; only the MySQL client process boundary is replaced in consent tests.
"""
import ast
import copy
import contextlib
import io
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import types
import unittest
from unittest import mock

from myflames import account_compare, connection_groups, connector, dbops, group_members, param_compare, topology_group, ui
from myflames.accounts_api import _group_login_guard
from myflames.connection_groups import ProfileReadError
from test_account_compare import CaptureConn, account, side
from test_chart_data import validate
from test_connection_groups import GroupCase, _FakeMemberConn
from test_param_compare import item, snapshot


UUID_A = 'aaaaaaaa-0000-4000-8000-000000000001'
UUID_B = 'bbbbbbbb-0000-4000-8000-000000000002'
UUID_C = 'cccccccc-0000-4000-8000-000000000003'
UUID_D = 'dddddddd-0000-4000-8000-000000000004'
ROOT = Path(__file__).resolve().parent.parent


# The synthetic client answers one normal connector invocation. The real
# connector retains defaults files, consent checks, pipe drainage and timeout
# cleanup; it opens one client process per query on the same connection object.
CLIENT = r'''
import json
import os
import re
import sys
import time
from xml.sax.saxutils import escape, quoteattr

cfg = json.loads(os.environ['TOPOLOGY_FIXTURE'])
log = os.environ['TOPOLOGY_SQL_LOG']
sql = sys.argv[sys.argv.index('-e') + 1]
first = not os.path.exists(log)
with open(log, 'a', encoding='utf-8') as handle:
    handle.write(json.dumps(sql) + '\n')
with open(log + '.pids', 'a', encoding='utf-8') as handle:
    handle.write(str(os.getpid()) + '\n')
if first or cfg.get('pause_each'):
    time.sleep(cfg.get('pause_each', cfg.get('pause', 0)))
if cfg.get('error'):
    sys.stderr.write(cfg['error'] + '\n')
    sys.exit(1)

def emit(columns, rows):
    if '--xml' not in sys.argv:
        def cell(value):
            return 'NULL' if value is None else str(value).replace('\\', '\\\\').replace('\0', '\\0').replace('\t', '\\t').replace('\n', '\\n')
        sys.stdout.write(''.join('\t'.join(cell(value) for value in row) + '\n' for row in rows))
        return
    out = ['<resultset statement=' + quoteattr(sql) +
           ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">']
    for row in rows:
        out.append('<row>')
        for name, value in zip(columns, row):
            if value is None:
                out.append('<field name=' + quoteattr(name) + ' xsi:nil="true"/>')
            else:
                out.append('<field name=' + quoteattr(name) + '>' + escape(str(value)) + '</field>')
        out.append('</row>')
    out.append('</resultset>\n')
    sys.stdout.write(''.join(out))

variables = cfg['variables']
evidence = sql.startswith('DELIMITER ')
if evidence:
    header, packet = sql.split('\n', 1)
    delimiter = header[len('DELIMITER '):]
    assert packet.endswith(delimiter + '\n') and packet.count(delimiter) == 1
    marker = re.search(r"SELECT '(myflames_evidence_[0-9a-f]{32})';\n", packet)
    assert marker is not None
    sql = packet[marker.end():-len(delimiter) - 1].strip()
    identity = dict(variables)
    if cfg.get('evidence_variables') and (not cfg.get('switch_on') or cfg['switch_on'] in sql):
        identity.update(cfg.get('evidence_variables', {}))
    if cfg.get('switch_evidence_body') and cfg.get('evidence_variables') and (
            not cfg.get('switch_on') or cfg['switch_on'] in sql):
        variables = dict(variables, **cfg.get('evidence_variables', {}))
    names = ('server_uuid', 'server_id', 'hostname', 'port', 'version', 'version_comment',
             'wsrep_on', 'wsrep_provider', 'wsrep_node_name')
    emit(['Variable_name', 'Value'], [[name, identity[name]] for name in names if name in identity])
    # Earlier synthetic Galera fixtures omit the node UUID. Supply a stable,
    # explicitly synthetic value for the new identity prefix only.
    wsrep = cfg.get('status', {}).get('wsrep_gcomm_uuid', 'aaaaaaaa-0000-4000-8000-000000000001')
    emit(['Variable_name', 'Value'], [['wsrep_gcomm_uuid', wsrep]])
    emit(['marker'], [[marker.group(1)]])

def fail(message):
    if evidence:
        message = re.sub(r'(ERROR\s+\d+(?:\s+\([^)]*\))?)(?: at line \d+)?:', r'\1 at line 2:', message)
    sys.stderr.write(message + '\n')
    sys.exit(1)

def run_one(part_sql):
    text = re.sub(r'^/\*myflames:[^*]*\*/\s*', '', part_sql.strip()).rstrip(';')
    lower = text.lower()
    if cfg.get('pause_on') and cfg['pause_on'].lower() in lower:
        time.sleep(cfg.get('pause_on_seconds', 0.5))
    if cfg.get('error_on') and cfg['error_on'].lower() == lower:
        fail(cfg.get('query_error', 'ERROR 1045 (28000): Refused login secret_payload'))
    if lower == 'select @@version_comment':
        comment = 'Readyset' if cfg.get('probe_readyset') and not part_sql.lstrip().startswith('/*') else variables['version_comment']
        emit(['@@version_comment'], [[comment]])
    elif lower == 'select @@version':
        emit(['@@version'], [[variables['version']]])
    elif lower == 'select @@server_uuid':
        if cfg.get('proxied_uuid_error'):
            fail(cfg['proxied_uuid_error'])
        if 'proxied_server_uuid' in cfg:
            val = cfg['proxied_server_uuid']
            emit(['@@server_uuid'], [[val]] if val is not None else [])
        elif cfg.get('proxy') == 'readyset':
            emit(['@@server_uuid'], [])
        else:
            val = variables.get('server_uuid')
            emit(['@@server_uuid'], [[val]] if val is not None else [])
    elif lower == 'select @@hostname, @@port':
        if cfg.get('proxied_host_port_error'):
            fail(cfg['proxied_host_port_error'])
        if 'proxied_hostname' in cfg or 'proxied_port' in cfg:
            h, p = cfg.get('proxied_hostname'), cfg.get('proxied_port')
            emit(['@@hostname', '@@port'], [[h, p]] if (h is not None or p is not None) else [])
        elif cfg.get('proxy') == 'readyset':
            emit(['@@hostname', '@@port'], [])
        else:
            emit(['@@hostname', '@@port'], [[variables.get('hostname'), variables.get('port')]])
    elif lower.startswith('select @@global.'):
        name = lower[len('select @@global.'):]
        if cfg.get('exact_denied'):
            fail('ERROR 1227 (42000): Access denied for exact variable read')
        value = cfg.get('exact_variables', {}).get(name, variables.get(name))
        emit(['@@GLOBAL.' + name], [[value]])
    elif "variable_name = 'admin-version'" in lower:
        rows = [[variables['version']]] if cfg.get('proxy') == 'proxysql' else []
        emit(['variable_value'], rows)
    elif lower == 'show readyset version':
        emit(['name', 'value'], [['release version', 'stable-test']])
    elif lower == 'show readyset status':
        rows = [['Status', 'Online'], ['CDC Server', cfg.get('cdc_server')]]
        if 'replication_status' in cfg:
            if cfg['replication_status'] is not None:
                rows.append(['Replication Status', cfg['replication_status']])
        else:
            rows.append(['Replication Status', 'Running'])
        emit(['name', 'value'], rows)
    elif 'from replication_status limit 0' in lower:
        if cfg.get('readyset_adapter'):
            fail("ERROR 1146 (42S02): Table 'replication_status' doesn't exist")
        emit([], [])
    elif 'from readyset.upstream_servers' in lower or 'from upstream_servers' in lower:
        rows = cfg.get('upstreams', [])
        columns = list(rows[0]) if rows else []
        emit(columns, [[row.get(name) for name in columns] for row in rows])
    elif lower == 'show caches':
        emit(['query_id', 'query'], [['q_secret', 'SELECT secret_sql FROM secret_table']])
    elif 'from runtime_mysql_servers' in lower or 'from mysql_servers' in lower:
        rows = cfg.get('servers', [])
        columns = list(rows[0]) if rows else []
        emit(columns, [[row.get(name) for name in columns] for row in rows])
    elif 'from stats_mysql_connection_pool' in lower:
        rows = cfg.get('pool', [])
        columns = ['hostgroup', 'srv_host', 'srv_port', 'status']
        emit(columns, [[row.get(name) for name in columns] for row in rows])
    elif any('from ' + table in lower for table in (
            'runtime_mysql_group_replication_hostgroups', 'runtime_mysql_galera_hostgroups')):
        table = re.search(r'from\s+(runtime_mysql_\w+_hostgroups)', lower).group(1)
        rows = cfg.get(table, [])
        if re.search(r'\bwhere\s+active\s*=\s*1', lower):
            rows = [row for row in rows if str(row.get('active', 1)) == '1']
        columns = ['offline_hostgroup']
        emit(columns, [[row.get(name) for name in columns] for row in rows])
    elif 'from runtime_mysql_query_rules' in lower or 'from mysql_query_rules' in lower:
        emit(['rule_id', 'comment'], [['1', 'secret_rule_sql']])
    elif lower.startswith('show global variables'):
        names = re.findall(r"'([^']+)'", text)
        if 'report_host' in names and 'probe_uuid' in cfg:
            local_vars = dict(variables, server_uuid=cfg['probe_uuid'])
        else:
            local_vars = variables
        rows = [[name, local_vars[name]] for name in names if name in local_vars]
        emit(['Variable_name', 'Value'], rows)
    elif lower.startswith('set session print_identified_with_as_hex'):
        return
    elif lower.startswith("select '") and text.endswith("'") and text.count("'") == 2:
        emit(['m'], [[text[8:-1]]])
    elif 'from mysql.user order by user, host' in lower:
        accts = cfg.get('accounts', [])
        include_tls = 'ssl_type' in lower
        inline_granted = 'from mysql.role_edges' in lower
        rows = []
        for a in accts:
            row = [a['name'], a['host'], 'caching_sha2_password', 'N', 'N', None, None, '0', '0']
            if include_tls:
                row.extend(['', '', '', ''])
            if inline_granted:
                row.append('0')
            rows.append(row)
        emit(['User'] * (len(rows[0]) if rows else 0), rows)
    elif lower.startswith('show grants for '):
        target = text[len('SHOW GRANTS FOR '):].strip()
        for a in cfg.get('accounts', []):
            if ('`%s`@`%s`' % (a['name'], a['host'])) == target:
                if a.get('grants_error'):
                    fail(a['grants_error'])
                emit(['Grants'], [[g] for g in a.get('grants', [])])
                return
        fail('ERROR 1141 (42000): There is no such grant defined for user')
    elif lower.startswith('show create user '):
        target = text[len('SHOW CREATE USER '):].strip()
        for a in cfg.get('accounts', []):
            if ('`%s`@`%s`' % (a['name'], a['host'])) == target:
                emit(['CREATE USER'], [[a['create']]])
                return
        fail('ERROR 1396 (HY000): Operation SHOW CREATE USER failed')
    elif 'from performance_schema.replication_group_members' in lower:
        if cfg.get('no_gr_role') and 'member_role' in lower:
            fail('ERROR 1054 (42S22): Unknown column MEMBER_ROLE')
        role = cfg.get('role')
        state = cfg.get('state', 'ONLINE')
        present = role or cfg.get('has_gr')
        if lower.startswith('select member_id'):
            rows = cfg.get('gr_members', [])
            columns = ['MEMBER_ID', 'MEMBER_HOST', 'MEMBER_PORT', 'MEMBER_STATE']
            if 'member_role' in lower:
                columns.append('MEMBER_ROLE')
            emit(columns, [row[:len(columns)] for row in rows])
        elif lower.startswith('select member_state'):
            emit(['MEMBER_STATE'], [[state]] if present else [])
        else:
            emit(['MEMBER_ROLE', 'MEMBER_STATE'], [[role, state]] if present else [])
    elif 'from performance_schema.replication_group_member_stats' in lower:
        emit(['COUNT_TRANSACTIONS_REMOTE_IN_APPLIER_QUEUE'], [[cfg.get('queue', 0)]])
    elif 'performance_schema.user_variables_by_thread' in lower:
        if cfg.get('dump_threads_error'):
            fail(cfg['dump_threads_error'])
        sessions = cfg.get('dump_threads')
        if sessions is None:
            sessions = cfg.get('readyset_replicas', [])
        out_rows = []
        for idx, row in enumerate(sessions):
            pid = row.get('pid', idx + 1)
            attrs = row.get('attrs') or []
            if not attrs:
                out_rows.append([pid, row.get('uuid'), row.get('host'), row.get('user'), None, None])
            else:
                for attr_name, attr_val in attrs:
                    out_rows.append([pid, row.get('uuid'), row.get('host'), row.get('user'), attr_name, attr_val])
        emit(['PROCESSLIST_ID', 'replica_uuid', 'PROCESSLIST_HOST', 'PROCESSLIST_USER', 'ATTR_NAME', 'ATTR_VALUE'],
             out_rows)
    elif 'from performance_schema.threads' in lower and 'performance_schema.session_connect_attrs' in lower:
        if cfg.get('readyset_clients_error'):
            fail(cfg['readyset_clients_error'])
        rows = cfg.get('readyset_clients', [])
        emit(['PROCESSLIST_HOST', 'PROCESSLIST_USER'], [[row.get('host'), row.get('user')] for row in rows])
    elif 'from performance_schema.session_connect_attrs' in lower:
        if cfg.get('attrs_error'):
            fail(cfg['attrs_error'])
        routed = cfg.get('routed')
        count = routed if isinstance(routed, int) and not isinstance(routed, bool) else (1 if routed else 0)
        emit(['COUNT(*)'], [[count]])
    elif lower.startswith('show global status'):
        if 'performance_schema_session_connect_attrs_lost' in lower and cfg.get('attrs_lost_error'):
            fail(cfg['attrs_lost_error'])
        values = dict(cfg.get('status', {}))
        names = re.findall(r"'([^']+)'", text)
        if ('Performance_schema_session_connect_attrs_lost' in names
                and 'Performance_schema_session_connect_attrs_lost' not in values
                and not cfg.get('attrs_lost_missing')
                and 'mariadb' not in str(variables.get('version', '')).lower()):
            values['Performance_schema_session_connect_attrs_lost'] = '0'
        emit(['Variable_name', 'Value'], [[name, values[name]] for name in names if name in values])
    elif lower in ('show replica status', 'show slave status', 'show all slaves status'):
        channels = cfg.get('channels', [])
        columns = list(channels[0]) if channels else []
        emit(columns, [[row.get(name) for name in columns] for row in channels])
    elif lower in ('show replicas', 'show slave hosts'):
        rows = cfg.get('replicas', [])
        if rows and isinstance(rows[0], dict):
            columns = list(rows[0])
            emit(columns, [[row.get(name) for name in columns] for row in rows])
        else:
            emit(['Server_id', 'Host', 'Port'], rows)
    elif 'from mysql.wsrep_cluster_members' in lower:
        emit(['node_uuid', 'node_name', 'node_incoming_address'], cfg.get('wsrep_members', []))
    elif 'from performance_schema.pxc_cluster_view' in lower:
        emit(['HOST_NAME', 'UUID', 'STATUS', 'LOCAL_INDEX', 'SEGMENT'], cfg.get('pxc_members', []))
    elif 'from information_schema.schemata' in lower:
        emit(['SCHEMA_NAME'], [['mysql_innodb_cluster_metadata']] if cfg.get('innodb_cluster') else [])
    elif 'from mysql_innodb_cluster_metadata' in lower:
        rows = cfg.get('cluster_metadata', [])
        columns = list(rows[0]) if rows else []
        emit(columns, [[row.get(name) for name in columns] for row in rows])
    elif 'from information_schema.plugins' in lower:
        emit(['PLUGIN_NAME', 'PLUGIN_STATUS', 'PLUGIN_TYPE', 'PLUGIN_VERSION', 'PLUGIN_LIBRARY', 'LOAD_OPTION'],
             cfg.get('plugins', [['auth', 'ACTIVE', 'AUTHENTICATION', '1.0', None, 'ON']]))
    elif 'from mysql.component' in lower:
        if cfg.get('component_denied'):
            fail('ERROR 1142 (42000): SELECT command denied for user secret_component_user on mysql.component')
        else:
            emit(['component_urn'], [[value] for value in cfg.get('components', [])])
    elif lower == 'select 1':
        emit(['1'], [['1']])
    else:
        fail('ERROR 1064 (42000): unexpected synthetic read')

for part in sql.split('\n;\n'):
    if part.strip():
        run_one(part)
'''


def member_fixture(uuid=UUID_A, engine='mysql', role=None, gtid=None, **fields):
    variables = {'version': '11.4.9-MariaDB' if engine == 'mariadb' else '8.4.11',
                 'version_comment': 'MariaDB Server' if engine == 'mariadb' else 'MySQL Community Server (GPL)',
                 'server_id': '1', 'hostname': 'own-member', 'port': '3306', 'read_only': 'OFF',
                 'gtid_current_pos' if engine == 'mariadb' else 'gtid_executed': gtid or ''}
    if engine == 'mysql':
        variables.update(server_uuid=uuid, super_read_only='OFF', gtid_mode='ON',
                         performance_schema='ON',
                         performance_schema_session_connect_attrs_size='512')
        if role:
            variables.update(group_replication_group_name=UUID_D, group_replication_single_primary_mode='ON')
    result = dict(variables=variables, role=role)
    result.update(fields)
    return result


class GTIDTests(unittest.TestCase):
    def test_empty_sets_and_interval_union(self):
        self.assertEqual(topology_group.parse_mysql_gtids(''), {})
        self.assertEqual(topology_group.count_mysql_gtids({}), 0)
        parsed = topology_group.parse_mysql_gtids(UUID_A + ':1-3:3-5:9:8-10')
        self.assertEqual(topology_group.count_mysql_gtids(parsed), 8)
        self.assertEqual(parsed[(UUID_A, '')], [(1, 5), (8, 10)])

    def test_subtract_splits_intervals_and_preserves_other_sources(self):
        source = topology_group.parse_mysql_gtids(UUID_A + ':1-10,' + UUID_B + ':5-7')
        member = topology_group.parse_mysql_gtids(UUID_A + ':2-4:6:9-15')
        difference = topology_group.subtract_mysql_gtids(source, member)
        self.assertEqual(difference[(UUID_A, '')], [(1, 1), (5, 5), (7, 8)])
        self.assertEqual(difference[(UUID_B, '')], [(5, 7)])
        self.assertEqual(topology_group.count_mysql_gtids(difference), 7)
        self.assertEqual(topology_group.subtract_mysql_gtids(source, source), {})
        self.assertEqual(topology_group.subtract_mysql_gtids({}, member), {})

    def test_tagged_sets_share_uuid_but_not_transaction_stream(self):
        # MySQL serializes several tag blocks under one UUID. Tag case is
        # normalized by the server, and untagged transactions stay separate.
        source = topology_group.parse_mysql_gtids(UUID_A.upper() + ':1-4:Tag_A:1-5:TagB:3-7')
        member = topology_group.parse_mysql_gtids(UUID_A + ':1-4:tag_a:1-2:tagb:5-7')
        self.assertEqual(set(source), {(UUID_A, ''), (UUID_A, 'tag_a'), (UUID_A, 'tagb')})
        diff = topology_group.subtract_mysql_gtids(source, member)
        self.assertEqual(diff[(UUID_A, 'tag_a')], [(3, 5)])
        self.assertEqual(diff[(UUID_A, 'tagb')], [(3, 4)])
        self.assertEqual(topology_group.count_mysql_gtids(diff), 5)

    def test_repeated_tag_streams_merge_and_large_ranges_are_not_expanded(self):
        parsed = topology_group.parse_mysql_gtids(UUID_A + ':blue:1-5,' + UUID_A + ':BLUE:4-10')
        self.assertEqual(parsed[(UUID_A, 'blue')], [(1, 10)])
        large = topology_group.parse_mysql_gtids(UUID_A + ':1-9000000000000')
        self.assertEqual(topology_group.count_mysql_gtids(large), 9000000000000)
        self.assertEqual(len(large[(UUID_A, '')]), 1)

    def test_malformed_gtids_are_unavailable_not_silently_zero(self):
        invalid = ('not-a-uuid:1', UUID_A + ':0', UUID_A + ':4-2', UUID_A + ':tag!',
                   UUID_A + ':tag:1-no', UUID_A + ':' + 'x' * 33 + ':1')
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ValueError):
                topology_group.parse_mysql_gtids(text)

    def test_mariadb_positions_keep_domain_sequence_and_origin(self):
        parsed = topology_group.parse_mariadb_gtids('0-12-100,7-45-30')
        self.assertEqual(parsed[0], {'server_id': 12, 'sequence': 100})
        self.assertEqual(parsed[7], {'server_id': 45, 'sequence': 30})
        self.assertEqual(topology_group.parse_mariadb_gtids(''), {})
        self.assertEqual(topology_group.mariadb_behind('0-12-100,7-45-30', '0-99-95,7-45-40'),
                         {'0': 5, '7': 0})
        self.assertEqual(topology_group.mariadb_behind('0-12-100,7-45-30', '0-12-100'),
                         {'0': 0, '7': 30})
        self.assertEqual(topology_group.mariadb_behind('', ''), {})

    def test_invalid_mariadb_position_refuses(self):
        for text in ('0-1', 'x-1-2', '0-1--2', '0-1-2,bad'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                topology_group.parse_mariadb_gtids(text)


class ParameterManyTests(unittest.TestCase):
    def test_differences_first_missing_values_and_identity_fold(self):
        a = snapshot([item('z_diff', '1', apply_type='dynamic', dynamic=True),
                      item('same', '001'), item('one_only', '4'), item('server_id', '1'),
                      item('group_replication_local_address', 'one:33061', 'string')])
        b = snapshot([item('z_diff', '2', apply_type='dynamic', dynamic=True),
                      item('same', '1'), item('server_id', '2'),
                      item('group_replication_local_address', 'two:33061', 'string')])
        before = copy.deepcopy((a, b))
        rows = param_compare.compare_many({'one': a, 'two': b})
        by_name = {row['name']: row for row in rows}
        self.assertEqual(by_name['z_diff']['status'], 'different')
        self.assertEqual(by_name['same']['status'], 'same')
        self.assertEqual(by_name['one_only']['status'], 'missing')
        self.assertEqual(by_name['one_only']['values'], {'one': '4', 'two': None})
        self.assertEqual(by_name['server_id']['status'], 'per_server')
        self.assertEqual(by_name['group_replication_local_address']['status'], 'per_server')
        self.assertEqual(by_name['z_diff']['apply_type'], 'dynamic')
        self.assertIs(by_name['z_diff']['dynamic'], True)
        first_same = min(i for i, r in enumerate(rows) if r['status'] in ('same', 'per_server'))
        self.assertTrue(all(r['status'] in ('different', 'missing') for r in rows[:first_same]))
        self.assertEqual((a, b), before, 'the pure comparison does not mutate captures')

    def test_galera_member_addresses_fold(self):
        names = ('wsrep_node_incoming_address', 'wsrep_sst_receive_address')
        a = snapshot([item(name, 'a:4444', 'string') for name in names], engine='mariadb')
        b = snapshot([item(name, 'b:4444', 'string') for name in names], engine='mariadb')
        self.assertTrue(all(r['status'] == 'per_server' for r in param_compare.compare_many({'a': a, 'b': b})))

    def test_auto_increment_folds_only_with_live_cluster_mode(self):
        for engine, mode, enabled, disabled in (
                ('mysql', 'group_replication_single_primary_mode', 'OFF', 'ON'),
                ('mariadb', 'wsrep_auto_increment_control', 'ON', 'OFF')):
            with self.subTest(engine=engine):
                a = snapshot([item(mode, enabled, 'boolean'), item('auto_increment_offset', '1'),
                              item('auto_increment_increment', '3')], engine=engine)
                b = snapshot([item(mode, enabled, 'boolean'), item('auto_increment_offset', '2'),
                              item('auto_increment_increment', '4')], engine=engine)
                if engine == 'mysql':
                    for value in (a, b):
                        value['items'].append(item('group_replication_group_name', UUID_A, 'string'))
                else:
                    for value in (a, b):
                        value['items'].extend([item('wsrep_on', 'ON', 'boolean'),
                                               item('wsrep_provider', '/lib/libgalera_smm.so', 'string')])
                active = dict.fromkeys(('a', 'b'), 'group_replication' if engine == 'mysql' else 'galera')
                rows = {r['name']: r for r in param_compare.compare_many({'a': a, 'b': b}, active_clusters=active)}
                self.assertEqual(rows['auto_increment_offset']['status'], 'per_server')
                self.assertEqual(rows['auto_increment_increment']['status'], 'different')
                a['items'][0]['value'] = b['items'][0]['value'] = disabled
                rows = {r['name']: r for r in param_compare.compare_many({'a': a, 'b': b}, active_clusters=active)}
                for name in ('auto_increment_offset', 'auto_increment_increment'):
                    self.assertEqual(rows[name]['status'], 'different')

    def test_secret_values_never_enter_many_comparison(self):
        a = snapshot([item('wsrep_sst_auth', 'root:private-a', 'string')])
        b = snapshot([item('wsrep_sst_auth', 'root:private-b', 'string')])
        rows = param_compare.compare_many({'a': a, 'b': b})
        encoded = json.dumps(rows)
        self.assertNotIn('private-a', encoded)
        self.assertNotIn('private-b', encoded)
        self.assertNotEqual(rows[0]['status'], 'same', 'redacted credentials cannot establish equality')
        self.assertEqual(param_compare.compare_many({}), [])

    def test_cluster_node_settings_fold_but_shared_settings_do_not(self):
        names = ('group_replication_local_address',
                 'group_replication_advertise_recovery_endpoints',
                 'group_replication_recovery_public_key_path',
                 'group_replication_recovery_ssl_ca', 'group_replication_recovery_ssl_capath',
                 'group_replication_recovery_ssl_cert', 'group_replication_recovery_ssl_key',
                 'group_replication_recovery_ssl_crl', 'group_replication_recovery_ssl_crlpath',
                 'wsrep_node_incoming_address', 'wsrep_sst_receive_address', 'wsrep_sst_donor',
                 'wsrep_start_position', 'wsrep_status_file', 'wsrep_provider_base_host',
                 'wsrep_provider_base_port', 'wsrep_provider_base_dir', 'wsrep_provider_gcache_dir',
                 'wsrep_provider_gmcast_listen_addr', 'wsrep_provider_ist_recv_addr')
        a = snapshot([item(name, '/node-a/value', 'string') for name in names] +
                     [item('group_replication_group_name', UUID_A, 'string'),
                      item('wsrep_cluster_name', 'cluster-a', 'string')])
        b = snapshot([item(name, '/node-b/value', 'string') for name in names] +
                     [item('group_replication_group_name', UUID_B, 'string'),
                      item('wsrep_cluster_name', 'cluster-b', 'string')])
        rows = {row['name']: row for row in param_compare.compare_many({'a': a, 'b': b})}
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(rows[name]['status'], 'per_server')
        self.assertEqual(rows['group_replication_group_name']['status'], 'different')
        self.assertEqual(rows['wsrep_cluster_name']['status'], 'different')

    def test_wsrep_provider_options_compare_shared_keys_without_order_or_node_keys(self):
        left = ('base_host = node-a; base_port = 4567; base_dir = /a; gcache.dir = /a/cache; '
                'gmcast.listen_addr = tcp://node-a:4567; ist.recv_addr = node-a:4568; '
                'gcache.size = 128M; evs.keepalive_period = PT1S;')
        right = ('evs.keepalive_period=PT1S; gcache.size=128M; ist.recv_addr=node-b:4568; '
                 'gmcast.listen_addr=tcp://node-b:4567; gcache.dir=/b/cache; base_dir=/b; '
                 'base_port=4568; base_host=node-b;')
        a = snapshot([item('wsrep_provider_options', left, 'string')], engine='mariadb')
        b = snapshot([item('wsrep_provider_options', right, 'string')], engine='mariadb')
        row = param_compare.compare_many({'a': a, 'b': b})[0]
        self.assertEqual(row['status'], 'same')
        self.assertEqual(row['values'], {'a': left, 'b': right})
        b['items'][0]['value'] = right.replace('gcache.size=128M', 'gcache.size=256M')
        self.assertEqual(param_compare.compare_many({'a': a, 'b': b})[0]['status'], 'different')

    def test_malformed_wsrep_provider_options_are_missing_even_when_text_matches(self):
        for value in ('gcache.size', '=128M;', 'gcache.size=128M; malformed',
                      'gcache.size=128M;gcache.size=256M;'):
            with self.subTest(value=value):
                side = snapshot([item('wsrep_provider_options', value, 'string')], engine='mariadb')
                row = param_compare.compare_many({'a': side, 'b': copy.deepcopy(side)})[0]
                self.assertEqual(row['status'], 'missing')
                self.assertTrue(row.get('reason'))


class AccountManyTests(unittest.TestCase):
    def test_incomplete_account_labels_are_bounded_before_redaction(self):
        from myflames import accounts
        limit = accounts.MAX_ACCOUNT_TEXT_CHARS
        hostile = 'PASSWORD' + ' ' * (1024 * 1024) + 'x'
        for name, host in ((hostile, '%'), ('app', hostile),
                           ('a' * (limit // 2), 'h' * (limit // 2))):
            with self.subTest(name_chars=len(name), host_chars=len(host)):
                captured = side([account('readable'), account(name, host=host, grants_checked=False)])
                results = [({'profile_id': 'member', 'outcome': 'read'}, captured, None, None, False)]
                with mock.patch.object(dbops, 'strip_credentials', wraps=dbops.strip_credentials) as strip, \
                        mock.patch.object(dbops, 'redact', wraps=dbops.redact) as redact:
                    snapshots, excluded = topology_group._comparison_inputs(results, 'accounts')
                self.assertEqual([item['name'] for item in snapshots['member']['items']], ['readable'])
                self.assertEqual(len(excluded), 1)
                self.assertIn(accounts.OMITTED_ACCOUNT_TEXT, excluded[0]['reason'])
                self.assertNotIn(hostile, json.dumps(excluded))
                strip.assert_not_called()
                self.assertTrue(redact.called)
                self.assertTrue(all(len(call[0][0]) <= limit for call in redact.call_args_list))

    def test_incomplete_account_label_at_cap_is_preserved(self):
        from myflames import accounts
        limit = accounts.MAX_ACCOUNT_TEXT_CHARS
        name, host = 'a' * (limit // 2), 'h' * (limit // 2 - 1)
        label = name + '@' + host
        self.assertEqual(len(label), limit)
        captured = side([account(name, host=host, grants_checked=False)])
        results = [({'profile_id': 'member', 'outcome': 'read'}, captured, None, None, False)]
        with mock.patch.object(dbops, 'strip_credentials', wraps=dbops.strip_credentials) as strip, \
                mock.patch.object(dbops, 'redact', wraps=dbops.redact) as redact:
            snapshots, excluded = topology_group._comparison_inputs(results, 'accounts')
        self.assertEqual(snapshots['member']['items'], [])
        self.assertIn(label, excluded[0]['reason'])
        self.assertNotIn(accounts.OMITTED_ACCOUNT_TEXT, excluded[0]['reason'])
        strip.assert_called_once_with(label)
        redact.assert_called_once_with(label)

    def test_real_account_capture_reuses_existing_member_evidence_scope(self):
        for engine in ('mysql', 'mariadb'):
            with self.subTest(engine=engine):
                connection = CaptureConn(engine)
                evidence = dbops.TargetEvidenceConnection(param_compare.BoundedConnection(connection))
                captured = account_compare.capture(evidence)
                evidence.check()
                self.assertTrue(captured['complete'])
                self.assertTrue(captured['items'])
                for item in captured['items']:
                    self.assertFalse(item.get('error'), item)
                    for field in ('attributes_checked', 'defaults_checked', 'tls_checked'):
                        self.assertTrue(item[field], (field, item))
                rows = account_compare.compare_many({'a': captured, 'b': copy.deepcopy(captured)})
                self.assertTrue(rows)
                self.assertTrue(all(row['status'] == 'same' for row in rows))

    def test_disappeared_account_keeps_evidence_valid_and_remains_unreadable(self):
        evidence = dbops.TargetEvidenceConnection(param_compare.BoundedConnection(CaptureConn(exists=False)))
        captured = account_compare.capture(evidence)
        evidence.check()
        self.assertTrue(captured['complete'])
        self.assertIn('disappeared', captured['items'][0]['error'])
        self.assertTrue(account_compare.incomplete_account(captured['items'][0]))

    def test_incomplete_inventory_and_account_fields_still_refuse_direct_comparison(self):
        for complete in (False, None, 1):
            with self.subTest(complete=complete), self.assertRaises(ValueError):
                account_compare.compare_many({'a': side([account()], complete=complete)})
        for fields in ({'error': 'unavailable'}, {'exists': False},
                       {'attributes_checked': False}, {'defaults_checked': False},
                       {'tls_checked': False}, {'grants_checked': False}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                account_compare.compare_many({'a': side([account(**fields)])})

    def test_mariadb_default_role_difference_is_counted_once_as_roles(self):
        a = account(grants=['GRANT SELECT ON *.* TO `app`@`%`',
                            'SET DEFAULT ROLE `reader` FOR `app`@`%`'],
                    default_roles=[{'name': 'reader', 'host': ''}])
        b = account(grants=['GRANT SELECT ON *.* TO `app`@`%`',
                            'SET DEFAULT ROLE NONE FOR `app`@`%`'])
        row = account_compare.compare_many({'a': side([a], 'mariadb'), 'b': side([b], 'mariadb')})[0]
        self.assertEqual(row['status'], 'different')
        self.assertEqual([difference['kind'] for difference in row['differences']], ['roles'])

    def test_mariadb_default_role_grant_rendering_does_not_invent_privilege_drift(self):
        a = account(grants=['GRANT SELECT ON *.* TO `app`@`%`',
                            'SET DEFAULT ROLE `reader` FOR `app`@`%`'],
                    default_roles=[{'name': 'reader', 'host': ''}])
        b = copy.deepcopy(a)
        b['grants'][-1] = "set default role 'reader' for 'app'@'%'"
        self.assertEqual(account_compare.compare_many({'a': side([a], 'mariadb'),
                                         'b': side([b], 'mariadb')})[0]['status'], 'same')

    def test_same_missing_and_changed_privileges_roles_attributes(self):
        base = account('changed', grants=['GRANT SELECT ON *.* TO `changed`@`%`'])
        other = copy.deepcopy(base)
        other['grants'] = ['GRANT INSERT ON *.* TO `changed`@`%`']
        other['roles'] = [{'name': 'reader', 'host': '%', 'admin_option': False}]
        other['attributes']['locked'] = True
        a = side([base, account('same'), account('only')])
        b = side([other, account('same')])
        rows = {r['account']: r for r in account_compare.compare_many({'a': a, 'b': b})}
        self.assertEqual(rows['same@%']['status'], 'same')
        self.assertEqual(rows['only@%']['status'], 'missing')
        self.assertEqual(rows['only@%']['present_on'], ['a'])
        changed = rows['changed@%']
        self.assertEqual(changed['status'], 'different')
        self.assertEqual(set(changed['present_on']), {'a', 'b'})
        self.assertEqual({d['kind'] for d in changed['differences']}, {'privileges', 'roles', 'attributes'})
        self.assertTrue(all(d['profile_id'] in ('a', 'b') and d['detail'] for d in changed['differences']))

    def test_normalization_and_credential_projection(self):
        a = account(grants=['GRANT SELECT, INSERT ON `db`.* TO `app`@`%`'])
        b = account(grants=["grant INSERT,SELECT on 'db'.* to 'app'@'%' ;"])
        a['password'] = b['password'] = 'raw-password'
        a['authentication_string'] = b['authentication_string'] = 'hash-secret'
        a['attributes']['credential'] = b['attributes']['credential'] = 'attribute-secret'
        rows = account_compare.compare_many({'a': side([a]), 'b': side([b])})
        self.assertEqual(rows[0]['status'], 'same')
        encoded = json.dumps(rows)
        for secret in ('raw-password', 'hash-secret', 'attribute-secret', 'authentication_string'):
            self.assertNotIn(secret, encoded)
        self.assertEqual(account_compare.compare_many({}), [])


class PluginManyTests(unittest.TestCase):
    def test_plugin_status_version_missing_and_component_kind(self):
        a = [{'name': 'auth', 'kind': 'plugin', 'status': 'ACTIVE', 'version': '1.0'},
             {'name': 'same', 'kind': 'plugin', 'status': 'ACTIVE', 'version': '1.0'},
             {'name': 'file://component_validate_password', 'kind': 'component', 'status': 'ACTIVE', 'version': None}]
        b = [{'name': 'auth', 'kind': 'plugin', 'status': 'DISABLED', 'version': '1.1'},
             {'name': 'same', 'kind': 'plugin', 'status': 'ACTIVE', 'version': '1.0'}]
        rows = {r['name']: r for r in topology_group.compare_plugins({'a': a, 'b': b})}
        self.assertEqual(rows['auth']['status'], 'different')
        self.assertEqual(rows['same']['status'], 'same')
        component = rows['file://component_validate_password']
        self.assertEqual(component['kind'], 'component')
        self.assertEqual(component['status'], 'missing')
        self.assertIsNone(component['values']['b'])
        self.assertEqual(rows['auth']['values']['b'], {'status': 'DISABLED', 'version': '1.1'})
        self.assertEqual(topology_group.compare_plugins({}), [])


class MemberLoginTests(GroupCase):
    def setUp(self):
        super(MemberLoginTests, self).setUp()
        self.active = self.save('active', 'G', type='development')
        self.member = self.save('member', 'G')
        self.route = dict(self.request(self.active), group_name='G')
        self.guard = _group_login_guard(self.store, self.route)

    @contextlib.contextmanager
    def client(self, error=None):
        def invoke(argv, **options):
            if error is not None:
                raise connector.ConnectorError(error)
            return types.SimpleNamespace(returncode=0, stdout=b'1\n', stderr=b'')
        with mock.patch.object(connector.MySQLConnection, '_resolve_binary', return_value='/nonexistent/mysql'), \
                mock.patch.object(connector, '_run_bounded', side_effect=invoke) as spawn:
            yield spawn

    def test_helper_fills_the_members_saved_password(self):
        self.member = self.store.save('profile', dict(self.member), 'member', password='member-only-password', remember=True)
        with self.client() as spawn:
            with group_members.open_group_member(self.store, 'G', self.member, self.guard) as opened:
                conn, options = opened
                self.assertEqual(options['password'], 'member-only-password')
                self.assertEqual(conn.run('SELECT 1'), '1\n')
        self.assertEqual(spawn.call_count, 1)

    def test_removed_member_after_password_lookup_never_logs_in(self):
        with self.client() as spawn:
            with group_members.open_group_member(self.store, 'G', self.member, self.guard) as opened:
                self.store.assign_profile_group(['member'], '')
                with self.assertRaises(ProfileReadError):
                    opened[0].run('SELECT 1')
        spawn.assert_not_called()

    def test_removed_active_member_revokes_peer_login(self):
        with self.client() as spawn:
            with group_members.open_group_member(self.store, 'G', self.member, self.guard) as opened:
                self.store.assign_profile_group(['active'], '')
                with self.assertRaises(ProfileReadError):
                    opened[0].run('SELECT 1')
        spawn.assert_not_called()

    def test_recreated_group_generation_refuses_old_consent(self):
        with self.client() as spawn:
            with group_members.open_group_member(self.store, 'G', self.member, self.guard) as opened:
                self.store.delete_connection_group('G')
                self.store.save_connection_group('G', ['active', 'member'])
                with self.assertRaises(ProfileReadError):
                    opened[0].run('SELECT 1')
        spawn.assert_not_called()

    def test_member_endpoint_change_refuses_old_consent(self):
        with self.client() as spawn:
            with group_members.open_group_member(self.store, 'G', self.member, self.guard) as opened:
                self.store.save('profile', dict(self.member, host='changed.example'), 'member',
                                password='test-password', remember=True)
                with self.assertRaises(ProfileReadError):
                    opened[0].run('SELECT 1')
        spawn.assert_not_called()

    def test_helper_redacts_saved_password_in_client_exception(self):
        with self.client(error='client failed with test-password'):
            with self.assertRaises(connector.ConnectorError) as error:
                with group_members.open_group_member(self.store, 'G', self.member, self.guard) as opened:
                    opened[0].run('SELECT 1')
        self.assertNotIn('test-password', str(error.exception))

    def test_helper_accepts_only_timeout_overrides(self):
        with self.client() as spawn:
            with group_members.open_group_member(self.store, 'G', self.member, self.guard,
                    {'query_timeout': 7, 'connect_timeout': 3}) as opened:
                conn, options = opened
                self.assertEqual(conn.query_timeout, 7)
                self.assertEqual(conn.connect_timeout, 3)
                self.assertEqual(options['host'], self.member['host'])
                conn.run('SELECT 1')
        self.assertEqual(spawn.call_count, 1)
        for override in ({'host': 'outside.example'}, {'user': 'other'}, {'password': 'other'},
                         {'database': 'other'}, {'_login_guard': None},
                         {'query_timeout': 1, 'ssl_mode': 'DISABLED'}):
            with self.subTest(keys=list(override)), self.client() as spawn:
                with self.assertRaises(ValueError):
                    with group_members.open_group_member(self.store, 'G', self.member, self.guard, override):
                        self.fail('unsafe overrides must be rejected')
                spawn.assert_not_called()

    def test_connect_uses_shared_helper_without_changing_discovery(self):
        _FakeMemberConn.reset(uuids={'active.example': UUID_A})
        with mock.patch.object(connector, 'MySQLConnection', _FakeMemberConn), \
                mock.patch.object(group_members, 'open_group_member', wraps=group_members.open_group_member) as helper:
            result = ui.api_response('/api/connect', {'group_name': 'G', 'active_member_profile_id': 'active'},
                                     types.SimpleNamespace(store=self.store))
        self.assertEqual(result['version'], '8.0.36')
        self.assertEqual(result['schemas'], ['app'])
        self.assertEqual(result['group_connection']['active_member_profile_id'], 'active')
        self.assertEqual(helper.call_count, 1)
        self.assertEqual(_FakeMemberConn.logins, ['active.example'])

    def test_connect_member_failure_keeps_siblings_and_logs_only_unexpected_errors(self):
        self.save('healthy', 'G')
        secret = "synthetic-'member\\key"
        self.member = self.store.save('profile', dict(self.member), 'member',
                                      password=secret, remember=True)
        original = ui._member_live_facts
        for error_type in (KeyError, connector.ConnectorError):
            with self.subTest(error=error_type.__name__):
                _FakeMemberConn.reset(uuids={'active.example': UUID_A, 'healthy.example': UUID_C})

                def facts(conn):
                    if conn.host == 'member.example':
                        raise error_type('synthetic member failure ' + secret)
                    return original(conn)

                stderr = io.StringIO()
                with mock.patch.object(connector, 'MySQLConnection', _FakeMemberConn), \
                        mock.patch.object(ui, '_member_live_facts', side_effect=facts), \
                        mock.patch.object(ui, '_log_unexpected_error', wraps=ui._log_unexpected_error) as logger, \
                        contextlib.redirect_stderr(stderr):
                    result = ui.api_response('/api/connect', {
                        'group_name': 'G', 'active_member_profile_id': 'active', 'probe_members': True},
                        types.SimpleNamespace(store=self.store))
                members = {row['profile_id']: row for row in result['group_connection']['members']}
                self.assertTrue(members['active']['reachable'])
                self.assertTrue(members['healthy']['reachable'])
                self.assertEqual(members['healthy']['server_uuid'], UUID_C)
                self.assertFalse(members['member']['reachable'])
                self.assertIsNone(members['member']['server_uuid'])
                self.assertEqual(result['schemas'], ['app'])
                self.assertNotIn(secret, json.dumps(result))
                logged = stderr.getvalue()
                self.assertNotIn(secret, logged)
                self.assertNotIn(repr(secret)[1:-1], logged)
                self.assertNotIn(json.dumps(secret)[1:-1], logged)
                if error_type is KeyError:
                    self.assertEqual(logger.call_count, 1)
                    self.assertEqual(logged.count('myflames: unexpected error'), 1)
                    self.assertIn('KeyError', logged)
                    self.assertIn('[redacted]', logged)
                else:
                    logger.assert_not_called()
                    self.assertEqual(logged, '')
                self.assertEqual(_FakeMemberConn.active, 0)


class TopologyEndpointTests(GroupCase):
    def setUp(self):
        super(TopologyEndpointTests, self).setUp()
        self.profile = self.save('active', 'G', type='development')
        self.connection = dict(self.request(self.profile), group_name='G')
        self.workspace = types.SimpleNamespace(store=self.store, token='test-token')

    def post(self, payload):
        """Run authenticated POST dispatch without starting an HTTP service."""
        from email.message import Message
        handler = object.__new__(ui.WorkspaceHandler)
        handler.path, handler.server = '/api/live/topology', self.workspace
        handler.headers = Message()
        body = json.dumps(payload).encode('utf-8')
        handler.headers['Content-Type'] = 'application/json'
        handler.headers['Content-Length'] = str(len(body))
        handler.headers['X-Myflames-Token'] = self.workspace.token
        handler._local_request = lambda: True
        handler._authenticated = lambda: True
        handler._read_body = lambda size: body
        handler._send = mock.Mock()
        handler.do_POST()
        return handler._send.call_args[0][:2]

    def test_missing_group_and_nonmember_are_http_400_before_login(self):
        other = self.save('other', 'H')
        cases = [self.request(self.profile), dict(self.connection, group_name=''),
                 dict(self.request(other), group_name='G'),
                 dict(self.connection, profile_id='missing')]
        with mock.patch.object(connector, 'MySQLConnection') as connection:
            for value in cases:
                with self.subTest(connection=value):
                    status, result = self.post({'connection': value, 'section': 'members'})
                    self.assertEqual(status, 400, result)
                    if value.get('profile_id') in ('other', 'missing'):
                        self.assertIn('This saved connection is outside the current connection group.', result['error'])
        connection.assert_not_called()

    def test_section_is_a_strict_allowlist(self):
        with mock.patch.object(connector, 'MySQLConnection') as connection:
            for section in ('Map', 'Members', 'members ', 'bad', '', None, True, [], {}):
                with self.subTest(section=section):
                    status, result = self.post({'connection': self.connection, 'section': section})
                    self.assertEqual(status, 400, result)
        connection.assert_not_called()

    def test_request_endpoint_cannot_borrow_stored_member_consent(self):
        with mock.patch.object(connector, 'MySQLConnection') as connection:
            status, result = self.post({'connection': dict(self.connection, host='outside.example'),
                                        'section': 'members'})
        self.assertEqual(status, 400, result)
        connection.assert_not_called()

    def test_topology_is_explicit_only_and_never_a_synchronous_reader_or_write(self):
        self.assertNotIn('/api/live/topology', ui._SERVER_VIEW_PATHS)
        self.assertNotIn('/api/live/topology', ui._WRITE_PATHS)

    def test_validated_endpoint_and_generation_survive_dispatch_races(self):
        read = topology_group.read_group
        for change in ('endpoint', 'recreate'):
            with self.subTest(change=change):
                current = self.store.get('profile', 'active')
                route = dict(self.request(current), group_name='G')

                def race(*args, **kwargs):
                    self.assertEqual(kwargs['expected_current'], current)
                    self.assertEqual(kwargs['expected_generation'], current['group_generation'])
                    if change == 'endpoint':
                        self.store.save('profile', dict(current, host='changed.example'), 'active',
                                        password='test-password', remember=True)
                    else:
                        self.store.delete_connection_group('G')
                        self.store.save_connection_group('G', ['active'])
                    return read(*args, **kwargs)

                with mock.patch.object(topology_group, 'read_group', side_effect=race), \
                        mock.patch.object(self.store, 'connection_with_saved_password') as password, \
                        mock.patch.object(connector, '_run_bounded') as login:
                    status, result = self.post({'connection': route, 'section': 'members'})
                self.assertEqual(status, 400, result)
                password.assert_not_called()
                login.assert_not_called()

    def test_endpoint_uses_store_generation_never_request_generation(self):
        with mock.patch.object(topology_group, 'read_group', return_value={}) as read:
            status, result = self.post({'connection': dict(self.connection, group_generation='forged'),
                                        'section': 'members'})
        self.assertEqual(status, 200, result)
        self.assertEqual(read.call_args[1]['expected_generation'], self.profile['group_generation'])


class TopologyReadTests(GroupCase):
    def setUp(self):
        super(TopologyReadTests, self).setUp()
        self.active = self.save('active', 'G', type='development')
        self.connection = dict(self.request(self.active), group_name='G')
        self.workspace = types.SimpleNamespace(store=self.store, token='test-token')

    @contextlib.contextmanager
    def clients(self, fixtures, after_spawn=None):
        script = Path(self.temp.name) / 'topology-client.py'
        script.write_text(CLIENT, encoding='utf-8')
        logins, logs, peak, running, connections, timeouts = [], {}, [0], [0], [], []
        lock = threading.Lock()
        invoke_client = connector._run_bounded
        connection_class = connector.MySQLConnection
        prefix = str(time.monotonic_ns())

        def make_connection(**options):
            conn = connection_class(**options)
            with lock:
                connections.append(conn)
            return conn

        def invoke(argv, **kwargs):
            self.assertIn('-e', argv, 'every query uses the standard connector invocation')
            self.assertNotIn('--force', argv)
            self.assertIn('--local-infile=0', argv)
            defaults = next(value.split('=', 1)[1] for value in argv if value.startswith('--defaults-file='))
            content = Path(defaults).read_text(encoding='utf-8')
            host = re.search(r'^host=(.+)$', content, re.M).group(1).strip('"')
            self.assertIn('password=', content, 'the real saved-password fill supplies credentials')
            self.assertIn(host, fixtures, 'a request cannot add an unsaved group member')
            logfile = Path(self.temp.name) / ('%s-sql-%s.jsonl' % (prefix, host))
            env = dict(kwargs.get('env') or {})
            env.update(TOPOLOGY_FIXTURE=json.dumps(fixtures[host]), TOPOLOGY_SQL_LOG=str(logfile))
            with lock:
                logins.append(host)
                logs[host] = logfile
                timeouts.append(kwargs['timeout'])
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            try:
                if after_spawn is not None:
                    after_spawn(host)
                return invoke_client([sys.executable, str(script)] + argv[1:], **dict(kwargs, env=env))
            finally:
                with lock:
                    running[0] -= 1

        observed = types.SimpleNamespace(logins=logins, logs=logs, peak=peak,
                                         connections=connections, timeouts=timeouts)
        with mock.patch.object(connection_class, '_resolve_binary', return_value=str(script)), \
                mock.patch.object(connection_class, '_client_engine_facts', return_value=('mysql', 'mysql')), \
                mock.patch.object(connector, 'MySQLConnection', side_effect=make_connection), \
                mock.patch.object(connector, '_run_bounded', side_effect=invoke):
            yield observed
        self.assertEqual(running[0], 0)
        for logfile in logs.values():
            pidfile = Path(str(logfile) + '.pids')
            if pidfile.exists():
                for pid in pidfile.read_text(encoding='utf-8').splitlines():
                    with self.assertRaises(ProcessLookupError, msg='every client must already be reaped'):
                        os.kill(int(pid), 0)

    def read(self, section='members', active='active'):
        expected = self.store.get('profile', active)
        return topology_group.read_group(self.store, 'G', section, active,
                                         expected, expected.get('group_generation'))

    def statements(self, observed, host=None):
        paths = [observed.logs[host]] if host else observed.logs.values()
        return [json.loads(line) for path in paths if path.exists()
                for line in path.read_text(encoding='utf-8').splitlines()]

    def test_member_unexpected_error_keeps_siblings_and_logs_one_redacted_traceback(self):
        broken = self.save('broken', 'G')
        self.save('healthy', 'G')
        secret = "synthetic-'member\\key"
        self.store.save('profile', dict(broken), 'broken', password=secret, remember=True)
        fixtures = {'active.example': member_fixture(UUID_A),
                    'broken.example': member_fixture(UUID_B),
                    'healthy.example': member_fixture(UUID_C)}
        for error_type in (KeyError, connector.ConnectorError):
            with self.subTest(error=error_type.__name__):
                def fail_one(host):
                    if host == 'broken.example':
                        raise error_type('synthetic member failure ' + secret)

                stderr = io.StringIO()
                with self.clients(fixtures, after_spawn=fail_one), \
                        mock.patch.object(ui, '_log_unexpected_error', wraps=ui._log_unexpected_error) as logger, \
                        contextlib.redirect_stderr(stderr):
                    report = self.read()
                members = {row['profile_id']: row for row in report['members']}
                self.assertEqual(members['active']['outcome'], 'read')
                self.assertEqual(members['healthy']['outcome'], 'read')
                self.assertEqual(members['healthy']['server_uuid'], UUID_C)
                self.assertEqual(members['broken']['outcome'], 'failed')
                self.assertIsNone(members['broken']['server_uuid'])
                self.assertEqual(report['summary']['read'], 2)
                self.assertEqual(report['summary']['failed'], 1)
                self.assertIn('the connection could not be read.', members['broken']['message'])
                self.assertNotIn(secret, json.dumps(report))
                logged = stderr.getvalue()
                self.assertNotIn(secret, logged)
                self.assertNotIn(repr(secret)[1:-1], logged)
                self.assertNotIn(json.dumps(secret)[1:-1], logged)
                if error_type is KeyError:
                    self.assertEqual(logger.call_count, 1)
                    self.assertEqual(logged.count('myflames: unexpected error'), 1)
                    self.assertIn('KeyError', logged)
                    self.assertIn('[redacted]', logged)
                else:
                    logger.assert_not_called()
                    self.assertEqual(logged, '')

    def test_topology_has_no_custom_client_process_path(self):
        tree = ast.parse((ROOT / 'myflames/topology_group.py').read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertNotIn('subprocess', [name.name for name in node.names])
            elif isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, 'subprocess')
            elif isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, ('Popen', '_invocation_defaults', '_resolve_binary', '_check_login_consent'))
            elif isinstance(node, ast.ClassDef):
                self.assertNotEqual(node.name, 'ReadSession')

    def test_members_endpoint_schema_identity_gtid_and_single_connection(self):
        self.save('replica', 'G', type='production')
        self.save('outsider', 'elsewhere')
        fixtures = {'active.example': member_fixture(UUID_A, role='PRIMARY', gtid=UUID_A + ':1-10'),
                    'replica.example': member_fixture(UUID_B, role='SECONDARY', gtid=UUID_A + ':1-5', queue=2)}
        fixtures['replica.example']['variables'].update(hostname='own-replica', server_id='2', read_only='ON', super_read_only='ON')
        with self.clients(fixtures) as observed:
            status, report = TopologyEndpointTests.post(self, {'connection': self.connection, 'section': 'members'})
        self.assertEqual(status, 200, report)
        self.assertEqual(sorted(conn.host for conn in observed.connections), ['active.example', 'replica.example'])
        self.assertEqual(set(observed.logins), {'active.example', 'replica.example'})
        self.assertGreater(len(observed.logins), 2, 'queries invoke the standard client separately')
        self.assertEqual(report['schema_version'], 'topology-group-1.0')
        self.assertEqual(report['primary_profile_id'], 'active')
        self.assertEqual(report['effective_safety'], {'type': 'production', 'read_only': False})
        members = {m['profile_id']: m for m in report['members']}
        replica = members['replica']
        self.assertEqual(replica['hostname'], 'own-replica')
        self.assertEqual(replica['server_uuid'], UUID_B)
        self.assertEqual(replica['server_id'], 2)
        self.assertTrue(replica['read_only'])
        self.assertTrue(replica['super_read_only'])
        self.assertEqual(replica['lag']['kind'], 'gr_applier_queue')
        self.assertEqual(replica['lag']['value'], 2)
        self.assertEqual(replica['behind']['value'], 5)
        self.assertEqual(members['active']['behind']['value'], 0)
        self.assertEqual(report['summary']['read'], 2)
        self.assertEqual(report['summary']['versions'], ['8.4.11'])
        self.assertTrue(all(timeout <= topology_group.QUERY_SECONDS for timeout in observed.timeouts))
        gr_sql = [sql for sql in self.statements(observed) if 'FROM performance_schema.replication_group_' in sql]
        self.assertEqual(len(gr_sql), 4)
        for sql in gr_sql:
            self.assertIn('WHERE MEMBER_ID = @@server_uuid', sql)
            self.assertNotIn(UUID_A, sql)
            self.assertNotIn(UUID_B, sql)
        self.assertEqual(members['active']['role'], 'primary')
        self.assertEqual(replica['role'], 'secondary')
        self.assertEqual(replica['state'], 'ONLINE')
        schema = json.loads((ROOT / 'docs/schemas/topology-group-1.0.json').read_text(encoding='utf-8'))
        validate(report, schema)
        self.assertNotIn('test-password', json.dumps(report))

    def test_gtid_reads_ignore_show_prefix_and_use_full_global_value(self):
        # A syntactically valid 1024-character SHOW prefix must not be parsed
        # as the whole GTID set, even when it ends at an interval boundary.
        prefix = UUID_A + ':' + ':'.join(['1'] * 493) + ':1'
        self.assertEqual(len(prefix), 1024)
        self.save('replica', 'G')
        source = member_fixture(UUID_A, role='PRIMARY', gtid=prefix,
                                exact_variables={'gtid_executed': UUID_A + ':1-1000'})
        replica = member_fixture(UUID_B, role='SECONDARY', gtid=prefix,
                                 exact_variables={'gtid_executed': UUID_A + ':1-900'})
        with self.clients({'active.example': source, 'replica.example': replica}) as observed:
            report = self.read()
        member = next(row for row in report['members'] if row['profile_id'] == 'replica')
        self.assertEqual(member['behind']['value'], 100)
        sql = self.statements(observed)
        self.assertEqual(sum('SELECT @@GLOBAL.gtid_executed' in statement for statement in sql), 2)
        self.assertFalse(any("'gtid_executed'" in statement for statement in sql))

    def test_mariadb_gtid_reads_full_global_position(self):
        self.save('replica', 'G')
        source = member_fixture(engine='mariadb', gtid='0-11-1',
                                exact_variables={'gtid_current_pos': '0-11-100'})
        source['variables']['server_id'] = '11'
        replica = member_fixture(engine='mariadb', gtid='0-11-1',
                                 exact_variables={'gtid_current_pos': '0-11-90'},
                                 channels=[{'Master_Server_Id': '11'}])
        replica['variables']['server_id'] = '12'
        with self.clients({'active.example': source, 'replica.example': replica}) as observed:
            report = self.read()
        member = next(row for row in report['members'] if row['profile_id'] == 'replica')
        self.assertEqual(member['behind']['value'], 10)
        self.assertEqual(sum('SELECT @@GLOBAL.gtid_current_pos' in sql for sql in self.statements(observed)), 2)

    def test_identity_runs_preflight_and_evidence_bound_and_always_probes_proxysql(self):
        with self.clients({'active.example': member_fixture(UUID_A)}) as observed:
            report = self.read('plugins')
        self.assertEqual(report['summary']['read'], 1)
        sql = self.statements(observed)
        # Base identity flow: preflight on raw transport + second _identity inside TargetEvidenceConnection.
        self.assertEqual(sum('SELECT @@version_comment' in s for s in sql), 2)
        self.assertEqual(sum("variable_name = 'admin-version'" in s for s in sql), 2)
        self.assertEqual(sum("'group_replication_group_name'" in s for s in sql), 2)
        # GTID position is skipped for plugins (and accounts).
        self.assertEqual(sum('SELECT @@GLOBAL.gtid_executed' in s for s in sql), 0)
        # Stale cached database facts must never skip ProxySQL Admin identification.
        proxy = member_fixture(proxy='proxysql')
        proxy['variables'].update(version='3.0.11', version_comment='MySQL Community Server - GPL')
        from myflames import client_tools
        stale_member = topology_group._entry({'id': 'm1', 'name': 'm1'})
        stale_member.update(engine='mysql', product='MySQL', version='8.4.11',
                            version_comment='MySQL Community Server - GPL')
        with mock.patch('myflames.proxysql.identify', return_value=(True, '3.0.11')) as probe:
            conn = mock.Mock()
            conn._version_cache = '8.4.11'
            conn._endpoint_key = lambda: ('stale-proxy-test.example', 6032, None, None, None)
            conn.run.return_value = 'MySQL Community Server - GPL\n'
            client_tools.remember_server(conn._endpoint_key(), 'mysql', '8.4.11')
            values, exclusion = topology_group._identity(conn, stale_member, read_position=False)
        probe.assert_called_once()
        self.assertEqual(values, {})
        self.assertEqual(exclusion, 'ProxySQL Admin is read for identity only.')
        self.assertEqual(stale_member['product'], 'ProxySQL Admin')
        self.assertEqual(stale_member['role'], 'proxy')

    def test_preflight_vs_evidence_full_node_switch_is_refused(self):
        # Router switches completely to another node between preflight and TargetEvidenceConnection:
        # both the evidence prefix and body of the second _identity report UUID_B, while preflight saw UUID_A.
        fixture = member_fixture(UUID_A, evidence_variables={'server_uuid': UUID_B}, switch_evidence_body=True)
        with self.clients({'active.example': fixture}) as observed:
            report = self.read('plugins')
        self.assertEqual(report['summary']['failed'], 1, report)
        self.assertEqual(report['plugins']['compared'], [])
        self.assertEqual(report['plugins']['rows'], [])
        self.assertIsNone(report['members'][0]['server_uuid'])
        self.assertFalse(any('FROM information_schema.PLUGINS' in s for s in self.statements(observed)))

    def test_accounts_chunked_identity_change_fails_member_like_base(self):
        accts = [{
            'name': 'u%03d' % i, 'host': '%',
            'grants': ['GRANT SELECT ON *.* TO `u%03d`@`%%`' % i],
            'create': "CREATE USER `u%03d`@`%%` IDENTIFIED WITH 'caching_sha2_password' REQUIRE NONE PASSWORD EXPIRE DEFAULT ACCOUNT UNLOCK" % i,
        } for i in range(55)]
        # Healthy 55-account group read succeeds in 2 evidence-bound chunk packets and skips GTID read.
        healthy = member_fixture(UUID_A, accounts=accts)
        with self.clients({'active.example': healthy}) as observed:
            ok_report = self.read('accounts')
        self.assertEqual(ok_report['summary']['read'], 1, ok_report)
        self.assertEqual(ok_report['accounts']['compared'], ['active'])
        self.assertEqual(len(ok_report['accounts']['rows']), 55)
        ok_sql = self.statements(observed)
        self.assertEqual(sum('SELECT @@GLOBAL.gtid_executed' in s for s in ok_sql), 0)
        self.assertEqual(sum('SHOW GRANTS FOR ' in s for s in ok_sql), 2)
        # Identity switch on chunk 2 (contains u050) fails the member identically to base.
        switching = member_fixture(UUID_A, accounts=accts, switch_on='u050',
                                   evidence_variables={'server_uuid': UUID_B})
        with self.clients({'active.example': switching}) as observed:
            bad_report = self.read('accounts')
        self.assertEqual(bad_report['summary']['failed'], 1, bad_report)
        self.assertEqual(bad_report['accounts']['compared'], [])
        self.assertEqual(bad_report['accounts']['rows'], [])
        member = bad_report['members'][0]
        self.assertIsNone(member['server_uuid'])
        self.assertIsNone(member['engine'])
        bad_sql = self.statements(observed)
        # Only the 2 chunk packets ran (chunk 1 ok, chunk 2 failed identity) — no per-account fallback.
        self.assertEqual(sum('SHOW GRANTS FOR ' in s for s in bad_sql), 2)

    def test_failed_exact_gtid_read_never_falls_back_to_show(self):
        fixture = member_fixture(role='PRIMARY', gtid=UUID_A + ':1-100', exact_denied=True)
        with self.clients({'active.example': fixture}):
            report = self.read()
        member = report['members'][0]
        self.assertIsNone(member['behind'])
        self.assertTrue(member['behind_reason'] or member['outcome'] == 'failed')

    def test_stopped_gr_member_falls_through_to_async_channel(self):
        for version in ('5.7.44', '8.4.11', '9.7.0'):
            with self.subTest(version=version):
                fixture = member_fixture(role='PRIMARY', has_gr=True, state='OFFLINE', channels=[{
                    'Master_Host': 'source.example', 'Master_Port': '3306',
                    'Slave_IO_Running': 'Yes', 'Slave_SQL_Running': 'Yes'}])
                fixture['variables'].update(version=version, group_replication_single_primary_mode='ON')
                with self.clients({'active.example': fixture}) as observed:
                    report = self.read()
                member = report['members'][0]
                self.assertEqual((member['role'], member['state']), ('replica', 'running'))
                self.assertTrue(any('SHOW SLAVE STATUS' in sql or 'SHOW REPLICA STATUS' in sql
                                    for sql in self.statements(observed)))
                self.assertFalse(any('replication_group_member_stats' in sql for sql in self.statements(observed)))

    def test_galera_never_reports_gtid_behind_even_with_a_stored_primary(self):
        self.save('second', 'G')
        self.store.record_topology_identity('active', {'server_uuid': UUID_A})
        self.store.record_topology_identity('second', {'server_uuid': UUID_B, 'source_uuids': [UUID_A]})
        fixtures = {}
        for host, gtid in (('active.example', '0-1-100'), ('second.example', '0-2-1')):
            fixture = member_fixture(engine='mariadb', gtid=gtid,
                status={'wsrep_local_state_comment': 'Synced', 'wsrep_local_recv_queue': '3'})
            fixture['variables'].update(wsrep_on='ON', wsrep_gtid_mode='OFF')
            fixtures[host] = fixture
        with self.clients(fixtures):
            report = self.read()
        self.assertEqual(report['primary_profile_id'], 'active')
        for member in report['members']:
            self.assertIsNone(member['behind'])
            self.assertEqual(member['behind_reason'],
                             'Galera nodes apply write sets from every node; see the receive queue.')

    def test_replica_connecting_and_preparing_states_reach_member_and_map(self):
        for engine, io in (('mysql', 'Connecting'), ('mariadb', 'Preparing')):
            with self.subTest(engine=engine, io=io):
                fixture = member_fixture(engine=engine, channels=[{
                    'Master_Host': 'external.example', 'Master_Port': '3306',
                    'Slave_IO_Running': io, 'Slave_SQL_Running': 'Yes'}])
                with self.clients({'active.example': fixture}):
                    report = self.read('map')
                self.assert_map_schema(report)
                self.assertEqual(report['members'][0]['state'], 'connecting')
                self.assertEqual(report['map']['edges'][0]['state'], 'connecting')

    def test_no_more_than_four_concurrent_members_and_one_connection_each(self):
        fixtures = {'active.example': member_fixture(pause=0.04)}
        for number in range(8):
            pid = 'member-%02d' % number
            self.save(pid, 'G')
            fixtures[pid + '.example'] = member_fixture(pause=0.04)
        with self.clients(fixtures) as observed:
            report = self.read()
        self.assertEqual(report['summary']['read'], 9, report)
        self.assertEqual(len(observed.connections), 9)
        self.assertEqual({conn.host for conn in observed.connections}, set(fixtures))
        self.assertEqual(set(observed.logins), set(fixtures))
        self.assertLessEqual(observed.peak[0], 4)
        self.assertGreater(observed.peak[0], 1, 'independent member reads overlap')

    def test_budget_exhausted_before_start_never_logs_in(self):
        self.save('peer', 'G')
        with self.clients({}) as observed, mock.patch.object(topology_group, 'REQUEST_SECONDS', 0):
            report = self.read()
        self.assertEqual(observed.logins, [])
        self.assertEqual(report['summary']['not_reached'], 2)
        self.assertTrue(all(m['message'].startswith('Not read: the time budget ') for m in report['members']))

    def test_accounts_have_300_seconds_but_each_query_keeps_30_seconds(self):
        self.assertEqual(topology_group.REQUEST_SECONDS, 120)
        self.assertEqual(topology_group.ACCOUNTS_REQUEST_SECONDS, 300)
        self.assertEqual(topology_group.QUERY_SECONDS, 30)
        clock = [0]

        def capture(conn, **kwargs):
            # Simulate serial account reads without waiting in the test. The
            # final real connector query is beyond the other tabs' budget.
            clock[0] = 150
            self.assertEqual(conn.run('SELECT 1').strip(), '1')
            return side([account('app-%02d' % index) for index in range(12)])

        with self.clients({'active.example': member_fixture()}) as observed, \
                mock.patch.object(topology_group.time, 'monotonic', side_effect=lambda: clock[0]), \
                mock.patch.object(account_compare, 'capture', side_effect=capture):
            report = self.read('accounts')
        self.assertEqual(report['summary']['read'], 1, report)
        self.assertEqual(report['accounts']['compared'], ['active'])
        self.assertEqual(len(report['accounts']['rows']), 12)
        self.assertTrue(all(timeout <= 30 for timeout in observed.timeouts))
        self.assertRegex(report['members'][0]['message'],
                         r'Accounts are read one at a time; 12 accounts took 150(?:\.0+)? s on this server\.')

    def test_accounts_request_budget_still_stops_reads_at_300_seconds(self):
        clock = [0]

        def capture(conn, **kwargs):
            clock[0] = 301
            conn.run('SELECT 1')
            self.fail('the expired request must reject the next account query')

        with self.clients({'active.example': member_fixture()}) as observed, \
                mock.patch.object(topology_group.time, 'monotonic', side_effect=lambda: clock[0]), \
                mock.patch.object(account_compare, 'capture', side_effect=capture):
            report = self.read('accounts')
        self.assertEqual(report['summary']['failed'], 1)
        self.assertEqual(report['accounts']['compared'], [])
        self.assertFalse(any(sql == 'SELECT 1' for sql in self.statements(observed)))

    def test_extra_accounts_budget_does_not_extend_other_sections(self):
        fill = self.store.connection_with_saved_password
        clock = [0]

        def slow_fill(value):
            result = fill(value)
            clock[0] = 150
            return result

        for section in ('members', 'parameters', 'plugins', 'map', 'accounts'):
            with self.subTest(section=section):
                clock[0] = 0
                with self.clients({'active.example': member_fixture()}) as observed, \
                        mock.patch.object(topology_group.time, 'monotonic', side_effect=lambda: clock[0]), \
                        mock.patch.object(self.store, 'connection_with_saved_password', side_effect=slow_fill), \
                        mock.patch.object(account_compare, 'capture', return_value=side([account()])):
                    report = self.read(section)
                if section == 'accounts':
                    self.assertEqual(report['summary']['read'], 1, report)
                    self.assertTrue(observed.logins)
                    self.assertNotIn('Accounts are read one at a time', report['members'][0]['message'])
                else:
                    self.assertEqual(report['summary']['not_reached'], 1, report)
                    self.assertEqual(observed.logins, [])

    def test_queued_member_without_a_query_window_is_not_reached(self):
        self.save('peer', 'G')
        fixtures = {p['host']: member_fixture(pause=0.15) for p in self.store.list('profile')}
        with self.clients(fixtures) as observed, \
                mock.patch.object(topology_group, 'MAX_CONCURRENCY', 1), \
                mock.patch.object(topology_group, 'REQUEST_SECONDS', 0.4), \
                mock.patch.object(topology_group, 'QUERY_SECONDS', 0.3):
            report = self.read()
        self.assertEqual(len(set(observed.logins)), 1, report)
        self.assertEqual(report['summary']['not_reached'], 1)
        skipped = next(m for m in report['members'] if m['outcome'] == 'not_reached')
        self.assertNotIn(skipped['host'], observed.logins)

    def test_budget_spent_loading_credentials_refuses_the_first_login(self):
        clock = [0]
        fill = self.store.connection_with_saved_password

        def slow_fill(value):
            result = fill(value)
            clock[0] = 100
            return result

        with self.clients({'active.example': member_fixture()}) as observed, \
                mock.patch.object(self.store, 'connection_with_saved_password', side_effect=slow_fill), \
                mock.patch.object(topology_group.time, 'monotonic', side_effect=lambda: clock[0]):
            report = self.read()
        self.assertEqual(observed.logins, [])
        self.assertEqual(report['summary']['not_reached'], 1, report)

    def test_removing_active_member_after_one_query_refuses_every_later_login(self):
        def revoke(host):
            self.store.assign_profile_group(['active'], '')

        with self.clients({'active.example': member_fixture()}, after_spawn=revoke) as observed:
            report = self.read()
        self.assertEqual(observed.logins, ['active.example'])
        self.assertEqual(report['summary']['failed'], 1, report)
        self.assertIn('group membership changed', report['members'][0]['message'])

    def test_active_endpoint_or_generation_change_revokes_later_member_logins(self):
        self.save('peer', 'G')
        for change in ('endpoint', 'recreate'):
            with self.subTest(change=change):
                current = self.store.get('profile', 'active')
                order = self.store.list('profile')
                first = order[0]
                fixtures = {profile['host']: member_fixture() for profile in order}
                changed = [False]

                def revoke(host):
                    if changed[0]:
                        return
                    changed[0] = True
                    if change == 'endpoint':
                        self.store.save('profile', dict(current, host='changed.example'), 'active',
                                        password='test-password', remember=True)
                    else:
                        self.store.delete_connection_group('G')
                        self.store.save_connection_group('G', ['active', 'peer'])

                with self.clients(fixtures, after_spawn=revoke) as observed, \
                        mock.patch.object(topology_group, 'MAX_CONCURRENCY', 1), \
                        mock.patch.object(self.store, 'connection_with_saved_password',
                                          wraps=self.store.connection_with_saved_password) as password:
                    report = self.read()
                self.assertEqual(observed.logins, [first['host']])
                self.assertEqual(password.call_count, 1)
                self.assertEqual(report['summary']['failed'], 2, report)

    def test_revoked_queued_member_gets_no_login(self):
        self.save('peer', 'G')
        order = self.store.list('profile')
        first, removed = order[0], order[-1]
        fixtures = {p['host']: member_fixture() for p in order}

        def revoke(host):
            if host == first['host']:
                self.store.assign_profile_group([removed['id']], '')

        with self.clients(fixtures, after_spawn=revoke) as observed, \
                mock.patch.object(topology_group, 'MAX_CONCURRENCY', 1):
            report = self.read(active=first['id'])
        self.assertEqual(set(observed.logins), {first['host']})
        revoked = next(m for m in report['members'] if m['profile_id'] == removed['id'])
        self.assertEqual(revoked['outcome'], 'failed')
        self.assertIn('group membership changed', revoked['message'])

    def test_client_failure_text_never_exposes_password_or_sql(self):
        bad = member_fixture(error='ERROR 1045 (28000): Access denied password=test-password; SELECT secret_payload')
        with self.clients({'active.example': bad}) as observed:
            report = self.read()
        self.assertEqual(len(observed.connections), 1)
        self.assertEqual(observed.logins, ['active.example'])
        self.assertEqual(report['members'][0]['outcome'], 'failed')
        text = json.dumps(report)
        for secret in ('test-password', 'secret_payload', 'ERROR 1045', 'SELECT'):
            self.assertNotIn(secret, text)
        self.assertIn('access was denied', report['members'][0]['message'])

    def test_login_and_transport_errors_cost_one_client_invocation_per_member(self):
        self.save('peer', 'G')
        for code in (1040, 1045, 1044, 1049, 1226, 1698, 1862, 2002, 2003, 2005, 2013, 2026,
                     1129, 1130, 1251, 3118, 3159, 3955, 4151):
            with self.subTest(code=code):
                fixtures = {host: member_fixture(error='ERROR %d (HY000): secret_payload' % code)
                            for host in ('active.example', 'peer.example')}
                with self.clients(fixtures) as observed:
                    report = self.read()
                self.assertEqual(sorted(observed.logins), ['active.example', 'peer.example'])
                self.assertEqual(report['summary']['failed'], 2)
                self.assertNotIn('secret_payload', json.dumps(report))

    def test_unlisted_initial_coded_failure_is_not_retried(self):
        connection = mock.Mock()
        connection.run.side_effect = connector.ConnectorError('ERROR 1999 (HY000): login refused')
        reader = topology_group._ReadConnection(connection)
        for unused in range(3):
            with self.assertRaises(connector.ConnectorError):
                reader.run('SELECT @@version_comment')
        self.assertEqual(connection.run.call_count, 1)

    def test_initial_sql_error_evidence_does_not_latch(self):
        for evidence in ('line', 'guard'):
            with self.subTest(evidence=evidence):
                error = connector.ConnectorError(
                    'ERROR 1045 (28000)%s: Access denied for user test' %
                    (' at line 1' if evidence == 'line' else ''))
                if evidence == 'guard':
                    error.guard_markers = ('private_guard',)
                connection = mock.Mock()
                connection.run.side_effect = [error, '1\n']
                reader = topology_group._ReadConnection(connection)
                with self.assertRaises(connector.ConnectorError):
                    reader.run('SELECT unavailable')
                reader.check()
                self.assertEqual(reader.run('SELECT 1'), '1\n')
                self.assertEqual(connection.run.call_count, 2)

    def test_explicit_login_failure_after_success_is_not_retried(self):
        for code in (1040, 1226, 1698, 1862, 3118, 3955, 4151):
            with self.subTest(code=code):
                message = ('ERROR %d (HY000): ' % code) + (
                    "User 'test' has exceeded the 'max_user_connections' resource" if code == 1226
                    else 'login refused')
                connection = mock.Mock()
                connection.run.side_effect = ['1\n', connector.ConnectorError(message), '1\n']
                reader = topology_group._ReadConnection(connection)
                self.assertEqual(reader.run('SELECT 1'), '1\n')
                for unused in range(2):
                    with self.assertRaises(connector.ConnectorError):
                        reader.run('SELECT @@version_comment')
                self.assertEqual(connection.run.call_count, 2)

    def test_proxy_echo_login_failure_cannot_fall_through_to_database_probe(self):
        for code in (1045, 1129, 1251, 3159, 2013):
            with self.subTest(code=code):
                fixture = member_fixture(proxy='proxysql', error_on='SELECT @@version',
                    query_error="ERROR %d (HY000): Access denied for user 'secret_payload'@'localhost'" % code)
                with self.clients({'active.example': fixture}) as observed:
                    report = self.read()
                self.assertEqual(len(observed.logins), 3)
                self.assertFalse(any('SHOW GLOBAL VARIABLES' in sql for sql in self.statements(observed)))
                self.assertEqual(report['summary']['failed'], 1)

    def test_mariadb_socket_authentication_refusal_is_not_retried(self):
        fixture = member_fixture(engine='mariadb',
            error="ERROR 1698 (28000): Access denied for user 'root'@'localhost'")
        with self.clients({'active.example': fixture}) as observed:
            report = self.read()
        self.assertEqual(observed.logins, ['active.example'])
        self.assertEqual(report['summary']['failed'], 1)
        self.assertNotIn("'root'", json.dumps(report))

    def test_proxysql_admin_sql_error_is_not_a_login_refusal(self):
        # Admin reports unsupported statements with 1045 even after login.
        # This is also the first optional statement in the identity probe.
        fixture = member_fixture(proxy='proxysql', error_on='SELECT @@version_comment',
            query_error='ERROR 1045 (28000): ProxySQL Admin Error: near "SELECT": syntax error')
        fixture['variables'].update(version='3.0.11', version_comment='ProxySQL Admin')
        with self.clients({'active.example': fixture}) as observed:
            report = self.read()
        self.assertEqual(report['summary']['read'], 1, report)
        self.assertEqual(report['members'][0]['product'], 'ProxySQL Admin')
        self.assertEqual(report['members'][0]['role'], 'proxy')
        self.assertGreater(len(observed.logins), 1)
        self.assertFalse(any('SHOW GLOBAL VARIABLES' in sql for sql in self.statements(observed)))

    def test_authenticated_proxysql_error_does_not_poison_later_reads(self):
        class Connection(object):
            calls = 0

            def run(self, sql, **kwargs):
                self.calls += 1
                if sql == 'SELECT unsupported':
                    raise connector.ConnectorError(
                        'ERROR 1045 (28000): ProxySQL Admin Error: near "unsupported": syntax error')
                return '1\n'

        connection = Connection()
        reader = topology_group._ReadConnection(connection)
        self.assertEqual(reader.run('SELECT 1'), '1\n')
        with self.assertRaises(connector.ConnectorError):
            reader.run('SELECT unsupported')
        reader.check()
        self.assertEqual(reader.run('SELECT 1'), '1\n')
        self.assertEqual(connection.calls, 3)

    def test_authenticated_component_database_denial_keeps_plugins(self):
        fixture = member_fixture(error_on='SELECT component_urn FROM mysql.component',
            query_error="ERROR 1044 (42000): Access denied for user 'secret_component_user'@'%' to database 'mysql'")
        with self.clients({'active.example': fixture}) as observed:
            report = self.read('plugins')
        self.assertEqual(report['summary']['read'], 1, report)
        self.assertEqual(report['plugins']['compared'], ['active'])
        self.assertEqual(report['plugins']['rows'][0]['name'], 'auth')
        self.assertTrue(any('mysql.component' in note['message'] and 'denied' in note['message'].lower()
                            for note in report['plugins']['notes']))
        self.assertNotIn('secret_component_user', json.dumps(report))
        self.assertGreater(len(observed.logins), 1)

    def test_authenticated_sql_timeout_word_is_not_a_transport_timeout(self):
        for message in ('ERROR 1045 (28000): ProxySQL Admin Error: no such column: timeout',
                        "ERROR 1193 (HY000): Unknown system variable 'lock_wait_timeout'",
                        'ERROR 1205 (HY000): Lock wait timeout exceeded',
                        "ERROR 1226 (42000): User 'test' has exceeded the 'max_questions' resource",
                        'ERROR 1045 (28000) at line 1: Access denied for user test'):
            with self.subTest(message=message):
                connection = mock.Mock()
                connection.run.side_effect = ['1\n', connector.ConnectorError(message), '1\n']
                reader = topology_group._ReadConnection(connection)
                self.assertEqual(reader.run('SELECT 1'), '1\n')
                with self.assertRaises(connector.ConnectorError):
                    reader.run('SELECT unsupported')
                reader.check()
                self.assertEqual(reader.run('SELECT 1'), '1\n')
                self.assertEqual(connection.run.call_count, 3)

    def test_authenticated_optional_identity_sql_error_remains_unavailable_fact(self):
        fixture = member_fixture(error_on='SELECT @@version_comment',
                                 query_error='ERROR 1193 (HY000) at line 1: optional variable unavailable')
        with self.clients({'active.example': fixture}) as observed:
            report = self.read()
        self.assertEqual(report['summary']['read'], 1, report)
        self.assertGreater(len(observed.logins), 1)

    def test_slow_query_is_bounded_and_client_is_reaped(self):
        with self.clients({'active.example': member_fixture(pause_each=1)}) as observed, \
                mock.patch.object(topology_group, 'REQUEST_SECONDS', 2), \
                mock.patch.object(topology_group, 'QUERY_SECONDS', 0.08):
            started = time.monotonic()
            report = self.read()
            elapsed = time.monotonic() - started
        self.assertEqual(len(observed.connections), 1)
        self.assertEqual(observed.logins, ['active.example'])
        self.assertLess(elapsed, 1)
        self.assertEqual(report['summary']['failed'], 1)
        self.assertTrue(all(timeout <= 0.08 for timeout in observed.timeouts))

    def test_proxy_members_are_identity_only_and_never_compared(self):
        self.save('readyset', 'G')
        proxy = member_fixture(proxy='proxysql')
        proxy['variables'].update(version='3.0.11', version_comment='ProxySQL Admin')
        readyset = member_fixture(proxy='readyset')
        readyset['variables']['version_comment'] = 'Readyset'
        fixtures = {'active.example': proxy, 'readyset.example': readyset}
        for section in ('parameters', 'accounts', 'plugins'):
            with self.subTest(section=section), self.clients(fixtures) as observed:
                report = self.read(section)
            self.assertEqual(report['summary']['read'], 2, report)
            self.assertEqual(report[section]['rows'], [])
            self.assertEqual(report[section]['compared'], [])
            self.assertEqual({r['profile_id'] for r in report[section]['not_compared']}, {'active', 'readyset'})
            self.assertTrue(all(r['reason'] for r in report[section]['not_compared']))
            self.assertEqual({m['product']: m['role'] for m in report['members']},
                             {'ProxySQL Admin': 'proxy', 'Readyset': 'cache'})
            self.assertTrue(all(m['server_uuid'] is None for m in report['members']))
            sql = '\n'.join(self.statements(observed)).lower()
            for forbidden in ('show global variables', 'information_schema.plugins', 'mysql.user', 'mysql.component'):
                self.assertNotIn(forbidden, sql)

    def test_plugins_are_read_on_each_own_connection_and_components_mysql_only(self):
        self.save('maria', 'G')
        mysql = member_fixture(components=['file://component_validate_password'])
        maria = member_fixture(engine='mariadb')
        with self.clients({'active.example': mysql, 'maria.example': maria}) as observed:
            report = self.read('plugins')
        self.assertEqual(report['plugins']['compared'], ['active', 'maria'], report)
        self.assertEqual(report['plugins']['not_compared'], [])
        rows = {r['name']: r for r in report['plugins']['rows']}
        self.assertEqual(rows['auth']['status'], 'same')
        component = rows['file://component_validate_password']
        self.assertEqual(component['status'], 'missing')
        self.assertIsNone(component['values']['maria'])
        self.assertEqual(sum('FROM information_schema.PLUGINS' in sql for sql in self.statements(observed)), 2)
        self.assertFalse(any('mysql.component' in sql for sql in self.statements(observed, 'maria.example')))

    def test_component_privilege_failure_keeps_plugin_comparison_and_member_note(self):
        self.save('other', 'G')
        fixtures = {'active.example': member_fixture(component_denied=True),
                    'other.example': member_fixture(UUID_B, components=['file://component_validate_password'])}
        with self.clients(fixtures):
            report = self.read('plugins')
        self.assertEqual(report['summary']['failed'], 0, report)
        self.assertEqual(report['plugins']['compared'], ['active', 'other'])
        self.assertEqual(report['plugins']['not_compared'], [])
        rows = {row['name']: row for row in report['plugins']['rows']}
        self.assertEqual(rows['auth']['status'], 'same')
        self.assertEqual(rows['file://component_validate_password']['status'], 'missing')
        note = ('mysql.component lists components installed with INSTALL COMPONENT; '
                'components loaded from a manifest file are not listed.')
        for pid in ('active', 'other'):
            member_notes = [row['message'] for row in report['plugins']['notes'] if row['profile_id'] == pid]
            self.assertIn(note, member_notes)
        self.assertTrue(any('denied' in row['message'].lower() for row in report['plugins']['notes']
                            if row['profile_id'] == 'active'))
        self.assertNotIn('secret_component_user', json.dumps(report))
        schema = json.loads((ROOT / 'docs/schemas/topology-group-1.0.json').read_text(encoding='utf-8'))
        validate(report, schema)

    def test_mariadb_read_only_enum_preserves_raw_text_and_refuses_every_non_off_value(self):
        for raw, expected in (('NO_LOCK', True), ('NO_LOCK_NO_ADMIN', True), ('on', True),
                              ('OFF', False), ('0', False), ('fAlSe', False), (None, None)):
            with self.subTest(raw=raw):
                fixture = member_fixture(engine='mariadb')
                fixture['variables']['version'] = '12.0.1-MariaDB'
                if raw is None:
                    fixture['variables'].pop('read_only')
                else:
                    fixture['variables']['read_only'] = raw
                with self.clients({'active.example': fixture}):
                    report = self.read()
                member = report['members'][0]
                self.assertEqual(member['outcome'], 'read', report)
                self.assertEqual(member['read_only_value'], raw)
                self.assertIs(member['read_only'], expected)

    def test_gr_member_role_and_applier_queue_columns_start_at_mysql_8002(self):
        for version in ('8.0.0', '8.0.1', '8.0.2'):
            with self.subTest(version=version):
                fixture = member_fixture(role='PRIMARY', has_gr=True)
                fixture['variables'].update(version=version, group_replication_single_primary_mode='ON')
                fixture['status'] = {'group_replication_primary_member': UUID_A}
                with self.clients({'active.example': fixture}) as observed:
                    report = self.read()
                member = report['members'][0]
                self.assertEqual(member['outcome'], 'read', report)
                self.assertEqual(member['role'], 'primary')
                sql = '\n'.join(self.statements(observed))
                if version == '8.0.2':
                    self.assertIn('MEMBER_ROLE', sql)
                    self.assertIn('COUNT_TRANSACTIONS_REMOTE_IN_APPLIER_QUEUE', sql)
                    self.assertEqual(member['lag']['value'], 0)
                else:
                    self.assertNotIn('MEMBER_ROLE', sql)
                    self.assertNotIn('COUNT_TRANSACTIONS_REMOTE_IN_APPLIER_QUEUE', sql)
                    self.assertIsNone(member['lag']['value'])

    def test_async_and_galera_lag_have_distinct_units_and_no_invented_primary(self):
        self.save('galera', 'G')
        replica = member_fixture(channels=[{'Seconds_Behind_Source': '7', 'Replica_IO_Running': 'Yes',
                                            'Replica_SQL_Running': 'Yes'}])
        galera = member_fixture(engine='mariadb', status={'wsrep_local_state_comment': 'Synced', 'wsrep_local_recv_queue': '3'})
        galera['variables']['wsrep_on'] = 'ON'
        with self.clients({'active.example': replica, 'galera.example': galera}):
            report = self.read()
        by_id = {m['profile_id']: m for m in report['members']}
        self.assertEqual(by_id['active']['role'], 'replica')
        self.assertEqual(by_id['active']['state'], 'running')
        self.assertEqual(by_id['active']['lag']['kind'], 'replica_seconds')
        self.assertEqual(by_id['active']['lag']['value'], 7)
        self.assertIn('Seconds_Behind_Source is an estimate', by_id['active']['lag']['message'])
        self.assertEqual(by_id['galera']['role'], 'node')
        self.assertEqual(by_id['galera']['state'], 'Synced')
        self.assertEqual(by_id['galera']['lag']['kind'], 'galera_recv_queue')
        self.assertEqual(by_id['galera']['lag']['value'], 3)
        self.assertIsNone(by_id['galera']['super_read_only'])
        self.assertIsNone(report['primary_profile_id'])
        self.assertTrue(all(m['behind'] is None and m['behind_reason'] for m in report['members']))

    def test_mysql57_offline_member_does_not_inherit_stale_primary_role(self):
        fixture = member_fixture(has_gr=True, state='OFFLINE', status={'group_replication_primary_member': UUID_A})
        fixture['variables'].update(version='5.7.44', group_replication_single_primary_mode='ON')
        with self.clients({'active.example': fixture}):
            report = self.read()
        member = report['members'][0]
        self.assertEqual(member['outcome'], 'read', report)
        self.assertEqual(member['state'], 'reachable')
        self.assertEqual(member['role'], 'standalone')
        self.assertIsNone(report['primary_profile_id'])

    def test_stored_source_fallback_and_disabled_gtids_are_not_caught_up(self):
        source = self.save('source', 'G')
        self.store.record_topology_identity('source', {'server_uuid': UUID_B})
        self.store.record_topology_identity('active', {'server_uuid': UUID_A, 'source_uuids': [UUID_B]})
        fixtures = {'active.example': member_fixture(UUID_A), 'source.example': member_fixture(UUID_B)}
        fixtures['active.example']['variables']['gtid_mode'] = 'OFF'
        with self.clients(fixtures):
            report = self.read()
        self.assertEqual(report['primary_profile_id'], source['id'])
        member = next(m for m in report['members'] if m['profile_id'] == 'active')
        self.assertIsNone(member['behind'])
        self.assertTrue(member['behind_reason'])

    def test_parameter_and_account_comparisons_run_once_after_all_captures(self):
        self.save('peer', 'G')
        fixtures = {'active.example': member_fixture(), 'peer.example': member_fixture(UUID_B)}
        for section, module, capture in (
                ('parameters', param_compare, snapshot([item('max_connections', '200')])),
                ('accounts', account_compare, side([account('app')]))):
            with self.subTest(section=section), self.clients(fixtures), \
                    mock.patch.object(module, 'capture', side_effect=lambda *a, **kw: copy.deepcopy(capture)) as collect, \
                    mock.patch.object(module, 'compare_many', wraps=module.compare_many) as compare:
                report = self.read(section)
            self.assertEqual(collect.call_count, 2)
            compare.assert_called_once()
            self.assertEqual(set(compare.call_args[0][0]), {'active', 'peer'})
            self.assertEqual(report[section]['compared'], ['active', 'peer'])

    def test_incomplete_inventories_are_not_compared_without_failing_other_members(self):
        self.save('peer', 'G')
        fixtures = {'active.example': member_fixture(), 'peer.example': member_fixture(UUID_B)}
        for section, module, good in (
                ('parameters', param_compare, snapshot([item('max_connections', '200')])),
                ('accounts', account_compare, side([account('app')]))):
            for failure in ({'complete': False}, {'complete': None},
                            {'error': 'ERROR 1142: SELECT secret_payload password=secret_value'}):
                with self.subTest(section=section, failure=failure):
                    def capture(conn, **kwargs):
                        return dict(copy.deepcopy(good), **failure) if conn.host == 'active.example' else copy.deepcopy(good)

                    with self.clients(fixtures), mock.patch.object(module, 'capture', side_effect=capture):
                        report = self.read(section)
                    self.assertEqual(report['summary']['failed'], 0, report)
                    self.assertEqual(report[section]['compared'], ['peer'])
                    self.assertEqual([row['profile_id'] for row in report[section]['not_compared']], ['active'])
                    self.assertIn('inventory is incomplete', report[section]['not_compared'][0]['reason'])
                    self.assertNotIn('secret_payload', json.dumps(report))
                    self.assertNotIn('secret_value', json.dumps(report))
                    self.assertTrue(report[section]['rows'])

    def test_unreadable_accounts_are_omitted_globally_while_readable_accounts_compare(self):
        self.save('peer', 'G')
        fixtures = {'active.example': member_fixture(), 'peer.example': member_fixture(UUID_B)}
        for failure in ({'error': 'ERROR 1142: secret_payload'}, {'exists': False},
                        {'attributes_checked': False}, {'defaults_checked': False},
                        {'tls_checked': False}, {'grants_checked': False}):
            with self.subTest(failure=failure):
                def capture(conn, **kwargs):
                    unreadable = account('unreadable', **failure) if conn.host == 'active.example' else account('unreadable')
                    return side([account('readable'), unreadable])

                with self.clients(fixtures), mock.patch.object(account_compare, 'capture', side_effect=capture):
                    report = self.read('accounts')
                self.assertEqual(report['accounts']['compared'], ['active', 'peer'])
                self.assertEqual([(row['account'], row['status']) for row in report['accounts']['rows']],
                                 [('readable@%', 'same')])
                excluded = report['accounts']['not_compared']
                self.assertEqual([row['profile_id'] for row in excluded], ['active'])
                self.assertIn('unreadable@%', excluded[0]['reason'])
                self.assertNotIn('secret_payload', json.dumps(report))
                validate(report, json.loads((ROOT / 'docs/schemas/topology-group-1.0.json').read_text()))

    def test_evidence_scope_refuses_a_changed_identity_after_preflight(self):
        for engine, change in (('mysql', {'server_uuid': UUID_B}),
                               ('mariadb', {'hostname': 'changed-member'})):
            with self.subTest(engine=engine):
                fixture = member_fixture(engine=engine, evidence_variables=change)
                with self.clients({'active.example': fixture}) as observed:
                    report = self.read('plugins')
                self.assertEqual(report['summary']['failed'], 1, report)
                self.assertEqual(report['plugins']['compared'], [])
                self.assertEqual(report['plugins']['rows'], [])
                self.assertIsNone(report['members'][0]['server_uuid'])
                self.assertIsNone(report['members'][0]['engine'])
                self.assertFalse(any('FROM information_schema.PLUGINS' in sql for sql in self.statements(observed)))

    def test_evidence_scope_discards_changed_role_and_plugin_reads(self):
        for section, sql in (('members', 'FROM performance_schema.replication_group_members'),
                             ('map', 'FROM performance_schema.replication_group_members'),
                             ('parameters', 'FROM performance_schema.replication_group_members'),
                             ('plugins', 'FROM information_schema.PLUGINS')):
            with self.subTest(section=section):
                fixture = member_fixture(role='PRIMARY', switch_on=sql, evidence_variables={'server_uuid': UUID_B})
                with self.clients({'active.example': fixture}) as observed:
                    report = self.read(section)
                self.assertEqual(report['summary']['failed'], 1, report)
                member = report['members'][0]
                for field in ('engine', 'server_uuid', 'version', 'role', 'state', 'read_only', 'lag', 'behind'):
                    self.assertIsNone(member[field], field)
                self.assertEqual(member['channels'], [])
                self.assertEqual(report['summary']['versions'], [])
                self.assertEqual(sum(sql in statement for statement in self.statements(observed)), 1)
                if section in ('parameters', 'plugins'):
                    self.assertEqual(report[section]['compared'], [])
                    self.assertEqual(report[section]['rows'], [])

    def test_optional_capture_cannot_hide_identity_failure(self):
        self.save('peer', 'G')
        for section, module, good in (
                ('parameters', param_compare, snapshot([item('max_connections', '200')])),
                ('accounts', account_compare, side([account('app')]))):
            with self.subTest(section=section):
                fixtures = {'active.example': member_fixture(switch_on='SELECT 1', evidence_variables={'server_uuid': UUID_B}),
                            'peer.example': member_fixture(UUID_C)}

                def capture(conn, **kwargs):
                    try:
                        conn.run('SELECT 1')
                    except connector.ConnectorError:
                        pass
                    return copy.deepcopy(good)

                with self.clients(fixtures) as observed, mock.patch.object(module, 'capture', side_effect=capture):
                    report = self.read(section)
                self.assertEqual(report['summary']['failed'], 1, report)
                self.assertEqual(report['summary']['read'], 1, report)
                self.assertEqual(report[section]['compared'], ['peer'])
                self.assertEqual([row['profile_id'] for row in report[section]['not_compared']], ['active'])
                member = next(row for row in report['members'] if row['profile_id'] == 'active')
                self.assertIsNone(member['engine'])

    def test_group_parameters_reread_provider_options_and_use_observed_roles(self):
        self.save('peer', 'G')
        catalog = snapshot([item('read_only', 'OFF', 'boolean'), item('super_read_only', 'OFF', 'boolean')])
        for mode, expected in (('single-primary', 'per_server'), ('async', 'per_server'),
                               ('offline-gr', 'different'), ('standalone', 'different')):
            with self.subTest(mode=mode):
                fixtures = {'active.example': member_fixture(UUID_A), 'peer.example': member_fixture(UUID_B)}
                if mode in ('single-primary', 'offline-gr'):
                    for host, role in (('active.example', 'PRIMARY'), ('peer.example', 'SECONDARY')):
                        fixtures[host]['role'] = role
                        if mode == 'offline-gr':
                            fixtures[host]['state'] = 'OFFLINE'
                        fixtures[host]['variables'].update(group_replication_group_name=UUID_D,
                                                           group_replication_single_primary_mode='ON')
                elif mode == 'async':
                    fixtures['peer.example']['channels'] = [{'Source_UUID': UUID_A,
                        'Replica_IO_Running': 'Yes', 'Replica_SQL_Running': 'Yes'}]

                def capture(conn, **kwargs):
                    value = copy.deepcopy(catalog)
                    if mode in ('single-primary', 'offline-gr'):
                        value['items'].extend([item('group_replication_group_name', UUID_D, 'string'),
                                               item('group_replication_single_primary_mode', 'ON', 'boolean')])
                    if conn.host == 'peer.example':
                        for row in value['items']:
                            if row['name'] in ('read_only', 'super_read_only'):
                                row['value'] = 'ON'
                    return value

                with self.clients(fixtures), mock.patch.object(param_compare, 'capture', side_effect=capture) as collect:
                    report = self.read('parameters')
                self.assertEqual(report['parameters']['compared'], ['active', 'peer'])
                self.assertTrue(all(call[1].get('full_provider_options') for call in collect.call_args_list))
                for row in report['parameters']['rows']:
                    if row['name'] not in ('read_only', 'super_read_only'):
                        continue
                    self.assertEqual(row['status'], expected)
                    if expected == 'per_server':
                        self.assertEqual(row['reason'], "Set by the member's role")

    def test_mysql_channel_projection_handles_current_and_legacy_columns(self):
        for version, prefix, status_sql in (('8.4.11', 'Source', 'SHOW REPLICA STATUS'),
                                             ('5.7.44', 'Master', 'SHOW SLAVE STATUS')):
            with self.subTest(version=version):
                channel = {'Channel_Name': 'east', prefix + '_Host': 'source.example',
                           prefix + '_Port': '3307', prefix + '_UUID': UUID_B,
                           prefix + '_Server_Id': '42', 'Seconds_Behind_' + prefix: '9',
                           prefix + '_User': 'channel-user-secret', prefix + '_Password': 'channel-password-secret',
                           'Password': 'extra-password-secret', 'User': 'extra-user-secret'}
                running_prefix = 'Replica' if prefix == 'Source' else 'Slave'
                channel.update({running_prefix + '_IO_Running': 'Connecting', running_prefix + '_SQL_Running': 'Yes'})
                fixture = member_fixture(channels=[channel])
                fixture['variables']['version'] = version
                with self.clients({'active.example': fixture}) as observed:
                    report = self.read()
                member = report['members'][0]
                self.assertEqual(member['channels'], [{'name': 'east', 'source_host': 'source.example',
                    'source_port': 3307, 'source_uuid': UUID_B, 'source_server_id': 42,
                    'io_running': 'Connecting', 'sql_running': 'Yes', 'seconds_behind': 9}])
                self.assertEqual(member['role'], 'replica')
                self.assertEqual(member['state'], 'connecting')
                self.assertTrue(any(status_sql in sql for sql in self.statements(observed)))
                for value in ('channel-user-secret', 'channel-password-secret', 'extra-password-secret', 'extra-user-secret'):
                    self.assertNotIn(value, json.dumps(report))
                validate(report, json.loads((ROOT / 'docs/schemas/topology-group-1.0.json').read_text(encoding='utf-8')))

    def test_group_parameters_keep_drift_between_two_observed_replicas(self):
        for pid in ('replica', 'writable'):
            self.save(pid, 'G')
        fixtures = {'active.example': member_fixture(UUID_A),
                    'replica.example': member_fixture(UUID_B, channels=[{'Source_UUID': UUID_A}]),
                    'writable.example': member_fixture(UUID_C, channels=[{'Source_UUID': UUID_A}])}

        def capture(conn, **kwargs):
            return snapshot([item(name, 'ON' if conn.host == 'replica.example' else 'OFF', 'boolean')
                             for name in ('read_only', 'super_read_only')])

        with self.clients(fixtures), mock.patch.object(param_compare, 'capture', side_effect=capture):
            report = self.read('parameters')
        self.assertTrue(all(row['status'] == 'different' for row in report['parameters']['rows']))

    def test_group_parameters_alias_inherits_canonical_async_source_role(self):
        self.save('router', 'G', port=6446)
        self.save('replica', 'G')
        fixtures = {'active.example': member_fixture(UUID_A),
                    'router.example': member_fixture(UUID_A),
                    'replica.example': member_fixture(UUID_B, channels=[{'Source_UUID': UUID_A}])}

        def capture(conn, **kwargs):
            return snapshot([item(name, 'ON' if conn.host == 'replica.example' else 'OFF', 'boolean')
                             for name in ('read_only', 'super_read_only')])

        with self.clients(fixtures), mock.patch.object(param_compare, 'capture', side_effect=capture):
            report = self.read('parameters')
        self.assertEqual(report['primary_profile_id'], 'active')
        members = {member['profile_id']: member for member in report['members']}
        self.assertEqual(members['router']['duplicate_of'], 'active')
        self.assertEqual(report['parameters']['compared'], ['active', 'replica', 'router'])
        for row in report['parameters']['rows']:
            self.assertEqual(row['status'], 'per_server', row)
            self.assertEqual(row['reason'], "Set by the member's role")

    def test_group_parameters_require_observed_active_cluster_for_offsets(self):
        self.save('peer', 'G')
        for mode, expected in (('galera', 'per_server'), ('non-primary', 'different'),
                               ('standalone-mariadb', 'different'), ('multi-gr', 'per_server'),
                               ('offline-gr', 'different')):
            with self.subTest(mode=mode):
                maria = mode in ('galera', 'non-primary', 'standalone-mariadb')
                fixtures = {}
                for host, uuid, offset in (('active.example', UUID_A, '1'), ('peer.example', UUID_B, '2')):
                    fixture = member_fixture(uuid, engine='mariadb' if maria else 'mysql',
                                             role=None if maria else 'PRIMARY')
                    fixture['variables']['auto_increment_offset'] = offset
                    if maria:
                        fixture['variables'].update(wsrep_on='OFF' if mode == 'standalone-mariadb' else 'ON',
                            wsrep_auto_increment_control='ON', wsrep_provider='none' if mode == 'standalone-mariadb' else '/lib/galera.so')
                        fixture['status'] = {'wsrep_local_state_comment': 'Synced',
                            'wsrep_cluster_status': 'non-Primary' if mode == 'non-primary' else 'Primary'}
                    else:
                        fixture['variables']['group_replication_single_primary_mode'] = 'OFF'
                        fixture['state'] = 'OFFLINE' if mode == 'offline-gr' else 'ONLINE'
                    fixtures[host] = fixture

                def capture(conn, **kwargs):
                    variables = fixtures[conn.host]['variables']
                    return snapshot([item(name, value, 'boolean' if name in (
                        'wsrep_on', 'wsrep_auto_increment_control', 'group_replication_single_primary_mode') else
                        'integer' if name == 'auto_increment_offset' else 'string')
                        for name, value in variables.items()], engine='mariadb' if maria else 'mysql')

                with self.clients(fixtures), mock.patch.object(param_compare, 'capture', side_effect=capture):
                    report = self.read('parameters')
                rows = {row['name']: row for row in report['parameters']['rows']}
                self.assertEqual(rows['auto_increment_offset']['status'], expected)

    def test_live_mysql_chain_finds_root_and_beats_stale_stored_source(self):
        self.save('middle', 'G')
        self.save('leaf', 'G')
        self.store.record_topology_identity('middle', {'server_uuid': UUID_B})
        self.store.record_topology_identity('active', {'server_uuid': UUID_A, 'source_uuids': [UUID_B]})
        fixtures = {
            'active.example': member_fixture(UUID_A, gtid=UUID_A + ':1-10'),
            'middle.example': member_fixture(UUID_B, gtid=UUID_A + ':1-8', channels=[{
                'Channel_Name': '', 'Source_UUID': UUID_A, 'Source_Host': 'untrusted-alias.example',
                'Replica_IO_Running': 'Yes', 'Replica_SQL_Running': 'Yes'}]),
            'leaf.example': member_fixture(UUID_C, gtid=UUID_A + ':1-5', channels=[{
                'Channel_Name': '', 'Source_UUID': UUID_B, 'Replica_IO_Running': 'Yes', 'Replica_SQL_Running': 'Yes'}]),
        }
        with self.clients(fixtures) as observed:
            report = self.read()
        self.assertEqual(report['primary_profile_id'], 'active')
        members = {entry['profile_id']: entry for entry in report['members']}
        self.assertEqual(members['leaf']['behind']['value'], 5)
        self.assertEqual(members['middle']['role'], 'replica')
        self.assertEqual(members['leaf']['channels'][0]['source_uuid'], UUID_B)
        self.assertEqual(set(observed.logins), set(fixtures), 'reported channel hosts are never opened')

    def test_mariadb_channels_find_root_by_unique_server_id_without_uuid(self):
        self.save('middle', 'G')
        self.save('leaf', 'G')
        source = member_fixture(engine='mariadb', gtid='0-11-10')
        middle = member_fixture(engine='mariadb', gtid='0-11-8', channels=[{
            'Connection_name': 'upstream', 'Master_Host': 'source.example', 'Master_Port': '3306',
            'Master_Server_Id': '11', 'Master_UUID': UUID_A, 'Master_User': 'secret-user',
            'Master_Password': 'secret-password', 'Slave_IO_Running': 'Yes',
            'Slave_SQL_Running': 'Yes', 'Seconds_Behind_Master': '2'}])
        leaf = member_fixture(engine='mariadb', gtid='0-11-5', channels=[{
            'Connection_name': 'downstream', 'Master_Server_Id': '12',
            'Slave_IO_Running': 'Yes', 'Slave_SQL_Running': 'Yes'}])
        for fixture, server_id in ((source, '11'), (middle, '12'), (leaf, '13')):
            fixture['variables']['server_id'] = server_id
        with self.clients({'active.example': source, 'middle.example': middle, 'leaf.example': leaf}) as observed:
            report = self.read()
        self.assertEqual(report['primary_profile_id'], 'active')
        members = {entry['profile_id']: entry for entry in report['members']}
        self.assertEqual(members['middle']['channels'], [{'name': 'upstream', 'source_host': 'source.example',
            'source_port': 3306, 'source_uuid': None, 'source_server_id': 11,
            'io_running': 'Yes', 'sql_running': 'Yes', 'seconds_behind': 2}])
        self.assertEqual(members['leaf']['behind']['value'], 5)
        self.assertEqual(sum('SHOW ALL SLAVES STATUS' in sql for sql in self.statements(observed)), 3)
        for value in ('secret-user', 'secret-password'):
            self.assertNotIn(value, json.dumps(report))
        validate(report, json.loads((ROOT / 'docs/schemas/topology-group-1.0.json').read_text(encoding='utf-8')))

    def test_duplicate_mariadb_server_ids_cannot_identify_a_source(self):
        self.save('duplicate', 'G')
        self.save('replica', 'G')
        source = member_fixture(engine='mariadb')
        duplicate = member_fixture(engine='mariadb')
        replica = member_fixture(engine='mariadb', channels=[{'Master_Server_Id': '1'}])
        replica['variables']['server_id'] = '2'
        with self.clients({'active.example': source, 'duplicate.example': duplicate, 'replica.example': replica}):
            report = self.read()
        self.assertIsNone(report['primary_profile_id'])
        self.assertTrue(all(m['behind_reason'] == 'No primary or source member was identified.' for m in report['members']))

    def test_mariadb_source_id_matches_only_read_mariadb_members(self):
        self.save('replica', 'G')
        source = member_fixture(UUID_A)
        source['variables']['server_id'] = '11'
        replica = member_fixture(engine='mariadb', channels=[{'Master_Server_Id': '11'}])
        replica['variables']['server_id'] = '12'
        fixtures = {'active.example': source, 'replica.example': replica}
        with self.clients(fixtures):
            report = self.read()
        self.assertIsNone(report['primary_profile_id'])
        member = next(m for m in report['members'] if m['profile_id'] == 'replica')
        self.assertEqual(member['behind_reason'], 'No primary or source member was identified.')
        self.save('duplicate', 'G')
        duplicate = member_fixture(engine='mariadb')
        duplicate['variables']['server_id'] = '11'
        fixtures['duplicate.example'] = duplicate
        with self.clients(fixtures):
            report = self.read()
        self.assertEqual(report['primary_profile_id'], 'duplicate')
        member = next(m for m in report['members'] if m['profile_id'] == 'replica')
        self.assertIsNotNone(member['behind'])

    def test_mysql_channel_server_id_and_host_do_not_substitute_for_uuid(self):
        self.save('replica', 'G')
        replica = member_fixture(UUID_B, channels=[{'Source_Server_Id': '1', 'Source_Host': 'active.example'}])
        replica['variables']['server_id'] = '2'
        with self.clients({'active.example': member_fixture(), 'replica.example': replica}):
            report = self.read()
        self.assertIsNone(report['primary_profile_id'])

    def test_multiple_live_roots_refuse_stored_fallback(self):
        for name in ('other', 'replica', 'other-replica'):
            self.save(name, 'G')
        self.store.record_topology_identity('active', {'server_uuid': UUID_A})
        self.store.record_topology_identity('replica', {'server_uuid': UUID_C, 'source_uuids': [UUID_A]})
        fixtures = {'active.example': member_fixture(UUID_A), 'other.example': member_fixture(UUID_B),
                    'replica.example': member_fixture(UUID_C, channels=[{'Source_UUID': UUID_A}]),
                    'other-replica.example': member_fixture(UUID_D, channels=[{'Source_UUID': UUID_B}])}
        with self.clients(fixtures):
            report = self.read()
        self.assertIsNone(report['primary_profile_id'])
        self.assertTrue(all(m['behind_reason'] == 'This group has more than one primary or source.' for m in report['members']))

    def test_multiple_online_primaries_refuse_stored_fallback(self):
        self.save('other', 'G')
        self.store.record_topology_identity('active', {'server_uuid': UUID_A})
        self.store.record_topology_identity('other', {'server_uuid': UUID_B, 'source_uuids': [UUID_A]})
        with self.clients({'active.example': member_fixture(UUID_A, role='PRIMARY'),
                           'other.example': member_fixture(UUID_B, role='PRIMARY')}):
            report = self.read()
        self.assertIsNone(report['primary_profile_id'])
        self.assertTrue(all(m['role'] == 'primary' for m in report['members']))
        self.assertTrue(all(m['behind_reason'] == 'This group has more than one primary or source.' for m in report['members']))

    def test_multi_primary_mode_refuses_a_single_visible_online_primary(self):
        fixture = member_fixture(UUID_A, role='PRIMARY')
        fixture['variables']['group_replication_single_primary_mode'] = 'OFF'
        with self.clients({'active.example': fixture}):
            report = self.read()
        self.assertEqual(report['summary']['read'], 1)
        self.assertEqual(report['members'][0]['role'], 'primary')
        self.assertEqual(report['members'][0]['state'], 'ONLINE')
        self.assertIsNone(report['primary_profile_id'])
        self.assertEqual(report['members'][0]['behind_reason'], 'This group has more than one primary or source.')

    def test_only_online_primary_wins_over_async_root(self):
        for name in ('offline', 'source', 'replica'):
            self.save(name, 'G')
        fixtures = {'active.example': member_fixture(UUID_A, role='PRIMARY'),
                    'offline.example': member_fixture(UUID_B, role='PRIMARY', state='OFFLINE'),
                    'source.example': member_fixture(UUID_C),
                    'replica.example': member_fixture(UUID_D, channels=[{'Source_UUID': UUID_C}])}
        with self.clients(fixtures):
            report = self.read()
        self.assertEqual(report['primary_profile_id'], 'active')

    def test_mysql57_primary_and_secondary_roles_are_lowercase(self):
        self.save('secondary', 'G')
        fixtures = {'active.example': member_fixture(UUID_A, has_gr=True),
                    'secondary.example': member_fixture(UUID_B, has_gr=True)}
        for fixture in fixtures.values():
            fixture['variables'].update(version='5.7.44', group_replication_single_primary_mode='ON')
            fixture['status'] = {'group_replication_primary_member': UUID_A}
        with self.clients(fixtures) as observed:
            report = self.read()
        self.assertEqual({m['profile_id']: m['role'] for m in report['members']},
                         {'active': 'primary', 'secondary': 'secondary'})
        self.assertEqual(report['primary_profile_id'], 'active')
        for sql in self.statements(observed):
            if 'FROM performance_schema.replication_group_members' in sql:
                self.assertIn('WHERE MEMBER_ID = @@server_uuid', sql)
                self.assertNotIn(UUID_A, sql)
                self.assertNotIn(UUID_B, sql)

    def test_mysql57_nil_primary_uuid_leaves_role_unknown(self):
        fixture = member_fixture(UUID_A, has_gr=True, state='ONLINE',
            status={'group_replication_primary_member': '00000000-0000-0000-0000-000000000000'})
        fixture['variables'].update(version='5.7.44', group_replication_single_primary_mode='ON')
        with self.clients({'active.example': fixture}):
            report = self.read()
        member = report['members'][0]
        self.assertEqual(member['outcome'], 'read', report)
        self.assertEqual(member['state'], 'ONLINE')
        self.assertIsNone(member['role'])
        self.assertIsNone(report['primary_profile_id'])

    def test_standalone_and_source_roles_use_lowercase_vocabulary(self):
        self.save('source', 'G')
        fixtures = {'active.example': member_fixture(),
                    'source.example': member_fixture(UUID_B, replicas=[['3', 'replica.example', '3306']])}
        with self.clients(fixtures):
            report = self.read()
        self.assertEqual({m['profile_id']: m['role'] for m in report['members']},
                         {'active': 'standalone', 'source': 'source'})
        self.assertTrue(all(m['channels'] == [] for m in report['members']))

    def assert_map_schema(self, report):
        self.assertEqual(report['section'], 'map')
        self.assertEqual(set(report['map']), {'nodes', 'edges', 'clusters', 'notes'})
        schema = json.loads((ROOT / 'docs/schemas/topology-group-1.0.json').read_text(encoding='utf-8'))
        validate(report, schema)
        encoded = json.dumps(report)
        for secret in ('test-password', 'secret_sql', 'secret_table', 'secret_rule_sql',
                       'secret_upstream_user', 'secret_channel_user', 'secret_channel_password'):
            self.assertNotIn(secret, encoded)
        node_ids = {node['id'] for node in report['map']['nodes']}
        for edge in report['map']['edges']:
            self.assertIn(edge['from'], node_ids)
            self.assertIn(edge['to'], node_ids)
        return {node['id']: node for node in report['map']['nodes']}

    def gr_map_fixture(self, uuid, role, rows, group=UUID_D, **fields):
        fixture = member_fixture(uuid, role=role, gr_members=rows, **fields)
        fixture['variables'].update(group_replication_group_name=group,
                                    group_replication_single_primary_mode='ON')
        return fixture

    def test_duplicate_mysql_uuid_notes_keep_all_saved_members_and_map_nodes(self):
        self.save('router-one', 'G', name='Router one')
        self.save('router-two', 'G', name='Router two')
        self.save('distinct', 'G')
        fixtures = {'active.example': member_fixture(UUID_A),
                    'router-one.example': member_fixture(UUID_A.upper()),
                    'router-two.example': member_fixture(UUID_A),
                    'distinct.example': member_fixture(UUID_B)}
        expected_order = [profile['id'] for profile in self.store.list('profile')]
        schema = json.loads((ROOT / 'docs/schemas/topology-group-1.0.json').read_text(encoding='utf-8'))
        for section in ('members', 'map'):
            with self.subTest(section=section), self.clients(fixtures) as observed:
                report = self.read(section)
            validate(report, schema)
            self.assertEqual([member['profile_id'] for member in report['members']], expected_order)
            self.assertEqual(report['summary']['read'], 4)
            aliases = [member for member in report['members'] if member['server_uuid'] == UUID_A]
            self.assertEqual(len(aliases), 3)
            # Saved profiles are returned newest first. With no corroborating
            # hostname, the oldest saved endpoint is canonical.
            first = next(member for member in aliases if member['profile_id'] == 'active')
            self.assertIsNone(first['duplicate_of'])
            self.assertNotIn('same server_uuid', first['message'])
            distinct = next(member for member in report['members'] if member['profile_id'] == 'distinct')
            self.assertIsNone(distinct['duplicate_of'])
            self.assertNotIn('same server_uuid', distinct['message'])
            for duplicate in (member for member in aliases if member is not first):
                note = ('%s and %s reach the same server (same server_uuid); '
                        'one may be a router or proxy port.' %
                        (first['profile_name'], duplicate['profile_name']))
                self.assertEqual(duplicate['duplicate_of'], first['profile_id'])
                self.assertIn(note, duplicate['message'])
                if section == 'map':
                    self.assertIn({'profile_id': duplicate['profile_id'], 'message': note}, report['map']['notes'])
            self.assertEqual(len(observed.connections), 4, 'duplicate reporting must not open extra connections')
            if section == 'map':
                nodes = self.assert_map_schema(report)
                self.assertEqual(set(nodes), set(expected_order), 'each saved endpoint keeps its own map node')
                self.assertEqual(len(report['map']['nodes']), 4)
                for member in report['members']:
                    node = nodes[member['profile_id']]
                    self.assertEqual(node['duplicate_of'], member['duplicate_of'])
                    self.assertEqual(node['kind'], 'member')
                duplicate_notes = [note for note in report['map']['notes'] if 'same server_uuid' in note['message']]
                self.assertEqual(len(duplicate_notes), 2)

    def test_live_router_alias_keeps_one_primary_and_no_spurious_cluster_peer(self):
        self.save('router', 'G', name='cluster-ic-router-rw', port=6446)
        self.save('secondary', 'G')
        # The newer router appears first in the store, but its forwarded port
        # is contrary evidence for choosing the direct server's canonical ID.
        rows = [[UUID_A, 'mf-ic-1', 3306, 'ONLINE', 'PRIMARY'],
                [UUID_B, 'mf-ic-2', 3306, 'ONLINE', 'SECONDARY']]
        fixtures = {'active.example': self.gr_map_fixture(UUID_A, 'PRIMARY', rows, gtid=UUID_A + ':1-10'),
                    'router.example': self.gr_map_fixture(UUID_A, 'PRIMARY', rows, gtid=UUID_A + ':1-10'),
                    'secondary.example': self.gr_map_fixture(UUID_B, 'SECONDARY', rows, gtid=UUID_A + ':1-8')}
        with self.clients(fixtures):
            report = self.read('map')
        nodes = self.assert_map_schema(report)
        self.assertEqual(report['primary_profile_id'], 'active')
        self.assertEqual(set(nodes), {'active', 'router', 'secondary'})
        self.assertEqual(nodes['router']['duplicate_of'], 'active')
        self.assertIsNone(nodes['router']['cluster_id'])
        self.assertEqual(report['map']['clusters'][0]['members'], ['active', 'secondary'])
        self.assertEqual(report['map']['edges'], [{'from': 'router', 'to': 'active', 'kind': 'alias',
            'evidence': 'server_uuid', 'verified': False, 'state': None, 'label': 'same server (alias)'}])
        members = {row['profile_id']: row for row in report['members']}
        self.assertEqual(members['secondary']['behind']['value'], 2)
        self.assertEqual(members['router']['behind']['value'], 0)

    def test_replica_alias_is_not_a_separate_verified_replication_target(self):
        self.save('router', 'G', port=6446)
        self.save('replica', 'G')
        self.save('active', 'G', type='development')
        source = member_fixture(UUID_A, replicas=[{'Replica_UUID': UUID_B, 'Server_id': '2',
                                                 'Host': 'replica.example', 'Port': '3306'}])
        replica = member_fixture(UUID_B, channels=[{'Source_UUID': UUID_A, 'Source_Host': 'active.example',
                                                   'Source_Port': '3306'}])
        with self.clients({'active.example': source, 'replica.example': replica, 'router.example': replica}):
            report = self.read('map')
        self.assert_map_schema(report)
        edges = report['map']['edges']
        self.assertEqual(len(edges), 2)
        self.assertEqual({(e['from'], e['to'], e['kind'], e['verified']) for e in edges},
                         {('active', 'replica', 'replication', True), ('router', 'replica', 'alias', False)})

    def test_alias_canonical_prefers_reported_hostname_or_report_host(self):
        self.save('direct', 'G')
        for field in ('hostname', 'report_host'):
            with self.subTest(field=field):
                fixture = member_fixture(UUID_A)
                fixture['variables'][field] = 'direct.example'
                with self.clients({'active.example': fixture, 'direct.example': copy.deepcopy(fixture)}):
                    report = self.read()
                members = {row['profile_id']: row for row in report['members']}
                self.assertIsNone(members['direct']['duplicate_of'])
                self.assertEqual(members['active']['duplicate_of'], 'direct')

    def test_alias_canonical_never_uses_a_known_contrary_port(self):
        # The oldest endpoint and its hostname both look preferable, but the
        # SQL server reports a different port, so the direct endpoint wins.
        self.save('active', 'G', type='development', port=6446)
        self.save('direct', 'G')
        fixture = member_fixture(UUID_A)
        fixture['variables']['hostname'] = 'active.example'
        with self.clients({'active.example': fixture, 'direct.example': copy.deepcopy(fixture)}):
            report = self.read()
        members = {row['profile_id']: row for row in report['members']}
        self.assertEqual(members['active']['duplicate_of'], 'direct')
        self.assertIsNone(members['direct']['duplicate_of'])

    def test_forwarded_aliases_without_matching_port_pick_oldest_canonical(self):
        # Neither saved endpoint uses the server's reported port (3306), as
        # happens behind Docker or NAT port forwarding. The oldest saved
        # profile wins even when the newer endpoint has an earlier profile ID.
        self.save('z-node', 'G', name='cluster-ic-1', port=34301)
        time.sleep(0.002)
        self.save('active', 'G', type='development', name='cluster-ic-router-rw', port=34306)
        rows = [[UUID_A, 'cluster-ic-1', 3306, 'ONLINE', 'PRIMARY']]
        fixture = self.gr_map_fixture(UUID_A, 'PRIMARY', rows, gtid=UUID_A + ':1-10')
        expected_note = (
            'cluster-ic-1 and cluster-ic-router-rw reach the same server (same server_uuid); '
            'one may be a router or proxy port. '
            "No saved connection uses the server's reported port 3306; "
            'cluster-ic-1 was kept as the server and the others are treated as aliases.'
        )
        for section in ('members', 'map'):
            with self.subTest(section=section), self.clients(
                    {'z-node.example': copy.deepcopy(fixture), 'active.example': copy.deepcopy(fixture)}):
                report = self.read(section)
            self.assertEqual(report['summary']['read'], 2)
            self.assertEqual(report['primary_profile_id'], 'z-node')
            members = {member['profile_id']: member for member in report['members']}
            self.assertIsNone(members['z-node']['duplicate_of'])
            self.assertEqual(members['active']['duplicate_of'], 'z-node')
            self.assertNotIn('same server_uuid', members['z-node']['message'])
            self.assertIn(expected_note, members['active']['message'])
            for member in members.values():
                self.assertIsNone(member['behind_reason'])
                self.assertEqual(member['behind']['value'], 0)
            if section == 'map':
                nodes = self.assert_map_schema(report)
                self.assertIsNone(nodes['z-node']['duplicate_of'])
                self.assertEqual(nodes['active']['duplicate_of'], 'z-node')
                self.assertIn({'profile_id': 'active', 'message': expected_note}, report['map']['notes'])
                self.assertFalse(any(note['profile_id'] == 'z-node' and 'same server_uuid' in note['message']
                                     for note in report['map']['notes']))

    def test_forwarded_router_alias_yields_to_direct_member_even_when_saved_earlier(self):
        self.save('active', 'G', type='development', name='cluster-ic-router-rw', port=34306)
        time.sleep(0.002)
        self.save('z-node', 'G', name='cluster-ic-1', port=34301)
        rows = [[UUID_A, 'cluster-ic-1', 3306, 'ONLINE', 'PRIMARY']]
        router = self.gr_map_fixture(UUID_A, 'PRIMARY', rows, gtid=UUID_A + ':1-10', routed=True)
        direct = self.gr_map_fixture(UUID_A, 'PRIMARY', rows, gtid=UUID_A + ':1-10', routed=False)
        schema = json.loads((ROOT / 'docs/schemas/topology-group-1.0.json').read_text(encoding='utf-8'))
        for section in ('members', 'map'):
            with self.subTest(section=section), self.clients(
                    {'active.example': copy.deepcopy(router), 'z-node.example': copy.deepcopy(direct)}) as observed:
                report = self.read(section)
            validate(report, schema)
            self.assertEqual(report['summary']['read'], 2)
            self.assertEqual(report['primary_profile_id'], 'z-node')
            members = {member['profile_id']: member for member in report['members']}
            self.assertIs(members['z-node']['routed'], False)
            self.assertIs(members['active']['routed'], True)
            self.assertIsNone(members['z-node']['duplicate_of'])
            self.assertEqual(members['active']['duplicate_of'], 'z-node')
            for host in ('active.example', 'z-node.example'):
                queries = [sql for sql in self.statements(observed, host)
                           if 'performance_schema.session_connect_attrs' in sql]
                self.assertEqual(len(queries), 1)
                self.assertIn('/*myflames:topology*/ ', queries[0])
                self.assertIn("ATTR_NAME IN ('_client_ip','_client_socket')", queries[0])
            if section == 'map':
                nodes = self.assert_map_schema(report)
                self.assertIsNone(nodes['z-node']['duplicate_of'])
                self.assertEqual(nodes['active']['duplicate_of'], 'z-node')
                self.assertEqual(report['map']['edges'], [{'from': 'active', 'to': 'z-node', 'kind': 'alias',
                    'evidence': 'server_uuid', 'verified': False, 'state': None, 'label': 'same server (alias)'}])

    def test_router_alias_with_equal_save_times_has_same_canonical_member_on_every_section(self):
        # Equal timestamps leave the router's earlier profile ID as the
        # fallback winner unless every section reads the session attributes.
        with mock.patch('myflames.workspace_store._now', return_value='2026-11-22T12:00:00+00:00'):
            self.save('active', 'G', type='development', port=34306)
            self.save('z-node', 'G', port=34301)
        profiles = self.store.list('profile')
        self.assertEqual(len({profile['updated_at'] for profile in profiles}), 1)
        self.assertEqual(min(profile['id'] for profile in profiles), 'active')
        router = member_fixture(UUID_A, routed=True)
        direct = member_fixture(UUID_A, routed=False)
        schema = json.loads((ROOT / 'docs/schemas/topology-group-1.0.json').read_text(encoding='utf-8'))
        for section in ('members', 'map', 'parameters', 'accounts', 'plugins'):
            with self.subTest(section=section):
                with self.clients(
                        {'active.example': copy.deepcopy(router), 'z-node.example': copy.deepcopy(direct)}) as observed, \
                        mock.patch.object(param_compare, 'capture', return_value=snapshot([item('max_connections', '151')])), \
                        mock.patch.object(account_compare, 'capture', return_value=side([account('app')])):
                    report = self.read(section)
                validate(report, schema)
                self.assertEqual(report['summary']['read'], 2, report)
                members = {member['profile_id']: member for member in report['members']}
                self.assertIsNone(members['z-node']['duplicate_of'])
                self.assertEqual(members['active']['duplicate_of'], 'z-node')
                self.assertIs(members['z-node']['routed'], False)
                self.assertIs(members['active']['routed'], True)
                for host in ('active.example', 'z-node.example'):
                    queries = [sql for sql in self.statements(observed, host) if 'session_connect_attrs' in sql]
                    self.assertEqual(len(queries), 1, (section, host))

    def test_routed_preference_applies_in_direct_and_port_match_steps(self):
        for case, port, host_var in (('direct', 3306, 'active.example'), ('matched', 3306, 'other-host')):
            with self.subTest(case=case):
                self.save('active', 'G', type='development', port=port)
                time.sleep(0.002)
                self.save('z-direct', 'G', port=port)
                router = member_fixture(UUID_A, routed=True)
                router['variables']['hostname'] = host_var
                direct = member_fixture(UUID_A, routed=False)
                direct['variables']['hostname'] = 'z-direct.example' if case == 'direct' else host_var
                with self.clients({'active.example': router, 'z-direct.example': direct}):
                    report = self.read('members')
                members = {member['profile_id']: member for member in report['members']}
                self.assertIsNone(members['z-direct']['duplicate_of'])
                self.assertEqual(members['active']['duplicate_of'], 'z-direct')

    def test_router_attribute_read_failure_stays_optional_and_uses_previous_fallback(self):
        self.save('active', 'G', type='development', name='cluster-ic-router-rw', port=34306)
        time.sleep(0.002)
        self.save('z-node', 'G', name='cluster-ic-1', port=34301)
        rows = [[UUID_A, 'cluster-ic-1', 3306, 'ONLINE', 'PRIMARY']]
        err = "ERROR 1142 (42000): SELECT command denied to user 'dba'@'%' for table 'session_connect_attrs'"
        router = self.gr_map_fixture(UUID_A, 'PRIMARY', rows, gtid=UUID_A + ':1-10', attrs_error=err)
        direct = self.gr_map_fixture(UUID_A, 'PRIMARY', rows, gtid=UUID_A + ':1-10', attrs_error=err)
        schema = json.loads((ROOT / 'docs/schemas/topology-group-1.0.json').read_text(encoding='utf-8'))
        for section in ('members', 'map', 'parameters', 'accounts', 'plugins'):
            with self.subTest(section=section), self.clients(
                    {'active.example': copy.deepcopy(router), 'z-node.example': copy.deepcopy(direct)}), \
                    mock.patch.object(param_compare, 'capture', return_value=snapshot([item('max_connections', '151')])), \
                    mock.patch.object(account_compare, 'capture', return_value=side([account('app')])):
                report = self.read(section)
            validate(report, schema)
            self.assertEqual(report['summary']['read'], 2, report)
            self.assertEqual(report['summary']['failed'], 0)
            if section in ('members', 'map', 'parameters'):
                self.assertEqual(report['primary_profile_id'], 'active')
            members = {member['profile_id']: member for member in report['members']}
            self.assertIsNone(members['active']['routed'])
            self.assertIsNone(members['z-node']['routed'])
            self.assertIsNone(members['active']['duplicate_of'])
            self.assertEqual(members['z-node']['duplicate_of'], 'active')

    def test_only_mysql_database_members_run_session_connect_attrs_query_on_every_section(self):
        self.save('proxy', 'G', port=6032)
        self.save('readyset', 'G', port=3307)
        self.save('maria', 'G')
        proxy = member_fixture(proxy='proxysql')
        proxy['variables'].update(version='3.0.11', version_comment='ProxySQL Admin')
        readyset = member_fixture(proxy='readyset')
        readyset['variables']['version_comment'] = 'Readyset'
        fixtures = {'active.example': member_fixture(UUID_A),
                    'maria.example': member_fixture(engine='mariadb'),
                    'proxy.example': proxy,
                    'readyset.example': readyset}
        for section in ('members', 'map', 'parameters', 'accounts', 'plugins'):
            with self.subTest(section=section), self.clients(fixtures) as observed, \
                    mock.patch.object(param_compare, 'capture', return_value=snapshot([item('max_connections', '151')])), \
                    mock.patch.object(account_compare, 'capture', return_value=side([account('app')])):
                report = self.read(section)
            self.assertEqual(report['summary']['read'], 4, report)
            for host in ('proxy.example', 'readyset.example', 'maria.example'):
                self.assertFalse(any('session_connect_attrs' in sql for sql in self.statements(observed, host)),
                                 (section, host))
            queries = [sql for sql in self.statements(observed, 'active.example') if 'session_connect_attrs' in sql]
            self.assertEqual(len(queries), 1, section)
            members = {member['profile_id']: member for member in report['members']}
            self.assertIs(members['active']['routed'], False)
            for pid in ('proxy', 'readyset', 'maria'):
                self.assertIsNone(members[pid]['routed'])

    def test_live_mariadb_dns_and_ipv6_rows_make_one_id_match_edge(self):
        self.save('replica', 'G')
        source = member_fixture(engine='mariadb', gtid='0-108-100', replicas=[{
            'Server_id': '109', 'Host': 'fda0:f59a:f4f:c::b', 'Port': '3306'}])
        source['variables'].update(server_id='108', version='11.8.9-MariaDB')
        replica = member_fixture(engine='mariadb', gtid='0-108-95', channels=[{
            'Master_Host': 'myflames-ga-mariadb-11-8', 'Master_Port': '3306', 'Master_Server_Id': '108',
            'Slave_IO_Running': 'Yes', 'Slave_SQL_Running': 'No', 'Seconds_Behind_Master': None}])
        replica['variables'].update(server_id='109', version='11.8.9-MariaDB')
        fixtures = {'active.example': source, 'replica.example': replica}
        for corroborated in (False, True):
            if corroborated:
                source['replicas'][0]['Host'] = 'replica.example'
                replica['channels'][0]['Master_Host'] = 'active.example'
            with self.subTest(corroborated=corroborated), self.clients(fixtures):
                report = self.read('map')
            self.assertEqual(set(self.assert_map_schema(report)), {'active', 'replica'})
            self.assertEqual(report['primary_profile_id'], 'active')
            self.assertEqual(len(report['map']['edges']), 1)
            edge = report['map']['edges'][0]
            self.assertEqual((edge['from'], edge['to'], edge['evidence'], edge['state']),
                             ('active', 'replica', 'server_id', 'stopped'))
            self.assertIs(edge['verified'], corroborated)
            member = next(m for m in report['members'] if m['profile_id'] == 'replica')
            self.assertEqual(member['behind']['value'], 5)
            self.assertEqual('estimate; source matched by server ID' in member['behind']['message'], not corroborated)

    def test_stored_source_cannot_override_external_live_channel_or_cycle(self):
        self.save('replica', 'G')
        self.store.record_topology_identity('active', {'server_uuid': UUID_A})
        self.store.record_topology_identity('replica', {'server_uuid': UUID_B, 'source_uuids': [UUID_A]})
        for cyclic in (False, True):
            source = member_fixture(UUID_A, channels=[{'Source_UUID': UUID_B}] if cyclic else [])
            replica = member_fixture(UUID_B, channels=[{'Source_UUID': UUID_A if cyclic else UUID_C,
                                                       'Source_Host': 'external.example', 'Source_Port': '3306'}])
            with self.subTest(cyclic=cyclic), self.clients({'active.example': source, 'replica.example': replica}):
                report = self.read()
            self.assertIsNone(report['primary_profile_id'])
            self.assertTrue(all(m['behind'] is None for m in report['members']))
            self.assertTrue(all(m['behind_reason'] == 'The stored source no longer matches the live replication channels.'
                                for m in report['members']))

    def test_mariadb_internal_port_corroborates_behind_through_forwarded_endpoint(self):
        self.save('active', 'G', port=34306, type='development')
        self.save('replica', 'G')
        source = member_fixture(engine='mariadb', gtid='0-108-100')
        source['variables'].update(server_id='108', hostname='db-source', port='3306')
        replica = member_fixture(engine='mariadb', gtid='0-108-95', channels=[{
            'Master_Host': 'db-source', 'Master_Port': '3306', 'Master_Server_Id': '108'}])
        replica['variables'].update(server_id='109', hostname='db-replica')
        with self.clients({'active.example': source, 'replica.example': replica}):
            report = self.read('map')
        self.assert_map_schema(report)
        self.assertTrue(report['map']['edges'][0]['verified'])
        behind = next(m['behind'] for m in report['members'] if m['profile_id'] == 'replica')
        self.assertEqual(behind['value'], 5)
        self.assertNotIn('source matched by server ID', behind['message'])

    def test_behind_excludes_unrelated_database_proxy_and_cache_members(self):
        for pid in ('replica', 'unrelated', 'proxy', 'cache'):
            self.save(pid, 'G')
        proxy = member_fixture(proxy='proxysql')
        proxy['variables'].update(version='3.0.11-797-g7c91137', version_comment='ProxySQL Admin')
        cache = member_fixture()
        cache['variables']['version_comment'] = 'Readyset'
        fixtures = {'active.example': member_fixture(UUID_A, gtid=UUID_A + ':1-20'),
                    'replica.example': member_fixture(UUID_B, gtid=UUID_A + ':1-18', channels=[{'Source_UUID': UUID_A}]),
                    'unrelated.example': member_fixture(UUID_C, gtid=UUID_A + ':1-2'),
                    'proxy.example': proxy, 'cache.example': cache}
        with self.clients(fixtures):
            report = self.read()
        members = {m['profile_id']: m for m in report['members']}
        self.assertEqual(members['replica']['behind']['value'], 2)
        self.assertIsNone(members['unrelated']['behind'])
        self.assertEqual(members['unrelated']['behind_reason'],
                         'No replication relationship to the primary or source was found.')
        for pid in ('proxy', 'cache'):
            self.assertIsNone(members[pid]['behind'])
            self.assertEqual(members[pid]['behind_reason'], 'Not a database server.')

    def test_behind_requires_same_active_gr_group(self):
        for pid in ('same', 'other', 'offline'):
            self.save(pid, 'G')
        fixtures = {'active.example': member_fixture(UUID_A, role='PRIMARY', gtid=UUID_A + ':1-20'),
                    'same.example': member_fixture(UUID_B, role='SECONDARY', gtid=UUID_A + ':1-18'),
                    'other.example': member_fixture(UUID_C, role='SECONDARY', gtid=UUID_A + ':1-2'),
                    'offline.example': member_fixture(UUID_D, role='SECONDARY', state='OFFLINE', gtid=UUID_A + ':1-1')}
        fixtures['other.example']['variables']['group_replication_group_name'] = UUID_C
        with self.clients(fixtures):
            report = self.read()
        members = {m['profile_id']: m for m in report['members']}
        self.assertEqual(members['same']['behind']['value'], 2)
        for pid in ('other', 'offline'):
            self.assertIsNone(members[pid]['behind'])
            self.assertEqual(members[pid]['behind_reason'],
                             'No replication relationship to the primary or source was found.')

    def test_failed_map_read_with_an_observed_uuid_is_not_duplicate_evidence(self):
        self.save('healthy', 'G')
        # The first identity read succeeds, but the later map probe reaches
        # another node. The failed member discards all observed facts.
        fixtures = {'active.example': member_fixture(UUID_A, probe_uuid=UUID_C),
                    'healthy.example': member_fixture(UUID_A)}
        with self.clients(fixtures):
            report = self.read('map')
        nodes = self.assert_map_schema(report)
        members = {member['profile_id']: member for member in report['members']}
        self.assertEqual(members['active']['outcome'], 'failed')
        self.assertIsNone(members['active']['server_uuid'])
        self.assertEqual(members['healthy']['outcome'], 'read')
        for member in members.values():
            self.assertIsNone(member['duplicate_of'])
            self.assertNotIn('same server_uuid', member['message'])
            self.assertIsNone(nodes[member['profile_id']]['duplicate_of'])
        self.assertFalse(any('same server_uuid' in note['message'] for note in report['map']['notes']))

    def test_nil_and_missing_mysql_uuid_never_mark_duplicate_connections(self):
        for pid in ('nil-peer', 'missing-one', 'missing-two'):
            self.save(pid, 'G')
        nil = '00000000-0000-0000-0000-000000000000'
        fixtures = {'active.example': member_fixture(nil), 'nil-peer.example': member_fixture(nil),
                    'missing-one.example': member_fixture(), 'missing-two.example': member_fixture()}
        for host in ('missing-one.example', 'missing-two.example'):
            fixtures[host]['variables'].pop('server_uuid')
        schema = json.loads((ROOT / 'docs/schemas/topology-group-1.0.json').read_text(encoding='utf-8'))
        for section in ('members', 'map'):
            with self.subTest(section=section), self.clients(fixtures):
                report = self.read(section)
            validate(report, schema)
            for member in report['members']:
                self.assertIsNone(member['server_uuid'])
                self.assertIsNone(member['duplicate_of'])
                self.assertNotIn('same server_uuid', member['message'])
            if section == 'map':
                nodes = self.assert_map_schema(report)
                self.assertEqual(len(nodes), 4)
                self.assertTrue(all(node['duplicate_of'] is None for node in nodes.values()))
                self.assertFalse(any('same server_uuid' in note['message'] for note in report['map']['notes']))

    def test_map_innodb_cluster_two_saved_members_one_peer_keeps_member_facts(self):
        self.save('second', 'G')
        self.save('outsider', 'outside')
        rows = [[UUID_A, 'active.example', 3306, 'ONLINE', 'PRIMARY'],
                [UUID_B, 'second.example', 3306, 'ONLINE', 'SECONDARY'],
                [UUID_C, 'outsider.example', 3306, 'ONLINE', 'SECONDARY']]
        metadata = [{'cluster_id': 'cluster-one', 'cluster_name': 'Production',
                     'primary_mode': 'pm', 'group_name': UUID_D}]
        fixtures = {
            'active.example': self.gr_map_fixture(UUID_A, 'PRIMARY', rows, innodb_cluster=True,
                                                  cluster_metadata=metadata, gtid=UUID_A + ':1-10'),
            'second.example': self.gr_map_fixture(UUID_B, 'SECONDARY', rows, innodb_cluster=True,
                                                  cluster_metadata=metadata, gtid=UUID_A + ':1-7', queue=2)}
        with self.clients(fixtures) as observed:
            status, report = TopologyEndpointTests.post(self, {'connection': self.connection, 'section': 'map'})
        self.assertEqual(status, 200, report)
        nodes = self.assert_map_schema(report)
        self.assertEqual(set(observed.logins), {'active.example', 'second.example'})
        self.assertEqual(len(observed.connections), 2)
        self.assertEqual(set(nodes), {'active', 'second', 'peer:' + UUID_C})
        self.assertEqual(nodes['peer:' + UUID_C]['seen_by'], ['active', 'second'])
        self.assertEqual(nodes['peer:' + UUID_C]['reason'], 'Not in this group')
        self.assertIsNone(nodes['peer:' + UUID_C]['profile_id'])
        self.assertEqual(nodes['peer:' + UUID_C]['label'], 'outsider.example:3306')
        self.assertEqual(nodes['active']['label'], 'active')
        self.assertEqual(nodes['second']['role'], 'secondary')
        self.assertEqual(nodes['second']['state'], 'ONLINE')
        self.assertEqual(nodes['second']['outcome'], 'read')
        self.assertEqual(len(report['map']['clusters']), 1)
        cluster = report['map']['clusters'][0]
        self.assertEqual(cluster['kind'], 'innodb_cluster')
        self.assertEqual(cluster['name'], 'Production')
        self.assertEqual(set(cluster['members']), set(nodes))
        self.assertEqual(set(cluster['seen_from']), {'active', 'second'})
        self.assertEqual(nodes['active']['cluster_id'], cluster['id'])
        self.assertEqual(nodes['second']['cluster_id'], cluster['id'])
        second = next(row for row in report['members'] if row['profile_id'] == 'second')
        self.assertEqual(second['lag']['value'], 2)
        self.assertEqual(second['behind']['value'], 3)

    def test_map_async_chain_edges_follow_source_to_replica(self):
        self.save('middle', 'G')
        self.save('leaf', 'G')
        def channel(uuid, host):
            return {'Source_UUID': uuid, 'Source_Host': host, 'Source_Port': '3306',
                    'Replica_IO_Running': 'Yes', 'Replica_SQL_Running': 'Yes',
                    'Source_User': 'secret_channel_user', 'Source_Password': 'secret_channel_password',
                    'Seconds_Behind_Source': '4'}
        fixtures = {
            'active.example': member_fixture(UUID_A, replicas=[{'Replica_UUID': UUID_B, 'Server_id': '2',
                                                               'Host': 'middle.example', 'Port': '3306'}]),
            'middle.example': member_fixture(UUID_B, channels=[channel(UUID_A, 'active.example')],
                                              replicas=[{'Replica_UUID': UUID_C, 'Server_id': '3',
                                                         'Host': 'leaf.example', 'Port': '3306'}]),
            'leaf.example': member_fixture(UUID_C, channels=[channel(UUID_B, 'middle.example')])}
        with self.clients(fixtures):
            report = self.read('map')
        nodes = self.assert_map_schema(report)
        self.assertEqual(set(nodes), {'active', 'middle', 'leaf'})
        edges = report['map']['edges']
        self.assertEqual({(edge['from'], edge['to']) for edge in edges}, {('active', 'middle'), ('middle', 'leaf')})
        self.assertEqual(len(edges), 2, 'source and replica observations of one link are deduplicated')
        self.assertTrue(all(edge['kind'] == 'replication' and edge['verified'] and
                            edge['evidence'] == 'server_uuid' for edge in edges))
        self.assertEqual(next(m for m in report['members'] if m['profile_id'] == 'leaf')['lag']['value'], 4)

    def test_map_stronger_replica_uuid_evidence_preserves_channel_running_state(self):
        self.save('source', 'G')
        # Profiles are read newest first. Refresh this existing profile so
        # its address-only channel observation precedes the source's UUID.
        self.save('active', 'G', type='development')
        replica = member_fixture(UUID_A, channels=[{
            'Source_Host': 'source.example', 'Source_Port': '3306',
            'Replica_IO_Running': 'Yes', 'Replica_SQL_Running': 'Yes'}])
        source = member_fixture(UUID_B, replicas=[{
            'Replica_UUID': UUID_A, 'Server_id': '1', 'Host': 'active.example', 'Port': '3306'}])
        with self.clients({'active.example': replica, 'source.example': source}):
            report = self.read('map')
        self.assert_map_schema(report)
        self.assertEqual([member['profile_id'] for member in report['members']], ['active', 'source'])
        self.assertEqual(len(report['map']['edges']), 1)
        edge = report['map']['edges'][0]
        self.assertEqual((edge['from'], edge['to']), ('source', 'active'))
        self.assertEqual(edge['evidence'], 'server_uuid')
        self.assertIs(edge['verified'], True)
        self.assertEqual(edge['state'], 'running')

    def test_map_mariadb_replication_uses_server_id_unique_among_read_mariadb_members(self):
        self.save('replica', 'G')
        self.save('mysql', 'G')
        source = member_fixture(engine='mariadb')
        source['variables']['server_id'] = '11'
        replica = member_fixture(engine='mariadb', channels=[{
            'Master_Host': 'active.example', 'Master_Port': '3306', 'Master_Server_Id': '11',
            'Slave_IO_Running': 'Yes', 'Slave_SQL_Running': 'Yes'}])
        replica['variables']['server_id'] = '12'
        mysql = member_fixture(UUID_C)
        mysql['variables']['server_id'] = '11'
        with self.clients({'active.example': source, 'replica.example': replica, 'mysql.example': mysql}):
            report = self.read('map')
        self.assert_map_schema(report)
        edges = report['map']['edges']
        self.assertEqual(len(edges), 1)
        self.assertEqual((edges[0]['from'], edges[0]['to']), ('active', 'replica'))
        self.assertEqual(edges[0]['evidence'], 'server_id')
        self.assertIs(edges[0]['verified'], True)

    def test_map_duplicate_mariadb_server_id_cannot_verify_a_source(self):
        self.save('duplicate', 'G')
        self.save('replica', 'G')
        source = member_fixture(engine='mariadb')
        source['variables']['server_id'] = '11'
        replica = member_fixture(engine='mariadb', channels=[{
            'Master_Host': 'active.example', 'Master_Port': '3306', 'Master_Server_Id': '11'}])
        replica['variables']['server_id'] = '12'
        with self.clients({'active.example': source, 'duplicate.example': copy.deepcopy(source),
                           'replica.example': replica}):
            report = self.read('map')
        self.assert_map_schema(report)
        self.assertFalse(any(edge['verified'] for edge in report['map']['edges']))

    def test_map_mariadb_contrary_server_id_blocks_an_address_only_match(self):
        self.save('replica', 'G')
        replica = member_fixture(engine='mariadb', channels=[{
            'Master_Host': 'active.example', 'Master_Port': '3306', 'Master_Server_Id': '11',
            'Slave_IO_Running': 'Yes', 'Slave_SQL_Running': 'Yes'}])
        replica['variables']['server_id'] = '12'
        for engine in ('mariadb', 'mysql'):
            with self.subTest(candidate_engine=engine):
                source = member_fixture(engine=engine)
                source['variables']['server_id'] = '99'
                with self.clients({'active.example': source, 'replica.example': replica}):
                    report = self.read('map')
                nodes = self.assert_map_schema(report)
                self.assertEqual(len(report['map']['edges']), 1)
                edge = report['map']['edges'][0]
                self.assertEqual((edge['from'], edge['to']), ('peer:active.example:3306', 'replica'))
                self.assertEqual(nodes[edge['from']]['kind'], 'peer')
                self.assertIsNone(edge['evidence'])
                self.assertIs(edge['verified'], False)
                self.assertFalse(any(e['from'] == 'active' for e in report['map']['edges']))

    def test_map_galera_three_nodes_groups_only_self_attested_members(self):
        self.save('second', 'G')
        rows = [[UUID_A, 'node-a', 'active.example:3306'],
                [UUID_B, 'node-b', 'second.example:3306'],
                [UUID_C, 'node-c', 'peer.example:3306']]
        fixtures = {}
        for host, uuid in (('active.example', UUID_A), ('second.example', UUID_B)):
            fixture = member_fixture(engine='mariadb', wsrep_members=rows, status={
                'wsrep_local_state_comment': 'Synced', 'wsrep_local_recv_queue': '0',
                'wsrep_gcomm_uuid': uuid, 'wsrep_cluster_state_uuid': UUID_D, 'wsrep_cluster_status': 'Primary'})
            fixture['variables'].update(wsrep_on='ON', wsrep_cluster_name='Galera production')
            fixtures[host] = fixture
        with self.clients(fixtures):
            report = self.read('map')
        nodes = self.assert_map_schema(report)
        self.assertEqual(set(nodes), {'active', 'second', 'peer:' + UUID_C})
        self.assertEqual(len(report['map']['clusters']), 1)
        cluster = report['map']['clusters'][0]
        self.assertEqual(cluster['kind'], 'galera')
        self.assertEqual(cluster['name'], 'Galera production')
        self.assertEqual(set(cluster['members']), set(nodes))
        self.assertEqual(set(cluster['seen_from']), {'active', 'second'})
        self.assertEqual(nodes['active']['cluster_id'], cluster['id'])
        self.assertEqual(nodes['second']['cluster_id'], cluster['id'])

    def test_map_pxc_uses_own_gcomm_uuid_and_pxc_membership_view(self):
        fixture = member_fixture(UUID_A, pxc_members=[
            ['node-a', UUID_B, 'SYNCED', '0', '0'],
            ['node-peer', UUID_C, 'SYNCED', '1', '0']],
            status={'wsrep_gcomm_uuid': UUID_B, 'wsrep_cluster_state_uuid': UUID_D,
                    'wsrep_cluster_status': 'Primary', 'wsrep_local_state_comment': 'Synced', 'wsrep_local_recv_queue': '0'})
        fixture['variables'].update(version_comment='Percona XtraDB Cluster (GPL)',
                                    wsrep_provider='/usr/lib/libgalera_smm.so', wsrep_cluster_name='PXC production')
        with self.clients({'active.example': fixture}) as observed:
            report = self.read('map')
        nodes = self.assert_map_schema(report)
        self.assertEqual(nodes['active']['server_uuid'], UUID_A)
        cluster = report['map']['clusters'][0]
        self.assertEqual(cluster['kind'], 'pxc')
        self.assertEqual(set(cluster['members']), {'active', 'peer:' + UUID_C})
        self.assertEqual(nodes['peer:' + UUID_C]['label'], 'node-peer')
        self.assertIsNone(nodes['peer:' + UUID_C]['port'])
        self.assertIn('performance_schema.pxc_cluster_view', '\n'.join(self.statements(observed)))

    def test_map_mysql8001_cluster_membership_falls_back_without_member_role_column(self):
        rows = [[UUID_A, 'active.example', 3306, 'ONLINE', 'PRIMARY'],
                [UUID_B, 'peer.example', 3306, 'ONLINE', 'SECONDARY']]
        fixture = self.gr_map_fixture(UUID_A, 'PRIMARY', rows, no_gr_role=True)
        fixture['variables']['version'] = '8.0.1'
        fixture['status'] = {'group_replication_primary_member': UUID_A}
        with self.clients({'active.example': fixture}) as observed:
            report = self.read('map')
        nodes = self.assert_map_schema(report)
        self.assertEqual(nodes['active']['role'], 'primary')
        self.assertEqual(nodes['active']['outcome'], 'read')
        self.assertEqual(set(report['map']['clusters'][0]['members']), {'active', 'peer:' + UUID_B})
        self.assertNotIn('COUNT_TRANSACTIONS_REMOTE_IN_APPLIER_QUEUE', '\n'.join(self.statements(observed)))

    def test_map_same_cluster_name_different_uuid_stays_two_clusters(self):
        self.save('second', 'G')
        fixtures = {}
        for host, node_uuid, group_uuid in (('active.example', UUID_A, UUID_C),
                                            ('second.example', UUID_B, UUID_D)):
            fixture = member_fixture(engine='mariadb', wsrep_members=[[node_uuid, 'node', host + ':3306']],
                                     status={'wsrep_gcomm_uuid': node_uuid,
                                             'wsrep_cluster_state_uuid': group_uuid, 'wsrep_cluster_status': 'Primary'})
            fixture['variables'].update(wsrep_on='ON', wsrep_cluster_name='same display name')
            fixtures[host] = fixture
        with self.clients(fixtures):
            report = self.read('map')
        nodes = self.assert_map_schema(report)
        self.assertEqual(len(report['map']['clusters']), 2)
        self.assertNotEqual(nodes['active']['cluster_id'], nodes['second']['cluster_id'])
        self.assertEqual({c['name'] for c in report['map']['clusters']}, {'same display name'})

    def test_map_gr_membership_requires_own_online_or_recovering_row(self):
        for state, attested in (('ONLINE', True), ('RECOVERING', True), ('OFFLINE', False), ('ERROR', False)):
            with self.subTest(state=state):
                rows = [[UUID_A, 'active.example', 3306, state, 'SECONDARY']]
                fixture = self.gr_map_fixture(UUID_A, 'SECONDARY', rows, state=state)
                with self.clients({'active.example': fixture}):
                    report = self.read('map')
                nodes = self.assert_map_schema(report)
                self.assertEqual(nodes['active']['cluster_id'] is not None, attested)
                self.assertEqual('active' in report['map']['clusters'][0]['members'], attested)

    def test_map_galera_membership_requires_primary_component_and_non_nil_identity(self):
        nil = '00000000-0000-0000-0000-000000000000'
        cases = [('Primary', UUID_A, UUID_D, True), ('non-Primary', UUID_A, UUID_D, False),
                 ('Disconnected', UUID_A, UUID_D, False), (None, UUID_A, UUID_D, False),
                 ('Primary', nil, UUID_D, False), ('Primary', UUID_A, nil, False)]
        for state, own_uuid, group_uuid, attested in cases:
            with self.subTest(state=state, own_uuid=own_uuid, group_uuid=group_uuid):
                fixture = member_fixture(engine='mariadb',
                    wsrep_members=[[own_uuid, 'node', 'active.example:3306']],
                    status={'wsrep_cluster_status': state, 'wsrep_gcomm_uuid': own_uuid,
                            'wsrep_cluster_state_uuid': group_uuid})
                fixture['variables'].update(wsrep_on='ON', wsrep_cluster_name='test-cluster')
                with self.clients({'active.example': fixture}):
                    report = self.read('map')
                nodes = self.assert_map_schema(report)
                self.assertEqual(nodes['active']['cluster_id'] is not None, attested)
                if group_uuid == nil:
                    self.assertEqual(report['map']['clusters'], [])

    def test_nil_mysql_uuid_cannot_attest_group_membership(self):
        nil = '00000000-0000-0000-0000-000000000000'
        fixture = self.gr_map_fixture(nil, 'PRIMARY', [[nil, 'active.example', 3306, 'ONLINE', 'PRIMARY']])
        with self.clients({'active.example': fixture}):
            report = self.read('map')
        nodes = self.assert_map_schema(report)
        self.assertIsNone(nodes['active']['cluster_id'])
        self.assertIsNone(report['members'][0]['server_uuid'])

    def test_map_mariadb_server_id_without_corroboration_is_explicitly_unverified(self):
        self.save('other', 'G')
        self.save('replica', 'G')
        source = member_fixture(engine='mariadb')
        source['variables']['server_id'] = '11'
        other = member_fixture(engine='mariadb')
        other['variables']['server_id'] = '13'
        for host in ('outside.example', 'other.example'):
            with self.subTest(host=host):
                replica = member_fixture(engine='mariadb', channels=[{
                    'Master_Host': host, 'Master_Port': '3306', 'Master_Server_Id': '11'}])
                replica['variables']['server_id'] = '12'
                with self.clients({'active.example': source, 'other.example': other, 'replica.example': replica}):
                    report = self.read('map')
                self.assert_map_schema(report)
                edge = report['map']['edges'][0]
                self.assertEqual((edge['from'], edge['to']), ('active', 'replica'))
                self.assertFalse(edge['verified'])
                self.assertEqual(edge['evidence'], 'server_id')
                self.assertEqual(edge['label'], 'server ID match, not verified')

    def test_map_pxc_node_names_preserve_incoming_address_membership(self):
        fixture = member_fixture(UUID_A, pxc_members=[
            ['node-a', UUID_B, 'SYNCED', '0', '0'],
            ['node-peer', UUID_C, 'SYNCED', '1', '0']],
            wsrep_members=[[UUID_B, 'node-a', 'active.example:3306'],
                           [UUID_C, 'node-peer', 'peer.example:4406']],
            status={'wsrep_gcomm_uuid': UUID_B, 'wsrep_cluster_state_uuid': UUID_D,
                    'wsrep_cluster_status': 'Primary', 'wsrep_local_state_comment': 'Synced'})
        fixture['variables'].update(version_comment='Percona XtraDB Cluster (GPL)',
                                    wsrep_provider='/usr/lib/libgalera_smm.so', wsrep_cluster_name='PXC production')
        with self.clients({'active.example': fixture}):
            report = self.read('map')
        nodes = self.assert_map_schema(report)
        peer = nodes['peer:' + UUID_C]
        self.assertEqual((peer['host'], peer['port'], peer['label']),
                         ('peer.example', 4406, 'peer.example:4406'))
        self.assertEqual(set(report['map']['clusters'][0]['members']), {'active', 'peer:' + UUID_C})

    def test_map_cluster_listing_does_not_attest_another_saved_members_membership(self):
        self.save('second', 'G')
        rows = [[UUID_A, 'active.example', 3306, 'ONLINE', 'PRIMARY'],
                [UUID_B, 'second.example', 3306, 'ONLINE', 'SECONDARY']]
        with self.clients({'active.example': self.gr_map_fixture(UUID_A, 'PRIMARY', rows),
                           'second.example': member_fixture(UUID_B)}):
            report = self.read('map')
        nodes = self.assert_map_schema(report)
        self.assertEqual(set(nodes), {'active', 'second'})
        self.assertIsNone(nodes['second']['cluster_id'])
        self.assertEqual(report['map']['clusters'][0]['members'], ['active'])

    def test_map_proxysql_routes_by_address_are_unverified(self):
        self.save('a', 'G')
        self.save('b', 'G')
        proxy = member_fixture(proxy='proxysql', servers=[
            {'hostgroup_id': '10', 'hostname': 'a.example', 'port': '3306', 'status': 'ONLINE',
             'comment': 'secret_rule_sql'},
            {'hostgroup_id': '20', 'hostname': 'b.example', 'port': '3306', 'status': 'SHUNNED'}])
        proxy['variables'].update(version='3.0.11', version_comment='ProxySQL Admin')
        with self.clients({'active.example': proxy, 'a.example': member_fixture(UUID_B),
                           'b.example': member_fixture(UUID_C)}):
            report = self.read('map')
        nodes = self.assert_map_schema(report)
        self.assertEqual(set(nodes), {'active', 'a', 'b'})
        edges = {edge['to']: edge for edge in report['map']['edges']}
        self.assertEqual(set(edges), {'a', 'b'})
        self.assertTrue(all(edge['from'] == 'active' and edge['kind'] == 'proxy_route' and
                            edge['verified'] is False and edge['evidence'] == 'endpoint'
                            for edge in edges.values()))
        self.assertEqual((edges['a']['label'], edges['a']['state']), ('hostgroup 10', 'ONLINE'))
        self.assertNotIn('reason', edges['a'])
        self.assertEqual(edges['b']['state'], 'SHUNNED')
        self.assertEqual(edges['b']['label'], 'hostgroup 20')
        self.assertIn('No ONLINE member', edges['b']['reason'])

    def test_map_proxysql_offline_runtime_rows_keep_reason_and_ignore_online_pool(self):
        self.save('a', 'G')
        self.save('b', 'G')
        for status in ('OFFLINE_SOFT', 'OFFLINE_HARD'):
            with self.subTest(status=status):
                proxy = member_fixture(proxy='proxysql', servers=[
                    {'hostgroup_id': '10', 'hostname': 'a.example', 'port': '3306', 'status': status},
                    {'hostgroup_id': '10', 'hostname': 'b.example', 'port': '3306', 'status': 'ONLINE'},
                ], pool=[
                    {'hostgroup': '10', 'srv_host': 'a.example', 'srv_port': '3306', 'status': 'ONLINE'},
                    {'hostgroup': '10', 'srv_host': 'b.example', 'srv_port': '3306', 'status': 'ONLINE'},
                ])
                proxy['variables'].update(version='3.0.11', version_comment='ProxySQL Admin')
                with self.clients({'active.example': proxy, 'a.example': member_fixture(UUID_B),
                                   'b.example': member_fixture(UUID_C)}):
                    report = self.read('map')
                self.assert_map_schema(report)
                edges = {edge['to']: edge for edge in report['map']['edges']}
                self.assertEqual(edges['a']['state'], status)
                self.assertEqual(edges['a']['label'], 'hostgroup 10')
                self.assertNotIn('No ONLINE member', edges['a']['reason'])
                self.assertRegex(edges['a']['reason'].lower(), 'no new traffic|out of traffic|stops using')
                self.assertEqual((edges['b']['state'], edges['b']['label']), ('ONLINE', 'hostgroup 10'))
                self.assertNotIn('reason', edges['b'])

    def test_map_proxysql_monitor_offline_hostgroup_is_offline_even_with_online_row(self):
        self.save('a', 'G')
        self.save('b', 'G')
        for table in ('runtime_mysql_group_replication_hostgroups', 'runtime_mysql_galera_hostgroups'):
            with self.subTest(table=table):
                proxy = member_fixture(proxy='proxysql', servers=[
                    {'hostgroup_id': '40', 'hostname': 'a.example', 'port': '3306', 'status': 'ONLINE'},
                    {'hostgroup_id': '10', 'hostname': 'b.example', 'port': '3306', 'status': 'ONLINE'},
                ])
                proxy[table] = [{'offline_hostgroup': '40', 'active': '1'}]
                proxy['variables'].update(version='3.0.11', version_comment='ProxySQL Admin')
                with self.clients({'active.example': proxy, 'a.example': member_fixture(UUID_B),
                                   'b.example': member_fixture(UUID_C)}) as observed:
                    report = self.read('map')
                self.assert_map_schema(report)
                self.assertTrue(any('FROM ' + table in sql for sql in self.statements(observed, 'active.example')))
                edges = {edge['to']: edge for edge in report['map']['edges']}
                self.assertEqual(edges['a']['state'], 'offline')
                self.assertEqual(edges['a']['label'], 'hostgroup 40')
                self.assertIn('offline hostgroup', edges['a']['reason'])
                self.assertEqual((edges['b']['state'], edges['b']['label']), ('ONLINE', 'hostgroup 10'))

    def test_map_proxysql_pool_lag_reason_and_hostgroup_health_are_scoped_per_route(self):
        self.save('a', 'G')
        self.save('b', 'G')
        proxy = member_fixture(proxy='proxysql', servers=[
            {'hostgroup_id': '10', 'hostname': 'a.example', 'port': '3306', 'status': 'ONLINE'},
            {'hostgroup_id': '20', 'hostname': 'a.example', 'port': '3306', 'status': 'SHUNNED'},
            {'hostgroup_id': '20', 'hostname': 'b.example', 'port': '3306', 'status': 'OFFLINE_HARD'},
        ], pool=[
            {'hostgroup': '10', 'srv_host': 'a.example', 'srv_port': '3306', 'status': 'ONLINE'},
            {'hostgroup': '20', 'srv_host': 'a.example', 'srv_port': '3306', 'status': 'SHUNNED_REPLICATION_LAG'},
            {'hostgroup': '20', 'srv_host': 'b.example', 'srv_port': '3306', 'status': 'ONLINE'},
        ])
        proxy['variables'].update(version='3.0.11', version_comment='ProxySQL Admin')
        with self.clients({'active.example': proxy, 'a.example': member_fixture(UUID_B),
                           'b.example': member_fixture(UUID_C)}) as observed:
            report = self.read('map')
        self.assert_map_schema(report)
        self.assertTrue(any('FROM stats_mysql_connection_pool' in sql
                            for sql in self.statements(observed, 'active.example')))
        edges = report['map']['edges']
        self.assertEqual(len(edges), 3)
        online = next(edge for edge in edges if edge['label'] == 'hostgroup 10')
        lagged = next(edge for edge in edges if edge['to'] == 'a' and edge['label'] == 'hostgroup 20')
        offline = next(edge for edge in edges if edge['to'] == 'b')
        self.assertEqual(online['state'], 'ONLINE')
        self.assertNotIn('reason', online)
        self.assertEqual(lagged['state'], 'SHUNNED_REPLICATION_LAG')
        self.assertIn('Shunned for replication lag', lagged['reason'])
        self.assertIn('No ONLINE member', lagged['reason'])
        self.assertEqual(offline['state'], 'OFFLINE_HARD')
        self.assertIn('No ONLINE member', offline['reason'])

    def test_map_proxysql_pool_online_recovers_runtime_shunned_backend(self):
        self.save('a', 'G')
        proxy = member_fixture(proxy='proxysql', servers=[
            {'hostgroup_id': '10', 'hostname': 'a.example', 'port': '3306', 'status': 'SHUNNED'},
        ], pool=[
            {'hostgroup': '10', 'srv_host': 'a.example', 'srv_port': '3306', 'status': 'ONLINE'},
        ])
        proxy['variables'].update(version='3.0.11', version_comment='ProxySQL Admin')
        with self.clients({'active.example': proxy, 'a.example': member_fixture(UUID_B)}):
            report = self.read('map')
        self.assert_map_schema(report)
        self.assertEqual(len(report['map']['edges']), 1)
        edge = report['map']['edges'][0]
        self.assertEqual((edge['to'], edge['state'], edge['label']), ('a', 'ONLINE', 'hostgroup 10'))

    def test_map_proxysql_same_hostgroup_keeps_distinct_backend_endpoints_separate(self):
        self.save('a', 'G', host='192.0.2.10')
        db = member_fixture(UUID_B)
        db['variables']['hostname'] = 'db.internal'
        rows = (
            {'hostgroup_id': '10', 'hostname': '192.0.2.10', 'port': '3306', 'status': 'ONLINE'},
            {'hostgroup_id': '10', 'hostname': 'db.internal', 'port': '3306', 'status': 'OFFLINE_HARD'},
        )
        for servers in (rows, tuple(reversed(rows))):
            with self.subTest(first=servers[0]['hostname']):
                proxy = member_fixture(proxy='proxysql', servers=list(servers))
                proxy['variables'].update(version='3.0.11', version_comment='ProxySQL Admin')
                with self.clients({'active.example': proxy, '192.0.2.10': db}):
                    report = self.read('map')
                self.assert_map_schema(report)
                edges = report['map']['edges']
                self.assertEqual(len(edges), 2)
                self.assertTrue(all(edge['from'] == 'active' and edge['to'] == 'a'
                                    and edge['kind'] == 'proxy_route' and edge['label'] == 'hostgroup 10'
                                    for edge in edges))
                by_state = {edge['state']: edge for edge in edges}
                self.assertEqual(set(by_state), {'ONLINE', 'OFFLINE_HARD'})
                self.assertNotIn('reason', by_state['ONLINE'])
                self.assertIn('out of traffic', by_state['OFFLINE_HARD']['reason'])
                self.assertEqual((by_state['ONLINE']['evidence'], by_state['OFFLINE_HARD']['evidence']),
                                 ('endpoint', 'hostname_port'))

    def test_map_proxysql_address_routes_with_router_alias_and_ambiguous_members(self):
        self.save('router', 'G', port=6446)
        self.save('proxy', 'G', port=6032)
        cases = (
            ('reported_hostname_with_alias', 'db-1', 3306, UUID_A, 'active', 'hostname_port'),
            ('router_saved_endpoint', 'router.example', 6446, UUID_A, 'router', 'endpoint'),
            ('distinct_members_same_hostname', 'db-1', 3306, UUID_B, 'peer:db-1:3306', None),
        )
        for name, host, port, router_uuid, destination, evidence in cases:
            with self.subTest(case=name):
                direct = member_fixture(UUID_A)
                direct['variables']['hostname'] = 'db-1'
                router = member_fixture(router_uuid)
                router['variables']['hostname'] = 'db-1'
                proxy = member_fixture(proxy='proxysql', servers=[{
                    'hostgroup_id': '10', 'hostname': host, 'port': str(port), 'status': 'ONLINE'}])
                proxy['variables'].update(version='3.0.11', version_comment='ProxySQL Admin')
                with self.clients({'active.example': direct, 'router.example': router,
                                   'proxy.example': proxy}):
                    report = self.read('map')
                nodes = self.assert_map_schema(report)
                self.assertEqual(report['summary']['read'], 3, report)
                self.assertEqual(nodes['router']['duplicate_of'], 'active' if router_uuid == UUID_A else None)
                routes = [edge for edge in report['map']['edges'] if edge['kind'] == 'proxy_route']
                self.assertEqual(len(routes), 1)
                route = routes[0]
                self.assertEqual((route['from'], route['to']), ('proxy', destination))
                self.assertEqual(route['evidence'], evidence)
                self.assertIs(route['verified'], False)
                peers = [node for node in nodes.values() if node['kind'] == 'peer']
                if evidence is None:
                    self.assertEqual([node['id'] for node in peers], [destination])
                    self.assertIsNone(nodes[destination]['profile_id'])
                    self.assertEqual(nodes[destination]['reason'], 'Not in this group')
                else:
                    self.assertEqual(peers, [])

    def test_map_proxysql_config_fallback_omits_edge_state(self):
        self.save('a', 'G')
        self.save('b', 'G')
        proxy = member_fixture(
            proxy='proxysql',
            error_on='SELECT hostgroup_id, hostname, port, status, weight, comment FROM runtime_mysql_servers',
            query_error='ERROR 1045 (28000): ProxySQL Admin Error: no such table: runtime_mysql_servers',
            servers=[
                {'hostgroup_id': '10', 'hostname': 'a.example', 'port': '3306', 'status': 'ONLINE'},
                {'hostgroup_id': '20', 'hostname': 'b.example', 'port': '3306', 'status': 'SHUNNED_REPLICATION_LAG'},
            ], pool=[
                {'hostgroup': '20', 'srv_host': 'b.example', 'srv_port': '3306', 'status': 'SHUNNED_REPLICATION_LAG'},
            ], runtime_mysql_group_replication_hostgroups=[{'offline_hostgroup': '10', 'active': '1'}])
        proxy['variables'].update(version='3.0.11', version_comment='ProxySQL Admin')
        with self.clients({'active.example': proxy, 'a.example': member_fixture(UUID_B),
                           'b.example': member_fixture(UUID_C)}) as observed:
            report = self.read('map')
        self.assert_map_schema(report)
        sqls = self.statements(observed, 'active.example')
        self.assertTrue(any('FROM runtime_mysql_servers' in sql for sql in sqls))
        self.assertTrue(any('FROM mysql_servers' in sql for sql in sqls))
        edges = {edge['to']: edge for edge in report['map']['edges']}
        self.assertEqual(set(edges), {'a', 'b'})
        self.assertEqual((edges['a']['label'], edges['a']['state']), ('hostgroup 10', None))
        self.assertEqual((edges['b']['label'], edges['b']['state']), ('hostgroup 20', None))

    def test_map_proxysql_runtime_keeps_shunned_replication_lag_and_online(self):
        self.save('a', 'G')
        self.save('b', 'G')
        proxy = member_fixture(proxy='proxysql', servers=[
            {'hostgroup_id': '10', 'hostname': 'a.example', 'port': '3306', 'status': 'ONLINE'},
            {'hostgroup_id': '20', 'hostname': 'b.example', 'port': '3306', 'status': 'SHUNNED_REPLICATION_LAG'}])
        proxy['variables'].update(version='3.0.11', version_comment='ProxySQL Admin')
        with self.clients({'active.example': proxy, 'a.example': member_fixture(UUID_B),
                           'b.example': member_fixture(UUID_C)}) as observed:
            report = self.read('map')
        self.assert_map_schema(report)
        sqls = self.statements(observed, 'active.example')
        self.assertTrue(any('FROM runtime_mysql_servers' in sql for sql in sqls))
        self.assertFalse(any('FROM mysql_servers' in sql for sql in sqls))
        edges = {edge['to']: edge for edge in report['map']['edges']}
        self.assertEqual(set(edges), {'a', 'b'})
        self.assertEqual((edges['a']['label'], edges['a']['state']), ('hostgroup 10', 'ONLINE'))
        self.assertEqual(edges['b']['state'], 'SHUNNED_REPLICATION_LAG')
        self.assertEqual(edges['b']['label'], 'hostgroup 20')
        self.assertIn('Shunned for replication lag', edges['b']['reason'])
        self.assertIn('No ONLINE member', edges['b']['reason'])

    def test_map_proxysql_runtime_keeps_shunned_aws_bgd_and_config_drops_it(self):
        self.save('a', 'G')
        self.save('b', 'G')
        proxy = member_fixture(proxy='proxysql', servers=[
            {'hostgroup_id': '10', 'hostname': 'a.example', 'port': '3306', 'status': 'ONLINE'},
            {'hostgroup_id': '20', 'hostname': 'b.example', 'port': '3306', 'status': 'SHUNNED_AWS_BGD'}])
        proxy['variables'].update(version='3.0.11', version_comment='ProxySQL Admin')
        with self.clients({'active.example': proxy, 'a.example': member_fixture(UUID_B),
                           'b.example': member_fixture(UUID_C)}):
            report = self.read('map')
        self.assert_map_schema(report)
        edges = {edge['to']: edge for edge in report['map']['edges']}
        self.assertEqual((edges['a']['label'], edges['a']['state']), ('hostgroup 10', 'ONLINE'))
        self.assertEqual(edges['b']['state'], 'SHUNNED_AWS_BGD')
        self.assertEqual(edges['b']['label'], 'hostgroup 20')
        self.assertIn('No ONLINE member', edges['b']['reason'])

        proxy_cfg = member_fixture(
            proxy='proxysql',
            error_on='SELECT hostgroup_id, hostname, port, status, weight, comment FROM runtime_mysql_servers',
            query_error='ERROR 1045 (28000): ProxySQL Admin Error: no such table: runtime_mysql_servers',
            servers=[{'hostgroup_id': '20', 'hostname': 'b.example', 'port': '3306', 'status': 'SHUNNED_AWS_BGD'}])
        proxy_cfg['variables'].update(version='3.0.11', version_comment='ProxySQL Admin')
        with self.clients({'active.example': proxy_cfg, 'a.example': member_fixture(UUID_B),
                           'b.example': member_fixture(UUID_C)}):
            cfg_report = self.read('map')
        self.assert_map_schema(cfg_report)
        cfg_edges = {edge['to']: edge for edge in cfg_report['map']['edges']}
        self.assertEqual((cfg_edges['b']['label'], cfg_edges['b']['state']), ('hostgroup 20', None))

    def test_map_readyset_upstream_projects_endpoints_without_user_or_sql(self):
        self.save('source', 'G')
        readyset = member_fixture(proxy='readyset', upstreams=[{
            'host': 'source.example', 'port': '3306', 'user': 'secret_upstream_user',
            'database': 'secret_table', 'health': 'up', 'purpose': 'cdc',
            'password': 'secret_channel_password'}])
        readyset['variables']['version_comment'] = 'Readyset'
        with self.clients({'active.example': readyset, 'source.example': member_fixture(UUID_B)}):
            report = self.read('map')
        self.assert_map_schema(report)
        self.assertEqual(len(report['map']['edges']), 1)
        edge = report['map']['edges'][0]
        self.assertEqual((edge['from'], edge['to'], edge['kind']), ('active', 'source', 'cache_upstream'))
        self.assertEqual(edge['evidence'], 'endpoint')
        self.assertIs(edge['verified'], False)

    def test_map_readyset_proxied_uuid_verifies_upstream_without_copying_identity(self):
        self.save('primary', 'G', host='203.0.113.10', port=3307)
        readyset = member_fixture(
            proxy='readyset',
            readyset_adapter=True,
            proxied_server_uuid=UUID_B,
            cdc_server='internal-primary:3306',
            upstreams=[{
                'host': 'internal-primary', 'port': '3306', 'user': 'secret_upstream_user',
                'database': 'secret_table', 'purpose': 'cdc, proxy', 'active_cdc': '1',
                'health': 'up', 'password': 'secret_channel_password',
            }],
        )
        readyset['variables']['version_comment'] = 'Readyset'
        primary = member_fixture(UUID_B)
        primary['variables'].update(hostname='vox1-host', port='3307')
        with self.clients({'active.example': readyset, '203.0.113.10': primary}):
            report = self.read('map')
        nodes = self.assert_map_schema(report)
        # Readyset never stores the proxied UUID as its own server_uuid or alias.
        rs_member = next(m for m in report['members'] if m['profile_id'] == 'active')
        self.assertIsNone(rs_member['server_uuid'])
        self.assertIsNone(rs_member['duplicate_of'])
        self.assertIsNone(nodes['active']['server_uuid'])
        self.assertIsNone(nodes['active']['duplicate_of'])
        self.assertEqual(set(nodes), {'active', 'primary'}, 'unmatched upstream address must not create a peer card')
        self.assertEqual(len(report['map']['edges']), 1)
        edge = report['map']['edges'][0]
        self.assertEqual((edge['from'], edge['to'], edge['kind'], edge['evidence'], edge['verified'],
                          edge['state'], edge['label']),
                         ('active', 'primary', 'cache_upstream', 'proxied_identity', True, 'up', 'Upstream'))

    def test_map_readyset_multi_upstream_labels_queries_and_preserves_other_address_evidence(self):
        self.save('primary', 'G')
        self.save('replica', 'G')
        cases = (
            ('queries_only', UUID_C, 'primary.example:3306', 'replica.example', 'replica', 'Upstream (queries)', 'primary'),
            ('queries_unmatched_host', UUID_C, 'primary.example:3306', 'internal-replica', 'replica', 'Upstream (queries)', 'primary'),
            ('cdc_matches_proxied', UUID_B, 'primary.example:3306', 'replica.example', 'primary', 'Upstream', 'replica'),
        )
        for name, proxied_uuid, cdc_server, query_host, verified_to, expected_label, addr_to in cases:
            with self.subTest(case=name):
                readyset = member_fixture(
                    proxy='readyset',
                    readyset_adapter=True,
                    proxied_server_uuid=proxied_uuid,
                    cdc_server=cdc_server,
                    upstreams=[
                        {'host': 'primary.example', 'port': '3306', 'purpose': 'cdc, proxy' if verified_to == 'primary' else 'cdc',
                         'active_cdc': '1', 'health': 'up'},
                        {'host': query_host, 'port': '3306', 'purpose': 'proxy', 'active_cdc': '0', 'health': 'up'},
                    ],
                )
                readyset['variables']['version_comment'] = 'Readyset'
                with self.clients({
                    'active.example': readyset,
                    'primary.example': member_fixture(UUID_B),
                    'replica.example': member_fixture(UUID_C),
                }):
                    report = self.read('map')
                nodes = self.assert_map_schema(report)
                self.assertEqual(set(nodes), {'active', 'primary', 'replica'})
                edges = {edge['to']: edge for edge in report['map']['edges']}
                self.assertEqual(set(edges), {'primary', 'replica'})
                self.assertEqual((edges[verified_to]['evidence'], edges[verified_to]['verified'], edges[verified_to]['label']),
                                 ('proxied_identity', True, expected_label))
                self.assertEqual((edges[addr_to]['evidence'], edges[addr_to]['verified'], edges[addr_to]['label']),
                                 ('endpoint', False, 'Upstream'))

    def test_map_readyset_registration_attributed_positively_on_mysql8_and_mysql57(self):
        rs_replica_uuid = 'ad67a025-bc44-42bf-9521-58f7dc4e4f95'
        ext_replica_uuid = 'eeeeeeee-0000-4000-8000-000000000005'
        self.save('replica', 'G')
        self.save('readyset', 'G', port=3309)
        cases = (
            ('mysql84_show_replicas', '8.4.13', None, [
                {'Server_Id': '103', 'Host': 'mf-rs-readyset', 'Port': '3307', 'Source_Id': '101', 'Replica_UUID': rs_replica_uuid},
                {'Server_Id': '102', 'Host': 'replica.example', 'Port': '3306', 'Source_Id': '101', 'Replica_UUID': UUID_B},
                {'Server_Id': '104', 'Host': 'ext-replica.example', 'Port': '3306', 'Source_Id': '101', 'Replica_UUID': ext_replica_uuid},
            ]),
            ('mysql57_show_slave_hosts', '5.7.44-48', 'SHOW REPLICAS', [
                {'Server_id': '103', 'Host': '', 'Port': '3306', 'Master_id': '101', 'Slave_UUID': rs_replica_uuid},
                {'Server_id': '102', 'Host': 'replica.example', 'Port': '3306', 'Master_id': '101', 'Slave_UUID': UUID_B},
                {'Server_id': '104', 'Host': 'ext-replica.example', 'Port': '3306', 'Master_id': '101', 'Slave_UUID': ext_replica_uuid},
            ]),
        )
        for name, version, err_on, replica_rows in cases:
            with self.subTest(case=name):
                primary = member_fixture(
                    UUID_A,
                    replicas=replica_rows,
                    dump_threads=[
                        {'pid': 10, 'uuid': UUID_B, 'host': '192.168.112.3', 'user': 'mf_repl',
                         'attrs': [('program_name', 'mysqld'), ('_client_role', 'binary_log_listener')]},
                        {'pid': 26, 'uuid': rs_replica_uuid, 'host': '192.168.112.4',
                         'user': 'secret_upstream_user', 'attrs': []},
                    ],
                    readyset_clients=[{'host': '192.168.112.4', 'user': 'secret_upstream_user'}],
                )
                primary['variables']['version'] = version
                if err_on:
                    primary['error_on'] = err_on
                    primary['query_error'] = "ERROR 1064 (42000) at line 1: You have an error in your SQL syntax near 'REPLICAS'"
                replica = member_fixture(UUID_B, channels=[{
                    'Source_UUID': UUID_A, 'Source_Host': 'active.example', 'Source_Port': '3306',
                    'Replica_IO_Running': 'Yes', 'Replica_SQL_Running': 'Yes',
                }])
                readyset = member_fixture(
                    proxy='readyset',
                    readyset_adapter=True,
                    proxied_server_uuid=UUID_A,
                    proxied_hostname='internal-primary',
                    proxied_port='3306',
                    cdc_server='internal-primary:3306',
                    upstreams=[{'host': 'internal-primary', 'port': '3306', 'purpose': 'cdc, proxy',
                                'active_cdc': '1', 'health': 'up', 'user': 'secret_upstream_user'}],
                )
                readyset['variables']['version_comment'] = 'Readyset'
                with self.clients({
                    'active.example': primary,
                    'replica.example': replica,
                    'readyset.example': readyset,
                }):
                    report = self.read('map')
                nodes = self.assert_map_schema(report)
                # Readyset registration must not produce a peer card; the real unsaved replica must stay a peer.
                self.assertEqual(set(nodes), {'active', 'replica', 'readyset', 'peer:' + ext_replica_uuid})
                rs_edge = next(e for e in report['map']['edges'] if e['kind'] == 'cache_upstream')
                self.assertEqual((rs_edge['from'], rs_edge['to'], rs_edge['evidence'], rs_edge['verified']),
                                 ('readyset', 'active', 'proxied_identity', True))
                self.assertEqual(rs_edge['reason'], 'Also registered on active as a replica (server ID 103)')
                self.assertNotIn('estimate', rs_edge['reason'])
                # No separate replication edge to Readyset or a Readyset peer.
                repl_targets = {e['to'] for e in report['map']['edges'] if e['kind'] == 'replication'}
                self.assertEqual(repl_targets, {'replica', 'peer:' + ext_replica_uuid})

    def test_map_readyset_dump_session_classes_and_no_endpoint_discriminator(self):
        rs_uuid = 'ad67a025-bc44-42bf-9521-58f7dc4e4f95'
        other_uuid = 'eeeeeeee-0000-4000-8000-000000000005'
        self.save('readyset', 'G', port=3309)

        def rs_fixture(proxied=UUID_A, host='internal-primary', user='rs_user', upstreams=None,
                       cdc_server=None, replication_status='Running', proxied_hostname=None, proxied_port=None):
            if upstreams is None:
                row = {'host': host, 'port': '3306', 'purpose': 'cdc, proxy', 'active_cdc': '1', 'health': 'up'}
                if user is not None:
                    row['user'] = user
                upstreams = [row]
            f = member_fixture(
                proxy='readyset',
                readyset_adapter=True,
                proxied_server_uuid=proxied,
                proxied_hostname=proxied_hostname,
                proxied_port=proxied_port,
                cdc_server=cdc_server if cdc_server is not None else (host + ':3306'),
                replication_status=replication_status,
                upstreams=upstreams,
            )
            f['variables']['version_comment'] = 'Readyset'
            return f

        # 1. Self-reported registration endpoint matching Readyset host+port is NEVER positive identity on its own.
        with self.subTest(case='self_reported_endpoint_not_positive_identity'):
            primary = member_fixture(UUID_A, replicas=[
                {'Server_Id': '103', 'Host': 'readyset.example', 'Port': '3309', 'Replica_UUID': rs_uuid},
            ], dump_threads=[])
            with self.clients({'active.example': primary, 'readyset.example': rs_fixture()}):
                report = self.read('map')
            nodes = self.assert_map_schema(report)
            self.assertIn('peer:' + rs_uuid, nodes)
            self.assertEqual(nodes['peer:' + rs_uuid]['label'], 'readyset.example:3309')
            edge = next(e for e in report['map']['edges'] if e['kind'] == 'cache_upstream')
            self.assertNotIn('reason', edge)

        # 2. Each dump session class: "mysqld" (all 3 spellings), "other", "unknown" (attrs_lost > 0), and "none".
        for case, attrs, lost, host, should_attr in (
            ('class_none', [], '0', 'mf-rs-readyset', True),
            ('class_mysqld_program_name', [('program_name', 'mysqld')], '0', '', False),
            ('class_mysqld_underscore_program_name', [('_program_name', 'mysqld')], '0', '', False),
            ('class_mysqld_client_role', [('_client_role', 'binary_log_listener')], '0', '', False),
            ('class_other_attrs', [('_client_name', 'debezium'), ('_os', 'Linux')], '0', '', False),
            ('class_unknown_attrs_lost_with_host', [], '2', 'mf-rs-readyset', False),
            ('class_mysqld_even_with_attrs_lost', [('program_name', 'mysqld')], '2', '', False),
        ):
            with self.subTest(case=case):
                primary = member_fixture(
                    UUID_A,
                    replicas=[{'Server_Id': '103', 'Host': host, 'Port': '3307', 'Replica_UUID': rs_uuid}],
                    dump_threads=[{'pid': 10, 'uuid': rs_uuid, 'host': 'readyset.example', 'user': 'rs_user', 'attrs': attrs}],
                    status={'Performance_schema_session_connect_attrs_lost': lost},
                )
                with self.clients({'active.example': primary, 'readyset.example': rs_fixture()}):
                    report = self.read('map')
                nodes = self.assert_map_schema(report)
                edge = next(e for e in report['map']['edges'] if e['kind'] == 'cache_upstream')
                if should_attr:
                    self.assertNotIn('peer:' + rs_uuid, nodes)
                    self.assertEqual(edge['reason'], 'Also registered on active as a replica (server ID 103)')
                else:
                    self.assertIn('peer:' + rs_uuid, nodes)
                    self.assertNotIn('reason', edge)

        # 3. attrs_lost > 0 with empty Host triggers estimate fallback (every dump session is "unknown"), never positive.
        with self.subTest(case='attrs_lost_positive_empty_host_estimate_fallback'):
            primary = member_fixture(
                UUID_A,
                replicas=[{'Server_Id': '103', 'Host': '', 'Port': '3306', 'Replica_UUID': rs_uuid}],
                dump_threads=[{'pid': 10, 'uuid': rs_uuid, 'host': 'readyset.example', 'user': 'rs_user', 'attrs': []}],
                status={'Performance_schema_session_connect_attrs_lost': '1'},
            )
            with self.clients({'active.example': primary, 'readyset.example': rs_fixture()}):
                report = self.read('map')
            nodes = self.assert_map_schema(report)
            self.assertEqual(set(nodes), {'active', 'readyset'})
            edge = next(e for e in report['map']['edges'] if e['kind'] == 'cache_upstream')
            self.assertEqual(edge['reason'], 'Also registered on active as a replica (server ID 103) (estimate)')

        # 4. mysqld replica and Readyset on the SAME host: Readyset claims only class "none"; mysqld stays a peer.
        with self.subTest(case='mysqld_replica_and_readyset_same_host'):
            primary = member_fixture(
                UUID_A,
                replicas=[
                    {'Server_Id': '102', 'Host': 'shared-host', 'Port': '3306', 'Replica_UUID': other_uuid},
                    {'Server_Id': '103', 'Host': 'shared-host', 'Port': '3307', 'Replica_UUID': rs_uuid},
                ],
                dump_threads=[
                    {'pid': 10, 'uuid': other_uuid, 'host': 'readyset.example', 'user': 'rs_user',
                     'attrs': [('program_name', 'mysqld')]},
                    {'pid': 11, 'uuid': rs_uuid, 'host': 'readyset.example', 'user': 'rs_user', 'attrs': []},
                ],
            )
            with self.clients({'active.example': primary, 'readyset.example': rs_fixture()}):
                report = self.read('map')
            nodes = self.assert_map_schema(report)
            self.assertEqual(set(nodes), {'active', 'readyset', 'peer:' + other_uuid})
            edge = next(e for e in report['map']['edges'] if e['kind'] == 'cache_upstream')
            self.assertEqual(edge['reason'], 'Also registered on active as a replica (server ID 103)')

        # 5. When Readyset exposes no upstream user, same (PROCESSLIST_HOST, USER) READYSET% attrs decide.
        for has_client_attr in (True, False):
            with self.subTest(case='no_upstream_user_readyset_attr', has_attr=has_client_attr):
                primary = member_fixture(
                    UUID_A,
                    replicas=[{'Server_Id': '103', 'Host': 'mf-rs-readyset', 'Port': '3307', 'Replica_UUID': rs_uuid}],
                    dump_threads=[{'pid': 10, 'uuid': rs_uuid, 'host': '192.168.112.4', 'user': 'rs_user', 'attrs': []}],
                    readyset_clients=[{'host': '192.168.112.4', 'user': 'rs_user'}] if has_client_attr else [],
                )
                rs_no_user = rs_fixture(user=None)
                with self.clients({'active.example': primary, 'readyset.example': rs_no_user}):
                    report = self.read('map')
                nodes = self.assert_map_schema(report)
                edge = next(e for e in report['map']['edges'] if e['kind'] == 'cache_upstream')
                if has_client_attr:
                    self.assertEqual(set(nodes), {'active', 'readyset'})
                    self.assertEqual(edge['reason'], 'Also registered on active as a replica (server ID 103)')
                else:
                    self.assertIn('peer:' + rs_uuid, nodes)
                    self.assertNotIn('reason', edge)

        # 6. Two Readysets on one source (one saved, one outside): the outside one stays a peer.
        for case, outside_host, outside_user in (
            ('distinct_hosts_outside_first', 'outside-rs.example', 'rs_user'),
            ('distinct_users', '192.168.112.9', 'outside_user'),
        ):
            with self.subTest(case='saved_and_outside_readyset_' + case):
                primary = member_fixture(
                    UUID_A,
                    replicas=[
                        {'Server_Id': '205', 'Host': 'outside-rs', 'Port': '3307', 'Replica_UUID': other_uuid},
                        {'Server_Id': '103', 'Host': 'saved-rs', 'Port': '3307', 'Replica_UUID': rs_uuid},
                    ],
                    dump_threads=[
                        {'pid': 10, 'uuid': other_uuid, 'host': outside_host, 'user': outside_user, 'attrs': []},
                        {'pid': 11, 'uuid': rs_uuid, 'host': 'readyset.example', 'user': 'rs_user', 'attrs': []},
                    ],
                )
                with self.clients({'active.example': primary, 'readyset.example': rs_fixture()}):
                    report = self.read('map')
                nodes = self.assert_map_schema(report)
                self.assertEqual(set(nodes), {'active', 'readyset', 'peer:' + other_uuid})
                edge = next(e for e in report['map']['edges'] if e['kind'] == 'cache_upstream')
                self.assertEqual(edge['reason'], 'Also registered on active as a replica (server ID 103)')

        # 7. Claim-once: a second matching registration for the same owner stays a peer.
        with self.subTest(case='claim_once_second_match_stays_peer'):
            primary = member_fixture(
                UUID_A,
                replicas=[
                    {'Server_Id': '103', 'Host': 'rs-a', 'Port': '3307', 'Replica_UUID': rs_uuid},
                    {'Server_Id': '104', 'Host': 'rs-b', 'Port': '3307', 'Replica_UUID': other_uuid},
                ],
                dump_threads=[
                    {'pid': 10, 'uuid': rs_uuid, 'host': 'readyset.example', 'user': 'rs_user', 'attrs': []},
                    {'pid': 11, 'uuid': other_uuid, 'host': 'readyset.example', 'user': 'rs_user', 'attrs': []},
                ],
            )
            with self.clients({'active.example': primary, 'readyset.example': rs_fixture()}):
                report = self.read('map')
            nodes = self.assert_map_schema(report)
            self.assertEqual(set(nodes), {'active', 'readyset', 'peer:' + other_uuid})
            edge = next(e for e in report['map']['edges'] if e['kind'] == 'cache_upstream')
            self.assertEqual(edge['reason'], 'Also registered on active as a replica (server ID 103)')

        # 8. SQL error on SELECT @@server_uuid stays optional and keeps address evidence.
        with self.subTest(case='proxied_uuid_sql_error_falls_back_to_address'):
            rs_err = rs_fixture(proxied=None, host='active.example')
            rs_err['proxied_uuid_error'] = "ERROR 1193 (HY000) at line 1: Unknown system variable 'server_uuid'"
            with self.clients({'active.example': member_fixture(UUID_A), 'readyset.example': rs_err}):
                report = self.read('map')
            nodes = self.assert_map_schema(report)
            self.assertEqual(set(nodes), {'active', 'readyset'})
            edge = next(e for e in report['map']['edges'] if e['kind'] == 'cache_upstream')
            self.assertEqual((edge['evidence'], edge['verified']), ('endpoint', False))

    def test_map_readyset_adapter_plus_admin_dedup_and_fallback_negative_conditions(self):
        rs_uuid = 'ad67a025-bc44-42bf-9521-58f7dc4e4f95'
        other_uuid = 'eeeeeeee-0000-4000-8000-000000000005'
        pfs_denied = "ERROR 1142 (42000): SELECT command denied to user 'dba'@'%' for table 'user_variables_by_thread'"
        self.save('readyset', 'G', port=3309)

        def rs_fixture(proxied=UUID_A, host='internal-primary', adapter=True, repl_status='Running',
                       upstreams=None, cdc_server=None):
            if upstreams is None:
                upstreams = [{'host': host, 'port': '3306', 'purpose': 'cdc, proxy', 'active_cdc': '1',
                              'health': 'up', 'user': 'rs_user'}]
            f = member_fixture(
                proxy='readyset',
                readyset_adapter=adapter,
                proxied_server_uuid=proxied,
                cdc_server=cdc_server if cdc_server is not None else (host + ':3306'),
                replication_status=repl_status,
                upstreams=upstreams,
            )
            f['variables']['version_comment'] = 'Readyset'
            return f

        # Positive estimate fallback: pfs denied via dump_threads_error, MySQL 5.7 empty Host,
        # verified CDC, 1 unmatched row, 1 Readyset, Replication Status Running.
        with self.subTest(case='fallback_estimate_pfs_denied'):
            primary = member_fixture(
                UUID_A,
                error_on='SHOW REPLICAS',
                query_error="ERROR 1064 (42000) at line 1: syntax error near 'REPLICAS'",
                dump_threads_error=pfs_denied,
                replicas=[{'Server_id': '103', 'Host': '', 'Port': '3306', 'Master_id': '101', 'Slave_UUID': rs_uuid}],
            )
            primary['variables']['version'] = '5.7.44'
            with self.clients({'active.example': primary, 'readyset.example': rs_fixture()}):
                report = self.read('map')
            nodes = self.assert_map_schema(report)
            self.assertEqual(set(nodes), {'active', 'readyset'})
            edge = next(e for e in report['map']['edges'] if e['kind'] == 'cache_upstream')
            self.assertTrue(edge['verified'])
            self.assertEqual(edge['reason'], 'Also registered on active as a replica (server ID 103) (estimate)')

        # Positive estimate fallback when performance_schema is OFF (skips pfs dump queries entirely).
        with self.subTest(case='fallback_estimate_pfs_off'):
            primary = member_fixture(
                UUID_A,
                replicas=[{'Server_Id': '103', 'Host': '', 'Port': '0', 'Replica_UUID': rs_uuid}],
            )
            primary['variables']['performance_schema'] = 'OFF'
            with self.clients({'active.example': primary, 'readyset.example': rs_fixture()}) as observed:
                report = self.read('map')
            nodes = self.assert_map_schema(report)
            self.assertEqual(set(nodes), {'active', 'readyset'})
            edge = next(e for e in report['map']['edges'] if e['kind'] == 'cache_upstream')
            self.assertEqual(edge['reason'], 'Also registered on active as a replica (server ID 103) (estimate)')
            self.assertFalse(any('user_variables_by_thread' in sql
                                 for sql in self.statements(observed, 'active.example')))

        # Negative fallback conditions (every single one must keep the row as a peer):
        # (a) Dump evidence IS available (pfs ON, attrs_lost=0, no matching dump row).
        with self.subTest(case='fallback_refused_dump_available'):
            primary = member_fixture(UUID_A, replicas=[
                {'Server_Id': '103', 'Host': '', 'Port': '3306', 'Replica_UUID': rs_uuid},
            ], dump_threads=[])
            with self.clients({'active.example': primary, 'readyset.example': rs_fixture()}):
                report = self.read('map')
            nodes = self.assert_map_schema(report)
            self.assertIn('peer:' + rs_uuid, nodes)
            edge = next(e for e in report['map']['edges'] if e['kind'] == 'cache_upstream')
            self.assertNotIn('reason', edge)

        # (b) Row has a reported Host (even with pfs denied).
        with self.subTest(case='fallback_refused_non_empty_host'):
            primary = member_fixture(UUID_A, dump_threads_error=pfs_denied, replicas=[
                {'Server_Id': '103', 'Host': 'ext-replica.example', 'Port': '3306', 'Replica_UUID': rs_uuid},
            ])
            with self.clients({'active.example': primary, 'readyset.example': rs_fixture()}):
                report = self.read('map')
            nodes = self.assert_map_schema(report)
            self.assertIn('peer:' + rs_uuid, nodes)
            edge = next(e for e in report['map']['edges'] if e['kind'] == 'cache_upstream')
            self.assertNotIn('reason', edge)

        # (c) Readyset Replication Status is not Running (Stopped, Disabled, missing).
        for status_val in ('Stopped', 'Disabled', None):
            with self.subTest(case='fallback_refused_repl_status', status=status_val):
                primary = member_fixture(UUID_A, dump_threads_error=pfs_denied, replicas=[
                    {'Server_Id': '103', 'Host': '', 'Port': '3306', 'Replica_UUID': rs_uuid},
                ])
                with self.clients({'active.example': primary,
                                   'readyset.example': rs_fixture(repl_status=status_val)}):
                    report = self.read('map')
                nodes = self.assert_map_schema(report)
                self.assertIn('peer:' + rs_uuid, nodes)
                edge = next(e for e in report['map']['edges'] if e['kind'] == 'cache_upstream')
                self.assertNotIn('reason', edge)

        # (d) Source has 2 unmatched registration rows (even with pfs denied and empty Hosts).
        with self.subTest(case='fallback_refused_two_unmatched_rows'):
            primary = member_fixture(UUID_A, dump_threads_error=pfs_denied, replicas=[
                {'Server_id': '103', 'Host': '', 'Port': '3306', 'Master_id': '101', 'Slave_UUID': rs_uuid},
                {'Server_id': '205', 'Host': '', 'Port': '3306', 'Master_id': '101', 'Slave_UUID': other_uuid},
            ])
            with self.clients({'active.example': primary, 'readyset.example': rs_fixture()}):
                report = self.read('map')
            nodes = self.assert_map_schema(report)
            self.assertIn('peer:' + rs_uuid, nodes)
            self.assertIn('peer:' + other_uuid, nodes)
            self.assertEqual(nodes['peer:' + rs_uuid]['label'],
                             'Replica without a reported address (server ID 103, port 3306)')
            self.assertEqual(nodes['peer:' + other_uuid]['label'],
                             'Replica without a reported address (server ID 205, port 3306)')
            self.assertNotIn('?:3306', json.dumps(report))
            edge = next(e for e in report['map']['edges'] if e['kind'] == 'cache_upstream')
            self.assertNotIn('reason', edge)

        # (e) Readyset upstream is unverified (address-only estimate, verified_cdc is False).
        with self.subTest(case='fallback_refused_unverified_upstream'):
            primary = member_fixture(UUID_A, dump_threads_error=pfs_denied, replicas=[
                {'Server_Id': '103', 'Host': '', 'Port': '3306', 'Replica_UUID': rs_uuid},
            ])
            with self.clients({'active.example': primary,
                               'readyset.example': rs_fixture(proxied=None, host='active.example')}):
                report = self.read('map')
            nodes = self.assert_map_schema(report)
            self.assertIn('peer:' + rs_uuid, nodes)
            edge = next(e for e in report['map']['edges'] if e['kind'] == 'cache_upstream')
            self.assertFalse(edge['verified'])
            self.assertNotIn('reason', edge)

        # (f) Proxied queries-only upstream (verified_cdc is False) never triggers fallback.
        with self.subTest(case='fallback_refused_queries_only_upstream'):
            primary = member_fixture(UUID_A, dump_threads_error=pfs_denied, replicas=[
                {'Server_Id': '103', 'Host': '', 'Port': '3306', 'Replica_UUID': rs_uuid},
            ])
            rs_q = rs_fixture(
                proxied=UUID_A, cdc_server='other-cdc:3306',
                upstreams=[{'host': 'other-cdc', 'port': '3306', 'purpose': 'cdc', 'active_cdc': '1'}],
            )
            with self.clients({'active.example': primary, 'readyset.example': rs_q}):
                report = self.read('map')
            nodes = self.assert_map_schema(report)
            self.assertIn('peer:' + rs_uuid, nodes)
            self.assertIn('peer:other-cdc:3306', nodes)
            edge = next(e for e in report['map']['edges']
                        if e['kind'] == 'cache_upstream' and e['to'] == 'active')
            self.assertEqual(edge['label'], 'Upstream (queries)')
            self.assertNotIn('reason', edge)

        # (g) Adapter + Admin of one deployment (same host, upstreams, cdc_server) are counted ONCE.
        with self.subTest(case='adapter_plus_admin_counted_once'):
            profiles = [
                {'id': 'active', 'name': 'active', 'host': 'active.example', 'port': 3306, 'group': 'G'},
                {'id': 'rs-sql', 'name': 'rs-sql', 'host': '127.0.0.1', 'port': 34507, 'group': 'G'},
                {'id': 'rs-adm', 'name': 'rs-adm', 'host': '127.0.0.1', 'port': 34536, 'group': 'G'},
            ]
            ups = [{'host': 'internal-primary', 'port': '3306', 'purpose': 'cdc, proxy', 'active_cdc': '1',
                    'health': 'up', 'user': 'rs_user'}]
            for mode in ('positive', 'fallback'):
                probes = {
                    'active': {
                        'kind': 'database', 'hostname': 'internal-primary', 'port': 3306,
                        'channels': [], 'cluster_members': [],
                        'replicas': [{'host': 'mf-rs-readyset' if mode == 'positive' else None,
                                      'port': 3307 if mode == 'positive' else None,
                                      'uuid': rs_uuid, 'server_id': 103}],
                        'dump_threads': ([{'uuid': rs_uuid, 'host': '192.168.112.4', 'user': 'rs_user', 'class': 'none'}]
                                         if mode == 'positive' else []),
                        'readyset_clients': ([{'host': '192.168.112.4', 'user': 'rs_user'}]
                                             if mode == 'positive' else []),
                        'dump_read_failed': mode == 'fallback',
                    },
                    'rs-sql': {'kind': 'readyset', 'port_role': 'adapter', 'proxied_server_uuid': UUID_A,
                               'cdc_server': 'internal-primary:3306', 'replication_status': 'Running', 'upstreams': ups},
                    'rs-adm': {'kind': 'readyset', 'port_role': 'admin', 'proxied_server_uuid': None,
                               'cdc_server': 'internal-primary:3306', 'replication_status': 'Running', 'upstreams': ups},
                }
                entries = []
                for p in profiles:
                    e = topology_group._entry(p)
                    is_rs = probes[p['id']]['kind'] == 'readyset'
                    e.update(outcome='read', engine='mysql', product='Readyset' if is_rs else 'MySQL',
                             version='8.4.11', role='cache' if is_rs else 'source', state='reachable',
                             server_uuid=None if is_rs else UUID_A)
                    entries.append(e)
                built = topology_group._build_map(entries, probes, profiles)
                self.assertEqual([n for n in built['nodes'] if n['kind'] == 'peer'], [], mode)
                sql_edge = next(e for e in built['edges'] if e['from'] == 'rs-sql' and e['to'] == 'active')
                expected_reason = ('Also registered on active as a replica (server ID 103)' +
                                   (' (estimate)' if mode == 'fallback' else ''))
                self.assertEqual(sql_edge['reason'], expected_reason, mode)

        # (h) More than one candidate owner (same host + same user) stays a peer, and SSH loopback never crosses.
        with self.subTest(case='more_than_one_candidate_owner_stays_peer'):
            profiles = [
                {'id': 'active', 'name': 'active', 'host': '127.0.0.1', 'port': 3306, 'group': 'G'},
                {'id': 'rs1', 'name': 'rs1', 'host': 'rs-shared.example', 'port': 3307, 'group': 'G'},
                {'id': 'rs2', 'name': 'rs2', 'host': 'rs-shared.example', 'port': 3308, 'group': 'G'},
            ]
            probes = {
                'active': {
                    'kind': 'database', 'hostname': 'primary', 'port': 3306,
                    'channels': [], 'cluster_members': [],
                    'replicas': [{'host': 'rs-shared', 'port': 3307, 'uuid': rs_uuid, 'server_id': 103}],
                    'dump_threads': [{'uuid': rs_uuid, 'host': 'rs-shared.example', 'user': 'rs_user', 'class': 'none'}],
                    'dump_read_failed': False,
                },
                'rs1': {'kind': 'readyset', 'port_role': 'adapter', 'proxied_server_uuid': UUID_A,
                        'cdc_server': 'primary:3306', 'replication_status': 'Running',
                        'upstreams': [{'host': 'primary', 'port': '3306', 'purpose': 'cdc, proxy', 'user': 'rs_user'}]},
                'rs2': {'kind': 'readyset', 'port_role': 'adapter', 'proxied_server_uuid': UUID_A,
                        'cdc_server': 'primary:3306', 'replication_status': 'Running',
                        'upstreams': [{'host': 'primary', 'port': '3306', 'purpose': 'cdc, proxy', 'user': 'rs_user'}]},
            }
            entries = []
            for p in profiles:
                e = topology_group._entry(p)
                is_rs = probes[p['id']]['kind'] == 'readyset'
                e.update(outcome='read', engine='mysql', product='Readyset' if is_rs else 'MySQL',
                         version='8.4.11', role='cache' if is_rs else 'source', state='reachable',
                         server_uuid=None if is_rs else UUID_A)
                entries.append(e)
            built = topology_group._build_map(entries, probes, profiles)
            self.assertIn('peer:' + rs_uuid, {n['id'] for n in built['nodes']})
            for edge in built['edges']:
                if edge['kind'] == 'cache_upstream':
                    self.assertNotIn('reason', edge)

        # (i) Two distinct Readyset deployments on the same source refuse fallback.
        with self.subTest(case='fallback_refused_two_readysets'):
            self.save('readyset2', 'G', port=3310)
            primary = member_fixture(UUID_A, dump_threads_error=pfs_denied, replicas=[
                {'Server_Id': '103', 'Host': '', 'Port': '3306', 'Replica_UUID': rs_uuid},
            ])
            with self.clients({
                'active.example': primary,
                'readyset.example': rs_fixture(),
                'readyset2.example': rs_fixture(),
            }):
                report = self.read('map')
            nodes = self.assert_map_schema(report)
            self.assertIn('peer:' + rs_uuid, nodes)
            for edge in report['map']['edges']:
                if edge['kind'] == 'cache_upstream':
                    self.assertNotIn('reason', edge)

    def test_map_readyset_cdc_evidence_rules_and_dump_query_gating(self):
        self.save('primary', 'G', host='primary.example')
        self.save('replica', 'G', host='replica.example')
        self.save('readyset', 'G', host='readyset.example', port=3309)

        # (a1) Single upstream with no purpose column -> affirmative CDC evidence -> "Upstream".
        with self.subTest(case='cdc_rule_a_no_purpose_column'):
            rs = member_fixture(
                proxy='readyset', readyset_adapter=True, proxied_server_uuid=UUID_B,
                cdc_server='internal-primary:3306',
                upstreams=[{'host': 'internal-primary', 'port': '3306', 'health': 'up'}],
            )
            rs['variables']['version_comment'] = 'Readyset'
            with self.clients({
                'active.example': member_fixture(UUID_A),
                'primary.example': member_fixture(UUID_B),
                'replica.example': member_fixture(UUID_C),
                'readyset.example': rs,
            }):
                report = self.read('map')
            nodes = self.assert_map_schema(report)
            self.assertEqual([n for n in nodes.values() if n['kind'] == 'peer'], [])
            edge = next(e for e in report['map']['edges'] if e['kind'] == 'cache_upstream')
            self.assertEqual((edge['to'], edge['label'], edge['verified']), ('primary', 'Upstream', True))

        # (b) Two unresolved upstreams: CDC Server == proxied @@hostname:@@port proves CDC ("Upstream");
        # mismatch keeps "Upstream (queries)" and keeps the unresolved CDC address visible as a peer.
        for proxied_host, expected_label, expect_cdc_peer in (
            ('internal-primary', 'Upstream', False),
            ('internal-replica', 'Upstream (queries)', True),
        ):
            with self.subTest(case='cdc_rule_b_proxied_hostname_port', proxied_host=proxied_host):
                target_uuid = UUID_B if proxied_host == 'internal-primary' else UUID_C
                target_id = 'primary' if proxied_host == 'internal-primary' else 'replica'
                rs = member_fixture(
                    proxy='readyset', readyset_adapter=True,
                    proxied_server_uuid=target_uuid,
                    proxied_hostname=proxied_host,
                    proxied_port='3306',
                    cdc_server='internal-primary:3306',
                    upstreams=[
                        {'host': 'internal-primary', 'port': '3306', 'purpose': 'cdc, proxy', 'active_cdc': '1', 'health': 'up'},
                        {'host': 'internal-replica', 'port': '3306', 'purpose': 'proxy', 'active_cdc': '0', 'health': 'up'},
                    ],
                )
                rs['variables']['version_comment'] = 'Readyset'
                with self.clients({
                    'active.example': member_fixture(UUID_A),
                    'primary.example': member_fixture(UUID_B),
                    'replica.example': member_fixture(UUID_C),
                    'readyset.example': rs,
                }):
                    report = self.read('map')
                nodes = self.assert_map_schema(report)
                edge = next(e for e in report['map']['edges']
                            if e['kind'] == 'cache_upstream' and e['to'] == target_id)
                self.assertEqual((edge['label'], edge['verified']), (expected_label, True))
                self.assertEqual('peer:internal-primary:3306' in nodes, expect_cdc_peer)

        # P1 regression: single CDC-only upstream (or CDC Server only with empty upstream_servers)
        # without affirmative CDC match MUST NOT be swallowed by covered.add(0).
        for case, upstreams in (
            ('single_cdc_only_row', [{'host': 'unresolved-cdc', 'port': '3306', 'purpose': 'cdc', 'active_cdc': '1'}]),
            ('empty_upstreams_with_cdc_server', []),
        ):
            with self.subTest(case=case):
                rs = member_fixture(
                    proxy='readyset', readyset_adapter=True,
                    proxied_server_uuid=UUID_C,
                    proxied_hostname='other-host', proxied_port='3306',
                    cdc_server='unresolved-cdc:3306',
                    upstreams=upstreams,
                )
                rs['variables']['version_comment'] = 'Readyset'
                with self.clients({
                    'active.example': member_fixture(UUID_A),
                    'primary.example': member_fixture(UUID_B),
                    'replica.example': member_fixture(UUID_C),
                    'readyset.example': rs,
                }):
                    report = self.read('map')
                nodes = self.assert_map_schema(report)
                self.assertIn('peer:unresolved-cdc:3306', nodes)
                q_edge = next(e for e in report['map']['edges']
                              if e['kind'] == 'cache_upstream' and e['to'] == 'replica')
                self.assertEqual((q_edge['label'], q_edge['verified']), ('Upstream (queries)', True))

        # P6 gating: dump SQL runs ONLY when group has a Readyset member, ONLY on MySQL members
        # that report replicas, and uses the 5 s statement budget.
        with self.subTest(case='dump_read_runs_with_5s_budget_only_on_mysql_source_with_replicas'):
            primary = member_fixture(UUID_B, replicas=[
                {'Server_Id': '102', 'Host': 'replica.example', 'Port': '3306', 'Replica_UUID': UUID_C},
            ], dump_threads=[])
            replica = member_fixture(UUID_C, channels=[{'Source_UUID': UUID_B}])
            maria = member_fixture(engine='mariadb', replicas=[{'Server_id': '2', 'Host': 'm-rep', 'Port': '3306'}])
            rs = member_fixture(proxy='readyset', readyset_adapter=True, proxied_server_uuid=UUID_B,
                                cdc_server='primary.example:3306',
                                upstreams=[{'host': 'primary.example', 'port': '3306', 'purpose': 'cdc, proxy'}])
            rs['variables']['version_comment'] = 'Readyset'
            with self.clients({
                'active.example': maria,
                'primary.example': primary,
                'replica.example': replica,
                'readyset.example': rs,
            }) as observed:
                report = self.read('map')
            self.assertEqual(report['summary']['read'], 4)
            self.assertTrue(any('user_variables_by_thread' in s for s in self.statements(observed, 'primary.example')))
            self.assertFalse(any('user_variables_by_thread' in s for s in self.statements(observed, 'replica.example')))
            self.assertFalse(any('user_variables_by_thread' in s for s in self.statements(observed, 'active.example')))
            self.assertTrue(any(t <= topology_group.DUMP_QUERY_SECONDS for t in observed.timeouts))

        with self.subTest(case='dump_read_skipped_when_group_has_no_readyset'):
            self.store.assign_profile_group(['readyset'], '')
            primary = member_fixture(UUID_B, replicas=[
                {'Server_Id': '102', 'Host': 'replica.example', 'Port': '3306', 'Replica_UUID': UUID_C},
            ])
            with self.clients({
                'active.example': member_fixture(UUID_A),
                'primary.example': primary,
                'replica.example': member_fixture(UUID_C, channels=[{'Source_UUID': UUID_B}]),
            }) as observed:
                report = self.read('map')
            self.assertEqual(report['summary']['read'], 3)
            all_sql = '\n'.join(self.statements(observed))
            self.assertNotIn('user_variables_by_thread', all_sql)
            self.assertNotIn('Performance_schema_session_connect_attrs_lost', all_sql)

    def test_map_readyset_r2_attrs_size_zero_or_unknown_marks_dump_unknown(self):
        # R2-1: performance_schema_session_connect_attrs_size = 0 stores no
        # attributes and leaves Performance_schema_session_connect_attrs_lost
        # at 0, so disabled or unknown attribute storage makes every dump class
        # "unknown" (never "none").
        rs_uuid = 'ad67a025-bc44-42bf-9521-58f7dc4e4f95'
        self.save('readyset', 'G', port=3309)

        def rs_fixture():
            f = member_fixture(
                proxy='readyset', readyset_adapter=True,
                proxied_server_uuid=UUID_A,
                cdc_server='internal-primary:3306',
                replication_status='Running',
                upstreams=[{'host': 'internal-primary', 'port': '3306', 'purpose': 'cdc, proxy',
                            'active_cdc': '1', 'health': 'up', 'user': 'rs_user'}],
            )
            f['variables']['version_comment'] = 'Readyset'
            return f

        for case, size_val, pfs_val, rep_host, dump_user, expect_peer, expect_estimate in (
            ('size_zero_named_host_blocks_positive', '0', 'ON', 'replica-a.example', 'rs_user', True, False),
            ('size_missing_named_host_blocks_positive', None, 'ON', 'replica-a.example', 'rs_user', True, False),
            ('pfs_missing_named_host_blocks_positive', '512', None, 'replica-a.example', 'rs_user', True, False),
            ('size_zero_empty_host_matching_user_estimates', '0', 'ON', '', 'rs_user', False, True),
            ('size_zero_empty_host_disjoint_user_stays_peer', '0', 'ON', '', 'other_user', True, False),
        ):
            with self.subTest(case=case):
                primary = member_fixture(
                    UUID_A,
                    replicas=[{'Server_Id': '103', 'Host': rep_host, 'Port': '3307', 'Replica_UUID': rs_uuid}],
                    dump_threads=[{'pid': 10, 'uuid': rs_uuid, 'host': 'readyset.example', 'user': dump_user, 'attrs': []}],
                    status={'Performance_schema_session_connect_attrs_lost': '0'},
                )
                if size_val is None:
                    primary['variables'].pop('performance_schema_session_connect_attrs_size', None)
                else:
                    primary['variables']['performance_schema_session_connect_attrs_size'] = size_val
                if pfs_val is None:
                    primary['variables'].pop('performance_schema', None)
                else:
                    primary['variables']['performance_schema'] = pfs_val
                with self.clients({'active.example': primary, 'readyset.example': rs_fixture()}) as observed:
                    report = self.read('map')
                nodes = self.assert_map_schema(report)
                edge = next(e for e in report['map']['edges'] if e['kind'] == 'cache_upstream')
                self.assertEqual(
                    sum('performance_schema_session_connect_attrs_size' in s
                        for s in self.statements(observed, 'active.example')),
                    1,
                )
                if expect_peer:
                    self.assertIn('peer:' + rs_uuid, nodes)
                    self.assertNotIn('reason', edge)
                else:
                    self.assertNotIn('peer:' + rs_uuid, nodes)
                    if expect_estimate:
                        self.assertEqual(edge['reason'],
                                         'Also registered on active as a replica (server ID 103) (estimate)')
        # Direct unit check of _classify_dump_attrs when storage is disabled/unknown.
        self.assertEqual(topology_group._classify_dump_attrs([], 0, attrs_storage_enabled=False), 'unknown')
        self.assertEqual(topology_group._classify_dump_attrs(
            [('program_name', 'mysqld')], 0, attrs_storage_enabled=False), 'unknown')

    def test_map_readyset_r2_dump_timeout_keeps_member_read_and_unknown_class(self):
        # R2-2: a DUMP_QUERY_SECONDS timeout on optional dump queries must never
        # latch _ReadConnection.failure or TargetEvidenceConnection.problem;
        # the member read continues (including later cluster queries) and the
        # dump read falls back safely. An actual node switch still fails.
        rs_uuid = 'ad67a025-bc44-42bf-9521-58f7dc4e4f95'
        self.save('readyset', 'G', port=3309)

        def rs_fixture():
            f = member_fixture(
                proxy='readyset', readyset_adapter=True,
                proxied_server_uuid=UUID_A,
                cdc_server='internal-primary:3306',
                replication_status='Running',
                upstreams=[{'host': 'internal-primary', 'port': '3306', 'purpose': 'cdc, proxy',
                            'active_cdc': '1', 'health': 'up', 'user': 'rs_user'}],
            )
            f['variables']['version_comment'] = 'Readyset'
            return f

        gr_rows = [[UUID_A, 'active.example', 3306, 'ONLINE', 'PRIMARY']]
        for case, pause_on, rep_host, expect_peer, expect_estimate in (
            ('pfs_vars_timeout_named_host', 'performance_schema_session_connect_attrs_size', 'mf-rs-readyset', True, False),
            ('dump_threads_timeout_named_host', 'user_variables_by_thread', 'mf-rs-readyset', True, False),
            ('dump_threads_timeout_empty_host', 'user_variables_by_thread', '', False, True),
            ('attrs_lost_timeout_named_host', 'performance_schema_session_connect_attrs_lost', 'mf-rs-readyset', True, False),
            ('readyset_clients_timeout_named_host', "attr_value like 'readyset%'", 'mf-rs-readyset', True, False),
        ):
            with self.subTest(case=case):
                primary = self.gr_map_fixture(
                    UUID_A, 'PRIMARY', gr_rows,
                    innodb_cluster=True,
                    cluster_metadata=[{'cluster_id': 'c1', 'cluster_name': 'ClusterAfterDump',
                                       'primary_mode': 'pm', 'group_name': UUID_D}],
                    replicas=[{'Server_Id': '103', 'Host': rep_host, 'Port': '3307', 'Replica_UUID': rs_uuid}],
                    dump_threads=[{'pid': 10, 'uuid': rs_uuid, 'host': '192.168.112.4', 'user': 'rs_user', 'attrs': []}],
                    readyset_clients=[{'host': '192.168.112.4', 'user': 'rs_user'}],
                    pause_on=pause_on,
                    pause_on_seconds=0.45,
                )
                with self.clients({'active.example': primary, 'readyset.example': rs_fixture()}), \
                        mock.patch.object(topology_group, 'DUMP_QUERY_SECONDS', 0.12):
                    report = self.read('map')
                nodes = self.assert_map_schema(report)
                members = {m['profile_id']: m for m in report['members']}
                self.assertEqual(members['active']['outcome'], 'read', report)
                self.assertEqual(report['summary']['failed'], 0, report)
                # Post-dump cluster metadata query still ran on the healthy member connection.
                self.assertEqual(report['map']['clusters'][0]['name'], 'ClusterAfterDump')
                edge = next(e for e in report['map']['edges'] if e['kind'] == 'cache_upstream')
                self.assertTrue(edge['verified'])
                if expect_peer:
                    self.assertIn('peer:' + rs_uuid, nodes)
                    self.assertNotIn('reason', edge)
                else:
                    self.assertNotIn('peer:' + rs_uuid, nodes)
                    self.assertEqual(edge['reason'],
                                     'Also registered on active as a replica (server ID 103) (estimate)')

        # Identity switch during _read_dump_threads MUST still fail the member.
        with self.subTest(case='identity_switch_during_dump_read_still_fails_member'):
            primary = member_fixture(
                UUID_A,
                replicas=[{'Server_Id': '103', 'Host': 'mf-rs-readyset', 'Port': '3307', 'Replica_UUID': rs_uuid}],
                switch_on='user_variables_by_thread',
                evidence_variables={'server_uuid': UUID_B},
            )
            with self.clients({'active.example': primary, 'readyset.example': rs_fixture()}):
                report = self.read('map')
            members = {m['profile_id']: m for m in report['members']}
            self.assertEqual(members['active']['outcome'], 'failed')
            self.assertIsNone(members['active']['server_uuid'])

    def test_map_r2_phase1_cancellation_closes_every_connection_and_ssh_tunnel(self):
        # R2-3: CancelledRead is a BaseException. Cancelling during phase 1 must
        # still run ExitStack cleanup for both the cancelled worker and every
        # sibling that already finished phase 1 with an open stack.
        from myflames import read_cancel
        self.save('member-b', 'G')
        self.save('member-c', 'G')
        fixtures = {
            'active.example': member_fixture(UUID_A),
            'member-b.example': member_fixture(UUID_B),
            'member-c.example': member_fixture(UUID_C),
        }
        cancel_event = threading.Event()
        opened_conns = []
        closed_conns = []
        released_tunnels = []
        exited_contexts = []
        lock = threading.Lock()
        real_open = topology_group.open_group_member
        real_identify = topology_group._identity
        phase1_done = [0]

        @contextlib.contextmanager
        def tracked_open(store, group_name, profile, active_guard, options_override=None):
            with real_open(store, group_name, profile, active_guard, options_override) as opened:
                conn, options = opened
                lease = mock.Mock()
                lease.release.side_effect = lambda h=conn.host: released_tunnels.append(h)
                conn._held_lease = lease
                orig_close = conn.close

                def tracked_close(orig=orig_close, host=conn.host):
                    with lock:
                        if host not in closed_conns:
                            closed_conns.append(host)
                    return orig()

                conn.close = tracked_close
                with lock:
                    opened_conns.append(conn.host)
                try:
                    yield conn, options
                finally:
                    with lock:
                        exited_contexts.append(conn.host)

        def identify_and_cancel(conn, member, read_position=True):
            res = real_identify(conn, member, read_position=read_position)
            if read_position:
                with lock:
                    phase1_done[0] += 1
                    # Member 1 completes phase 1 (holding an open stack);
                    # Member 2 cancels mid-phase-1 while its own stack is open.
                    if phase1_done[0] >= 2:
                        cancel_event.set()
                        read_cancel.check()
            return res

        with self.clients(fixtures), \
                mock.patch.object(topology_group, 'MAX_CONCURRENCY', 1), \
                mock.patch.object(topology_group, 'open_group_member', side_effect=tracked_open), \
                mock.patch.object(topology_group, '_identity', side_effect=identify_and_cancel):
            with read_cancel.scope(cancel_event):
                with self.assertRaises(read_cancel.CancelledRead):
                    self.read('map')
        self.assertGreaterEqual(len(opened_conns), 2, opened_conns)
        self.assertEqual(sorted(closed_conns), sorted(opened_conns))
        self.assertEqual(sorted(exited_contexts), sorted(opened_conns))
        self.assertEqual(sorted(released_tunnels), sorted(opened_conns))

    def test_map_readyset_r2_pass_b_sole_requires_running_attrs_and_unique_candidate(self):
        # R2-4: Pass B ("sole") claims only when the owner's Replication Status
        # is running, a READYSET% client attribute exists on the dump row's
        # host, and exactly one unmatched row resolves to that owner.
        rs_uuid = 'ad67a025-bc44-42bf-9521-58f7dc4e4f95'
        rs_uuid_2 = 'bd67a025-bc44-42bf-9521-58f7dc4e4f96'
        ext_uuid = 'eeeeeeee-0000-4000-8000-000000000005'
        self.save('readyset', 'G', host='127.0.0.1', port=34507)

        def rs_fixture(repl_status='Running'):
            f = member_fixture(
                proxy='readyset', readyset_adapter=True,
                proxied_server_uuid=UUID_A,
                cdc_server='internal-primary:3306',
                replication_status=repl_status,
                upstreams=[{'host': 'internal-primary', 'port': '3306', 'purpose': 'cdc, proxy',
                            'active_cdc': '1', 'health': 'up', 'user': 'rs_user'}],
            )
            f['variables']['version_comment'] = 'Readyset'
            return f

        # 1. Replication Status not running -> stays peer even with READYSET% client attrs on host.
        for status_val in ('Stopped', 'Disabled', None):
            with self.subTest(case='pass_b_refused_repl_not_running', status=status_val):
                primary = member_fixture(
                    UUID_A,
                    replicas=[{'Server_Id': '103', 'Host': 'mf-rs-readyset', 'Port': '3307', 'Replica_UUID': rs_uuid}],
                    dump_threads=[{'pid': 10, 'uuid': rs_uuid, 'host': '192.168.112.4', 'user': 'rs_user', 'attrs': []}],
                    readyset_clients=[{'host': '192.168.112.4', 'user': 'rs_user'}],
                )
                with self.clients({'active.example': primary, '127.0.0.1': rs_fixture(status_val)}):
                    report = self.read('map')
                nodes = self.assert_map_schema(report)
                self.assertIn('peer:' + rs_uuid, nodes)
                edge = next(e for e in report['map']['edges'] if e['kind'] == 'cache_upstream')
                self.assertNotIn('reason', edge)

        # 2. Missing READYSET% client attrs on dump row's host (none, or on another host) -> stays peer.
        for case, clients in (
            ('no_readyset_clients', []),
            ('readyset_client_on_other_host', [{'host': '192.168.112.99', 'user': 'rs_user'}]),
        ):
            with self.subTest(case='pass_b_refused_' + case):
                primary = member_fixture(
                    UUID_A,
                    replicas=[{'Server_Id': '103', 'Host': 'mf-rs-readyset', 'Port': '3307', 'Replica_UUID': rs_uuid}],
                    dump_threads=[{'pid': 10, 'uuid': rs_uuid, 'host': '192.168.112.4', 'user': 'rs_user', 'attrs': []}],
                    readyset_clients=clients,
                )
                with self.clients({'active.example': primary, '127.0.0.1': rs_fixture()}):
                    report = self.read('map')
                nodes = self.assert_map_schema(report)
                self.assertIn('peer:' + rs_uuid, nodes)
                edge = next(e for e in report['map']['edges'] if e['kind'] == 'cache_upstream')
                self.assertNotIn('reason', edge)

        # 3. Two competing unmatched rows both resolving to the one owner via Pass B -> BOTH stay peers.
        with self.subTest(case='pass_b_refused_two_competing_unmatched_rows'):
            primary = member_fixture(
                UUID_A,
                replicas=[
                    {'Server_Id': '103', 'Host': 'rs-one', 'Port': '3307', 'Replica_UUID': rs_uuid},
                    {'Server_Id': '104', 'Host': 'rs-two', 'Port': '3307', 'Replica_UUID': rs_uuid_2},
                ],
                dump_threads=[
                    {'pid': 10, 'uuid': rs_uuid, 'host': '192.168.112.4', 'user': 'rs_user', 'attrs': []},
                    {'pid': 11, 'uuid': rs_uuid_2, 'host': '192.168.112.5', 'user': 'rs_user', 'attrs': []},
                ],
                readyset_clients=[
                    {'host': '192.168.112.4', 'user': 'rs_user'},
                    {'host': '192.168.112.5', 'user': 'rs_user'},
                ],
            )
            with self.clients({'active.example': primary, '127.0.0.1': rs_fixture()}):
                report = self.read('map')
            nodes = self.assert_map_schema(report)
            self.assertIn('peer:' + rs_uuid, nodes)
            self.assertIn('peer:' + rs_uuid_2, nodes)
            edge = next(e for e in report['map']['edges'] if e['kind'] == 'cache_upstream')
            self.assertNotIn('reason', edge)

        # 4. Positive Pass B: running + READYSET% on dump host + exactly one row resolving to owner
        #    (even alongside an unrelated external mysqld replica that does not resolve to owner).
        with self.subTest(case='pass_b_succeeds_with_running_attrs_and_unique_match'):
            primary = member_fixture(
                UUID_A,
                replicas=[
                    {'Server_Id': '103', 'Host': 'mf-rs-readyset', 'Port': '3307', 'Replica_UUID': rs_uuid},
                    {'Server_Id': '104', 'Host': 'ext-replica', 'Port': '3306', 'Replica_UUID': ext_uuid},
                ],
                dump_threads=[
                    {'pid': 10, 'uuid': rs_uuid, 'host': '192.168.112.4', 'user': 'rs_user', 'attrs': []},
                    {'pid': 11, 'uuid': ext_uuid, 'host': '192.168.112.9', 'user': 'mf_repl',
                     'attrs': [('program_name', 'mysqld')]},
                ],
                readyset_clients=[{'host': '192.168.112.4', 'user': 'rs_sampler'}],
            )
            with self.clients({'active.example': primary, '127.0.0.1': rs_fixture()}):
                report = self.read('map')
            nodes = self.assert_map_schema(report)
            self.assertEqual(set(nodes), {'active', 'readyset', 'peer:' + ext_uuid})
            edge = next(e for e in report['map']['edges'] if e['kind'] == 'cache_upstream')
            self.assertEqual(edge['reason'], 'Also registered on active as a replica (server ID 103)')

    def test_map_readyset_r2_estimate_fallback_uses_row_evidence_and_checks_user(self):
        # R2-5: estimate fallback inspects the unmatched row's own evidence
        # (not all dump classes), so a matched in-group mysqld replica does not
        # block fallback when attrs were lost; refuse when the row's dump user
        # is known and disjoint from the owner's upstream users.
        rs_uuid = 'ad67a025-bc44-42bf-9521-58f7dc4e4f95'
        self.save('replica', 'G')
        self.save('readyset', 'G', port=3309)

        def rs_fixture(user='rs_user'):
            row = {'host': 'internal-primary', 'port': '3306', 'purpose': 'cdc, proxy',
                   'active_cdc': '1', 'health': 'up'}
            if user is not None:
                row['user'] = user
            f = member_fixture(
                proxy='readyset', readyset_adapter=True,
                proxied_server_uuid=UUID_A,
                cdc_server='internal-primary:3306',
                replication_status='Running',
                upstreams=[row],
            )
            f['variables']['version_comment'] = 'Readyset'
            return f

        replica = member_fixture(UUID_B, channels=[{
            'Source_UUID': UUID_A, 'Source_Host': 'active.example', 'Source_Port': '3306',
            'Replica_IO_Running': 'Yes', 'Replica_SQL_Running': 'Yes',
        }])

        # 1. Matched in-group mysqld replica + attrs_lost > 0 + unmatched hostless Readyset (matching user) -> estimate!
        with self.subTest(case='in_group_mysqld_does_not_block_estimate_when_attrs_lost'):
            primary = member_fixture(
                UUID_A,
                replicas=[
                    {'Server_Id': '102', 'Host': 'replica.example', 'Port': '3306', 'Replica_UUID': UUID_B},
                    {'Server_Id': '103', 'Host': '', 'Port': '3306', 'Replica_UUID': rs_uuid},
                ],
                dump_threads=[
                    {'pid': 10, 'uuid': UUID_B, 'host': 'replica.example', 'user': 'mf_repl',
                     'attrs': [('program_name', 'mysqld')]},
                    {'pid': 11, 'uuid': rs_uuid, 'host': '192.168.112.4', 'user': 'rs_user', 'attrs': []},
                ],
                status={'Performance_schema_session_connect_attrs_lost': '1'},
            )
            with self.clients({'active.example': primary, 'replica.example': replica,
                               'readyset.example': rs_fixture()}):
                report = self.read('map')
            nodes = self.assert_map_schema(report)
            self.assertEqual(set(nodes), {'active', 'replica', 'readyset'})
            edge = next(e for e in report['map']['edges'] if e['kind'] == 'cache_upstream')
            self.assertEqual(edge['reason'], 'Also registered on active as a replica (server ID 103) (estimate)')

        # 2. Contrary known dump user refuses estimate fallback (both when attrs_lost > 0 and when attrs_size = 0).
        for case, lost, size in (('attrs_lost_contrary_user', '1', '512'), ('attrs_size_zero_contrary_user', '0', '0')):
            with self.subTest(case=case):
                primary = member_fixture(
                    UUID_A,
                    replicas=[
                        {'Server_Id': '102', 'Host': 'replica.example', 'Port': '3306', 'Replica_UUID': UUID_B},
                        {'Server_Id': '103', 'Host': '', 'Port': '3306', 'Replica_UUID': rs_uuid},
                    ],
                    dump_threads=[
                        {'pid': 10, 'uuid': UUID_B, 'host': 'replica.example', 'user': 'mf_repl',
                         'attrs': [('program_name', 'mysqld')]},
                        {'pid': 11, 'uuid': rs_uuid, 'host': '192.168.112.4', 'user': 'different_user', 'attrs': []},
                    ],
                    status={'Performance_schema_session_connect_attrs_lost': lost},
                )
                primary['variables']['performance_schema_session_connect_attrs_size'] = size
                with self.clients({'active.example': primary, 'replica.example': replica,
                                   'readyset.example': rs_fixture('rs_user')}):
                    report = self.read('map')
                nodes = self.assert_map_schema(report)
                self.assertIn('peer:' + rs_uuid, nodes)
                edge = next(e for e in report['map']['edges'] if e['kind'] == 'cache_upstream')
                self.assertNotIn('reason', edge)

    def test_map_readyset_r2_contradicting_unresolved_cdc_server_never_hidden(self):
        # R2-6: a single purpose-less or "cdc, proxy" upstream row never hides
        # a different unresolved CDC Server and never sets verified_cdc; only
        # an equal endpoint is a duplicate.
        rs_uuid = 'ad67a025-bc44-42bf-9521-58f7dc4e4f95'
        self.save('primary', 'G', host='primary.example')
        self.save('readyset', 'G', host='readyset.example', port=3309)

        for case, upstream_row, cdc_server, expect_cdc_peer, expect_label in (
            ('single_no_purpose_different_cdc_server',
             {'host': 'internal-primary', 'port': '3306', 'health': 'up'},
             'other-cdc:3306', True, 'Upstream (queries)'),
            ('single_cdc_proxy_different_cdc_server',
             {'host': 'internal-primary', 'port': '3306', 'purpose': 'cdc, proxy', 'active_cdc': '1', 'health': 'up'},
             'other-cdc:3306', True, 'Upstream (queries)'),
            ('single_cdc_proxy_same_host_different_port_cdc_server',
             {'host': 'internal-primary', 'port': '3306', 'purpose': 'cdc, proxy', 'active_cdc': '1', 'health': 'up'},
             'internal-primary:3307', True, 'Upstream (queries)'),
            ('single_no_purpose_equal_cdc_server',
             {'host': 'INTERNAL-PRIMARY.', 'port': '3306', 'health': 'up'},
             'internal-primary:3306', False, 'Upstream'),
            ('single_cdc_proxy_equal_cdc_server',
             {'host': 'internal-primary', 'port': '3306', 'purpose': 'cdc, proxy', 'active_cdc': '1', 'health': 'up'},
             'internal-primary:3306', False, 'Upstream'),
        ):
            with self.subTest(case=case):
                primary = member_fixture(
                    UUID_B,
                    dump_threads_error="ERROR 1142 (42000): SELECT command denied",
                    replicas=[{'Server_Id': '103', 'Host': '', 'Port': '3306', 'Replica_UUID': rs_uuid}],
                )
                rs = member_fixture(
                    proxy='readyset', readyset_adapter=True,
                    proxied_server_uuid=UUID_B,
                    proxied_hostname='internal-primary',
                    proxied_port='3306',
                    cdc_server=cdc_server,
                    replication_status='Running',
                    upstreams=[upstream_row],
                )
                rs['variables']['version_comment'] = 'Readyset'
                with self.clients({
                    'active.example': member_fixture(UUID_A),
                    'primary.example': primary,
                    'readyset.example': rs,
                }):
                    report = self.read('map')
                nodes = self.assert_map_schema(report)
                edge = next(e for e in report['map']['edges']
                            if e['kind'] == 'cache_upstream' and e['to'] == 'primary')
                self.assertEqual((edge['label'], edge['verified']), (expect_label, True))
                if expect_cdc_peer:
                    self.assertIn('peer:' + cdc_server.lower(), nodes)
                    # verified_cdc is False, so the hostless registration on primary cannot be attributed.
                    self.assertIn('peer:' + rs_uuid, nodes)
                    self.assertNotIn('reason', edge)
                else:
                    self.assertNotIn('peer:' + cdc_server.lower(), nodes)
                    self.assertNotIn('peer:' + rs_uuid, nodes)
                    self.assertEqual(edge['reason'],
                                     'Also registered on primary as a replica (server ID 103) (estimate)')

    def test_map_hostless_registration_peer_never_uses_question_mark_label(self):
        cases = (
            ('mysql57_uuid', '5.7.44', 'SHOW REPLICAS',
             {'Server_id': '42', 'Host': '', 'Port': '3306', 'Master_id': '1', 'Slave_UUID': UUID_C},
             'peer:' + UUID_C, 3306, 'Replica without a reported address (server ID 42, port 3306)'),
            ('mysql84_uuid', '8.4.13', None,
             {'Server_Id': '42', 'Host': '', 'Port': '3306', 'Source_Id': '1', 'Replica_UUID': UUID_C},
             'peer:' + UUID_C, 3306, 'Replica without a reported address (server ID 42, port 3306)'),
            ('mysql57_no_uuid', '5.7.44', 'SHOW REPLICAS',
             {'Server_id': '42', 'Host': '', 'Port': '3306', 'Master_id': '1', 'Slave_UUID': ''},
             'peer:active:server-id:42:3306', 3306, 'Replica without a reported address (server ID 42, port 3306)'),
            ('mysql57_port_zero_omits_port', '5.7.44', 'SHOW REPLICAS',
             {'Server_id': '42', 'Host': '', 'Port': '0', 'Master_id': '1', 'Slave_UUID': UUID_C},
             'peer:' + UUID_C, None, 'Replica without a reported address (server ID 42)'),
            ('mysql84_port_null_omits_port', '8.4.13', None,
             {'Server_Id': '42', 'Host': '', 'Port': None, 'Source_Id': '1', 'Replica_UUID': ''},
             'peer:active:server-id:42', None, 'Replica without a reported address (server ID 42)'),
        )
        for name, version, err_on, row, expected_id, expected_port, expected_label in cases:
            with self.subTest(case=name):
                primary = member_fixture(UUID_A, replicas=[row])
                primary['variables']['version'] = version
                if err_on:
                    primary['error_on'] = err_on
                    primary['query_error'] = "ERROR 1064 (42000) at line 1: syntax error near 'REPLICAS'"
                with self.clients({'active.example': primary}):
                    report = self.read('map')
                nodes = self.assert_map_schema(report)
                self.assertIn(expected_id, nodes)
                peer = nodes[expected_id]
                self.assertEqual(peer['label'], expected_label)
                self.assertIsNone(peer['host'])
                self.assertEqual(peer['port'], expected_port)
                self.assertNotIn('?:3306', json.dumps(report))

    def test_map_external_source_deduplicates_peer_uuid_across_member_reads(self):
        self.save('second', 'G')
        channel = {'Source_UUID': UUID_C, 'Source_Host': 'outside.example', 'Source_Port': '3306',
                   'Replica_IO_Running': 'Yes', 'Replica_SQL_Running': 'Yes'}
        with self.clients({'active.example': member_fixture(UUID_A, channels=[channel]),
                           'second.example': member_fixture(UUID_B, channels=[dict(channel, Source_Host='OUTSIDE.EXAMPLE')])}):
            report = self.read('map')
        nodes = self.assert_map_schema(report)
        peer = nodes['peer:' + UUID_C]
        self.assertEqual(peer['seen_by'], ['active', 'second'])
        self.assertEqual(peer['server_uuid'], UUID_C)
        self.assertEqual(peer['reason'], 'Not in this group')
        self.assertEqual({(edge['from'], edge['to']) for edge in report['map']['edges']},
                         {('peer:' + UUID_C, 'active'), ('peer:' + UUID_C, 'second')})

    def test_map_external_source_without_uuid_deduplicates_case_insensitive_endpoint(self):
        self.save('second', 'G')
        channels = [{'Source_Host': host, 'Source_Port': '3307'}
                    for host in ('OUTSIDE.EXAMPLE', 'outside.example')]
        with self.clients({'active.example': member_fixture(UUID_A, channels=[channels[0]]),
                           'second.example': member_fixture(UUID_B, channels=[channels[1]])}):
            report = self.read('map')
        nodes = self.assert_map_schema(report)
        self.assertEqual(set(nodes), {'active', 'second', 'peer:outside.example:3307'})
        self.assertEqual(nodes['peer:outside.example:3307']['seen_by'], ['active', 'second'])
        self.assertIsNone(nodes['peer:outside.example:3307']['server_uuid'])

    def test_map_reported_hostname_match_is_not_identity_verification(self):
        self.save('replica', 'G')
        source = member_fixture(UUID_A)
        source['variables'].update(hostname='private-source', port='3306')
        replica = member_fixture(UUID_B, channels=[{'Source_Host': 'private-source', 'Source_Port': '3306'}])
        with self.clients({'active.example': source, 'replica.example': replica}):
            report = self.read('map')
        self.assert_map_schema(report)
        edge = report['map']['edges'][0]
        self.assertEqual((edge['from'], edge['to']), ('active', 'replica'))
        self.assertEqual(edge['evidence'], 'hostname_port')
        self.assertIs(edge['verified'], False)

    def test_map_same_innodb_cluster_name_different_group_name_stays_two_clusters(self):
        self.save('second', 'G')
        fixtures = {}
        for host, uuid, group in (('active.example', UUID_A, UUID_C), ('second.example', UUID_B, UUID_D)):
            fixtures[host] = self.gr_map_fixture(uuid, 'PRIMARY', [[uuid, host, 3306, 'ONLINE', 'PRIMARY']],
                group=group, innodb_cluster=True,
                cluster_metadata=[{'cluster_id': group, 'cluster_name': 'same display name',
                                   'primary_mode': 'pm', 'group_name': group}])
        with self.clients(fixtures):
            report = self.read('map')
        nodes = self.assert_map_schema(report)
        self.assertEqual(len(report['map']['clusters']), 2)
        self.assertNotEqual(nodes['active']['cluster_id'], nodes['second']['cluster_id'])
        self.assertEqual({cluster['name'] for cluster in report['map']['clusters']}, {'same display name'})

    def test_map_cluster_without_own_node_in_membership_never_claims_own_membership(self):
        rows = [[UUID_B, 'peer.example', 3306, 'ONLINE', 'PRIMARY']]
        with self.clients({'active.example': self.gr_map_fixture(UUID_A, 'SECONDARY', rows)}):
            report = self.read('map')
        nodes = self.assert_map_schema(report)
        self.assertIn('peer:' + UUID_B, nodes)
        self.assertEqual(nodes['peer:' + UUID_B]['host'], 'peer.example')
        self.assertEqual(nodes['peer:' + UUID_B]['seen_by'], ['active'])
        self.assertIsNone(nodes['active']['cluster_id'])
        self.assertFalse(any('active' in cluster['members'] for cluster in report['map']['clusters']))

    def test_map_loopback_report_from_remote_proxy_does_not_match_saved_local_endpoint(self):
        self.save('local', 'G', host='127.0.0.1', port=3306)
        proxy = member_fixture(proxy='proxysql', servers=[{
            'hostgroup_id': '10', 'hostname': '127.0.0.1', 'port': '3306', 'status': 'ONLINE'}])
        proxy['variables'].update(version='3.0.11', version_comment='ProxySQL Admin')
        with self.clients({'active.example': proxy, '127.0.0.1': member_fixture(UUID_B)}):
            report = self.read('map')
        nodes = self.assert_map_schema(report)
        self.assertIn('local', nodes)
        self.assertIn('peer:127.0.0.1:3306', nodes)
        self.assertEqual(report['map']['edges'][0]['to'], 'peer:127.0.0.1:3306')
        self.assertIs(report['map']['edges'][0]['verified'], False)

    def test_map_failed_member_keeps_node_without_edges_from_failed_read(self):
        self.save('failed', 'G')
        broken = member_fixture(UUID_B, error='ERROR 1045 (28000): secret_channel_password SELECT secret_sql')
        with self.clients({'active.example': member_fixture(UUID_A), 'failed.example': broken}):
            report = self.read('map')
        nodes = self.assert_map_schema(report)
        self.assertEqual(set(nodes), {'active', 'failed'})
        self.assertEqual(nodes['failed']['outcome'], 'failed')
        self.assertEqual(report['map']['edges'], [])

    def test_map_kind_change_between_identity_and_probe_discards_probe_routes(self):
        self.save('source', 'G')
        changing = member_fixture(UUID_A, probe_readyset=True, upstreams=[{
            'host': 'source.example', 'port': '3306', 'health': 'up',
            'user': 'secret_upstream_user', 'password': 'secret_channel_password'}])
        with self.clients({'active.example': changing, 'source.example': member_fixture(UUID_B)}) as observed:
            report = self.read('map')
        nodes = self.assert_map_schema(report)
        self.assertEqual(nodes['active']['outcome'], 'failed')
        self.assertEqual(nodes['source']['outcome'], 'read')
        self.assertEqual(report['map']['edges'], [])
        member = next(row for row in report['members'] if row['profile_id'] == 'active')
        self.assertIn('the connection could not be read.', member['message'])
        self.assertNotIn('identity changed', member['message'].lower())
        self.assertIn('SHOW READYSET STATUS', '\n'.join(self.statements(observed, 'active.example')))

    def test_map_uuid_change_between_identity_and_probe_discards_probe_links(self):
        self.save('source', 'G')
        changing = member_fixture(UUID_A, probe_uuid=UUID_C, channels=[{
            'Source_UUID': UUID_B, 'Source_Host': 'source.example', 'Source_Port': '3306',
            'Replica_IO_Running': 'Yes', 'Replica_SQL_Running': 'Yes'}])
        with self.clients({'active.example': changing, 'source.example': member_fixture(UUID_B)}):
            report = self.read('map')
        nodes = self.assert_map_schema(report)
        self.assertEqual(nodes['active']['outcome'], 'failed')
        self.assertEqual(nodes['source']['outcome'], 'read')
        self.assertEqual(report['map']['edges'], [])
        member = next(row for row in report['members'] if row['profile_id'] == 'active')
        self.assertIn('the connection could not be read.', member['message'])


class MapBoundsTests(unittest.TestCase):
    """Server-controlled map rows stay bounded without opening any endpoint."""

    def build(self, profiles, probes, duplicates=None):
        entries = []
        for profile in profiles:
            entry = topology_group._entry(profile)
            probe = probes.get(profile['id'], {})
            kind = probe.get('kind')
            product = ('ProxySQL Admin' if kind == 'proxysql' else
                       'Readyset' if kind == 'readyset' else 'MySQL')
            role = 'proxy' if kind == 'proxysql' else 'cache' if kind == 'readyset' else 'standalone'
            entry.update(outcome='read', engine='mysql', product=product,
                         version='3.0.11' if kind == 'proxysql' else '8.4.11',
                         role=role, state='reachable',
                         server_uuid=None if kind in ('proxysql', 'readyset') else probe.get('server_uuid'),
                         duplicate_of=(duplicates or {}).get(profile['id']))
            entries.append(entry)
        result = topology_group._build_map(entries, probes, profiles)
        schema = json.loads((ROOT / 'docs/schemas/topology-group-1.0.json').read_text(encoding='utf-8'))
        validate(result, schema['properties']['map'], root=schema)
        ids = {node['id'] for node in result['nodes']}
        self.assertTrue(all(edge['from'] in ids and edge['to'] in ids for edge in result['edges']))
        self.assertTrue(all(set(cluster['members']) <= ids for cluster in result['clusters']))
        return result

    def route(self, host='mf-galera-2', hostgroup=10):
        return {'hostname': host, 'port': '3306', 'hostgroup_id': str(hostgroup), 'status': 'ONLINE'}

    def test_unknown_backend_state_prevents_no_online_hostgroup_claim(self):
        profile = {'id': 'proxy', 'host': 'proxy.example', 'port': 6032}
        for unknown in ({'status': None}, {'status': 'FUTURE_STATE'},
                        {'status': 'ONLINE', 'status_source': 'config'}):
            with self.subTest(unknown=unknown):
                servers = [dict(self.route('shunned.example'), status='SHUNNED'),
                           dict(self.route('unknown.example'), **unknown)]
                result = self.build([profile], {'proxy': {'kind': 'proxysql', 'servers': servers}})
                self.assertEqual(len(result['edges']), 2)
                self.assertFalse(any('No ONLINE member' in edge.get('reason', '') for edge in result['edges']))
                states = {edge['to']: edge['state'] for edge in result['edges']}
                self.assertEqual(states['peer:shunned.example:3306'], 'SHUNNED')
                self.assertIsNone(states['peer:unknown.example:3306'])

    def test_truncated_backends_prevent_no_online_hostgroup_claim(self):
        profile = {'id': 'proxy', 'host': 'proxy.example', 'port': 6032}
        # An ONLINE member beyond the shared traversal cap cannot justify
        # reporting every visible SHUNNED route's hostgroup as unavailable.
        # A preceding channel can consume part of that shared row budget.
        for preceding_rows in (0, 1):
            with self.subTest(preceding_rows=preceding_rows):
                servers = [dict(self.route(), status='SHUNNED')
                           for unused in range(topology_group.MAP_ROWS_PER_MEMBER - preceding_rows)]
                servers.append(self.route('online-after-cap.example'))
                probe = {'kind': 'proxysql', 'servers': servers, 'channels': [{}] * preceding_rows}
                result = self.build([profile], {'proxy': probe})
                self.assertEqual(len(result['edges']), 1)
                self.assertEqual(result['edges'][0]['state'], 'SHUNNED')
                self.assertNotIn('No ONLINE member', result['edges'][0].get('reason', ''))
                self.assertTrue(any('rows capped' in note['message'] for note in result['notes']))

    def test_ssh_loopback_proxy_route_cannot_match_a_local_saved_server(self):
        profiles = [{'id': 'proxy', 'host': '127.0.0.1', 'port': 6032, 'ssh_host': 'remote.example'},
                    {'id': 'local', 'host': '127.0.0.1', 'port': 3306}]
        result = self.build(profiles, {'proxy': {'kind': 'proxysql', 'servers': [self.route('127.0.0.1')]},
                                       'local': {'kind': 'database', 'server_uuid': UUID_A}})
        self.assertEqual(len(result['edges']), 1)
        self.assertEqual(result['edges'][0]['to'], 'peer:127.0.0.1:3306')
        self.assertFalse(result['edges'][0]['verified'])

    def test_ssh_ipv4_mapped_loopback_proxy_route_does_not_cross_ssh_hosts(self):
        for db_host in ('127.0.0.1', '::ffff:127.0.0.1'):
            for backend in ('::ffff:127.0.0.1', '::ffff:7f00:1', '[::1]'):
                with self.subTest(db_host=db_host, backend=backend):
                    profiles = [
                        {'id': 'proxy', 'host': '127.0.0.1', 'port': 6032, 'ssh_host': 'ssh-a.example'},
                        {'id': 'db_b', 'host': db_host, 'port': 3306, 'ssh_host': 'ssh-b.example'},
                    ]
                    probes = {
                        'proxy': {'kind': 'proxysql', 'servers': [self.route(backend)]},
                        'db_b': {'kind': 'database', 'server_uuid': UUID_A, 'hostname': backend, 'port': 3306},
                    }
                    result = self.build(profiles, probes)
                    routes = [edge for edge in result['edges'] if edge['kind'] == 'proxy_route']
                    self.assertEqual(len(routes), 1)
                    self.assertNotEqual(routes[0]['to'], 'db_b')
                    self.assertTrue(routes[0]['to'].startswith('peer:'))
                    self.assertIsNone(routes[0]['evidence'])
                    self.assertFalse(routes[0]['verified'])

    def test_ssh_loopback_proxy_route_does_not_match_database_on_same_ssh_host(self):
        for backend in ('::ffff:127.0.0.1', '::ffff:7f00:1', '[::1]', '127.0.0.1', 'localhost'):
            with self.subTest(backend=backend):
                profiles = [
                    {'id': 'proxy', 'host': '127.0.0.1', 'port': 6032, 'ssh_host': 'ssh-a.example'},
                    {'id': 'db_a', 'host': '127.0.0.1', 'port': 3306, 'ssh_host': 'ssh-a.example'},
                    {'id': 'db_b', 'host': '127.0.0.1', 'port': 3306, 'ssh_host': 'ssh-b.example'},
                    {'id': 'local', 'host': '127.0.0.1', 'port': 3306},
                ]
                probes = {
                    'proxy': {'kind': 'proxysql', 'servers': [self.route(backend)]},
                    'db_a': {'kind': 'database', 'server_uuid': UUID_A, 'hostname': 'db-a', 'port': 3306},
                    'db_b': {'kind': 'database', 'server_uuid': UUID_B, 'hostname': 'db-b', 'port': 3306},
                    'local': {'kind': 'database', 'server_uuid': UUID_C, 'hostname': 'local', 'port': 3306},
                }
                result = self.build(profiles, probes)
                routes = [edge for edge in result['edges'] if edge['kind'] == 'proxy_route']
                self.assertEqual(len(routes), 1)
                self.assertNotIn(routes[0]['to'], ('db_a', 'db_b', 'local'))
                self.assertTrue(routes[0]['to'].startswith('peer:'))
                self.assertIsNone(routes[0]['evidence'])
                self.assertFalse(routes[0]['verified'])

    def test_bastion_proxysql_loopback_backend_does_not_match_bastion_local_database(self):
        # ProxySQL at 10.0.0.5:6033 via bastion A reports 127.0.0.1:3306 (10.0.0.5's
        # own machine); a DB saved as 127.0.0.1:3306 via bastion A is bastion A itself.
        profiles = [
            {'id': 'proxy', 'host': '10.0.0.5', 'port': 6033, 'ssh_host': 'bastion-a.example'},
            {'id': 'bastion_db', 'host': '127.0.0.1', 'port': 3306, 'ssh_host': 'bastion-a.example'},
        ]
        probes = {
            'proxy': {'kind': 'proxysql', 'servers': [self.route('127.0.0.1')]},
            'bastion_db': {'kind': 'database', 'server_uuid': UUID_A, 'hostname': 'bastion-a', 'port': 3306},
        }
        result = self.build(profiles, probes)
        routes = [edge for edge in result['edges'] if edge['kind'] == 'proxy_route']
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]['to'], 'peer:127.0.0.1:3306')
        self.assertNotEqual(routes[0]['to'], 'bastion_db')
        self.assertIsNone(routes[0]['evidence'])
        self.assertFalse(routes[0]['verified'])

    def test_map_ipv4_mapped_non_loopback_proxy_route_matches_ipv4_member(self):
        profiles = [
            {'id': 'proxy', 'host': 'proxy.example', 'port': 6032},
            {'id': 'db', 'host': '10.0.0.5', 'port': 3306},
        ]
        probes = {
            'proxy': {'kind': 'proxysql', 'servers': [self.route('::ffff:10.0.0.5')]},
            'db': {'kind': 'database', 'server_uuid': UUID_A, 'hostname': 'db-v4', 'port': 3306},
        }
        result = self.build(profiles, probes)
        routes = [edge for edge in result['edges'] if edge['kind'] == 'proxy_route']
        self.assertEqual([(r['from'], r['to'], r['evidence'], r['verified']) for r in routes],
                         [('proxy', 'db', 'endpoint', False)])
        self.assertFalse(any(node['kind'] == 'peer' for node in result['nodes']))

    def test_alias_and_distinct_server_at_same_endpoint_over_ssh_stay_unmatched(self):
        profiles = [{'id': 'proxy', 'host': 'proxy.example', 'port': 6032},
                    {'id': 'canonical', 'host': 'db-a.example', 'port': 3306},
                    {'id': 'alias', 'host': 'shared.example', 'port': 3306, 'ssh_host': 'jump-a.example'},
                    {'id': 'other', 'host': 'shared.example', 'port': 3306, 'ssh_host': 'jump-b.example'}]
        probes = {'proxy': {'kind': 'proxysql', 'servers': [self.route('shared.example')]},
                  'canonical': {'kind': 'database', 'server_uuid': UUID_A, 'hostname': 'db-a', 'port': 3306},
                  'alias': {'kind': 'database', 'server_uuid': UUID_A, 'hostname': 'db-a', 'port': 3306},
                  'other': {'kind': 'database', 'server_uuid': UUID_B, 'hostname': 'db-b', 'port': 3306}}
        result = self.build(profiles, probes, duplicates={'alias': 'canonical'})
        routes = [edge for edge in result['edges'] if edge['kind'] == 'proxy_route']
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]['to'], 'peer:shared.example:3306')
        self.assertIsNone(routes[0]['evidence'])
        self.assertFalse(routes[0]['verified'])
        self.assertEqual([edge['to'] for edge in result['edges'] if edge['kind'] == 'alias'], ['canonical'])

    def test_saved_endpoint_aliases_cannot_be_displaced_by_another_servers_hostname(self):
        profiles = [{'id': 'proxy', 'host': 'proxy.example', 'port': 6032},
                    {'id': 'canonical', 'host': 'shared.example', 'port': 3306},
                    {'id': 'alias', 'host': 'shared.example', 'port': 3306, 'ssh_host': 'jump.example'},
                    {'id': 'other', 'host': 'other.example', 'port': 3306}]
        probes = {'proxy': {'kind': 'proxysql', 'servers': [self.route('shared.example')]},
                  'canonical': {'kind': 'database', 'server_uuid': UUID_A, 'hostname': 'db-a', 'port': 3306},
                  'alias': {'kind': 'database', 'server_uuid': UUID_A, 'hostname': 'db-a', 'port': 3306},
                  'other': {'kind': 'database', 'server_uuid': UUID_B, 'hostname': 'shared.example', 'port': 3306}}
        result = self.build(profiles, probes, duplicates={'alias': 'canonical'})
        routes = [edge for edge in result['edges'] if edge['kind'] == 'proxy_route']
        self.assertEqual([(route['to'], route['evidence'], route['verified']) for route in routes],
                         [('canonical', 'endpoint', False)])

    def test_conflicting_saved_endpoints_ignore_unique_hostname_fallback_with_or_without_alias(self):
        for alias in (False, True):
            with self.subTest(alias=alias):
                profiles = [{'id': 'proxy', 'host': 'proxy.example', 'port': 6032},
                            {'id': 'canonical', 'host': 'db-a.example', 'port': 3306},
                            {'id': 'first', 'host': 'shared.example', 'port': 3306, 'ssh_host': 'jump-a.example'},
                            {'id': 'other', 'host': 'shared.example', 'port': 3306, 'ssh_host': 'jump-b.example'}]
                probes = {'proxy': {'kind': 'proxysql', 'servers': [self.route('shared.example')]},
                          'canonical': {'kind': 'database', 'server_uuid': UUID_A, 'hostname': 'db-a', 'port': 3306},
                          'first': {'kind': 'database', 'server_uuid': UUID_A, 'hostname': 'db-a', 'port': 3306},
                          'other': {'kind': 'database', 'server_uuid': UUID_B, 'hostname': 'shared.example', 'port': 3306}}
                result = self.build(profiles, probes, duplicates={'first': 'canonical'} if alias else {})
                routes = [edge for edge in result['edges'] if edge['kind'] == 'proxy_route']
                self.assertEqual([(route['to'], route['evidence']) for route in routes],
                                 [('peer:shared.example:3306', None)])

    def test_peer_cap_and_exact_boundary(self):
        profile = {'id': 'proxy', 'host': 'proxy.example', 'port': 6032}
        for count in (256, 257):
            with self.subTest(count=count):
                result = self.build([profile], {'proxy': {'kind': 'proxysql',
                    'servers': [self.route('peer-%d.example' % index) for index in range(count)]}})
                self.assertEqual(len(result['nodes']), 257)
                self.assertEqual(len(result['edges']), 256)
                self.assertEqual(any('peers capped' in note['message'] for note in result['notes']), count > 256)

    def test_edge_cap_with_many_hostgroups_for_one_live_backend_shape(self):
        profile = {'id': 'proxy', 'host': 'proxy.example', 'port': 6032}
        for count in (1024, 1025):
            with self.subTest(count=count):
                result = self.build([profile], {'proxy': {'kind': 'proxysql',
                    'servers': [self.route(hostgroup=index) for index in range(count)]}})
                self.assertEqual(len(result['nodes']), 2)
                self.assertEqual(len(result['edges']), 1024)
                self.assertEqual(any('edges capped' in note['message'] for note in result['notes']), count > 1024)

    def test_global_node_cap_with_sixteen_members(self):
        profiles = [{'id': 'proxy-%d' % index, 'host': 'proxy-%d.example' % index, 'port': 6032}
                    for index in range(16)]
        probes = {profile['id']: {'kind': 'proxysql', 'servers': [
            self.route('%s-peer-%d.example' % (profile['id'], index)) for index in range(256)]}
            for profile in profiles}
        result = self.build(profiles, probes)
        self.assertEqual(len(result['nodes']), 4096)
        self.assertTrue(any('nodes capped at 4096' in note['message'] for note in result['notes']))

    def test_row_traversal_is_bounded_even_for_repeated_reported_routes(self):
        consumed = []

        def rows():
            for index in range(100000):
                consumed.append(index)
                yield self.route()

        result = self.build([{'id': 'proxy', 'host': 'proxy.example', 'port': 6032}],
                            {'proxy': {'kind': 'proxysql', 'servers': rows()}})
        self.assertEqual(len(consumed), topology_group.MAP_ROWS_PER_MEMBER + 1)
        self.assertEqual(len(result['edges']), 1)
        self.assertEqual(len(result['nodes']), 2)
        self.assertTrue(any('rows capped' in note['message'] for note in result['notes']))

    def test_readyset_attribution_cost_long_hosts_and_edge_reason_are_bounded(self):
        from myflames import topology_links
        shared_uuid = '11111111-2222-3333-4444-555555555555'
        long_pad = 'a' * 65536
        call_counts = {}

        for n_rs in (2, 15):
            for dump_kind in ('long_hosts', 'multi_host_uuid', 'single_host_match'):
                with self.subTest(n_rs=n_rs, dump_kind=dump_kind):
                    profiles = [{'id': 's', 'name': 'source', 'host': 'source.example', 'port': 3306, 'group': 'G'}]
                    for i in range(n_rs):
                        profiles.append({'id': 'r%d' % i, 'name': 'rs%d' % i, 'host': 'rs%d.example' % i,
                                         'port': 3307, 'group': 'G'})
                    replicas = [{'host': 'h%d.example' % j, 'port': 3306, 'uuid': shared_uuid, 'server_id': j + 2}
                                for j in range(2048)]
                    if dump_kind == 'long_hosts':
                        dump_rows = [{'uuid': shared_uuid, 'host': '%s%d' % (long_pad, k), 'user': 'u', 'class': 'none'}
                                     for k in range(256)]
                    elif dump_kind == 'multi_host_uuid':
                        dump_rows = [{'uuid': shared_uuid, 'host': 'd%d.example' % k, 'user': 'u', 'class': 'none'}
                                     for k in range(256)]
                    else:
                        dump_rows = [{'uuid': shared_uuid, 'host': 'rs0.example', 'user': 'u', 'class': 'none'}]
                    probes = {
                        's': {'kind': 'database', 'server_uuid': UUID_A, 'channels': [], 'cluster_members': [],
                              'replicas': replicas, 'dump_threads': dump_rows, 'dump_read_failed': False},
                    }
                    for i in range(n_rs):
                        probes['r%d' % i] = {
                            'kind': 'readyset', 'port_role': 'adapter', 'proxied_server_uuid': None,
                            'replication_status': 'Running',
                            'upstreams': [{'host': 'source.example', 'port': 3306, 'purpose': 'cdc',
                                           'active_cdc': '1', 'user': 'u'}],
                        }
                    with mock.patch.object(topology_links, '_host_key',
                                           wraps=topology_links._host_key) as host_key_spy:
                        result = self.build(profiles, probes)
                    calls = host_key_spy.call_count
                    call_counts[(n_rs, dump_kind)] = calls
                    # Strictly linear in rows + dump rows + Readyset members (never O(n_rs * n_replicas * n_dump)).
                    self.assertLessEqual(calls, 10 * (2048 + 256 + n_rs), (n_rs, dump_kind, calls))
                    if dump_kind == 'long_hosts':
                        for call in host_key_spy.call_args_list:
                            arg = call[0][0] if call[0] else ''
                            self.assertFalse(isinstance(arg, str) and len(arg) > 255,
                                             'overlong dump hosts must be dropped before _host_key')
                    if dump_kind in ('long_hosts', 'multi_host_uuid'):
                        # Rejected long hosts and multi-host UUIDs never attribute to any Readyset edge.
                        self.assertIn('peer:' + shared_uuid, {n['id'] for n in result['nodes']})
                        for edge in result['edges']:
                            if edge['kind'] == 'cache_upstream':
                                self.assertNotIn('reason', edge)
                    else:
                        # Claim-once: r0 claims at most one registration; remaining rows stay a peer.
                        r0_edge = next(e for e in result['edges'] if e['from'] == 'r0' and e['to'] == 's')
                        self.assertEqual(r0_edge['reason'], 'Also registered on source as a replica (server ID 2)')
                    for edge in result['edges']:
                        self.assertLessEqual(len(edge.get('reason') or ''), 512)

        # Scaling from 2 to 15 Readyset members adds O(n_rs) host_key calls, not O(n_rs * n_replicas).
        for dump_kind in ('long_hosts', 'multi_host_uuid', 'single_host_match'):
            delta = call_counts[(15, dump_kind)] - call_counts[(2, dump_kind)]
            self.assertLess(delta, 200, (dump_kind, delta))

        # Overlong dump usernames (> 32 chars) are dropped and cannot match a Readyset member.
        long_user = 'u' * 33
        self.assertIsNone(topology_group._map_user(long_user))
        self.assertEqual(topology_group._map_user('u' * 32), 'u' * 32)
        long_user_res = self.build(
            [{'id': 's', 'name': 'source', 'host': 'source.example', 'port': 3306, 'group': 'G'},
             {'id': 'r0', 'name': 'rs0', 'host': 'rs0.example', 'port': 3307, 'group': 'G'}],
            {'s': {'kind': 'database', 'server_uuid': UUID_A, 'channels': [], 'cluster_members': [],
                   'replicas': [{'host': 'rs0', 'port': 3307, 'uuid': shared_uuid, 'server_id': 2}],
                   'dump_threads': [{'uuid': shared_uuid, 'host': 'rs0.example', 'user': long_user, 'class': 'none'}],
                   'dump_read_failed': False},
             'r0': {'kind': 'readyset', 'port_role': 'adapter', 'proxied_server_uuid': UUID_A,
                    'replication_status': 'Running',
                    'upstreams': [{'host': 'source.example', 'port': 3306, 'purpose': 'cdc, proxy',
                                   'active_cdc': '1', 'user': 'u' * 32}]}},
        )
        self.assertIn('peer:' + shared_uuid, {n['id'] for n in long_user_res['nodes']})

        # Defense-in-depth cap on notes per edge ("and N more registrations").
        edges = {'k': {'from': 'r0', 'to': 's', 'kind': 'cache_upstream', 'evidence': 'proxied_identity',
                       'verified': True, 'state': 'up', 'label': 'Upstream'}}
        note_state = {}
        for sid in range(1, 25):
            topology_group._attach_replica_note(edges, 'k', 'source', sid, estimate=False, note_state=note_state)
        reason = edges['k']['reason']
        self.assertIn('and %d more registrations' % (24 - topology_group.MAP_NOTES_PER_EDGE), reason)
        self.assertEqual(reason.count('Also registered on source'), topology_group.MAP_NOTES_PER_EDGE)
        self.assertLess(len(reason), 400)


if __name__ == '__main__':
    unittest.main()
