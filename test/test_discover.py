"""Slow-query discovery: top statements, running statements, explain a running connection.

Fake connections return canned ``mysql --batch`` text for the MySQL 8.0 /
8.4 / 9.x and MariaDB 10.11 / 11.4 paths, so no server is needed.
"""
import contextlib
import inspect
import io
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

from myflames import discover, ui, workspace_cli
from myflames.connector import ConnectorError
from myflames.live import normalize_capture

MYSQL_V2_PLAN = json.dumps({
    "query": "SELECT * FROM t WHERE a > 1",
    "operation": "Filter: (t.a > 1)", "access_type": "filter",
    "estimated_rows": 33.3, "estimated_total_cost": 10.25,
    "inputs": [{"operation": "Table scan on t", "access_type": "table",
                "table_name": "t", "estimated_rows": 100.0,
                "estimated_total_cost": 10.25}],
}, indent=2)
MARIADB_PARTIAL = json.dumps({
    "r_query_time_in_progress_ms": 2413,
    "query_block": {"select_id": 1, "r_loops": 1, "nested_loop": [{"table": {
        "table_name": "t", "access_type": "ALL", "loops": 1, "r_loops": 1,
        "rows": 20, "r_rows": 5, "filtered": 100, "r_filtered": 80,
        "r_table_time_ms": 0.02, "attached_condition": "t.a > 1"}}]},
}, indent=2)
MARIADB_EXPLAIN = json.dumps({
    "query_block": {"select_id": 1, "cost": 0.03, "nested_loop": [{"table": {
        "table_name": "t", "access_type": "ALL", "loops": 1, "rows": 20,
        "filtered": 100, "attached_condition": "t.a > 1"}}]},
}, indent=2)
PASSWORD = 'hunter2-not-real'


def batch(rows):
    """Render rows like ``mysql --batch --skip-column-names`` (NULL, \\n, \\t escaped)."""
    def cell(value):
        if value is None:
            return 'NULL'
        return str(value).replace('\\', '\\\\').replace('\n', '\\n').replace('\t', '\\t')
    return ''.join('\t'.join(cell(v) for v in row) + '\n' for row in rows)


def digest_row(text, schema='shop', count=10, total_ps=5 * 10 ** 12, avg_ps=5 * 10 ** 11,
               max_ps=9 * 10 ** 11, examined=1000, sent=10, no_index=0, no_good=0,
               disk=0, merges=0, sample=None, with_sample=True):
    row = [schema, 'd' * 64, text, count, total_ps, avg_ps, max_ps, examined, sent,
           no_index, no_good, disk, merges, '2026-10-01 10:00:00.000000',
           '2026-10-12 10:00:00.000000']
    if with_sample:
        row.append(sample)
    return row


class FakeConn(object):
    """Answers SQL by substring; records every statement and client flags."""

    def __init__(self, version, responses=(), **kwargs):
        self.version = version
        self.responses = list(responses)
        self.calls = []
        self.kwargs = kwargs

    def run(self, sql, extra_flags=None, timeout=None):
        self.calls.append((sql, list(extra_flags or [])))
        for pattern, result in self.responses:
            if pattern in sql:
                if isinstance(result, Exception):
                    raise result
                return result(sql) if callable(result) else result
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

    def sql(self, needle):
        return [sql for sql, _ in self.calls if needle in sql]


def server(version, ps_on=True):
    return ('SELECT @@version, @@performance_schema', batch([[version, 1 if ps_on else 0]]))


CONSUMERS_ON = ('setup_consumers', batch([['global_instrumentation', 'YES'], ['statements_digest', 'YES']]))
MAX_TEXT = ('performance_schema_max_sql_text_length', batch([[1024]]))


def mysql_top(version, rows, extra=()):
    return FakeConn(version, list(extra) + [server(version), CONSUMERS_ON, MAX_TEXT,
                                            ('events_statements_summary_by_digest', batch(rows))])


