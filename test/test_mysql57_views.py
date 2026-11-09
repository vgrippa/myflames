"""MySQL 5.7 in the Server place and its CLI (5.7.44, the last 5.7 release).

Every fixture named ``*-mysql-5.7*`` was recorded live from a throwaway
mysql:5.7.44 container (linux/amd64 under emulation) seeded by
``scripts/ga-matrix.sh up mysql-5.7`` and read with
``scripts/record-perf-fixtures.py``; the replica fixtures from
``mysql-5.7-replica`` (binary log file and position, no GTIDs) and the
cluster ones from a three-member single-primary Group Replication group
(``cluster-gr-5.7-*``, ``scripts/record-cluster-fixtures.py``). The replays
answer exactly what 5.7 answered, Unknown column errors included, so each
test fails if a view starts reading a table or column 5.7 does not have.

What 5.7 lacks, checked in the mysql-5.7.44 tag and on the live server:
information_schema.STATISTICS has no IS_VISIBLE or EXPRESSION, there is no
performance_schema.variables_info, persisted_variables, data_locks or
error_log, no SET PERSIST, no TempTable, no information_schema statistics
cache, no INNODB_TABLESPACES (INNODB_SYS_TABLESPACES instead), and
replication_group_members has no MEMBER_ROLE or MEMBER_VERSION.
"""
import json
import os
import re
import subprocess
import unittest
from unittest import mock

from _perf_replay import FakeTime, assert_read_only, deny_blocker_read, fixture, replay
from myflames import (cluster, config_review, ddl_progress, discover, error_log, health, indexes, innodb_status,
                      locks, param_compare, parameters, perf_common, ps_instruments, replication, schema_health,
                      server_checks, sessions, variables, watch)
from myflames.connector import ConnectorError
from myflames.perf_common import Server

SERVER = 'mysql-5.7'
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def sent(conn):
    return [sql for sql, _flags in conn.calls]


def server57(**variables):
    values = {'version': '5.7.44-log', 'version_comment': 'MySQL Community Server (GPL)',
              'performance_schema': 'ON'}
    values.update(variables)
    return Server(values, {'Uptime': '86400'})


class TestOptionalColumns(unittest.TestCase):
    """perf_common.Reader.rows_with_columns: drop a column the server does not have, once."""

    class Conn(object):
        def __init__(self, missing):
            self.missing, self.calls = list(missing), []

        def run(self, sql, extra_flags=None, timeout=None):
            self.calls.append(sql)
            for name in self.missing:
                if re.search(r'\b%s\b' % name, sql):
                    raise ConnectorError("ERROR 1054 (42S22) at line 1: Unknown column '%s' in 'field list'" % name)
            return 'a\tb\n'

    def build(self, columns):
        return 'SELECT x, %s, %s FROM t' % (columns['IS_VISIBLE'], columns['EXPRESSION'])

    def test_each_missing_column_is_replaced_by_its_stand_in(self):
        conn = self.Conn(['IS_VISIBLE', 'EXPRESSION'])
        rows, columns = perf_common.Reader(conn, '/*t*/ ').rows_with_columns(
            self.build, {'IS_VISIBLE': "'YES'", 'EXPRESSION': 'NULL'})
        self.assertEqual(rows, [['a', 'b']])
        self.assertEqual(columns, {'IS_VISIBLE': "'YES'", 'EXPRESSION': 'NULL'})
        self.assertEqual(conn.calls[-1], "/*t*/ SELECT x, 'YES', NULL FROM t")
        self.assertEqual(len(conn.calls), 3)

    def test_a_server_with_every_column_reads_once(self):
        conn = self.Conn([])
        _, columns = perf_common.Reader(conn, '/*t*/ ').rows_with_columns(
            self.build, {'IS_VISIBLE': "'YES'", 'EXPRESSION': 'NULL'})
        self.assertEqual(columns, {'IS_VISIBLE': 'IS_VISIBLE', 'EXPRESSION': 'EXPRESSION'})
        self.assertEqual(len(conn.calls), 1)

    def test_only_8_0_13_s_expression_missing(self):
        """MySQL 8.0.0 to 8.0.12 have IS_VISIBLE but not EXPRESSION."""
        conn = self.Conn(['EXPRESSION'])
        _, columns = perf_common.Reader(conn, '/*t*/ ').rows_with_columns(
            self.build, {'IS_VISIBLE': "'YES'", 'EXPRESSION': 'NULL'})
        self.assertEqual(columns, {'IS_VISIBLE': 'IS_VISIBLE', 'EXPRESSION': 'NULL'})

    def test_other_errors_raise(self):
        conn = self.Conn([])
        conn.run = mock.Mock(side_effect=ConnectorError("ERROR 1142 (42000): SELECT command denied"))
        with self.assertRaises(ConnectorError):
            perf_common.Reader(conn, '/*t*/ ').rows_with_columns(self.build, {'IS_VISIBLE': "'YES'",
                                                                              'EXPRESSION': 'NULL'})

    def test_unknown_column_names_the_column(self):
        self.assertEqual(perf_common.unknown_column("Unknown column 'm.IS_VISIBLE' in 'field list'"), 'IS_VISIBLE')
        self.assertIsNone(perf_common.unknown_column("Unknown table 'X'"))


