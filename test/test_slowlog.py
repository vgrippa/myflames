"""Slow query logs (myflames/slowlog.py, slow_queries.py, slowlog_api.py,
slowlog_cli.py): every header form, multi-line statements, malformed
entries, time zones and ranges, fingerprints, aggregation, the
mysql.slow_log table source, the workspace endpoints and the CLI.

The three logs in test/fixtures/slowlog were written by throwaway MySQL
5.7.44, MySQL 8.4.13 and MariaDB 11.4.13 containers with log_output=FILE,TABLE
and long_query_time=0; table-expected.json holds what mysql.slow_log recorded
for the same statements, so the parser is checked against the server's own
record (entries, total query time, rows, statements, users and times).
"""
import collections
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, '..')
sys.path.insert(0, ROOT)

from myflames import slowlog, slow_queries, slowlog_api, ui  # noqa: E402
from myflames.connector import ConnectorError  # noqa: E402
from test_chart_data import validate  # noqa: E402

FIXTURES = os.path.join(HERE, 'fixtures', 'slowlog')
LOGS = ('mysql-5.7.44-slow.log', 'mysql-8.4.13-slow.log', 'mariadb-11.4.13-slow.log')


def _schema(name):
    with open(os.path.join(ROOT, 'docs', 'schemas', name), encoding='utf-8') as handle:
        return json.load(handle)


REPORT_SCHEMA = _schema('slow-log-1.0.json')
PROGRESS_SCHEMA = _schema('slow-log-progress-1.0.json')


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding='utf-8') as handle:
        return handle.read()


def parse(text, **options):
    options.setdefault('limit', 1000)
    report = slowlog.parse_text(text, **options)
    validate(report, REPORT_SCHEMA)
    return report


def by_text(report):
    return {(s['schema'], s['digest_text']): s for s in report['statements']}


MYSQL_57 = """mysqld, Version: 5.7.44-log (MySQL Community Server (GPL)). started with:
Tcp port: 3306  Unix socket: /var/run/mysqld/mysqld.sock
Time                 Id Command    Argument
# Time: 2026-10-31T10:00:00.250000Z
# User@Host: app[app] @ web1 [10.0.0.5]  Id:    12
# Query_time: 0.500000  Lock_time: 0.000100 Rows_sent: 1  Rows_examined: 1000
use shop;
SET timestamp=1793440800;
SELECT *
  FROM orders
 WHERE id = 42;
# Time: 2026-10-31T10:00:05.000000Z
# User@Host: app[app] @ web1 [10.0.0.5]  Id:    12
# Query_time: 1.500000  Lock_time: 0.000200 Rows_sent: 1  Rows_examined: 3000
SET timestamp=1793440805;
SELECT * FROM orders WHERE id = 7;
# Time: 2026-10-31T10:30:00.000000Z
# User@Host: report[report] @  [10.0.0.9]  Id:    13
# Query_time: 0.000050  Lock_time: 0.000000 Rows_sent: 0  Rows_examined: 0
SET timestamp=1793442600;
# administrator command: Quit;
"""

MARIADB = """/usr/sbin/mariadbd, Version: 11.4.13-MariaDB-log (mariadb.org binary distribution). started with:
Tcp port: 3306  Unix socket: /run/mysqld/mysqld.sock
Time\t\t    Id Command\tArgument
# User@Host: app[app] @  [10.0.0.5]
# Thread_id: 7  Schema: shop  QC_hit: No
# Query_time: 0.100000  Lock_time: 0.000010  Rows_sent: 0  Rows_examined: 0
# Rows_affected: 1  Bytes_sent: 11
SET timestamp=1793458800;
INSERT INTO t VALUES (1, 'a');
# Time: 261031  9:00:01
# User@Host: app[app] @  [10.0.0.5]
# Thread_id: 7  Schema: shop  QC_hit: No
# Query_time: 0.200000  Lock_time: 0.000010  Rows_sent: 5  Rows_examined: 500
# Rows_affected: 0  Bytes_sent: 120
# Tmp_tables: 1  Tmp_disk_tables: 1  Tmp_table_sizes: 4096
# Full_scan: Yes  Full_join: No  Tmp_table: Yes  Tmp_table_on_disk: Yes
# Filesort: Yes  Filesort_on_disk: No  Merge_passes: 2  Priority_queue: No
#
# explain: id\tselect_type\ttable\ttype
# explain: 1\tSIMPLE\tt\tALL
#
use `shop`;
SET timestamp=1793458801;
SELECT b, COUNT(*) FROM t GROUP BY b ORDER BY 2 DESC;
# User@Host: app[app] @  [10.0.0.5]
# Thread_id: 7  Schema: shop  QC_hit: No
# Query_time: 0.300000  Lock_time: 0.000010  Rows_sent: 5  Rows_examined: 500
# Rows_affected: 0  Bytes_sent: 120
SET timestamp=1793458801;
SELECT b, COUNT(*) FROM t GROUP BY b ORDER BY 2 DESC;
# Time: 261031 10:15:00
# User@Host: root[root] @ localhost []
# Thread_id: 8  Schema:   QC_hit: No
# Query_time: 0.000100  Lock_time: 0.000000  Rows_sent: 1  Rows_examined: 0
# Rows_affected: 0  Bytes_sent: 60
SET timestamp=1793463300;
SELECT 1;
"""

PERCONA = """/usr/sbin/mysqld, Version: 8.0.36-28 (Percona Server (GPL), Release 28, Revision 47601f19). started with:
Tcp port: 3306  Unix socket: /var/lib/mysql/mysql.sock
Time                 Id Command    Argument
# Time: 2026-10-31T10:00:00.000100Z
# User@Host: app[app] @ localhost []  Id:     8
# Schema: shop  Last_errno: 1062  Killed: 0
# Query_time: 0.010000  Lock_time: 0.000010  Rows_sent: 0  Rows_examined: 0  Rows_affected: 0  Bytes_sent: 56
# Tmp_tables: 0  Tmp_disk_tables: 0  Tmp_table_sizes: 0
# InnoDB_trx_id: 0
# QC_Hit: No  Full_scan: No  Full_join: Yes  Tmp_table: No  Tmp_table_on_disk: No
# Filesort: No  Filesort_on_disk: No  Merge_passes: 0
#   InnoDB_IO_r_ops: 0  InnoDB_IO_r_bytes: 0  InnoDB_IO_r_wait: 0.000000
#   InnoDB_rec_lock_wait: 0.000000  InnoDB_queue_wait: 0.000000
#   InnoDB_pages_distinct: 1
# Log_slow_rate_type: query  Log_slow_rate_limit: 10
SET timestamp=1793440800;
INSERT INTO orders (id) VALUES (1);
"""


