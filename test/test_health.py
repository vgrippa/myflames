"""Server health: ``myflames health``, the Health tab API, the rate math and
the diagnostics.

Fake connections answer ``SHOW GLOBAL VARIABLES`` and successive ``SHOW GLOBAL
STATUS`` reads with canned ``mysql --batch`` text, so no server is needed. The
two-sample fixtures in fixtures/health/ were captured from live MySQL 8.4.11,
9.7.2 and 26.7.0 and MariaDB 11.4.13 and 13.0.2 servers (throwaway Docker
containers) under light load (a query loop plus one row lock wait), trimmed to
the variables the report uses. The scenario-*.json fixtures were captured the
same way for the diagnostics: warming (a fresh start with a 1 GiB buffer pool
and random reads of a larger table), full (a 64 MiB buffer pool with
innodb_lru_scan_depth 128 and random reads), redo (an insert loop), binlog
(400-row INSERT ... SELECT transactions, MySQL), threads (thread_cache_size 0
and one connection after another) and table-cache (table_open_cache 64 and a
loop over 400 tables).
"""
import contextlib
import inspect
import io
import json
import math
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

from myflames import health, ui, workspace_cli
from myflames.connector import ConnectorError

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'health')
VERSIONS = ('mysql-8.4', 'mysql-9.7', 'mysql-26.7', 'mariadb-11.4', 'mariadb-13.0')
PASSWORD = 'hunter2-not-real'
# Literal tag (not the module constant) so a changed or empty tag fails here;
# ``top`` excludes statements that start with '/*myflames:'.
HEALTH_TAG = '/*myflames:health*/ '
WRITE_WORDS = re.compile(r'\b(UPDATE|SET|INSERT|DELETE|REPLACE|KILL|GRANT|FLUSH|ALTER|CREATE|DROP|'
                         r'TRUNCATE)\b', re.I)
METRIC_KEYS = [
    'questions', 'queries', 'com_select', 'com_insert', 'com_update', 'com_delete',
    'threads_connected', 'threads_running', 'aborted_connects', 'aborted_clients', 'connections_used',
    'bp_hit_ratio', 'bp_disk_reads', 'bp_dirty', 'bp_free', 'bp_wait_free',
    'row_lock_waits', 'row_lock_time', 'row_lock_avg', 'row_lock_current',
    'tmp_disk_ratio', 'sort_merge_passes', 'select_full_join', 'select_scan',
    'opened_tables', 'table_cache_used', 'data_reads', 'data_writes', 'redo_generated', 'log_waits',
]
METRIC_FIELDS = {'key', 'group', 'label', 'explain', 'unit', 'value', 'delta', 'rate', 'max', 'status',
                 'why', 'rule', 'detail', 'link', 'learn', 'sources', 'inputs', 'sparkline'}
DIAGNOSTIC_KEYS = ['buffer_pool', 'redo', 'table_cache', 'table_definitions', 'thread_cache', 'binlog_cache',
                   'open_files', 'full_scans', 'tmp_tables', 'sorts']
DIAGNOSTIC_FIELDS = {'key', 'title', 'status', 'state', 'headline', 'value', 'unit', 'verdict', 'why', 'rule',
                     'numbers', 'settings', 'recommendation', 'sources', 'learn', 'link', 'trend'}
SCENARIO_SERVERS = ('mysql-8.4', 'mysql-26.7', 'mariadb-11.4', 'mariadb-13.0')


def fixture(name):
    with open(os.path.join(FIXTURES, name + '.json'), encoding='utf-8') as handle:
        return json.load(handle)


def batch(rows):
    """Render rows like ``mysql --batch --skip-column-names``."""
    def cell(value):
        if value is None:
            return 'NULL'
        return str(value).replace('\\', '\\\\').replace('\n', '\\n').replace('\t', '\\t')
    return ''.join('\t'.join(cell(v) for v in row) + '\n' for row in rows)


class FakeTime(object):
    """Stands in for the time module: sleep() advances monotonic()."""

    def __init__(self, query_s=0.05):
        self.now = 1000.0
        self.query_s = query_s
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class FakeConn(object):
    """Answers the variables query and successive status reads; records SQL."""

    def __init__(self, version, variables, samples, clock=None, error=None):
        self.version = version
        self.variables = variables
        self.samples = list(samples)
        self.clock = clock
        self.error = error
        self.calls = []

    def run(self, sql, extra_flags=None, timeout=None):
        self.calls.append((sql, list(extra_flags or [])))
        if self.error is not None:
            raise self.error
        if self.clock is not None:
            self.clock.now += self.clock.query_s
        if 'SHOW GLOBAL VARIABLES' in sql:
            names = re.findall(r"'([a-z_]+)'", sql)
            return batch([[k, v] for k, v in self.variables.items() if k in names])
        if 'SHOW GLOBAL STATUS' in sql:
            if not self.samples:
                raise AssertionError('more status reads than samples')
            return batch(sorted(self.samples.pop(0).items()))
        raise AssertionError('unexpected SQL: ' + sql)

    def server_version(self):
        return self.version

    def is_mariadb(self):
        return 'mariadb' in self.version.lower()

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeFactory(object):
    def __init__(self, conn):
        self.conn = conn
        self.options = None

    def __call__(self, **options):
        self.options = options
        return self.conn


def conn_for(name, clock=None, **changes):
    """A FakeConn replaying a live fixture; *changes* edit the second sample."""
    data = fixture(name)
    first = dict(data['samples'][0]['status'])
    second = dict(data['samples'][1]['status'])
    for key, value in changes.items():
        if value is None:
            second.pop(key, None)
        else:
            second[key] = str(value)
    return FakeConn(data['variables']['version'], data['variables'], [first, second], clock=clock)


def samples(first, second, elapsed=10.0):
    return ({'status': first, 't': 0.0, 'taken_at': '2026-10-13T12:00:00+00:00'},
            {'status': second, 't': elapsed, 'taken_at': '2026-10-13T12:00:10+00:00'})


BASE = {
    'Uptime': '86400', 'Questions': '1000', 'Queries': '1200', 'Com_select': '500', 'Com_insert': '100',
    'Com_insert_select': '10', 'Com_update': '50', 'Com_update_multi': '5', 'Com_delete': '20',
    'Com_delete_multi': '2', 'Threads_connected': '10', 'Threads_running': '3', 'Aborted_connects': '7',
    'Aborted_clients': '9', 'Max_used_connections': '20', 'Innodb_buffer_pool_reads': '1000',
    'Innodb_buffer_pool_read_ahead': '500', 'Innodb_buffer_pool_read_requests': '1000000',
    'Innodb_buffer_pool_pages_total': '10000', 'Innodb_buffer_pool_pages_dirty': '100',
    'Innodb_buffer_pool_pages_free': '2000', 'Innodb_buffer_pool_wait_free': '0',
    'Innodb_row_lock_waits': '40', 'Innodb_row_lock_time': '4000', 'Innodb_row_lock_current_waits': '0',
    'Created_tmp_tables': '300', 'Created_tmp_disk_tables': '30', 'Sort_merge_passes': '3',
    'Select_full_join': '1', 'Select_scan': '400', 'Opened_tables': '600', 'Open_tables': '500',
    'Innodb_data_reads': '2000', 'Innodb_data_writes': '3000', 'Innodb_os_log_written': '1000000',
    'Innodb_log_waits': '0',
    # diagnostics
    'Innodb_buffer_pool_pages_data': '7000', 'Innodb_buffer_pool_pages_misc': '1000',
    'Innodb_buffer_pool_read_ahead_evicted': '50', 'Innodb_data_read': '50000000',
    'Table_open_cache_hits': '90000', 'Table_open_cache_misses': '600', 'Table_open_cache_overflows': '100',
    'Opened_table_definitions': '300', 'Open_table_definitions': '250', 'Connections': '5000',
    'Threads_created': '40', 'Threads_cached': '8', 'Binlog_cache_use': '1000', 'Binlog_cache_disk_use': '5',
    'Binlog_stmt_cache_use': '10', 'Binlog_stmt_cache_disk_use': '0', 'Open_files': '50',
    'Innodb_num_open_files': '30', 'Handler_read_first': '100', 'Handler_read_key': '50000',
    'Handler_read_last': '0', 'Handler_read_next': '80000', 'Handler_read_prev': '0', 'Handler_read_rnd': '500',
    'Handler_read_rnd_next': '20000', 'Sort_range': '50', 'Sort_scan': '70', 'Sort_rows': '9000',
}
MYSQL_VARS = {'version': '8.4.6', 'version_comment': 'MySQL Community Server - GPL', 'max_connections': '200',
              'table_open_cache': '4000', 'innodb_buffer_pool_size': '134217728', 'innodb_page_size': '16384',
              'innodb_max_dirty_pages_pct': '90.000000', 'innodb_redo_log_capacity': '104857600',
              'temptable_max_mmap': '0', 'innodb_lru_scan_depth': '1024', 'innodb_buffer_pool_instances': '1',
              'table_open_cache_instances': '16', 'table_definition_cache': '2000', 'thread_cache_size': '9',
              'thread_handling': 'one-thread-per-connection', 'log_bin': 'ON', 'binlog_cache_size': '32768',
              'binlog_stmt_cache_size': '32768', 'open_files_limit': '10000', 'sort_buffer_size': '262144',
              'tmp_table_size': '16777216', 'max_heap_table_size': '16777216', 'temptable_max_ram': '1073741824',
              'internal_tmp_mem_storage_engine': 'TempTable'}
MARIADB_VARS = {'version': '11.4.13-MariaDB-ubu2404', 'version_comment': 'mariadb.org binary distribution',
                'max_connections': '151', 'table_open_cache': '2000', 'innodb_buffer_pool_size': '134217728',
                'innodb_page_size': '16384', 'innodb_max_dirty_pages_pct': '90.000000',
                'innodb_log_file_size': '100663296', 'innodb_lru_scan_depth': '1536',
                'table_open_cache_instances': '8', 'table_definition_cache': '400', 'thread_cache_size': '151',
                'thread_handling': 'one-thread-per-connection', 'log_bin': 'OFF', 'binlog_cache_size': '32768',
                'binlog_stmt_cache_size': '32768', 'open_files_limit': '32198', 'sort_buffer_size': '2097152',
                'tmp_memory_table_size': '16777216', 'tmp_table_size': '16777216',
                'max_heap_table_size': '16777216', 'tmp_disk_table_size': '18446744073709551615'}


def after(**increments):
    """BASE with each named counter increased (or a gauge set, with =value)."""
    second = dict(BASE)
    for key, value in increments.items():
        if isinstance(value, str):
            second[key] = value
        else:
            second[key] = str(int(BASE[key]) + value)
    return second


def compute(second, variables=MYSQL_VARS, elapsed=10.0, first=None):
    a, b = samples(first or BASE, second, elapsed)
    return health.compute_health(a, b, variables, 10)


