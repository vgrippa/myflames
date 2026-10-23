"""Error log contracts with synthetic batch replies and temporary local files.

The formats follow the source-verified design spec; these tests do not claim
to be live MySQL/MariaDB validation. No server or network is used.
"""
import ast
import calendar
import datetime
import email.message
import inspect
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile
import unittest
from unittest import mock

try:
    from _perf_replay import FakeFactory, ReplayConn, assert_read_only, batch, fixture, run_cli, run_main
except ImportError:
    from ._perf_replay import FakeFactory, ReplayConn, assert_read_only, batch, fixture, run_cli, run_main
from myflames import error_log as m, ui
from myflames.connector import ConnectorError

START = '2026-10-19 20:26:00'
NOW = '2026-10-19 20:30:00'


def epoch(text):
    """UNIX_TIMESTAMP() of a server time; the fake server keeps UTC."""
    whole = calendar.timegm(datetime.datetime.strptime(text[:19], '%Y-%m-%d %H:%M:%S').timetuple())
    return whole + (float('0' + text[19:]) if len(text) > 19 else 0.0)
MYSQL_ROWS = [
    ['2026-10-19 20:25:59.999999', None, 'Error', 'MY-000001', 'Server', 'previous crash'],
    ['2026-10-19 20:26:00.000000', 0, 'System', 'MY-010931', 'Server', 'ready for connections.'],
    ['2026-10-19 20:26:15.338106', 42, 'Warning', 'MY-000002', 'InnoDB', 'a warning\nmore detail'],
    ['2026-10-19 20:26:16.000000', 0, 'Note', 'MY-000003', None, 'a note'],
    ['2026-10-19 20:26:17.000000', 2 ** 63, 'Error', 'MY-000001', 'Server', '{"msg":"current error\\ncontinued"}'],
]
MARIA_LOG = """2026-10-19 20:26:10 0 [Warning] mariadbd: io_uring_queue_init() failed with EPERM: ...
create_uring failed: falling back to libaio
2026-10-19 20:26:11 0 [Note] Starting MariaDB 11.4.13-MariaDB-ubu2404 source revision ... as process 100
2026-10-19 20:26:11 0 [Note] InnoDB: Compressed tables use zlib 1.3
2026-10-19 20:26:11 0 [Note] mariadbd: ready for connections.
Version: '11.4.13-MariaDB-ubu2404'  socket: '/run/mysqld/mysqld.sock'  port: 0  mariadb.org binary distribution
2026-10-19 20:26:14 0 [Note] mariadbd: ready for connections.
Version: '11.4.13-MariaDB-ubu2404'  socket: '/run/mysqld/mysqld.sock'  port: 3306  mariadb.org binary distribution
2026-10-19 20:26:15 5 [ERROR] InnoDB: example failure
stack line
2026-10-19 20:26:16 8 [Warning] another warning
"""
# The running server's startup lines (start 20:26:12 in the fake), for tests
# that exercise the file report rather than identity.
BANNER = ("2026-10-19 20:26:14 0 [Note] mariadbd: ready for connections.\n"
          "Version: '11.4.13-MariaDB-ubu2404'  socket: '/run/mysqld/mysqld.sock'  port: 3306  "
          "mariadb.org binary distribution\n")


class LogConn(object):
    """Answer only the known tagged read shapes, in mysql --batch format."""
    def __init__(self, maria=False, path='', datadir='', host='localhost', ps=True, fail=None):
        self.maria, self.path, self.datadir, self.host, self.ps = maria, path, datadir, host, ps
        self.fail = fail
        self.hostname, self.port = 'server.example.test', 3306
        self.version = '11.4.13-MariaDB-ubu2404' if maria else '8.4.11'
        self.verbosity, self.services = 2, 'log_filter_internal; log_sink_internal'
        self.rows = [list(row) for row in MYSQL_ROWS]
        self.calls = []
        self.latest_write = 1792441577338106
        self.expired = 12
        self.uptime = 228 if maria else 240

    def run(self, sql, extra_flags=None, timeout=None):
        self.calls.append((sql, list(extra_flags or [])))
        if self.fail and (self.fail[0] in sql):
            raise ConnectorError(self.fail[1])
        if not sql.startswith(m.ERROR_LOG_TAG):
            raise AssertionError('untagged SQL: ' + sql)
        sql = sql[len(m.ERROR_LOG_TAG):]
        if sql.startswith('SHOW GLOBAL VARIABLES WHERE Variable_name IN '):
            rows = [['version', self.version], ['version_comment', ''], ['performance_schema', 'ON' if self.ps else 'OFF'],
                    ['log_error', self.path if self.maria else 'stderr'], ['hostname', self.hostname],
                    ['port', self.port]]
            if self.maria:
                rows += [['datadir', self.datadir], ['log_warnings', 2]]
            else:
                rows += [['log_error_verbosity', self.verbosity], ['log_error_services', self.services]]
            return batch(rows)
        if sql.startswith('SHOW GLOBAL STATUS WHERE Variable_name = '):
            return batch([['Uptime', self.uptime]])
        started = (datetime.datetime.strptime(NOW, '%Y-%m-%d %H:%M:%S')
                   - datetime.timedelta(seconds=self.uptime)).strftime('%Y-%m-%d %H:%M:%S')
        if sql == m._MYSQL_STATUS:
            rows = [['Uptime', self.uptime], ['Error_log_buffered_bytes', 4096],
                    ['Error_log_buffered_events', len(self.rows)], ['Error_log_expired_events', self.expired],
                    ['Error_log_latest_write', self.latest_write]]
            return batch([row + [int(epoch(started)), started] for row in rows])
        if sql == m._MARIA_STATUS:
            return batch([[self.uptime, started]])
        if sql == ("SELECT PRIO, COUNT(*), SUM(UNIX_TIMESTAMP(LOGGED) >= %d) FROM performance_schema.error_log "
                   "GROUP BY PRIO" % epoch(started)):
            return batch([[prio, sum(row[2] == prio for row in self.rows),
                           sum(row[2] == prio and epoch(row[0]) >= epoch(started) for row in self.rows)]
                          for prio in m.LEVELS if any(row[2] == prio for row in self.rows)])
        if sql == ('SELECT ERROR_CODE, PRIO, SUBSYSTEM, COUNT(*), MIN(LOGGED), MAX(LOGGED), MAX(DATA) '
                   'FROM performance_schema.error_log GROUP BY ERROR_CODE, PRIO, SUBSYSTEM ORDER BY COUNT(*) DESC LIMIT 20'):
            groups = {}
            for row in self.rows:
                groups.setdefault((row[3], row[2], row[4]), []).append(row)
            return batch([list(key) + [len(rows), min(row[0] for row in rows), max(row[0] for row in rows),
                                      max(row[5] for row in rows)]
                          for key, rows in sorted(groups.items(), key=lambda group: -len(group[1]))][:20])
        if sql.startswith('SELECT e.LOGGED, e.THREAD_ID, e.PRIO, e.ERROR_CODE, e.SUBSYSTEM, e.DATA, '
                          'UNIX_TIMESTAMP(e.LOGGED), t.entries_total '):
            levels = re.search(r'WHERE PRIO IN \(([^)]+)\)', sql).group(1)
            selected = sorted([row for row in self.rows if "'%s'" % row[2] in levels], reverse=True)
            limit = int(re.search(r'LIMIT (\d+)', sql).group(1))
            return batch([row + ['%.6f' % epoch(row[0]), len(selected)] for row in selected[:limit]]
                         or [[None] * 7 + [0]])
        raise AssertionError('unexpected SQL: ' + sql)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


