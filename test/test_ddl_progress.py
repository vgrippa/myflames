"""DDL reads use synthetic counters/configuration, never a database write."""
import ast
import json
import os
import sys
import unittest
from unittest import mock

from myflames import ddl_progress as ddl, sessions, ui
from myflames.connector import ConnectorError
from myflames.perf_common import Server
from _perf_replay import FakeFactory, ReplayConn, batch, run_cli


def settings(engine='mysql', off=(), timed_off=()):
    return ([['consumer', None, name, 'NO' if name in off else 'YES'] for name in ddl.CONSUMERS] +
            [['instrument', None, name, 'NO' if name in off else 'YES', 'NO' if name in timed_off else 'YES']
             for name in ddl.STAGES if engine != 'mariadb' or not name.endswith('(flush)')])


def server(engine='mysql', ps=True):
    return Server({'version': '11.4.13-MariaDB' if engine == 'mariadb' else '8.4.11',
                   'performance_schema': 'ON' if ps else 'OFF'}, {})


class Reader(object):
    def __init__(self, rows=(), error=None):
        self.result, self.error, self.calls = rows, error, []

    def rows(self, sql, timeout=None):
        self.calls.append((sql, timeout))
        if self.error:
            raise self.error
        return self.result


def stage(completed=42, estimated=100, elapsed=126, name='stage/sql/copy to tmp table', cid=42):
    return ['stage', cid, name, completed, estimated, elapsed, '/* comment */ ALTER TABLE t ADD x INT']