def by_key(report):
    return {m['key']: m for m in report['metrics']}


def diag(report, key=None):
    """The diagnostics by key, or one of them."""
    found = {d['key']: d for d in report['diagnostics']}
    return found[key] if key else found


def numbers(diagnostic):
    return {n['key']: n for n in diagnostic['numbers']}


class HealthCase(unittest.TestCase):
    maxDiff = None

    def assert_read_only(self, conn):
        """Only SHOW statements, each tagged and kept with --comments."""
        self.assertTrue(conn.calls, 'no SQL was sent')
        for sql, flags in conn.calls:
            self.assertTrue(sql.startswith(HEALTH_TAG + 'SHOW GLOBAL '), sql)
            self.assertIn('--comments', flags, sql)
            self.assertNotRegex(sql[len(HEALTH_TAG):].replace('SHOW GLOBAL', ''), WRITE_WORDS)

    def assert_shape(self, report):
        json.dumps(report, allow_nan=False)
        for key in ('schema_version', 'engine', 'server_version', 'interval', 'elapsed_s', 'started_at',
                    'taken_at', 'uptime_s', 'restarted', 'server', 'summary', 'groups', 'diagnostics', 'metrics',
                    'notes'):
            self.assertIn(key, report)
        self.assertEqual(report['schema_version'], 'health-1.0')
        self.assertEqual([m['key'] for m in report['metrics']], METRIC_KEYS)
        groups = [g['key'] for g in report['groups']]
        for metric in report['metrics']:
            self.assertEqual(set(metric), METRIC_FIELDS, metric['key'])
            self.assertIn(metric['status'], health.STATUSES)
            self.assertIn(metric['group'], groups)
            self.assertTrue(metric['label'] and metric['explain'] and metric['why'], metric['key'])
            if metric['status'] in ('ok', 'watch', 'problem'):
                self.assertIn('Rule of thumb: ', metric['why'], metric['key'])
                self.assertTrue(metric['rule'])
            for field in ('value', 'delta', 'rate', 'max'):
                value = metric[field]
                self.assertTrue(value is None or (isinstance(value, (int, float)) and math.isfinite(value)))
            if metric['link']:
                self.assertIn(metric['link']['view'], ('top', 'running', 'locks'))
            if metric['learn']:
                from myflames.teach import LESSONS
                self.assertIn(metric['learn'], LESSONS)
        counts = {s: sum(1 for m in report['metrics'] if m['status'] == s) for s in health.STATUSES}
        self.assertEqual({k: report['summary'][k] for k in health.STATUSES}, counts)
        self.assert_diagnostics(report)

    def assert_diagnostics(self, report):
        """Every diagnostic: title, numbers with units, the rule, a verdict, the why, settings."""
        from myflames.teach import LESSONS
        self.assertEqual([d['key'] for d in report['diagnostics']], DIAGNOSTIC_KEYS)
        counts = {s: sum(1 for d in report['diagnostics'] if d['status'] == s) for s in health.STATUSES}
        self.assertEqual(report['summary']['diagnostics'], counts)
        worst = max([health.SEVERITY.get(x['status'], 0) for x in report['diagnostics'] + report['metrics']] or [0])
        self.assertEqual(report['summary']['status'], {0: 'ok', 1: 'watch', 2: 'problem'}[worst])
        for d in report['diagnostics']:
            with self.subTest(diagnostic=d['key']):
                self.assertEqual(set(d), DIAGNOSTIC_FIELDS)
                self.assertIn(d['status'], health.STATUSES)
                for field in ('title', 'headline', 'verdict', 'why', 'rule'):
                    self.assertTrue(isinstance(d[field], str) and d[field].strip(), field)
                    self.assertNotIn('%%', d[field])
                    self.assertNotIn('\u2014', d[field], 'no em dashes')
                self.assertTrue(d['value'] is None or (isinstance(d['value'], (int, float))
                                                       and math.isfinite(d['value'])))
                for n in d['numbers']:
                    self.assertLessEqual({'key', 'label', 'value', 'unit', 'text'}, set(n))
                    self.assertTrue(n['label'] and n['text'])
                    self.assertTrue(n['value'] is None or math.isfinite(n['value']))
                for x in d['settings']:
                    self.assertEqual(set(x), {'name', 'value', 'display', 'default'})
                if d['recommendation'] is not None:
                    # Suggestions only for watch and problem, always with a caveat.
                    self.assertIn(d['status'], ('watch', 'problem'))
                    self.assertTrue(d['recommendation']['text'] and d['recommendation']['caveat'])
                if d['status'] == 'info':
                    self.assertTrue(d['rule'].startswith('No threshold') or d['key'] == 'buffer_pool', d['rule'])
                if d['learn']:
                    self.assertIn(d['learn'], LESSONS)
                if d['link']:
                    self.assertIn(d['link']['view'], ('top', 'running', 'locks'))
                if d['trend']:
                    self.assertEqual(set(d['trend']), {'key', 'label', 'unit', 'value'})
                self.assertTrue(d['sources'])


# ---- 1. live fixtures, every supported version ----------------------------------------------------

class TestLiveFixtures(HealthCase):
    def report(self, name):
        clock = FakeTime()
        conn = conn_for(name, clock=clock)
        result = health.health_report(conn, 10, sleep=clock.sleep, clock=clock.monotonic)
        self.assert_read_only(conn)
        self.assert_shape(result)
        return result, fixture(name)

    def test_every_version_reports_every_metric(self):
        for name in VERSIONS:
            with self.subTest(server=name):
                result, data = self.report(name)
                self.assertEqual(result['engine'], 'mariadb' if name.startswith('mariadb') else 'mysql')
                self.assertEqual(result['server_version'], data['variables']['version'])
                self.assertFalse(result['restarted'])
                # Only MySQL reports the LSN (MariaDB's Innodb_os_log_written is already the LSN
                # delta); the TempTable hit counters are MySQL 9.1+. Both are optional reads.
                expected = [] if name in ('mysql-9.7', 'mysql-26.7') else [
                    'Count_hit_tmp_table_size', 'TempTable_count_hit_max_ram']
                if name.startswith('mariadb'):
                    expected = sorted(expected + ['Innodb_redo_log_current_lsn'])
                self.assertEqual(data['missing_names'], expected)
                self.assertEqual([m['key'] for m in result['metrics'] if m['status'] == 'unknown'], [])
                self.assertFalse(any('does not report' in n for n in result['notes']))

    def test_rates_follow_the_fixture_deltas(self):
        for name in VERSIONS:
            with self.subTest(server=name):
                result, data = self.report(name)
                a, b = data['samples'][0]['status'], data['samples'][1]['status']
                elapsed = result['elapsed_s']
                self.assertAlmostEqual(elapsed, 10.05, places=2)
                m = by_key(result)
                questions = int(b['Questions']) - int(a['Questions'])
                self.assertEqual(m['questions']['delta'], questions)
                self.assertAlmostEqual(m['questions']['rate'], questions / elapsed, places=2)
                inserts = sum(int(b[k]) - int(a[k]) for k in ('Com_insert', 'Com_insert_select'))
                self.assertEqual(m['com_insert']['delta'], inserts)
                # The scripted lock wait: one wait of about 5.5 s, inside the window.
                self.assertEqual(m['row_lock_waits']['delta'], 1)
                self.assertGreater(m['row_lock_time']['delta'], 5000)
                self.assertEqual(m['row_lock_avg']['status'], 'problem')
                self.assertEqual(m['row_lock_current']['value'], int(b['Innodb_row_lock_current_waits']))
                # The loop's join has no index on either side.
                self.assertEqual(m['select_full_join']['status'], 'problem')
                self.assertEqual(m['bp_dirty']['value'], round(100.0 * int(b['Innodb_buffer_pool_pages_dirty'])
                                                               / int(b['Innodb_buffer_pool_pages_total']), 2))

    def test_read_ahead_is_added_on_mysql_only(self):
        for name in VERSIONS:
            with self.subTest(server=name):
                result, data = self.report(name)
                a, b = data['samples'][0]['status'], data['samples'][1]['status']
                reads = int(b['Innodb_buffer_pool_reads']) - int(a['Innodb_buffer_pool_reads'])
                ahead = int(b['Innodb_buffer_pool_read_ahead']) - int(a['Innodb_buffer_pool_read_ahead'])
                disk = by_key(result)['bp_disk_reads']
                if name.startswith('mysql'):
                    self.assertEqual(disk['delta'], reads + ahead)
                    self.assertIn('Innodb_buffer_pool_read_ahead', disk['sources'])
                else:
                    self.assertEqual(disk['delta'], reads)
                    self.assertEqual(disk['sources'], ['Innodb_buffer_pool_reads'])

    def test_every_setting_shows_its_engine_default(self):
        for name in VERSIONS:
            with self.subTest(server=name):
                result, _ = self.report(name)
                for d in result['diagnostics']:
                    for setting in d['settings']:
                        self.assertIsNotNone(setting['default'], (d['key'], setting['name']))
        mysql, _ = self.report('mysql-8.4')
        tmp = {x['name']: x for x in diag(mysql, 'tmp_tables')['settings']}
        self.assertEqual(tmp['temptable_max_mmap']['default'], '0 B')
        self.assertIn('1 GiB from 9.6', tmp['temptable_max_ram']['default'])

    def test_temporary_tables_differ_between_engines(self):
        # MySQL's TempTable keeps the TEXT GROUP BY in memory; MariaDB writes it to disk.
        mysql, _ = self.report('mysql-8.4')
        mariadb, _ = self.report('mariadb-13.0')
        self.assertEqual(by_key(mysql)['tmp_disk_ratio']['value'], 0)
        self.assertGreater(by_key(mariadb)['tmp_disk_ratio']['value'], 40)
        self.assertIn('counts twice', by_key(mariadb)['tmp_disk_ratio']['explain'])
        self.assertIn('temptable_max_mmap', by_key(mysql)['tmp_disk_ratio']['explain'])

    def test_text_output_for_every_version(self):
        for name in VERSIONS:
            with self.subTest(server=name):
                result, _ = self.report(name)
                text = health.format_text(result)
                head = 'MariaDB ' if name.startswith('mariadb') else 'MySQL '
                self.assertTrue(text.startswith(head + result['server_version']))
                for _, label in health.GROUPS:
                    self.assertIn('\n' + label.upper() + '\n', text)
                self.assertRegex(text, r'\n  PROBLEM  Joins without an index +[\d.]+/s')
                self.assertIn('\nWHY\n', text)


# ---- 2. rate math -----------------------------------------------------------------------------------