class SparseConn(LogConn):
    """LogConn whose settings read or error-log status read lacks the named rows."""
    def __init__(self, omit, **kwargs):
        LogConn.__init__(self, **kwargs)
        self.omit = omit

    def run(self, sql, extra_flags=None, timeout=None):
        text = LogConn.run(self, sql, extra_flags, timeout)
        if sql.endswith(m._MARIA_STATUS) and 'Uptime' in self.omit:
            return ''
        if 'SHOW GLOBAL VARIABLES' in sql or sql.endswith(m._MYSQL_STATUS):
            text = ''.join(line + '\n' for line in text.splitlines() if line.split('\t')[0] not in self.omit)
        return text


class LogCase(unittest.TestCase):
    def report(self, conn=None, **kwargs):
        conn = conn or LogConn()
        result = m.error_log_report(conn, **kwargs)
        assert_read_only(self, conn, m.ERROR_LOG_TAG)
        for sql, _ in conn.calls:
            self.assertNotIn('LOAD_FILE', sql.upper())
        json.dumps(result, allow_nan=False)
        return result


class TestMySQL(LogCase):
    def test_old_mysql_returns_unsupported_before_error_log_reads(self):
        """MariaDB uses its separate file reader, not this MySQL-only table."""
        for version, omit in [('8.0.21', []), ('8.0.1', []),
                              ('5.7.44', ['log_error_services', 'log_error_verbosity'])]:
            with self.subTest(version=version):
                conn = SparseConn(omit)
                conn.version = version
                report = self.report(conn)
                self.assertFalse(report['available'])
                self.assertEqual(report['reason'], 'unsupported_version')
                self.assertEqual(report['reason_text'],
                                 'performance_schema.error_log needs MySQL 8.0.22 or later.')
                self.assertTrue(report['how_to_view'])
                self.assertEqual(report['summary']['status'], 'unknown')
                self.assertIsNone(report['buffer'])
                self.assertEqual(report['entries'], [])
                self.assertEqual(len(conn.calls), 2, 'only preflight may read an older server')
                self.assertTrue(all('performance_schema.error_log' not in sql and 'Error_log_' not in sql
                                    for sql, _ in conn.calls))

    def test_mysql_8022_reads_the_error_log(self):
        conn = LogConn()
        conn.version = '8.0.22'
        report = self.report(conn)
        self.assertTrue(report['available'])
        self.assertEqual(report['source'], 'performance_schema.error_log')
        self.assertEqual(len(conn.calls), 6)

    def test_complete_envelope_and_four_report_reads(self):
        conn = LogConn()
        report = self.report(conn)
        self.assertEqual(len(conn.calls), 6)  # Two preflight reads (settings included), four report reads.
        self.assertEqual(report['schema_version'], 'error-log-1.0')
        self.assertTrue(report['available'])
        self.assertEqual(report['source'], 'performance_schema.error_log')
        self.assertIsNone(report['file'])
        self.assertIsNone(report['reason'])
        self.assertIsNone(report['reason_text'])
        self.assertIsNone(report['how_to_view'])
        self.assertEqual(report['settings'], {'log_error': 'stderr', 'log_error_verbosity': 2,
                                              'log_error_services': conn.services, 'datadir': None, 'log_warnings': None})
        self.assertEqual(report['buffer'], {'bytes': 4096, 'events': 5, 'expired_events': 12,
                                            'latest_write': '2026-10-19 20:26:17Z', 'capacity_bytes': 5242880})
        self.assertEqual(report['server_started'], START)
        self.assertEqual(report['counts'], {'system': 1, 'error': 2, 'warning': 1, 'note': 1})
        self.assertEqual(report['counts_current_run'], {'system': 1, 'error': 1, 'warning': 1, 'note': 1})
        self.assertEqual(report['entries_total'], 5)
        self.assertEqual(report['limit'], 200)
        self.assertEqual(report['levels'], list(m.LEVELS))
        self.assertEqual(report['summary']['status'], 'watch')
        self.assertEqual(report['entries'][0]['message'], 'current error\ncontinued')
        self.assertEqual(report['entries'][0]['thread_id'], 2 ** 63)
        self.assertFalse(report['entries'][-2]['previous_run'])  # Exact start time.
        self.assertTrue(report['entries'][-1]['previous_run'])
        self.assertIsNone(report['entries'][-1]['thread_id'])
        self.assertEqual(report['codes'][0], {'error_code': 'MY-000001', 'prio': 'Error', 'subsystem': 'Server',
                                            'count': 2, 'first_logged': MYSQL_ROWS[0][0], 'last_logged': MYSQL_ROWS[-1][0],
                                            'sample': 'current error\ncontinued'})
        self.assertTrue(any('expired 12' in note for note in report['notes']))

    def test_filter_limit_does_not_filter_summary_counts_or_codes(self):
        report = self.report(levels=['warning', 'NOTE', 'warning'], limit=1)
        self.assertEqual(report['levels'], ['Warning', 'Note'])
        self.assertEqual(report['entries_total'], 2)
        self.assertEqual([row['prio'] for row in report['entries']], ['Note'])
        self.assertEqual(report['counts_current_run']['error'], 1)
        self.assertEqual(report['summary']['status'], 'watch')
        self.assertEqual(report['codes'][0]['count'], 2)

    def test_empty_filtered_entries_still_return_count(self):
        conn = LogConn()
        conn.rows = [row for row in conn.rows if row[2] != 'System']
        report = self.report(conn, levels='system')
        self.assertEqual(report['entries'], [])
        self.assertEqual(report['entries_total'], 0)

    def test_empty_ring_is_ok_and_latest_write_zero_is_null(self):
        conn = LogConn()
        conn.rows, conn.latest_write = [], 0
        report = self.report(conn)
        self.assertEqual(report['counts'], dict.fromkeys(('system', 'error', 'warning', 'note'), 0))
        self.assertEqual(report['summary']['status'], 'ok')
        self.assertIsNone(report['buffer']['latest_write'])

    def test_previous_errors_do_not_trip_gate(self):
        conn = LogConn()
        conn.rows = conn.rows[:1]
        report = self.report(conn)
        self.assertEqual(report['summary']['status'], 'ok')
        self.assertFalse(m.at_or_worse(report, 'watch'))

    def test_levels_and_limit_validation_before_sql(self):
        for bad in (0, -1, 1001, 2.5, '3', True, None):
            conn = LogConn()
            with self.subTest(limit=bad), self.assertRaises(ValueError):
                m.error_log_report(conn, limit=bad)
            self.assertEqual(conn.calls, [])
        for bad in ('fatal', 'Error,,Note', ['Error', 1], 1, {}, True, ['']):
            conn = LogConn()
            with self.subTest(levels=bad), self.assertRaises(ValueError):
                m.error_log_report(conn, levels=bad)
            self.assertEqual(conn.calls, [])
        for limit in (1, 1000):
            self.assertEqual(self.report(limit=limit)['limit'], limit)
        for empty in (None, [], (), ''):
            self.assertEqual(self.report(levels=empty)['levels'], list(m.LEVELS))

    def test_performance_schema_off_never_reads_pfs(self):
        conn = LogConn(ps=False)
        report = self.report(conn)
        self.assertEqual(report['reason'], 'performance_schema_off')
        self.assertFalse(report['available'])
        self.assertEqual(report['summary']['status'], 'unknown')
        self.assertFalse(any('FROM performance_schema.' in sql for sql, _ in conn.calls))

    def test_no_sink_and_capable_sink_lists(self):
        conn = LogConn()
        for services in ('', 'log_filter_internal; log_sink_test', 'not_log_sink_internal'):
            conn.services = services
            report = self.report(conn)
            self.assertEqual(report['reason'], 'no_pfs_sink')
        for services in ('log_filter_internal; log_sink_internal', 'log_filter_internal, log_sink_json',
                         ' log_sink_syseventlog '):
            conn.services = services
            self.assertTrue(self.report(conn)['available'])

    def test_denials_at_each_read_are_structured(self):
        for fragment in ('SHOW GLOBAL VARIABLES', 'FROM performance_schema.global_status',
                         'SELECT PRIO', 'SELECT ERROR_CODE', 'SELECT e.LOGGED'):
            for error in ('ERROR 1142 SELECT command denied', 'ERROR 1227 Access denied'):
                with self.subTest(fragment=fragment, error=error):
                    report = self.report(LogConn(fail=(fragment, error)))
                    self.assertEqual(report['reason'], 'denied')
                    self.assertEqual(report['summary']['status'], 'unknown')
                    self.assertFalse(m.at_or_worse(report, 'watch'))

    def test_denial_after_earlier_reads_carries_no_partial_data(self):
        for fragment in ('SELECT PRIO', 'SELECT ERROR_CODE', 'SELECT e.LOGGED'):
            with self.subTest(fragment=fragment):
                report = self.report(LogConn(fail=(fragment, 'ERROR 1143 (42000): SELECT command denied for column')))
                self.assertEqual(report['reason'], 'denied')
                self.assertEqual(set(report['counts'].values()), {0})
                self.assertEqual(set(report['counts_current_run'].values()), {0})
                self.assertEqual((report['codes'], report['entries'], report['buffer'], report['source']),
                                 ([], [], None, None))

    def test_previous_run_uses_epoch_seconds_not_the_displayed_time(self):
        # After a daylight saving change the displayed LOGGED and start times
        # can be an hour apart from their real order; epochs decide.
        class Shifted(LogConn):
            def run(self, sql, extra_flags=None, timeout=None):
                text = LogConn.run(self, sql, extra_flags, timeout)
                if 'UNIX_TIMESTAMP(e.LOGGED)' in sql:
                    rows = [line.split('\t') for line in text.splitlines()]
                    for row in rows:
                        if row[0] == '2026-10-19 20:25:59.999999':
                            row[6] = '%.6f' % (epoch(START) + 3600)
                    text = ''.join('\t'.join(row) + '\n' for row in rows)
                return text
        report = self.report(Shifted())
        shifted = next(e for e in report['entries'] if e['logged'] == '2026-10-19 20:25:59.999999')
        self.assertFalse(shifted['previous_run'])

    def test_connection_error_is_not_empty_success(self):
        with self.assertRaisesRegex(ValueError, 'ERROR 2003'):
            self.report(LogConn(fail=('SELECT PRIO', 'ERROR 2003 connection failed')))

    def test_verbosity_and_dragnet_coverage(self):
        conn = LogConn()
        for verbosity, hidden in ((1, 'Warning and Note'), (2, 'Note')):
            conn.verbosity = verbosity
            self.assertIn(hidden, ' '.join(self.report(conn)['notes']))
        conn.services = 'log_filter_dragnet; log_sink_json'
        notes = ' '.join(self.report(conn)['notes'])
        self.assertIn('rules decide', notes)
        self.assertNotIn('not recorded by the built-in', notes)
        conn.services, conn.verbosity = 'log_filter_internal; log_sink_internal', 3
        self.assertNotIn('not recorded by the built-in', ' '.join(self.report(conn)['notes']))

    def test_json_decoding_and_message_caps(self):
        for raw in ('{bad', '{"other":"value"}', '{"msg":4}', '["msg"]', 'plain text'):
            self.assertEqual(m.decode_data(raw), raw)
        self.assertEqual(m.decode_data(' {"msg":"hi"}'), 'hi')
        conn = LogConn()
        conn.rows = [['2026-10-19 20:28:00', 0, 'Error', 'MY-000001', 'Server', json.dumps({'msg': 'x' * 6000})]]
        report = self.report(conn)
        self.assertEqual(len(report['entries'][0]['message']), 4000)
        self.assertEqual(len(report['codes'][0]['sample']), 4000)

    def test_start_time_uses_server_read_not_report_generation_clock(self):
        with mock.patch('myflames.perf_common.now', return_value='2000-01-01T00:00:00+00:00'):
            report = self.report()
        self.assertEqual(report['taken_at'], '2000-01-01T00:00:00+00:00')
        self.assertEqual(report['server_started'], START)
        self.assertEqual(report['counts_current_run']['error'], 1)

    def test_mysql_versions_share_the_envelope(self):
        for version in ('8.4.11', '9.7.2'):
            conn = LogConn()
            conn.version = version
            report = self.report(conn)
            self.assertEqual(report['server_version'], version)
            self.assertEqual(report['schema_version'], 'error-log-1.0')

    def test_text_uses_100_columns_for_entries_and_preserves_json_controls(self):
        conn = LogConn()
        conn.rows[-1][-1] = 'a' * 500 + '\x1b[31m'
        report = self.report(conn)
        table = m.format_text(report).split('\n\n', 1)[1]
        self.assertTrue(all(len(line) <= 100 for line in table.splitlines()))
        code, out, _, _ = run_cli('error_log', ['--json'], conn)
        self.assertEqual(code, 0)
        self.assertIn('\x1b', json.loads(out)['entries'][0]['message'])
        self.assertNotIn('\x1b', out)

    def test_summary_words_count_current_run_errors_and_warnings(self):
        more = [['2026-10-19 20:27:00', 1, 'Error', 'MY-000004', 'Server', 'second error'],
                ['2026-10-19 20:27:01', 1, 'Warning', 'MY-000005', 'Server', 'w2'],
                ['2026-10-19 20:27:02', 1, 'Warning', 'MY-000005', 'Server', 'w3']]
        # The default rows hold one earlier-run Error, which the words leave out.
        for rows, words, status in ((MYSQL_ROWS, '1 error and 1 warning', 'watch'),
                                    ([], '0 errors and 0 warnings', 'ok'),
                                    (MYSQL_ROWS + more, '2 errors and 3 warnings', 'watch')):
            conn = LogConn()
            conn.rows = [list(row) for row in rows]
            with self.subTest(words=words):
                report = self.report(conn)
                self.assertEqual(report['summary'], {'status': status, 'text': '%s recorded since the server '
                                                     'started at %s.' % (words, START)})

    def test_text_lists_system_and_cuts_one_line_messages_at_100_columns(self):
        conn = LogConn()
        conn.rows[-1][-1] = 'first line\n' + 'word ' * 60
        text = m.format_text(self.report(conn))
        self.assertEqual(text.splitlines()[1], 'Retained: 1 System, 2 Error, 1 Warning, 1 Note')
        table = text.split('\n\n', 1)[1].splitlines()
        self.assertEqual(len(table), 1 + 5, 'one line per entry: message newlines are collapsed')
        self.assertEqual(len(table[1]), 100)
        self.assertTrue(table[1].endswith('word…'), table[1])
        self.assertIn('Server     first line word word', table[1])
        self.assertTrue(table[3].endswith('InnoDB     a warning more detail'), table[3])

    def test_levels_keep_canonical_order_and_reach_sql_and_cli(self):
        self.assertEqual(m.check_levels(['note', 'SYSTEM', ' error ']), ['System', 'Error', 'Note'])
        self.assertEqual(m.check_levels('Note,warning,note'), ['Warning', 'Note'])
        self.assertEqual(m.check_levels('  '), list(m.LEVELS))
        conn = LogConn()
        report = self.report(conn, levels=['note', 'system', 'Note'])
        self.assertEqual(report['levels'], ['System', 'Note'])
        self.assertEqual([row['prio'] for row in report['entries']], ['Note', 'System'])
        entries_sql = [sql for sql, _ in conn.calls if 'e.LOGGED' in sql][0]
        self.assertEqual(entries_sql.count("WHERE PRIO IN ('System', 'Note')"), 2)
        code, out, err, _ = run_cli('error_log', ['--json', '-q', '--level', 'Note,SYSTEM,note'], LogConn())
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(json.loads(out)['levels'], ['System', 'Note'])
        self.assertEqual([row['prio'] for row in json.loads(out)['entries']], ['Note', 'System'])

    def test_null_data_and_deeply_nested_json_stay_text(self):
        self.assertEqual(m.decode_data(None), '')
        deep = '{"msg":' + '[' * 100000 + ']' * 100000 + '}'
        self.assertEqual(m.decode_data(deep), deep)
        # Python 3.14 parses that depth; older versions raise RecursionError. Cover both.
        with mock.patch.object(m.json, 'loads', side_effect=RecursionError('too deep')):
            self.assertEqual(m.decode_data('{"msg":"hi"}'), '{"msg":"hi"}')
        conn = LogConn()
        conn.rows = [['2026-10-19 20:28:00', 0, 'Error', 'MY-000001', 'Server', None]]
        report = self.report(conn)
        self.assertEqual(report['entries'][0]['message'], '')
        self.assertEqual(report['codes'][0]['sample'], '')
        self.assertIn('MY-000001', m.format_text(report))

    def test_missing_settings_or_server_time_raise_before_reading_the_table(self):
        for omit, message in ((('log_error_services',), 'error log settings'),
                              (('log_error_verbosity',), 'error log settings'),
                              (('Uptime',), r'status counters \(Uptime\)')):
            conn = SparseConn(omit)
            with self.subTest(omit=omit), self.assertRaisesRegex(ValueError, message):
                m.error_log_report(conn)
            self.assertFalse(any('FROM performance_schema.error_log' in sql for sql, _ in conn.calls))
        code, out, err, _ = run_cli('error_log', ['--json'], SparseConn(('Uptime',)))
        self.assertEqual((code, out), (2, ''))
        self.assertIn('status counters (Uptime)', err)

    def test_notes_only_describe_filters_and_expiry_that_apply(self):
        conn = LogConn()
        conn.services, conn.verbosity, conn.expired = 'log_sink_internal', 1, 0
        notes = ' '.join(self.report(conn)['notes'])
        self.assertNotIn('not recorded by the built-in filter', notes)
        self.assertNotIn('rules decide', notes)
        self.assertNotIn('expired', notes)
        self.assertIn('separate reads of a changing buffer', notes)