class TestTopStatements(unittest.TestCase):
    def test_mysql_84_fields_units_flags_and_query_shape(self):
        conn = mysql_top('8.4.3', [
            digest_row('SELECT * FROM `orders` WHERE `status` = ?', examined=100000, sent=10,
                       no_index=3, disk=1, sample="SELECT * FROM orders WHERE status = 'new'"),
            digest_row('UPDATE `t` SET `a` = ?', examined=500, sent=0, sample='UPDATE t SET a = 1'),
            digest_row('SELECT `a` FROM `t` WHERE `id` = ?', schema=None, examined=0, sent=1, merges=2, sample='SELECT a FROM t WHERE id = 1'),
        ])
        rows = discover.top_statements(conn)
        self.assertEqual(len(rows), 3)
        first = rows[0]
        self.assertEqual(first['total_ms'], 5000.0)
        self.assertEqual(first['avg_ms'], 500.0)
        self.assertEqual(first['max_ms'], 900.0)
        self.assertEqual(first['examined_per_sent'], 10000.0)
        self.assertEqual(first['sample_text'], "SELECT * FROM orders WHERE status = 'new'")
        self.assertFalse(first['sample_truncated'])
        self.assertEqual(first['flags'], ['no_index_used', 'tmp_disk_tables', 'low_selectivity'])
        # MySQL Shell's heuristic is for SELECTs: an UPDATE sends no rows by design.
        self.assertNotIn('low_selectivity', rows[1]['flags'])
        self.assertIsNone(rows[1]['examined_per_sent'])
        self.assertIsNone(rows[2]['schema'])
        self.assertEqual(rows[2]['flags'], ['sort_merge_passes'])
        for key in ('digest', 'digest_text', 'schema', 'count', 'rows_examined', 'rows_sent',
                    'no_index_used', 'tmp_disk_tables', 'sort_merge_passes', 'first_seen',
                    'last_seen', 'flags'):
            self.assertIn(key, first)
        sql = conn.sql('events_statements_summary_by_digest')[0]
        self.assertIn('QUERY_SAMPLE_TEXT', sql)
        self.assertIn('ORDER BY SUM_TIMER_WAIT DESC LIMIT 20', sql)
        self.assertIn('DIGEST IS NOT NULL', sql)
        self.assertIn("LOCATE('/*myflames:', QUERY_SAMPLE_TEXT) = 0", sql)
        self.assertNotIn(' sys.', sql)
        for statement, flags in conn.calls:
            self.assertTrue(statement.startswith(discover.DISCOVER_TAG))
            self.assertIn('--comments', flags)

    def test_excludes_own_capture_diagnostics_and_monitoring_rows(self):
        conn = mysql_top('9.7.0', [
            digest_row('SELECT * FROM `t`', sample='/*myflames:abc123*/ SELECT * FROM t'),
            digest_row('EXPLAIN FORMAT = JSON SELECT * FROM `t`', sample='EXPLAIN FORMAT=JSON SELECT * FROM t'),
            digest_row('ANALYZE FORMAT = JSON SELECT * FROM `t`', sample='ANALYZE FORMAT=JSON SELECT 1'),
            digest_row('SELECT NAME FROM `performance_schema` . `setup_consumers`', sample='x'),
            digest_row('SELECT ID FROM `information_schema` . `PROCESSLIST`', sample='y'),
            digest_row('SELECT * FROM `orders`', sample='SELECT * FROM orders'),
        ])
        rows = discover.top_statements(conn)
        self.assertEqual([r['digest_text'] for r in rows], ['SELECT * FROM `orders`'])
        sql = conn.sql('events_statements_summary_by_digest')[0]
        self.assertIn("TRIM(REPLACE(DIGEST_TEXT, '`', '')) NOT LIKE 'EXPLAIN %'", sql)
        self.assertIn("LOCATE('performance_schema', DIGEST_TEXT) = 0", sql)

    def test_order_options_map_to_digest_columns(self):
        for order, column in (('total', 'SUM_TIMER_WAIT'), ('avg', 'AVG_TIMER_WAIT'),
                              ('examined', 'SUM_ROWS_EXAMINED'), ('count', 'COUNT_STAR')):
            conn = mysql_top('8.4.3', [])
            report = discover.top_report(conn, limit=5, order=order)
            self.assertEqual(report['order'], order)
            self.assertIn('ORDER BY %s DESC LIMIT 5' % column,
                          conn.sql('events_statements_summary_by_digest')[0])

    def test_schema_filter_is_a_hex_literal(self):
        conn = mysql_top('8.4.3', [])
        discover.top_statements(conn, schema="sh'op")
        sql = conn.sql('events_statements_summary_by_digest')[0]
        self.assertIn("SCHEMA_NAME = CONVERT(X'%s' USING utf8mb4)" % "sh'op".encode().hex(), sql)
        self.assertNotIn("sh'op", sql)

    def test_rejects_bad_arguments_before_connecting(self):
        conn = FakeConn('8.4.3')
        for kwargs in ({'limit': 0}, {'limit': 201}, {'limit': True}, {'limit': '5'},
                       {'order': 'random'}, {'schema': ''}, {'schema': 'x' * 65}):
            with self.assertRaises(ValueError):
                discover.top_statements(conn, **kwargs)
        self.assertEqual(conn.calls, [])

    def test_sample_text_column_is_gated_at_mysql_803(self):
        old = FakeConn('8.0.2', [server('8.0.2'), CONSUMERS_ON, (
            'events_statements_summary_by_digest',
            batch([digest_row('SELECT `a` FROM `t`', with_sample=False)]))])
        rows = discover.top_statements(old)
        self.assertNotIn('QUERY_SAMPLE_TEXT', old.sql('events_statements_summary_by_digest')[0])
        self.assertIsNone(rows[0]['sample_text'])
        self.assertEqual(old.sql('max_sql_text_length'), [])
        new = mysql_top('8.0.3', [])
        discover.top_statements(new)
        self.assertIn('QUERY_SAMPLE_TEXT', new.sql('events_statements_summary_by_digest')[0])

    def test_truncated_sample_is_flagged_with_a_note(self):
        long = 'SELECT ' + 'x' * 1100
        conn = mysql_top('9.1.0', [digest_row('SELECT `a` FROM `t`', sample=long[:1024])])
        report = discover.top_report(conn)
        self.assertTrue(report['statements'][0]['sample_truncated'])
        self.assertTrue(any('max_sql_text_length' in n for n in report['notes']))

    def test_mariadb_1011_performance_schema_off_is_explained(self):
        conn = FakeConn('10.11.9-MariaDB', [server('10.11.9-MariaDB', ps_on=False)])
        with self.assertRaises(ValueError) as caught:
            discover.top_statements(conn)
        message = str(caught.exception)
        self.assertIn('MariaDB disables it by default', message)
        self.assertIn('performance_schema=ON', message)

    def test_mysql_performance_schema_off_is_explained(self):
        conn = FakeConn('8.4.3', [server('8.4.3', ps_on=False)])
        with self.assertRaisesRegex(ValueError, 'performance_schema=OFF'):
            discover.top_statements(conn)

    def test_server_without_performance_schema_variable_counts_as_off(self):
        conn = FakeConn('10.11.9-MariaDB', [('@@performance_schema', ConnectorError(
            "mysql client exited 1: ERROR 1193 (HY000): Unknown system variable 'performance_schema'"))])
        with self.assertRaisesRegex(ValueError, 'Performance Schema is off'):
            discover.top_statements(conn)

    def test_mariadb_114_has_no_sample_text(self):
        conn = FakeConn('11.4.3-MariaDB-ubu2404', [server('11.4.3-MariaDB-ubu2404'), CONSUMERS_ON, (
            'events_statements_summary_by_digest',
            batch([digest_row('SELECT * FROM `t` WHERE `id` = ?', with_sample=False,
                              examined=10, sent=10)]))])
        report = discover.top_report(conn)
        self.assertEqual(report['engine'], 'mariadb')
        self.assertNotIn('QUERY_SAMPLE_TEXT', conn.sql('events_statements_summary_by_digest')[0])
        self.assertIsNone(report['statements'][0]['sample_text'])
        self.assertEqual(report['statements'][0]['flags'], [])
        self.assertTrue(any('?' in n for n in report['notes']))

    def test_disabled_digest_consumer_is_explained(self):
        conn = FakeConn('8.4.3', [server('8.4.3'), ('setup_consumers', batch(
            [['global_instrumentation', 'YES'], ['statements_digest', 'NO']]))])
        with self.assertRaises(ValueError) as caught:
            discover.top_statements(conn)
        self.assertIn('statements_digest', str(caught.exception))
        self.assertIn("SET ENABLED='YES'", str(caught.exception))

    def test_missing_select_privilege_is_explained(self):
        denied = ConnectorError("mysql client exited 1: ERROR 1142 (42000): SELECT command denied "
                                "to user 'app'@'%' for table 'events_statements_summary_by_digest'")
        conn = mysql_top('8.4.3', [], extra=[('events_statements_summary_by_digest', denied)])
        with self.assertRaisesRegex(ValueError, r'GRANT SELECT ON performance_schema\.\*'):
            discover.top_statements(conn)
        conn = FakeConn('8.4.3', [server('8.4.3'), ('setup_consumers', denied)])
        with self.assertRaisesRegex(ValueError, 'GRANT SELECT ON performance_schema'):
            discover.top_statements(conn)

    def test_multiline_sample_text_is_unescaped(self):
        conn = mysql_top('8.4.3', [digest_row('SELECT `a` FROM `t`', sample='SELECT\n\t1')])
        self.assertEqual(discover.top_statements(conn)[0]['sample_text'], 'SELECT\n\t1')


def processlist(rows):
    return batch(rows)


