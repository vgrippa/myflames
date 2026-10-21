"""I/O hot spots: ``myflames io``, the I/O tab API, file mapping, and deltas.

Replays live captures (see _perf_replay): a query loop scanned and updated
mfshop.orders during each window.
"""
import inspect
import json
import unittest
from unittest import mock

from _perf_replay import (MARIADB_PS, MYSQL, PASSWORD, FakeFactory, FakeTime, ReplayConn, assert_read_only, replay, batch,
                          run_cli, run_main)
from myflames import io_stats, ui
from myflames.connector import ConnectorError
from myflames.perf_common import Server

TAG = '/*myflames:io*/ '
GLOBAL_KEYS = ['data_read', 'data_written', 'data_reads', 'data_writes', 'data_fsyncs', 'redo_written', 'pages_read']


_recorded_replay = replay


def replay(view, name, **kwargs):
    conn, data = _recorded_replay(view, name, **kwargs)
    schema = data['args'].get('schema')
    queries = io_stats._instance_queries(schema)
    patterns = {'tables': 'FROM performance_schema.table_io_waits_summary_by_table ',
                'files': 'FROM performance_schema.file_summary_by_instance ',
                'classes': 'FROM performance_schema.file_summary_by_event_name ',
                'userstat': 'FROM information_schema.TABLE_STATISTICS '}
    for kind, pattern in patterns.items():
        old = next((sql for sql in conn.answers if pattern in sql), None)
        if old is None:
            continue
        answers = conn.answers[old]
        if kind == 'files' and schema:
            # Reuse recorded counters, applying the new directory scope.
            answers = [''.join(line + '\n' for line in answer.split('\n') if line and
                       io_stats.classify_file(*line.split('\t')[:2])['schema'] == schema) for answer in answers]
        sql = queries[kind]
        conn.answers[TAG + sql] = answers
        conn.answers[TAG + sql + ' LIMIT %d' % (io_stats.INSTANCE_LIMIT + 1)] = answers
        count_sql = 'SELECT COUNT(*) FROM ' + sql.split(' FROM ', 1)[1]
        conn.answers[TAG + count_sql] = [batch([[len(answers[0].splitlines())]])]
    return conn, data


def server(engine='mysql', ps='ON'):
    version = '8.4.11' if engine == 'mysql' else '11.4.13-MariaDB'
    return Server({'version': version, 'performance_schema': ps, 'datadir': '/var/lib/mysql/'}, {'Uptime': '500'})


def snap(t, uptime=500, status=None, tables=None, files=None, classes=None):
    return {'status': status or {'Uptime': str(uptime)}, 'uptime': uptime, 'tables': tables or {},
            'files': files or {}, 'classes': classes or {}, 't': t, 'taken_at': 'x%d' % t}


def table(count, wait, fetch=0, fetch_t=0, ins=0, ins_t=0, upd=0, upd_t=0, dele=0, del_t=0):
    return (count, wait, fetch, fetch_t, ins, ins_t, upd, upd_t, dele, del_t)


def file_(reads=0, rbytes=0, rtime=0, writes=0, wbytes=0, wtime=0, misc=0, mtime=0):
    return (reads, rbytes, rtime, writes, wbytes, wtime, misc, mtime, rtime + wtime + mtime)


class IoCase(unittest.TestCase):
    maxDiff = None

    def report(self, name):
        clock = FakeTime()
        conn, data = replay('io', name, clock=clock)
        args = dict(data['args'])
        result = io_stats.io_report(conn, args['interval'], args['schema'], args['limit'], sleep=clock.sleep,
                                    clock=clock.monotonic)
        assert_read_only(self, conn, TAG)
        json.dumps(result, allow_nan=False)
        self.assertEqual(result['schema_version'], 'io-1.0')
        return result, conn


