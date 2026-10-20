"""Configuration review: ``myflames config``, the Parameters view API, and the rules.

Replays live captures (see _perf_replay) of default-configured servers, then
checks each rule with edited variables and status counters.
"""
import inspect
import json
import unittest
from unittest import mock

from _perf_replay import (MARIADB_OFF, MARIADB_PS, MYSQL, PASSWORD, SERVERS, FakeFactory, ReplayConn,
                          assert_read_only, replay, run_cli, run_main)
from myflames import config_review, ui
from myflames.advisor import advise, flush_log_durability
from myflames.connector import ConnectorError
from myflames.perf_common import Server

TAG = '/*myflames:config*/ '
KEYS = ['buffer_pool', 'sort_buffer_size', 'join_buffer_size', 'tmp_tables', 'flush_log', 'sync_binlog',
        'flush_method', 'redo', 'max_connections', 'table_open_cache', 'performance_schema', 'slow_log',
        'io_capacity']
CHECK_FIELDS = {'key', 'group', 'label', 'explain', 'value', 'status', 'why', 'rule', 'caveat', 'action', 'settings',
                'sources'}
MIB = 1 << 20

MYSQL_VARS = {'version': '8.4.11', 'version_comment': 'MySQL Community Server - GPL', 'performance_schema': 'ON',
              'innodb_buffer_pool_size': str(128 * MIB), 'innodb_redo_log_capacity': str(100 * MIB),
              'innodb_flush_log_at_trx_commit': '1', 'sync_binlog': '1', 'log_bin': 'ON',
              'innodb_flush_method': 'O_DIRECT', 'max_connections': '151', 'table_open_cache': '4000',
              'tmp_table_size': str(16 * MIB), 'max_heap_table_size': str(16 * MIB),
              'internal_tmp_mem_storage_engine': 'TempTable', 'temptable_max_ram': str(1 << 30),
              'sort_buffer_size': '262144', 'join_buffer_size': '262144', 'innodb_io_capacity': '10000',
              'slow_query_log': 'OFF', 'long_query_time': '10.000000', 'innodb_dedicated_server': 'OFF',
              'thread_handling': 'one-thread-per-connection'}
MARIADB_VARS = {'version': '11.4.13-MariaDB', 'version_comment': 'mariadb.org binary distribution',
                'performance_schema': 'OFF', 'innodb_buffer_pool_size': str(128 * MIB),
                'innodb_log_file_size': str(96 * MIB), 'innodb_flush_log_at_trx_commit': '1', 'sync_binlog': '0',
                'log_bin': 'OFF', 'innodb_flush_method': 'O_DIRECT', 'max_connections': '151',
                'table_open_cache': '2000', 'tmp_table_size': str(16 * MIB), 'tmp_memory_table_size': str(16 * MIB),
                'max_heap_table_size': str(16 * MIB), 'sort_buffer_size': str(2 * MIB),
                'join_buffer_size': '262144', 'innodb_io_capacity': '200', 'slow_query_log': 'OFF',
                'log_slow_query': 'OFF', 'long_query_time': '10.000000', 'log_slow_query_time': '10.000000',
                'thread_handling': 'one-thread-per-connection', 'thread_pool_size': '8'}
STATUS = {'Uptime': '86400', 'Max_used_connections': '20', 'Connection_errors_max_connections': '0',
          'Opened_tables': '1000', 'Open_tables': '500', 'Created_tmp_tables': '1000', 'Created_tmp_disk_tables': '10',
          'Innodb_os_log_written': str(864 * MIB)}


def compute(variables=None, status=None, engine='mysql', tablespaces=64 * MIB, sources=None, **changes):
    base = dict(MYSQL_VARS if engine == 'mysql' else MARIADB_VARS)
    base.update(variables or {})
    for key, value in list(base.items()):
        if value is None:
            del base[key]
    stat = dict(STATUS)
    stat.update(status or {})
    server = Server(base, {'Uptime': stat['Uptime']})
    raw = {'tablespaces': tablespaces, 'sources': sources}
    raw.update(changes)
    return config_review.compute_config(server, stat, raw)