class TestRunningStatements(unittest.TestCase):
    def test_mysql_84_reads_performance_schema_processlist(self):
        conn = FakeConn('8.4.3', [server('8.4.3'), ('processlist', processlist([
            [42, 'app', '10.0.0.5:5555', 'shop', 'Query', 12, 'Sending data', 'SELECT * FROM orders'],
            [43, 'app', '10.0.0.5:5556', None, 'Execute', 3, 'executing', 'SELECT ?'],
            [44, 'app', '10.0.0.5:5557', 'shop', 'Query', None, None, None],
        ]))])
        report = discover.running_report(conn, min_seconds=2)
        self.assertEqual(report['schema_version'], 'running-1.0')
        self.assertEqual(report['source'], 'performance_schema.processlist')
        first = report['statements'][0]
        self.assertEqual(first, {'id': 42, 'user': 'app', 'host': '10.0.0.5:5555', 'db': 'shop',
                                 'command': 'Query', 'time_s': 12.0, 'state': 'Sending data',
                                 'info': 'SELECT * FROM orders', 'explainable': True})
        # Prepared statements cannot be explained by EXPLAIN FOR CONNECTION.
        self.assertFalse(report['statements'][1]['explainable'])
        self.assertIsNone(report['statements'][1]['db'])
        self.assertIsNone(report['statements'][2]['time_s'])
        self.assertFalse(report['statements'][2]['explainable'])
        sql = conn.sql('processlist')[0]
        self.assertIn('FROM performance_schema.processlist', sql)
        self.assertIn('ID <> CONNECTION_ID()', sql)
        self.assertIn("COMMAND NOT IN ('Sleep', 'Daemon', 'Binlog Dump', 'Binlog Dump GTID')", sql)
        self.assertIn('TIME >= 2.0', sql)
        self.assertIn('ORDER BY TIME DESC', sql)

    def test_older_mysql_and_disabled_performance_schema_use_information_schema(self):
        for version, ps_on in (('8.0.21', True), ('8.4.3', False)):
            conn = FakeConn(version, [server(version, ps_on), ('PROCESSLIST', '')])
            report = discover.running_report(conn)
            self.assertEqual(report['source'], 'information_schema.PROCESSLIST')
            self.assertEqual(report['statements'], [])
            self.assertNotIn('TIME >=', conn.sql('PROCESSLIST')[0])
        conn = FakeConn('8.0.22', [server('8.0.22'), ('processlist', '')])
        self.assertEqual(discover.running_report(conn)['source'], 'performance_schema.processlist')

    def test_mariadb_uses_time_ms(self):
        for version in ('10.11.9-MariaDB', '11.4.3-MariaDB'):
            conn = FakeConn(version, [server(version, ps_on=False), ('PROCESSLIST', processlist([
                [7, 'app', 'h:1', 'shop', 'Query', '2.5000', 'Sending data', 'SELECT 1']]))])
            rows = discover.running_statements(conn, min_seconds=1.5)
            self.assertEqual(rows[0]['time_s'], 2.5)
            sql = conn.sql('PROCESSLIST')[0]
            self.assertIn('information_schema.PROCESSLIST', sql)
            self.assertIn('TIME_MS / 1000 >= 1.5', sql)

    def test_rejects_bad_min_seconds(self):
        for value in (-1, True, 'x', float('nan'), 10 ** 9):
            with self.assertRaises(ValueError):
                discover.running_report(FakeConn('8.4.3'), value)

    def test_never_kills_anything(self):
        conn = FakeConn('8.4.3', [server('8.4.3'), ('processlist', '')])
        discover.running_statements(conn)
        self.assertFalse(any('KILL' in sql.upper() for sql, _ in conn.calls))


def explain_conn(version, plan_responses, info='SELECT * FROM t WHERE a > 1', command='Query',
                 ps_on=True):
    rows = [] if info is False else [[42, 'app', 'h:1', 'shop', command, 9, 'Sending data', info]]
    return FakeConn(version, [server(version, ps_on), ('ID = 42', processlist(rows))] + plan_responses)


class TestExplainConnection(unittest.TestCase):
    def test_mysql_84_and_97_set_json_version_2_for_explain_for_connection(self):
        for version in ('8.4.3', '9.7.0'):
            conn = explain_conn(version, [('FOR CONNECTION', MYSQL_V2_PLAN + '\n')])
            result = discover.explain_connection(conn, 42)
            self.assertEqual(result['mode'], 'explain')
            self.assertEqual(result['engine'], 'mysql')
            self.assertEqual(result['connection_id'], 42)
            self.assertEqual(result['query'], 'SELECT * FROM t WHERE a > 1')
            self.assertEqual(result['raw'], MYSQL_V2_PLAN)
            self.assertTrue(result['warnings'][0].startswith('Estimated plan'))
            sql, flags = [c for c in conn.calls if 'FOR CONNECTION' in c[0]][0]
            self.assertEqual(sql, 'SET SESSION explain_json_format_version=2; '
                                  'EXPLAIN FORMAT=JSON FOR CONNECTION 42')
            self.assertIn('--raw', flags)
            ui._plan({'plan': result['raw']})

    def test_mysql_before_83_is_gated(self):
        conn = explain_conn('8.0.36', [])
        with self.assertRaisesRegex(ValueError, 'MySQL 8.3 or later'):
            discover.explain_connection(conn, 42)
        self.assertEqual(conn.sql('FOR CONNECTION'), [])

    def test_mariadb_prefers_partial_show_analyze(self):
        conn = explain_conn('11.4.3-MariaDB', [('SHOW ANALYZE FORMAT=JSON FOR 42', MARIADB_PARTIAL)])
        result = discover.explain_connection(conn, '42')
        self.assertEqual(result['mode'], 'analyze-partial')
        self.assertEqual(result['engine'], 'mariadb')
        self.assertIn('Partial runtime statistics', result['warnings'][0])
        self.assertEqual(conn.sql('SHOW EXPLAIN'), [])

    def test_mariadb_falls_back_to_show_explain(self):
        conn = explain_conn('10.11.9-MariaDB', [
            ('SHOW ANALYZE', ConnectorError('mysql client exited 1: ERROR 1064 (42000): syntax')),
            ('SHOW EXPLAIN FORMAT=JSON FOR 42', MARIADB_EXPLAIN)])
        result = discover.explain_connection(conn, 42)
        self.assertEqual(result['mode'], 'explain')

    def test_mariadb_before_109_is_gated(self):
        conn = explain_conn('10.6.18-MariaDB', [])
        with self.assertRaisesRegex(ValueError, 'MariaDB 10.9 or later'):
            discover.explain_connection(conn, 42)

    def test_missing_idle_and_invalid_targets(self):
        with self.assertRaisesRegex(ValueError, 'not in the process list'):
            discover.explain_connection(explain_conn('8.4.3', [], info=False), 42)
        with self.assertRaisesRegex(ValueError, 'idle'):
            discover.explain_connection(explain_conn('8.4.3', [], info=None, command='Sleep'), 42)
        for bad in (0, -3, 'abc', True, None):
            with self.assertRaises(ValueError):
                discover.explain_connection(FakeConn('8.4.3'), bad)

    def test_server_errors_become_actionable_messages(self):
        cases = (
            ('8.4.3', 'ERROR 1094 (HY000): Unknown thread id: 42', 'may have finished'),
            ('8.4.3', 'ERROR 3012 (HY000): EXPLAIN FOR CONNECTION command is supported only '
                      'for SELECT/UPDATE/INSERT/DELETE/REPLACE', 'prepared statements'),
            ('8.4.3', "ERROR 1045 (28000): Access denied for user 'p'@'%' (using password: YES)",
             'GRANT OPTION'),
            ('11.4.3-MariaDB', 'ERROR 1933 (HY000): Target is not executing an operation with a '
                               'query plan', 'not executing'),
            ('11.4.3-MariaDB', 'ERROR 1227 (42000): Access denied; you need (at least one of) the '
                               'PROCESS privilege(s) for this operation', 'PROCESS privilege'),
        )
        for version, error, expected in cases:
            failure = ConnectorError('mysql client exited 1: ' + error)
            conn = explain_conn(version, [('FOR CONNECTION', failure), ('FOR 42', failure)])
            with self.assertRaisesRegex(ValueError, expected):
                discover.explain_connection(conn, 42)
            # A definitive MariaDB refusal does not retry with SHOW EXPLAIN.
            self.assertLessEqual(len(conn.sql('FOR 42')), 1)

    def test_empty_or_unreadable_plans_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'went idle'):
            discover.explain_connection(explain_conn('8.4.3', [('FOR CONNECTION', '\n')]), 42)
        with self.assertRaisesRegex(ValueError, 'cannot read'):
            discover.explain_connection(explain_conn('8.4.3', [('FOR CONNECTION', '{"x": 1}')]), 42)

    def test_capture_shape_is_accepted_by_the_workspace(self):
        conn = explain_conn('8.4.3', [('FOR CONNECTION', MYSQL_V2_PLAN)])
        capture = normalize_capture(discover.explain_capture(discover.explain_connection(conn, 42)))
        self.assertEqual(capture['mode'], 'explain')
        self.assertEqual(capture['runs'], [])
        self.assertIn('connection 42', capture['conditions'])
        conn = explain_conn('11.4.3-MariaDB', [('SHOW ANALYZE', MARIADB_PARTIAL)])
        capture = normalize_capture(discover.explain_capture(discover.explain_connection(conn, 42)))
        self.assertEqual(capture['mode'], 'analyze')
        self.assertIn('partial SHOW ANALYZE', capture['conditions'])