class TestDdlProgress(unittest.TestCase):
    def test_mysql_whole_statement_fraction_eta_and_exact_roundtrip(self):
        reader = Reader(settings() + [stage(name='stage/innodb/alter table (merge sort)')])
        result = ddl.read_progress(reader, server())
        item = result['items'][0]
        self.assertTrue(result['available'])
        self.assertEqual(result['missing'], [])
        self.assertEqual((item['percent'], item['left_s'], item['elapsed_s']), (42, 174, 126))
        self.assertEqual(item['statement_kind'], 'ALTER')
        self.assertEqual(item['units'], 'work_units')
        self.assertEqual(item['stage_label'], 'Merging sorted data')
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)
        self.assertEqual(len(reader.calls), 1)
        sql, timeout = reader.calls[0]
        self.assertEqual(timeout, 5)
        self.assertTrue(sql.startswith('SELECT '))
        self.assertNotIn(';', sql)
        self.assertIn('s.END_EVENT_ID IS NULL', sql)
        self.assertIn('ON t.THREAD_ID = s.THREAD_ID', sql)
        self.assertIn('t.PROCESSLIST_TIME', sql)
        self.assertIn('LIMIT 10011', sql)

    def test_copy_row_estimate_overrun_never_claims_complete_or_negative_eta(self):
        for completed in (100, 123):
            with self.subTest(completed=completed):
                item = ddl.read_progress(Reader(settings() + [stage(completed)]), server())['items'][0]
                self.assertIsNone(item['percent'])
                self.assertIsNone(item['left_s'])
                self.assertIn('no estimate yet', item['note'])
                self.assertEqual(item['units'], 'rows')
                self.assertEqual(item['completed'], completed)

    def test_estimate_that_follows_the_work_has_no_percentage(self):
        """Seen live on MySQL 8.4 with stale statistics: WORK_ESTIMATED equals
        WORK_COMPLETED at every read because InnoDB keeps max(formula, completed)."""
        for completed in (32, 4596, 25666):
            row = stage(completed, completed, name='stage/innodb/alter table (read PK and internal sort)')
            item = ddl.read_progress(Reader(settings() + [row]), server())['items'][0]
            self.assertIsNone(item['percent'])
            self.assertIsNone(item['left_s'])
            self.assertTrue(item['note'])
        ahead = ddl.read_progress(Reader(settings() + [stage(40)]), server())['items'][0]
        self.assertEqual(ahead['percent'], 40)
        self.assertIsNone(ahead['note'])

    def test_no_time_left_before_one_second(self):
        """Seen live: processlist TIME is 0 in the first second, which made the estimate 0 s."""
        item = ddl.read_progress(Reader(settings() + [stage(12, 100, 0)]), server())['items'][0]
        self.assertEqual(item['percent'], 12)
        self.assertIsNone(item['left_s'])

    def test_zero_unknown_estimate_and_small_samples_have_no_eta(self):
        for completed, estimated, elapsed, percent in (
                (0, 100, 30, 0), (1, 1000, 30, 0.1), (1, 0, 30, None),
                (None, 100, 30, None), (10, 100, None, 10), (10, None, 30, None),
                (-1, 100, 30, None)):
            with self.subTest(completed=completed, estimated=estimated):
                item = ddl.read_progress(Reader(settings() + [stage(completed, estimated, elapsed)]), server())['items'][0]
                self.assertEqual(item['percent'], percent)
                self.assertIsNone(item['left_s'])

    def test_big_work_counts_remain_exact_strings(self):
        count = 2 ** 60 + 1
        item = ddl.read_progress(Reader(settings() + [stage(count, count * 2)]), server())['items'][0]
        self.assertEqual((item['completed'], item['estimated']), (str(count), str(count * 2)))
        self.assertEqual(item['percent'], 50)

    def test_exact_missing_consumers_parents_and_disabled_instruments(self):
        names = ('events_stages_current', 'thread_instrumentation', 'stage/sql/copy to tmp table')
        result = ddl.read_progress(Reader(settings(off=names)), server())
        self.assertFalse(result['available'])
        self.assertEqual(set(result['missing']), set(names))
        for name in names:
            self.assertIn(name, result['reason'])
        # Work counters do not require a timed instrument. ETA uses processlist TIME.
        result = ddl.read_progress(Reader(settings(timed_off=list(ddl.STAGES))), server())
        self.assertTrue(result['available'])
        self.assertEqual(result['missing'], [])

    def test_mysql_ps_off_sends_no_progress_sql(self):
        reader = Reader()
        result = ddl.read_progress(reader, server(ps=False))
        self.assertFalse(result['available'])
        self.assertEqual(result['missing'], ['performance_schema'])
        self.assertIn('restart', result['reason'])
        self.assertEqual(reader.calls, [])

    def test_mariadb_native_combines_one_based_stage_and_percentage(self):
        reader = Reader([['processlist', 42, 'copy to tmp table', 2, 4, 100, 'ALTER TABLE t ADD x INT', 50, 'Query']])
        result = ddl.read_progress(reader, server('mariadb', ps=False))
        item = result['items'][0]
        self.assertEqual(item['percent'], 37.5)
        self.assertEqual(item['left_s'], 166.7)
        self.assertEqual(item['source'], 'processlist_progress')
        self.assertIsNone(item['completed'])
        self.assertIsNone(item['estimated'])
        self.assertIn('not InnoDB in-place ALTER', result['reason'])
        self.assertNotIn('performance_schema.', reader.calls[0][0])

    def test_mariadb_uses_native_for_uncovered_session_but_prefers_ps_for_matching_id(self):
        native = ['processlist', 42, 'copy to tmp table', 1, 2, 100, 'ALTER TABLE t ADD x INT', 10, 'Query']
        result = ddl.read_progress(Reader(settings('mariadb') + [stage()] + [native, native[:1] + [43] + native[2:]]),
                                   server('mariadb'))
        self.assertEqual([(i['id'], i['percent'], i['source']) for i in result['items']],
                         [(42, 42, 'performance_schema'), (43, 5, 'processlist_progress')])
        self.assertEqual(result['missing'], [])

    def test_consumer_off_native_fallback_and_ps_read_failure_reuses_existing_rows(self):
        native = [['processlist', 42, 'copy to tmp table', 1, 2, 100, 'ALTER TABLE t ADD x INT', 40, 'Query']]
        result = ddl.read_progress(Reader(settings('mariadb', off=['events_stages_current']) + native), server('mariadb'))
        self.assertTrue(result['available'])
        self.assertEqual(result['missing'], ['events_stages_current'])
        result = ddl.read_progress(Reader(error=ConnectorError('ERROR 1142: SELECT denied')), server('mariadb'), native_rows=native)
        self.assertEqual(result['items'][0]['percent'], 20)
        self.assertIn('SELECT denied', result['reason'])
        reader = Reader(error=ConnectorError('must not connect'))
        result = ddl.read_progress(reader, server('mariadb', ps=False), native_rows=native)
        self.assertTrue(result['available'])
        self.assertEqual(reader.calls, [])

    def test_read_errors_are_local_to_progress(self):
        for error in ('ERROR 1142: denied', 'ERROR 2013: connection lost', 'timed out'):
            result = ddl.read_progress(Reader(error=ConnectorError(error)), server())
            self.assertFalse(result['available'])
            self.assertIn(error, result['reason'])
            self.assertEqual(result['missing'], [])
            self.assertEqual(result['items'], [])

    def test_ids_validate_before_connect_and_bound_query(self):
        for ids in (None, [], '42', [True], [-1], [0], ['42'], ['42 OR 1'], [2 ** 53], list(range(1, 202))):
            with self.subTest(ids=ids):
                conn = ReplayConn([])
                with self.assertRaises(ValueError):
                    ddl.ddl_progress_report(conn, ids)
                self.assertEqual(conn.calls, [])
        sql = ddl.progress_sql('mysql', ddl.check_ids([42, 42, 43]))
        self.assertIn('t.PROCESSLIST_ID IN (42, 43)', sql)
        result = ddl.read_progress(Reader(settings() + [stage(), stage(cid=43)]), server(), ids=[42])
        self.assertEqual([i['id'] for i in result['items']], [42])

    def test_small_endpoint_and_allowlists(self):
        conn = ReplayConn([
            [ddl.TAG + "SHOW GLOBAL VARIABLES WHERE Variable_name IN ('performance_schema', 'version', 'version_comment')",
             batch([['version', '8.4.11'], ['version_comment', 'MySQL'], ['performance_schema', 'ON']])],
            [ddl.TAG + ddl.progress_sql('mysql', [42]), batch(settings() + [stage()])],
        ])
        with mock.patch('myflames.connector.MySQLConnection', FakeFactory(conn)):
            result = ui._discover_response('/api/live/ddl-progress', {'connection': {'host': 'localhost', 'user': 'app'},
                                                                     'connection_ids': [42]})
        self.assertEqual(result['schema_version'], 'ddl-progress-1.0')
        self.assertEqual(result['server_product'], 'MySQL')
        self.assertEqual(result['flavor'], 'mysql')
        self.assertEqual(len(conn.calls), 2)  # metadata, then one bounded progress read
        self.assertEqual(result['items'][0]['percent'], 42)
        self.assertIn('/api/live/ddl-progress', ui._SERVER_VIEW_PATHS)
        self.assertIn('/api/live/ddl-progress', ui._CONNECTION_PATHS)
        with open(ui.__file__, encoding='utf8') as source:
            self.assertIn('"/api/live/ddl-progress"', source.read().split('if self.path not in (', 1)[1])

    def test_mariadb_endpoint_falls_back_when_ps_select_is_denied(self):
        conn = ReplayConn([
            [ddl.TAG + "SHOW GLOBAL VARIABLES WHERE Variable_name IN ('performance_schema', 'version', 'version_comment')",
             batch([['version', '11.4.13-MariaDB'], ['version_comment', 'MariaDB'], ['performance_schema', 'ON']])],
            [ddl.TAG + ddl.progress_sql('mariadb', [42]), None, 'ERROR 1142: SELECT denied'],
            [ddl.TAG + ddl.progress_sql('mariadb', [42], ps_on=False), batch([
                ['processlist', 42, 'copy to tmp table', 2, 4, 100, 'ALTER TABLE t ADD x INT', 50, 'Query']])],
        ])
        result = ddl.ddl_progress_report(conn, [42])
        self.assertEqual(len(conn.calls), 3)  # metadata, failed data read, bounded native recovery
        self.assertTrue(result['available'])
        self.assertIn('SELECT denied', result['reason'])
        self.assertEqual(result['source'], 'processlist_progress')
        self.assertEqual(result['items'][0]['percent'], 37.5)

    def test_editor_preflight_failure_is_a_progress_reason(self):
        conn = ReplayConn([], error=ConnectorError('ERROR 2003: cannot connect'))
        result = ddl.ddl_progress_report(conn, [42])
        self.assertFalse(result['available'])
        self.assertIn('cannot connect', result['reason'])
        self.assertEqual(result['schema_version'], 'ddl-progress-1.0')

    def test_python_37_syntax_and_schema_shape(self):
        with open(ddl.__file__, encoding='utf8') as source:
            ast.parse(source.read(), **({"feature_version": (3, 7)} if sys.version_info >= (3, 8) else {}))
        with open(os.path.join(os.path.dirname(__file__), '..', 'docs', 'schemas', 'ddl-progress-1.0.json')) as source:
            schema = json.load(source)
        result = dict(ddl.read_progress(Reader(settings() + [stage()]), server()), schema_version='ddl-progress-1.0')
        from test_chart_data import validate
        validate(json.loads(json.dumps(result)), schema)
        bad = dict(result, items=[dict(result['items'][0], percent=100)])
        with self.assertRaises(AssertionError):
            validate(bad, schema)