class TestRateMath(HealthCase):
    def test_deltas_and_rates_over_the_measured_window(self):
        report = compute(after(Questions=1234, Com_update=30, Com_update_multi=10), elapsed=8.0)
        m = by_key(report)
        self.assertEqual((m['questions']['delta'], m['questions']['rate']), (1234, 154.25))
        self.assertEqual(m['questions']['value'], m['questions']['rate'])
        self.assertEqual(m['com_update']['delta'], 40)
        self.assertEqual(m['com_update']['inputs'], {'Com_update': 30, 'Com_update_multi': 10})
        self.assertEqual(report['elapsed_s'], 8.0)
        self.assertEqual(report['interval'], 10)

    def test_hit_ratio_adds_read_ahead_on_mysql_but_not_mariadb(self):
        second = after(Innodb_buffer_pool_read_requests=100000, Innodb_buffer_pool_reads=600,
                       Innodb_buffer_pool_read_ahead=400)
        mysql = by_key(compute(second))
        mariadb = by_key(compute(second, MARIADB_VARS))
        self.assertEqual(mysql['bp_hit_ratio']['value'], 99.0)
        self.assertEqual(mysql['bp_disk_reads']['delta'], 1000)
        self.assertEqual(mysql['bp_hit_ratio']['inputs']['Innodb_buffer_pool_read_ahead'], 400)
        self.assertEqual(mariadb['bp_hit_ratio']['value'], 99.4)
        self.assertEqual(mariadb['bp_disk_reads']['delta'], 600)
        self.assertIn('already includes read-ahead', mariadb['bp_hit_ratio']['explain'])

    def test_hit_ratio_is_clamped_and_needs_enough_requests(self):
        m = by_key(compute(after(Innodb_buffer_pool_read_requests=20000, Innodb_buffer_pool_read_ahead=50000)))
        self.assertEqual(m['bp_hit_ratio']['value'], 0)
        self.assertEqual(m['bp_hit_ratio']['status'], 'problem')
        few = by_key(compute(after(Innodb_buffer_pool_read_requests=9999, Innodb_buffer_pool_reads=9999)))
        self.assertEqual(few['bp_hit_ratio']['status'], 'info')
        self.assertIn('too few to judge', few['bp_hit_ratio']['why'])
        # A low ratio with few disk reads per second is at most watch.
        slow = by_key(compute(after(Innodb_buffer_pool_read_requests=10000, Innodb_buffer_pool_reads=600),
                              elapsed=100.0))
        self.assertEqual(slow['bp_hit_ratio']['value'], 94.0)
        self.assertEqual(slow['bp_hit_ratio']['status'], 'watch')
        self.assertIn('only 6 disk reads per second', slow['bp_hit_ratio']['why'])

    def test_row_lock_time_average_and_current(self):
        m = by_key(compute(after(Innodb_row_lock_waits=4, Innodb_row_lock_time=2000,
                                 Innodb_row_lock_current_waits='2')))
        self.assertEqual((m['row_lock_time']['delta'], m['row_lock_time']['rate']), (2000, 200))
        self.assertEqual(m['row_lock_avg']['value'], 500)
        self.assertEqual(m['row_lock_current']['value'], 2)
        # A wait seen in only one sample is a blip; waits in both are watched.
        self.assertEqual(m['row_lock_current']['status'], 'ok')
        both = by_key(compute(after(Innodb_row_lock_current_waits='2'),
                              first=dict(BASE, Innodb_row_lock_current_waits='3')))
        self.assertEqual(both['row_lock_current']['status'], 'watch')
        many = by_key(compute(after(Innodb_row_lock_current_waits='12'),
                              first=dict(BASE, Innodb_row_lock_current_waits='10')))
        self.assertEqual(many['row_lock_current']['status'], 'problem')
        for key in ('row_lock_waits', 'row_lock_time', 'row_lock_avg', 'row_lock_current'):
            self.assertEqual(m[key]['link']['view'], 'locks')

    def test_no_wait_started_means_no_average(self):
        m = by_key(compute(after(Innodb_row_lock_time=300)))
        self.assertIsNone(m['row_lock_avg']['value'])
        self.assertEqual(m['row_lock_avg']['status'], 'info')
        self.assertIn('300 ms of earlier waits ended in it', m['row_lock_avg']['why'])
        idle = by_key(compute(after()))
        self.assertEqual(idle['row_lock_avg']['status'], 'ok')

    def test_connections_and_threads(self):
        m = by_key(compute(after(Threads_connected='150', Threads_running='40', Max_used_connections='200'),
                           first=dict(BASE, Threads_running='12')))
        self.assertEqual(m['connections_used']['value'], 75.0)
        self.assertEqual(m['connections_used']['status'], 'watch')
        self.assertIn('reached max_connections; refusals are counted in Connection_errors_max_connections',
                      m['connections_used']['why'])
        self.assertEqual((m['threads_running']['value'], m['threads_running']['max']), (40, 40))
        self.assertEqual(m['threads_running']['status'], 'watch')
        peak_first = by_key(compute(after(Threads_running='2'), first=dict(BASE, Threads_running='120')))
        self.assertEqual((peak_first['threads_running']['value'], peak_first['threads_running']['max']), (2, 120))
        self.assertEqual(peak_first['threads_running']['status'], 'problem')

    def test_refused_connections_are_a_problem(self):
        first = dict(BASE, Connection_errors_max_connections='4')
        quiet = by_key(compute(dict(after(), Connection_errors_max_connections='4'), first=first))
        self.assertEqual(quiet['connections_used']['status'], 'ok')
        self.assertEqual(quiet['connections_used']['inputs']['Connection_errors_max_connections'], 0)
        refused = by_key(compute(dict(after(), Connection_errors_max_connections='7'), first=first))
        self.assertEqual(refused['connections_used']['status'], 'problem')
        self.assertIn('3 clients were refused at max_connections', refused['connections_used']['why'])
        self.assertIn('Connection_errors_max_connections', refused['connections_used']['sources'])
        # Servers without the counter are not noted as missing it.
        self.assertFalse(any('Connection_errors' in n for n in compute(after())['notes']))

    def test_mariadb_replica_workers_raise_the_threads_limits(self):
        replica = dict(MARIADB_VARS, slave_parallel_threads='32')
        m = by_key(compute(after(Threads_running='40'), replica))
        self.assertEqual(m['threads_running']['status'], 'ok')
        self.assertIn('slave_parallel_threads (32)', m['threads_running']['why'])
        self.assertEqual(by_key(compute(after(Threads_running='70'), replica))['threads_running']['status'], 'watch')
        self.assertEqual(by_key(compute(after(Threads_running='40'), MARIADB_VARS))['threads_running']['status'],
                         'watch')
        self.assertIn('parallel replication worker', m['threads_running']['explain'])

    def test_temporary_tables_ratio_and_small_counts(self):
        m = by_key(compute(after(Created_tmp_tables=100, Created_tmp_disk_tables=30)))
        self.assertEqual(m['tmp_disk_ratio']['value'], 30.0)
        self.assertEqual(m['tmp_disk_ratio']['status'], 'problem')
        small = by_key(compute(after(Created_tmp_tables=4, Created_tmp_disk_tables=4)))
        self.assertEqual(small['tmp_disk_ratio']['status'], 'watch')
        none = by_key(compute(after()))
        self.assertIsNone(none['tmp_disk_ratio']['value'])
        self.assertEqual(none['tmp_disk_ratio']['status'], 'ok')

    def test_dirty_pages_are_watched_at_innodb_max_dirty_pages_pct_never_a_problem(self):
        dirty = lambda pages, variables=MYSQL_VARS: by_key(compute(  # noqa: E731
            after(Innodb_buffer_pool_pages_dirty=str(pages)), variables))['bp_dirty']
        self.assertEqual(dirty(8900)['status'], 'ok')
        self.assertEqual(dirty(9000)['status'], 'watch')
        self.assertEqual(dirty(9900)['status'], 'watch')
        low_limit = dict(MYSQL_VARS, innodb_max_dirty_pages_pct='60.000000')
        self.assertEqual(dirty(5000, low_limit)['status'], 'ok')
        self.assertEqual(dirty(6000, low_limit)['status'], 'watch')
        no_var = {k: v for k, v in MYSQL_VARS.items() if k != 'innodb_max_dirty_pages_pct'}
        self.assertEqual(dirty(7300, no_var)['status'], 'ok')
        self.assertEqual(dirty(9000, no_var)['status'], 'watch')
        self.assertIn('can stay idle below the maximum', dirty(100, MARIADB_VARS)['explain'])
        self.assertIn('innodb_max_dirty_pages_pct_lwm (10 by default)', dirty(100)['explain'])

    def test_redo_capacity_on_both_engines(self):
        second = after(Innodb_os_log_written=1048576 * 10)
        mysql = by_key(compute(second))['redo_generated']
        self.assertEqual(mysql['rate'], 1048576)
        self.assertEqual(mysql['sources'], ['Innodb_os_log_written', 'innodb_redo_log_capacity'])
        # No checkpoint age in these samples: the capacity at this rate.
        self.assertIn('100.0 MiB of redo capacity covers about 1 min 40 s at this rate', mysql['detail'])
        # With the checkpoint age, the time runs from it to about 73% of the capacity, not from empty.
        aged = by_key(compute(dict(second, Innodb_redo_log_current_lsn='70000000',
                                   Innodb_redo_log_checkpoint_lsn='60000000'),
                              first=dict(BASE, Innodb_redo_log_current_lsn='60000000',
                                         Innodb_redo_log_checkpoint_lsn='55000000')))['redo_generated']
        # 10 MB over 10 s; 0.73 x 100 MiB = 76,546,048 B; age 10,000,000 B -> 66,546,048 B / 1 MB/s.
        self.assertEqual(aged['rate'], 1000000)
        self.assertIn('adaptive flushing reaches its limit in about 1 min 6 s at this rate', aged['detail'])
        self.assertEqual(aged['inputs']['checkpoint_age_bytes'], 10000000)
        self.assertIn('Innodb_redo_log_checkpoint_lsn', aged['sources'])
        # The LSN delta wins on MySQL: Innodb_os_log_written re-counts partial blocks.
        lsn = by_key(compute(dict(second, Innodb_redo_log_current_lsn='5242880'),
                             first=dict(BASE, Innodb_redo_log_current_lsn='0')))['redo_generated']
        self.assertEqual((lsn['delta'], lsn['sources'][0]), (5242880, 'Innodb_redo_log_current_lsn'))
        mariadb = compute(second, MARIADB_VARS)
        self.assertEqual(mariadb['server']['redo_capacity_bytes'], 100663296)
        self.assertIn('since startup on MariaDB', by_key(mariadb)['redo_generated']['explain'])
        old = dict(MYSQL_VARS, innodb_log_file_size='50331648', innodb_log_files_in_group='2')
        del old['innodb_redo_log_capacity']
        self.assertEqual(compute(second, old)['server']['redo_capacity_bytes'], 100663296)

    def test_large_counters_stay_exact(self):
        big = '18446744073709551000'
        m = by_key(compute(dict(after(), Innodb_os_log_written=str(int(big) + 7)),
                           first=dict(BASE, Innodb_os_log_written=big)))
        self.assertEqual(m['redo_generated']['delta'], 7)