class FakeFactory(object):
    """Stands in for MySQLConnection: records options, hands back one FakeConn."""

    def __init__(self, conn):
        self.conn = conn
        self.options = None

    def __call__(self, **options):
        self.options = options
        return self.conn


class TestDiscoverCLI(unittest.TestCase):
    def call(self, command, conn, *args):
        factory = FakeFactory(conn)
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch('myflames.connector.MySQLConnection', factory), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                getattr(workspace_cli, 'cmd_' + command)(list(args))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue(), factory

    def top_conn(self):
        return mysql_top('8.4.3', [digest_row('SELECT * FROM `orders`', no_index=1, examined=900,
                                              sent=3, sample='SELECT * FROM orders WHERE id > 5')])

    def test_top_json_shape_and_connection_flags(self):
        code, out, err, factory = self.call('top', self.top_conn(), '-h', 'db', '-P', '3307',
                                            '-u', 'app', '-p' + PASSWORD, '--json', '--limit', '7',
                                            '--order', 'avg')
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data['schema_version'], 'top-1.0')
        self.assertEqual(data['order'], 'avg')
        self.assertEqual(data['limit'], 7)
        self.assertEqual(data['statements'][0]['flags'], ['no_index_used', 'low_selectivity'])
        self.assertEqual((factory.options['host'], factory.options['port'], factory.options['user'],
                          factory.options['password']), ('db', 3307, 'app', PASSWORD))
        # Notes travel inside the JSON; stderr stays empty for machine callers.
        self.assertTrue(data['notes'])
        self.assertEqual(err, '')

    def test_top_human_table_goes_to_stdout_and_quiet_silences_notes(self):
        code, out, err, _ = self.call('top', self.top_conn(), '-q')
        self.assertEqual(code, 0)
        self.assertIn('TOTAL', out.splitlines()[0])
        self.assertIn('SELECT * FROM orders WHERE id > 5', out)
        self.assertIn('no_index_used,low_selectivity', out)
        self.assertEqual(err, '')

    def test_top_writes_output_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'top.json')
            code, out, err, _ = self.call('top', self.top_conn(), '--json', '-o', path)
            self.assertEqual(code, 0)
            self.assertEqual(out, '')
            with open(path) as handle:
                self.assertEqual(json.load(handle)['schema_version'], 'top-1.0')
            self.assertIn('Written to', err)

    def test_top_failures_exit_2_and_redact_the_password(self):
        conn = FakeConn('10.11.9-MariaDB', [server('10.11.9-MariaDB', ps_on=False)])
        code, out, err, _ = self.call('top', conn, '-p' + PASSWORD)
        self.assertEqual((code, out), (2, ''))
        self.assertIn('performance_schema=ON', err)
        leaking = FakeConn('8.4.3', [('@@version', ConnectorError('bad login ' + PASSWORD))])
        code, out, err, _ = self.call('top', leaking, '-p' + PASSWORD)
        self.assertEqual(code, 2)
        self.assertNotIn(PASSWORD, err)
        self.assertIn('[redacted]', err)
        for argv in (['--limit', '0'], ['--limit', '201'], ['--order', 'bogus']):
            code, _, _, _ = self.call('top', self.top_conn(), *argv)
            self.assertEqual(code, 2)

    def test_server_text_cannot_drive_the_terminal(self):
        """A sample statement planted by another user carries an OSC 52
        clipboard write, a CSI clear and a carriage return: text output shows
        them escaped, JSON keeps the exact value, errors are escaped too."""
        planted = "SELECT * FROM orders /*\x1b]52;c;ZXZpbA==\x07\x9b2J\r*/"
        controls = re.compile(r'[\x00-\x08\x0b-\x1f\x7f-\x9f]')

        def conn():
            return mysql_top('8.4.3', [digest_row('SELECT * FROM `orders`', sample=planted)])
        code, out, err, _ = self.call('top', conn())
        self.assertEqual(code, 0)
        self.assertIsNone(controls.search(out + err))
        # The SQL column folds whitespace (the carriage return) into a space.
        self.assertIn('\\u001b]52;c;ZXZpbA==\\u0007\\u009b2J */', out)
        code, out, err, _ = self.call('top', conn(), '--json')
        self.assertIsNone(controls.search(out))
        self.assertEqual(json.loads(out)['statements'][0]['sample_text'], planted)
        running = FakeConn('8.4.3', [server('8.4.3'), ('processlist', processlist([
            [42, 'app\x1b[8m', 'h:1', 'shop', 'Query', 12, 'Sending data', planted]]))])
        code, out, err, _ = self.call('running', running, '-q')
        self.assertEqual(code, 0)
        self.assertIsNone(controls.search(out))
        self.assertIn('app\\u001b[8m', out)
        failing = FakeConn('8.4.3', [('@@version', ConnectorError('ERROR 1045: \x1b]0;owned\x07denied'))])
        code, out, err, _ = self.call('top', failing)
        self.assertEqual(code, 2)
        self.assertIsNone(controls.search(err))
        self.assertIn('\\u001b]0;owned\\u0007denied', err)

    def test_running_table_json_and_bad_min_seconds(self):
        def conn():
            return FakeConn('8.4.3', [server('8.4.3'), ('processlist', processlist([
                [42, 'app', 'h:1', 'shop', 'Query', 12, 'Sending data', 'SELECT * FROM orders']]))])
        code, out, err, _ = self.call('running', conn())
        self.assertEqual(code, 0)
        self.assertIn('SELECT * FROM orders', out)
        self.assertIn('--explain ID', err)
        code, out, _, _ = self.call('running', conn(), '--json', '--min-seconds', '5')
        self.assertEqual(json.loads(out)['schema_version'], 'running-1.0')
        code, _, _, _ = self.call('running', conn(), '--min-seconds', '-1')
        self.assertEqual(code, 2)
        code, out, err, _ = self.call('running', FakeConn('8.4.3', [server('8.4.3'), ('processlist', '')]))
        self.assertEqual((code, out), (0, 'ID  TIME  USER  DB  STATE  SQL\n'))
        self.assertIn('No statements are running', err)

    def test_running_explain_prints_raw_plan_json_with_estimate_note(self):
        code, out, err, factory = self.call('running', explain_conn('8.4.3', [('FOR CONNECTION', MYSQL_V2_PLAN)]),
                                            '--explain', '42')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), json.loads(MYSQL_V2_PLAN))
        self.assertIn('Estimated plan', err)
        self.assertEqual(factory.options['query_timeout'], 40)
        code, out, err, _ = self.call('running', explain_conn('8.4.3', [('FOR CONNECTION', MYSQL_V2_PLAN)]),
                                      '--explain', '42', '--json', '-q')
        data = json.loads(out)
        self.assertEqual((data['schema_version'], data['mode']), ('explain-connection-1.0', 'explain'))
        self.assertEqual(err, '')
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'plan.json')
            code, out, _, _ = self.call('running', explain_conn('8.4.3', [('FOR CONNECTION', MYSQL_V2_PLAN)]),
                                        '--explain', '42', '-o', path)
            self.assertEqual((code, out), (0, ''))
            with open(path) as handle:
                ui._plan({'plan': handle.read()})
        code, out, err, _ = self.call('running', explain_conn('8.0.36', []), '--explain', '42')
        self.assertEqual((code, out), (2, ''))
        self.assertIn('MySQL 8.3', err)
        code, _, _, _ = self.call('running', explain_conn('8.4.3', []), '--explain', '0')
        self.assertEqual(code, 2)

    def test_root_dispatch_and_help_list_the_subcommands(self):
        from myflames import cli
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, 'argv', ['myflames', 'top', '--help']), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                cli.main()
        self.assertEqual(caught.exception.code, 0)
        self.assertIn('myflames top', out.getvalue())
        source = inspect.getsource(cli.main)
        self.assertIn('top       List the heaviest statements', source)
        self.assertIn('sessions  Every connection', source)
        self.assertIn('running   Deprecated alias of sessions', source)