class TestServerFixtures(unittest.TestCase):
    """The parser's totals are the server's: every fixture against the rows
    mysql.slow_log recorded for the same statements."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(FIXTURES, 'table-expected.json'), encoding='utf-8') as handle:
            cls.expected = json.load(handle)['logs']

    def test_entries_times_rows_and_statements_match_the_table(self):
        for name in LOGS:
            with self.subTest(log=name):
                expected = self.expected[name]
                report = parse(fixture(name))
                statements = report['statements']
                self.assertEqual(report['parse']['malformed'], 0)
                self.assertEqual(report['parse']['entries'], expected['entries'])
                self.assertEqual(report['totals']['executions'], expected['entries'])
                self.assertEqual(round(report['totals']['total_ms'] * 1000), expected['query_us'])
                self.assertEqual(round(sum(s['lock_time_ms'] for s in statements) * 1000), expected['lock_us'])
                self.assertEqual(sum(s['rows_sent'] for s in statements), expected['rows_sent'])
                self.assertEqual(sum(s['rows_examined'] for s in statements), expected['rows_examined'])
                self.assertEqual({u['name']: u['count'] for u in report['users']}, expected['users'])
                admin = {'Quit', 'Init DB', 'Ping'}
                want = collections.Counter(
                    ('administrator command: ' + text.strip()) if text.strip() in admin else
                    slowlog.fingerprint(text) for _, text, _ in expected['statements'])
                got = collections.Counter()
                for s in statements:
                    got[s['digest_text']] += s['count']
                self.assertEqual(got, want)

    def test_times_are_the_tables_digits_never_moved(self):
        for name in LOGS:
            with self.subTest(log=name):
                expected = self.expected[name]
                report = parse(fixture(name))
                if name.startswith('mariadb'):
                    # MariaDB's # Time has whole seconds; the table keeps microseconds.
                    self.assertEqual(report['span']['first'], expected['first'][:19])
                    self.assertEqual(report['span']['last'], expected['last'][:19])
                else:
                    self.assertEqual(report['span']['first'], expected['first'])
                    self.assertEqual(report['span']['last'], expected['last'])

    def test_engine_version_and_clock_per_server(self):
        cases = {
            'mysql-5.7.44-slow.log': ('mysql', '5.7.44', 'utc', 'UTC', 'end'),
            'mysql-8.4.13-slow.log': ('mysql', '8.4.13', 'offset', 'UTC-03:00', 'end'),
            'mariadb-11.4.13-slow.log': ('mariadb', '11.4.13', 'local', 'Server local time (UTC+05:30)', 'start'),
        }
        for name, (engine, version, kind, label, event) in cases.items():
            with self.subTest(log=name):
                report = parse(fixture(name))
                self.assertEqual((report['engine'], report['server_version']), (engine, version))
                self.assertEqual((report['clock']['kind'], report['clock']['label']), (kind, label))
                self.assertEqual(report['time_meaning']['event'], event)
                self.assertEqual(report['anchor'], report['span']['last'])

    def test_mariadb_entries_before_the_first_time_line_get_their_second(self):
        """mariadb-11.4.13-before-time-slow.log is the start of a log MariaDB
        reopened inside a second it had already written: three entries without
        a # Time line, then the first one. SET timestamp at the offset the
        later entry shows gives each the second mysql.slow_log recorded for it
        (18:18:09.709662, 18:18:09.783892, 18:18:09.868872)."""
        report = parse(fixture('mariadb-11.4.13-before-time-slow.log'))
        self.assertEqual((report['parse']['inferred_times'], report['parse']['no_time']), (3, 0))
        self.assertEqual(report['clock']['label'], 'Server local time (UTC+05:30)')
        self.assertTrue(any('SET timestamp at UTC+05:30' in note for note in report['notes']))
        seen = {s['digest_text']: s['first_seen'] for s in report['statements']}
        self.assertEqual(seen['SET GLOBAL slow_query_log = ON'], '2026-10-31 18:18:09')
        self.assertEqual(seen['SET autocommit = ?'], '2026-10-31 18:18:09')
        self.assertEqual(report['span'], {'first': '2026-10-31 18:18:09', 'last': '2026-10-31 18:18:12'})

    def test_mariadb_plan_lines_fill_scans_tmp_tables_and_filesorts(self):
        report = parse(fixture('mariadb-11.4.13-slow.log'))
        group = by_text(report)[('crm', 'SELECT city, COUNT(*) FROM customers GROUP BY city ORDER BY COUNT(*) DESC')]
        self.assertEqual(group['count'], 2)
        self.assertEqual(group['select_scan'], 2)
        self.assertEqual(group['tmp_tables'], 2)
        self.assertEqual(group['filesort'], 2)
        self.assertIsNone(group['errors'], 'MariaDB writes no Errno')
        self.assertTrue(any('Full_scan line only for statements' in note for note in report['notes']))

    def test_mysql_slow_extra_fields(self):
        report = parse(fixture('mysql-8.4.13-slow.log'))
        group = by_text(report)[('crm', 'SELECT city, COUNT(*) FROM customers GROUP BY city ORDER BY COUNT(*) DESC')]
        self.assertEqual(group['tmp_tables'], 2)
        self.assertEqual(group['errors'], 0)
        self.assertIsNone(group['select_scan'], 'MySQL logs no Full_scan')
        old = parse(fixture('mysql-5.7.44-slow.log'))
        self.assertIsNone(by_text(old)[('crm', 'SELECT city, COUNT(*) FROM customers GROUP BY city ORDER BY COUNT(*) DESC')]
                          ['tmp_tables'], '5.7 has no log_slow_extra')

    def test_multi_line_statement_and_a_newline_inside_a_string(self):
        for name in LOGS:
            with self.subTest(log=name):
                report = parse(fixture(name))
                texts = [s['sample_text'] for s in report['statements']]
                # The 8.4 client keeps comments; the 5.7 and MariaDB clients strip them.
                self.assertTrue(any('SELECT o.status,\n       COUNT(*) AS n,\n' in t for t in texts))
                self.assertIn("SELECT note FROM orders WHERE note = 'gift; wrap\nplease' LIMIT 1", texts)


class TestHeaderForms(unittest.TestCase):
    def test_mysql_entries_use_lines_and_administrator_commands(self):
        report = parse(MYSQL_57)
        self.assertEqual(report['engine'], 'mysql')
        self.assertEqual(report['parse']['entries'], 3)
        groups = by_text(report)
        select = groups[('shop', 'SELECT * FROM orders WHERE id = ?')]
        self.assertEqual(select['count'], 2)
        self.assertEqual(select['total_ms'], 2000.0)
        self.assertEqual(select['min_ms'], 500.0)
        self.assertEqual(select['lock_time_ms'], 0.3)
        self.assertEqual(select['sample_text'], 'SELECT * FROM orders WHERE id = 7')
        self.assertEqual(select['sample']['user'], 'app')
        self.assertEqual(select['sample']['host'], 'web1')
        self.assertEqual(select['sample']['thread_id'], 12)
        self.assertEqual(select['sample']['line'], 12)
        self.assertEqual((select['first_seen'], select['last_seen']),
                         ('2026-10-31 10:00:00.250000', '2026-10-31 10:00:05.000000'))
        quit_ = groups[('shop', 'administrator command: Quit')]
        self.assertTrue(quit_['admin'])
        self.assertTrue(quit_['capture_problem'])
        self.assertEqual(report['parse']['admin_commands'], 1)
        # The report user has no host name: its address stands in.
        self.assertEqual(quit_['hosts'], [{'name': '10.0.0.9', 'count': 1}])
        self.assertTrue(any('use db only when the database changes' in note for note in report['notes']))

    def test_mariadb_thread_schema_and_carried_time(self):
        report = parse(MARIADB)
        self.assertEqual((report['engine'], report['server_version'], report['flavor']), ('mariadb', '11.4.13', 'mariadb'))
        groups = by_text(report)
        group = groups[('shop', 'SELECT b, COUNT(*) FROM t GROUP BY b ORDER BY ? DESC')]
        self.assertEqual(group['count'], 2)
        # The second entry has no # Time: it belongs to the second written before it.
        self.assertEqual((group['first_seen'], group['last_seen']), ('2026-10-31 09:00:01', '2026-10-31 09:00:01'))
        self.assertEqual(group['select_scan'], 1, 'absence of the plan line means No once the log has one')
        self.assertEqual(group['select_full_join'], 0)
        self.assertEqual(group['tmp_disk_tables'], 1)
        self.assertEqual(group['sort_merge_passes'], 2)
        self.assertEqual(group['rows_affected'], 0)
        # Schema: empty means no database, not the previous one.
        self.assertIn((None, 'SELECT ?'), groups)
        # The first entry precedes any # Time: SET timestamp at the offset the
        # second entry shows (09:00:01 local = 1793458801 UTC) gives 09:00:00.
        insert = groups[('shop', 'INSERT INTO t VALUES (...)')]
        self.assertEqual(insert['first_seen'], '2026-10-31 09:00:00')
        self.assertEqual(report['parse']['inferred_times'], 1)
        self.assertEqual(report['clock']['label'], 'Server local time (UTC-06:00)')

    def test_percona_extended_lines(self):
        report = parse(PERCONA)
        self.assertEqual((report['engine'], report['flavor'], report['server_product']),
                         ('mysql', 'percona', 'Percona Server'))
        group = report['statements'][0]
        self.assertEqual(group['schema'], 'shop')
        self.assertEqual(group['errors'], 1)
        self.assertEqual(group['killed'], 0)
        self.assertEqual(group['select_full_join'], 1)
        self.assertEqual(group['select_scan'], 0)
        self.assertEqual(group['bytes_sent'], 56)

    def test_short_format_without_time_or_user_lines(self):
        text = ("# Query_time: 0.100000  Lock_time: 0.000000 Rows_sent: 1  Rows_examined: 1\n"
                "SET timestamp=1793440800;\nSELECT 1;\n"
                "# Query_time: 0.200000  Lock_time: 0.000000 Rows_sent: 1  Rows_examined: 1\n"
                "SET timestamp=1793440801;\nSELECT 2;\n")
        report = parse(text)
        self.assertEqual(report['statements'][0]['count'], 2)
        self.assertEqual(report['parse']['no_time'], 2)
        self.assertEqual(report['clock']['kind'], 'none')
        ranged = parse(text, range_from='2026-10-31 00:00')
        self.assertEqual(ranged['totals']['executions'], 0)
        self.assertTrue(any('no time and are left out of the range' in note for note in ranged['notes']))

    def test_server_header_in_the_middle_ends_the_entry_before_it(self):
        text = MYSQL_57.replace('# administrator command: Quit;\n', '# administrator command: Quit;\n' +
                                MYSQL_57.split('# Time:')[0])
        report = parse(text)
        self.assertEqual(report['parse']['entries'], 3)
        self.assertEqual(report['parse']['malformed'], 0)

    def test_hash_comment_lines_inside_a_statement_stay_in_it(self):
        text = ("# Time: 2026-10-31T10:00:00.000000Z\n# User@Host: a[a] @ h []  Id: 1\n"
                "# Query_time: 0.1  Lock_time: 0.0 Rows_sent: 0  Rows_examined: 0\nSET timestamp=1;\n"
                "SELECT 1\n# a comment line\nFROM dual;\n")
        report = parse(text)
        self.assertEqual(report['statements'][0]['sample_text'], 'SELECT 1\n# a comment line\nFROM dual')
        self.assertEqual(report['statements'][0]['digest_text'], 'SELECT ? FROM DUAL')


class TestMalformed(unittest.TestCase):
    def test_problems_are_counted_and_listed_by_line(self):
        text = ("garbage before the log\nmore garbage\n"
                "# Time: 2026-10-31T10:00:00.000000Z\n# User@Host: a[a] @ h []  Id: 1\n"
                "# Query_time: soon  Lock_time: 0.0 Rows_sent: 0  Rows_examined: 0\nSET timestamp=1;\nSELECT 1;\n"
                "# Time: 2026-10-31T10:00:01.000000Z\n# User@Host: a[a] @ h []  Id: 1\nSET timestamp=1;\nSELECT 2;\n"
                "# Time: 2026-13-45T10:00:01.000000Z\n# User@Host: a[a] @ h []  Id: 1\n"
                "# Query_time: 0.1  Lock_time: 0.0 Rows_sent: 0  Rows_examined: 0\nSET timestamp=1;\nSELECT 3;\n"
                "# Time: 2026-10-31T10:00:02.000000Z\n# User@Host: a[a] @ h []  Id: 1\n"
                "# Query_time: 0.1  Lock_time: 0.0 Rows_sent: many  Rows_examined: 0\nSET timestamp=1;\nSELECT 4;\n"
                "# Time: 2026-10-31T10:00:03.000000Z\n# User@Host: a[a] @ h []  Id: 1\n"
                "# Query_time: 0.1  Lock_time: 0.0 Rows_sent: 0  Rows_examined: 0\nSET timestamp=1;\n"
                "# Time: 2026-10-31T10:00:04.000000Z\n# User@Host: a[a] @ h []  Id: 1\n"
                "# Query_time: 0.25  Lock_time: 0.0 Rows_sent: 0  Rows_examined: 0\nSET timestamp=1;\nSELECT 5;\n")
        report = parse(text)
        problems = report['parse']['problems']
        self.assertEqual(report['parse']['malformed'], 6)
        self.assertEqual([p['line'] for p in problems], [1, 3, 8, 12, 17, 22])
        self.assertIn('2 lines outside any entry', problems[0]['reason'])
        self.assertIn("Query_time 'soon'", problems[1]['reason'])
        self.assertIn('no # Query_time line', problems[2]['reason'])
        self.assertIn('# Time value is not a date', problems[3]['reason'])
        self.assertIn("Rows_sent 'many'", problems[4]['reason'])
        self.assertIn('no statement text', problems[5]['reason'])
        self.assertEqual(report['totals']['executions'], 1)
        self.assertEqual(report['statements'][0]['sample_text'], 'SELECT 5')
        self.assertTrue(any('6 parts of the log could not be read (the first at line 1)' in n for n in report['notes']))

    def test_listed_problems_are_bounded(self):
        text = '# Query_time: x\nSELECT 1;\n' * (slowlog.MAX_PROBLEMS + 20)
        report = parse(text)
        self.assertEqual(report['parse']['malformed'], slowlog.MAX_PROBLEMS + 20)
        self.assertEqual(len(report['parse']['problems']), slowlog.MAX_PROBLEMS)

    def test_long_lines_are_cut_and_the_rest_skipped(self):
        head = ("# Time: 2026-10-31T10:00:00.000000Z\n# User@Host: a[a] @ h []  Id: 1\n"
                "# Query_time: 0.1  Lock_time: 0.0 Rows_sent: 0  Rows_examined: 0\nSET timestamp=1;\n")
        text = head + "SELECT '" + 'x' * 300 + "' FROM t;\n" + head + 'SELECT 2;\n'
        with mock.patch.object(slowlog, 'MAX_LINE_BYTES', 100):
            report = parse(text)
        self.assertEqual(report['parse']['long_lines'], 1)
        self.assertEqual(report['parse']['entries'], 2)
        cut = [s for s in report['statements'] if s['sample_text'].startswith("SELECT 'x")][0]
        self.assertEqual(len(cut['sample_text']), 100)
        self.assertTrue(any('longer than 1 MiB' in note for note in report['notes']))

    def test_statement_text_is_bounded(self):
        head = ("# Time: 2026-10-31T10:00:00.000000Z\n# User@Host: a[a] @ h []  Id: 1\n"
                "# Query_time: 0.1  Lock_time: 0.0 Rows_sent: 0  Rows_examined: 0\nSET timestamp=1;\n")
        with mock.patch.object(slowlog, 'MAX_TEXT_CHARS', 50):
            report = parse(head + 'SELECT\n' + 'a,\n' * 60 + 'b FROM t;\n')
        self.assertEqual(report['parse']['truncated_statements'], 1)
        self.assertTrue(report['statements'][0]['sample_truncated'])

    def test_not_a_slow_log(self):
        report = parse('2026-10-31T10:00:00.000000Z 0 [System] [MY-010116] [Server] starting\n')
        self.assertEqual(report['parse']['entries'], 0)
        self.assertTrue(any('No slow log entries were found' in note for note in report['notes']))
        self.assertIsNone(report['engine'])


class TestTimes(unittest.TestCase):
    def test_log_time_forms(self):
        self.assertEqual(slowlog.parse_log_time('2026-10-31T10:00:00.123456Z'), ('2026-10-31 10:00:00.123456', 'Z'))
        self.assertEqual(slowlog.parse_log_time('2026-10-31T07:00:00.123456-03:00'),
                         ('2026-10-31 07:00:00.123456', '-03:00'))
        self.assertEqual(slowlog.parse_log_time('261031  9:05:01'), ('2026-10-31 09:05:01', None))
        self.assertEqual(slowlog.parse_log_time('261031 19:05:01'), ('2026-10-31 19:05:01', None))
        for bad in ('', 'yesterday', '2026-02-30T10:00:00Z', '261332 10:00:00', '2026-10-31T25:00:00Z'):
            self.assertIsNone(slowlog.parse_log_time(bad), bad)

    def test_bounds_are_inclusive_to_the_precision_written(self):
        self.assertEqual(slowlog.normalize_bound('2026-10-31 10:00'), '2026-10-31 10:00:00.000000')
        self.assertEqual(slowlog.normalize_bound('2026-10-31T10:00', end=True), '2026-10-31 10:00:59.999999')
        self.assertEqual(slowlog.normalize_bound('2026-10-31', end=True), '2026-10-31 23:59:59.999999')
        self.assertEqual(slowlog.normalize_bound('2026-10-31 10:00:05.5', end=True), '2026-10-31 10:00:05.599999')
        self.assertIsNone(slowlog.normalize_bound('  '))
        for bad in ('31/10/2026', '2026-10-31 24:00', 12, '2026-10-31 10'):
            with self.assertRaises(ValueError):
                slowlog.normalize_bound(bad)
        with self.assertRaisesRegex(ValueError, 'From must be before To'):
            slowlog.check_range('2026-10-31 11:00', '2026-10-31 10:00')

    def test_range_keeps_exactly_the_entries_inside_it(self):
        inside = parse(MYSQL_57, range_from='2026-10-31 10:00:05', range_to='2026-10-31 10:30')
        self.assertEqual(inside['totals']['executions'], 2)
        self.assertEqual(inside['totals']['out_of_range'], 1)
        self.assertEqual(inside['range'], {'from': '2026-10-31 10:00:05.000000', 'to': '2026-10-31 10:30:59.999999',
                                           'applied': True})
        # The span and the anchor describe the whole log, whatever the range.
        self.assertEqual(inside['span']['first'], '2026-10-31 10:00:00.250000')
        self.assertEqual(inside['anchor'], '2026-10-31 10:30:00.000000')
        edge = parse(MYSQL_57, range_from='2026-10-31 10:00:00.25', range_to='2026-10-31 10:00:00.25')
        self.assertEqual(edge['totals']['executions'], 1)
        nothing = parse(MYSQL_57, range_from='2027-01-01')
        self.assertEqual(nothing['statements'], [])
        self.assertEqual(nothing['totals']['executions'], 0)

    def test_offset_logs_compare_the_digits_as_written(self):
        text = MYSQL_57.replace('Z\n', '-03:00\n')
        report = parse(text, range_to='2026-10-31 10:00:01')
        self.assertEqual(report['clock']['label'], 'UTC-03:00')
        self.assertEqual(report['totals']['executions'], 1)

    def test_clock_summaries(self):
        self.assertEqual(slowlog.clock_summary({'Z'}, 3)['kind'], 'utc')
        self.assertEqual(slowlog.clock_summary({'-00:00'}, 3)['label'], 'UTC')
        mixed = slowlog.clock_summary({'-03:00', '-02:00'}, 3)
        self.assertEqual(mixed['kind'], 'mixed')
        self.assertIn('UTC-02:00, UTC-03:00', mixed['text'])
        self.assertIn('not moved to one zone', mixed['text'])
        self.assertEqual(slowlog.clock_summary({'Z', None}, 3)['kind'], 'mixed')
        self.assertEqual(slowlog.clock_summary({None}, 3)['label'], 'Server local time')
        self.assertEqual(slowlog.clock_summary(set(), 0)['kind'], 'none')


class TestFingerprint(unittest.TestCase):
    def test_literals_numbers_comments_and_case(self):
        f = slowlog.fingerprint
        self.assertEqual(f("select * from Orders where id=-42 and name='bob''s' /* hi */ -- c\n"),
                         'SELECT * FROM Orders WHERE id = ? AND name = ?')
        self.assertEqual(f("SELECT a FROM t WHERE b = 0x1F AND c = X'AB' AND d = b'101' AND e = 1.5e3 AND f = \"q\""),
                         'SELECT a FROM t WHERE b = ? AND c = ? AND d = ? AND e = ? AND f = ?')
        self.assertEqual(f("SELECT a FROM t WHERE v = _utf8mb4'x' AND w = N'y' # tail"),
                         'SELECT a FROM t WHERE v = ? AND w = ?')
        self.assertEqual(f('UPDATE t SET a = a - 1 WHERE b = 2'), 'UPDATE t SET a = a - ? WHERE b = ?')
        self.assertEqual(f('select  t1.col1,\n\t`Weird Name`\nfrom `db`.`t1`'), 'SELECT t1.col1, `Weird Name` FROM `db`.`t1`')
        self.assertEqual(f('SELECT 1abc FROM t2'), 'SELECT 1abc FROM t2', 'a name may start with a digit')

    def test_lists_of_values(self):
        f = slowlog.fingerprint
        self.assertEqual(f('SELECT * FROM t WHERE id IN (1)'), f('SELECT * FROM t WHERE id IN (1, 2, 3, 4)'))
        self.assertEqual(f('SELECT * FROM t WHERE id IN (1, 2)'), 'SELECT * FROM t WHERE id IN (...)')
        self.assertEqual(f('SELECT * FROM t WHERE (a, b) IN ((1, 2), (3, 4))'), 'SELECT * FROM t WHERE (a, b) IN (...)')
        self.assertEqual(f("INSERT INTO t (a, b) VALUES (1, 'x'), (2, 'y'), (3, NULL)"),
                         f("INSERT INTO t (a, b) VALUES (9, 'z')"))
        self.assertEqual(f("INSERT INTO t (a, b) VALUES (1, 'x')"), 'INSERT INTO t (a, b) VALUES (...)')
        self.assertEqual(f('INSERT INTO t VALUES (1, NOW()), (2, NOW())'), 'INSERT INTO t VALUES (?, NOW())')
        self.assertEqual(f('INSERT INTO t VALUES ROW(1, 2), ROW(3, 4)'), 'INSERT INTO t VALUES ROW (...)')
        self.assertEqual(f('SELECT CONCAT(1, 2), IFNULL(x, 0) FROM t'), 'SELECT CONCAT(?, ?), IFNULL(x, ?) FROM t',
                         "a call's arguments are not a list of values")
        self.assertEqual(f('SELECT a FROM t WHERE b IN (SELECT c FROM u WHERE d = 3)'),
                         'SELECT a FROM t WHERE b IN (SELECT c FROM u WHERE d = ?)')
        self.assertEqual(f('INSERT INTO t VALUES (1) ON DUPLICATE KEY UPDATE a = 2'),
                         'INSERT INTO t VALUES (...) ON DUPLICATE KEY UPDATE a = ?')

    def test_placeholders_and_variables(self):
        self.assertEqual(slowlog.fingerprint('SELECT @@version, @x := 5, ? FROM t WHERE a IN (?)'),
                         'SELECT @@version, @x := ?, ? FROM t WHERE a IN (...)')

    def test_the_cache_never_changes_a_fingerprint(self):
        """Fingerprints are cached by a shape with literals as ?: texts that
        share a shape differ only in values, so the cached answer is the one
        the tokenizer gives."""
        def direct(text):
            return slowlog._join(slowlog._collapse(slowlog._tokens(text))).rstrip(';').rstrip()
        # (first, second, shapes): names, variables and comments keep their
        # text in the shape; literal values do not.
        pairs = [
            ("SELECT @1, @'a b', `col 1` FROM t WHERE a = 'x' -- it's 1",
             "SELECT @2, @'c d', `col 2` FROM t WHERE a = 'y' -- it's 2", 2),
            ("SELECT * FROM t WHERE a IN (1, 2)", "SELECT * FROM t WHERE a IN (3, 4)", 1),
            ("SELECT x'AB', N'n', _utf8mb4'u', 1.5e3, t.5", "SELECT x'CD', N'm', _utf8mb4'v', 2.5e3, t.6", 1),
            ("SELECT 1 /* it's */ FROM t WHERE b = \"q\"", "SELECT 2 /* it's */ FROM u WHERE b = \"r\"", 2),
        ]
        for first, second, shapes in pairs:
            cached = slowlog.Fingerprints()
            self.assertEqual(cached(first), direct(first))
            self.assertEqual(cached(second), direct(second), second)
            self.assertEqual(len(cached.cache), shapes, second)

    def test_fingerprint_id_is_stable_and_short(self):
        a = slowlog.fingerprint_id('SELECT ?')
        self.assertRegex(a, r'^[0-9a-f]{16}$')
        self.assertEqual(a, slowlog.fingerprint_id('SELECT ?'))
        self.assertNotEqual(a, slowlog.fingerprint_id('SELECT ? FROM t'))


class TestAggregation(unittest.TestCase):
    def log(self, entries):
        out = []
        for i, (user, db, seconds, text) in enumerate(entries):
            out.append('# Time: 2026-10-31T10:00:%02d.000000Z\n# User@Host: %s[%s] @ h []  Id: 1\n'
                       '# Query_time: %s  Lock_time: 0.000001 Rows_sent: 1  Rows_examined: 10\n'
                       'use %s;\nSET timestamp=1;\n%s;\n' % (i, user, user, seconds, db, text))
        return ''.join(out)

    def test_groups_by_schema_and_fingerprint_with_users_and_sample(self):
        text = self.log([('a', 'shop', '0.1', 'SELECT 1'), ('b', 'shop', '0.3', 'SELECT 2'),
                         ('a', 'crm', '0.2', 'SELECT 3'), ('a', 'shop', '0.05', 'SELECT 4')])
        report = parse(text)
        groups = by_text(report)
        shop = groups[('shop', 'SELECT ?')]
        self.assertEqual(shop['count'], 3)
        self.assertEqual(shop['users'], [{'name': 'a', 'count': 2}, {'name': 'b', 'count': 1}])
        self.assertEqual(shop['sample_text'], 'SELECT 2')
        self.assertEqual(shop['sample']['user'], 'b')
        self.assertEqual(groups[('crm', 'SELECT ?')]['count'], 1)
        self.assertEqual(report['databases'], [{'name': 'shop', 'count': 3}, {'name': 'crm', 'count': 1}])

    def test_orders_limit_and_others(self):
        text = self.log([('a', 's', '0.5', 'SELECT 1'), ('a', 's', '0.1', 'SELECT 1 FROM t'),
                         ('a', 's', '0.1', 'SELECT 1 FROM t'), ('a', 's', '0.1', 'SELECT 1 FROM t'),
                         ('a', 's', '0.01', 'SELECT 1 FROM u')])
        self.assertEqual(parse(text, order='total')['statements'][0]['digest_text'], 'SELECT ?')
        self.assertEqual(parse(text, order='count')['statements'][0]['digest_text'], 'SELECT ? FROM t')
        limited = parse(text, order='total', limit=1)
        self.assertEqual(len(limited['statements']), 1)
        self.assertEqual(limited['others'], {'statements': 2, 'executions': 4, 'total_ms': 310.0})
        self.assertEqual(limited['totals']['statements'], 3)
        self.assertIsNone(parse(text)['others'])
        with self.assertRaises(ValueError):
            parse(text, order='random')
        with self.assertRaises(ValueError):
            parse(text, limit=0)

    def test_user_and_schema_filters_keep_the_lists_whole(self):
        text = self.log([('a', 'shop', '0.1', 'SELECT 1'), ('b', 'shop', '0.3', 'SELECT 2'), ('a', 'crm', '0.2', 'SELECT 3')])
        report = parse(text, user='a')
        self.assertEqual(report['totals']['executions'], 2)
        self.assertEqual({u['name'] for u in report['users']}, {'a', 'b'})
        report = parse(text, database='crm')
        self.assertEqual(report['totals']['executions'], 1)
        self.assertEqual(report['filters'], {'user': None, 'database': 'crm'})

    def test_percentiles_follow_performance_schema_buckets(self):
        text = self.log([('a', 's', '%.6f' % (n / 1000.0), 'SELECT 1') for n in range(1, 21)])
        group = parse(text)['statements'][0]
        # 20 calls of 1 to 20 ms: the 19th call (19 ms) holds P95; its bucket's top bound is reported.
        self.assertEqual(group['p95_ms'], slowlog.bucket_high(slowlog.bucket_of(19.0)))
        self.assertGreaterEqual(group['p95_ms'], 19.0)
        self.assertEqual(group['p99_ms'], 20.0, 'never above the slowest call')
        self.assertEqual(sum(b['count'] for b in group['histogram']), 20)

    def test_buckets_match_performance_schema_bounds(self):
        self.assertEqual(slowlog.bucket_of(0.005), 0)
        self.assertEqual(slowlog.bucket_of(0.01), 1)
        for index in (1, 50, 51, 151, 300, 448):
            low = slowlog.bucket_low(index)
            self.assertEqual(slowlog.bucket_of(low + 1e-9), index, index)
        self.assertEqual(slowlog.bucket_of(10 ** 9), 449)
        self.assertIsNone(slowlog.bucket_high(449))

    def test_own_statements_are_left_out_and_counted(self):
        text = self.log([('a', 's', '0.1', 'SELECT 1'), ('a', 's', '0.1', '/*myflames:discover*/ SELECT 2')])
        report = parse(text)
        self.assertEqual(report['totals']['executions'], 1)
        self.assertEqual(report['parse']['excluded_own'], 1)

    def test_too_many_fingerprints_are_summed_apart(self):
        text = self.log([('a', 's', '0.1', 'SELECT %d FROM t%d' % (n, n)) for n in range(5)])
        with mock.patch.object(slowlog, 'MAX_FINGERPRINTS', 2):
            report = parse(text, limit=1)
        self.assertEqual(report['totals']['statements'], 5)
        self.assertEqual(report['others']['statements'], 4)
        self.assertEqual(report['others']['executions'], 4)
        self.assertTrue(any('summed apart' in note for note in report['notes']))
        # Past the overflow bound the count says "at least" and memory stays bounded.
        with mock.patch.object(slowlog, 'MAX_FINGERPRINTS', 2), mock.patch.object(slowlog, 'MAX_OVERFLOW_KEYS', 1):
            report = parse(text, limit=1)
        self.assertEqual(report['others']['executions'], 4)
        self.assertTrue(any('at least 1 more were summed apart' in note for note in report['notes']))

    def test_cache_and_deep_nesting_never_change_a_fingerprint(self):
        cached = slowlog.Fingerprints()
        self.assertEqual([cached('SELECT * FROM orders$2025 WHERE id = 1'), cached('SELECT * FROM orders$2026 WHERE id = 2')],
                         ['SELECT * FROM orders$2025 WHERE id = ?', 'SELECT * FROM orders$2026 WHERE id = ?'])
        deep = 'SELECT * FROM t WHERE ' + '(' * 5000 + 'a = 1' + ')' * 5000
        self.assertTrue(slowlog.fingerprint(deep).endswith('a = ?' + ')' * 5000))
        self.assertEqual(slowlog.fingerprint('SELECT 1 FROM t WHERE (a, b) IN ((1, 2), (3, 4))'),
                         'SELECT ? FROM t WHERE (a, b) IN (...)')

    def test_a_string_left_open_by_a_backslash_is_read_without_escapes(self):
        # sql_mode NO_BACKSLASH_ESCAPES: 'C:\\' is a whole string.
        a = slowlog.fingerprint("SELECT * FROM t WHERE path = 'C:\\' AND b = 'x'")
        b = slowlog.fingerprint("SELECT * FROM t WHERE path = 'C:\\' OR c = 'x'")
        self.assertEqual((a, b), ('SELECT * FROM t WHERE path = ? AND b = ?', 'SELECT * FROM t WHERE path = ? OR c = ?'))
        self.assertEqual(slowlog.fingerprint("SELECT 'it\\'s', 'a\\\\' FROM t"), 'SELECT ?, ? FROM t')
        cached = slowlog.Fingerprints()
        self.assertNotEqual(cached("SELECT 'C:\\' AND 'x'"), cached("SELECT 'C:\\' OR 'x'"))

    def test_stop_is_checked_by_bytes_on_long_lines(self):
        line = b'x' * 1000 + b'\n'
        stream = io.BytesIO(line * 50)
        checks = []
        lines = slowlog.iter_lines(stream, slowlog.ParseStats(), progress=checks.append, every=4096, every_bytes=5000)
        self.assertEqual(len(list(lines)), 50)
        self.assertGreaterEqual(len(checks), 9)


class FakeConn(object):
    """Answers the table source's statements like the mysql client in batch mode."""

    def __init__(self, rows, engine='mysql', log_output='TABLE', fail=None, span=None):
        self.rows = rows
        self.engine = engine
        self.log_output = log_output
        self.fail = fail
        self.sent = []
        self.span = span or ('2026-10-31 09:00:00.000000', '2026-10-31 11:00:00.000000', str(len(rows)))

    def server_version(self):
        return '8.4.13' if self.engine == 'mysql' else '11.4.13-MariaDB'

    def is_mariadb(self):
        return self.engine == 'mariadb'

    def server_version_comment(self):
        return 'MySQL Community Server - GPL'

    def run(self, sql, extra_flags=None):
        self.sent.append(sql)
        assert sql.startswith(slow_queries.TAG), sql
        body = sql[len(slow_queries.TAG):]
        if body.startswith('SELECT @@version'):
            return '%s\t1\n' % self.server_version()
        if 'NOW(6)' in body:
            return '2026-10-31 11:30:00.000000\tSYSTEM\t-03\t-180\t%s\tON\t0.000000\n' % self.log_output
        if self.fail:
            raise ConnectorError(self.fail)
        if 'MIN(start_time)' in body:
            return '\t'.join(self.span) + '\n'
        if body.startswith('SELECT OCTET_LENGTH('):
            return ''.join('%d\n' % len(str(row[-1]).encode('utf-8')) for row in self.rows)
        escaped = []
        for row in self.rows:
            escaped.append('\t'.join('NULL' if c is None else str(c).replace('\\', '\\\\').replace('\n', '\\n')
                                     .replace('\t', '\\t') for c in row))
        return '\n'.join(escaped) + ('\n' if escaped else '')