class TestLiveReplay(IoCase):
    def test_performance_schema_servers(self):
        for name in MYSQL + MARIADB_PS:
            with self.subTest(server=name):
                result, conn = self.report(name)
                self.assertEqual(result['source'], 'performance_schema')
                self.assertEqual([m['key'] for m in result['global']], GLOBAL_KEYS)
                self.assertTrue(all(m['rate'] is not None for m in result['global']))
                window = result['window']
                self.assertEqual(window['status'], 'ok')
                orders = [t for t in window['tables'] if t['table'] == 'orders'][0]
                self.assertEqual(window['tables'][0]['table'], 'orders')
                self.assertGreater(orders['read_rows'], 100000)
                self.assertGreater(orders['write_rows'], 0)
                self.assertEqual(orders['write_rows'], orders['inserts'] + orders['updates'] + orders['deletes'])
                self.assertTrue(all(t['schema'] == 'mfshop' for t in window['tables']))
                kinds = {f['kind'] for f in window['files']} | {f['kind'] for f in result['since_start']['files']}
                self.assertIn('table', kinds)
                mapped = [f for f in result['since_start']['files'] if f['kind'] == 'table' and f['schema'] == 'mfshop']
                self.assertIn('orders', [f['table'] for f in mapped])
                self.assertTrue(window['file_classes'])
                self.assertTrue(result['since_start']['tables'])
                if name.startswith('mariadb'):
                    self.assertTrue(any('does not instrument the redo log file' in n for n in result['notes']))
                    self.assertNotIn('wait/io/file/innodb/innodb_log_file',
                                     [c['event'] for c in result['since_start']['file_classes']])
                    self.assertIn('redo log writes are not included', result['global'][1]['explain'])
                else:
                    self.assertNotIn('redo', kinds, 'a selected schema scopes file instances')
                    self.assertIn('wait/io/file/innodb/innodb_log_file',
                                  [c['event'] for c in result['since_start']['file_classes']])
                    self.assertIn('includes redo log writes', result['global'][1]['explain'])
                    self.assertEqual(result['global'][-1]['sources'],
                                     ['Innodb_buffer_pool_reads', 'Innodb_buffer_pool_read_ahead'])
                # The instruments are checked before the snapshots, and are on by default.
                self.assertTrue(any('setup_instruments' in sql for sql, _ in conn.calls))
                self.assertFalse(any('instrument is off' in n or 'instruments are on' in n for n in result['notes']))
                # The per-table read is scoped to the schema.
                tables_sql = [sql for sql, _ in conn.calls if 'table_io_waits_summary_by_table' in sql and 'COUNT(*)' not in sql]
                self.assertEqual(len(tables_sql), 2)
                self.assertIn("OBJECT_SCHEMA = CONVERT(X'%s' USING utf8mb4)" % 'mfshop'.encode().hex(), tables_sql[0])
                self.assertTrue(any('Tables are limited to schema mfshop' in n for n in result['notes']))

    def test_userstat_and_status_only(self):
        result, conn = self.report('mariadb-11.4-userstat')
        self.assertEqual(result['source'], 'userstat')
        self.assertEqual(result['window']['tables'][0]['table'], 'orders')
        self.assertIsNone(result['window']['tables'][0]['read_ms'])
        self.assertEqual(result['window']['files'], [])
        self.assertIn('MariaDB user statistics', result['notes'][0])
        # Live: a range read through a partitioned table's index is missing from user statistics.
        self.assertIn('They do not count partitioned tables reliably', result['notes'][0])
        self.assertFalse(any('performance_schema.' in sql for sql, _ in conn.calls))
        off, conn = self.report('mariadb-13.0-default')
        self.assertEqual(off['source'], 'status')
        self.assertEqual([m['key'] for m in off['global']], GLOBAL_KEYS)
        self.assertEqual(off['window']['tables'], [])
        self.assertIn('performance_schema=ON under [mysqld]', off['notes'][0])
        self.assertIn('SET GLOBAL userstat = ON', off['notes'][0])
        self.assertTrue(all(sql.startswith(TAG + 'SHOW GLOBAL') for sql, _ in conn.calls))

    def test_text_output(self):
        result, _ = self.report('mysql-8.4')
        text = io_stats.format_text(result)
        self.assertTrue(text.startswith('MySQL 8.4.11, up '))
        self.assertIn(', schema mfshop\n', text)
        self.assertIn('SERVER-WIDE INNODB I/O', text)
        self.assertRegex(text, r'\n  mfshop\.orders +[\d,]+ ')
        self.assertIn('FILE KINDS OVER THE WINDOW (temporary files included)', text)