class TestMariaDB(LogCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, 'host.err')
        self.write(MARIA_LOG)
        # Same name as the fake server's @@hostname, in another case.
        hostname = mock.patch.object(m.socket, 'gethostname', return_value='Server.Example.Test')
        hostname.start()
        self.addCleanup(hostname.stop)

    def write(self, text):
        # newline='': keep \n on Windows, so byte offsets match the text.
        with open(self.path, 'w', encoding='utf-8', newline='') as handle:
            handle.write(text)

    def conn(self, **kwargs):
        return LogConn(maria=True, path=self.path, datadir=self.temp.name, **kwargs)

    def test_file_happy_path_latest_banner_and_continuations(self):
        conn = self.conn(ps=False)
        conn.path = './host.err'
        report = self.report(conn)
        self.assertTrue(report['available'])
        self.assertEqual(report['source'], 'file')
        self.assertIsNone(report['buffer'])
        self.assertEqual(report['server_started'], '2026-10-19 20:26:12')
        self.assertEqual(report['counts'], {'system': 0, 'error': 1, 'warning': 2, 'note': 4})
        self.assertEqual(report['counts_current_run'], {'system': 0, 'error': 1, 'warning': 1, 'note': 1})
        self.assertEqual(report['file'], {'path': os.path.realpath(self.path), 'size_bytes': len(MARIA_LOG.encode('utf-8')),
                                         'read_bytes': len(MARIA_LOG.encode('utf-8')), 'truncated': False, 'banner_found': True})
        self.assertEqual(report['settings'], {'log_error': './host.err', 'datadir': self.temp.name,
                                             'log_warnings': 2, 'log_error_services': None, 'log_error_verbosity': None})
        self.assertEqual(report['entries'][1]['subsystem'], 'InnoDB')
        self.assertEqual(report['entries'][1]['message'], 'InnoDB: example failure\nstack line')
        self.assertIn('create_uring failed', report['entries'][-1]['message'])
        self.assertTrue(report['entries'][-1]['previous_run'])
        self.assertIsNone(report['entries'][0]['subsystem'])
        self.assertTrue(all(row['error_code'] is None for row in report['entries']))
        variables = [sql for sql, _ in conn.calls if 'SHOW GLOBAL VARIABLES' in sql]
        self.assertEqual(len(variables), 1, 'settings come with the preflight read')
        for name in ('log_error', 'datadir', 'hostname', 'port', 'log_warnings'):
            self.assertIn("'%s'" % name, variables[0])
        # The start is computed in UTC, then shown in the server's SYSTEM time like the log.
        self.assertTrue(any("CONVERT_TZ(UTC_TIMESTAMP() - INTERVAL" in sql and "'+00:00', 'SYSTEM')" in sql
                            for sql, _ in conn.calls))

    def assert_reason(self, conn, code):
        report = self.report(conn)
        self.assertFalse(report['available'])
        self.assertEqual(report['reason'], code)
        self.assertEqual(report['summary']['status'], 'unknown')
        self.assertIsInstance(report['reason_text'], str)
        self.assertIsInstance(report['how_to_view'], str)
        self.assertEqual(report['entries'], [])
        # Nothing parsed from a refused or unread file leaves the module.
        self.assertEqual(report['codes'], [])
        self.assertEqual(report['entries_total'], 0)
        self.assertIsNone(report['source'])
        self.assertEqual(set(report['counts'].values()), {0})
        self.assertEqual(set(report['counts_current_run'].values()), {0})
        self.assertNotIn('None', report['reason_text'])
        return report

    def test_stderr_precedes_host_checks(self):
        conn = self.conn(host='remote')
        conn.path = ''
        self.assertIn('journalctl', self.assert_reason(conn, 'log_to_stderr')['how_to_view'])

    def test_remote_and_other_host_do_not_touch_local_files(self):
        for host, expected in (('db.example.test', 'remote'), ('localhost', 'other_host')):
            conn = self.conn(host=host)
            conn.hostname = 'docker-container'
            with mock.patch.object(m, 'resolve_path', side_effect=AssertionError('local filesystem touched')):
                report = self.assert_reason(conn, expected)
            self.assertIn('tail -n 200', report['how_to_view'])

    def test_local_hosts_and_hostname_comparison(self):
        for host in ('localhost', 'LOCALHOST', '127.0.0.1', '::1', ''):
            self.assertTrue(self.report(self.conn(host=host))['available'])
        self.assertFalse(m.same_host('', 'server'))
        # The whole name must match: a tunnel to db.production.example is not db.local.
        self.assertFalse(m.same_host('db.production.example', 'db.local'))
        self.assertTrue(m.same_host('Dev-Mac.local.', 'dev-mac.local'))
        self.assertFalse(m.local_connection(None))
        self.assertFalse(m.local_connection('127.0.0.2'))

    def test_not_found_and_nonregular(self):
        conn = self.conn()
        conn.path = os.path.join(self.temp.name, 'missing.err')
        self.assert_reason(conn, 'not_found')
        conn.path = self.temp.name
        self.assert_reason(conn, 'not_found')
        if hasattr(os, 'mkfifo'):
            fifo = os.path.join(self.temp.name, 'fifo')
            os.mkfifo(fifo)
            conn.path = fifo
            self.assert_reason(conn, 'not_found')

    @unittest.skipIf(os.name == 'nt' or (hasattr(os, 'geteuid') and os.geteuid() == 0), 'Permission bits need a non-root Unix process')
    def test_not_readable_permission_bits(self):
        os.chmod(self.path, 0)
        try:
            self.assert_reason(self.conn(), 'not_readable')
        finally:
            os.chmod(self.path, 0o600)

    def test_open_permission_and_disappearance_races(self):
        for error, reason in ((PermissionError('denied'), 'not_readable'), (FileNotFoundError('gone'), 'not_found')):
            with mock.patch.object(m, '_read_tail', side_effect=error):
                self.assert_reason(self.conn(), reason)

    def test_identity_mismatch_version_port_and_incomplete_latest_banner(self):
        for change in (MARIA_LOG.replace("Version: '11.4.13-MariaDB-ubu2404'", "Version: 'wrong'"),
                       MARIA_LOG.replace('port: 3306', 'port: 3307'),
                       MARIA_LOG + '2026-10-19 20:26:20 0 [Note] mariadbd: ready for connections.\nVersion: '):
            self.write(change)
            self.assert_reason(self.conn(), 'identity_mismatch')

    def test_most_recent_banner_must_win_even_if_earlier_one_matches(self):
        self.write(MARIA_LOG.replace('port: 0', 'port: 3306').replace('port: 3306  mariadb.org binary distribution\n2026-10-19 20:26:15',
                                                                  'port: 0  mariadb.org binary distribution\n2026-10-19 20:26:15'))
        self.assert_reason(self.conn(), 'identity_mismatch')

    def test_unrecognized_file_does_not_return_raw_text(self):
        for text in ('private arbitrary file content\n', '', 'Version: \'11.4.13-MariaDB-ubu2404\' socket: \'s\' port: 3306\n'):
            self.write(text)
            report = self.assert_reason(self.conn(), 'unrecognized_format')
            self.assertNotIn('private arbitrary file', json.dumps(report))

    def test_no_banner_is_unverified_and_space_padded_hour_parses(self):
        text = 'orphan continuation\n2026-10-19  9:01:02 15 [ERROR] InnoDB: old failure\nstack\n'
        self.write(text)
        report = self.assert_reason(self.conn(), 'identity_unverified')
        self.assertFalse(report['file']['banner_found'])
        self.assertNotIn('old failure', json.dumps(report))
        entries = m.parse_mariadb(text)
        self.assertEqual(entries[0]['logged'], '2026-10-19 09:01:02')
        self.assertEqual(entries[0]['message'], 'InnoDB: old failure\nstack')

    def test_one_log_shaped_line_cannot_carry_other_file_text(self):
        # Without the server's own startup line, nothing after a log-shaped
        # line (here a key) reaches the report.
        self.write('2024-01-01 10:00:00 0 [Note] x\n-----BEGIN PRIVATE KEY-----\nsecret-material\n')
        report = self.assert_reason(self.conn(), 'identity_unverified')
        self.assertNotIn('secret-material', json.dumps(report))

    def test_banner_must_come_from_this_start(self):
        old = BANNER.replace('2026-10-19 20:26:14', '2026-10-19 19:00:00')
        self.write(old + '2026-10-19 20:27:00 5 [ERROR] after\n')
        self.assertIn('before this server started', self.assert_reason(self.conn(), 'identity_unverified')['reason_text'])
        late = BANNER.replace('2026-10-19 20:26:14', '2026-10-19 21:30:00')
        self.write(late)
        self.assertIn('more than an hour', self.assert_reason(self.conn(), 'identity_mismatch')['reason_text'])
        # Two seconds of clock granularity before the start are accepted.
        self.write(BANNER.replace('2026-10-19 20:26:14', '2026-10-19 20:26:10'))
        self.assertTrue(self.report(self.conn())['available'])

    def test_banner_with_a_version_set_at_startup(self):
        self.write(BANNER.replace("Version: '11.4.13-MariaDB-ubu2404'", "Version: '11.4.13-MariaDB-ubu2404' as 'custom-1.0'"))
        conn = self.conn()
        conn.version = 'custom-1.0-MariaDB'
        self.write(BANNER.replace("Version: '11.4.13-MariaDB-ubu2404'",
                                  "Version: '11.4.13-MariaDB-ubu2404' as 'custom-1.0-MariaDB'"))
        self.assertTrue(self.report(conn)['available'])
        conn = self.conn()
        self.assert_reason(conn, 'identity_mismatch')

    def test_banner_fields_are_bounded(self):
        huge = BANNER.replace("Version: '11.4.13-MariaDB-ubu2404'", "Version: '" + 'v' * 10000 + "'")
        self.write(huge)
        report = self.assert_reason(self.conn(), 'identity_mismatch')
        self.assertIn('incomplete', report['reason_text'])
        self.assertLess(len(report['reason_text']), 500)

    def test_crash_report_header_is_an_error_entry(self):
        self.write(BANNER + '261019 20:27:01 [ERROR] /usr/sbin/mariadbd got signal 11 ;\n'
                   'Sorry, we probably made a mistake, and this is a bug.\n')
        report = self.report(self.conn())
        crash = report['entries'][0]
        self.assertEqual((crash['logged'], crash['prio'], crash['thread_id']), ('2026-10-19 20:27:01', 'Error', None))
        self.assertTrue(crash['message'].startswith('/usr/sbin/mariadbd got signal 11 ;\nSorry'))
        self.assertEqual(report['counts_current_run']['error'], 1)

    def test_continuation_lines_are_capped_per_entry(self):
        entries = m.parse_mariadb('2026-10-19 20:27:00 1 [ERROR] trace\n' + 'frame\n' * 500)
        lines = entries[0]['message'].split('\n')
        self.assertEqual(len(lines), m.MAX_CONTINUATION + 2)
        self.assertEqual(lines[-1], '[more lines not shown]')

    def test_hint_never_offers_a_command_for_a_path_with_control_characters(self):
        conn = self.conn(host='db.example.test')
        conn.path = '/var/log/mysql/x\n; rm -rf ~'
        report = self.assert_reason(conn, 'remote')
        self.assertNotIn('tail', report['how_to_view'])

    def test_relative_log_error_without_datadir_is_not_found(self):
        conn = self.conn()
        conn.path, conn.datadir = 'host.err', ''
        with mock.patch.object(m, 'resolve_path', side_effect=AssertionError('resolved against the working directory')):
            self.assertIn('relative name', self.assert_reason(conn, 'not_found')['reason_text'])

    def test_tail_bound_drops_partial_line_and_decodes_replacement(self):
        suffix = '2026-10-19 20:26:16 8 [Warning] last event\n'.encode('utf-8')
        prefix = ('2026-10-19 20:26:15 8 [ERROR] ' + 'x' * 500 + '\n').encode('utf-8')
        with open(self.path, 'wb') as handle:
            handle.write(prefix + suffix + b'continuation \xff\n')
        tail_bytes = len(suffix) + 50
        with mock.patch.object(m, 'TAIL_BYTES', tail_bytes):
            text, info = m._read_tail(self.path)
        self.assertTrue(info['truncated'])
        self.assertEqual(info['read_bytes'], tail_bytes)
        entries = m.parse_mariadb(text)
        self.assertEqual([e['message'] for e in entries], ['last event\ncontinuation \ufffd'])

    def test_tail_cut_at_a_line_start_keeps_that_line(self):
        first = '2026-10-19 20:26:15 8 [ERROR] ' + 'x' * 100 + '\n'
        rest = '2026-10-19 20:26:16 8 [ERROR] whole error\n2026-10-19 20:26:17 8 [Note] after\n'
        self.write(first + rest)
        with mock.patch.object(m, 'TAIL_BYTES', len(rest.encode('utf-8'))):
            text, info = m._read_tail(self.path)
        self.assertTrue(info['truncated'])
        self.assertEqual([e['message'] for e in m.parse_mariadb(text)], ['whole error', 'after'])
        # One byte later the first line is partial and dropped.
        with mock.patch.object(m, 'TAIL_BYTES', len(rest.encode('utf-8')) - 1):
            text, _ = m._read_tail(self.path)
        self.assertEqual([e['message'] for e in m.parse_mariadb(text)], ['after'])

    def test_file_counts_groups_limit_and_filter(self):
        text = BANNER + ''.join('2026-10-19 20:27:%02d %d [Warning] InnoDB: group %d\n' % (i, i, i % 22)
                                for i in range(44))
        text += '2026-10-19 20:28:00 0 [ERROR] failure\n'
        self.write(text)
        report = self.report(self.conn(), levels='warning', limit=1)
        self.assertEqual(report['entries_total'], 44)
        self.assertEqual(len(report['entries']), 1)
        self.assertEqual(report['counts']['warning'], 44)
        self.assertEqual(report['summary']['status'], 'watch')
        self.assertEqual(len(report['codes']), 20)
        self.assertTrue(all(group['count'] == 2 for group in report['codes']))
        group = next(g for g in report['codes'] if g['sample'] == 'InnoDB: group 0')
        self.assertEqual(group['first_logged'], '2026-10-19 20:27:00')
        self.assertEqual(group['last_logged'], '2026-10-19 20:27:22')

    def test_file_groups_use_first_80_message_characters_and_cap_after_identity(self):
        text = MARIA_LOG + ''.join('2026-10-19 20:28:0%d 0 [Warning] %s\n' % (i, 'x' * 80 + str(i))
                                  for i in range(2))
        text += '2026-10-19 20:28:03 0 [ERROR] detail\n' + 'line\n' * 20000
        text += '2026-10-19 20:28:04 0 [ERROR] ' + 'y' * 5000 + '\n'
        self.write(text)
        report = self.report(self.conn())
        group = next(g for g in report['codes'] if g['sample'].startswith('x'))
        self.assertEqual(group['count'], 2)
        self.assertEqual(len(report['entries'][0]['message']), 4000)
        self.assertTrue(report['entries'][1]['message'].endswith('[more lines not shown]'))
        self.assertTrue(report['file']['banner_found'])

    def test_text_never_lists_system_and_keeps_entries_on_one_line(self):
        self.write(MARIA_LOG + '2026-10-19 20:26:17 9 [Warning] ' + 'long ' * 40 + '\nsecond line\n')
        report = self.report(self.conn())
        text = m.format_text(report)
        lines = text.splitlines()
        self.assertIn('(%s)' % os.path.realpath(self.path), lines[0])
        self.assertEqual(lines[1], 'Retained: 1 Error, 3 Warning, 4 Note')
        self.assertNotIn('System', '\n'.join(lines[1:]))
        table = text.split('\n\n', 1)[1].splitlines()
        self.assertEqual(len(table), 1 + report['entries_total'], 'one line per entry: continuations are joined')
        self.assertTrue(all(len(line) <= 100 for line in table))
        self.assertEqual(len(table[1]), 100)
        # TIME 19 + LEVEL 7 + CODE 4 + SUBSYSTEM 9 + 4 gaps of 2 = 47; 53 left, 52 kept plus the ellipsis.
        self.assertTrue(table[1].endswith('  ' + 'long ' * 10 + 'lo…'), table[1])
        self.assertTrue(any(line.endswith('InnoDB     InnoDB: example failure stack line') for line in table), table)
        conn = self.conn()
        conn.path = ''
        unavailable = m.format_text(self.report(conn))
        self.assertTrue(unavailable.startswith('Error log: ?, server started 2026-10-19 20:26:12\n'), unavailable)
        self.assertIn('journalctl -u mariadb', unavailable)
        self.assertNotIn('TIME', unavailable)

    def test_crlf_line_endings_parse_like_lf(self):
        reports = []
        for newline in ('\n', '\r\n'):
            with open(self.path, 'w', encoding='utf-8', newline='') as handle:
                handle.write(MARIA_LOG.replace('\n', newline))
            reports.append(self.report(self.conn()))
        lf, crlf = reports
        self.assertTrue(crlf['available'])
        self.assertTrue(crlf['file']['banner_found'])
        self.assertNotIn('\\r', json.dumps(crlf))
        for key in ('counts', 'counts_current_run', 'codes', 'entries', 'entries_total', 'summary', 'notes'):
            self.assertEqual(crlf[key], lf[key], key)

    def test_impossible_timestamps_are_not_entries(self):
        self.write(BANNER + '2026-10-19 20:26:15 5 [ERROR] real failure\n'
                   '2026-13-40 20:26:16 6 [ERROR] month 13\n'
                   '2026-02-30 20:26:16 6 [Warning] February 30\n'
                   '2026-10-19 24:00:00 6 [Warning] hour 24\n'
                   '2026-10-19 20:26:17 7 [Note] after\n')
        report = self.report(self.conn())
        self.assertEqual([(e['logged'], e['prio']) for e in report['entries']],
                         [('2026-10-19 20:26:17', 'Note'), ('2026-10-19 20:26:15', 'Error'),
                          ('2026-10-19 20:26:14', 'Note')])
        self.assertEqual(report['counts'], {'system': 0, 'error': 1, 'warning': 0, 'note': 2})
        self.assertEqual(report['entries_total'], 3)

    def test_truncated_tail_drops_continuations_before_the_first_entry(self):
        head = '2026-10-19 20:26:15 5 [ERROR] InnoDB: first failure\nframe one cut-secret\n'.encode('utf-8')
        orphan = 'frame two orphan-secret\n'.encode('utf-8')
        suffix = '2026-10-19 20:26:16 8 [Warning] last event\n'.encode('utf-8')
        with open(self.path, 'wb') as handle:
            handle.write(head + orphan + suffix)
        with mock.patch.object(m, 'TAIL_BYTES', len(orphan) + len(suffix) + 5):
            text, info = m._read_tail(self.path)
        self.assertTrue(info['truncated'])
        entries = m.parse_mariadb(text)
        self.assertEqual([e['message'] for e in entries], ['last event'])
        for secret in ('cut-secret', 'orphan-secret', 'first failure'):
            self.assertNotIn(secret, json.dumps(entries))
        # A tail inside one unterminated line holds no complete line at all.
        with open(self.path, 'wb') as handle:
            handle.write(suffix + b'unterminated-secret ' * 30)
        with mock.patch.object(m, 'TAIL_BYTES', 100):
            report = self.assert_reason(self.conn(), 'unrecognized_format')
        self.assertTrue(report['file']['truncated'])
        self.assertNotIn('unterminated-secret', json.dumps(report))

    def test_identity_mismatch_names_the_file_without_log_content(self):
        path = os.path.realpath(self.path)
        incomplete = MARIA_LOG + '2026-10-19 20:26:20 0 [Note] mariadbd: ready for connections.\nVersion: \n'
        for text, phrases in ((incomplete, ('is incomplete', 'cannot be matched')),
                              (MARIA_LOG.replace('port: 3306', 'port: 3307'),
                               ('version 11.4.13-MariaDB-ubu2404 and port 3307', "another server's log"))):
            with self.subTest(phrases=phrases):
                self.write(text)
                report = self.assert_reason(self.conn(), 'identity_mismatch')
                self.assertIn(path, report['reason_text'])
                for phrase in phrases:
                    self.assertIn(phrase, report['reason_text'])
                dumped = json.dumps(report)
                for message in ('example failure', 'create_uring', 'another warning', 'stack line'):
                    self.assertNotIn(message, dumped)

    def test_not_found_and_not_readable_reasons_name_the_resolved_path(self):
        conn = self.conn()
        for path in (os.path.join(self.temp.name, 'missing.err'), self.temp.name):
            conn.path = path
            self.assertIn(os.path.realpath(path), self.assert_reason(conn, 'not_found')['reason_text'])
        for error, code in ((PermissionError('denied'), 'not_readable'), (FileNotFoundError('gone'), 'not_found')):
            with mock.patch.object(m, '_read_tail', side_effect=error):
                report = self.assert_reason(self.conn(), code)
            self.assertIn(os.path.realpath(self.path), report['reason_text'])

    def test_remote_hint_resolves_against_datadir_and_quotes_for_the_shell(self):
        conn = self.conn(host='db.example.test')
        conn.datadir, conn.path = '/srv/my data/', "../logs/-x'; rm -rf ~.err"
        hint = self.assert_reason(conn, 'remote')['how_to_view']
        prefix = 'Read it on the server host: '
        self.assertTrue(hint.startswith(prefix), hint)
        self.assertEqual(shlex.split(hint[len(prefix):]), ['tail', '-n', '200', '--', "/srv/logs/-x'; rm -rf ~.err"])

    def test_remote_hint_keeps_a_posix_server_path_on_windows(self):
        import ntpath
        with mock.patch.object(m.os, 'path', ntpath):
            hint = m._how_to_view('mariadb.err', '/var/lib/mysql/')
        self.assertEqual(shlex.split(hint.split(': ', 1)[1]), ['tail', '-n', '200', '--', '/var/lib/mysql/mariadb.err'])

    def test_same_second_entries_keep_the_later_line_first(self):
        report = self.report(self.conn())
        logged = [entry['logged'] for entry in report['entries']]
        self.assertEqual(logged, sorted(logged, reverse=True))
        same_second = [entry['message'].split('\n')[0] for entry in report['entries']
                       if entry['logged'] == '2026-10-19 20:26:11']
        self.assertEqual(same_second, ['mariadbd: ready for connections.', 'InnoDB: Compressed tables use zlib 1.3',
                                       'Starting MariaDB 11.4.13-MariaDB-ubu2404 source revision ... as process 100'])

    def test_symlinked_log_is_read_through_its_target(self):
        link, dangling = os.path.join(self.temp.name, 'link.err'), os.path.join(self.temp.name, 'dangling.err')
        try:
            os.symlink(self.path, link)
            os.symlink(os.path.join(self.temp.name, 'gone.err'), dangling)
        except (AttributeError, NotImplementedError, OSError):
            self.skipTest('This platform or account cannot create symbolic links.')
        conn = self.conn()
        conn.path = 'link.err'
        report = self.report(conn)
        self.assertTrue(report['available'])
        self.assertEqual(report['file']['path'], os.path.realpath(self.path))
        conn.path = dangling
        self.assert_reason(conn, 'not_found')

    def test_missing_settings_or_server_time_raise_before_reading_a_file(self):
        for omit, message in ((('log_error',), 'error log settings'), (('port',), 'error log settings'),
                              (('Uptime',), 'server start time')):
            conn = SparseConn(omit, maria=True, path=self.path, datadir=self.temp.name)
            with self.subTest(omit=omit), self.assertRaisesRegex(ValueError, message), \
                    mock.patch.object(m, 'resolve_path', side_effect=AssertionError('local filesystem touched')):
                m.error_log_report(conn)

    def test_absent_log_warnings_is_not_described(self):
        report = self.report(SparseConn(('log_warnings',), maria=True, path=self.path, datadir=self.temp.name))
        self.assertTrue(report['available'])
        self.assertIsNone(report['settings']['log_warnings'])
        self.assertNotIn('log_warnings', ' '.join(report['notes']))