def table_row(start, user, qt, text, db='shop', sent=1, examined=10):
    return (start, '%s[%s] @ web [10.0.0.1]' % (user, user), qt, '00:00:00.000010', sent, examined, db, 42,
            len(text.encode('utf-8')), text)


class TestTableSource(unittest.TestCase):
    def test_rows_are_summed_like_a_file_with_the_servers_clock(self):
        rows = [table_row('2026-10-31 10:00:02.000000', 'b', '00:00:00.300000', 'SELECT 2\nFROM t;'),
                table_row('2026-10-31 10:00:01.000000', 'a', '00:00:00.100000', 'SELECT 1 FROM t')]
        conn = FakeConn(rows)
        report = slow_queries.slow_log_table_report(conn, '2026-10-31 10:00', '2026-10-31 10:59')
        validate(report, REPORT_SCHEMA)
        self.assertEqual(report['source'], {'kind': 'table', 'table': 'mysql.slow_log', 'rows': 2, 'rows_read': 2,
                                            'capped': False})
        group = report['statements'][0]
        self.assertEqual((group['count'], group['total_ms'], group['source']), (2, 400.0, 'mysql.slow_log'))
        self.assertEqual(group['sample_text'], 'SELECT 2\nFROM t')
        self.assertIsNone(group['sample']['line'])
        self.assertEqual(report['clock']['label'], 'Server time (UTC-03:00)')
        self.assertIn('time_zone=SYSTEM', report['clock']['text'])
        self.assertEqual(report['anchor'], '2026-10-31 11:30:00.000000')
        self.assertEqual(report['time_meaning']['event'], 'end')
        self.assertIn('in start_time', report['time_meaning']['text'])
        read = [s for s in conn.sent if 'FROM mysql.slow_log WHERE' in s][0]
        self.assertIn('CHAR_LENGTH(sql_text)', read, 'the cut check counts what LEFT counts on both engines')
        self.assertIn("start_time >= '2026-10-31 10:00:00.000000'", read)
        self.assertIn("start_time <= '2026-10-31 10:59:59.999999'", read)
        self.assertIn("LOCATE('/*myflames:', sql_text) = 0", read)
        self.assertIn('ORDER BY start_time DESC LIMIT %d' % (slow_queries.MAX_ROWS + 1), read)
        for sql in conn.sent:
            self.assertRegex(sql[len(slow_queries.TAG):], r'^SELECT ')

    def test_long_statements_read_fewer_rows_than_the_client_limit(self):
        rows = [table_row('2026-10-31 10:00:%02d.000000' % (9 - n), 'a', '00:00:00.001000', 'SELECT %s' % ('x' * 100))
                for n in range(5)]
        small = FakeConn(rows)
        slow_queries.slow_log_table_report(small)
        self.assertFalse(any('OCTET_LENGTH' in sql for sql in small.sent), 'a small table needs no sizing read')
        conn = FakeConn(rows, span=('2026-10-31 09:00:00.000000', '2026-10-31 11:00:00.000000', '100000'))
        budget = 2 * (slow_queries.ROW_BYTES + 107 * slow_queries.TEXT_GROWTH)
        with mock.patch.object(slow_queries, 'READ_BUDGET', budget):
            report = slow_queries.slow_log_table_report(conn, user='a')
        read = [sql for sql in conn.sent if 'CHAR_LENGTH(sql_text)' in sql][0]
        self.assertIn('ORDER BY start_time DESC LIMIT 3', read)
        self.assertEqual(report['source']['rows_read'], 2)
        self.assertTrue(report['source']['capped'])
        self.assertTrue(any("Only the newest 2 rows in the range were read (their statements' text fills what one read "
                            "may return); the User and Database choices apply to those rows" in note
                            for note in report['notes']))

    def test_the_threshold_note_follows_the_server(self):
        sentence = slow_queries._threshold_sentence
        self.assertIn('whole time, lock waits included, exceeds', sentence('mysql', (8, 4, 6), '1'))
        self.assertIn('whole time', sentence('mysql', (8, 0, 28), '1'))
        for version in ((5, 7, 44), (8, 0, 27)):
            self.assertIn('after its tables are locked exceeds', sentence('mysql', version, '1'))
        self.assertIn('after its tables are locked is at least', sentence('mariadb', (11, 4, 13), '1'))
        self.assertIn("Each row keeps the time zone of the session that ran the statement",
                      slow_queries._clock('SYSTEM', '-03', -180)['text'])

    def test_capped_reads_say_so(self):
        rows = [table_row('2026-10-31 10:00:%02d.000000' % n, 'a', '00:00:00.001000', 'SELECT %d' % n)
                for n in range(5)]
        report = slow_queries.slow_log_table_report(FakeConn(rows), max_rows=3)
        self.assertTrue(report['source']['capped'])
        self.assertEqual(report['totals']['executions'], 3)
        self.assertTrue(any('Only the newest 3 rows' in note for note in report['notes']))

    def test_empty_table_on_a_file_log_points_at_the_file(self):
        report = slow_queries.slow_log_table_report(FakeConn([], log_output='FILE'))
        self.assertTrue(any('choose Slow log file as the source' in note for note in report['notes']))

    def test_denied_and_other_errors(self):
        with self.assertRaisesRegex(ValueError, 'GRANT SELECT ON mysql.slow_log'):
            slow_queries.slow_log_table_report(FakeConn([], fail="ERROR 1142 (42000): SELECT command denied"))
        with self.assertRaisesRegex(ValueError, 'Could not read mysql.slow_log'):
            slow_queries.slow_log_table_report(FakeConn([], fail="ERROR 1146 (42S02): Table doesn't exist"))

    def test_time_values(self):
        self.assertEqual(slow_queries.time_micros('00:00:01.000123'), 1000123)
        self.assertEqual(slow_queries.time_micros('838:59:59'), 3020399000000)
        self.assertIsNone(slow_queries.time_micros('-00:00:01'))
        self.assertIsNone(slow_queries.time_micros('soon'))

    def test_mariadb_meaning(self):
        rows = [table_row('2026-10-31 10:00:01.000000', 'a', '00:00:00.100000', 'SELECT 1')]
        report = slow_queries.slow_log_table_report(FakeConn(rows, engine='mariadb'))
        self.assertEqual((report['engine'], report['time_meaning']['event']), ('mariadb', 'start'))