class TestFileMapping(unittest.TestCase):
    def test_classify(self):
        cases = [
            ('/var/lib/mysql/shop/orders.ibd', 'wait/io/file/innodb/innodb_data_file', ('table', 'shop', 'orders', None)),
            ('./shop/orders#P#p2024.ibd', 'wait/io/file/innodb/innodb_data_file', ('table', 'shop', 'orders', 'p2024')),
            ('/var/lib/mysql/shop/orders#p#p1#sp#p1sp0.ibd', 'wait/io/file/innodb/innodb_data_file',
             ('table', 'shop', 'orders', 'p1 / p1sp0')),
            ('/var/lib/mysql/my@002dshop/odd@0024name.ibd', 'wait/io/file/innodb/innodb_data_file',
             ('table', 'my-shop', 'odd$name', None)),
            ('/var/lib/mysql/mysql.ibd', 'wait/io/file/innodb/innodb_data_file', ('dictionary', None, None, None)),
            ('/var/lib/mysql/ibdata1', 'wait/io/file/innodb/innodb_data_file', ('system', None, None, None)),
            ('/var/lib/mysql/undo_001', 'wait/io/file/innodb/innodb_data_file', ('undo', None, None, None)),
            ('./undo002', 'wait/io/file/innodb/innodb_data_file', ('undo', None, None, None)),
            ('/var/lib/mysql/#innodb_redo/#ib_redo16', 'wait/io/file/innodb/innodb_log_file', ('redo', None, None, None)),
            ('/var/lib/mysql/ib_logfile0', 'wait/io/file/innodb/innodb_data_file', ('redo', None, None, None)),
            ('/var/lib/mysql/ibtmp1', 'wait/io/file/innodb/innodb_data_file', ('temporary', None, None, None)),
            ('/var/lib/mysql/#innodb_temp/temp_3.ibt', 'wait/io/file/innodb/innodb_temp_file',
             ('temporary', None, None, None)),
            ('./ibtmp1', 'wait/io/file/innodb/innodb_data_file', ('temporary', None, None, None)),
            ('/tmp/ib4Xf2', 'wait/io/file/innodb/innodb_temp_file', ('sortfile', None, None, None)),
            ('/var/lib/mysql/#ib_16384_0.dblwr', 'wait/io/file/innodb/innodb_dblwr_file',
             ('doublewrite', None, None, None)),
            ('/var/lib/mysql/binlog.000003', 'wait/io/file/sql/binlog', ('binlog', None, None, None)),
            ('/var/lib/mysql/relay.000002', 'wait/io/file/sql/relaylog', ('relaylog', None, None, None)),
            ('/var/lib/mysql/shop/#sql-1_2.ibd', 'wait/io/file/innodb/innodb_data_file', ('other', None, None, None)),
            ('/var/lib/mysql/mysql/plugin.MAD', 'wait/io/file/aria/MAD', ('table', 'mysql', 'plugin', None)),
            ('/data/fast/shop/big.ibd', 'wait/io/file/innodb/innodb_data_file', ('table', 'shop', 'big', None)),
        ]
        for path, event, (kind, schema, table, partition) in cases:
            with self.subTest(path=path):
                got = io_stats.classify_file(path, event, '/var/lib/mysql/')
                self.assertEqual((got['kind'], got['schema'], got['table'], got['partition']),
                                 (kind, schema, table, partition))
                self.assertEqual(got['label'], io_stats.FILE_KINDS[kind])