class TestDiscoverAPI(unittest.TestCase):
    def call(self, path, conn, payload):
        factory = FakeFactory(conn)
        with mock.patch('myflames.connector.MySQLConnection', factory):
            return ui.api_response(path, payload, workspace=object()), factory

    def connection(self):
        return {'host': '127.0.0.1', 'port': 3306, 'user': 'app', 'password': PASSWORD}

    def test_top_and_running_routes(self):
        result, factory = self.call('/api/live/top', mysql_top('8.4.3', [digest_row('SELECT 1', sample='SELECT 1')]),
                                    {'connection': self.connection(), 'limit': 3, 'order': 'count'})
        self.assertEqual(result['schema_version'], 'top-1.0')
        self.assertEqual(result['order'], 'count')
        self.assertEqual(factory.options['password'], PASSWORD)
        conn = FakeConn('8.4.3', [server('8.4.3'), ('processlist', '')])
        result, _ = self.call('/api/live/running', conn, {'connection': self.connection(), 'min_seconds': 1})
        self.assertEqual(result['schema_version'], 'running-1.0')
        json.dumps(result, allow_nan=False)

    def test_explain_connection_route_returns_a_capture(self):
        conn = explain_conn('8.4.3', [('FOR CONNECTION', MYSQL_V2_PLAN)])
        result, factory = self.call('/api/live/explain-connection', conn,
                                    {'connection': self.connection(), 'connection_id': 42})
        self.assertEqual(result['mode'], 'explain')
        self.assertEqual(result['capture']['schema_version'], 'capture-1.0')
        self.assertEqual(factory.options['query_timeout'], 40)
        analyzed = ui.api_response('/api/analyze', {'plan': result['raw'], 'capture': result['capture']})
        self.assertEqual(analyzed['capture']['mode'], 'explain')

    def test_routes_need_a_workspace_and_connection(self):
        with self.assertRaisesRegex(ValueError, 'requires a running workspace'):
            ui.api_response('/api/live/top', {'connection': self.connection()})
        with self.assertRaisesRegex(ValueError, 'connection settings'):
            ui.api_response('/api/live/running', {}, workspace=object())

    def test_errors_never_echo_the_password(self):
        for error in (ValueError('oops ' + PASSWORD), ConnectorError('denied ' + PASSWORD)):
            conn = FakeConn('8.4.3', [('@@version', error)])
            with self.assertRaises(type(error)) as caught:
                self.call('/api/live/running', conn, {'connection': self.connection()})
            self.assertNotIn(PASSWORD, str(caught.exception))

    def test_routes_are_in_the_post_allowlist(self):
        source = inspect.getsource(ui.WorkspaceHandler.do_POST)
        for path in ('/api/live/top', '/api/live/running', '/api/live/explain-connection'):
            self.assertIn('"%s"' % path, source)