class TestEndpoints(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {'HOME': self.home.name, 'USERPROFILE': self.home.name,
                                               'MYFLAMES_SECRET_BACKEND': 'vault'})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.home.cleanup)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        self.workspace = SimpleNamespace()
        self.path = os.path.join(self.home.name, 'host-slow.log')
        with open(self.path, 'w', encoding='utf-8') as handle:
            handle.write(MYSQL_57)

    def call(self, path, payload):
        return ui.api_response(path, payload, self.workspace)

    def test_open_read_progress_and_schema(self):
        opened = self.call('/api/slow-log/open-path', {'path': self.path})
        self.assertEqual(set(opened), {'file_id', 'name', 'size', 'modified'})
        self.assertEqual(opened['name'], 'host-slow.log')
        report = self.call('/api/slow-log/read', {'file_id': opened['file_id'], 'progress_id': 'read-0001',
                                                  'from': '2026-10-31 10:00', 'to': '2026-10-31 10:10',
                                                  'order': 'count', 'limit': 20})
        validate(report, REPORT_SCHEMA)
        self.assertEqual(report['source']['file_id'], opened['file_id'])
        self.assertEqual(report['source']['name'], 'host-slow.log')
        self.assertNotIn(self.home.name, json.dumps(report), 'the folder never reaches the page')
        self.assertEqual(report['totals']['executions'], 2)
        progress = self.call('/api/slow-log/progress', {'progress_id': 'read-0001'})
        validate(progress, PROGRESS_SCHEMA)
        self.assertEqual((progress['phase'], progress['bytes_read'], progress['percent']),
                         ('done', len(MYSQL_57.encode('utf-8')), 100.0))
        unknown = self.call('/api/slow-log/progress', {'progress_id': 'nothing-here'})
        validate(unknown, PROGRESS_SCHEMA)
        self.assertEqual(unknown['phase'], 'unknown')

    def test_a_file_moved_after_opening_is_named_without_its_folder(self):
        opened = self.call('/api/slow-log/open-path', {'path': self.path})
        os.remove(self.path)
        with self.assertRaises(ValueError) as caught:
            self.call('/api/slow-log/read', {'file_id': opened['file_id']})
        self.assertIn('host-slow.log no longer exists', str(caught.exception))
        self.assertNotIn(self.home.name, str(caught.exception))

    def test_bad_requests_are_refused_before_reading(self):
        opened = self.call('/api/slow-log/open-path', {'path': self.path})
        with mock.patch.object(slowlog, 'read_file', side_effect=AssertionError('read')):
            for payload, message in (({'from': 'soon'}, 'From must be'), ({'order': 'x'}, 'Rank by'),
                                     ({'limit': 5000}, 'limit'), ({'progress_id': '../x'}, 'progress_id'),
                                     ({'from': '2026-10-31 11:00', 'to': '2026-10-31 10:00'}, 'before To')):
                with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, message):
                    self.call('/api/slow-log/read', dict(payload, file_id=opened['file_id']))
        with self.assertRaisesRegex(ValueError, 'no longer open'):
            self.call('/api/slow-log/read', {'file_id': 'unknown'})
        for path, message in (('relative/slow.log', 'full path'), (self.home.name, 'is a folder'),
                              (os.path.join(self.home.name, 'missing.log'), 'No file exists')):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, message):
                self.call('/api/slow-log/open-path', {'path': path})

    def test_stop_ends_a_running_read(self):
        opened = self.call('/api/slow-log/open-path', {'path': self.path})
        files = slowlog_api.registry(self.workspace)
        real = slowlog.read_file

        def read_with_stop(*args, **kwargs):
            # Stop arrives while the file is read: the next check raises.
            self.assertEqual(self.call('/api/slow-log/stop', {'progress_id': 'read-0002'})['stop_requested'], True)
            self.assertTrue(kwargs['stop']())
            return real(*args, **dict(kwargs, stop=lambda: True))
        with mock.patch.object(slowlog, 'read_file', side_effect=read_with_stop), \
                mock.patch.object(slowlog, 'iter_lines', side_effect=lambda stream, stats, progress, stop: iter(
                    _raise_stopped(stop))):
            with self.assertRaisesRegex(ValueError, 'Stopped reading'):
                self.call('/api/slow-log/read', {'file_id': opened['file_id'], 'progress_id': 'read-0002'})
        self.assertEqual(self.call('/api/slow-log/progress', {'progress_id': 'read-0002'})['phase'], 'stopped')
        self.assertEqual(files.running, 0)
        self.assertFalse(self.call('/api/slow-log/stop', {'progress_id': 'read-0002'})['stop_requested'])

    def test_choose_uses_the_native_chooser_and_its_cancel(self):
        with mock.patch.object(slowlog_api, 'choose_file', return_value={'path': None, 'cancelled': True}):
            self.assertEqual(self.call('/api/slow-log/choose', {}), {'cancelled': True})
        with mock.patch.object(slowlog_api, 'choose_file', return_value={'path': self.path, 'cancelled': False}):
            self.assertEqual(self.call('/api/slow-log/choose', {})['name'], 'host-slow.log')

    def test_chooser_runs_a_fixed_dialog(self):
        from myflames import file_picker
        with mock.patch.object(file_picker, '_run_dialog', return_value=self.path) as run:
            self.assertEqual(slowlog_api.choose_file('darwin'), {'path': os.path.realpath(self.path), 'cancelled': False})
        argv = run.call_args[0][0]
        self.assertEqual(argv, [file_picker._MACOS_OSASCRIPT])
        self.assertIn('slow query log', run.call_args[1]['input_text'])
        with mock.patch.object(file_picker, '_run_dialog', return_value=None):
            self.assertEqual(slowlog_api.choose_file('linux'), {'path': None, 'cancelled': True})
        with mock.patch.object(file_picker, '_run_dialog', return_value='relative.log'):
            with self.assertRaisesRegex(ValueError, 'Type the file path instead'):
                slowlog_api.choose_file('darwin')
        with self.assertRaisesRegex(ValueError, 'No native file chooser'):
            slowlog_api.choose_file('plan9')

    def test_table_endpoint_validates_before_connecting(self):
        with mock.patch('myflames.connector.MySQLConnection', side_effect=AssertionError('connected')):
            with self.assertRaisesRegex(ValueError, 'From must be'):
                self.call('/api/live/slow-log', {'connection': {'host': '127.0.0.1', 'user': 'root'}, 'from': 'x'})
        self.assertIn('/api/live/slow-log', ui._SERVER_VIEW_PATHS)
        for path in slowlog_api.PATHS:
            self.assertIn(path, ui._SLOW_LOG_PATHS)

    def test_table_endpoint_reads_through_the_connection(self):
        rows = [table_row('2026-10-31 10:00:01.000000', 'a', '00:00:00.100000', 'SELECT 1')]

        class Conn(FakeConn):
            def __init__(self, **options):
                FakeConn.__init__(self, rows)
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        with mock.patch('myflames.connector.MySQLConnection', Conn):
            report = self.call('/api/live/slow-log', {'connection': {'host': '127.0.0.1', 'user': 'root'},
                                                      'order': 'avg', 'limit': 10})
        validate(report, REPORT_SCHEMA)
        self.assertEqual(report['order'], 'avg')

    def test_endpoints_need_a_workspace(self):
        with self.assertRaisesRegex(ValueError, 'running workspace'):
            ui.api_response('/api/slow-log/read', {}, None)