def check(report, key):
    return [c for c in report['checks'] if c['key'] == key][0]


class ConfigCase(unittest.TestCase):
    maxDiff = None

    def report(self, name):
        conn, _ = replay('config', name)
        result = config_review.config_report(conn)
        assert_read_only(self, conn, TAG)
        json.dumps(result, allow_nan=False)
        self.assertEqual(result['schema_version'], 'config-1.0')
        for c in result['checks']:
            self.assertEqual(set(c), CHECK_FIELDS, c['key'])
            self.assertIn(c['status'], config_review.STATUSES)
            self.assertTrue(c['why'] and c['explain'])
            if c['status'] in ('ok', 'watch', 'problem'):
                self.assertIn('Rule of thumb: ', c['why'])
                self.assertIn(c['rule'], config_review.RULES.values())
            if c['status'] in ('watch', 'problem'):
                self.assertTrue(c['caveat'], c['key'])
                self.assertTrue(c['action'], c['key'])
        counts = {s: sum(1 for c in result['checks'] if c['status'] == s) for s in config_review.STATUSES}
        self.assertEqual({k: result['summary'][k] for k in config_review.STATUSES}, counts)
        return result, conn


class TestLiveReplay(ConfigCase):
    def test_every_server(self):
        for name in SERVERS:
            with self.subTest(server=name):
                result, conn = self.report(name)
                keys = [c['key'] for c in result['checks']]
                self.assertEqual(keys[:13], KEYS)
                mariadb = name.startswith('mariadb')
                self.assertEqual('dedicated_server' in keys, not mariadb)
                # Default servers: buffer pool fits, durable commits, defaults recognized.
                self.assertEqual(check(result, 'buffer_pool')['status'], 'ok')
                self.assertEqual(check(result, 'flush_log')['status'], 'ok')
                self.assertEqual(check(result, 'slow_log')['status'], 'watch')
                pool = check(result, 'buffer_pool')['settings'][0]
                self.assertEqual((pool['display'], pool['default']), ('128.0 MiB', '128.0 MiB'))
                self.assertEqual(pool['set_by'], 'default')
                sort = check(result, 'sort_buffer_size')['settings'][0]
                self.assertEqual(sort['default'], '2.0 MiB' if mariadb else '256.0 KiB')
                self.assertEqual(check(result, 'performance_schema')['status'],
                                 'watch' if name in MARIADB_OFF else 'ok')
                slow = check(result, 'slow_log')
                self.assertIn('log_slow_query = ON' if mariadb else 'slow_query_log = ON', slow['action']['sql'])
                sync = check(result, 'sync_binlog')
                if mariadb:
                    self.assertEqual(sync['status'], 'info')
                    self.assertIn('log_bin=OFF', sync['why'])
                    self.assertEqual(sync['settings'][0]['default'], '0')
                    self.assertIn('Aria', check(result, 'tmp_tables')['explain'])
                    self.assertIn('deprecated in MariaDB 11.0.1', check(result, 'flush_method')['explain'])
                else:
                    self.assertEqual(sync['status'], 'ok')
                    self.assertIn('TempTable', check(result, 'tmp_tables')['explain'])
                    self.assertTrue(any('INNODB_TABLESPACES' in sql for sql, _ in conn.calls))
                    self.assertTrue(any('variables_info' in sql for sql, _ in conn.calls))
                # Young containers: the redo rate is not judged.
                self.assertEqual(check(result, 'redo')['status'], 'info')

    def test_mariadb_reads_system_variables_and_aliases(self):
        result, conn = self.report('mariadb-13.0')
        self.assertTrue(any('information_schema.SYSTEM_VARIABLES' in sql for sql, _ in conn.calls))
        self.assertTrue(any('INNODB_SYS_TABLESPACES WHERE SPACE <> 0' in sql for sql, _ in conn.calls))
        slow = check(result, 'slow_log')['settings']
        self.assertEqual((slow[0]['name'], slow[0]['alias']), ('log_slow_query', 'slow_query_log'))
        tmp = check(result, 'tmp_tables')['settings'][0]
        self.assertEqual((tmp['name'], tmp['alias']), ('tmp_memory_table_size', 'tmp_table_size'))
        io = check(result, 'io_capacity')
        self.assertEqual(io['value'], '200')
        self.assertEqual(io['settings'][0]['default'], '200')
        result, _ = self.report('mariadb-11.4')
        self.assertEqual(check(result, 'performance_schema')['settings'][0]['set_by'], 'command line')

    def test_text_output(self):
        result, _ = self.report('mariadb-13.0-default')
        text = config_review.format_text(result)
        self.assertTrue(text.startswith('MariaDB 13.0.2'))
        self.assertIn('Status: WATCH (0 problem, 2 watch', text)
        self.assertRegex(text, r'\n  WATCH +Performance Schema +OFF')
        self.assertIn('    caveat: Turning it on needs a restart', text)


