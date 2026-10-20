"""Wait events: ``myflames waits``, the Waits tab API, and the delta math.

Replays live captures (see _perf_replay) from MySQL 8.4, 9.7 and 26.7 and
MariaDB 11.4 and 13.0, with Performance Schema on and with MariaDB's default
(off). Each window held a query loop and a metadata lock wait of about 4 s.
"""
import inspect
import json
import os
import tempfile
import unittest
from unittest import mock

from _perf_replay import (MARIADB_OFF, MARIADB_PS, MYSQL, PASSWORD, SERVERS, FakeFactory, FakeTime, ReplayConn,
                          assert_read_only, batch, replay, run_cli, run_main)
from myflames import ui, waits
from myflames.connector import ConnectorError
from myflames.perf_common import Server

TAG = '/*myflames:waits*/ '
EVENT_FIELDS = {'event', 'class', 'class_label', 'meaning', 'count', 'wait_ms', 'avg_ms', 'timed'}


def server(engine='mysql', ps='ON', uptime='86400'):
    version = '8.4.11' if engine == 'mysql' else '11.4.13-MariaDB'
    return Server({'version': version, 'version_comment': 'x', 'performance_schema': ps}, {'Uptime': uptime})


def snap(rows, t=0.0, uptime=1000):
    return {'uptime': uptime, 'rows': rows, 't': t, 'taken_at': '2026-10-13T12:00:%02dZ' % int(t)}


COVERAGE = [['wait/io/file', '51', '51', '51'], ['wait/synch/mutex', '0', '0', '236'],
            ['wait/lock/metadata', '1', '1', '1'], ['wait/io/table', '1', '1', '1']]


class WaitsCase(unittest.TestCase):
    maxDiff = None

    def report(self, name, **kw):
        clock = FakeTime()
        conn, data = replay('waits', name, clock=clock)
        result = waits.waits_report(conn, data['args']['interval'], data['args']['limit'],
                                    sleep=clock.sleep, clock=clock.monotonic, **kw)
        assert_read_only(self, conn, TAG)
        json.dumps(result, allow_nan=False)
        self.assertEqual(result['schema_version'], 'waits-1.0')
        return result, data, conn