# ---- 3. restarts, resets, missing variables -------------------------------------------------------

class TestResetsAndMissing(HealthCase):
    def test_restart_between_samples(self):
        report = compute(dict(after(), Uptime='3', Questions='12'))
        self.assertTrue(report['restarted'])
        self.assertIn('restarted between the two samples', report['notes'][0])
        m = by_key(report)
        for key in ('questions', 'bp_hit_ratio', 'row_lock_waits', 'tmp_disk_ratio', 'redo_generated'):
            self.assertEqual(m[key]['status'], 'unknown', key)
            self.assertIsNone(m[key]['delta'])
        self.assertIn('restarted between the two samples', m['questions']['why'])
        # Gauges still read the second sample.
        self.assertEqual(m['threads_connected']['value'], 10)
        self.assertEqual(m['bp_dirty']['status'], 'ok')
        self.assertFalse(any('warming up' in n for n in report['notes']))
        self.assert_shape(report)

    def test_counter_reset_without_restart(self):
        report = compute(after(Aborted_clients=-9, Questions=50))
        m = by_key(report)
        self.assertFalse(report['restarted'])
        self.assertEqual(m['aborted_clients']['status'], 'unknown')
        self.assertIn('went down between the samples', m['aborted_clients']['why'])
        self.assertEqual(m['questions']['delta'], 50)
        self.assertTrue(any('went down between the samples: Aborted_clients' in n for n in report['notes']))

    def test_missing_status_variables(self):
        second = after(Questions=10)
        for name in ('Innodb_buffer_pool_read_ahead', 'Innodb_row_lock_time', 'Threads_running'):
            second.pop(name)
        report = compute(second)
        m = by_key(report)
        self.assertEqual(m['bp_hit_ratio']['status'], 'unknown')
        self.assertIn('does not report Innodb_buffer_pool_read_ahead', m['bp_hit_ratio']['why'])
        self.assertEqual(m['row_lock_time']['status'], 'unknown')
        self.assertEqual(m['row_lock_avg']['status'], 'unknown')
        self.assertEqual(m['threads_running']['status'], 'unknown')
        self.assertEqual(m['questions']['delta'], 10)
        note = [n for n in report['notes'] if n.startswith('This server does not report')][0]
        for name in ('Innodb_buffer_pool_read_ahead', 'Innodb_row_lock_time', 'Threads_running'):
            self.assertIn(name, note)
        self.assert_shape(report)

    def test_missing_system_variables(self):
        report = compute(after(), {'version': '8.4.6'})
        m = by_key(report)
        self.assertEqual(m['connections_used']['status'], 'unknown')
        self.assertIn('does not report max_connections', m['connections_used']['why'])
        self.assertEqual(m['table_cache_used']['status'], 'unknown')
        self.assertIsNone(report['server']['redo_capacity_bytes'])
        self.assertEqual(m['bp_dirty']['status'], 'ok')

    def test_empty_samples_are_all_unknown(self):
        report = compute({}, {}, first={})
        self.assertEqual(report['engine'], 'mysql')
        self.assertEqual(report['summary']['status'], 'ok')
        self.assertEqual(report['summary']['ok'], 0)
        self.assert_shape(report)

    def test_warmup_and_mmap_notes(self):
        young = compute(dict(after(), Uptime='600'), first=dict(BASE, Uptime='590'))
        self.assertTrue(any('started 10 min 0 s ago' in n for n in young['notes']))
        mmap = compute(after(), dict(MYSQL_VARS, temptable_max_mmap='1073741824', temptable_use_mmap='ON'))
        self.assertTrue(any('temptable_max_mmap is 1.0 GiB' in n for n in mmap['notes']))
        off = compute(after(), dict(MYSQL_VARS, temptable_max_mmap='1073741824', temptable_use_mmap='OFF'))
        self.assertFalse(any('temptable_max_mmap' in n for n in off['notes']))


# ---- 4. thresholds ----------------------------------------------------------------------------------

class TestThresholds(HealthCase):
    def test_grade_boundaries(self):
        self.assertEqual(health._grade(0, (0, 1.0)), 'ok')
        self.assertEqual(health._grade(0.01, (0, 1.0)), 'watch')
        self.assertEqual(health._grade(1.0, (0, 1.0)), 'problem')
        self.assertEqual(health._grade(0.99, (1.0, 10.0)), 'ok')
        self.assertEqual(health._grade(1.0, (1.0, 10.0)), 'watch')
        self.assertEqual(health._grade(10.0, (1.0, 10.0)), 'problem')
        self.assertEqual(health._grade(99.0, (99.0, 95.0), lower_is_worse=True), 'ok')
        self.assertEqual(health._grade(98.9, (99.0, 95.0), lower_is_worse=True), 'watch')
        self.assertEqual(health._grade(94.9, (99.0, 95.0), lower_is_worse=True), 'problem')
        self.assertEqual(health._grade(None, (1, 2)), 'unknown')

    def test_joins_without_an_index(self):
        join = lambda n: by_key(compute(after(Select_full_join=n)))['select_full_join']['status']  # noqa: E731
        self.assertEqual([join(0), join(1), join(99), join(100)], ['ok', 'watch', 'watch', 'problem'])

    def test_should_be_zero_counters(self):
        for key, name in (('bp_wait_free', 'Innodb_buffer_pool_wait_free'), ('log_waits', 'Innodb_log_waits')):
            with self.subTest(metric=key):
                self.assertEqual(by_key(compute(after()))[key]['status'], 'ok')
                self.assertEqual(by_key(compute(after(**{name: 1})))[key]['status'], 'watch')
                self.assertEqual(by_key(compute(after(**{name: 10})))[key]['status'], 'problem')

    def test_in_moderation_counters_need_a_minimum_count(self):
        for key, name in (('aborted_clients', 'Aborted_clients'), ('aborted_connects', 'Aborted_connects'),
                          ('sort_merge_passes', 'Sort_merge_passes'),
                          ('opened_tables', 'Opened_tables'), ('row_lock_waits', 'Innodb_row_lock_waits')):
            with self.subTest(metric=key):
                # This tool's own opened table at --interval 1 must not trip the rule.
                self.assertEqual(by_key(compute(after(**{name: 1}), elapsed=1.0))[key]['status'], 'ok')
                self.assertEqual(by_key(compute(after(**{name: 4}), elapsed=2.0))[key]['status'], 'ok')
                self.assertEqual(by_key(compute(after(**{name: 10})))[key]['status'], 'watch')
                self.assertEqual(by_key(compute(after(**{name: 100})))[key]['status'], 'problem')

    def test_self_overhead_never_trips_a_rule(self):
        # Measured live per window: 3 statements, 1 temporary table, 2 scans, 1 opened table.
        own = after(Questions=3, Queries=3, Com_select=1, Select_scan=2, Created_tmp_tables=1, Opened_tables=1)
        report = compute(own, elapsed=1.0)
        self.assertEqual(report['summary']['status'], 'ok')

    def test_info_metrics_have_no_rule(self):
        m = by_key(compute(after(Questions=99999, Select_scan=99999, Innodb_data_reads=99999)))
        for key in ('questions', 'queries', 'com_select', 'select_scan', 'data_reads', 'data_writes',
                    'redo_generated', 'bp_free', 'table_cache_used', 'threads_connected'):
            self.assertEqual(m[key]['status'], 'info', key)
            self.assertIsNone(m[key]['rule'])
            self.assertIn('No threshold', m[key]['why'])

    def test_summary_and_at_or_worse(self):
        report = compute(after(Select_full_join=500, Innodb_log_waits=1))
        self.assertEqual(report['summary']['status'], 'problem')
        self.assertEqual([m['key'] for m in health.at_or_worse(report, 'problem')], ['select_full_join'])
        # Diagnostics come first: the redo diagnostic watches log waits too.
        self.assertEqual([m['key'] for m in health.at_or_worse(report, 'watch')],
                         ['redo', 'select_full_join', 'log_waits'])
        clean = compute(after())
        self.assertEqual(clean['summary']['status'], 'ok')
        self.assertEqual(health.at_or_worse(clean, 'watch'), [])

    def test_rules_are_documented_for_every_graded_metric(self):
        report = compute(after(Select_full_join=500))
        for m in report['metrics']:
            if m['status'] in ('ok', 'watch', 'problem'):
                self.assertIn(m['rule'], health.RULES.values())

    def test_full_join_text_hint_is_once_and_only_when_flagged(self):
        hint = 'Statements: myflames top --order full_join'
        for variables in (MYSQL_VARS, MARIADB_VARS):
            for status in ('ok', 'info', 'unknown', 'watch', 'problem'):
                with self.subTest(engine=variables['version'], status=status):
                    report = compute(after(), variables)
                    by_key(report)['select_full_join']['status'] = status
                    self.assertEqual(health.format_text(report).count(hint),
                                     int(status in ('watch', 'problem')))
                    diagnostic = dict(report['diagnostics'][0], key='select_full_join', status=status)
                    report['diagnostics'].append(diagnostic)
                    self.assertEqual(health.format_text(report).count(hint),
                                     int(status in ('watch', 'problem')))
                    by_key(report)['select_full_join']['status'] = 'ok'
                    self.assertEqual(health.format_text(report).count(hint),
                                     int(status in ('watch', 'problem')))


# ---- 5. diagnostics ---------------------------------------------------------------------------------