class TestLiveReplay(unittest.TestCase):
    """Recordings from throwaway Docker servers (2026-10-19): MySQL 8.4.11 with a
    log file, a restart and a failing replica (previous-run rows, current-run
    Errors); MySQL 9.7.2 with log_sink_json; the same MySQL 8.4 as a user
    without SELECT on the table; MariaDB 11.4.13 with log_error in a folder
    shared with this computer and a failing named replica connection, its log
    text kept in ``file_text``; MariaDB 11.4.13 with log_error empty."""

    def replay(self, server):
        data = fixture('error-log', server)
        conn = ReplayConn(data['calls'])
        conn.host = '127.0.0.1'
        if 'file_text' not in data:
            return conn, data, m.error_log_report(conn, **data['args'])
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'mariadb.err')
            with open(path, 'w', encoding='utf-8', newline='') as handle:
                handle.write(data['file_text'])
            with mock.patch.object(m, 'resolve_path', return_value=path), \
                    mock.patch.object(m.socket, 'gethostname', return_value=data['local_hostname']):
                report = m.error_log_report(conn, **data['args'])
        report['file']['path'] = data['report']['file']['path']
        return conn, data, report

    def test_every_recording_replays_to_the_recorded_report(self):
        for server in ('mysql-8.4', 'mysql-9.7', 'mysql-8.4-denied', 'mariadb-11.4', 'mariadb-11.4-stderr'):
            with self.subTest(server=server):
                conn, data, report = self.replay(server)
                assert_read_only(self, conn, m.ERROR_LOG_TAG)
                report.pop('taken_at', None)
                expected = dict(data['report'])
                expected.pop('taken_at', None)
                self.assertEqual(report, expected)
                json.dumps(report, allow_nan=False)

    def test_mysql_restart_keeps_previous_run_rows_apart(self):
        report = self.replay('mysql-8.4')[2]
        self.assertTrue(report['available'])
        self.assertEqual(report['source'], 'performance_schema.error_log')
        self.assertEqual(report['settings']['log_error_services'], 'log_filter_internal; log_sink_internal')
        self.assertGreater(report['counts']['error'], report['counts_current_run']['error'])
        self.assertGreaterEqual(report['counts_current_run']['error'], 1)
        self.assertTrue(any(e['previous_run'] for e in report['entries']))
        self.assertTrue(all(not e['previous_run'] for e in report['entries'] if e['logged'] >= report['server_started']))
        self.assertEqual(report['summary']['status'], 'watch')
        self.assertTrue(any(c['error_code'] == 'MY-010584' and c['subsystem'] == 'Repl' for c in report['codes']))

    def test_mysql_json_sink_messages_are_decoded(self):
        report = self.replay('mysql-9.7')[2]
        self.assertEqual(report['settings']['log_error_services'], 'log_filter_internal; log_sink_json')
        raw = [c for c in fixture('error-log', 'mysql-9.7')['calls'] if 'e.LOGGED' in c[0]][0][1]
        self.assertIn('"msg" : ', raw)
        for entry in report['entries']:
            self.assertFalse(entry['message'].lstrip().startswith('{'), entry['message'])

    def test_mysql_without_select_on_the_table_is_denied(self):
        report = self.replay('mysql-8.4-denied')[2]
        self.assertFalse(report['available'])
        self.assertEqual(report['reason'], 'denied')

    def test_mariadb_file_matches_the_latest_banner_and_marks_the_entrypoint_run(self):
        report = self.replay('mariadb-11.4')[2]
        self.assertTrue(report['available'])
        self.assertEqual(report['source'], 'file')
        self.assertTrue(report['file']['banner_found'])
        self.assertEqual(report['counts']['system'], 0)
        self.assertGreaterEqual(report['counts_current_run']['error'], 1)
        errors = [e for e in report['entries'] if e['prio'] == 'Error']
        self.assertTrue(any(e['message'].startswith("Master 'src1': ") for e in errors))
        # The Docker entrypoint's temporary server (port 0) ran before this start.
        self.assertTrue(any(e['previous_run'] for e in report['entries']))

    def test_mariadb_without_a_log_file_says_where_to_look(self):
        report = self.replay('mariadb-11.4-stderr')[2]
        self.assertEqual(report['reason'], 'log_to_stderr')
        self.assertEqual(report['settings']['log_error'], '')
        self.assertIn('journalctl', report['how_to_view'])