class TestLiveReplay(WaitsCase):
    def test_every_server_with_performance_schema(self):
        for name in MYSQL + MARIADB_PS:
            with self.subTest(server=name):
                result, data, conn = self.report(name)
                self.assertTrue(result['available'])
                self.assertEqual(result['engine'], 'mariadb' if name.startswith('mariadb') else 'mysql')
                window = result['window']
                self.assertEqual(window['status'], 'ok')
                # The scripted metadata lock wait (about 4 s) leads the window.
                top = window['events'][0]
                self.assertEqual(top['event'], 'wait/lock/metadata/sql/mdl')
                self.assertGreater(top['wait_ms'], 3000)
                self.assertEqual(top['class_label'], 'Metadata locks')
                for event in window['events']:
                    self.assertTrue(EVENT_FIELDS <= set(event), event)
                    self.assertTrue(event['event'].startswith('wait/'))
                    self.assertGreater(event['count'], 0)
                waits_ms = [e['wait_ms'] for e in window['events']]
                self.assertEqual(waits_ms, sorted(waits_ms, reverse=True))
                self.assertIn('wait/io/table/sql/handler', [e['event'] for e in window['events']])
                # Classes are listed one by one; nothing adds them up or shows a share.
                text = json.dumps(result)
                for word in ('"total_ms"', '"pct"', '"share"', '"percent"'):
                    self.assertNotIn(word, text)
                self.assertTrue(any('never added up' in n for n in result['notes']))
                since = result['since_start']
                self.assertTrue(since['events'])
                self.assertIn('max_ms', since['events'][0])
                self.assertNotIn('max_ms', window['events'][0])
                # Two snapshots, the interval apart.
                snapshots = [sql for sql, _ in conn.calls if 'events_waits_summary_global_by_event_name' in sql]
                self.assertEqual(len(snapshots), 2)
                self.assertIn("EVENT_NAME LIKE 'wait/%'", snapshots[0])

    def test_coverage_and_enable_statements(self):
        for name in MYSQL + MARIADB_PS:
            with self.subTest(server=name):
                result, _, _ = self.report(name)
                coverage = {c['class']: c for c in result['coverage']}
                self.assertNotIn('idle', coverage)
                self.assertEqual(coverage['wait/synch/mutex']['state'], 'off')
                self.assertEqual(coverage['wait/synch/mutex']['enabled'], 0)
                self.assertEqual(coverage['wait/io/file']['state'], 'on')
                self.assertNotIn('enable', coverage['wait/io/file'])
                enable = coverage['wait/synch/mutex']['enable']
                self.assertEqual(enable['runtime'], "UPDATE performance_schema.setup_instruments SET ENABLED = 'YES', "
                                                    "TIMED = 'YES' WHERE NAME LIKE 'wait/synch/mutex/%';")
                self.assertEqual(enable['config'], "performance-schema-instrument = 'wait/synch/mutex/%=ON'")
                # The fixtures' MariaDB servers started with the MDL instrument on.
                self.assertEqual(coverage['wait/lock/metadata']['state'], 'on')
                self.assertTrue(any('never means no contention' in n for n in result['notes']))

    def test_mariadb_notes_the_mdl_default(self):
        mariadb, _, _ = self.report('mariadb-13.0')
        mysql, _, _ = self.report('mysql-8.4')
        self.assertTrue(any('wait/lock/metadata/sql/mdl) off by default' in n for n in mariadb['notes']))
        self.assertFalse(any('off by default' in n for n in mysql['notes']))

    def test_performance_schema_off(self):
        for name in MARIADB_OFF:
            with self.subTest(server=name):
                result, _, conn = self.report(name)
                self.assertFalse(result['available'])
                self.assertEqual(result['unavailable'], 'performance_schema_off')
                self.assertIsNone(result['window'])
                self.assertIn('performance_schema=ON under [mysqld]', result['notes'][0])
                self.assertIn('restart', result['notes'][0])
                # Only the preflight ran: P_S tables return empty sets when it is off.
                self.assertFalse(any('performance_schema.' in sql for sql, _ in conn.calls))

    def test_text_output(self):
        result, _, _ = self.report('mysql-9.7')
        text = waits.format_text(result)
        self.assertTrue(text.startswith('MySQL 9.7.2, up '))
        self.assertIn('TOP WAITS OVER THE WINDOW (aggregate across threads)', text)
        self.assertRegex(text, r'\n  wait/lock/metadata/sql/mdl +\d+ +[\d.]+ s')
        self.assertIn('BY CLASS (each class on its own; never add them up)', text)
        self.assertIn("WHERE NAME LIKE 'wait/synch/mutex/%';", text)
        off, _, _ = self.report('mariadb-13.0-default')
        self.assertIn('Wait events are not available', waits.format_text(off))