class TestDeltaMath(unittest.TestCase):
    def test_tables_files_and_classes(self):
        a = snap(0, status={'Uptime': '500', 'Innodb_data_read': '1000', 'Innodb_data_written': '0'},
                 tables={('s', 'a'): table(10, 1e9, 8, 5e8), ('s', 'b'): table(5, 1e9)},
                 files={('/var/lib/mysql/s/a.ibd', 'wait/io/file/innodb/innodb_data_file'): file_(1, 16384, 1e8),
                        ('/tmp/MLfd=1', 'wait/io/file/sql/io_cache'): file_(2, 100, 1e6)},
                 classes={'wait/io/file/sql/io_cache': file_(2, 100, 1e6)})
        b = snap(10, uptime=510, status={'Uptime': '510', 'Innodb_data_read': '11000', 'Innodb_data_written': '5'},
                 tables={('s', 'a'): table(110, 21e9, 58, 105e8, 50, 5e9), ('s', 'b'): table(5, 1e9),
                         ('s', 'c'): table(3, 3e9, 3, 3e9)},
                 files={('/var/lib/mysql/s/a.ibd', 'wait/io/file/innodb/innodb_data_file'): file_(3, 49152, 3e8, 1, 16384, 2e8)},
                 classes={'wait/io/file/sql/io_cache': file_(9, 9000, 9e6, 4, 800, 4e6)})
        report = io_stats.compute_io(server(), a, b, 'performance_schema', 10, 's', 20)
        tables = report['window']['tables']
        self.assertEqual([t['table'] for t in tables], ['a', 'c'], 'unchanged tables leave the window')
        self.assertEqual((tables[0]['read_rows'], tables[0]['read_ms'], tables[0]['inserts'], tables[0]['write_ms']),
                         (50, 10, 50, 5))
        self.assertEqual(tables[0]['total_ms'], 20)
        self.assertEqual(tables[0]['total_ms_per_s'], 2)
        self.assertEqual(tables[1]['read_rows'], 3, 'a table first seen in the second snapshot counts in full')
        files = report['window']['files']
        self.assertEqual(len(files), 1, 'a closed temporary file leaves the per-file list')
        self.assertEqual((files[0]['read_bytes'], files[0]['write_bytes'], files[0]['table']), (32768, 16384, 'a'))
        classes = report['window']['file_classes']
        self.assertEqual((classes[0]['read_bytes'], classes[0]['write_ops']), (8900, 4), 'its I/O stays in the class')
        rates = {m['key']: m['rate'] for m in report['global']}
        self.assertEqual(rates['data_read'], 1000)
        self.assertIsNone(rates['data_reads'], 'missing counters have no rate')

    def test_counters_that_go_down(self):
        a = snap(0, tables={('s', 'a'): table(100, 1e9), ('s', 'gone'): table(5, 1e9)},
                 files={('f', 'wait/io/file/sql/binlog'): file_(10, 100, 1e6)},
                 classes={'wait/io/file/sql/binlog': file_(10, 100, 1e6)})
        b = snap(10, uptime=510, tables={('s', 'a'): table(4, 1e8)},
                 files={('f', 'wait/io/file/sql/binlog'): file_(2, 20, 1e5)},
                 classes={'wait/io/file/sql/binlog': file_(2, 20, 1e5)})
        report = io_stats.compute_io(server(), a, b, 'performance_schema', 10, None, 20)
        self.assertEqual(report['window']['tables'], [])
        self.assertTrue(any('Counters went down for s.a, s.gone' in n for n in report['notes']))
        # A file whose counters restart is a new instance with the same name.
        self.assertEqual(report['window']['files'][0]['read_ops'], 2)
        self.assertEqual(report['window']['status'], 'reset')
        self.assertEqual(report['window']['file_classes'], [])

    def test_restart(self):
        a = snap(0, uptime=500, tables={('s', 'a'): table(100, 1e9)})
        b = snap(10, uptime=3, tables={('s', 'a'): table(4, 1e8)})
        report = io_stats.compute_io(server(), a, b, 'performance_schema', 10, None, 20)
        self.assertTrue(report['restarted'])
        self.assertEqual(report['window']['status'], 'restarted')
        self.assertEqual(report['global'], [])
        self.assertEqual(report['window']['tables'], [])

    def test_instruments_off_are_noted(self):
        a, b = snap(0), snap(10, uptime=510)
        report = io_stats.compute_io(server(), a, b, 'performance_schema', 10, None, 20,
                                     coverage=[['0', '40', '55']], consumers={'global_instrumentation': False})
        text = ' '.join(report['notes'])
        self.assertIn("WHERE NAME = 'wait/io/table/sql/handler';", text)
        self.assertIn('Only 40 of 55 file I/O instruments are on', text)
        self.assertIn("NAME LIKE 'wait/io/file/%';", text)
        self.assertIn("NAME = 'global_instrumentation'", text)
        quiet = io_stats.compute_io(server(), a, b, 'performance_schema', 10, None, 20,
                                    coverage=[['1', '55', '55']], consumers={'global_instrumentation': True})
        self.assertFalse(any('instrument' in n for n in quiet['notes']))

    def test_validation_before_sql(self):
        for kwargs in ({'interval': 1}, {'interval': 61}, {'schema': 'x' * 65}, {'limit': 0}):
            with self.subTest(**kwargs):
                conn = ReplayConn([])
                with self.assertRaises(ValueError):
                    io_stats.io_report(conn, **kwargs)
                self.assertEqual(conn.calls, [])

    def test_denied_falls_back_to_status_counters(self):
        def edit(sql, text, n):
            if 'performance_schema.' in sql:
                raise ConnectorError('mysql client exited 1: ERROR 1142 (42000): SELECT command denied to user')
            return text
        clock = FakeTime()
        conn, _ = replay('io', 'mysql-8.4', clock=clock, edit=edit)
        report = io_stats.io_report(conn, 5, 'mfshop', sleep=clock.sleep, clock=clock.monotonic)
        self.assertEqual(report['source'], 'status')
        self.assertTrue(any('Could not read per-table and per-file I/O' in n for n in report['notes']))
        self.assertTrue(all(m['rate'] is not None for m in report['global']))