class TestDiagnostics(HealthCase):
    """One explained answer per diagnostic, from fake samples on both engines."""

    def run_diag(self, key, second=None, variables=MYSQL_VARS, first=None, elapsed=10.0):
        report = compute(second if second is not None else after(), variables, elapsed, first)
        self.assert_shape(report)
        return diag(report, key), report

    # buffer pool -------------------------------------------------------------
    def test_buffer_pool_exactly_full_is_ok_with_no_disk_reads(self):
        # The free list target is innodb_lru_scan_depth x instances: 100 x 2 = 200 pages.
        variables = dict(MYSQL_VARS, innodb_lru_scan_depth='100', innodb_buffer_pool_instances='2')
        first = dict(BASE, Innodb_buffer_pool_pages_free='200')
        d, _ = self.run_diag('buffer_pool', after(Innodb_buffer_pool_pages_free='200'), variables, first=first)
        self.assertEqual((d['state'], d['status']), ('full', 'ok'))
        self.assertEqual(d['headline'], '0 B/s read from disk')
        self.assertIn("at or below the page cleaner's target of 200", d['verdict'])
        self.assertIn('100 x 2 = 200 pages', d['why'])
        self.assertEqual(numbers(d)['free_pages']['text'], "200 (the page cleaner's target is 200)")
        self.assertEqual(d['trend'], {'key': 'bp_read_bytes', 'label': 'Read from disk', 'unit': 'B/s', 'value': 0})
        self.assertIsNone(d['recommendation'])
        # One page above the target is not full.
        d, _ = self.run_diag('buffer_pool', after(Innodb_buffer_pool_pages_free='201'), variables, first=first)
        self.assertNotEqual(d['state'], 'full')

    def test_buffer_pool_full_with_sustained_disk_reads(self):
        first = dict(BASE, Innodb_buffer_pool_pages_free='300')
        # MySQL: 800 + 200 read-ahead pages in 10 s = 100 pages/s x 16 KiB = 1,638,400 B/s.
        second = after(Innodb_buffer_pool_pages_free='300', Innodb_buffer_pool_reads=800,
                       Innodb_buffer_pool_read_ahead=200, Innodb_buffer_pool_read_requests=5000000,
                       Innodb_data_read=16384000)
        d, _ = self.run_diag('buffer_pool', second, first=first)
        self.assertEqual((d['state'], d['status']), ('full', 'watch'))
        self.assertEqual(d['value'], 1638400)
        self.assertEqual(d['unit'], 'B/s')
        self.assertEqual(d['headline'], '1.6 MiB/s read from disk')
        self.assertIn('read 100 pages per second from disk (1.6 MiB/s)', d['verdict'])
        self.assertIn('innodb_buffer_pool_size 128.0 MiB', d['verdict'])
        self.assertEqual(numbers(d)['read_bytes']['text'], '1.6 MiB/s (15.6 MiB in the window)')
        self.assertEqual(numbers(d)['data_read']['value'], 1638400)
        self.assertIn('Innodb_data_read is 100% of pages x page size', d['why'])
        self.assertIn('Innodb_buffer_pool_read_ahead', d['sources'])
        self.assertIn('Leave memory for connections', d['recommendation']['caveat'])
        # MariaDB's reads already include read-ahead: 800 pages, not 1,000.
        mariadb, _ = self.run_diag('buffer_pool', second, MARIADB_VARS, first=first)
        self.assertEqual(mariadb['value'], 1310720)
        self.assertIn('already includes read-ahead', mariadb['why'])
        self.assertNotIn('innodb_buffer_pool_instances', mariadb['sources'])

    def test_buffer_pool_full_with_a_low_hit_ratio_is_a_problem(self):
        first = dict(BASE, Innodb_buffer_pool_pages_free='0')
        second = after(Innodb_buffer_pool_pages_free='0', Innodb_buffer_pool_reads=2000,
                       Innodb_buffer_pool_read_ahead=0, Innodb_buffer_pool_read_requests=20000)
        d, report = self.run_diag('buffer_pool', second, first=first)
        self.assertEqual(by_key(report)['bp_hit_ratio']['status'], 'problem')
        self.assertEqual(d['status'], 'problem')
        self.assertIn('10.0% of page requests missed the buffer pool', d['verdict'])

    def test_buffer_pool_read_ahead_waste(self):
        first = dict(BASE, Innodb_buffer_pool_pages_free='0')
        second = after(Innodb_buffer_pool_pages_free='0', Innodb_buffer_pool_reads=500,
                       Innodb_buffer_pool_read_ahead=1000, Innodb_buffer_pool_read_ahead_evicted=400,
                       Innodb_buffer_pool_read_requests=5000000)
        d, _ = self.run_diag('buffer_pool', second, first=first)
        self.assertEqual(numbers(d)['read_ahead_waste']['text'], '400 of 1,000 (40.0%)')
        self.assertIn('40% of the pages read ahead were evicted before any use', d['verdict'])

    def test_buffer_pool_warming_with_a_time_to_full_estimate(self):
        young = dict(BASE, Uptime='600')
        # 4,000 of 10,000 pages in use; data pages +640 (10 MiB) in 10 s.
        second = dict(after(Innodb_buffer_pool_pages_data=640, Innodb_buffer_pool_reads=640), Uptime='610',
                      Innodb_buffer_pool_pages_free='6000')
        d, _ = self.run_diag('buffer_pool', second, first=dict(young, Innodb_buffer_pool_pages_free='6640'))
        self.assertEqual((d['state'], d['status']), ('warming', 'info'))
        # (6,000 free - 1,024 target) / 64 pages per second = 77.75 s.
        self.assertEqual(d['headline'], 'Warming: 40% full, about 1 min 17 s to full at this rate')
        self.assertEqual((d['value'], d['unit']), (40.0, '%'))
        self.assertIn('Data pages grew by 640 in the window (64 pages per second, 1.0 MiB/s)', d['verdict'])
        self.assertIn('That is an estimate', d['verdict'])
        self.assertIn('Reads from disk are expected while it warms', d['verdict'])
        eta = numbers(d)['time_to_full']
        self.assertTrue(eta['estimate'])
        self.assertEqual(eta['value'], 77.75)
        self.assertEqual(d['learn'], 'buffer_pool_warmup')
        self.assertIsNone(d['recommendation'])

    def test_buffer_pool_warming_without_enough_growth_has_no_estimate(self):
        second = dict(after(Innodb_buffer_pool_pages_data=10), Uptime='610', Innodb_buffer_pool_pages_free='6000')
        d, _ = self.run_diag('buffer_pool', second, first=dict(BASE, Uptime='600',
                                                               Innodb_buffer_pool_pages_free='6010'))
        self.assertEqual(d['headline'], 'Warming: 40% full')
        self.assertNotIn('time_to_full', numbers(d))
        loading = dict(second, Innodb_buffer_pool_load_status='Loaded 120/900 pages')
        d, _ = self.run_diag('buffer_pool', loading, first=dict(BASE, Uptime='600',
                                                                Innodb_buffer_pool_pages_free='6010'))
        self.assertIn('A buffer pool load from the dump file is in progress (Loaded 120/900 pages)', d['verdict'])

    def test_buffer_pool_not_full_after_a_long_uptime_has_room(self):
        second = after(Innodb_buffer_pool_pages_free='6000')
        d, _ = self.run_diag('buffer_pool', second, first=dict(BASE, Innodb_buffer_pool_pages_free='6000'))
        self.assertEqual((d['state'], d['status'], d['headline']), ('room', 'info', 'Not full: 40% in use'))
        self.assertIn('after 1 d 0 h up and did not grow in this window', d['verdict'])

    def test_buffer_pool_restart_and_missing_counters(self):
        restarted = dict(after(), Uptime='3', Innodb_buffer_pool_pages_free='100')
        d, _ = self.run_diag('buffer_pool', restarted)
        self.assertEqual((d['state'], d['status']), ('full', 'unknown'))
        self.assertIn('restarted between the two samples', d['verdict'])
        missing = after()
        del missing['Innodb_buffer_pool_pages_total']
        d, report = self.run_diag('buffer_pool', missing)
        self.assertEqual((d['status'], d['headline']), ('unknown', 'Unknown'))
        self.assertIn('does not report Innodb_buffer_pool_pages_total', d['verdict'])
        self.assertTrue(any('Innodb_buffer_pool_pages_total' in n for n in report['notes']))
        # No innodb_lru_scan_depth: the engine default, said so.
        no_depth = {k: v for k, v in MARIADB_VARS.items() if k != 'innodb_lru_scan_depth'}
        d, _ = self.run_diag('buffer_pool', after(), no_depth)
        self.assertIn('1,536 x 1 = 1,536 pages here, the engine default', d['why'])

    def test_buffer_pool_small_pool_is_called_approximate(self):
        small = dict(BASE, Innodb_buffer_pool_pages_total='1500', Innodb_buffer_pool_pages_free='100')
        d, _ = self.run_diag('buffer_pool', dict(after(), Innodb_buffer_pool_pages_total='1500',
                                                 Innodb_buffer_pool_pages_free='100'), first=small)
        self.assertIn('the full or warming call is approximate', d['why'])

    # redo --------------------------------------------------------------------
    def lsn(self, current_a, current_b, checkpoint_a, checkpoint_b, **changes):
        first = dict(BASE, Innodb_redo_log_current_lsn=str(current_a), Innodb_redo_log_checkpoint_lsn=str(checkpoint_a))
        second = dict(after(**changes), Innodb_redo_log_current_lsn=str(current_b),
                      Innodb_redo_log_checkpoint_lsn=str(checkpoint_b))
        return first, second

    def test_redo_coverage_from_the_lsn_and_the_hour_rule(self):
        # 10 MiB of LSN in 10 s = 1 MiB/s; 100 MiB covers 100 s: watch.
        first, second = self.lsn(0, 10485760, 0, 5242880, Innodb_os_log_written=20971520)
        d, _ = self.run_diag('redo', second, first=first)
        self.assertEqual(d['status'], 'watch')
        self.assertEqual(d['headline'], 'Covers about 1 min 40 s of writes')
        self.assertEqual((d['value'], d['unit']), (100, 's'))
        n = numbers(d)
        self.assertEqual(n['redo_rate']['text'], '1.0 MiB/s (10.0 MiB in the window)')
        self.assertEqual(n['redo_per_hour']['text'], '3.5 GiB')
        # Innodb_os_log_written re-counts partial blocks: shown, not used for the time.
        self.assertIn('100% more than the redo generated', n['os_log_written']['text'])
        self.assertEqual(n['checkpoint_age']['text'], '5.0 MiB (5% of the capacity)')
        # From the current age (5 MiB) to 73% of 100 MiB at 1 MiB/s: 68 s.
        self.assertEqual(n['until_pressure']['value'], 68)
        self.assertTrue(n['until_pressure']['estimate'])
        self.assertIn('rewrites the last, partly filled block', d['why'])
        self.assertIn('512-byte log blocks including their headers and trailers', d['why'])
        self.assertIn('about 73% of the capacity', d['rule'])
        self.assertIn('3.8 GiB (rounded up to 256 MiB): SET GLOBAL innodb_redo_log_capacity = 4026531840; resizes it online', d['recommendation']['text'])
        self.assertIn('or use SET PERSIST', d['recommendation']['text'])
        self.assertEqual(d['trend']['key'], 'redo_bytes')
        self.assertEqual(d['trend']['value'], 1048576)
        # A slow window covers more than an hour: ok, with no suggestion.
        first, second = self.lsn(0, 10000, 0, 0)
        d, _ = self.run_diag('redo', second, first=first)
        self.assertEqual((d['status'], d['recommendation']), ('ok', None))
        self.assertIn('meets the rule of thumb of about an hour', d['verdict'])

    def test_redo_checkpoint_age_thresholds_on_mysql(self):
        cap = 104857600
        # 74% of the capacity in the second sample: flushing harder (watch).
        first, second = self.lsn(0, 1000, 0, 1000 - int(0.74 * cap))
        d, _ = self.run_diag('redo', second, first=first)
        self.assertEqual(d['status'], 'watch')
        self.assertEqual(d['headline'], 'Checkpoint age at 74% of the capacity')
        self.assertIn('where adaptive flushing is at its age limit', d['verdict'])
        # 80% in either sample: forced flushing (problem).
        first, second = self.lsn(int(0.80 * cap), int(0.80 * cap) + 1000, 0, int(0.80 * cap))
        d, _ = self.run_diag('redo', second, first=first)
        self.assertEqual(d['status'], 'problem')
        self.assertIn('where InnoDB forces flushing', d['verdict'])

    def test_redo_capacity_in_use_wins_over_the_variable(self):
        # Legacy innodb_log_file_size overrides innodb_redo_log_capacity, which still shows 100 MiB.
        second = dict(after(Innodb_os_log_written=1000), Innodb_redo_log_capacity_resized='100663296')
        report = compute(second)
        d = diag(report, 'redo')
        self.assertEqual(report['server']['redo_capacity_bytes'], 100663296)
        self.assertEqual(numbers(d)['capacity']['text'], '96.0 MiB (Innodb_redo_log_capacity_resized)')

    def test_redo_on_mariadb_uses_checkpoint_max_age(self):
        first = dict(BASE, Innodb_checkpoint_age='1000', Innodb_checkpoint_max_age='80000000')
        # 7/8 of 80,000,000 = 70,000,000.
        second = dict(after(Innodb_os_log_written=1000), Innodb_checkpoint_age='70000000',
                      Innodb_checkpoint_max_age='80000000')
        d, _ = self.run_diag('redo', second, MARIADB_VARS, first=first)
        self.assertEqual(d['status'], 'watch')
        self.assertIn('Innodb_checkpoint_max_age', d['sources'])
        self.assertIn("MariaDB's Innodb_os_log_written is the growth of the log sequence number", d['why'])
        self.assertIn('7/8 of Innodb_checkpoint_max_age', d['rule'])
        self.assertNotIn('os_log_written', numbers(d))
        self.assertIn('SET GLOBAL innodb_log_file_size', d['recommendation']['text'])
        self.assertIn('waits until the resize finishes', d['recommendation']['text'])
        problem = dict(second, Innodb_checkpoint_age='80000000')
        d, _ = self.run_diag('redo', problem, MARIADB_VARS, first=first)
        self.assertEqual(d['status'], 'problem')
        self.assertIn('makes writes wait', d['verdict'])
        # Without max_age: 0.71 of innodb_log_file_size (100,663,296) = 71,470,940.
        no_max = dict(after(Innodb_os_log_written=1000), Innodb_checkpoint_age='72000000')
        d, _ = self.run_diag('redo', no_max, MARIADB_VARS, first=dict(BASE, Innodb_checkpoint_age='1000'))
        self.assertEqual(d['status'], 'watch')

    def test_redo_on_old_mysql_uses_the_legacy_variables(self):
        old = dict(MYSQL_VARS, version='8.0.28', innodb_log_file_size='50331648', innodb_log_files_in_group='2')
        del old['innodb_redo_log_capacity']
        d, report = self.run_diag('redo', after(Innodb_os_log_written=1048576 * 10), old)
        self.assertEqual(report['server']['redo_capacity_bytes'], 100663296)
        self.assertEqual(numbers(d)['capacity']['text'], '96.0 MiB (innodb_log_file_size x innodb_log_files_in_group)')
        self.assertIn('does not report Innodb_redo_log_current_lsn (MySQL before 8.0.30)', d['why'])
        self.assertEqual([x['name'] for x in d['settings']], ['innodb_log_file_size', 'innodb_log_files_in_group'])
        self.assertIn('in the configuration file and restart', d['recommendation']['text'])

    def test_redo_log_waits_quiet_window_and_restart(self):
        d, _ = self.run_diag('redo', after(Innodb_log_waits=2))
        self.assertEqual(d['status'], 'watch')
        self.assertIn('points at innodb_log_buffer_size', d['verdict'])
        d, _ = self.run_diag('redo', after())
        self.assertEqual((d['status'], d['headline']), ('ok', 'No redo in this window'))
        d, _ = self.run_diag('redo', dict(after(), Uptime='5'))
        self.assertEqual(d['status'], 'unknown')
        self.assertEqual(d['trend']['value'], None)
        no_capacity = {k: v for k, v in MYSQL_VARS.items() if k != 'innodb_redo_log_capacity'}
        d, _ = self.run_diag('redo', after(Innodb_os_log_written=1000), no_capacity)
        self.assertEqual(d['status'], 'info')
        self.assertIn('does not report the redo capacity', d['verdict'])

    # table caches ------------------------------------------------------------
    def test_table_cache_overflows(self):
        cases = ((4, 'ok'), (20, 'watch'), (200, 'problem'))
        for overflows, status in cases:
            with self.subTest(overflows=overflows):
                d, _ = self.run_diag('table_cache', after(Table_open_cache_overflows=overflows,
                                                          Table_open_cache_misses=overflows,
                                                          Opened_tables=overflows, Table_open_cache_hits=1000))
                self.assertEqual(d['status'], status)
                if status != 'ok':
                    self.assertIn('raise table_open_cache', d['recommendation']['text'].lower())
                    self.assertIn('open_files_limit', d['recommendation']['text'])
                    self.assertIn('overflowed %d times' % overflows, d['verdict'])
                else:
                    self.assertIsNone(d['recommendation'])
        # Misses alone (a first open, another session's own instance) are not judged.
        d, _ = self.run_diag('table_cache', after(Table_open_cache_misses=300, Opened_tables=300,
                                                  Table_open_cache_hits=700))
        self.assertEqual((d['status'], d['headline']), ('ok', '30.0% misses, no overflows'))
        self.assertIn('MySQL divides table_open_cache across table_open_cache_instances', d['why'])

    def test_table_cache_on_mariadb_counts_active_instances(self):
        second = dict(after(), Open_tables='3000', Table_open_cache_active_instances='2')
        d, _ = self.run_diag('table_cache', second, MARIADB_VARS)
        self.assertEqual(numbers(d)['open_tables']['text'],
                         '3,000 of 4,000 (table_open_cache 2,000 x 2 active instances)')
        self.assertIn('Open_tables can exceed table_open_cache', d['why'])

    def test_table_definition_cache(self):
        full = dict(after(Opened_table_definitions=50), Open_table_definitions='1950')
        d, _ = self.run_diag('table_definitions', full)
        self.assertEqual(d['status'], 'watch')
        self.assertIn('so the cache is full', d['verdict'])
        self.assertIn('table_definition_cache', d['recommendation']['text'])
        self.assertEqual(d['settings'][0]['default'], '400 + table_open_cache / 2, at most 2000')
        roomy = after(Opened_table_definitions=50)
        self.assertEqual(self.run_diag('table_definitions', roomy)[0]['status'], 'ok')
        self.assertEqual(self.run_diag('table_definitions', full, MARIADB_VARS)[0]['settings'][0]['default'], '400')

    # threads, binary log, files ---------------------------------------------
    def test_thread_cache(self):
        d, _ = self.run_diag('thread_cache', after(Connections=100, Threads_created=50))
        self.assertEqual(d['status'], 'watch')
        self.assertEqual(d['headline'], '50% of connections reused a thread')
        self.assertIn('Raise thread_cache_size', d['recommendation']['text'])
        self.assertEqual(d['settings'][0]['default'], '8 + max_connections / 100, at most 100')
        self.assertEqual(self.run_diag('thread_cache', after(Connections=100, Threads_created=2))[0]['status'], 'ok')
        few, _ = self.run_diag('thread_cache', after(Connections=3, Threads_created=3))
        self.assertEqual(few['status'], 'ok')
        self.assertIn('too few to judge', few['verdict'])
        pool = dict(MARIADB_VARS, thread_handling='pool-of-threads')
        d, _ = self.run_diag('thread_cache', after(Connections=100, Threads_created=50), pool)
        self.assertEqual((d['status'], d['state']), ('info', 'not_applicable'))
        self.assertIn('Threads_created counts pool workers', d['verdict'])
        self.assertEqual(self.run_diag('thread_cache', after(Connections=100, Threads_created=50),
                                       MARIADB_VARS)[0]['settings'][0]['default'], '256, at most max_connections')

    def test_binary_log_cache(self):
        d, _ = self.run_diag('binlog_cache', after(Binlog_cache_use=100, Binlog_cache_disk_use=20))
        self.assertEqual(d['status'], 'watch')
        self.assertIn('20 of 100 transactions spilled to disk (20.0%)', d['verdict'])
        self.assertIn('binlog_cache_size', d['recommendation']['text'])
        self.assertEqual(numbers(d)['trx_disk_pct']['text'], '20 of 100 (20.0%)')
        ok, _ = self.run_diag('binlog_cache', after(Binlog_cache_use=100, Binlog_cache_disk_use=2))
        self.assertEqual(ok['status'], 'ok')
        # The binary log off (MariaDB's default): not applicable.
        off, _ = self.run_diag('binlog_cache', after(Binlog_cache_use=100, Binlog_cache_disk_use=20), MARIADB_VARS)
        self.assertEqual((off['status'], off['state'], off['headline']), ('info', 'not_applicable', 'Binary log off'))
        self.assertEqual(off['settings'][0], {'name': 'log_bin', 'value': 'OFF', 'display': 'OFF', 'default': 'OFF'})
        missing = {k: v for k, v in MYSQL_VARS.items() if k != 'log_bin'}
        d, _ = self.run_diag('binlog_cache', after(), missing)
        self.assertEqual(d['status'], 'unknown')
        self.assertIn('does not report log_bin', d['verdict'])

    def test_open_files(self):
        # 50 + 30 InnoDB files + 10 connections = 90 of 10,000.
        d, _ = self.run_diag('open_files')
        self.assertEqual((d['status'], d['value']), ('ok', 0.9))
        self.assertIn('At least 90 file descriptors are in use', d['verdict'])
        watch = dict(after(), Open_files='7500')
        self.assertEqual(self.run_diag('open_files', watch)[0]['status'], 'watch')
        problem, _ = self.run_diag('open_files', dict(after(), Open_files='9000'))
        self.assertEqual(problem['status'], 'problem')
        self.assertIn('needs a restart', problem['recommendation']['text'])
        no_limit = {k: v for k, v in MYSQL_VARS.items() if k != 'open_files_limit'}
        d, _ = self.run_diag('open_files', after(), no_limit)
        self.assertEqual(d['status'], 'unknown')

    def test_full_scans_rate_and_share(self):
        d, _ = self.run_diag('full_scans', after(Handler_read_rnd_next=60000, Handler_read_key=30000,
                                                 Handler_read_next=10000))
        self.assertEqual(d['status'], 'info')
        self.assertEqual(d['headline'], '6,000 rows/s by sequential scans')
        self.assertEqual(numbers(d)['share']['text'], '60.0% of 100,000')
        self.assertIn('internal temporary tables', d['why'])
        self.assertTrue(d['rule'].startswith('No threshold'))
        self.assertEqual((d['learn'], d['link']['view']), ('full_scan', 'top'))

    # temporary tables and sorts ---------------------------------------------
    def test_temporary_tables_explain_each_engine(self):
        second = after(Created_tmp_tables=100, Created_tmp_disk_tables=30)
        d, report = self.run_diag('tmp_tables', second)
        self.assertEqual(d['status'], by_key(report)['tmp_disk_ratio']['status'])
        self.assertEqual(d['status'], 'problem')
        self.assertIn('TempTable keeps each internal temporary table in memory up to tmp_table_size (16.0 MiB)',
                      d['why'])
        self.assertIn('on disk they are InnoDB tables', d['why'])
        self.assertEqual([x['name'] for x in d['settings']][:2], ['internal_tmp_mem_storage_engine', 'tmp_table_size'])
        memory = dict(MYSQL_VARS, internal_tmp_mem_storage_engine='MEMORY')
        self.assertIn('The MEMORY engine keeps', self.run_diag('tmp_tables', second, memory)[0]['why'])
        mariadb, _ = self.run_diag('tmp_tables', second, MARIADB_VARS)
        self.assertIn('go to Aria tables on disk', mariadb['why'])
        self.assertIn('twice in Created_tmp_tables', mariadb['why'])
        self.assertIn('max_heap_table_size too', mariadb['recommendation']['text'])
        self.assertIn('BLOB or TEXT columns go to disk regardless', mariadb['recommendation']['caveat'])
        unlimited = [x for x in mariadb['settings'] if x['name'] == 'tmp_disk_table_size'][0]
        self.assertEqual(unlimited['display'], 'unlimited')

    def test_temporary_tables_say_which_limit_on_mysql_9_1(self):
        first = dict(BASE, Count_hit_tmp_table_size='5', TempTable_count_hit_max_ram='0')
        second = dict(after(Created_tmp_tables=100, Created_tmp_disk_tables=30), Count_hit_tmp_table_size='5',
                      TempTable_count_hit_max_ram='30')
        d, _ = self.run_diag('tmp_tables', second, first=first)
        self.assertIn("TempTable's total memory (temptable_max_ram) was hit 30 times", d['verdict'])
        self.assertIn('user MEMORY tables that fill up count too', d['why'])
        self.assertIn('BLOB and TEXT columns stay in memory', d['recommendation']['caveat'])
        self.assertIn('temptable_max_ram (global) is the limit to look at', d['recommendation']['text'])
        self.assertEqual(numbers(d)['hit_max_ram']['value'], 30)
        # Absent before 9.1: no numbers, and nothing reported missing.
        d, report = self.run_diag('tmp_tables', after(Created_tmp_tables=100, Created_tmp_disk_tables=30))
        self.assertNotIn('hit_max_ram', numbers(d))
        self.assertFalse(any('Count_hit' in n for n in report['notes']))

    def test_sorts(self):
        d, report = self.run_diag('sorts', after(Sort_merge_passes=100, Sort_range=40, Sort_scan=10))
        self.assertEqual(d['status'], by_key(report)['sort_merge_passes']['status'])
        self.assertEqual(d['headline'], '100 merge passes for 50 sorts')
        self.assertEqual(d['settings'][0], {'name': 'sort_buffer_size', 'value': '262144', 'display': '256.0 KiB',
                                            'default': '256.0 KiB'})
        mariadb, _ = self.run_diag('sorts', after(Sort_merge_passes=100), MARIADB_VARS)
        self.assertEqual(mariadb['settings'][0]['default'], '2.0 MiB')
        self.assertEqual(mariadb['learn'], 'filesort')
        self.assertEqual(self.run_diag('sorts')[0]['status'], 'ok')

    # together ----------------------------------------------------------------
    def test_self_overhead_keeps_every_diagnostic_quiet(self):
        own = after(Questions=3, Queries=3, Com_select=1, Select_scan=2, Created_tmp_tables=1, Opened_tables=1,
                    Connections=1, Threads_created=1, Table_open_cache_misses=1, Handler_read_rnd_next=600)
        for variables in (MYSQL_VARS, MARIADB_VARS):
            with self.subTest(engine=variables['version']):
                report = compute(own, variables, elapsed=1.0)
                self.assertEqual(report['summary']['status'], 'ok')
                self.assertEqual(report['summary']['diagnostics']['watch'], 0)

    def test_every_diagnostic_is_unknown_or_info_without_data(self):
        report = compute({}, {}, first={})
        for d in report['diagnostics']:
            self.assertIn(d['status'], ('unknown', 'info'), d['key'])
        self.assertEqual(report['summary']['diagnostics']['unknown'], len(DIAGNOSTIC_KEYS))

    def test_a_diagnostic_alone_sets_the_overall_status(self):
        # Every metric is fine; the redo diagnostic watches (100 MiB covers 100 s).
        report = compute(after(Innodb_os_log_written=10485760))
        self.assertEqual(report['summary']['watch'], 0)
        self.assertEqual(report['summary']['diagnostics']['watch'], 1)
        self.assertEqual(report['summary']['status'], 'watch')
        self.assertEqual([x['key'] for x in health.at_or_worse(report, 'watch')], ['redo'])

    def test_text_output_prints_the_diagnostics_first(self):
        report = compute(after(Innodb_os_log_written=10485760))
        text = health.format_text(report)
        self.assertIn('; diagnostics: 0 problem, 1 watch,', text.splitlines()[1])
        self.assertLess(text.index('\nDIAGNOSTICS\n'), text.index('\nTHROUGHPUT\n'))
        self.assertRegex(text, r'\n  WATCH  Redo log size for the write rate +Covers about 1 min 40 s of writes\n')
        self.assertIn('Suggestion: If this rate is typical of busy periods', text)
        self.assertIn('Caveat: One short window may be a burst or a lull', text)
        for line in text.splitlines():
            self.assertLessEqual(len(line), 140, line)