class TestIndexes(unittest.TestCase):
    """Owner report: Unknown column 'IS_VISIBLE' / 'EXPRESSION' on 5.7."""

    def report(self):
        conn, data = replay('indexes', SERVER)
        report = indexes.index_report(conn, **data['args'])
        assert_read_only(self, conn, indexes.INDEXES_TAG)
        return report, conn

    def test_reads_statistics_without_the_8_0_columns(self):
        report, conn = self.report()
        statistics = [sql for sql in sent(conn) if 'information_schema.STATISTICS' in sql]
        self.assertEqual(len(statistics), 3, 'IS_VISIBLE refused, then EXPRESSION, then the read')
        self.assertIn("COLLATION, INDEX_TYPE, 'YES', NULL FROM", statistics[-1])
        self.assertGreater(report['summary']['indexes'], 30)
        self.assertTrue(all(row['visible'] for row in report['indexes']))

    def test_no_invisible_index_statement_on_5_7(self):
        report, _ = self.report()
        self.assertTrue(report['unused'] and report['redundant'])
        for row in report['unused'] + report['redundant']:
            self.assertNotIn('INVISIBLE', json.dumps(row))
            if row.get('drop_sql'):
                self.assertEqual(row['test_sql'], '')
                self.assertIn(indexes.NO_INVISIBLE_CAVEAT, row['caveat'])
        text = indexes.format_text(report)
        self.assertIn('drop: ALTER TABLE `review`.`redundant_idx` DROP INDEX `idx_customer`;', text)
        self.assertNotIn('test:', text)


class TestTables(unittest.TestCase):
    """Owner report: Could not read table health: Unknown column 'EXPRESSION'."""

    def report(self, fresh=False):
        conn, data = replay('tables', SERVER)
        report = schema_health.table_report(conn, data['args']['schemas'], fresh)
        return report, conn

    def test_reads_unique_keys_without_expression(self):
        report, conn = self.report()
        keys = [sql for sql in sent(conn) if 'NON_UNIQUE = 0' in sql]
        self.assertIn('INDEX_TYPE, NULL FROM information_schema.STATISTICS', keys[-1])
        self.assertEqual([m['table'] for m in report['missing_primary_keys']], ['no_pk_events'])

    def test_free_space_tablespaces_from_innodb_sys_tablespaces(self):
        report, conn = self.report()
        self.assertTrue(any('information_schema.INNODB_SYS_TABLESPACES' in sql for sql in sent(conn)))
        self.assertTrue(report['tablespaces_known'])
        churn = next(f for f in report['fragmentation'] if f['table'] == 'churn')
        self.assertEqual((churn['status'], churn['tablespace']), ('info', 'file_per_table'))
        self.assertFalse(any('PROCESS' in note for note in report['notes']))

    def test_no_statistics_cache_on_5_7(self):
        report, _ = self.report()
        self.assertEqual(report['statistics'], {'fresh_requested': False, 'fresh': True, 'cache_expiry_s': None,
                                                'cached': False})
        self.assertTrue(any('read from the tablespace every time' in n for n in report['notes']))
        fresh, conn = self.report(fresh=True)
        self.assertFalse(any('SET SESSION information_schema_stats_expiry' in sql for sql in sent(conn)))
        self.assertTrue(any('no information_schema statistics cache' in n for n in fresh['notes']))

    def test_suggestions_are_valid_on_5_7(self):
        report, _ = self.report()
        sql = ' '.join(f['sql'] for m in report['missing_primary_keys'] for f in m['fixes'])
        self.assertNotIn('INVISIBLE', sql, 'invisible columns are MySQL 8.0.23+')
        self.assertIn('AUTO_INCREMENT PRIMARY KEY FIRST, ALGORITHM=INPLACE, LOCK=SHARED', sql)
        why = {w['key']: w['text'] for w in report['why']}
        self.assertIn('slave_rows_search_algorithms (INDEX_SCAN,TABLE_SCAN)', why['replication'])
        self.assertNotIn('functional part', why['clustering'])


