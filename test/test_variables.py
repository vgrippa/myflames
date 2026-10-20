"""Server variable changes (myflames/variables.py): SET GLOBAL, SET PERSIST,
SET PERSIST_ONLY, RESET PERSIST and SET SESSION, from the CLI
(``myflames config set`` / ``reset-persist``) and the workspace endpoints.

Replays in fixtures/perf/variable-<server>-<scenario>.json were recorded
live on throwaway MySQL 8.4.11 and MariaDB 11.4 containers (every statement
myflames sent and the client's answer): SET GLOBAL max_connections, SET
PERSIST then a container restart (``restart`` shows the value survived), SET
PERSIST_ONLY of the read-only innodb_log_file_size (``restart-persist-only``
shows it applied), RESET PERSIST, SET SESSION, the server's 1238 and 1227
refusals, a value changed between plan and apply, and the rounding both
engines apply (buffer pool chunks, MariaDB's block size).
Server identity and platform rows, and the IS NULL/HEX persisted-value
projection, are synthetic extensions documented in each replay fixture.
"""
import copy
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))

from _perf_replay import FakeFactory, ReplayConn, fixture, run_cli  # noqa: E402
from myflames import dbops, variables as V  # noqa: E402

TAG = V.TAG


def replay(server, scenario, edit=None):
    data = fixture('variable', '%s-%s' % (server, scenario))
    return ReplayConn(data['calls'], edit=edit), data


def plan_from(server, scenario):
    conn, data = replay(server, scenario)
    args = data['args']
    return conn, V.plan_change(conn, args['name'], args['value'], args['mode']), data


def assert_reads_only(case, conn):
    """Every statement sent was a tagged SHOW or SELECT, kept with --comments."""
    case.assertTrue(conn.calls)
    for sql, flags in conn.calls:
        for part in sql.split('; '):
            case.assertRegex(part, r'^/\*myflames:variable\*/ (SHOW|SELECT) ', sql)
        case.assertIn('--comments', flags)


def mysql_variable(name, **changes):
    """A variable-1.0 for a MySQL 8.4 server built from a fake read."""
    info = {'name': name, 'engine': 'mysql', 'server_version': '8.4.11', 'version_tuple': (8, 4, 11),
            'in_global': True, 'in_session': True, 'global_value': '151', 'session_value': '151',
            'persisted_globals_load': 'ON', 'related': {}, 'catalog': None, 'platform': 'Linux',
            'server_identity': {'server_uuid': '11111111-2222-3333-4444-555555555555'},
            'info': {'source': 'COMPILED', 'path': '', 'min': '1', 'max': '100000', 'set_time': None,
                     'set_user': None, 'set_host': None},
            'persisted': {'present': False, 'value': None, 'read': True}, 'metadata': None, 'notes': []}
    info.update(changes)
    return V.describe(info)


def mariadb_variable(name, **catalog):
    row = {'name': name.upper(), 'session_value': None, 'global_value': '151', 'origin': 'COMPILE-TIME',
           'default': '151', 'scope': 'GLOBAL', 'type': 'BIGINT UNSIGNED', 'min': '10', 'max': '100000',
           'block': '1', 'values': None, 'read_only': 'NO', 'path': None, 'comment': ''}
    row.update(catalog)
    info = {'name': name, 'engine': 'mariadb', 'server_version': '11.4.9-MariaDB', 'version_tuple': (11, 4, 9),
            'in_global': row['scope'] != 'SESSION ONLY', 'in_session': True, 'global_value': row['global_value'],
            'session_value': row['session_value'], 'persisted_globals_load': None, 'related': {}, 'catalog': row,
            'server_identity': {'server_id': '1', 'hostname': 'fixture-db', 'version': '11.4.9-MariaDB'},
            'info': None, 'persisted': None, 'metadata': None, 'notes': []}
    return V.describe(info)


class TestNamesAndValues(unittest.TestCase):
    def test_regression_string_value_preserves_surrounding_spaces(self):
        for engine in ('mysql', 'mariadb'):
            for raw in (' +-><()~*:""&|', ' +-><()~*:""&| ', ' ', ' DEFAULT '):
                with self.subTest(engine=engine, raw=raw):
                    variable = {'name': 'ft_boolean_syntax', 'type': 'string', 'engine': engine}
                    value = V.normalize_value(variable, raw)
                    self.assertEqual(value['problems'], [])
                    self.assertEqual(value['value'], raw)
                    self.assertEqual(value['display'], raw)
                    self.assertEqual(value['literal'], V._text_literal(raw))
                    self.assertFalse(value['default'])

    def test_names(self):
        self.assertEqual(V.check_name('Max_Connections'), 'max_connections')
        self.assertEqual(V.check_name('@@GLOBAL.sort_buffer_size'), 'sort_buffer_size')
        self.assertEqual(V.check_name('innodb-buffer-pool-size'), 'innodb_buffer_pool_size')
        self.assertEqual(V.check_name('validate_password.length'), 'validate_password.length')
        for bad in ('', 'bad name', "x'; DROP TABLE t; --", 'a.b.c', None, 'x' * 65):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                V.check_name(bad)
        with self.assertRaises(ValueError):
            V.check_mode('persistent')

    def test_byte_sizes_are_converted(self):
        v = mysql_variable('innodb_buffer_pool_size', global_value='134217728',
                           info={'source': 'COMPILED', 'min': '5242880', 'max': '9223372036854775807'})
        for raw, number in (('1G', 1 << 30), ('512m', 512 << 20), ('1.5G', 3 << 29), ('2GiB', 2 << 30),
                            ('268435456', 268435456), (268435456, 268435456)):
            with self.subTest(raw=raw):
                out = V.normalize_value(v, raw)
                self.assertEqual(out['problems'], [])
                self.assertEqual((out['literal'], out['number']), (str(number), number))
        self.assertEqual(V.normalize_value(v, '1.3K')['problems'][0]['code'], 'invalid_value')
        self.assertEqual(V.normalize_value(v, '1M')['problems'][0]['code'], 'out_of_range')

    def test_integers_take_no_suffix_and_respect_the_range(self):
        v = mysql_variable('max_connections')
        self.assertEqual(V.normalize_value(v, '300')['literal'], '300')
        self.assertEqual(V.normalize_value(v, '1K')['problems'][0]['code'], 'invalid_value')
        self.assertEqual(V.normalize_value(v, '0')['problems'][0]['code'], 'out_of_range')
        self.assertEqual(V.normalize_value(v, '100001')['problems'][0]['code'], 'out_of_range')
        self.assertEqual(V.normalize_value(v, 'abc')['problems'][0]['code'], 'invalid_value')

    def test_byte_conversion_uses_exact_decimal_arithmetic(self):
        v = mysql_variable('innodb_buffer_pool_size', info={'min': '0', 'max': '18446744073709551615'})
        # Binary floats round both of these decimals near 2**53.
        exact = '8388608.000000000931322574615478515625G'
        converted = V.normalize_value(v, exact)
        self.assertEqual(converted['problems'], [])
        self.assertEqual(converted['number'], (1 << 53) + 1)
        self.assertEqual(converted['literal'], str((1 << 53) + 1))
        fractional = V.normalize_value(v, '8388608.0000000000000000001G')
        self.assertTrue(fractional['problems'], 'a fractional byte must not be rounded into an integer')

    def test_huge_and_nonfinite_numbers_are_refused_without_overflow(self):
        sizes = mysql_variable('innodb_buffer_pool_size', info={'min': '0', 'max': '18446744073709551615'})
        for raw in ('9' * 400 + 'G', '1e309'):
            with self.subTest(raw=raw[:20]):
                normalized = V.normalize_value(sizes, raw)
                self.assertTrue(normalized['problems'])
                self.assertIsNone(normalized['literal'])
                json.dumps(normalized, allow_nan=False)
        double = mysql_variable('long_query_time')
        for raw in ('1e309', float('inf'), float('nan')):
            with self.subTest(raw=raw):
                self.assertTrue(V.normalize_value(double, raw)['problems'])

    def test_optimizer_switch_flags_are_checked_against_the_current_value(self):
        v = mysql_variable('optimizer_switch', global_value='index_merge=on,hash_join=on')
        self.assertEqual(V.normalize_value(v, 'index_merge=off')['literal'], "'index_merge=off'")
        for raw in ('invented_flag=on', 'index_merge=maybe', 'index_merge=off;DROP TABLE t'):
            with self.subTest(raw=raw):
                self.assertTrue(V.normalize_value(v, raw)['problems'])
        self.assertTrue(V.build_plan(v, 'invented_flag=on', 'persist_only')['problems'])

    def test_flush_method_enum_uses_the_server_platform(self):
        windows = mysql_variable('innodb_flush_method', global_value='unbuffered',
                                 platform='Win64')
        unix = mysql_variable('innodb_flush_method', global_value='fsync',
                              platform='Linux')
        for raw in ('normal', 'unbuffered'):
            with self.subTest(raw=raw):
                self.assertFalse(V.normalize_value(windows, raw)['problems'])
                self.assertTrue(V.normalize_value(unix, raw)['problems'])
        self.assertFalse(V.normalize_value(unix, 'O_DIRECT')['problems'])
        self.assertTrue(V.normalize_value(windows, 'O_DIRECT')['problems'])

    def test_explicit_null_is_distinct_from_default_and_the_string_null(self):
        for v in (mysql_variable('character_set_results', global_value='utf8mb4', session_value='utf8mb4'),
                  mariadb_variable('character_set_results', type='VARCHAR', scope='SESSION',
                                   global_value='utf8mb4', session_value='utf8mb4', min=None, max=None)):
            with self.subTest(engine=v['engine']):
                null = V.normalize_value(v, None)
                self.assertEqual((null['literal'], null['value']), ('NULL', None))
                self.assertTrue(null['sql_null'])
                self.assertFalse(null['default'])
                default = V.normalize_value(v, 'DEFAULT')
                self.assertTrue(default['default'])
                self.assertFalse(default['sql_null'])
                text = V.normalize_value(v, 'NULL')
                self.assertEqual((text['literal'], text['value']), ("'NULL'", 'NULL'))
                self.assertFalse(text['sql_null'])
                self.assertFalse(text['default'])
                plan = V.build_plan(v, None, 'session')
                self.assertEqual(plan['status'], 'ready')
                self.assertIsNone(plan['requested'])
                self.assertEqual(plan['statement'], 'SET SESSION character_set_results = NULL')

    def test_quoting_per_type(self):
        boolean = mysql_variable('slow_query_log', global_value='OFF')
        for raw in ('on', '1', 'TRUE', True):
            self.assertEqual(V.normalize_value(boolean, raw)['literal'], 'ON')
        for raw in ('off', '0', 'false', False):
            self.assertEqual(V.normalize_value(boolean, raw)['literal'], 'OFF')
        self.assertTrue(V.normalize_value(boolean, 'maybe')['problems'])
        enum = mysql_variable('transaction_isolation', global_value='REPEATABLE-READ')
        self.assertEqual(V.normalize_value(enum, 'read-committed')['literal'], "'READ-COMMITTED'")
        self.assertTrue(V.normalize_value(enum, 'DIRTY')['problems'])
        set_ = mysql_variable('log_output', global_value='FILE')
        self.assertEqual(V.normalize_value(set_, 'file, table')['literal'], "'FILE,TABLE'")
        self.assertTrue(V.normalize_value(set_, 'FILE,SYSLOG')['problems'])
        double = mysql_variable('long_query_time', global_value='10.000000')
        self.assertEqual(V.normalize_value(double, '2.50')['literal'], '2.5')
        self.assertEqual(V.normalize_value(double, '-1')['problems'][0]['code'], 'out_of_range')
        self.assertEqual(V.normalize_value(mysql_variable('max_connections'), 'default')['literal'], 'DEFAULT')

    def test_mysql_log_output_requires_a_nonempty_set(self):
        v = mysql_variable('log_output', global_value='FILE')
        for raw in ('', '   ', ',', ' , , '):
            with self.subTest(raw=raw):
                self.assertTrue(V.normalize_value(v, raw)['problems'])
                self.assertEqual(V.build_plan(v, raw, 'persist_only')['status'], 'refused')
        for raw in ('NONE', 'FILE', 'FILE,TABLE'):
            with self.subTest(raw=raw):
                self.assertEqual(V.build_plan(v, raw, 'persist_only')['status'], 'ready')

    def test_text_never_needs_escaping(self):
        """Anything beyond plain characters becomes a hex literal, so neither
        backslash reading nor a quote can change the statement."""
        v = mariadb_variable('init_connect', type='VARCHAR', min=None, max=None, block=None, global_value='')
        self.assertEqual(V.normalize_value(v, 'SET NAMES utf8mb4')['literal'], "'SET NAMES utf8mb4'")
        tricky = V.normalize_value(v, "x'; SET GLOBAL read_only=0; -- \\")['literal']
        self.assertTrue(tricky.startswith("CONVERT(X'") and tricky.endswith("' USING utf8mb4)"), tricky)
        self.assertNotIn(';', tricky)
        self.assertEqual(V.statement_for('init_connect', 'global', tricky), 'SET GLOBAL init_connect = ' + tricky)
        verdict = dbops.classify(V.statement_for('init_connect', 'global', tricky))
        self.assertEqual((verdict['count'], verdict['kind']), (1, 'risky'))

    def test_statements_per_mode(self):
        self.assertEqual(V.statement_for('max_connections', 'global', '300'), 'SET GLOBAL max_connections = 300')
        self.assertEqual(V.statement_for('max_connections', 'persist', '300'), 'SET PERSIST max_connections = 300')
        self.assertEqual(V.statement_for('innodb_log_file_size', 'persist_only', '67108864'),
                         'SET PERSIST_ONLY innodb_log_file_size = 67108864')
        self.assertEqual(V.statement_for('sort_buffer_size', 'session', '1048576'),
                         'SET SESSION sort_buffer_size = 1048576')
        self.assertEqual(V.statement_for('max_connections', 'reset_persist'), 'RESET PERSIST max_connections')
        self.assertEqual(V.config_line('max_connections', '300'), 'max_connections = 300')
        self.assertEqual(V.config_line('init_connect', 'SET NAMES utf8mb4'), 'init_connect = "SET NAMES utf8mb4"')
        self.assertIsNone(V.config_line('max_connections', None))

    def test_classification_follows_dbops(self):
        """SET GLOBAL and PERSIST are risky (confirmed on both connection
        types); SET SESSION follows dbops' variable lists."""
        kinds = {mode: dbops.classify(V.representative_statement(name, mode))['kind'] for name, mode in (
            ('max_connections', 'global'), ('max_connections', 'persist'), ('max_connections', 'persist_only'),
            ('max_connections', 'reset_persist'))}
        self.assertEqual(set(kinds.values()), {'risky'})
        self.assertEqual(dbops.classify(V.representative_statement('sort_buffer_size', 'session'))['kind'], 'write')
        self.assertEqual(dbops.classify(V.representative_statement('max_execution_time', 'session'))['kind'], 'read')
        self.assertEqual(dbops.classify(V.representative_statement('sql_log_bin', 'session'))['kind'], 'risky')
        development = dbops.decide(dbops.classify(V.representative_statement('sort_buffer_size', 'session')),
                                   'development')
        self.assertEqual(development['action'], 'allow')
        self.assertEqual(dbops.decide(dbops.classify(V.representative_statement('max_connections', 'global')),
                                      'development')['action'], 'confirm')