class TestLiveScenarios(HealthCase):
    """Diagnostics on captures from throwaway MySQL 8.4/26.7 and MariaDB 11.4/13.0 containers."""

    def scenario(self, server, name):
        data = fixture('scenario-%s-%s' % (server, name))
        first, second = data['samples']
        report = health.compute_health(first, second, data['variables'], 5)
        self.assert_shape(report)
        return report

    def test_warming_right_after_start(self):
        for server in SCENARIO_SERVERS:
            with self.subTest(server=server):
                d = diag(self.scenario(server, 'warming'), 'buffer_pool')
                self.assertEqual((d['state'], d['status']), ('warming', 'info'))
                self.assertTrue(d['headline'].startswith('Warming: '))
                self.assertIn('to full at this rate', d['headline'])
                self.assertTrue(numbers(d)['time_to_full']['estimate'])

    def test_full_with_a_working_set_larger_than_the_pool(self):
        for server in SCENARIO_SERVERS:
            with self.subTest(server=server):
                report = self.scenario(server, 'full')
                d = diag(report, 'buffer_pool')
                self.assertEqual(d['state'], 'full')
                self.assertIn(d['status'], ('watch', 'problem'))
                self.assertGreater(d['value'], 50 * 1048576, 'more than 50 MiB/s read from disk')
                self.assertTrue(d['headline'].endswith('/s read from disk'))
                n = numbers(d)
                # Innodb_data_read agrees with pages x page size within 10%.
                self.assertAlmostEqual(n['data_read']['value'] / n['read_bytes']['value'], 1.0, delta=0.1)
                self.assertTrue(d['recommendation'])

    def test_redo_from_an_insert_loop(self):
        for server in SCENARIO_SERVERS:
            with self.subTest(server=server):
                d = diag(self.scenario(server, 'redo'), 'redo')
                self.assertIn(d['status'], ('watch', 'problem'))
                self.assertLess(numbers(d)['coverage']['value'], 3600)
                if server.startswith('mysql'):
                    self.assertIn('Innodb_redo_log_capacity_resized', numbers(d)['capacity']['text'])
                    self.assertIn('os_log_written', numbers(d))
                else:
                    self.assertIn('Innodb_checkpoint_max_age', d['sources'])

    def test_table_cache_overflows_with_a_small_cache(self):
        for server in SCENARIO_SERVERS:
            with self.subTest(server=server):
                d = diag(self.scenario(server, 'table-cache'), 'table_cache')
                self.assertEqual(d['status'], 'problem')
                self.assertGreater(numbers(d)['overflows']['value'], 100)

    def test_thread_cache_off(self):
        for server in SCENARIO_SERVERS:
            with self.subTest(server=server):
                d = diag(self.scenario(server, 'threads'), 'thread_cache')
                self.assertEqual(d['status'], 'watch')
                self.assertEqual(d['value'], 0)

    def test_binary_log_cache_spills_on_mysql_and_is_off_on_mariadb(self):
        for server in ('mysql-8.4', 'mysql-26.7'):
            with self.subTest(server=server):
                d = diag(self.scenario(server, 'binlog'), 'binlog_cache')
                self.assertEqual(d['status'], 'watch')
        for server in ('mariadb-11.4', 'mariadb-13.0'):
            with self.subTest(server=server):
                d = diag(self.scenario(server, 'redo'), 'binlog_cache')
                self.assertEqual(d['state'], 'not_applicable')