class TestIoCLIAndAPI(IoCase):
    def test_cli_json_text_and_failures(self):
        clock = FakeTime()
        conn, _ = replay('io', 'mysql-26.7', clock=clock)
        code, out, err, factory = run_cli('io', ['--json', '--schema', 'mfshop', '--interval', '5'], conn,
                                          [mock.patch('myflames.perf_common.time', clock)])
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(json.loads(out)['scope'], {'schema': 'mfshop'})
        self.assertEqual(factory.options['query_timeout'], 30)
        conn, _ = replay('io', 'mariadb-13.0-default', clock=FakeTime())
        code, out, err, _ = run_cli('io', ['--interval', '5'], conn,
                                    [mock.patch('myflames.perf_common.time', FakeTime())])
        self.assertEqual(code, 0)
        self.assertIn('SERVER-WIDE INNODB I/O', out)
        self.assertIn('note: Performance Schema is off', err)
        for argv in (['--interval', '0'], ['--limit', '201']):
            conn = ReplayConn([])
            code, out, _, factory = run_cli('io', argv, conn)
            self.assertEqual((code, out, factory.options), (2, '', None))
        conn = ReplayConn([], error=ConnectorError('mysql client exited 1: ERROR 1045: denied ' + PASSWORD))
        code, _, err, _ = run_cli('io', ['-p' + PASSWORD], conn)
        self.assertEqual(code, 2)
        self.assertNotIn(PASSWORD, err)
        code, out, _ = run_main(['myflames', '--help'])
        self.assertIn('  io        Tables and files with the most I/O over a window, and since server start', out)

    def test_api(self):
        clock = FakeTime()
        conn, _ = replay('io', 'mariadb-11.4', clock=clock)
        factory = FakeFactory(conn)
        with mock.patch('myflames.connector.MySQLConnection', factory), mock.patch('myflames.perf_common.time', clock):
            result = ui.api_response('/api/live/io', {'connection': {'host': '127.0.0.1', 'password': PASSWORD},
                                                      'interval': 5, 'schema': 'mfshop'}, workspace=object())
        self.assertEqual(result['schema_version'], 'io-1.0')
        self.assertEqual(result['interval'], 5)
        self.assertEqual(factory.options['query_timeout'], 30)
        assert_read_only(self, conn, TAG)
        self.assertIn('"/api/live/io"', inspect.getsource(ui.WorkspaceHandler.do_POST))
        self.assertIn('/api/live/io', ui._CONNECTION_PATHS)
        conn = ReplayConn([])
        with mock.patch('myflames.connector.MySQLConnection', FakeFactory(conn)):
            with self.assertRaisesRegex(ValueError, 'from 2 to 60 seconds'):
                ui.api_response('/api/live/io', {'connection': {'host': 'h'}, 'interval': 90}, workspace=object())
        self.assertEqual(conn.calls, [])