class TestModesAndPlans(unittest.TestCase):
    def test_persist_only_requires_a_known_type_range_and_enum(self):
        cases = (
            mysql_variable('unknown_plugin_var'),
            mysql_variable('max_connections', info={'source': 'COMPILED', 'min': None, 'max': '100000'}),
            mysql_variable('max_connections', info={'source': 'COMPILED', 'min': '1', 'max': None}),
            dict(mysql_variable('transaction_isolation', global_value='REPEATABLE-READ'), values=[]),
        )
        for v in cases:
            with self.subTest(name=v['name'], type=v['type'], minimum=v['min'], values=v['values']):
                plan = V.build_plan(v, '300', 'persist_only')
                self.assertEqual(plan['status'], 'refused')
                self.assertTrue(plan['problems'])

    def test_persist_only_cannot_enable_an_unavailable_hypergraph_optimizer(self):
        v = mysql_variable('optimizer_switch', global_value='index_merge=on,hypergraph_optimizer=off')
        for raw in ('hypergraph_optimizer=on', 'hypergraph_optimizer=default'):
            with self.subTest(raw=raw):
                self.assertEqual(V.build_plan(v, raw, 'persist_only')['status'], 'refused')
        self.assertEqual(V.build_plan(v, 'hypergraph_optimizer=off', 'persist_only')['status'], 'ready')
        self.assertEqual(V.build_plan(v, 'index_merge=off', 'persist_only')['status'], 'ready')

    def test_missing_server_identity_refuses_a_plan(self):
        for v in (mysql_variable('max_connections', server_identity=None),
                  dict(mariadb_variable('max_connections'), server_identity=None)):
            with self.subTest(engine=v['engine']):
                plan = V.build_plan(v, '300', 'global')
                self.assertEqual(plan['status'], 'refused')
                self.assertTrue(plan['problems'])

    def test_persisted_read_failure_is_not_an_absent_entry(self):
        for persisted in (None, {'read': False, 'present': False, 'value': None}):
            v = mysql_variable('max_connections', persisted=persisted)
            self.assertEqual(v['persisted']['state'], 'unreadable')
            self.assertFalse(v['persisted']['read'])
            self.assertIsNone(v['persisted']['present'])
            for mode in ('persist', 'persist_only', 'reset_persist'):
                with self.subTest(mode=mode, persisted=persisted):
                    self.assertEqual(V.build_plan(v, '300', mode)['status'], 'refused')

    def test_absent_null_and_empty_persisted_entries_remain_distinct(self):
        for present, value, state in ((False, None, 'absent'), (True, None, 'present'),
                                      (True, '', 'present'), (True, 'NULL', 'present')):
            with self.subTest(present=present, value=value):
                v = mysql_variable('character_set_results', global_value='utf8mb4', session_value='utf8mb4',
                                   persisted={'present': present, 'value': value, 'read': True})
                self.assertEqual((v['persisted']['state'], v['persisted']['present'], v['persisted']['value']),
                                 (state, present, value))
                plan = V.build_plan(v, 'utf8mb4', 'persist')
                for snapshot in (plan['before']['persisted_state'], plan['expect']['persisted_state']):
                    self.assertEqual((snapshot['state'], snapshot['present'], snapshot['value']),
                                     (state, present, value))
                if present:
                    literal = 'NULL' if value is None else "'%s'" % value
                    self.assertIn('SET PERSIST_ONLY character_set_results = %s;' % literal, plan['undo'])
                    reset = V.build_plan(v, None, 'reset_persist')
                    self.assertEqual(reset['undo'], ['SET PERSIST_ONLY character_set_results = %s;' % literal])
                else:
                    self.assertIn('RESET PERSIST character_set_results;', plan['undo'])

    def test_null_current_value_has_a_null_undo(self):
        v = mysql_variable('character_set_results', global_value=None, session_value=None)
        for mode in ('global', 'session'):
            with self.subTest(mode=mode):
                plan = V.build_plan(v, 'utf8mb4', mode)
                self.assertEqual(plan['undo'], ['SET %s character_set_results = NULL;' % mode.upper()])

    def test_read_only_variable_needs_persist_only_on_mysql(self):
        conn, plan, _data = plan_from('mysql-8.4', 'readonly-global')
        self.assertEqual(plan['status'], 'refused')
        self.assertEqual(plan['problems'][0]['code'], 'mode_unavailable')
        self.assertEqual(plan['alternative']['mode'], 'persist_only')
        v = plan['variable']
        self.assertIs(v['dynamic'], False)
        self.assertTrue(v['modes']['persist_only']['available'] and v['modes']['persist_only']['recommended'])
        self.assertFalse(v['modes']['persist']['available'])
        self.assertFalse(v['modes']['session']['available'])
        assert_reads_only(self, conn)

    def test_mariadb_has_no_persist_and_offers_the_option_file_line(self):
        conn, plan, _data = plan_from('mariadb-11.4', 'persist')
        self.assertEqual(plan['status'], 'refused')
        self.assertEqual(plan['problems'][0]['message'], 'MariaDB has no SET PERSIST.')
        self.assertEqual(plan['alternative']['mode'], 'global')
        self.assertEqual(plan['alternative']['config_line'], 'max_connections = 300')
        self.assertIn('never edits option files', plan['alternative']['text'])
        modes = plan['variable']['modes']
        self.assertFalse(modes['persist']['available'] or modes['persist_only']['available']
                         or modes['reset_persist']['available'])
        self.assertTrue(modes['global']['available'])
        assert_reads_only(self, conn)

    def test_mariadb_read_only_variable_has_no_runtime_change(self):
        _conn, plan, _data = plan_from('mariadb-11.4', 'readonly')
        self.assertEqual(plan['status'], 'refused')
        self.assertIsNone(plan['alternative']['mode'])
        self.assertEqual(plan['alternative']['config_line'], 'performance_schema = ON')
        self.assertIn('restart the server', plan['alternative']['text'])

    def test_scopes(self):
        session_only = mysql_variable('sql_log_bin', in_global=False, global_value=None, session_value='ON')
        self.assertEqual(session_only['scope'], 'session')
        plan = V.build_plan(session_only, 'OFF', 'global')
        self.assertEqual((plan['status'], plan['problems'][0]['code']), ('refused', 'mode_unavailable'))
        self.assertIn('only a session value', plan['problems'][0]['message'])
        global_only = mysql_variable('max_connections')
        plan = V.build_plan(global_only, '300', 'session')
        self.assertIn('only a global value', plan['problems'][0]['message'])
        session = V.build_plan(session_only, 'OFF', 'session')
        self.assertEqual(session['status'], 'ready')
        self.assertTrue(any('replicas' in c for c in session['caveats']))
        self.assertEqual(session['classification']['kind'], 'risky')
        read_only_session = mysql_variable('max_allowed_packet', global_value='67108864',
                                           info={'source': 'COMPILED', 'min': '1024', 'max': '1073741824'})
        self.assertFalse(read_only_session['modes']['session']['available'])
        # MySQL 9.x scope from variables_metadata; MariaDB from the catalog.
        metadata = V.describe(dict(
            name='some_component_var', engine='mysql', server_version='9.7.0', version_tuple=(9, 7, 0),
            in_global=False, in_session=True, global_value=None, session_value='1', persisted_globals_load='ON',
            related={}, catalog=None, info=None, persisted={'present': False, 'value': None, 'read': True},
            metadata={'scope': 'SESSION_ONLY', 'type': 'Integer', 'min': '0', 'max': '10', 'documentation': 'x'},
            notes=[]))
        self.assertEqual((metadata['scope'], metadata['type'], metadata['max']), ('session', 'integer', 10))
        self.assertIsNone(metadata['dynamic'])
        self.assertEqual(mariadb_variable('sort_buffer_size', scope='SESSION')['scope'], 'both')

    def test_unknown_variable_is_refused(self):
        v = mysql_variable('no_such_variable', in_global=False, in_session=False, global_value=None, info=None)
        plan = V.build_plan(v, '1', 'global')
        self.assertEqual(plan['status'], 'refused')
        self.assertIn('no variable named no_such_variable', plan['problems'][0]['message'])

    def test_generated_catalog_refuses_static_but_unknown_still_lets_server_decide(self):
        _conn, plan, _data = plan_from('mysql-8.4', 'server-refused')
        self.assertEqual(plan['status'], 'refused')
        self.assertIs(plan['variable']['dynamic'], False)
        # This recording used to exercise a name outside the hand map. Remove
        # the generated facts to keep coverage of an unrecognized server name.
        with mock.patch.dict(V.mysql_sysvars.SYSVARS, clear=True):
            _conn, plan, _data = plan_from('mysql-8.4', 'server-refused')
        self.assertEqual(plan['status'], 'ready')
        self.assertIsNone(plan['variable']['dynamic'])
        self.assertTrue(any('error 1238' in c for c in plan['caveats']))
        self.assertEqual(plan['statement'], "SET GLOBAL datadir = '/tmp'")

    def test_global_plan_has_statement_effect_privileges_undo_and_expect(self):
        _conn, plan, _data = plan_from('mysql-8.4', 'global')
        self.assertEqual(plan['status'], 'ready')
        self.assertEqual(plan['statement'], 'SET GLOBAL max_connections = 300')
        self.assertEqual(plan['undo'], ['SET GLOBAL max_connections = 151;'])
        self.assertEqual(plan['effect'], {'now': True, 'after_restart': False, 'needs_restart': False,
                                          'text': 'Changes the running value now, until the server restarts.'})
        self.assertEqual(plan['privileges']['need'], 'SYSTEM_VARIABLES_ADMIN or SUPER')
        self.assertIs(plan['privileges']['allowed'], True)
        self.assertEqual(plan['classification']['kind'], 'risky')
        legacy_expect = {key: plan['expect'][key] for key in (
            'name', 'mode', 'statement', 'global_value', 'session_value', 'persisted_value')}
        self.assertEqual(legacy_expect, {'name': 'max_connections', 'mode': 'global',
                                          'statement': 'SET GLOBAL max_connections = 300', 'global_value': '151',
                                          'session_value': None, 'persisted_value': None})
        self.assertEqual(plan['expect']['engine'], 'mysql')
        self.assertEqual(plan['expect']['server_identity'],
                         {'server_uuid': '11111111-2222-3333-4444-555555555555'})
        self.assertEqual(plan['expect']['source_metadata'], {key: plan['variable'][key] for key in (
            'source', 'source_path', 'set_by', 'set_time', 'set_user', 'set_host')})
        self.assertEqual(plan['expect']['persisted_state'],
                         {'state': 'absent', 'present': False, 'value': None, 'read': True})
        self.assertTrue(plan['variable']['read_at'])
        json.dumps(plan, allow_nan=False)

    def test_persist_plans(self):
        _conn, plan, _data = plan_from('mysql-8.4', 'persist')
        self.assertTrue(plan['effect']['now'] and plan['effect']['after_restart'])
        self.assertIn('RESET PERSIST max_connections;', plan['undo'])
        self.assertTrue(any('mysqld-auto.cnf after the option files' in c for c in plan['caveats']))
        _conn, plan, _data = plan_from('mysql-8.4', 'persist-only')
        self.assertEqual(plan['statement'], 'SET PERSIST_ONLY innodb_log_file_size = 67108864')
        self.assertEqual(plan['effect']['needs_restart'], True)
        self.assertFalse(plan['effect']['now'])
        self.assertEqual(plan['privileges']['need'], 'SYSTEM_VARIABLES_ADMIN and PERSIST_RO_VARIABLES_ADMIN')
        self.assertTrue(any('does not check the value of SET PERSIST_ONLY' in c for c in plan['caveats']))

    def test_persisted_globals_load_off_is_a_caveat(self):
        v = mysql_variable('max_connections', persisted_globals_load='OFF')
        plan = V.build_plan(v, '300', 'persist')
        self.assertTrue(any('persisted_globals_load is OFF' in c for c in plan['caveats']))

    def test_rounding_previews(self):
        _conn, plan, _data = plan_from('mysql-8.4', 'buffer-pool')
        self.assertTrue(any('so 300.0 MiB becomes 384.0 MiB' in c for c in plan['caveats']), plan['caveats'])
        _conn, plan, _data = plan_from('mariadb-11.4', 'block')
        self.assertTrue(any('so 100,000,000 becomes 99,999,744' in c for c in plan['caveats']), plan['caveats'])

    def test_privileges_per_engine(self):
        ma = mariadb_variable('max_connections')
        hint = V.privileges(ma, 'global', ['GRANT SUPER ON *.* TO `u`@`%`'])
        self.assertEqual(hint['need'], 'CONNECTION ADMIN')
        self.assertIsNone(hint['allowed'], 'SUPER counts on 11.0+ only for grants stored earlier')
        self.assertTrue(V.privileges(ma, 'global', ['GRANT CONNECTION ADMIN ON *.* TO `u`@`%`'])['allowed'])
        old = V.privileges(dict(ma, server_version='10.11.9-MariaDB'), 'global', ['GRANT SUPER ON *.* TO `u`@`%`'])
        self.assertTrue(old['allowed'])
        self.assertEqual(V.privileges(mariadb_variable('sync_binlog'), 'global')['need'], 'BINLOG ADMIN')
        self.assertEqual(V.privileges(mariadb_variable('innodb_io_capacity'), 'global')['need'], 'SUPER')
        self.assertEqual(V.privileges(mariadb_variable('sql_log_bin', scope='SESSION'), 'session')['need'],
                         'BINLOG ADMIN')
        my = mysql_variable('min_examined_row_limit', global_value='0')
        hint = V.privileges(my, 'session', ['GRANT SUPER ON *.* TO `u`@`%`'])
        self.assertIs(hint['allowed'], False, 'check_session_admin_no_super does not take SUPER')
        self.assertEqual(V.privileges(mysql_variable('sort_buffer_size'), 'session', [])['need'], 'no privilege')
        pool = mysql_variable('innodb_buffer_pool_size', persisted={'present': True, 'value': '1', 'read': True})
        self.assertEqual(V.privileges(pool, 'reset_persist')['need'],
                         'SYSTEM_VARIABLES_ADMIN and PERSIST_RO_VARIABLES_ADMIN',
                         'a PERSIST_AS_READONLY entry needs both privileges to reset')
        self.assertEqual(V.privileges(mysql_variable('max_connections'), 'reset_persist')['need'],
                         'SYSTEM_VARIABLES_ADMIN or SUPER')
        self.assertEqual(V.privileges(mysql_variable('default_table_encryption', global_value='OFF'), 'global')['need'],
                         'SUPER or SYSTEM_VARIABLES_ADMIN and TABLE_ENCRYPTION_ADMIN')

    def test_privilege_maps_follow_the_source(self):
        """Corrections from the source review (M/sql/sys_vars.cc, Ma/sql/privilege.h, Ma/sql/sys_vars.cc)."""
        superuser = ['GRANT SUPER ON *.* TO `u`@`%`']
        for name in ('max_delayed_threads', 'max_insert_delayed_threads'):
            self.assertIs(V.privileges(mysql_variable(name, global_value='20'), 'session', superuser)['allowed'], False,
                          '%s: check_session_admin_no_super' % name)
        self.assertEqual(V.privileges(mysql_variable('sql_require_primary_key', global_value='OFF'), 'session')['need'],
                         'SESSION_VARIABLES_ADMIN or SYSTEM_VARIABLES_ADMIN or SUPER')
        applier = V.privileges(mysql_variable('original_server_version', global_value='0'), 'session',
                               ['GRANT REPLICATION_APPLIER ON *.* TO `u`@`%`'])
        self.assertTrue(applier['allowed'], 'check_session_admin_or_replication_applier')
        self.assertEqual(V.privileges(mariadb_variable('thread_pool_size'), 'global')['need'], 'CONNECTION ADMIN')
        self.assertNotIn('thread_pool', V.MARIADB_GLOBAL_PRIVILEGE)
        self.assertEqual(V.privileges(mariadb_variable('binlog_expire_logs_seconds'), 'global')['need'], 'BINLOG ADMIN')
        self.assertTrue(V.privileges(mariadb_variable('binlog_do_db'), 'global', superuser)['allowed'],
                        'SUPER_ACL | BINLOG_ADMIN_ACL: either one')
        self.assertEqual(V.privileges(mariadb_variable('wsrep_on', scope='SESSION'), 'session')['need'], 'SUPER')
        read_only = V.privileges(mariadb_variable('read_only'), 'global', superuser)
        self.assertEqual(read_only['need'], 'READ_ONLY ADMIN')
        # 10.11.1 re-adds READ_ONLY ADMIN only for grants stored before 10.11 (mariadb-10.11.1 sql_acl.cc:1548-1551).
        self.assertIn('MariaDB 10.11.1 and later', read_only['note'])

    def test_corrected_caveats(self):
        slow = mariadb_variable('log_slow_query', type='BOOLEAN', scope='SESSION', global_value='OFF', min=None,
                                max=None, block=None, values='OFF,ON')
        text = ' '.join(V.build_plan(slow, 'ON', 'global')['caveats'])
        self.assertIn('turning it off stops logging for every connection', text)
        persist_only = V.build_plan(mysql_variable('innodb_log_file_size', global_value='50331648',
                                                   info={'source': 'COMPILED', 'min': '4194304',
                                                         'max': '18446744073709551615'}), '64M', 'persist_only')
        self.assertTrue(any('it does check ON and OFF for booleans' in c for c in persist_only['caveats']))
        doublewrite = mysql_variable('innodb_doublewrite', global_value='ON')
        self.assertEqual(V.normalize_value(doublewrite, 'true')['literal'], "'TRUE'")

    def test_server_errors_become_statuses(self):
        from myflames.connector import ConnectorError
        cases = {1227: 'denied', 3630: 'denied', 1228: 'refused_by_server', 1229: 'refused_by_server',
                 1238: 'refused_by_server', 1231: 'refused_by_server', 1232: 'refused_by_server',
                 1193: 'refused_by_server', 3615: 'refused_by_server', 1317: 'interrupted', 1064: 'failed'}
        for code, status in cases.items():
            with self.subTest(code=code):
                exc = ConnectorError('mysql client exited 1: ERROR %d (HY000) at line 1: message' % code)
                found, message = V.error_status(exc)
                self.assertEqual(found, status)
                self.assertNotIn('at line', message)
        status, message = V.error_status(ConnectorError(
            "mysql client exited 1: ERROR 1229 (HY000) at line 1: Variable 'sql_log_bin' is a SESSION variable"))
        self.assertIn('Choose the other scope', message)

    def test_static_dynamic_covers_the_configuration_view(self):
        from myflames.config_review import VARIABLES
        for name in VARIABLES:
            for engine in ('mysql', 'mariadb'):
                value = V.static_dynamic(engine, name)
                if engine == 'mysql' and name in V.MYSQL_VARIABLES or engine == 'mariadb' and (
                        name in V.MARIADB_DYNAMIC or name in V.MARIADB_READ_ONLY):
                    self.assertIn(value, (True, False), (engine, name))
        self.assertIs(V.static_dynamic('mysql', 'performance_schema'), False)
        self.assertIs(V.static_dynamic('mysql', 'max_connections'), True)
        self.assertIs(V.static_dynamic('mariadb', 'innodb_log_file_size'), True)
        self.assertIs(V.static_dynamic('mysql', 'validate_password.length'), True)