class TestParameters(unittest.TestCase):
    """Owner report: variables_info / persisted_variables ERROR 1146 and Compare's Not read."""

    def report(self):
        conn, _ = replay('config', SERVER)
        return config_review.config_report(conn, parameters=True), conn

    def test_no_8_0_tables_are_read_and_the_inventory_is_complete(self):
        report, conn = self.report()
        self.assertFalse(any('variables_info' in sql or 'persisted_variables' in sql for sql in sent(conn)))
        self.assertTrue(report['parameters']['complete'])
        self.assertFalse(any('Could not read' in n for n in report['notes']))
        self.assertEqual(report['notes'].count(config_review.MYSQL57_SOURCE_NOTE), 1, 'said once')
        self.assertEqual(report['parameters']['notes'], [config_review.MYSQL57_SOURCE_NOTE])

    def test_items_on_5_7(self):
        report, _ = self.report()
        items = {i['name']: i for i in report['parameters']['items']}
        self.assertGreater(len(items), 450)
        self.assertTrue(all(i['persisted'] is False and i['source'] is None for i in items.values()))
        self.assertFalse(any(i['modifiable'] == 'restart' for i in items.values()), '5.7 has no SET PERSIST_ONLY')
        self.assertEqual(items['innodb_log_file_size']['modifiable'], 'option_file')
        # Apply type comes from the 5.7.44 release, never the 8.x catalog:
        # both are read-only in 5.7 and dynamic in 8.x.
        for name in ('innodb_log_buffer_size', 'innodb_undo_tablespaces'):
            self.assertEqual((items[name]['apply_type'], items[name]['modifiable']), ('static', 'option_file'), name)
        self.assertEqual(items['query_cache_size']['apply_type'], 'dynamic', 'a 5.7-only variable is known too')
        self.assertEqual(items['table_open_cache']['default'], '2000')
        self.assertEqual(items['innodb_io_capacity']['default'], '200')
        checks = {c['key']: c for c in report['checks']}
        self.assertIn('fsync', checks['flush_method']['why'])
        self.assertIn('INNODB_SYS_TABLESPACES', ' '.join(checks['buffer_pool']['sources']))
        self.assertEqual(checks['buffer_pool']['status'], 'ok')

    def test_compare_with_8_4_lists_what_is_only_on_one_side(self):
        def capture(name, port):
            return param_compare.capture(replay('config', name)[0], {'host': 'h', 'port': port})
        result = param_compare.compare(capture(SERVER, 1), capture('percona-8.4', 2))
        self.assertTrue(result['complete'])
        self.assertGreater(result['counts']['only_in_target'], 100)
        self.assertGreater(result['counts']['only_in_source'], 10)
        self.assertEqual(sorted(i['name'] for i in result['items'] if i['status'] == 'not_compared'),
                         ['gtid_executed', 'report_password'], 'only redacted values')

    def test_compute_parameters_needs_no_details_on_5_7(self):
        result = parameters.compute_parameters('mysql', (5, 7, 44), rows=[['max_connections', '151']],
                                               info_rows=None, persisted_rows=None)
        self.assertTrue(result['complete'])
        result = parameters.compute_parameters('mysql', (8, 4, 0), rows=[['max_connections', '151']],
                                               info_rows=None, persisted_rows=None)
        self.assertFalse(result['complete'], 'MySQL 8.0+ still needs its detail tables')