def _raise_stopped(stop):
    if stop():
        raise slowlog.Stopped()
    return iter(())


class TestCli(unittest.TestCase):
    def run_cli(self, *args, stdin=None):
        home = tempfile.mkdtemp()
        env = dict(os.environ, HOME=home, USERPROFILE=home, MYFLAMES_SECRET_BACKEND='vault', PYTHONPATH=ROOT)
        proc = subprocess.run([sys.executable, '-m', 'myflames', 'slow-log'] + list(args), input=stdin,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=ROOT)
        return proc.returncode, proc.stdout.decode('utf-8'), proc.stderr.decode('utf-8')

    def test_table_output_and_notes(self):
        path = os.path.join(FIXTURES, 'mysql-8.4.13-slow.log')
        code, out, err = self.run_cli(path, '--limit', '3')
        self.assertEqual(code, 0, err)
        self.assertEqual(len(out.strip().splitlines()), 4)
        self.assertIn('FINGERPRINT', out.splitlines()[0])
        self.assertIn('source: Slow log file: mysql-8.4.13-slow.log', err)
        self.assertIn('UTC-03:00', err)
        code, out, err = self.run_cli(path, '--limit', '3', '-q')
        self.assertEqual((code, err), (0, ''))

    def test_json_output_file_stdin_and_filters(self):
        path = os.path.join(FIXTURES, 'mariadb-11.4.13-slow.log')
        code, out, err = self.run_cli(path, '--json', '--schema', 'crm', '--executed-by', 'report', '--order', 'count')
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        validate(report, REPORT_SCHEMA)
        self.assertEqual(report['filters'], {'user': 'report', 'database': 'crm'})
        self.assertEqual(report['totals']['executions'], 4)
        target = os.path.join(tempfile.mkdtemp(), 'out.json')
        code, out, err = self.run_cli(path, '--json', '-o', target)
        self.assertEqual((code, out), (0, ''))
        with open(target, encoding='utf-8') as handle:
            self.assertEqual(json.load(handle)['schema_version'], 'slow-log-1.0')
        with open(path, 'rb') as handle:
            code, out, err = self.run_cli('-', '--json', stdin=handle.read())
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['source']['name'], 'stdin')

    def test_bad_input_exits_2(self):
        path = os.path.join(FIXTURES, 'mysql-5.7.44-slow.log')
        for args, message in (((), 'name a slow log FILE'), ((path, '--from', 'soon'), 'From must be'),
                              (('/no/such/slow.log',), 'No file exists'), ((path, '--table'), 'name a slow log FILE'),
                              ((path, '--limit', '0'), 'Choose an integer')):
            with self.subTest(args=args):
                code, out, err = self.run_cli(*args)
                self.assertEqual(code, 2)
                self.assertEqual(out, '')
                self.assertIn(message, err)

    def test_standard_input_is_bounded_and_never_held_whole(self):
        from myflames import slowlog_cli
        stream = slowlog_cli._Bounded(io.BytesIO(b'0123456789\n' * 3))
        with mock.patch.object(slowlog, 'MAX_FILE_BYTES', 20):
            self.assertEqual(stream.readline(), b'0123456789\n')
            with self.assertRaisesRegex(ValueError, 'larger than 4 GiB'):
                stream.readline()
                stream.readline()

    def test_listed_in_the_root_help(self):
        home = tempfile.mkdtemp()
        env = dict(os.environ, HOME=home, USERPROFILE=home, PYTHONPATH=ROOT)
        out = subprocess.run([sys.executable, '-m', 'myflames', '--help'], stdout=subprocess.PIPE, env=env,
                             cwd=ROOT).stdout.decode('utf-8')
        self.assertIn('slow-log  Sum a slow query log', out)


if __name__ == '__main__':
    unittest.main()