class TestDeltaMath(WaitsCase):
    def compute(self, a, b, elapsed=10.0, **kw):
        return waits.compute_waits(server(**kw), snap(a), snap(b, elapsed, uptime=b.pop('_uptime', 1010)),
                                   COVERAGE, {'global_instrumentation': True}, 10, 20)

    def test_deltas_new_events_and_rates(self):
        a = {'wait/io/file/innodb/innodb_log_file': (100, 5e9, 1e9), 'wait/io/table/sql/handler': (1000, 2e9, 1e8)}
        b = {'wait/io/file/innodb/innodb_log_file': (150, 25e9, 1e9), 'wait/io/table/sql/handler': (1000, 2e9, 1e8),
             'wait/lock/metadata/sql/mdl': (1, 4e12, 4e12)}
        report = self.compute(a, b)
        events = report['window']['events']
        self.assertEqual([e['event'] for e in events], ['wait/lock/metadata/sql/mdl', 'wait/io/file/innodb/innodb_log_file'])
        mdl, log = events
        self.assertEqual((mdl['count'], mdl['wait_ms'], mdl['wait_ms_per_s']), (1, 4000, 400))
        self.assertEqual((log['count'], log['wait_ms'], log['avg_ms']), (50, 20, 0.4))
        self.assertIn('redo log', log['meaning'])
        # Unchanged events are not in the window; classes sum within a class only.
        self.assertEqual([c['class'] for c in report['window']['classes']], ['wait/lock/metadata', 'wait/io/file'])
        self.assertEqual(report['window']['classes'][1]['events'], 1)
        # Since start keeps every timed event, with its longest single wait.
        since = {e['event']: e for e in report['since_start']['events']}
        self.assertEqual(since['wait/lock/metadata/sql/mdl']['max_ms'], 4000)
        self.assertEqual(report['since_start']['uptime_s'], 1010)

    def test_untimed_events_are_listed_not_ranked(self):
        report = self.compute({}, {'wait/io/socket/sql/client_connection': (40, 0, 0)})
        self.assertEqual(report['window']['events'], [])
        self.assertEqual(report['window']['untimed'], [{'event': 'wait/io/socket/sql/client_connection', 'count': 40}])
        self.assertTrue(any('counted but not timed' in n for n in report['notes']))

    def test_counter_that_goes_down_drops_the_window(self):
        a = {'wait/io/table/sql/handler': (1000, 2e9, 0), 'wait/io/file/sql/binlog': (5, 1e6, 0)}
        b = {'wait/io/table/sql/handler': (10, 1e7, 0)}
        report = self.compute(a, b)
        self.assertEqual(report['window']['status'], 'reset')
        self.assertEqual(report['window']['events'], [])
        note = [n for n in report['notes'] if 'went down' in n][0]
        self.assertIn('wait/io/file/sql/binlog', note)
        self.assertIn('truncated', note)
        self.assertTrue(report['since_start']['events'])

    def test_restart_between_snapshots(self):
        b = {'wait/io/table/sql/handler': (10, 1e7, 0), '_uptime': 4}
        report = self.compute({'wait/io/table/sql/handler': (1000, 2e9, 0)}, b)
        self.assertTrue(report['restarted'])
        self.assertEqual(report['window']['status'], 'restarted')
        self.assertTrue(any('restarted between the two snapshots' in n for n in report['notes']))

    def test_large_counters_stay_exact(self):
        big = 18446744073709551000
        report = self.compute({'wait/io/table/sql/handler': (10, big, 0)},
                              {'wait/io/table/sql/handler': (12, big + 5000000, 0)})
        self.assertEqual(report['window']['events'][0]['wait_ms'], 0.005)

    def test_limit_and_more(self):
        b = {'wait/io/file/sql/f%02d' % i: (1, (i + 1) * 1e9, 0) for i in range(30)}
        report = waits.compute_waits(server(), snap({}), snap(b, 10.0), COVERAGE, None, 10, 5)
        self.assertEqual(len(report['window']['events']), 5)
        self.assertEqual(report['window']['more'], 25)
        self.assertEqual(report['window']['events'][0]['event'], 'wait/io/file/sql/f29')

    def test_global_instrumentation_off_and_lost(self):
        lost = Server({'version': '8.4.11', 'performance_schema': 'ON'},
                      {'Uptime': '100', 'Performance_schema_mutex_instances_lost': '3',
                       'Performance_schema_file_handles_lost': '0'})
        report = waits.compute_waits(lost, snap({}), snap({}, 10.0), COVERAGE,
                                     {'global_instrumentation': False}, 10, 20)
        self.assertTrue(any("SET ENABLED = 'YES' WHERE NAME = 'global_instrumentation'" in n for n in report['notes']))
        self.assertTrue(any('Performance_schema_mutex_instances_lost above zero' in n for n in report['notes']))
        self.assertFalse(any('file_handles_lost' in n for n in report['notes']))

    def test_reviewed_wording(self):
        # A runtime UPDATE re-applies to existing instances (no restart needed).
        self.assertIn('takes effect at once, for existing and new instances', waits.ENABLE_CAVEAT)
        self.assertNotIn('only picked up through my.cnf', waits.ENABLE_CAVEAT)
        # InnoDB row lock waits have no event of their own.
        self.assertIn('row lock waits are not here', waits.meaning('wait/lock/table/sql/handler'))
        self.assertIn('row lock waits have no event', waits.meaning('wait/io/table/sql/handler'))
        # innodb_temp_file differs by engine.
        temp = 'wait/io/file/innodb/innodb_temp_file'
        self.assertIn('temporary tablespaces', waits.meaning(temp, 'mysql'))
        self.assertIn('merge-sort files', waits.meaning(temp, 'mariadb'))
        self.assertIn('ibtmp1', waits.meaning(temp, 'mariadb'))

    def test_meanings(self):
        self.assertIn('counted here and again', waits.meaning('wait/io/table/sql/handler'))
        self.assertEqual(waits.meaning('wait/io/file/foo/bar'), 'A file read, write or sync (foo/bar).')
        self.assertEqual(waits.meaning('wait/synch/mutex/innodb/x'), waits.CLASS_EXPLAIN['wait/synch/mutex'])
        self.assertEqual(waits.event_class('wait/synch/mutex/innodb/x'), 'wait/synch/mutex')