class TestVariables(unittest.TestCase):
    """The single-variable read (Change this parameter…, myflames config set)."""

    def read(self, name):
        conn, data = replay('variable', '%s-%s' % (SERVER, name))
        return variables.read_variable(conn, **data['args']), conn

    def test_persist_modes_say_5_7_has_none(self):
        v, conn = self.read('max-connections')
        self.assertEqual(len(conn.calls), 1, 'no variables_info or persisted_variables read')
        self.assertEqual(v['modes']['persist']['reason'], 'MySQL 5.7 has no SET PERSIST.')
        self.assertEqual(v['modes']['persist_only']['reason'], 'MySQL 5.7 has no SET PERSIST_ONLY.')
        self.assertEqual(v['modes']['reset_persist']['reason'], 'MySQL 5.7 has no RESET PERSIST.')
        self.assertTrue(v['modes']['global']['available'])
        self.assertFalse(v['persisted']['supported'])
        self.assertEqual(v['notes'], [config_review.MYSQL57_SOURCE_NOTE])
        plan = variables.build_plan(v, '200', 'persist')
        self.assertEqual([p['code'] for p in plan['problems']], ['mode_unavailable'])
        self.assertEqual(plan['alternative']['mode'], 'global')
        self.assertIn('MySQL 5.7 has no SET PERSIST', plan['alternative']['text'])
        self.assertEqual(variables.build_plan(v, '200', 'global')['statement'], 'SET GLOBAL max_connections = 200')

    def test_a_static_variable_goes_to_the_option_file(self):
        v, _ = self.read('log-file-size')
        plan = variables.build_plan(v, '100663296', 'persist_only')
        self.assertIsNone(plan['statement'])
        self.assertEqual(plan['alternative']['config_line'], 'innodb_log_file_size = 100663296')


class TestLocks(unittest.TestCase):
    """5.7 has information_schema.INNODB_LOCKS and INNODB_LOCK_WAITS, no data_locks."""

    def report(self):
        conn, data = replay('locks', SERVER)
        return locks.lock_report(conn, **data['args']), conn

    def test_row_locks_come_from_information_schema(self):
        report, conn = self.report()
        self.assertFalse(any('data_lock' in sql for sql in sent(conn)))
        self.assertTrue(any('information_schema.INNODB_LOCK_WAITS' in sql for sql in sent(conn)))
        self.assertEqual(report['capabilities']['row_locks'], 'waits_only')
        self.assertFalse(report['capabilities']['data_locks_global_latch'])
        self.assertEqual(report['summary']['row_lock_waits'], 1)
        wait = report['waits'][0]
        self.assertEqual((wait['requesting']['schema'], wait['requesting']['table']), ('review', 'lock_probe'))
        self.assertEqual(wait['blocking']['status'], 'GRANTED')
        self.assertIn('MySQL 5.7 shows record-only and next-key locks both as X', wait['blocking']['explain'])
        self.assertEqual(report['summary']['root_blockers'], 1)

    def test_notes_name_5_7_defaults(self):
        report, _ = self.report()
        text = ' '.join(report['notes'])
        self.assertIn('MySQL 5.7 lists only the row locks involved in a wait', text)
        self.assertIn('(the MySQL 5.7 default)', text)
        self.assertNotIn('data_locks', text)
        self.assertEqual(report['ddl_waits'][0]['precision'], 'likely')

    def test_kill_privilege_on_5_7_is_super(self):
        grants = ["GRANT SELECT ON *.* TO 'app'@'%'"]
        privilege = locks.parse_kill_privilege('mysql', (5, 7, 44), grants, 'app', "app@%")
        self.assertIsNone(privilege['system_user'])
        note = locks._privilege_notes('mysql', privilege, (5, 7, 44))[0]
        self.assertIn('Ending another user\'s session needs SUPER.', note)
        self.assertNotIn('CONNECTION_ADMIN', note)
        root = locks.parse_kill_privilege('mysql', (5, 7, 44), ["GRANT SUPER ON *.* TO 'dba'@'%'"], 'dba', 'dba@%')
        self.assertEqual((root['via'], root['system_user']), ('SUPER', None))
        self.assertEqual(locks._privilege_notes('mysql', root, (5, 7, 44)), [], 'no SYSTEM_USER warning on 5.7')
        status, message = locks._kill_error('mysql', ConnectorError('ERROR 1095 (HY000): You are not owner of thread'),
                                            7, (5, 7, 44))
        self.assertEqual(status, 'denied')
        self.assertIn('needs SUPER.', message)

    def test_version_boundaries(self):
        self.assertTrue(locks.innodb_lock_tables('mysql', (5, 7, 44)))
        self.assertTrue(locks.innodb_lock_tables('mysql', (8, 0, 0)))
        self.assertFalse(locks.innodb_lock_tables('mysql', (8, 0, 1)))
        self.assertFalse(locks.innodb_lock_tables('mysql', (0, 0, 0)), 'an unknown version is a current MySQL')
        self.assertTrue(locks.innodb_lock_tables('mariadb', (11, 4, 13)))
        self.assertFalse(locks.data_locks_latch_caution('mysql', (5, 7, 44)))