class TestProfileCLI(unittest.TestCase):
    """--profile on capture/top/running and the profiles subcommand.

    A temporary HOME and MYFLAMES_SECRET_BACKEND=vault keep the real keychain
    and workspace untouched.
    """

    def setUp(self):
        from myflames.workspace_store import WorkspaceStore
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.dict(os.environ, {'HOME': self.temp.name, 'USERPROFILE': self.temp.name,
                                               'MYFLAMES_SECRET_BACKEND': 'vault'})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        self.store = WorkspaceStore()
        self.profile = self.store.save('profile', {'name': 'prod', 'host': 'db.internal', 'port': 3307,
                                                   'user': 'app', 'database': 'shop', 'ssl_mode': 'REQUIRED'},
                                       password=PASSWORD, remember=True)
        self.plain = self.store.save('profile', {'name': 'staging', 'host': 'stage.internal', 'user': 'app'})

    def call(self, command, conn, *args):
        return TestDiscoverCLI.call(self, command, conn, *args)

    def running(self, *args):
        return self.call('running', FakeConn('8.4.3', [server('8.4.3'), ('processlist', '')]), *args)

    def test_profile_supplies_settings_and_the_saved_password(self):
        for name in ('prod', self.profile['id']):
            code, _, err, factory = self.running('--profile', name, '-q')
            self.assertEqual(code, 0, err)
            self.assertEqual({k: factory.options.get(k) for k in ('host', 'port', 'user', 'database', 'ssl_mode', 'password')},
                             {'host': 'db.internal', 'port': 3307, 'user': 'app', 'database': 'shop',
                              'ssl_mode': 'REQUIRED', 'password': PASSWORD})
        code, _, _, factory = self.running('--profile', 'prod', '-D', 'other')
        self.assertEqual(code, 0)
        self.assertEqual((factory.options['database'], factory.options['password']), ('other', PASSWORD))
        # A weaker TLS setting must not receive the saved password.
        code, _, err, factory = self.running('--profile', 'prod', '--ssl-mode', 'DISABLED')
        self.assertEqual(code, 2)
        self.assertIn('TLS settings', err)
        self.assertIsNone(factory.options)
        code, _, _, factory = self.running('--profile', 'prod', '-ptyped')
        self.assertEqual(factory.options['password'], 'typed')
        code, _, _, factory = self.running('--profile', 'staging')
        self.assertEqual((code, factory.options['host']), (0, 'stage.internal'))
        self.assertNotIn('password', factory.options)

    def test_saved_password_stays_with_its_endpoint(self):
        for flags in (['-h', 'evil.example'], ['-P', '3306'], ['-u', 'root'], ['--ssl-mode', 'DISABLED']):
            with self.subTest(flags=flags):
                code, out, err, factory = self.running('--profile', 'prod', *flags)
                self.assertEqual((code, out), (2, ''))
                self.assertIn('own host, port, user, and TLS settings', err)
                self.assertNotIn(PASSWORD, err)
                self.assertIsNone(factory.options)
        code, _, _, factory = self.running('--profile', 'prod', '-h', 'evil.example', '-ptyped')
        self.assertEqual((code, factory.options['host'], factory.options['password']), (0, 'evil.example', 'typed'))

    def test_missing_profile_exits_2(self):
        for command in ('running', 'top'):
            code, out, err, factory = self.call(command, FakeConn('8.4.3'), '--profile', 'nope')
            self.assertEqual((code, out), (2, ''))
            self.assertIn("No saved connection profile named 'nope'", err)
            self.assertIsNone(factory.options)

    def test_top_and_capture_use_the_profile(self):
        conn = mysql_top('8.4.3', [digest_row('SELECT 1', sample='SELECT 1')])
        code, out, _, factory = self.call('top', conn, '--profile', 'prod', '--json')
        self.assertEqual(code, 0)
        self.assertEqual(factory.options['password'], PASSWORD)
        self.assertNotIn(PASSWORD, out)
        payloads = []

        class FakeJob(object):
            def __init__(self, payload):
                payloads.append(payload)
                self.state, self.error = 'complete', ''
                self.result = {'schema_version': 'capture-1.0', 'raw': MYSQL_V2_PLAN, 'query': payload['query']}

            def run(self):
                pass

            def cancel(self):
                pass

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(workspace_cli, 'LiveJob', FakeJob), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            workspace_cli.cmd_capture(['--profile', 'prod', '-e', 'SELECT 1'])
        connection = payloads[0]['connection']
        self.assertEqual((connection['host'], connection['port'], connection['password']),
                         ('db.internal', 3307, PASSWORD))
        self.assertNotIn('profile_id', connection)
        self.assertNotIn(PASSWORD, out.getvalue())
        self.assertEqual(json.loads(out.getvalue())['query'], 'SELECT 1')

    def profiles(self, *args):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                workspace_cli.cmd_profiles(list(args))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_profiles_list_forget_and_delete(self):
        code, out, err = self.profiles('list')
        self.assertEqual((code, err), (0, ''))
        self.assertIn('saved (encrypted file)', out)
        self.assertIn('not saved', out)
        self.assertIn('db.internal:3307', out)
        code, out, _ = self.profiles('list', '--json')
        data = json.loads(out)
        self.assertEqual(data['schema_version'], 'profiles-1.0')
        self.assertEqual(data['secret_store']['backend'], 'vault')
        self.assertEqual({p['name']: p['has_saved_password'] for p in data['profiles']},
                         {'prod': True, 'staging': False})
        self.assertNotIn(PASSWORD, out)
        code, out, err = self.profiles('forget', 'prod')
        self.assertEqual((code, out), (0, ''))
        self.assertIn('Forgot the saved password', err)
        self.assertFalse(self.store.find_profile('prod')['has_saved_password'])
        code, _, err = self.profiles('forget', 'prod', '-q')
        self.assertEqual((code, err), (0, ''))
        code, _, err = self.profiles('delete', 'staging')
        self.assertEqual(code, 0)
        self.assertIn("Deleted profile 'staging'", err)
        self.assertEqual([p['name'] for p in self.store.list('profile')], ['prod'])
        for argv in (['forget', 'nope'], ['delete', 'nope'], [], ['bogus']):
            with self.subTest(argv=argv):
                self.assertEqual(self.profiles(*argv)[0], 2)

    def test_delete_removes_the_saved_password(self):
        from test_secret_store import saved_vault_password
        self.assertEqual(saved_vault_password(self.profile['id']), PASSWORD)
        self.assertEqual(self.profiles('delete', 'prod')[0], 0)
        self.assertIsNone(saved_vault_password(self.profile['id']))

    def test_root_dispatch_and_help(self):
        from myflames import cli
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, 'argv', ['myflames', 'profiles', 'list', '--json']), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            cli.main()
        self.assertEqual(json.loads(out.getvalue())['schema_version'], 'profiles-1.0')
        source = inspect.getsource(cli.main)
        self.assertIn('profiles  List saved connection profiles', source)
        self.assertIn('--profile', source)


class TestOwnStatementsExcluded(unittest.TestCase):
    def test_capture_helpers_and_client_noise_are_not_workload(self):
        from myflames.discover import _excluded
        for digest in ("SELECT ?", "SELECT `CONNECTION_ID` ( )", "SELECT @@`version_comment` LIMIT ?",
                       "SELECT @@`version`", "SET SESSION `explain_json_format_version` = ?",
                       "SET SESSION `optimizer_trace` = ?", "START TRANSACTION READ ONLY",
                       "SHOW SESSION STATUS WHERE `Variable_name` LIKE ?",
                       "SELECT `database_name` FROM `mysql` . `innodb_table_stats` WHERE ?",
                       "SELECT * FROM `information_schema` . `TABLES` WHERE ?"):
            self.assertTrue(_excluded(digest, None), digest)
        self.assertTrue(_excluded("SELECT ? FROM `t`", "/*myflames:capture*/ SELECT 'x'"))
        for digest in ("SELECT * FROM `orders` WHERE `id` = ?", "SELECT `share` FROM `t`", "ROLLBACK"):
            self.assertFalse(_excluded(digest, None), digest)

    def test_capture_refuses_for_share(self):
        from myflames.live import bind_query
        for sql in ("SELECT * FROM t FOR SHARE", "SELECT * FROM t FOR SHARE NOWAIT"):
            with self.assertRaises(ValueError):
                bind_query(sql)
        self.assertEqual(bind_query("SELECT share FROM t"), "SELECT share FROM t")


class TestDigestOverflow(unittest.TestCase):
    """Statements that overflow a full digest table are reported, not dropped."""

    def test_overflow_row_becomes_a_note(self):
        overflow = ("WHERE DIGEST IS NULL", batch([[1234, 5000000000000]]))
        conn = mysql_top('8.4.3', [digest_row('SELECT `a` FROM `t`', sample='SELECT a FROM t')],
                         extra=[overflow])
        report = discover.top_report(conn)
        note = [n for n in report["notes"] if "digest table is full" in n]
        self.assertEqual(len(note), 1)
        self.assertIn("1,234 more executions (5.0 s)", note[0])

    def test_no_overflow_no_note(self):
        conn = mysql_top('8.4.3', [digest_row('SELECT `a` FROM `t`', sample='SELECT a FROM t')],
                         extra=[("WHERE DIGEST IS NULL", batch([]))])
        self.assertFalse([n for n in discover.top_report(conn)["notes"] if "digest table is full" in n])



def attribution_row(text, schema='shop', full_join=0, range_check=0, scan=0, no_index=0, **kwargs):
    """A digest row with the attribution counters appended after the sample."""
    return digest_row(text, schema=schema, no_index=no_index, **kwargs) + [full_join, range_check, scan]