class TestSampling(WaitsCase):
    def test_interval_and_limit_are_validated_before_any_sql(self):
        for kwargs in ({'interval': 1}, {'interval': 61}, {'interval': '5'}, {'interval': True}, {'limit': 0},
                       {'limit': 201}):
            with self.subTest(**kwargs):
                conn = ReplayConn([])
                with self.assertRaises(ValueError):
                    waits.waits_report(conn, **kwargs)
                self.assertEqual(conn.calls, [])

    def test_waits_out_the_interval(self):
        clock = FakeTime(query_s=0.2)
        conn, _ = replay('waits', 'mysql-8.4', clock=clock)
        report = waits.waits_report(conn, 7, sleep=clock.sleep, clock=clock.monotonic)
        self.assertEqual(len(clock.slept), 1)
        self.assertAlmostEqual(report['elapsed_s'], 7.4, places=3)
        self.assertEqual(report['interval'], 7)

    def test_denied_performance_schema(self):
        def edit(sql, text, n):
            if 'events_waits_summary_global_by_event_name' in sql:
                raise ConnectorError("mysql client exited 1: ERROR 1142 (42000): SELECT command denied to user "
                                     "'app'@'%' for table 'events_waits_summary_global_by_event_name'")
            return text
        clock = FakeTime()
        conn, _ = replay('waits', 'mysql-8.4', clock=clock, edit=edit)
        report = waits.waits_report(conn, 5, sleep=clock.sleep, clock=clock.monotonic)
        self.assertFalse(report['available'])
        self.assertEqual(report['unavailable'], 'denied')
        self.assertTrue(any('Grant SELECT on performance_schema' in n for n in report['notes']))

    def test_connection_errors_become_value_errors(self):
        conn = ReplayConn([], error=ConnectorError("mysql client exited 1: ERROR 2003 (HY000): Can't connect"))
        with self.assertRaisesRegex(ValueError, r'^Could not read wait events: ERROR 2003'):
            waits.waits_report(conn, 2, sleep=lambda s: None)


class TestWaitsCLI(WaitsCase):
    def call(self, name, *argv, conn=None):
        clock = FakeTime()
        if conn is None:
            conn, _ = replay('waits', name, clock=clock)
        else:
            conn.clock = clock
        patches = [mock.patch('myflames.perf_common.time', clock)]
        return run_cli('waits', argv, conn, patches) + (conn, clock)

    def test_json_and_connection_flags(self):
        code, out, err, factory, conn, clock = self.call('mysql-26.7', '-h', 'db', '-P', '3307', '-u', 'app',
                                                         '-p' + PASSWORD, '--json', '--interval', '5')
        self.assertEqual((code, err), (0, ''))
        data = json.loads(out)
        self.assertEqual((data['schema_version'], data['server_version'], data['interval']), ('waits-1.0', '26.7.0', 5))
        self.assertAlmostEqual(clock.slept[0], 5.0)
        self.assertEqual((factory.options['host'], factory.options['port'], factory.options['password']),
                         ('db', 3307, PASSWORD))
        self.assertNotIn(PASSWORD, out)
        assert_read_only(self, conn, TAG)

    def test_text_notes_quiet_and_output_file(self):
        code, out, err, _, _, _ = self.call('mariadb-11.4')
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith('MariaDB 11.4.13'))
        self.assertIn('note: Wait time is summed across all threads', err)
        code, out, err, _, _, _ = self.call('mariadb-11.4', '-q')
        self.assertEqual((code, err), (0, ''))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'waits.json')
            code, out, err, _, _, _ = self.call('mysql-8.4', '--json', '-o', path)
            self.assertEqual((code, out), (0, ''))
            self.assertIn('Written to ' + path, err)
            with open(path, encoding='utf-8') as handle:
                self.assertEqual(json.load(handle)['schema_version'], 'waits-1.0')

    def test_performance_schema_off_still_exits_0_with_the_how_to(self):
        code, out, err, _, _, _ = self.call('mariadb-13.0-default', '--json')
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertFalse(data['available'])
        self.assertIn('performance_schema=ON', data['notes'][0])

    def test_bad_arguments_exit_2_without_connecting(self):
        for argv in (['--interval', '1'], ['--interval', '61'], ['--limit', '0'], ['--interval', 'x']):
            with self.subTest(argv=argv):
                conn = ReplayConn([])
                code, out, _, factory, _, _ = self.call('', *argv, conn=conn)
                self.assertEqual((code, out), (2, ''))
                self.assertIsNone(factory.options)
                self.assertEqual(conn.calls, [])

    def test_failures_exit_2_and_redact_the_password(self):
        conn = ReplayConn([], error=ConnectorError("mysql client exited 1: ERROR 1045 (28000): Access denied "
                                                   "for user 'app' " + PASSWORD))
        code, out, err, _, _, _ = self.call('', '-u', 'app', '-p' + PASSWORD, conn=conn)
        self.assertEqual((code, out), (2, ''))
        self.assertNotIn(PASSWORD, err)
        self.assertIn('Could not read wait events: ERROR 1045', err)

    def test_root_help_and_dispatch(self):
        code, out, _ = run_main(['myflames', '--help'])
        self.assertEqual(code, 0)
        self.assertIn('  waits     Top wait events over a window, by class, since start; uninstrumented classes', out)
        self.assertIn('myflames waits -h localhost -u admin -p', out)
        code, out, _ = run_main(['myflames', 'waits', '--help'])
        for flag in ('--interval N', '--limit N', '--json', '--profile', '-q, --quiet'):
            self.assertIn(flag, out)
        self.assertIn('Exit codes: 0 success, 2 bad input or connection failure.', ' '.join(out.split()))
        conn, _ = replay('waits', 'mariadb-13.0')
        clock = FakeTime()
        conn.clock = clock
        code, out, err = run_main(['myflames', 'waits', '--json', '-q', '--interval', '5'], conn,
                                  [mock.patch('myflames.perf_common.time', clock)])
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(json.loads(out)['engine'], 'mariadb')