class TestSessionsWatchProgress(unittest.TestCase):
    def test_sessions(self):
        conn, data = replay('sessions', SERVER)
        deny_blocker_read(conn)  # the 5.7 recording predates the blocker read
        report = sessions.sessions_report(conn, **data['args'])
        self.assertEqual(report['source'], 'information_schema.PROCESSLIST')
        self.assertEqual(report['summary']['lock_wait'], {'innodb': 1, 'metadata': 1, 'table': 0, 'total': 2})
        self.assertEqual(report['summary']['idle_in_trx'], 2)

    def test_watch(self):
        conn, _ = replay('watch', SERVER)
        report = watch.watch_report(conn)
        self.assertEqual(report['longest_lock_wait']['seconds'], 4)
        self.assertEqual(report['longest_waiting_statement']['state'], 'Waiting for table metadata lock')
        self.assertFalse(any(report['errors'].values()))

    def test_ddl_progress(self):
        conn, data = replay('ddl-progress', SERVER)
        report = ddl_progress.ddl_progress_report(conn, data['args']['connection_ids'])
        self.assertTrue(report['available'])
        item = report['items'][0]
        self.assertEqual(item['stage'], 'stage/innodb/alter table (merge sort)')
        self.assertLess(item['percent'], 100)


class TestInnoDBErrorLogTop(unittest.TestCase):
    def test_innodb_status_parses(self):
        conn, _ = replay('innodb', SERVER)
        report = innodb_status.innodb_report(conn)
        self.assertTrue(report['parse']['complete'])
        self.assertEqual(report['parse']['unknown_sections'], [])

    def test_error_log_gives_the_file_guidance(self):
        conn, _ = replay('error-log', SERVER)
        report = error_log.error_log_report(conn)
        self.assertEqual(report['reason'], 'unsupported_version')
        self.assertFalse(any('error_log' in sql and 'performance_schema' in sql for sql in sent(conn)))

    def test_top_without_sample_text_or_quantiles(self):
        conn, data = replay('top', SERVER)
        report = discover.top_report(conn, **data['args'])
        self.assertFalse(any('QUERY_SAMPLE_TEXT' in sql for sql in sent(conn)))
        statement = report['statements'][0]
        self.assertEqual((statement['p95_ms'], statement['cpu_ms'], statement['sample_text']), (None, None, None))
        self.assertIsNone(report['cpu'])