# ---- 6. sampling ------------------------------------------------------------------------------------

class TestSampling(HealthCase):
    def test_waits_out_the_interval_and_reads_only(self):
        clock = FakeTime(query_s=0.2)
        conn = FakeConn('8.4.6', MYSQL_VARS, [BASE, after(Questions=100)], clock=clock)
        report = health.health_report(conn, 5, sleep=clock.sleep, clock=clock.monotonic)
        self.assert_read_only(conn)
        self.assertEqual([sql[len(HEALTH_TAG):].split(' WHERE')[0] for sql, _ in conn.calls],
                         ['SHOW GLOBAL VARIABLES', 'SHOW GLOBAL STATUS', 'SHOW GLOBAL STATUS'])
        self.assertIn("'max_connections'", conn.calls[0][0])
        self.assertEqual(len(clock.slept), 1)
        self.assertAlmostEqual(clock.slept[0], 5.0)
        self.assertAlmostEqual(report['elapsed_s'], 5.2)
        self.assertEqual(by_key(report)['questions']['rate'], round(100 / 5.2, 2))

    def test_no_sleep_when_the_first_sample_was_slow(self):
        clock = FakeTime()
        conn = FakeConn('8.4.6', MYSQL_VARS, [BASE, after()], clock=clock)
        calls = iter([0.0, 3.0, 3.5])
        health.health_report(conn, 1, sleep=clock.sleep, clock=lambda: next(calls))
        self.assertEqual(clock.slept, [])

    def test_interval_is_validated_before_any_sql(self):
        for interval in (0, 61, '5', 5.0, True, None):
            with self.subTest(interval=interval):
                conn = FakeConn('8.4.6', MYSQL_VARS, [])
                with self.assertRaisesRegex(ValueError, 'from 1 to 60 seconds'):
                    health.health_report(conn, interval, sleep=lambda s: None)
                self.assertEqual(conn.calls, [])

    def test_connector_errors_become_value_errors(self):
        conn = FakeConn('8.4.6', MYSQL_VARS, [], error=ConnectorError(
            'mysql client exited 1: ERROR 2003 (HY000): Can\'t connect to MySQL server'))
        with self.assertRaisesRegex(ValueError, r"^Could not read the server status: ERROR 2003"):
            health.health_report(conn, 1, sleep=lambda s: None)