class TestDdlSessions(unittest.TestCase):
    def test_report_attaches_progress_and_cli_text(self):
        from test_sessions import synthetic
        conn = synthetic([[42, 'app', 'localhost', 'db', 'Query', 126, 'copy to tmp table', 'ALTER TABLE t ADD x INT', 24]],
                         ddl_rows=settings() + [stage()])
        result = sessions.sessions_report(conn)
        self.assertEqual(result['sessions'][0]['ddl'], result['ddl']['items'][0])
        text = sessions.format_text(result)
        self.assertIn('ALTER on 42: copying rows (stage/sql/copy to tmp table) 42%, about 2 min 54 s left (estimate)', text)
        self.assertEqual(sum('events_stages_current s JOIN' in sql for sql in conn.calls), 1)
        # The actual CLI shares the same projection, including additive JSON.
        code, out, err, _ = run_cli('sessions', ['-q'], conn)
        self.assertEqual(code, 0, err)
        self.assertIn('ALTER on 42:', out)
        code, out, err, _ = run_cli('sessions', ['--json', '-q'], conn)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['ddl']['items'][0]['percent'], 42)

    def test_report_survives_progress_failure_and_cli_names_missing_consumer(self):
        from test_sessions import synthetic
        conn = synthetic([], ddl_rows=settings(off=['events_stages_current']))
        report = sessions.sessions_report(conn)
        self.assertIn('Turn on events_stages_current', sessions.format_text(report))
        conn = synthetic([], ddl_rows=ConnectorError('ERROR 2013: disconnected'))
        report = sessions.sessions_report(conn)
        self.assertIn('Could not read DDL progress', report['ddl']['reason'])
        self.assertEqual(report['sessions'], [])

    def test_mariadb_off_reuses_native_rows_and_show_overall_percentage(self):
        from test_sessions import synthetic
        row = [42, 'app', 'localhost', 'db', 'Query', 100, 'copy to tmp table', 'ALTER TABLE t ADD x INT', 24,
               2, 4, 50, 0, 0]
        conn = synthetic([row], version='11.4.13-MariaDB', ps='OFF')
        result = sessions.sessions_report(conn)
        self.assertEqual(result['sessions'][0]['ddl']['percent'], 37.5)
        self.assertFalse(any("SELECT 'processlist'" in sql for sql in conn.calls))
        conn = synthetic([], version='11.4.13-MariaDB', ps='OFF', pl_rows=[row[:8] + [37.5]])
        result = sessions.sessions_report(conn, source_mode='show_processlist')
        self.assertEqual(result['sessions'][0]['ddl']['percent'], 37.5)
        self.assertEqual(result['sessions'][0]['ddl']['left_s'], 166.7)
        self.assertEqual(result['sessions'][0]['ddl']['stage'], 'copy to tmp table')

    def test_progress_does_not_expose_sessions_missing_from_processlist(self):
        from test_sessions import synthetic
        conn = synthetic([], ddl_rows=settings() + [stage()])
        result = sessions.sessions_report(conn)
        self.assertEqual(result['ddl']['items'], [])