class TestReadStates(unittest.TestCase):
    def test_nullable_reads_and_session_readback_preserve_null_empty_and_literal_null(self):
        # Synthetic encodings exercise the client protocol independently of
        # whether the server would accept these strings as a charset name.
        for server in ('mysql-8.4', 'mariadb-11.4'):
            for value, encoded in ((None, '1\tNULL'), ('', '0\t'), ('NULL', '0\t4E554C4C')):
                with self.subTest(server=server, value=value):
                    data = fixture('variable', server + '-global')
                    sql, text = data['calls'][0][:2]
                    sql = sql.replace('max_connections', 'character_set_results')
                    start, end = sql.index('(') + 1, sql.index(')')
                    sql = sql[:start] + ', '.join(sorted(sql[start:end].split(', '))) + sql[end:]
                    sql += ("; " + TAG + "SELECT 'myflames:nullable'; " + TAG +
                            'SELECT @@GLOBAL.character_set_results IS NULL, HEX(@@GLOBAL.character_set_results), '
                            '@@SESSION.character_set_results IS NULL, HEX(@@SESSION.character_set_results)')
                    text = text.replace('max_connections\t151', 'character_set_results\tNULL')
                    text += 'myflames:nullable\n%s\t%s\n' % (encoded, encoded)
                    catalog_sql, catalog_text = data['calls'][1][:2]
                    catalog_sql = catalog_sql.replace('max_connections', 'character_set_results').replace(
                        'MAX_CONNECTIONS', 'CHARACTER_SET_RESULTS')
                    if server.startswith('mariadb'):
                        catalog_text = ('CHARACTER_SET_RESULTS\tNULL\tNULL\tCOMPILE-TIME\tutf8mb4\tSESSION\t'
                                        'VARCHAR\tNULL\tNULL\tNULL\tNULL\tNO\tNULL\tResult charset\n')
                    conn = ReplayConn([[sql, text], [catalog_sql, catalog_text]])
                    v = V.read_variable(conn, 'character_set_results')
                    self.assertEqual((v['global_value'], v['session_value']), (value, value))
                    after = V._after(conn, 'character_set_results', 'session', 'myflames:after\n' + encoded + '\n')
                    self.assertTrue(after['session_read'])
                    self.assertEqual(after['session'], value)
                    assert_reads_only(self, conn)

    def test_persisted_projection_preserves_null_empty_and_literal_null(self):
        # These answers are explicit synthetic edge cases; the normal
        # numeric persisted entries come from the live replay fixtures.
        for row, present, value in (('', False, None), ('1\tNULL\n', True, None),
                                    ('0\t\n', True, ''), ('0\t4E554C4C\n', True, 'NULL')):
            with self.subTest(row=row):
                def edit(sql, text, n):
                    if 'performance_schema.persisted_variables' in sql:
                        return text.split('myflames:persisted\n')[0] + 'myflames:persisted\n' + row
                    return text
                conn, _data = replay('mysql-8.4', 'global', edit=edit)
                persisted = V.read_variable(conn, 'max_connections')['persisted']
                self.assertEqual((persisted['present'], persisted['value'], persisted['state'], persisted['read']),
                                 (present, value, 'present' if present else 'absent', True))

    def test_denied_catalog_read_blocks_persistence_modes(self):
        from myflames.connector import ConnectorError
        def edit(sql, text, n):
            if 'performance_schema.persisted_variables' in sql:
                raise ConnectorError('ERROR 1142: SELECT denied for persisted_variables')
            return text
        conn, _data = replay('mysql-8.4', 'global', edit=edit)
        v = V.read_variable(conn, 'max_connections')
        self.assertEqual((v['persisted']['state'], v['persisted']['present']), ('unreadable', None))
        for mode in ('persist', 'persist_only', 'reset_persist'):
            with self.subTest(mode=mode):
                self.assertEqual(V.build_plan(v, '300', mode)['status'], 'refused')
        assert_reads_only(self, conn)