class TestCLI(LogCase):
    def test_unavailable_json_retains_documented_envelope(self):
        for fail_on in ([], ['--fail-on', 'watch'], ['--fail-on', 'problem']):
            code, out, err, _ = run_cli('error_log', ['--json'] + fail_on, LogConn(maria=True, path=''))
            self.assertEqual(code, 2)
            body = json.loads(out)
            self.assertEqual(body['schema_version'], 'error-log-1.0')
            self.assertFalse(body['available'])
            self.assertTrue(body['reason'])
            self.assertTrue(body['reason_text'])
            self.assertIn('journalctl', body['how_to_view'])
            self.assertIn(body['reason_text'], err)

    def test_success_json_main_dispatch_and_output_file(self):
        code, out, err = run_main(['myflames', 'error-log', '--json'], LogConn())
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(json.loads(out)['schema_version'], 'error-log-1.0')
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'error-log.json')
            code, out, err, factory = run_cli('error_log', ['--json', '-o', path], LogConn())
            self.assertEqual((code, out), (0, ''))
            with open(path, encoding='utf-8') as handle:
                self.assertEqual(json.load(handle)['schema_version'], 'error-log-1.0')
            self.assertEqual(factory.options['query_timeout'], 30)

    def test_fail_on_watch_one_problem_zero_and_quiet(self):
        for level, expected in (('watch', 1), ('problem', 0)):
            code, out, err, _ = run_cli('error_log', ['--fail-on', level, '--json', '-q', '--level', 'note'], LogConn())
            self.assertEqual((code, err), (expected, ''))
            self.assertEqual(json.loads(out)['summary']['status'], 'watch')

    def test_invalid_input_returns_two_before_connecting(self):
        for args in (['--level', 'fatal'], ['--level', 'error;DROP'], ['--limit', '0'], ['--limit', '1001'],
                     ['--limit', '1.5'], ['--fail-on', 'error']):
            conn = LogConn()
            code, out, err, _ = run_cli('error_log', args, conn)
            self.assertEqual((code, out), (2, ''))
            self.assertTrue(err)
            self.assertEqual(conn.calls, [])

    def test_unavailable_json_is_two_even_with_fail_on_and_writes_envelope(self):
        for level in ('watch', 'problem'):
            with tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, 'existing.json')
                for exists in (False, True):
                    if exists:
                        with open(path, 'w') as handle:
                            handle.write('preserve me')
                    conn = LogConn(maria=True, path='')
                    code, out, err, _ = run_cli('error_log', ['--json', '-q', '-o', path, '--fail-on', level], conn)
                    self.assertEqual((code, out), (2, ''))
                    self.assertIn('standard error', err)
                    self.assertIn('journalctl -u mariadb', err)
                    if exists:
                        with open(path) as handle:
                            body = json.load(handle)
                            self.assertFalse(body['available'])
                    else:
                        with open(path) as handle:
                            self.assertFalse(json.load(handle)['available'])

    def test_connection_failure_is_two_and_redacts_password(self):
        conn = LogConn(fail=('SHOW GLOBAL', 'ERROR 2003 failed secret'))
        code, out, err, _ = run_cli('error_log', ['--json', '-q', '-psecret'], conn)
        self.assertEqual((code, out), (2, ''))
        self.assertIn('[redacted]', err)
        self.assertNotIn('secret', err)

    def test_text_payload_and_notes_are_separate(self):
        code, out, err, _ = run_cli('error_log', [], LogConn())
        self.assertEqual(code, 0)
        self.assertIn('TIME', out)
        self.assertIn('current error', out)
        self.assertIn('note: ', err)
        self.assertNotIn('note: ', out)

    def test_help_and_python37_syntax(self):
        code, out, err = run_main(['myflames', 'error-log', '--help'])
        self.assertEqual((code, err), (0, ''))
        for flag in ('--profile', '--json', '--limit', '--level', '--fail-on', '--quiet', '--output'):
            self.assertIn(flag, out)
        self.assertIn("never reports 'problem'", out)
        for path in (Path(m.__file__), Path(__file__)):
            ast.parse(path.read_text(encoding="utf-8"), **({'feature_version': 7} if sys.version_info >= (3, 8) else {}))