# ---- 7. CLI ------------------------------------------------------------------------------------------

class TestHealthCLI(HealthCase):
    def call(self, conn, *args):
        factory = FakeFactory(conn)
        clock = conn.clock or FakeTime()
        conn.clock = clock
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch('myflames.connector.MySQLConnection', factory), \
                mock.patch.object(health, 'time', clock), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                workspace_cli.cmd_health(list(args))
            except SystemExit as exc:
                code = exc.code
        if conn.calls and conn.error is None:
            self.assert_read_only(conn)
        return code, out.getvalue(), err.getvalue(), factory, clock

    def test_json_shape_and_connection_flags(self):
        code, out, err, factory, clock = self.call(conn_for('mysql-8.4'), '-h', 'db', '-P', '3307', '-u', 'app',
                                                   '-p' + PASSWORD, '--json', '--interval', '7')
        self.assertEqual((code, err), (0, ''))
        data = json.loads(out)
        self.assert_shape(data)
        self.assertEqual(data['interval'], 7)
        self.assertAlmostEqual(clock.slept[0], 7.0)
        self.assertEqual((factory.options['host'], factory.options['port'], factory.options['user'],
                          factory.options['password']), ('db', 3307, 'app', PASSWORD))
        self.assertNotIn(PASSWORD, out)

    def test_human_output_and_notes(self):
        code, out, err, _, _ = self.call(conn_for('mariadb-13.0'))
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith('MariaDB 13.0.2-MariaDB-ubu2604, up '))
        positions = [out.index('\n%s\n' % label.upper()) for _, label in health.GROUPS]
        self.assertEqual(positions, sorted(positions))
        self.assertRegex(out, r'\n  ok +Waits for a free page +0/s')
        self.assertRegex(out, r'\n  PROBLEM +Joins without an index')
        self.assertIn('note: Statuses are rules of thumb, not verdicts', err)

    def test_quiet_and_output_file(self):
        code, out, err, _, _ = self.call(conn_for('mysql-9.7'), '-q')
        self.assertEqual((code, err), (0, ''))
        self.assertIn('INNODB BUFFER POOL', out)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'health.json')
            code, out, err, _, _ = self.call(conn_for('mysql-26.7'), '--json', '-o', path)
            self.assertEqual((code, out), (0, ''))
            self.assertIn('Written to ' + path, err)
            with open(path, encoding='utf-8') as handle:
                self.assertEqual(json.load(handle)['server_version'], '26.7.0')

    def test_fail_on_problem_and_watch(self):
        # The live fixtures have problems (joins without an index, the long lock wait).
        code, out, err, _, _ = self.call(conn_for('mysql-8.4'), '--fail-on', 'problem')
        self.assertEqual(code, 1)
        self.assertIn('MySQL 8.4.11', out)
        self.assertRegex(err, r'FAIL: \d+ diagnostic\(s\) or metric\(s\) at --fail-on problem or worse:\n')
        self.assertIn('  [problem] Joins without an index: ', err)
        # A diagnostic prints its title and verdict, before the metrics.
        self.assertIn('  [problem] Sorts that spill to disk: ', err)
        self.assertLess(err.index('Sorts that spill to disk'), err.index('Joins without an index'))
        code, out, err, _, _ = self.call(conn_for('mysql-8.4'), '--fail-on', 'problem', '--json', '-q')
        self.assertEqual((code, err), (1, ''))
        self.assertEqual(json.loads(out)['summary']['status'], 'problem')

    def test_fail_on_passes_on_a_quiet_server(self):
        quiet = FakeConn('8.4.6', MYSQL_VARS, [BASE, after(Questions=3, Opened_tables=1)])
        code, out, err, _, _ = self.call(quiet, '--fail-on', 'watch')
        self.assertEqual(code, 0)
        self.assertIn('OK: no diagnostic or metric at --fail-on watch', err)
        watch = FakeConn('8.4.6', MYSQL_VARS, [BASE, after(Innodb_log_waits=1)])
        code, _, err, _, _ = self.call(watch, '--fail-on', 'problem')
        self.assertEqual(code, 0)
        watch = FakeConn('8.4.6', MYSQL_VARS, [BASE, after(Innodb_log_waits=1)])
        code, _, err, _, _ = self.call(watch, '--fail-on', 'watch')
        self.assertEqual(code, 1)
        self.assertIn('[watch] Waits for the log buffer', err)

    def test_bad_arguments_exit_2_without_connecting(self):
        for argv in (['--interval', '0'], ['--interval', '61'], ['--interval', 'x'], ['--fail-on', 'warn'],
                     ['--fail-on']):
            with self.subTest(argv=argv):
                conn = FakeConn('8.4.6', MYSQL_VARS, [BASE, BASE])
                code, out, _, factory, _ = self.call(conn, *argv)
                self.assertEqual((code, out), (2, ''))
                self.assertIsNone(factory.options)
                self.assertEqual(conn.calls, [])

    def test_failures_exit_2_and_redact_the_password(self):
        conn = FakeConn('8.4.6', MYSQL_VARS, [], error=ConnectorError(
            "mysql client exited 1: ERROR 1045 (28000): Access denied for user 'app' " + PASSWORD))
        code, out, err, _, _ = self.call(conn, '-u', 'app', '-p' + PASSWORD, '--fail-on', 'problem')
        self.assertEqual((code, out), (2, ''))
        self.assertNotIn(PASSWORD, err)
        self.assertIn('[redacted]', err)
        self.assertIn('Could not read the server status: ERROR 1045', err)

    def test_root_help_lists_health_and_dispatch_reaches_cmd_health(self):
        from myflames import cli

        def run(argv, conn=None):
            out, err = io.StringIO(), io.StringIO()
            code = 0
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(sys, 'argv', argv))
                if conn is not None:
                    stack.enter_context(mock.patch('myflames.connector.MySQLConnection', FakeFactory(conn)))
                    stack.enter_context(mock.patch.object(health, 'time', FakeTime()))
                stack.enter_context(contextlib.redirect_stdout(out))
                stack.enter_context(contextlib.redirect_stderr(err))
                try:
                    cli.main()
                except SystemExit as exc:
                    code = exc.code
            return code, out.getvalue(), err.getvalue()

        code, out, _ = run(['myflames', '--help'])
        self.assertEqual(code, 0)
        self.assertIn('  health    Sample global status twice: diagnostics and rates, ok/watch/problem (--fail-on)',
                      out)
        self.assertIn('myflames health -h localhost -u admin -p --interval 10', out)
        code, out, _ = run(['myflames', 'health', '--help'])
        self.assertEqual(code, 0)
        for flag in ('--interval N', '--fail-on LEVEL', '--json', '--profile', '-q, --quiet'):
            self.assertIn(flag, out)
        self.assertIn('Exit codes: 0 success', ' '.join(out.split()))
        conn = conn_for('mariadb-11.4')
        code, out, err = run(['myflames', 'health', '--json', '-q'], conn)
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(json.loads(out)['engine'], 'mariadb')


# ---- 8. UI endpoint ------------------------------------------------------------------------------------

class TestHealthAPI(HealthCase):
    def connection(self):
        return {'host': '127.0.0.1', 'port': 3306, 'user': 'app', 'password': PASSWORD}

    def call(self, conn, payload):
        factory = FakeFactory(conn)
        clock = FakeTime()
        conn.clock = clock
        with mock.patch('myflames.connector.MySQLConnection', factory), mock.patch.object(health, 'time', clock):
            result = ui.api_response('/api/live/health', payload, workspace=object())
        self.assert_read_only(conn)
        return result, factory, clock

    def test_returns_the_report_with_the_interval(self):
        result, factory, clock = self.call(conn_for('mariadb-13.0'), {'connection': self.connection(), 'interval': 2})
        self.assert_shape(result)
        self.assertEqual(result['interval'], 2)
        self.assertAlmostEqual(clock.slept[0], 2.0)
        self.assertEqual(factory.options['password'], PASSWORD)

    def test_default_interval(self):
        result, _, clock = self.call(conn_for('mysql-8.4'), {'connection': self.connection()})
        self.assertEqual(result['interval'], 5)

    def test_bad_interval_is_rejected_before_any_sql(self):
        for interval in (0, 61, '5', True, 2.5):
            with self.subTest(interval=interval):
                conn = FakeConn('8.4.6', MYSQL_VARS, [])
                with mock.patch('myflames.connector.MySQLConnection', FakeFactory(conn)):
                    with self.assertRaisesRegex(ValueError, 'from 1 to 60 seconds'):
                        ui.api_response('/api/live/health', {'connection': self.connection(), 'interval': interval},
                                        workspace=object())
                self.assertEqual(conn.calls, [])

    def test_needs_a_workspace_and_connection(self):
        with self.assertRaisesRegex(ValueError, 'requires a running workspace'):
            ui.api_response('/api/live/health', {'connection': self.connection()})
        with self.assertRaisesRegex(ValueError, 'connection settings'):
            ui.api_response('/api/live/health', {}, workspace=object())

    def test_errors_never_echo_the_password(self):
        for error in (ConnectorError('mysql client exited 1: ERROR 1045: denied ' + PASSWORD),
                      ValueError('oops ' + PASSWORD)):
            with self.subTest(error=error):
                conn = FakeConn('8.4.6', MYSQL_VARS, [], error=error)
                with mock.patch('myflames.connector.MySQLConnection', FakeFactory(conn)):
                    with self.assertRaises((ValueError, ConnectorError)) as caught:
                        ui.api_response('/api/live/health', {'connection': self.connection()}, workspace=object())
                self.assertNotIn(PASSWORD, str(caught.exception))
                self.assertIn('[redacted]', str(caught.exception))

    def test_saved_password_is_filled_for_the_request(self):
        class Store(object):
            def connection_with_saved_password(self, connection):
                return dict(connection, password='from-keychain')

        class Workspace(object):
            store = Store()

        conn = conn_for('mysql-8.4')
        factory = FakeFactory(conn)
        with mock.patch('myflames.connector.MySQLConnection', factory), \
                mock.patch.object(health, 'time', FakeTime()):
            ui.api_response('/api/live/health', {'connection': {'host': '127.0.0.1', 'profile_id': 'p1'},
                                                 'interval': 1}, workspace=Workspace())
        self.assertEqual(factory.options['password'], 'from-keychain')

    def test_route_is_allowlisted_and_takes_saved_profiles(self):
        self.assertIn('"/api/live/health"', inspect.getsource(ui.WorkspaceHandler.do_POST))
        self.assertIn('/api/live/health', ui._CONNECTION_PATHS)


if __name__ == '__main__':
    unittest.main()