class TestRules(ConfigCase):
    def test_buffer_pool_vs_tablespaces(self):
        self.assertEqual(check(compute(tablespaces=200 * MIB), 'buffer_pool')['status'], 'ok')
        small = check(compute(tablespaces=300 * MIB), 'buffer_pool')
        self.assertEqual(small['status'], 'watch')
        self.assertIn('is smaller than the InnoDB tablespace files (300.0 MiB)', small['why'])
        tiny = check(compute(tablespaces=600 * MIB), 'buffer_pool')
        self.assertIn('much smaller than', tiny['why'])
        self.assertIn('hit ratio in Health', tiny['why'])
        denied = check(compute(tablespaces=None, tablespaces_error='ERROR 1227 (42000): Access denied'), 'buffer_pool')
        self.assertEqual(denied['status'], 'unknown')
        self.assertIn('needs the PROCESS privilege', denied['why'])

    def test_durability(self):
        for value, status in (('1', 'ok'), ('2', 'watch'), ('0', 'watch')):
            with self.subTest(value=value):
                result = check(compute({'innodb_flush_log_at_trx_commit': value}), 'flush_log')
                self.assertEqual(result['status'], status)
        mariadb3 = check(compute({'innodb_flush_log_at_trx_commit': '3'}, engine='mariadb'), 'flush_log')
        self.assertEqual(mariadb3['status'], 'ok')
        two = check(compute({'innodb_flush_log_at_trx_commit': '2'}), 'flush_log')
        self.assertIn('fsynced once per second', two['why'])
        self.assertEqual(two['action']['sql'], 'SET GLOBAL innodb_flush_log_at_trx_commit = 1;')

    def test_advisor_no_longer_calls_three_unsafe(self):
        self.assertIsNone(flush_log_durability(3))
        self.assertIsNone(flush_log_durability(1))
        self.assertIn('fsynced once per second', flush_log_durability(2))
        analysis = {'query_text_lines': ['UPDATE t SET a = 1']}
        advise(analysis, variables={'innodb_flush_log_at_trx_commit': '3'})
        self.assertFalse([f for f in analysis['environment_findings'] if f['rule_id'].startswith('FLUSH_LOG')])

    def test_sync_binlog(self):
        self.assertEqual(check(compute({'sync_binlog': '1'}), 'sync_binlog')['status'], 'ok')
        zero = check(compute({'sync_binlog': '0'}), 'sync_binlog')
        self.assertEqual(zero['status'], 'watch')
        self.assertIn('the OS decides', zero['why'])
        hundred = check(compute({'sync_binlog': '100'}), 'sync_binlog')
        self.assertIn('every 100 commit groups', hundred['why'])
        self.assertEqual(check(compute({'log_bin': 'OFF', 'sync_binlog': '0'}), 'sync_binlog')['status'], 'info')

    def test_redo(self):
        # 864 MiB in a day is 10 KiB/s: 100 MiB holds hours.
        self.assertEqual(check(compute(), 'redo')['status'], 'ok')
        # 8,640 MiB in a day is 0.1 MiB/s: 100 MiB holds 1,000 s.
        busy = check(compute(status={'Innodb_os_log_written': str(8640 * MIB)}), 'redo')
        self.assertEqual(busy['status'], 'watch')
        self.assertIn('so it holds about 16 min 40 s', busy['why'])
        # An hour at 0.1 MiB/s is 360 MiB, rounded up to 512 MiB.
        self.assertEqual(busy['action']['sql'], 'SET GLOBAL innodb_redo_log_capacity = 536870912;')
        self.assertIn('re-counts rewrites', busy['explain'])
        young = check(compute(status={'Uptime': '600'}), 'redo')
        self.assertEqual(young['status'], 'info')
        # MySQL's capacity in use wins over the variable; MariaDB uses innodb_log_file_size.
        resized = check(compute(status={'Innodb_redo_log_capacity_resized': str(2048 * MIB),
                                        'Innodb_redo_log_current_lsn': str(5 * MIB),
                                        'Innodb_redo_log_checkpoint_lsn': str(4 * MIB)}), 'redo')
        self.assertEqual(resized['value'], '2.0 GiB')
        self.assertIn('the checkpoint age is 1.0 MiB now', resized['why'])
        legacy = check(compute({'innodb_redo_log_capacity': None, 'innodb_log_file_size': str(48 * MIB),
                                'innodb_log_files_in_group': '2'}), 'redo')
        self.assertEqual(legacy['value'], '96.0 MiB')
        mariadb = check(compute(engine='mariadb', status={'Innodb_os_log_written': str(8640 * MIB),
                                                          'Innodb_checkpoint_age': str(48 * MIB)}), 'redo')
        self.assertEqual(mariadb['value'], '96.0 MiB')
        self.assertIn('SET GLOBAL innodb_log_file_size', mariadb['action']['sql'])
        self.assertIn('the checkpoint age is 48.0 MiB now', mariadb['why'])

    def test_legacy_mysql_redo_watch_recommends_configuration_and_restart(self):
        # Only MySQL has the legacy two-variable capacity; MariaDB uses one file.
        result = check(compute({'version': '8.0.29', 'innodb_redo_log_capacity': None,
                                'innodb_log_file_size': str(48 * MIB), 'innodb_log_files_in_group': '2'},
                               status={'Innodb_os_log_written': str(8640 * MIB)}), 'redo')
        self.assertEqual(result['status'], 'watch')
        action = result['action']
        self.assertNotIn('sql', action)
        self.assertNotIn('innodb_redo_log_capacity', action['config'])
        self.assertIn('innodb_log_file_size = 268435456', action['config'])
        self.assertIn('innodb_log_files_in_group = 2', action['config'])
        self.assertIn('restart', action['text'])

    def test_correctness_regression_mariadb_redo_resize_version(self):
        # MariaDB has one redo file; MySQL's legacy multi-file path is separate.
        for version in ('10.6.25', '10.8.8', '10.9.0', '10.9.1', '10.11.19', '11.4.13'):
            with self.subTest(version=version):
                result = check(compute({'version': version + '-MariaDB'}, engine='mariadb',
                                       status={'Innodb_os_log_written': str(8640 * MIB)}), 'redo')
                self.assertEqual(result['status'], 'watch')
                action = result['action']
                # An hour at 0.1 MiB/s rounds up to 512 MiB total, in one file.
                self.assertEqual(action['config'], 'innodb_log_file_size = 536870912  # under [mysqld]')
                if version in ('10.6.25', '10.8.8', '10.9.0'):
                    self.assertNotIn('sql', action)
                    self.assertNotIn('SET ', json.dumps(action))
                    self.assertIn('restart', action['text'])
                else:
                    self.assertEqual(action['sql'], 'SET GLOBAL innodb_log_file_size = 536870912;')

    def test_correctness_regression_temptable_per_table_version(self):
        # The 8.0.28 boundary affects TempTable, never the MEMORY engine or MariaDB.
        for version in ('8.0.27', '8.0.28', '8.4.11'):
            for storage in ('TempTable', 'MEMORY'):
                with self.subTest(version=version, storage=storage):
                    result = check(compute({'version': version, 'internal_tmp_mem_storage_engine': storage},
                                           status={'Created_tmp_disk_tables': '300'}), 'tmp_tables')
                    self.assertEqual(result['status'], 'watch')
                    text = result['explain'] + json.dumps(result['action'])
                    if version == '8.0.27' and storage == 'TempTable':
                        self.assertIn('Before MySQL 8.0.28 TempTable has no per-table limit', result['explain'])
                        self.assertNotIn('tmp_table_size', text)
                        self.assertIn('temptable_max_ram (global)', text)
                        self.assertIn('all TempTable tables share', result['caveat'])
                        self.assertNotIn('tmp_table_size', [v['name'] for v in result['settings']])
                    else:
                        self.assertIn('tmp_table_size', result['explain'])
                        self.assertIn('SET SESSION tmp_table_size', result['action']['text'])

    def test_connections_and_table_cache(self):
        self.assertEqual(check(compute(status={'Max_used_connections': '119'}), 'max_connections')['status'], 'ok')
        self.assertEqual(check(compute(status={'Max_used_connections': '121'}), 'max_connections')['status'], 'watch')
        refused = check(compute(status={'Connection_errors_max_connections': '3'}), 'max_connections')
        self.assertEqual(refused['status'], 'problem')
        self.assertIn('3 connection attempts were refused', refused['why'])
        self.assertEqual(check(compute(), 'table_open_cache')['status'], 'ok')
        churn = compute(status={'Open_tables': '4000', 'Opened_tables': str(2 * 86400)})
        self.assertEqual(check(churn, 'table_open_cache')['status'], 'watch')
        not_full = compute(status={'Open_tables': '100', 'Opened_tables': str(2 * 86400)})
        self.assertEqual(check(not_full, 'table_open_cache')['status'], 'ok')

    def test_temporary_tables_per_engine(self):
        disk = {'Created_tmp_tables': '1000', 'Created_tmp_disk_tables': '300'}
        temptable = check(compute(status=disk), 'tmp_tables')
        self.assertEqual(temptable['status'], 'watch')
        self.assertIn('temptable_max_ram (1.0 GiB)', temptable['explain'])
        memory = check(compute({'internal_tmp_mem_storage_engine': 'MEMORY', 'max_heap_table_size': str(8 * MIB)},
                               status=disk), 'tmp_tables')
        self.assertIn('smaller of tmp_table_size and max_heap_table_size (8.0 MiB)', memory['explain'])
        mariadb = check(compute(engine='mariadb', status=disk), 'tmp_tables')
        self.assertIn('counts a table that moves to disk twice', mariadb['explain'])
        few = check(compute(status={'Created_tmp_tables': '50', 'Created_tmp_disk_tables': '50'}), 'tmp_tables')
        self.assertEqual(few['status'], 'ok')

    def test_session_buffers(self):
        big = check(compute({'sort_buffer_size': str(64 * MIB)}), 'sort_buffer_size')
        self.assertEqual(big['status'], 'watch')
        self.assertEqual(big['action']['sql'], 'SET GLOBAL sort_buffer_size = 262144;')
        self.assertIn('new sessions only', big['caveat'])
        self.assertEqual(check(compute({'join_buffer_size': str(8 * MIB)}), 'join_buffer_size')['status'], 'ok')
        mariadb = check(compute({'sort_buffer_size': str(32 * MIB)}, engine='mariadb'), 'sort_buffer_size')
        self.assertEqual(mariadb['action']['sql'], 'SET GLOBAL sort_buffer_size = 2097152;')

    def test_reviewed_wording(self):
        # MariaDB caps online growth at innodb_buffer_pool_size_max.
        capped = check(compute({'innodb_buffer_pool_size_max': '0'}, engine='mariadb', tablespaces=600 * MIB),
                       'buffer_pool')
        self.assertIn('caps it at the startup size', capped['action']['text'])
        roomy = check(compute({'innodb_buffer_pool_size_max': str(8 << 40)}, engine='mariadb', tablespaces=600 * MIB),
                      'buffer_pool')
        self.assertIn('up to innodb_buffer_pool_size_max (8.0 TiB)', roomy['action']['text'])
        self.assertIn('resized online.', check(compute(tablespaces=600 * MIB), 'buffer_pool')['action']['text'])
        # MariaDB connections copy log_slow_query when they connect.
        self.assertIn('pooled connections keep the old setting',
                      check(compute(engine='mariadb'), 'slow_log')['caveat'])
        self.assertNotIn('pooled', check(compute(), 'slow_log')['caveat'])
        # The redo log is sized from CPUs, the buffer pool from memory.
        self.assertIn('redo log from its CPU count', check(compute(), 'dedicated_server')['explain'])
        self.assertIn('1 syncs at prepare', check(compute(engine='mariadb'), 'flush_log')['explain'])
        notes = ' '.join(compute()['notes'])
        self.assertIn('The connection peak (Max_used_connections) also restarts with FLUSH STATUS', notes)

    def test_slow_log_states(self):
        on = check(compute({'slow_query_log': 'ON', 'long_query_time': '0.500000'}), 'slow_log')
        self.assertEqual(on['status'], 'ok')
        self.assertIn('over 0.5 s', on['why'])
        late = check(compute({'slow_query_log': 'ON'}), 'slow_log')
        self.assertEqual(late['status'], 'info')

    def test_sources_and_deprecation_note(self):
        sources = [['innodb_buffer_pool_size', 'PERSISTED', None], ['max_connections', 'GLOBAL', None],
                   ['innodb_log_file_size', 'GLOBAL', None], ['innodb_redo_log_capacity', 'COMPILED', None]]
        report = compute({'innodb_log_file_size': str(48 * MIB)}, sources=sources)
        self.assertEqual(check(report, 'buffer_pool')['settings'][0]['set_by'], 'SET PERSIST')
        self.assertEqual(check(report, 'max_connections')['settings'][0]['set_by'], 'my.cnf')
        self.assertTrue(any('deprecated since MySQL 8.0.30 and removed in 9.3' in n for n in report['notes']))

    def test_fail_on_levels(self):
        report = compute(status={'Connection_errors_max_connections': '1'})
        self.assertEqual(report['summary']['status'], 'problem')
        self.assertEqual([c['key'] for c in config_review.at_or_worse(report, 'problem')], ['max_connections'])
        self.assertIn('slow_log', [c['key'] for c in config_review.at_or_worse(report, 'watch')])