class TestAPI(LogCase):
    def call(self, payload, conn=None):
        workspace = mock.Mock()
        workspace.store.connection_with_saved_password.side_effect = lambda c: dict(c, password='saved-secret')
        self.factory = FakeFactory(conn or LogConn())
        with mock.patch('myflames.connector.MySQLConnection', self.factory):
            return ui.api_response('/api/live/error-log', dict({'connection': {'host': '127.0.0.1', 'port': 3306,
                                                                            'user': 'root', 'profile_id': 'saved'}}, **payload), workspace)

    def test_allowlist_dispatch_saved_credentials_and_timeout(self):
        report = self.call({'limit': 1, 'levels': ['warning']})
        self.assertEqual(report['schema_version'], 'error-log-1.0')
        self.assertEqual([entry['prio'] for entry in report['entries']], ['Warning'])
        self.assertEqual(self.factory.options['query_timeout'], 30)
        self.assertEqual(self.factory.options['password'], 'saved-secret')
        self.assertIn('/api/live/error-log', ui._SERVER_VIEW_PATHS)
        self.assertIn('/api/live/error-log', ui._CONNECTION_PATHS)
        self.assertNotIn('/api/live/error-log', ui._WRITE_PATHS)
        self.assertIn('"/api/live/error-log"', inspect.getsource(ui.WorkspaceHandler.do_POST))
        with self.assertRaisesRegex(ValueError, 'requires a running workspace'):
            ui.api_response('/api/live/error-log', {})

    def test_levels_payload_reaches_the_report(self):
        everything = list(m.LEVELS)
        for payload, levels in (({}, everything), ({'levels': None}, everything), ({'levels': []}, everything),
                                ({'levels': ''}, everything), ({'levels': 'note,ERROR'}, ['Error', 'Note']),
                                ({'levels': ['Note', 'note', 'system']}, ['System', 'Note'])):
            with self.subTest(payload=payload):
                report = self.call(payload)
                self.assertEqual(report['levels'], levels)
                self.assertEqual(report['limit'], m.DEFAULT_LIMIT)
                self.assertEqual(report['entries_total'], sum(row[2] in levels for row in MYSQL_ROWS))
                self.assertEqual(sorted(set(entry['prio'] for entry in report['entries'])), sorted(levels))

    def test_unavailable_is_structured_and_bad_filters_raise(self):
        self.assertEqual(self.call({}, LogConn(ps=False))['reason'], 'performance_schema_off')
        for payload in ({'limit': 1001}, {'limit': True}, {'levels': ['fatal']}):
            with self.assertRaises(ValueError):
                self.call(payload)

    def test_post_handler_returns_400_for_invalid_filters_without_network(self):
        handler = object.__new__(ui.WorkspaceHandler)
        handler.path = '/api/live/error-log'
        handler.headers = email.message.Message()
        handler.headers['Content-Type'] = 'application/json'
        handler.headers['X-Myflames-Token'] = 'token'
        payload = json.dumps({'connection': {'host': 'localhost'}, 'limit': 0}).encode('utf-8')
        handler.headers['Content-Length'] = str(len(payload))
        handler.server = mock.Mock(token='token')
        handler._local_request = mock.Mock(return_value=True)
        handler._authenticated = mock.Mock(return_value=True)
        handler._read_body = mock.Mock(return_value=payload)
        handler._send = mock.Mock()
        with mock.patch('myflames.connector.MySQLConnection', FakeFactory(LogConn())):
            handler.do_POST()
        self.assertEqual(handler._send.call_args[0][0], 400)
        self.assertIn('limit', handler._send.call_args[0][1]['error'])


if __name__ == '__main__':
    unittest.main()