class TestCollectionLimits(unittest.TestCase):
    def test_cli_all_instances_reaches_the_report(self):
        """The gate's note tells CLI users how to lift it: io had no flag for all_instances
        (found live: 3,032 tables and 4,289 file instances on MySQL 8.4.11 omitted both families)."""
        seen = []

        def fake(conn, interval, schema, limit, all_instances=False):
            seen.append(all_instances)
            return {'schema_version': 'io-1.0', 'notes': []}
        with mock.patch.object(io_stats, 'io_report', side_effect=fake), \
                mock.patch.object(io_stats, 'format_text', return_value='ok\n'):
            for argv, expected in ((['--all-instances'], True), ([], False)):
                code, out, err, _ = run_cli('io', argv + ['--interval', '2'], ReplayConn([]))
                self.assertEqual(code, 0, err)
        self.assertEqual(seen, [True, False])

    def scripted(self, engine='mysql', count=1, grow=False):
        class Conn(object):
            def __init__(self):
                self.calls, self.table_reads, self.file_reads = [], 0, 0
            def run(self, sql, extra_flags=None, timeout=None):
                self.calls.append((sql, timeout))
                if 'COUNT(*)' in sql:
                    return batch([[count]])
                if 'table_io_waits_summary_by_table' in sql:
                    self.table_reads += 1
                    n = io_stats.INSTANCE_LIMIT + 1 if grow and self.table_reads == 2 else 1
                    return batch([['s', 't%d' % i] + [self.table_reads * 10] * 10 for i in range(n)])
                if 'file_summary_by_instance' in sql:
                    self.file_reads += 1
                    return batch([['C:\\data\\s\\t.ibd', 'wait/io/file/innodb/innodb_data_file'] +
                                  [self.file_reads * 20] * 9])
                if 'SHOW GLOBAL STATUS' in sql:
                    return batch([['Uptime', 100]])
                return ''
        return Conn()

    def run_report(self, conn, engine='mysql', **kwargs):
        clock = FakeTime()
        with mock.patch.object(io_stats, 'preflight', return_value=server(engine)):
            return io_stats.io_report(conn, 2, 's', sleep=clock.sleep, clock=clock.monotonic, **kwargs)

    def test_instance_gate_and_explicit_full_scan(self):
        for engine in ('mysql', 'mariadb'):
            conn = self.scripted(engine, count=2001)
            report = self.run_report(conn, engine)
            self.assertEqual(report['collection']['omitted'], ['files', 'tables'])
            self.assertEqual((conn.table_reads, conn.file_reads), (0, 0))
            self.assertEqual(report['window']['tables'], [])
            conn = self.scripted(engine, count=2001)
            report = self.run_report(conn, engine, all_instances=True)
            self.assertTrue(report['collection']['complete'])
            self.assertEqual((conn.table_reads, conn.file_reads), (2, 2))
            self.assertEqual(report['window']['tables'][0]['read_rows'], 10)
            self.assertFalse(any('COUNT(*)' in sql or 'LIMIT' in sql for sql, _ in conn.calls))

    def test_snapshot_growth_drops_both_families_not_independent_top_lists(self):
        conn = self.scripted(grow=True)
        report = self.run_report(conn)
        self.assertEqual(report['collection']['omitted'], ['tables'])
        self.assertEqual(report['window']['tables'], [])
        self.assertEqual(report['since_start']['tables'], [])
        self.assertEqual(report['window']['files'][0]['read_ops'], 20)
        self.assertTrue(all('LIMIT 2001' in sql for sql, _ in conn.calls
                            if 'SELECT OBJECT_SCHEMA' in sql))
        file_sql = next(sql for sql, _ in conn.calls if 'SELECT FILE_NAME' in sql)
        self.assertIn('CHAR(92)', file_sql)
        self.assertIn("CONVERT(X'73' USING utf8mb4)", file_sql)

    def test_collection_timeout_marks_unknown_instead_of_partial_delta(self):
        conn = self.scripted()
        run = conn.run
        def timeout(sql, extra_flags=None, timeout=None):
            if 'SELECT FILE_NAME' in sql:
                self.assertGreater(timeout, 0)
                self.assertLessEqual(timeout, io_stats.COLLECTION_BUDGET_S + 2)
                raise ConnectorError('mysql query timed out')
            return run(sql, extra_flags, timeout)
        conn.run = timeout
        report = self.run_report(conn)
        self.assertEqual(report['collection']['omitted'], ['files'])
        self.assertEqual(report['window']['files'], [])
        self.assertEqual(report['since_start']['files'], [])
        self.assertTrue(report['window']['tables'])


if __name__ == '__main__':
    unittest.main()