class TestApply(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.dict(os.environ, {'HOME': self.temp.name, 'USERPROFILE': self.temp.name,
                                               'MYFLAMES_SECRET_BACKEND': 'vault'})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        from myflames.workspace_store import WorkspaceStore
        self.store = WorkspaceStore(Path(self.temp.name) / 'workspace.sqlite3')

    def audit(self, mode, password=None):
        return dbops.AuditTrail(self.store, V.OPERATIONS[mode], 'ui',
                                connection={'host': '127.0.0.1', 'port': 34811, 'user': 'root'}, password=password)

    def apply(self, server, scenario, **kwargs):
        conn, plan, data = plan_from(server, scenario)
        audit = kwargs.pop('audit', None) or self.audit(data['args']['mode'])
        result = V.apply_change(conn, plan, plan['expect'], confirmed=True, audit=audit, **kwargs)
        json.dumps(result, allow_nan=False)
        return conn, plan, result

    def records(self):
        return dbops.audit_report(self.store)['records']

    def sent(self, conn):
        return [(sql, flags) for sql, flags in conn.calls if not sql.startswith((TAG + 'SHOW', TAG + 'SELECT'))]

    def test_global_on_both_engines(self):
        for server in ('mysql-8.4', 'mariadb-11.4'):
            with self.subTest(server=server):
                conn, plan, result = self.apply(server, 'global')
                self.assertEqual(result['status'], 'changed')
                self.assertEqual(result['after']['global'], '300')
                self.assertEqual(result['message'], 'Set max_connections globally: 151 -> 300.')
                required = {'statement', 'global value', 'engine', 'server identity', 'server version',
                            'source metadata'}
                if server.startswith('mysql'):
                    required.update(('persisted value', 'persisted state'))
                self.assertEqual(set(result['verified']), required)
                sent = self.sent(conn)
                self.assertEqual(sent, [(TAG + 'SET GLOBAL max_connections = 300', list(dbops.WRITE_CLIENT_FLAGS))])
        record = self.records()[0]
        self.assertEqual((record['status'], record['outcome'], record['operation']), ('ok', 'changed', 'set_global'))
        self.assertEqual(record['statements'], [TAG + 'SET GLOBAL max_connections = 300'])

    def test_persist_then_restart_keeps_the_value(self):
        conn, _plan, result = self.apply('mysql-8.4', 'persist')
        self.assertEqual(result['status'], 'changed')
        self.assertEqual((result['after']['global'], result['after']['persisted']), ('400', '400'))
        self.assertIn('globally and in mysqld-auto.cnf', result['message'])
        after_restart, _data = replay('mysql-8.4', 'restart')
        v = V.read_variable(after_restart, 'max_connections')
        self.assertEqual((v['global_value'], v['set_by'], v['persisted']['value']), ('400', 'SET PERSIST', '400'))
        self.assertTrue(v['modes']['reset_persist']['available'])

    def test_persist_only_applies_at_the_next_restart(self):
        _conn, _plan, result = self.apply('mysql-8.4', 'persist-only')
        self.assertEqual(result['status'], 'changed')
        self.assertEqual(result['after']['global'], '50331648', 'the running value does not change')
        self.assertEqual(result['after']['persisted'], '67108864')
        self.assertIn('for the next restart', result['message'])
        after_restart, _data = replay('mysql-8.4', 'restart-persist-only')
        v = V.read_variable(after_restart, 'innodb_log_file_size')
        self.assertEqual(v['global_value'], '67108864')

    def test_reset_persist(self):
        conn, plan, result = self.apply('mysql-8.4', 'reset-persist')
        self.assertEqual(plan['statement'], 'RESET PERSIST max_connections')
        self.assertEqual(plan['undo'], ['SET PERSIST_ONLY max_connections = 400;'])
        self.assertEqual(result['status'], 'changed')
        self.assertIsNone(result['after']['persisted'])
        self.assertEqual(result['after']['global'], '400', 'RESET PERSIST leaves the running value')
        self.assertEqual(self.records()[0]['operation'], 'reset_persist')

    def test_reset_persist_that_keeps_the_entry_is_unchanged(self):
        def edit(sql, text, n):
            if 'performance_schema.persisted_variables' in sql and n == 2:
                return text.split('myflames:persisted\n')[0] + 'myflames:persisted\n0\t343030\n'
            return text
        conn, data = replay('mysql-8.4', 'reset-persist', edit=edit)
        args = data['args']
        plan = V.plan_change(conn, args['name'], args['value'], args['mode'])
        result = V.apply_change(conn, plan, plan['expect'], confirmed=True, audit=self.audit('reset_persist'))
        self.assertEqual(result['status'], 'unchanged')
        self.assertEqual(result['after']['persisted_state'],
                         {'state': 'present', 'read': True, 'present': True, 'value': '400'})
        self.assertEqual(self.sent(conn), [(TAG + 'RESET PERSIST max_connections', list(dbops.WRITE_CLIENT_FLAGS))])
        self.assertEqual((self.records()[0]['status'], self.records()[0]['outcome']), ('error', 'unchanged'))

    def test_session_reads_back_in_the_same_client_call(self):
        for server, value in (('mysql-8.4', '1048576'), ('mariadb-11.4', '4194304')):
            with self.subTest(server=server):
                conn, _plan, result = self.apply(server, 'session')
                self.assertEqual(result['status'], 'changed')
                self.assertEqual(result['after']['session'], value)
                self.assertNotEqual(result['after']['global'], value, 'SET SESSION leaves the global value')
                (sql, flags), = self.sent(conn)
                self.assertTrue(sql.startswith(TAG + 'SET SESSION sort_buffer_size = %s; ' % value))
                self.assertIn("SHOW SESSION VARIABLES WHERE Variable_name = 'sort_buffer_size'", sql)
                self.assertEqual(flags, list(dbops.WRITE_CLIENT_FLAGS))
                self.assertIn('run the statement in your own session', result['message'])

    def test_adjusted_values_are_reported(self):
        _conn, _plan, result = self.apply('mysql-8.4', 'buffer-pool')
        self.assertEqual((result['status'], result['adjusted']), ('changed', True))
        self.assertIn('the server made it 384.0 MiB instead of 300.0 MiB', result['message'])
        self.assertTrue(result['after']['resize_status'].startswith('Completed resizing buffer pool'))
        _conn, _plan, result = self.apply('mariadb-11.4', 'block')
        self.assertEqual((result['status'], result['adjusted'], result['after']['global']), ('changed', True, '99999744'))
        self.assertIn('made it 99,999,744 instead of 100,000,000', result['message'])
        _conn, _plan, result = self.apply('mariadb-11.4', 'set')
        self.assertEqual((result['status'], result['adjusted'], result['after']['global']), ('changed', False,
                                                                                            'FILE,TABLE'))

    def test_server_refusals(self):
        # Simulate a variable absent from the catalog; known datadir now
        # refuses before sending. The recorded 1238 response still maps/audits.
        with mock.patch.dict(V.mysql_sysvars.SYSVARS, clear=True):
            conn, _plan, result = self.apply('mysql-8.4', 'server-refused')
        self.assertEqual(result['status'], 'refused_by_server')
        self.assertIn("Variable 'datadir' is a read only variable", result['message'])
        self.assertIn('SET PERSIST_ONLY', result['message'])
        record = self.records()[0]
        self.assertEqual((record['status'], record['outcome'], record['error_code']), ('error', 'refused_by_server',
                                                                                      1238))
        conn, _plan, result = self.apply('mysql-8.4', 'denied', audit=self.audit('global', password='hunter2-not-real'))
        self.assertEqual(result['status'], 'denied')
        self.assertIn('SYSTEM_VARIABLES_ADMIN', result['message'])
        self.assertIs(_plan['privileges']['allowed'], False, 'the hint saw the missing privilege first')
        self.assertNotIn('hunter2', json.dumps(self.records()))

    def test_value_changed_since_the_plan_sends_nothing(self):
        conn, _plan, result = self.apply('mysql-8.4', 'changed')
        self.assertEqual(result['status'], 'refused')
        self.assertIn('changed since you reviewed it (400, now 222)', result['message'])
        self.assertEqual(self.sent(conn), [])
        record = self.records()[0]
        self.assertEqual((record['status'], record['outcome']), ('refused', 'changed'))
        self.assertEqual(record['statements'], [TAG + 'SET GLOBAL max_connections = 250'])

    def test_statement_mismatch_sends_nothing(self):
        conn, plan, _data = plan_from('mysql-8.4', 'global')
        expect = dict(plan['expect'], statement='SET GLOBAL max_connections = 3000')
        result = V.apply_change(conn, plan, expect, confirmed=True, audit=self.audit('global'))
        self.assertEqual(result['status'], 'refused')
        self.assertIn('differs from the one you confirmed', result['message'])
        self.assertEqual(self.sent(conn), [])

    def test_same_endpoint_with_a_different_server_refuses_before_sending(self):
        cases = (
            ('mysql-8.4', 'server_uuid', '11111111-2222-3333-4444-555555555555', 'different-uuid'),
            ('mariadb-11.4', 'server_id', '1', '2'),
            ('mariadb-11.4', 'hostname', 'fixture-db', 'replacement-db'),
            ('mariadb-11.4', 'version', '11.4.13-MariaDB-ubu2404', '11.4.14-MariaDB-ubu2404'),
        )
        for server, field, previous, replacement in cases:
            with self.subTest(server=server, field=field):
                def edit(sql, text, n):
                    if 'SHOW GLOBAL VARIABLES WHERE' in sql and n == 1:
                        if field == 'version':
                            lines = text.splitlines()
                            old = next(line for line in lines if line.startswith('version\t'))
                            return text.replace(old, 'version\t' + replacement)
                        return text.replace('%s\t%s\n' % (field, previous),
                                            '%s\t%s\n' % (field, replacement))
                    return text
                conn, data = replay(server, 'global', edit=edit)
                args = data['args']
                plan = V.plan_change(conn, args['name'], args['value'], args['mode'])
                self.assertEqual(plan['status'], 'ready')
                result = V.apply_change(conn, plan, plan['expect'], confirmed=True, audit=self.audit('global'))
                self.assertEqual(result['status'], 'refused')
                self.assertEqual(self.sent(conn), [])

    def test_mariadb_server_id_change_updates_only_the_confirmed_server_identity(self):
        v = mariadb_variable('server_id', global_value='1', session_value='1', scope='SESSION',
                             min='1', max='4294967295')
        plan = V.build_plan(v, '2', 'global')
        self.assertEqual(plan['status'], 'ready')
        for hostname, status, audit_status in (('fixture-db', 'changed', 'ok'),
                                               ('other-db', 'applied_unverified', 'unknown')):
            with self.subTest(hostname=hostname):
                after = {'global': '2', 'global_read': True, 'session': None, 'session_read': False,
                         'persisted': None, 'persisted_state': {'state': 'unsupported', 'read': False,
                                                               'present': None, 'value': None},
                         'engine': v['engine'], 'server_version': v['server_version'],
                         'server_identity': dict(v['server_identity'], server_id='2', hostname=hostname)}
                conn = ReplayConn([[TAG + 'SET GLOBAL server_id = 2', '']])
                with mock.patch.object(V, 'read_variable', return_value=v), \
                        mock.patch.object(V, '_after', return_value=after):
                    result = V.apply_change(conn, plan, plan['expect'], confirmed=True, audit=self.audit('global'))
                self.assertEqual(result['status'], status)
                self.assertEqual(self.sent(conn), [(TAG + 'SET GLOBAL server_id = 2', list(dbops.WRITE_CLIENT_FLAGS))])
                record = next(r for r in self.records() if r['id'] == result['audit_id'])
                self.assertEqual((record['status'], record['outcome']), (audit_status, status))

    def test_same_value_with_changed_source_metadata_refuses(self):
        for field, changed in (('source', 'PERSISTED'), ('source_path', '/etc/changed.cnf'),
                               ('set_time', '2026-10-15 17:00:00'), ('set_user', 'other'), ('set_host', 'other')):
            with self.subTest(field=field):
                conn, plan, _data = plan_from('mysql-8.4', 'global')
                fresh = copy.deepcopy(plan['variable'])
                fresh[field] = changed
                with mock.patch.object(V, 'read_variable', return_value=fresh):
                    result = V.apply_change(conn, plan, plan['expect'], confirmed=True, audit=self.audit('global'))
                self.assertEqual(result['status'], 'refused')
                self.assertEqual(self.sent(conn), [])

    def test_persisted_absent_to_null_or_empty_refuses(self):
        for value in (None, ''):
            with self.subTest(value=value):
                conn, plan, _data = plan_from('mysql-8.4', 'global')
                fresh = copy.deepcopy(plan['variable'])
                fresh['persisted'].update(state='present', read=True, present=True, value=value)
                with mock.patch.object(V, 'read_variable', return_value=fresh):
                    result = V.apply_change(conn, plan, plan['expect'], confirmed=True, audit=self.audit('global'))
                self.assertEqual(result['status'], 'refused')
                self.assertEqual(self.sent(conn), [])

    def test_post_write_read_failure_is_applied_unverified(self):
        from myflames.connector import ConnectorError
        for server in ('mysql-8.4', 'mariadb-11.4'):
            with self.subTest(server=server):
                conn, plan, _data = plan_from(server, 'global')
                with mock.patch.object(V, '_after', side_effect=ConnectorError('ERROR 2013: Lost connection')):
                    result = V.apply_change(conn, plan, plan['expect'], confirmed=True, audit=self.audit('global'))
                self.assertEqual(result['status'], 'applied_unverified')
                self.assertEqual(len(self.sent(conn)), 1)
                self.assertIsNone(result['after'])
                record = next(r for r in self.records() if r['id'] == result['audit_id'])
                self.assertEqual((record['status'], record['outcome']), ('unknown', 'applied_unverified'))

    def test_persist_after_read_denied_is_applied_unverified(self):
        from myflames.connector import ConnectorError
        for scenario in ('persist', 'persist-only', 'reset-persist'):
            with self.subTest(scenario=scenario):
                def edit(sql, text, n):
                    if 'performance_schema.persisted_variables' in sql and n == 2:
                        raise ConnectorError('ERROR 1142: SELECT denied for persisted_variables')
                    return text
                conn, data = replay('mysql-8.4', scenario, edit=edit)
                args = data['args']
                plan = V.plan_change(conn, args['name'], args['value'], args['mode'])
                result = V.apply_change(conn, plan, plan['expect'], confirmed=True, audit=self.audit(args['mode']))
                self.assertEqual(result['status'], 'applied_unverified')
                self.assertEqual(result['after']['persisted_state']['state'], 'unreadable')
                record = next(r for r in self.records() if r['id'] == result['audit_id'])
                self.assertEqual((record['status'], record['outcome']), ('unknown', 'applied_unverified'))

    def test_persist_matching_requires_running_and_persisted_values(self):
        v = mysql_variable('max_connections')
        normalized = V.normalize_value(v, '300')
        for global_value, persisted in (('300', '151'), ('151', '300')):
            with self.subTest(global_value=global_value, persisted=persisted):
                after = {'global': global_value, 'persisted': persisted, 'session': None,
                         'persisted_state': {'state': 'present', 'present': True, 'value': persisted, 'read': True},
                         'global_read': True, 'session_read': False, 'engine': v['engine'],
                         'server_identity': v['server_identity'], 'server_version': v['server_version']}
                self.assertIs(V._matches(v, 'persist', normalized, after), False)

    def test_persist_partial_success_is_applied_unverified(self):
        for global_value, persisted in (('400', '300'), ('300', '400')):
            with self.subTest(global_value=global_value, persisted=persisted):
                conn, plan, _data = plan_from('mysql-8.4', 'persist')
                after = {'global': global_value, 'persisted': persisted, 'session': None,
                         'persisted_state': {'state': 'present', 'present': True, 'value': persisted, 'read': True},
                         'global_read': True, 'session_read': False, 'engine': plan['engine'],
                         'server_identity': plan['variable']['server_identity'],
                         'server_version': plan['server_version']}
                with mock.patch.object(V, '_after', return_value=after):
                    result = V.apply_change(conn, plan, plan['expect'], confirmed=True, audit=self.audit('persist'))
                self.assertEqual(result['status'], 'applied_unverified')
                record = next(r for r in self.records() if r['id'] == result['audit_id'])
                self.assertEqual((record['status'], record['outcome']), ('unknown', 'applied_unverified'))

    def test_persist_only_mismatch_or_absent_entry_is_applied_unverified(self):
        # PERSIST_ONLY bypasses most server validation, so a different saved
        # value cannot be explained as normal runtime clamping or rounding.
        for persisted in ('83886080', None):
            with self.subTest(persisted=persisted):
                conn, plan, _data = plan_from('mysql-8.4', 'persist-only')
                present = persisted is not None
                after = {'global': plan['before']['global'], 'persisted': persisted, 'session': None,
                         'persisted_state': {'state': 'present' if present else 'absent', 'read': True,
                                             'present': present, 'value': persisted},
                         'global_read': True, 'session_read': False, 'engine': plan['engine'],
                         'server_identity': plan['variable']['server_identity'],
                         'server_version': plan['server_version']}
                with mock.patch.object(V, '_after', return_value=after):
                    result = V.apply_change(conn, plan, plan['expect'], confirmed=True,
                                            audit=self.audit('persist_only'))
                self.assertEqual(result['status'], 'applied_unverified')
                self.assertEqual(len(self.sent(conn)), 1)
                record = next(r for r in self.records() if r['id'] == result['audit_id'])
                self.assertEqual((record['status'], record['outcome']), ('unknown', 'applied_unverified'))

    def test_null_matching_distinguishes_null_empty_and_literal_null(self):
        v = mysql_variable('character_set_results', global_value='utf8mb4', session_value='utf8mb4')
        null = V.normalize_value(v, None)
        literal_null = V.normalize_value(v, 'NULL')
        for value in (None, '', 'NULL'):
            with self.subTest(value=value):
                after = {'session': value}
                self.assertIs(V._matches(v, 'session', null, after), value is None)
                self.assertIs(V._matches(v, 'session', literal_null, after), value == 'NULL')

    def test_optimizer_switch_matches_only_the_requested_flags(self):
        v = mysql_variable('optimizer_switch', global_value='index_merge=on,hash_join=on')
        wanted = V.normalize_value(v, 'index_merge=off')
        after = {'global': 'hash_join=on,index_merge=off', 'global_read': True}
        self.assertIs(V._matches(v, 'global', wanted, after), True)
        after['global'] = 'hash_join=on,index_merge=on'
        self.assertIs(V._matches(v, 'global', wanted, after), False)
        after['global'] = 'hash_join=on'
        self.assertIs(V._matches(v, 'global', wanted, after), False)

    def test_optimizer_switch_persist_only_matches_exact_assignments_and_literal_default(self):
        v = mysql_variable('optimizer_switch', global_value='index_merge=on,hash_join=on')
        cases = (
            ('index_merge=off', 'index_merge=off', True),
            ('index_merge=off', 'index_merge=off,hash_join=off', False),
            ('index_merge=off,hash_join=off', 'hash_join=off,index_merge=off', True),
            ('index_merge=default', 'index_merge=default', True),
            ('index_merge=default', 'index_merge=on', False),
            ('index_merge=default', 'index_merge=default,hash_join=on', False),
        )
        for requested, persisted, matched in cases:
            with self.subTest(requested=requested, persisted=persisted):
                wanted = V.normalize_value(v, requested)
                self.assertFalse(wanted['problems'])
                after = {'persisted_state': {'state': 'present', 'read': True, 'present': True, 'value': persisted}}
                self.assertIs(V._matches(v, 'persist_only', wanted, after), matched)
        wanted = V.normalize_value(v, 'index_merge=default')
        for mode, running in (('global', 'index_merge=on,hash_join=on'),
                               ('session', 'index_merge=off,hash_join=on')):
            with self.subTest(mode=mode, running=running):
                self.assertIsNone(V._matches(v, mode, wanted, {mode: running, mode + '_read': True}),
                                  'the running flag list does not reveal the compiled default')

    def test_persisted_optimizer_switch_cannot_add_overrides_absent_from_the_running_flags(self):
        v = mysql_variable('optimizer_switch', global_value='index_merge=on,hash_join=on')
        plan = V.build_plan(v, 'index_merge=off', 'persist')
        self.assertEqual(plan['status'], 'ready')
        for persisted, status in (('hash_join=on,index_merge=off', 'changed'),
                                   ('hash_join=on,index_merge=off,mrr=off', 'applied_unverified')):
            with self.subTest(persisted=persisted):
                after = {'global': 'index_merge=off,hash_join=on', 'global_read': True,
                         'session': None, 'session_read': False, 'persisted': persisted,
                         'persisted_state': {'state': 'present', 'read': True, 'present': True, 'value': persisted},
                         'engine': v['engine'], 'server_version': v['server_version'],
                         'server_identity': v['server_identity']}
                self.assertIs(V._persisted_matches_running(v, after), status == 'changed')
                conn = ReplayConn([[TAG + plan['statement'], '']])
                with mock.patch.object(V, 'read_variable', return_value=v), \
                        mock.patch.object(V, '_after', return_value=after):
                    result = V.apply_change(conn, plan, plan['expect'], confirmed=True, audit=self.audit('persist'))
                self.assertEqual(result['status'], status)
                record = next(r for r in self.records() if r['id'] == result['audit_id'])
                self.assertEqual((record['status'], record['outcome']),
                                 ('ok' if status == 'changed' else 'unknown', status))

    def test_expect_must_match_the_request(self):
        conn, plan, _data = plan_from('mysql-8.4', 'global')
        for expect in (None, 'x', dict(plan['expect'], name='sort_buffer_size'), dict(plan['expect'], mode='persist'),
                       dict(plan['expect'], global_value=151)):
            with self.subTest(expect=expect), self.assertRaises(ValueError):
                V.apply_change(conn, plan, expect, confirmed=True)

    def test_policy_gates_before_any_read(self):
        conn, plan, _data = plan_from('mysql-8.4', 'global')
        calls = len(conn.calls)
        result = V.apply_change(conn, plan, plan['expect'], audit=self.audit('global'))
        self.assertEqual(result['status'], 'needs_confirmation')
        read_only = dbops.decide(dbops.classify(plan['statement']), 'development', read_only=True)
        result = V.apply_change(conn, plan, plan['expect'], decision=read_only, confirmed=True,
                                audit=self.audit('global'))
        self.assertEqual(result['status'], 'refused')
        self.assertIn('read-only in myflames', result['message'])
        self.assertEqual(len(conn.calls), calls, 'nothing was read or sent')
        outcomes = sorted(r['outcome'] for r in self.records())
        self.assertEqual(outcomes, ['needs_confirmation', 'read_only'])

    def test_audit_record_is_written_before_sending(self):
        conn, plan, _data = plan_from('mysql-8.4', 'global')
        broken = mock.Mock()
        broken.audit_insert.side_effect = RuntimeError('disk full')
        audit = dbops.AuditTrail(broken, 'set_global', 'ui')
        with self.assertRaisesRegex(ValueError, 'nothing was sent'):
            V.apply_change(conn, plan, plan['expect'], confirmed=True, audit=audit)
        self.assertEqual(self.sent(conn), [])

    def test_lost_connection_is_unknown_not_an_error(self):
        from myflames.connector import ConnectorError
        conn, plan, _data = plan_from('mysql-8.4', 'global')
        original = conn.run

        def run(sql, extra_flags=None, timeout=None):
            if 'SET GLOBAL' in sql:
                raise ConnectorError('mysql client exited 1: ERROR 2013 (HY000): Lost connection to MySQL server')
            return original(sql, extra_flags, timeout)
        conn.run = run
        with self.assertRaisesRegex(ValueError, 'outcome is unknown'):
            V.apply_change(conn, plan, plan['expect'], confirmed=True, audit=self.audit('global'))
        self.assertEqual(self.records()[0]['status'], 'unknown')


class TestCli(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.dict(os.environ, {'HOME': self.temp.name, 'USERPROFILE': self.temp.name,
                                               'MYFLAMES_SECRET_BACKEND': 'vault'})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)

    def cli(self, server, scenario, argv, tty=False, answer='y\n'):
        conn, _data = replay(server, scenario)
        stdin = mock.Mock()
        stdin.isatty.return_value = tty
        stdin.readline.return_value = answer
        code, out, err, factory = run_cli('config', argv + ['-h', '127.0.0.1', '-P', '34811', '-u', 'root',
                                                            '-phunter2-not-real'], conn,
                                          patches=[mock.patch('sys.stdin', stdin)])
        return code, out, err, conn

    def audit(self):
        from myflames.workspace_store import WorkspaceStore
        return dbops.audit_report(WorkspaceStore())['records']

    def test_set_with_yes_exits_0(self):
        code, out, err, conn = self.cli('mysql-8.4', 'global', ['set', 'max_connections', '300', '--yes'])
        self.assertEqual(code, 0, err)
        self.assertEqual(out, 'Set max_connections globally: 151 -> 300.\n')
        self.assertIn('SET GLOBAL max_connections = 300;', err)
        self.assertIn('Undo: SET GLOBAL max_connections = 151;', err)
        self.assertIn('Production connection', err)
        self.assertEqual(self.audit()[0]['surface'], 'cli')
        self.assertNotIn('hunter2', json.dumps(self.audit()))

    def test_prompt_and_non_tty(self):
        code, out, err, conn = self.cli('mysql-8.4', 'global', ['set', 'max_connections', '300'])
        self.assertEqual(code, 2)
        self.assertIn('stdin is not a terminal', err)
        self.assertFalse([sql for sql, _ in conn.calls if 'SET GLOBAL' in sql])
        code, out, err, conn = self.cli('mysql-8.4', 'global', ['set', 'max_connections', '300'], tty=True,
                                        answer='n\n')
        self.assertEqual((code, out), (2, ''))
        self.assertIn('Nothing was sent.', err)
        self.assertIn('Change it? [y/N]', err)
        code, out, err, conn = self.cli('mysql-8.4', 'global', ['set', 'max_connections', '300'], tty=True)
        self.assertEqual(code, 0)
        statuses = [r['status'] for r in self.audit()]
        self.assertEqual(statuses, ['ok', 'declined', 'refused'])

    def test_session_explains_one_off_connection_on_stderr(self):
        for server in ('mysql-8.4', 'mariadb-11.4'):
            for quiet in (False, True):
                with self.subTest(server=server, quiet=quiet):
                    value = fixture('variable', server + '-session')['args']['value']
                    argv = ['set', 'sort_buffer_size', value, '--session', '--yes', '--json']
                    if quiet:
                        argv.append('-q')
                    code, out, err, _conn = self.cli(server, 'session', argv)
                    self.assertEqual(code, 0, err)
                    self.assertEqual(json.loads(out)['mode'], 'session')
                    note = ('SET SESSION applies only to the one-off connection myflames opens; '
                            'use it to test a value.\n')
                    if quiet:
                        self.assertEqual(err, "")
                    else:
                        self.assertEqual(err.count(note), 1)

    def test_json_and_quiet(self):
        code, out, err, _conn = self.cli('mysql-8.4', 'persist', ['set', 'max_connections', '400', '--persist',
                                                                  '--yes', '--json', '-q'])
        self.assertEqual(code, 0)
        self.assertEqual(err, '')
        result = json.loads(out)
        self.assertEqual((result['schema_version'], result['status'], result['mode']),
                         ('variable-change-1.0', 'changed', 'persist'))
        self.assertEqual(result['after']['persisted'], '400')

    def test_mariadb_persist_is_refused_with_the_option_file_line(self):
        code, out, err, _conn = self.cli('mariadb-11.4', 'persist', ['set', 'max_connections', '300', '--persist',
                                                                     '--yes'])
        self.assertEqual(code, 2)
        self.assertIn('MariaDB has no SET PERSIST.', err)
        self.assertIn('Option file line: max_connections = 300', err)
        self.assertIn('Run: myflames config set max_connections 300', err)
        self.assertEqual(out, '')
        self.assertEqual(self.audit()[0]['statements'], [TAG + 'SET PERSIST max_connections = 300'])

    def test_server_refusal_and_denial_exit_1(self):
        with mock.patch.dict(V.mysql_sysvars.SYSVARS, clear=True):
            code, out, _err, _conn = self.cli('mysql-8.4', 'server-refused', ['set', 'datadir', '/tmp', '--yes'])
        self.assertEqual(code, 1)
        self.assertIn('read only variable', out)
        code, out, _err, _conn = self.cli('mysql-8.4', 'denied', ['set', 'max_connections', '260', '--yes'])
        self.assertEqual(code, 1)

    def test_reset_persist(self):
        code, out, err, _conn = self.cli('mysql-8.4', 'reset-persist', ['reset-persist', 'max_connections', '--yes'])
        self.assertEqual(code, 0, err)
        self.assertIn('Removed max_connections from mysqld-auto.cnf', out)

    def test_bad_input_exits_2(self):
        code, _out, err, conn = self.cli('mysql-8.4', 'global', ['set', 'bad name', '1', '--yes'])
        self.assertEqual(code, 2)
        self.assertIn('not a system variable name', err)
        self.assertEqual(conn.calls, [])
        code, _out, _err, _conn = self.cli('mysql-8.4', 'global', ['set', 'max_connections', '1', '--persist',
                                                                   '--session'])
        self.assertEqual(code, 2)

    def test_connection_failure_exits_2(self):
        from myflames.connector import ConnectorError
        conn = ReplayConn([], error=ConnectorError("mysql client exited 1: ERROR 2003 (HY000): Can't connect"))
        code, _out, err, _factory = run_cli('config', ['set', 'max_connections', '300', '--yes', '-phunter2-not-real'],
                                            conn)
        self.assertEqual(code, 2)
        self.assertIn("Can't connect", err)
        self.assertEqual(self.audit()[0]['status'], 'error')

    def test_post_write_read_failure_exits_1_and_audits_unknown(self):
        from myflames.connector import ConnectorError
        with mock.patch.object(V, '_after', side_effect=ConnectorError('ERROR 2013: Lost connection')):
            code, out, err, conn = self.cli('mysql-8.4', 'global',
                                           ['set', 'max_connections', '300', '--yes', '--json'])
        self.assertEqual(code, 1, err)
        self.assertEqual(json.loads(out)['status'], 'applied_unverified')
        self.assertEqual((self.audit()[0]['status'], self.audit()[0]['outcome']),
                         ('unknown', 'applied_unverified'))
        self.assertEqual(len([sql for sql, _flags in conn.calls if 'SET GLOBAL' in sql]), 1)

    def test_read_only_profile_refuses_before_connecting(self):
        from myflames.workspace_store import WorkspaceStore
        WorkspaceStore().save('profile', {'name': 'locked', 'host': '127.0.0.1', 'port': 34811, 'user': 'root',
                                          'type': 'development', 'read_only': True})
        conn = ReplayConn([])
        code, out, err, factory = run_cli('config', ['set', 'max_connections', '300', '--profile', 'locked',
                                                      '--yes'], conn)
        self.assertEqual(code, 2)
        self.assertIn('read-only in myflames', err)
        self.assertIsNone(factory.options, 'no connection was opened')
        self.assertEqual(self.audit()[0]['outcome'], 'read_only')

    def test_review_still_reads_only(self):
        """``myflames config`` without set or reset-persist is the read-only review."""
        from _perf_replay import replay as perf_replay
        conn, _data = perf_replay('config', 'mysql-8.4')
        code, out, _err, _factory = run_cli('config', [], conn)
        self.assertEqual(code, 0)
        self.assertIn('Status:', out)

    def test_development_session_change_needs_yes_on_non_tty(self):
        from myflames.workspace_store import WorkspaceStore
        WorkspaceStore().save('profile', {'name': 'dev', 'host': '127.0.0.1', 'port': 34811, 'user': 'root',
                                          'type': 'development'})
        for server in ('mysql-8.4', 'mariadb-11.4'):
            with self.subTest(server=server):
                value = fixture('variable', server + '-session')['args']['value']
                args = ['set', 'sort_buffer_size', value, '--session', '--profile', 'dev']
                code, out, err, conn = self.cli(server, 'session', args)
                self.assertEqual(code, 2, err)
                self.assertIn('--yes', err)
                self.assertFalse(any('SET SESSION' in sql for sql, _ in conn.calls))
                record = self.audit()[0]
                self.assertEqual(record['policy']['action'], 'allow')
                self.assertEqual((record['status'], record['outcome']), ('refused', 'no_terminal'))
                self.assertTrue(record['finished_at'])
                code, _, err, _ = self.cli(server, 'session', args + ['--yes'])
                self.assertEqual(code, 0, err)
                code, _, err, _ = self.cli(server, 'session', args, tty=True)
                self.assertEqual(code, 0, err)
                self.assertNotIn('[y/N]', err)

    def test_direct_read_only_refuses_before_prompt_and_is_audited(self):
        import io
        from myflames.workspace_store import WorkspaceStore
        conn, _data = replay('mysql-8.4', 'global')
        audit = dbops.AuditTrail(WorkspaceStore(), 'set_global', 'cli')
        stdin = mock.Mock()
        stdin.isatty.return_value = True
        decision = dbops.decide('risky', read_only=True)
        with mock.patch.object(V, 'apply_change', side_effect=AssertionError('must refuse before applying')):
            code, result = V.change_cli(conn, 'max_connections', '300', 'global', stdin=stdin,
                                        stderr=io.StringIO(), decision=decision, audit=audit)
        self.assertEqual((code, result['status']), (1, 'refused'))
        stdin.readline.assert_not_called()
        self.assertEqual(self.audit()[0]['outcome'], 'read_only')
        self.assertTrue(self.audit()[0]['finished_at'])

    def test_invalid_plan_is_audited_before_prompt(self):
        code, _, err, conn = self.cli('mariadb-11.4', 'persist',
                                     ['set', 'max_connections', '300', '--persist'], tty=True)
        self.assertEqual(code, 2, err)
        self.assertNotIn('[y/N]', err)
        self.assertFalse(any('SET PERSIST' in sql for sql, _ in conn.calls))
        self.assertEqual((self.audit()[0]['status'], self.audit()[0]['outcome']), ('refused', 'invalid'))
        self.assertTrue(self.audit()[0]['finished_at'])



class TestEndpoints(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.dict(os.environ, {'HOME': self.temp.name, 'USERPROFILE': self.temp.name,
                                               'MYFLAMES_SECRET_BACKEND': 'vault'})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        from myflames.workspace_store import WorkspaceStore
        self.workspace = types.SimpleNamespace(store=WorkspaceStore(Path(self.temp.name) / 'w.sqlite3'))
        self.connection = {'host': '127.0.0.1', 'port': 34811, 'user': 'root', 'password': 'hunter2-not-real'}

    def call(self, path, payload, conn):
        from myflames.ui import _variable_response
        factory = FakeFactory(conn)
        with mock.patch('myflames.connector.MySQLConnection', factory):
            return _variable_response(path, payload, self.workspace), factory

    def test_paths_are_registered(self):
        from myflames import ui
        self.assertIn('/api/live/variable/apply', ui._WRITE_PATHS)
        for path in ('/api/live/variable', '/api/live/variable/plan', '/api/live/variable/apply'):
            self.assertIn(path, ui._CONNECTION_PATHS)
            self.assertNotIn(path, ui._SERVER_VIEW_PATHS)

    def test_read_plan_apply(self):
        conn, _data = replay('mysql-8.4', 'global')
        plan, _ = self.call('/api/live/variable/plan', {'connection': self.connection, 'name': 'max_connections',
                                                        'value': '300', 'mode': 'global'}, conn)
        self.assertEqual(plan['status'], 'ready')
        self.assertEqual(plan['policy']['action'], 'confirm')
        self.assertEqual(plan['policy']['tone'], 'danger')
        result, factory = self.call('/api/live/variable/apply', {
            'connection': self.connection, 'name': 'max_connections', 'value': '300', 'mode': 'global',
            'expect': plan['expect'], 'confirmed': True}, conn)
        self.assertEqual(result['status'], 'changed')
        self.assertEqual(factory.options['password'], 'hunter2-not-real')
        record = dbops.audit_report(self.workspace.store)['records'][0]
        self.assertEqual((record['surface'], record['status']), ('ui', 'ok'))
        conn, _data = replay('mysql-8.4', 'restart')
        read, _ = self.call('/api/live/variable', {'connection': self.connection, 'name': 'max_connections'}, conn)
        self.assertEqual((read['schema_version'], read['persisted']['value']), ('variable-1.0', '400'))

    def test_confirmed_plan_is_bound_to_connection_host_port_tls_and_profile(self):
        profile = self.workspace.store.save('profile', dict(self.connection, name='same endpoint'))
        changes = ({'host': 'other-db'}, {'port': 34812}, {'user': 'other-user'},
                   {'ssl_mode': 'REQUIRED'}, {'ssl_ca': '/tmp/another-ca.pem'}, {'profile_id': profile['id']})
        for change in changes:
            with self.subTest(change=change):
                conn, _data = replay('mysql-8.4', 'global')
                plan, _factory = self.call('/api/live/variable/plan', {
                    'connection': self.connection, 'name': 'max_connections', 'value': '300', 'mode': 'global'}, conn)
                self.assertEqual(plan['status'], 'ready')
                self.assertIn('connection_identity', plan['expect'])
                calls = len(conn.calls)
                result, factory = self.call('/api/live/variable/apply', {
                    'connection': dict(self.connection, **change), 'name': 'max_connections', 'value': '300',
                    'mode': 'global', 'expect': plan['expect'], 'confirmed': True}, conn)
                self.assertEqual(result['status'], 'refused')
                self.assertIsNone(factory.options, 'a different endpoint must refuse before opening a connection')
                self.assertEqual(len(conn.calls), calls)

    def test_explicit_json_null_reaches_the_nullable_variable_plan(self):
        v = mysql_variable('character_set_results', global_value='utf8mb4', session_value='utf8mb4')
        conn = ReplayConn([[TAG + 'SHOW GRANTS', '']])
        with mock.patch.object(V, 'read_variable', return_value=v):
            plan, _factory = self.call('/api/live/variable/plan', {
                'connection': self.connection, 'name': 'character_set_results', 'value': None, 'mode': 'session'}, conn)
        self.assertEqual(plan['status'], 'ready')
        self.assertEqual(plan['statement'], 'SET SESSION character_set_results = NULL')
        with mock.patch.object(V, 'apply_change', return_value={'status': 'changed'}) as apply:
            result, _factory = self.call('/api/live/variable/apply', {
                'connection': self.connection, 'name': 'character_set_results', 'value': None,
                'mode': 'session', 'expect': plan['expect'], 'confirmed': True}, conn)
        self.assertEqual(result['status'], 'changed')
        self.assertIsNone(apply.call_args[0][1]['requested'])

    def test_cancel_pending_or_refused_plan_is_audited_without_connecting(self):
        for reviewed in (None, 'SET PERSIST_ONLY max_connections = 300'):
            with self.subTest(reviewed=reviewed):
                conn = ReplayConn([])
                payload = {'connection': self.connection, 'name': 'max_connections', 'value': '300',
                           'mode': 'persist_only', 'declined': True}
                if reviewed is not None:
                    payload['reviewed_statement'] = reviewed
                    payload['expect'] = {'name': 'max_connections', 'mode': 'persist_only', 'statement': reviewed}
                result, factory = self.call('/api/live/variable/apply', payload, conn)
                self.assertEqual(result['status'], 'declined')
                self.assertIsNone(factory.options)
                self.assertEqual(conn.calls, [])
                records = dbops.audit_report(self.workspace.store)['records']
                record = next(r for r in records if r['id'] == result['audit_id'])
                self.assertEqual((record['status'], record['outcome']), ('declined', 'declined'))
                self.assertEqual(record['statements'], [TAG + reviewed] if reviewed else [])
                self.assertNotIn('hunter2', json.dumps(record))

    def test_apply_needs_confirmation_and_respects_read_only(self):
        conn, _data = replay('mysql-8.4', 'global')
        plan, _ = self.call('/api/live/variable/plan', {'connection': self.connection, 'name': 'max_connections',
                                                        'value': '300', 'mode': 'global'}, conn)
        calls = len(conn.calls)
        payload = {'connection': self.connection, 'name': 'max_connections', 'value': '300', 'mode': 'global',
                   'expect': plan['expect']}
        result, factory = self.call('/api/live/variable/apply', payload, conn)
        self.assertEqual(result['status'], 'needs_confirmation')
        self.assertIsNone(factory.options, 'no connection without the confirmation')
        result, factory = self.call('/api/live/variable/apply', dict(
            payload, confirmed=True, connection=dict(self.connection, read_only=True)), conn)
        self.assertEqual(result['status'], 'refused')
        self.assertIsNone(factory.options)
        self.assertEqual(len(conn.calls), calls)
        statuses = [(r['status'], r['outcome']) for r in dbops.audit_report(self.workspace.store)['records']]
        self.assertEqual(sorted(statuses), [('refused', 'needs_confirmation'), ('refused', 'read_only')])

    def test_bad_requests(self):
        conn = ReplayConn([])
        for path, payload in (('/api/live/variable', {'name': 'x y'}),
                              ('/api/live/variable/plan', {'name': 'max_connections', 'value': '1', 'mode': 'x'}),
                              ('/api/live/variable/apply', {'name': 'max_connections', 'mode': 'global',
                                                            'expect': {}, 'confirmed': True})):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.call(path, dict(payload, connection=self.connection), conn)
        self.assertEqual(conn.calls, [])

    def test_password_never_reaches_an_error(self):
        from myflames.connector import ConnectorError
        conn = ReplayConn([], error=ConnectorError('ERROR 1045: Access denied (using password hunter2-not-real)'))
        with self.assertRaises(ConnectorError) as caught:
            self.call('/api/live/variable', {'connection': self.connection, 'name': 'max_connections'}, conn)
        self.assertNotIn('hunter2', str(caught.exception))


if __name__ == '__main__':
    unittest.main()


class TestGeneratedCatalogTypes(unittest.TestCase):
    """Facts the generated catalog adds on top of the hand-verified map."""

    def test_generated_flagsets_take_their_flags_from_the_live_value(self):
        from myflames import variables as V
        info = {"name": "optimizer_trace", "engine": "mysql", "global_value": "enabled=off,one_line=off",
                "session_value": None, "catalog": None, "metadata": None, "platform": None}
        self.assertEqual(V._type(info), ("flagset", ["enabled", "one_line"]))
        info = dict(info, name="optimizer_trace_features",
                    global_value="greedy_search=on,range_optimizer=on,dynamic_range=on,repeated_subselect=on")
        self.assertEqual(V._type(info)[1], ["greedy_search", "range_optimizer", "dynamic_range", "repeated_subselect"])

    def test_count_sizes_are_not_bytes(self):
        from myflames import variables as V
        for name in ("host_cache_size", "thread_cache_size", "performance_schema_accounts_size",
                     "performance_schema_events_waits_history_size", "binlog_transaction_dependency_history_size",
                     "max_join_size", "innodb_purge_batch_size", "ngram_token_size"):
            self.assertFalse(V._is_bytes(name), name)
        for name in ("sort_buffer_size", "binlog_cache_size", "innodb_log_write_ahead_size",
                     "replica_pending_jobs_size_max", "key_buffer_size"):
            self.assertTrue(V._is_bytes(name), name)


class TestServerSetValues(unittest.TestCase):
    """A value no startup option sets: no PERSIST_ONLY and no option file line."""

    def info(self, engine, name, catalog=None):
        return {"name": name, "engine": engine, "server_version": "11.4.13" if engine == "mariadb" else "8.4.10",
                "server_identity": None, "platform": "Linux", "read_at": "2026-10-18T00:00:00+00:00",
                "version_tuple": (11, 4, 13) if engine == "mariadb" else (8, 4, 10), "in_global": True,
                "in_session": False, "global_value": "db-host", "session_value": None,
                "persisted_globals_load": "ON", "related": {}, "catalog": catalog, "info": None,
                "persisted": {"present": False, "value": None, "read": True}, "metadata": None, "notes": []}

    def test_mariadb_hostname_offers_no_option_file_line(self):
        from myflames import variables as V
        catalog = {"name": "HOSTNAME", "session_value": None, "global_value": "db-host", "origin": "COMPILE-TIME",
                   "default": "", "scope": "GLOBAL", "type": "VARCHAR", "min": None, "max": None, "block": None,
                   "values": None, "read_only": "YES", "path": None, "comment": "Server host name",
                   "command_line_argument": None}
        v = V.describe(self.info("mariadb", "hostname", catalog))
        self.assertIs(v["startup_option"], False)
        plan = V.build_plan(v, "other-host", "global")
        self.assertEqual(plan["status"], "refused")
        self.assertIsNone(plan["alternative"]["config_line"])
        self.assertIn("The server sets hostname", plan["alternative"]["text"])

    def test_mariadb_static_with_option_keeps_its_option_file_line(self):
        from myflames import variables as V
        catalog = {"name": "PORT", "session_value": None, "global_value": "3306", "origin": "CONFIG",
                   "default": "3306", "scope": "GLOBAL", "type": "INT UNSIGNED", "min": "0", "max": "65535",
                   "block": "1", "values": None, "read_only": "YES", "path": None, "comment": "Port",
                   "command_line_argument": "REQUIRED"}
        v = V.describe(dict(self.info("mariadb", "port", catalog), global_value="3306"))
        self.assertIs(v["startup_option"], True)
        self.assertTrue(V.build_plan(v, "3307", "global")["alternative"]["config_line"])

    def test_mysql_catalog_no_option_variable_has_no_persist_only(self):
        from myflames import variables as V
        v = V.describe(dict(self.info("mysql", "version"), global_value="8.4.10"))
        self.assertIs(v["startup_option"], False)
        self.assertFalse(v["modes"]["persist_only"]["available"])
        self.assertIn("The server sets version", V.build_plan(v, "9.0", "global")["alternative"]["text"])