class TestInstruments(unittest.TestCase):
    def report(self):
        conn, _ = replay('instruments', SERVER)
        return ps_instruments.instruments_report(conn)

    def test_5_7_defaults(self):
        report = self.report()
        groups = {g['key']: g for g in report['groups']}
        self.assertNotIn('error', groups, '5.7 has no error instrument')
        self.assertEqual(groups['wait/lock/metadata']['default'], 'off')
        self.assertEqual(groups['transaction']['default'], 'off')
        self.assertEqual(groups['statement']['changed'], 0, 'every statement instrument is on by default in 5.7')
        self.assertEqual(groups['stage']['changed'], 0)
        consumers = {c['name']: c['default'] for c in report['consumers']}
        self.assertTrue(consumers['events_statements_history'])
        self.assertFalse(consumers['events_transactions_current'])
        presets = {p['key']: p for p in report['presets']}
        self.assertTrue(presets['defaults']['applied'])
        self.assertNotIn('PROPERTIES', presets['defaults']['runtime'])
        self.assertIn("'stage/innodb/alter table (flush)'", presets['defaults']['runtime'])
        self.assertIn("mdl=ON", presets['minimal']['config'])
        self.assertTrue(any(n.startswith("These are MySQL 5.7's defaults") for n in report['notes']))
        self.assertFalse(any('MySQL 8.4 and 9.x share' in n for n in report['notes']))
        features = {f['key']: f['status'] for f in report['features']}
        self.assertEqual(features['locks_rows'], 'not_needed')
        self.assertEqual(features['locks_mdl'], 'off')

    def test_defaults_profile(self):
        self.assertEqual(ps_instruments.defaults_profile(server57()), 'mysql57')
        self.assertEqual(ps_instruments.default_state('mysql57', 'memory/innodb/buf_buf_pool'), (False, None))
        self.assertEqual(ps_instruments.default_state('mysql', 'memory/innodb/buf_buf_pool'), (True, None))
        self.assertEqual(ps_instruments.default_state('mysql57', 'statement/sp/jump'), (True, True))


class TestReplication(unittest.TestCase):
    def read(self, name):
        conn, _ = replay('replication', name)
        return replication.read_status(conn), conn

    def test_healthy_replica(self):
        report, conn = self.read('mysql-5.7-healthy')
        self.assertIn('/*myflames:replication*/ SHOW SLAVE STATUS', sent(conn))
        self.assertFalse(any('LAST_QUEUED_TRANSACTION' in sql or 'APPLYING_TRANSACTION' in sql for sql in sent(conn)))
        self.assertFalse(any('Could not read' in n for n in report['notes']))
        self.assertFalse(report['capabilities']['worker_timestamps'])
        channel = report['channels'][0]
        self.assertEqual(channel['status'], 'ok')
        self.assertTrue(channel['lag_notes'][0].startswith('Seconds_Behind_Master is 0'))

    def test_failed_applier_plans_the_skip_counter(self):
        report, _ = self.read('mysql-5.7-error')
        plan = report['channels'][0]['skip_plan']
        self.assertEqual(plan['method'], 'skip_counter')
        self.assertEqual(plan['statements'], ['SET GLOBAL sql_slave_skip_counter = 1',
                                              "START SLAVE SQL_THREAD FOR CHANNEL ''"])
        self.assertEqual(plan['privileges'], 'SET GLOBAL sql_slave_skip_counter needs SUPER; START SLAVE needs SUPER.')

    def test_receiver_stage_name(self):
        self.assertEqual(replication.receiver_stage('mysql', '5.7.44-log'), 'stage/sql/Checking master version')
        self.assertEqual(replication.receiver_stage('mysql', '8.0.25'), 'stage/sql/Checking master version')
        self.assertEqual(replication.receiver_stage('mysql', '8.0.26'), 'stage/sql/Checking source version')
        self.assertEqual(replication.receiver_stage('mysql', None), 'stage/sql/Checking source version')