class TestWaitsAPI(WaitsCase):
    connection = {'host': '127.0.0.1', 'port': 3306, 'user': 'app', 'password': PASSWORD}

    def call(self, name, payload, workspace=object()):
        clock = FakeTime()
        conn, _ = replay('waits', name, clock=clock)
        factory = FakeFactory(conn)
        with mock.patch('myflames.connector.MySQLConnection', factory), \
                mock.patch('myflames.perf_common.time', clock):
            result = ui.api_response('/api/live/waits', payload, workspace=workspace)
        assert_read_only(self, conn, TAG)
        return result, factory, clock

    def test_default_interval_is_10(self):
        result, factory, clock = self.call('mysql-8.4', {'connection': self.connection})
        self.assertEqual(result['interval'], 10)
        self.assertEqual(result['schema_version'], 'waits-1.0')
        self.assertEqual(factory.options['password'], PASSWORD)

    def test_bad_interval_is_rejected_before_any_sql(self):
        for interval in (1, 61, '5', True, 2.5):
            with self.subTest(interval=interval):
                conn = ReplayConn([])
                with mock.patch('myflames.connector.MySQLConnection', FakeFactory(conn)):
                    with self.assertRaisesRegex(ValueError, 'from 2 to 60 seconds'):
                        ui.api_response('/api/live/waits', {'connection': self.connection, 'interval': interval},
                                        workspace=object())
                self.assertEqual(conn.calls, [])

    def test_errors_never_echo_the_password(self):
        conn = ReplayConn([], error=ConnectorError('mysql client exited 1: ERROR 1045: denied ' + PASSWORD))
        with mock.patch('myflames.connector.MySQLConnection', FakeFactory(conn)):
            with self.assertRaises(ValueError) as caught:
                ui.api_response('/api/live/waits', {'connection': self.connection}, workspace=object())
        self.assertNotIn(PASSWORD, str(caught.exception))
        self.assertIn('[redacted]', str(caught.exception))

    def test_saved_password_and_allowlist(self):
        class Store(object):
            def connection_with_saved_password(self, connection):
                return dict(connection, password='from-keychain')

        class Workspace(object):
            store = Store()
        _, factory, _ = self.call('mysql-8.4', {'connection': {'host': '127.0.0.1', 'profile_id': 'p1'}, 'interval': 5},
                                  workspace=Workspace())
        self.assertEqual(factory.options['password'], 'from-keychain')
        self.assertIn('"/api/live/waits"', inspect.getsource(ui.WorkspaceHandler.do_POST))
        self.assertIn('/api/live/waits', ui._CONNECTION_PATHS)


if __name__ == '__main__':
    unittest.main()