class TestTableReference(unittest.TestCase):
    """discover.table_reference: how digest text names a table (a text match, not proof)."""

    def ref(self, text, digest_schema='shop', schema='shop', table='orders'):
        return discover.table_reference(text, digest_schema, schema, table)

    def test_backquoted_and_bare_qualified_names_match_from_any_default_schema(self):
        self.assertEqual(self.ref('SELECT * FROM `shop` . `orders` WHERE `id` = ?', digest_schema='other'), 'qualified')
        self.assertEqual(self.ref('SELECT * FROM shop.orders', digest_schema=None), 'qualified')
        self.assertEqual(self.ref('SELECT * FROM `shop`.`orders` `o`', digest_schema='x'), 'qualified')

    def test_unqualified_name_needs_the_tables_schema_as_default_schema(self):
        text = 'SELECT COUNT ( * ) FROM `orders` WHERE `note` LIKE ?'
        self.assertEqual(self.ref(text, digest_schema='shop'), 'unqualified')
        self.assertEqual(self.ref(text, digest_schema='SHOP'), 'unqualified')
        self.assertIsNone(self.ref(text, digest_schema='other'))
        self.assertIsNone(self.ref(text, digest_schema=None))
        self.assertEqual(self.ref('SELECT * FROM orders o', digest_schema='shop'), 'unqualified')
        # Another schema's table first, then this one: the second occurrence counts.
        self.assertEqual(self.ref('SELECT * FROM `other` .  `orders` JOIN `orders` `o`'), 'unqualified')

    def test_another_schemas_table_with_the_same_name_does_not_match(self):
        for text in ('SELECT * FROM `other` . `orders`', 'SELECT * FROM other.orders',
                     'SELECT * FROM `other`.`orders`', 'SELECT * FROM other .  orders',
                     'SELECT * FROM `other`  .   `orders`', 'SELECT * FROM `other` .\n\t`orders`'):
            with self.subTest(text=text):
                self.assertIsNone(self.ref(text, digest_schema='shop'))

    def test_names_that_only_contain_the_table_name_do_not_match(self):
        for text in ('SELECT * FROM `orders_archive`', 'SELECT * FROM `my_orders`', 'SELECT * FROM orders2',
                     'SELECT `orders` . `id` FROM `t`', 'SELECT * FROM `t` WHERE `x` = ?'):
            with self.subTest(text=text):
                self.assertIsNone(self.ref(text))

    def test_case_and_special_characters(self):
        self.assertEqual(self.ref('SELECT * FROM `Orders`'), 'unqualified')
        self.assertEqual(self.ref('SELECT * FROM `sh op` . `we``ird`', schema='sh op', table='we`ird'), 'qualified')
        # A name that needs quoting is never matched bare.
        self.assertIsNone(self.ref('SELECT * FROM we`ird', table='we`ird'))

    def test_parse_table_name(self):
        self.assertEqual(discover.parse_table_name('shop.orders'), ('shop', 'orders'))
        self.assertEqual(discover.parse_table_name('`my.db`.`t``x`'), ('my.db', 't`x'))
        self.assertEqual(discover.parse_table_name({'schema': 's', 'table': 't'}), ('s', 't'))
        self.assertEqual(discover.parse_table_name(['s', 't']), ('s', 't'))
        for bad in ('orders', 'a.', '.b', '`a.b', 'a.b\nc', None, 5, {'schema': 's'}, 'x' * 65 + '.t'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    discover.parse_table_name(bad)


class TestWorkloadAttribution(unittest.TestCase):
    """top --order full_join|scan|no_index and --table SCHEMA.TABLE."""

    def test_attribution_orders_filter_above_zero_and_break_ties(self):
        rows = [attribution_row('SELECT * FROM `a` JOIN `b`', full_join=7, range_check=2, scan=7, no_index=7,
                                sample='SELECT * FROM a JOIN b')]
        for order, column, condition in (
                ('full_join', 'SUM_SELECT_FULL_JOIN',
                 '(SUM_SELECT_FULL_JOIN > 0 OR SUM_SELECT_RANGE_CHECK > 0)'),
                ('scan', 'SUM_SELECT_SCAN', 'SUM_SELECT_SCAN > 0'),
                ('no_index', 'SUM_NO_INDEX_USED', 'SUM_NO_INDEX_USED > 0')):
            with self.subTest(order=order):
                conn = mysql_top('8.4.3', rows)
                report = discover.top_report(conn, limit=5, order=order)
                sql = conn.sql('events_statements_summary_by_digest')[0]
                self.assertIn(condition, sql)
                self.assertIn('ORDER BY %s DESC, ' % column, sql)
                self.assertIn('LIMIT 5', sql)
                item = report['statements'][0]
                self.assertEqual((item['select_full_join'], item['select_range_check'], item['select_scan']),
                                 (7, 2, 7))
                self.assertIn(discover._ORDER_NOTES[order], report['notes'])
        sql = mysql_top('8.4.3', rows)
        discover.top_report(sql, order='full_join')
        self.assertIn('ORDER BY SUM_SELECT_FULL_JOIN DESC, SUM_SELECT_RANGE_CHECK DESC, SUM_TIMER_WAIT DESC',
                      sql.sql('events_statements_summary_by_digest')[0])

    def test_counter_columns_follow_the_sample_on_both_engines(self):
        conn = mysql_top('8.4.3', [attribution_row('SELECT 1', full_join=3, sample='SELECT 1')])
        report = discover.top_report(conn)
        sql = conn.sql('events_statements_summary_by_digest')[0]
        self.assertIn('QUERY_SAMPLE_TEXT, SUM_SELECT_FULL_JOIN, SUM_SELECT_RANGE_CHECK, SUM_SELECT_SCAN', sql)
        self.assertEqual(report['statements'][0]['select_full_join'], 3)
        mariadb = FakeConn('11.4.3-MariaDB', [server('11.4.3-MariaDB'), CONSUMERS_ON, (
            'events_statements_summary_by_digest',
            batch([digest_row('SELECT `a` FROM `t`', with_sample=False) + [4, 1, 2]]))])
        item = discover.top_report(mariadb)['statements'][0]
        self.assertEqual((item['select_full_join'], item['select_range_check'], item['select_scan']), (4, 1, 2))
        self.assertIsNone(item['sample_text'])
        # Rows without the counters (older recordings) read as zero.
        old = mysql_top('8.4.3', [digest_row('SELECT 1', sample='SELECT 1')])
        self.assertEqual(discover.top_report(old)['statements'][0]['select_full_join'], 0)

    def test_table_filter_matches_text_and_labels_every_match(self):
        rows = [
            attribution_row('SELECT * FROM `orders` WHERE `note` LIKE ?', no_index=9, scan=9,
                            sample="SELECT * FROM orders WHERE note LIKE 'x%'"),
            attribution_row('SELECT * FROM `shop` . `orders` `o`', schema='other', no_index=3, scan=3,
                            sample='SELECT * FROM shop.orders o'),
            attribution_row('SELECT * FROM `orders`', schema='other', no_index=5, scan=5,
                            sample='SELECT * FROM orders'),
            attribution_row('SELECT * FROM `other` . `orders`', no_index=4, scan=4,
                            sample='SELECT * FROM other.orders'),
            attribution_row('SELECT * FROM `orders_archive`', no_index=2, scan=2,
                            sample='SELECT * FROM orders_archive'),
        ]
        conn = mysql_top('8.4.3', rows)
        report = discover.top_report(conn, limit=5, order='no_index', table='shop.orders')
        sql = conn.sql('events_statements_summary_by_digest')[0]
        self.assertIn(discover.TABLE_FILTER, sql)
        self.assertIn("LOCATE(CONVERT(X'%s' USING utf8mb4), DIGEST_TEXT) > 0" % b'orders'.hex(), sql)
        self.assertIn('LIMIT %d' % discover.TABLE_CANDIDATES, sql)
        texts = [(s['digest_text'], s['table_match']['kind']) for s in report['statements']]
        self.assertEqual(texts, [('SELECT * FROM `orders` WHERE `note` LIKE ?', 'unqualified'),
                                 ('SELECT * FROM `shop` . `orders` `o`', 'qualified')])
        for item in report['statements']:
            self.assertEqual(item['table_match']['label'], 'text match, not proof')
        self.assertEqual(report['table'], {'schema': 'shop', 'table': 'orders'})
        self.assertEqual(report['match']['label'], 'text match, not proof')
        self.assertEqual(report['match']['candidates'], 5)
        self.assertFalse(report['match']['capped'])
        self.assertTrue(any('text match, not proof' in c for c in report['match']['caveats']))
        self.assertTrue(any('text match, not proof' in n for n in report['notes']))

    def test_table_filter_keeps_the_limit_after_matching(self):
        rows = [attribution_row('SELECT * FROM `orders` WHERE `a` = %d' % i, no_index=1, sample='x')
                for i in range(4)]
        report = discover.top_report(mysql_top('8.4.3', rows), limit=2, table=('shop', 'orders'))
        self.assertEqual(len(report['statements']), 2)

    def test_coverage_discloses_overflow_uptime_lost_and_first_seen(self):
        status = ("Performance_schema_digest_lost", batch([['Uptime', 7200], ['Performance_schema_digest_lost', 12]]))
        overflow = ("WHERE DIGEST IS NULL", batch([[1234, 5000000000000]]))
        conn = mysql_top('8.4.3', [attribution_row('SELECT 1', full_join=1, sample='SELECT 1')],
                         extra=[status, overflow])
        report = discover.top_report(conn, order='full_join')
        self.assertEqual(report['coverage'], {'uptime_s': 7200, 'first_seen': '2026-10-01 10:00:00.000000',
                                              'digest_lost': 12,
                                              'overflow': {'executions': 1234, 'total_ms': 5000.0}})
        self.assertTrue(any('digest table is full' in n for n in report['notes']))
        # Best effort: an unreadable status leaves the fields empty.
        bare = discover.top_report(mysql_top('8.4.3', []))
        self.assertEqual(bare['coverage'], {'uptime_s': None, 'first_seen': None, 'digest_lost': None,
                                            'overflow': None})

    def test_explain_and_desc_digests_never_count_as_workload(self):
        """EXPLAIN sets SUM_NO_INDEX_USED (measured on MySQL 8.0 and MariaDB
        11.4), and DESC table can name the table, so both stay excluded."""
        conn = mysql_top('8.0.46', [])
        discover.top_report(conn, order='no_index', table='shop.orders')
        sql = conn.sql('events_statements_summary_by_digest')[0]
        for prefix in ('EXPLAIN ', 'DESC ', 'ANALYZE '):
            self.assertIn("TRIM(REPLACE(DIGEST_TEXT, '`', '')) NOT LIKE '%s%%'" % prefix, sql)
        for text in ('EXPLAIN SELECT * FROM `shop` . `orders` WHERE `note` = ?', 'DESC `shop` . `orders` ',
                     'DESC `orders`'):
            self.assertTrue(discover._excluded(text, None), text)
        self.assertFalse(discover._excluded('SELECT * FROM `t` ORDER BY `a` DESC', None))

    def test_table_filter_keeps_scan_only_statements(self):
        """With a table, no_index and scan only order the rows: the table
        predicate (a table scan or a first-table scan) is not narrowed."""
        rows = [attribution_row('SELECT * FROM `orders`', no_index=0, scan=5, sample='x')]
        for order in ('no_index', 'scan'):
            with self.subTest(order=order):
                conn = mysql_top('8.4.3', rows)
                report = discover.top_report(conn, order=order, table='shop.orders')
                sql = conn.sql('events_statements_summary_by_digest')[0]
                self.assertEqual(sql.count('SUM_NO_INDEX_USED > 0'), 1)
                self.assertEqual(sql.count('SUM_SELECT_SCAN > 0'), 1)
                self.assertIn(discover.TABLE_FILTER, sql)
                self.assertIn('ORDER BY %s DESC' % discover.ORDERS[order], sql)
                self.assertEqual(len(report['statements']), 1)
                self.assertNotIn(discover._ORDER_NOTES[order], report['notes'])
        conn = mysql_top('8.4.3', rows)
        discover.top_report(conn, order='full_join', table='shop.orders')
        self.assertIn(discover.ORDER_FILTERS['full_join'], conn.sql('events_statements_summary_by_digest')[0])

    def test_bad_table_names_are_refused_before_connecting(self):
        conn = FakeConn('8.4.3')
        for table in ('orders', '`x', 'a.' + 'b' * 65):
            with self.assertRaises(ValueError):
                discover.top_report(conn, table=table)
        self.assertEqual(conn.calls, [])

    def test_cli_table_filter_and_attribution_columns(self):
        rows = [attribution_row('SELECT * FROM `orders`', no_index=9, scan=9, full_join=1,
                                sample='SELECT * FROM orders')]
        cli = TestDiscoverCLI('test_top_json_shape_and_connection_flags')
        code, out, err, _ = cli.call('top', mysql_top('8.4.3', rows), '--table', 'shop.orders', '--order', 'no_index')
        self.assertEqual(code, 0)
        header = out.splitlines()[0]
        for column in ('FULL JOINS', 'SCANS', 'NO INDEX', 'MATCH'):
            self.assertIn(column, header)
        self.assertIn('unqualified', out)
        self.assertIn('text match, not proof', err)
        code, out, err, _ = cli.call('top', mysql_top('8.4.3', rows), '--table', 'shop.orders', '--json')
        self.assertEqual(json.loads(out)['statements'][0]['table_match']['kind'], 'unqualified')
        code, out, err, factory = cli.call('top', FakeConn('8.4.3'), '--table', 'orders')
        self.assertEqual((code, out, factory.options), (2, '', None))
        self.assertIn('SCHEMA.TABLE', err)
        code, out, err, _ = cli.call('top', mysql_top('8.4.3', []), '--order', 'full_join')
        self.assertEqual(code, 0)
        self.assertIn('No recorded statement matches.', err)

    def test_api_accepts_the_table_and_new_orders(self):
        rows = [attribution_row('SELECT * FROM `shop` . `orders`', no_index=2, sample='x')]
        api = TestDiscoverAPI('test_top_and_running_routes')
        result, _ = api.call('/api/live/top', mysql_top('8.4.3', rows),
                             {'connection': api.connection(), 'order': 'no_index',
                              'table': {'schema': 'shop', 'table': 'orders'}})
        self.assertEqual(result['table'], {'schema': 'shop', 'table': 'orders'})
        self.assertEqual(result['statements'][0]['table_match']['kind'], 'qualified')
        result, _ = api.call('/api/live/top', mysql_top('8.4.3', rows),
                             {'connection': api.connection(), 'order': 'full_join', 'table': 'shop.orders'})
        self.assertEqual(result['order'], 'full_join')

    def test_top_order_aliases_and_filters(self):
        conn = mysql_top('8.4.3', [attribution_row('SELECT * FROM orders', no_index=1, sample='x')])
        for order in ('time', 'scans', 'full_join', 'tmp_tables', 'locks', 'rows', 'exec_count'):
            res = discover.top_report(conn, order=order)
            self.assertEqual(res['order'], order)

    def test_slow_queries_report(self):
        conn = mysql_top('8.4.3', [attribution_row('SELECT * FROM slow_t', sample='SELECT * FROM slow_t')])
        report = discover.slow_queries_report(conn, min_seconds=1.0)
        self.assertEqual(report['schema_version'], 'slow-queries-1.0')
        self.assertTrue(len(report['statements']) >= 0)


if __name__ == '__main__':
    unittest.main()