class TestGroupReplication(unittest.TestCase):
    """cluster-gr-5.7-*: a three-member 5.7.44 group. Before, 5.7 was "Not part of a cluster"."""

    def read(self, name):
        from _cluster_fixtures import recorded
        conn, data = recorded(name)
        return cluster.read_status(conn), conn

    def test_healthy_group(self):
        report, conn = self.read('gr-5.7-healthy')
        self.assertEqual((report['kind'], report['status']), ('group_replication', 'ok'))
        members = {m['name']: m for m in report['members']}
        self.assertEqual([members[n]['role'] for n in ('gr57-1', 'gr57-2', 'gr57-3')],
                         ['PRIMARY', 'SECONDARY', 'SECONDARY'])
        self.assertEqual(members['gr57-1']['version'], '5.7.44')
        self.assertIsNone(members['gr57-2']['version'], "only this member's version is known")
        self.assertEqual(report['cluster']['failures_tolerated'], 1)
        members_sql = next(sql for sql in sent(conn) if 'replication_group_members' in sql)
        self.assertIn('MEMBER_STATE, NULL, NULL FROM', members_sql)
        self.assertTrue(any(n.startswith('MySQL 5.7 has no MEMBER_ROLE') for n in report['notes']))

    def test_member_offline_and_local_offline(self):
        report, _ = self.read('gr-5.7-member-offline')
        self.assertEqual((report['status'], len(report['members'])), ('watch', 2))
        report, _ = self.read('gr-5.7-local-offline')
        self.assertEqual(report['status'], 'problem')
        self.assertIsNone(report['members'][0]['role'], 'an OFFLINE member has no role')

    def test_no_reviewed_changes_before_8_0_13(self):
        report, _ = self.read('gr-5.7-healthy')
        self.assertEqual(cluster.with_actions(report)['members'][0].get('actions'), [])

    def test_undefined_primary_names_no_member(self):
        """5.7's group_replication_primary_member reads UNDEFINED when no member is
        primary or this member is in ERROR (member_info.cc:887-890)."""
        def edit(sql, text, n):
            if "group_replication_primary_member" in sql:
                return 'group_replication_primary_member\tUNDEFINED\n'
            return text
        from _cluster_fixtures import recorded
        conn, _ = recorded('gr-5.7-healthy', edit=edit)
        report = cluster.read_status(conn)
        self.assertEqual([m['role'] for m in report['members']], [None, None, None])

    def test_older_than_group_replication(self):
        report = cluster._base(server57(version='5.7.16-log'))
        cluster._read_group_replication(None, server57(version='5.7.16-log'), report)
        self.assertIn('Group Replication needs MySQL 5.7.17 or later', report['message'])


class TestServerChecksAndReport(unittest.TestCase):
    def test_checks(self):
        conn, _ = replay('checks', SERVER)
        with mock.patch.object(server_checks.datetime, 'date', wraps=server_checks.datetime.date) as date:
            date.today.return_value = server_checks.datetime.date(2026, 10, 31)
            report = server_checks.server_checks(conn)
        checks = {c['key']: c for c in report['checks']}
        expiry = checks['binlog_expiry']
        self.assertEqual(expiry['status'], 'watch')
        self.assertEqual(expiry['action']['sql'], 'SET GLOBAL expire_logs_days = <days>;')
        self.assertNotIn('binlog_expire_logs_seconds', json.dumps(expiry))
        self.assertEqual(checks['query_cache']['status'], 'ok', '5.7 has a query cache, off by default')
        self.assertEqual(checks['version_support']['status'], 'problem')
        self.assertIn('2023-10-25', checks['version_support']['why'])
        self.assertNotIn('internal_tmp_mem_storage_engine', checks['memory_budget']['why'])
        for check in report['checks']:
            self.assertNotIn('SET PERSIST', (check.get('action') or {}).get('sql') or '', check['key'])

    def test_query_cache_on_5_7_is_watch_with_the_5_7_caveat(self):
        checks = server_checks._query_cache(server57(query_cache_type='ON', query_cache_size='1048576',
                                                     have_query_cache='YES'), {'status': {}})
        self.assertEqual(checks[0]['status'], 'watch')
        self.assertIn('MySQL 8.0 has none', checks[0]['caveat'])
        self.assertEqual(server_checks._query_cache(Server({'version': '8.4.11'}, {}), {}), [])

    def test_writable_replica_on_5_7(self):
        conn, _ = replay('checks', SERVER + '-replica')
        checks = {c['key']: c for c in server_checks.server_checks(conn)['checks']}
        self.assertIn('/*myflames:checks*/ SHOW SLAVE STATUS', sent(conn))
        writable = checks['replica_read_only']
        self.assertEqual(writable['status'], 'watch')
        self.assertEqual(writable['action']['sql'], 'SET GLOBAL super_read_only = ON;')
        self.assertIn('lets users with SUPER write', writable['caveat'])
        self.assertNotIn('CONNECTION_ADMIN', writable['caveat'])
        self.assertEqual(checks['replication']['status'], 'problem', 'the applier stopped on error 1062')

    def test_full_analysis_reads_every_view(self):
        import test_report
        result = test_report.run_report(SERVER)[0]
        self.assertFalse([v for v in result['header']['views'] if v['status'] != 'ok'])
        for f in result['findings']:
            self.assertNotIn('SET PERSIST', (f.get('action') or {}).get('sql') or '', f['id'])
            self.assertNotIn('invisible first', json.dumps(f.get('action') or {}), f['id'])
        self.assertNotIn('ERROR 1054', json.dumps(result))
        self.assertNotIn('ERROR 1146', json.dumps(result))