class TestConfigCLIAndAPI(ConfigCase):
    def call(self, name, *argv, conn=None):
        if conn is None:
            conn, _ = replay('config', name)
        return run_cli('config', argv, conn)

    def test_json_and_fail_on(self):
        code, out, err, factory = self.call('mysql-8.4', '--json', '-u', 'app', '-p' + PASSWORD)
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(json.loads(out)['schema_version'], 'config-1.0')
        self.assertEqual(factory.options['query_timeout'], 30)
        code, out, err, _ = self.call('mysql-8.4', '--fail-on', 'watch')
        self.assertEqual(code, 1)
        self.assertIn('FAIL: 1 check(s) at --fail-on watch or worse:', err)
        self.assertIn('  [watch] Slow query log: ', err)
        code, out, err, _ = self.call('mysql-8.4', '--fail-on', 'problem')
        self.assertEqual(code, 0)
        self.assertIn('OK: no check at --fail-on problem', err)
        code, out, err, _ = self.call('mysql-8.4', '--fail-on', 'watch', '-q', '--json')
        self.assertEqual((code, err), (1, ''))

    def test_bad_input_and_failures_exit_2(self):
        conn = ReplayConn([])
        code, out, _, factory = self.call('', '--fail-on', 'warn', conn=conn)
        self.assertEqual((code, out, factory.options), (2, '', None))
        conn = ReplayConn([], error=ConnectorError('mysql client exited 1: ERROR 1045: denied ' + PASSWORD))
        code, out, err, _ = self.call('', '-p' + PASSWORD, '--fail-on', 'problem', conn=conn)
        self.assertEqual((code, out), (2, ''))
        self.assertNotIn(PASSWORD, err)
        self.assertIn('Could not read the configuration', err)

    def test_root_help(self):
        code, out, _ = run_main(['myflames', '--help'])
        self.assertIn('  config    Review key settings: engine default, source, ok/watch/problem (--fail-on)', out)
        code, out, _ = run_main(['myflames', 'config', '--help'])
        self.assertIn('--fail-on LEVEL', out)
        self.assertIn('1 a check reached the --fail-on level', ' '.join(out.split()))

    def test_api(self):
        from myflames.parameters import MARIADB_SQL
        _, recording = replay('config', 'mariadb-11.4')
        conn = ReplayConn(recording['calls'] + [[TAG + MARIADB_SQL, '']])
        factory = FakeFactory(conn)
        with mock.patch('myflames.connector.MySQLConnection', factory):
            result = ui.api_response('/api/live/config', {'connection': {'host': '127.0.0.1', 'password': PASSWORD}},
                                     workspace=object())
        self.assertEqual(result['schema_version'], 'config-1.0')
        self.assertTrue(result['parameters']['complete'])
        assert_read_only(self, conn, TAG)
        self.assertIn('"/api/live/config"', inspect.getsource(ui.WorkspaceHandler.do_POST))
        self.assertIn('/api/live/config', ui._CONNECTION_PATHS)
        conn = ReplayConn([], error=ConnectorError('mysql client exited 1: ERROR 1045: denied ' + PASSWORD))
        with mock.patch('myflames.connector.MySQLConnection', FakeFactory(conn)):
            with self.assertRaises(ValueError) as caught:
                ui.api_response('/api/live/config', {'connection': {'host': 'h', 'password': PASSWORD}},
                                workspace=object())
        self.assertNotIn(PASSWORD, str(caught.exception))


class TestRedoCheckpointShare(unittest.TestCase):
    """The checkpoint age is shown against the source's pressure points."""

    def _redo(self, engine, variables, status):
        server = mock.Mock(engine=engine, variables=variables, uptime=100000)
        return config_review._redo(config_review._Config(server, status, {}))

    def test_mysql_share_of_capacity(self):
        check = self._redo("mysql", {"innodb_redo_log_capacity": str(1 << 30)},
                           {"Uptime": "100000", "Innodb_os_log_written": "1000",
                            "Innodb_redo_log_capacity_resized": str(1 << 30),
                            "Innodb_redo_log_current_lsn": str(900 << 20), "Innodb_redo_log_checkpoint_lsn": "0"})
        self.assertIn("88% of capacity", json.dumps(check))
        self.assertIn("near 79%", json.dumps(check))

    def test_mariadb_share_of_log_size(self):
        check = self._redo("mariadb", {"innodb_log_file_size": str(1 << 30)},
                           {"Uptime": "100000", "Innodb_os_log_written": "1000",
                            "Innodb_checkpoint_age": str(512 << 20)})
        self.assertIn("50% of the log size", json.dumps(check))