class TestHealth(unittest.TestCase):
    def report(self):
        with open(os.path.join(HERE, 'fixtures', 'health', SERVER + '.json'), encoding='utf-8') as handle:
            data = json.load(handle)
        first, second = data['samples']
        return health.compute_health(first, second, data['variables'], 2)

    def test_temporary_tables_and_redo_on_5_7(self):
        diagnostics = {d['key']: d for d in self.report()['diagnostics']}
        why = diagnostics['tmp_tables']['why']
        self.assertIn('The MEMORY engine keeps each internal temporary table', why)
        self.assertIn('internal_tmp_disk_storage_engine', why)
        self.assertNotIn('TempTable', why)
        redo = diagnostics['redo']['why']
        self.assertIn('MySQL 8.0.30', redo)
        self.assertNotIn('84%', redo)
        cache = {s['name']: s for s in diagnostics['table_cache']['settings'] if s}
        self.assertEqual(cache['table_open_cache']['default'], '2000')
        binlog = {s['name']: s for s in diagnostics['binlog_cache']['settings'] if s}
        self.assertEqual(binlog['log_bin']['default'], 'OFF', '5.7 starts without a binary log')
        pool = {s['name']: s for s in diagnostics['buffer_pool']['settings'] if s}
        self.assertEqual(pool['innodb_buffer_pool_instances']['default'], '8 from 1 GiB of buffer pool, otherwise 1')

    def test_no_set_persist_in_recommendations(self):
        for d in self.report()['diagnostics']:
            self.assertNotIn('SET PERSIST', json.dumps(d.get('recommendation') or {}), d['key'])


class TestWaitsAndIo(unittest.TestCase):
    def test_waits(self):
        from myflames import waits
        clock = FakeTime()
        conn, data = replay('waits', SERVER, clock=clock)
        report = waits.waits_report(conn, data['args']['interval'], data['args']['limit'],
                                    sleep=clock.sleep, clock=clock.monotonic)
        self.assertTrue(report['available'])
        self.assertTrue(report['since_start'])

    def test_io(self):
        from myflames import io_stats
        clock = FakeTime()
        conn, data = replay('io', SERVER, clock=clock)
        with mock.patch('myflames.perf_common.time', clock):
            report = io_stats.io_report(conn, data['args']['interval'], data['args']['schema'],
                                        data['args']['limit'])
        self.assertEqual(report['source'], 'performance_schema')
        self.assertTrue(report['since_start'])


@unittest.skipUnless(os.name == 'posix', 'needs bash and python3')
class TestGaMatrixSeed(unittest.TestCase):
    """scripts/ga-matrix.sh seeds MySQL 5.7, which has no WITH RECURSIVE."""

    def test_rewrite_leaves_no_recursive_cte(self):
        script = os.path.join(REPO, 'scripts', 'ga-matrix.sh')
        text = open(script, encoding='utf-8').read()
        function = re.search(r'^no_cte\(\) \{\n.*?^\}\n', text, re.M | re.S).group(0)
        seed = subprocess.run(['awk', "/^mysql_exec << 'SQL'/{f=1;next} /^SQL$/{f=0} f",
                               os.path.join(REPO, 'scripts', 'generate-fixtures.sh')],
                              stdout=subprocess.PIPE, check=True).stdout.decode()
        out = subprocess.run(['bash', '-c', function + '\nno_cte'], input=seed.encode(), stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, check=True).stdout.decode()
        self.assertNotIn('WITH RECURSIVE', out)
        self.assertNotIn('cte_max_recursion_depth', out)
        self.assertIn('FROM (SELECT n FROM mf_seq.n WHERE n <= 40000) cte;', out)
        self.assertIn('mysql-5.7)          echo "mysql:5.7.44 mysql 33057 primary', text)
        self.assertIn('mysql-5.7-replica)  echo "mysql:5.7.44 mysql 33058 replica', text)


if __name__ == '__main__':
    unittest.main()
