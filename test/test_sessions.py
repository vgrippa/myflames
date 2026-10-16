"""Sessions view: the process list joined with InnoDB transactions, flagged and summarized.

fixtures/perf/sessions-<server>.json replay a whole report (every statement
and the mysql --batch text it returned) recorded from throwaway Docker
containers (MySQL 8.4.11, 9.7.2, 26.7.0; MariaDB 11.4.13 and 13.0.2) while a
scenario held a transaction idle with a row lock, made another session wait
for that lock, queued ALTER TABLE behind it (a metadata lock wait), kept an
old read view open, ran SELECT SLEEP, and ran a scan and update load; plus a
run as an account without PROCESS (-noproc) and a MySQL 8.4 server with
performance_schema=OFF (-ps-off).
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from _perf_replay import FakeFactory, PASSWORD, ReplayConn, batch, fixture, run_cli, run_main
from myflames import sessions, ui
from myflames.connector import ConnectorError
from myflames.sessions import sessions_report, split_host, thread_kind

SERVERS = ('mysql-8.4', 'mysql-9.7', 'mysql-26.7', 'mariadb-11.4', 'mariadb-13.0')


def replay(server, edit=None):
    calls = list(fixture('sessions', server)['calls'])
    # Identity answers are synthetic additions to the older live recordings.
    identity_sql = ('SELECT @@server_id, @@hostname, @@version' if server.startswith('mariadb')
                    else 'SELECT @@server_uuid')
    identity = [['1', 'server-one', '11.4.13-MariaDB']] if server.startswith('mariadb') else [['server-uuid-1']]
    calls.append([sessions.SESSIONS_TAG + identity_sql, batch(identity)])
    return ReplayConn(calls, edit=edit)


def report_for(server, **kwargs):
    return sessions_report(replay(server), **dict({'active_only': False}, **kwargs))


def by_flag(report, flag):
    return [s for s in report['sessions'] if flag in s['flags']]


class TestHelpers(unittest.TestCase):
    def test_split_host(self):
        self.assertEqual(split_host('172.17.0.1:62158'), ('172.17.0.1', 62158))
        self.assertEqual(split_host('::1:51234'), ('::1', 51234))
        self.assertEqual(split_host('app-7.internal:3306'), ('app-7.internal', 3306))
        self.assertEqual(split_host('localhost'), ('localhost', None))
        self.assertEqual(split_host(''), (None, None))
        self.assertEqual(split_host(None), (None, None))

    def test_thread_kind(self):
        self.assertEqual(thread_kind('event_scheduler', 'Daemon', 'Waiting on empty queue'), 'daemon')
        self.assertEqual(thread_kind('system user', 'Daemon', 'Suspending'), 'daemon')
        self.assertEqual(thread_kind('system user', 'Connect', 'Waiting for source to send event'), 'replication')
        self.assertEqual(thread_kind('system user', 'Query',
                                     'Replica has read all relay log; waiting for more updates'), 'replication')
        self.assertEqual(thread_kind('repl', 'Binlog Dump GTID', 'Source has sent all binlog to replica'),
                         'replication')
        self.assertEqual(thread_kind('system user', 'Slave_SQL', None), 'replication')
        self.assertEqual(thread_kind('app', 'Query', 'executing'), 'client')

    def test_argument_validation(self):
        conn = ReplayConn([])
        for kwargs in ({'active_only': 'yes'}, {'min_seconds': -1}, {'min_seconds': float('nan')},
                       {'min_seconds': True}, {'limit': 0}, {'limit': 10001}, {'user': 'x' * 256},
                       {'db': 5}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    sessions_report(conn, **kwargs)
        self.assertEqual(conn.calls, [])


class TestLiveReplay(unittest.TestCase):
    def test_report_captures_server_identity_once_for_both_engines(self):
        for server, expected in (
                ('mysql-8.4', {'server_uuid': 'server-uuid-1'}),
                ('mariadb-11.4', {'server_id': '1', 'hostname': 'server-one', 'version': '11.4.13-MariaDB'})):
            with self.subTest(server=server):
                conn = replay(server)
                report = sessions_report(conn, identity=True)
                self.assertEqual(report['server_identity'], expected)
                self.assertEqual(sum('SELECT @@server_' in sql for sql, _ in conn.calls), 1)

    def test_missing_or_denied_identity_keeps_the_report_readable(self):
        for answer in ('', batch([[None]]), batch([['one', 'extra']]), ConnectorError('ERROR 1227: denied')):
            with self.subTest(answer=answer):
                def edit(sql, text, n):
                    if 'SELECT @@server_uuid' in sql:
                        if isinstance(answer, Exception):
                            raise answer
                        return answer
                    return text
                conn = replay('mysql-8.4', edit=edit)
                report = sessions_report(conn, identity=True)
                self.assertEqual(report['server_identity'], {})
                self.assertTrue(report['sessions'])

    def test_every_server_flags_the_scenario(self):
        for server in SERVERS:
            with self.subTest(server=server):
                conn = replay(server)
                report = sessions_report(conn, active_only=False)
                self.assertEqual(report['schema_version'], 'sessions-1.0')
                for sql, flags in conn.calls:
                    self.assertTrue(sql.startswith(sessions.SESSIONS_TAG))
                    body = sql[len(sessions.SESSIONS_TAG):]
                    if body.startswith(sessions.SYSTEM_TIME_ZONE):
                        # The only session setting: trx_started is in the OS time zone.
                        body = body[len(sessions.SYSTEM_TIME_ZONE):]
                        self.assertTrue(body.startswith(sessions.SESSIONS_TAG))
                        body = body[len(sessions.SESSIONS_TAG):]
                    self.assertRegex(body, r'^(SELECT|SHOW GLOBAL) ')
                    self.assertNotIn(';', body)
                    self.assertNotRegex(sql, r'\b(KILL|UPDATE|INSERT|DELETE|SET GLOBAL)\b')
                    self.assertIn('--comments', flags)
                self.assertIn('ID <> CONNECTION_ID()', next(sql for sql, _ in conn.calls if 'PROCESSLIST' in sql
                                                            or 'processlist' in sql))
                idle = by_flag(report, 'idle_in_trx')
                self.assertEqual(len(idle), 2)
                for s in idle:
                    self.assertEqual(s['command'], 'Sleep')
                    self.assertIsNotNone(s['trx'])
                    self.assertEqual(s['status'], 'problem')
                kinds = {s['lock_wait']['kind'] for s in by_flag(report, 'lock_wait')}
                self.assertEqual(kinds, {'innodb', 'metadata'})
                row = next(s for s in by_flag(report, 'lock_wait') if s['lock_wait']['kind'] == 'innodb')
                self.assertEqual(row['trx']['state'], 'LOCK WAIT')
                self.assertIsNotNone(row['lock_wait']['wait_s'])
                sleep = next(s for s in report['sessions'] if (s['info'] or '').startswith('SELECT SLEEP(3600)'))
                self.assertIn('long_running', sleep['flags'])
                self.assertEqual(sleep['status'], 'problem')
                self.assertTrue(sleep['explainable'])
                summary = report['summary']
                self.assertEqual(summary['idle_in_trx'], 2)
                self.assertEqual(summary['lock_wait']['total'], 2)
                self.assertGreaterEqual(summary['long_running']['problem'], 1)
                self.assertEqual(summary['oldest_transaction']['status'], 'problem')
                self.assertIsNotNone(summary['history_list_length'])
                self.assertEqual(summary['connections']['max'], 151)
                self.assertTrue(summary['text'].startswith('%d connections of 151' % summary['connections']['current']))
                self.assertEqual(report['capabilities'], {'process_privilege': True, 'transactions': True,
                                                          'history_list_length': True})
                self.assertEqual([a['id'] for a in report['attention']['idle_in_trx']],
                                 sorted([s['id'] for s in idle], key=lambda i: -next(
                                     s['trx']['age_s'] for s in idle if s['id'] == i)))
                json.dumps(report, allow_nan=False)

    def test_engine_specific_columns_and_sources(self):
        mysql = report_for('mysql-8.4')
        self.assertEqual(mysql['source'], 'performance_schema.processlist')
        self.assertTrue(all(s['progress'] is None and s['memory_used'] is None for s in mysql['sessions']))
        daemon = next(s for s in mysql['sessions'] if s['user'] == 'event_scheduler')
        self.assertEqual((daemon['thread_kind'], daemon['flags'], daemon['status']), ('daemon', ['system_thread'], 'ok'))
        maria = report_for('mariadb-11.4')
        self.assertEqual(maria['source'], 'information_schema.PROCESSLIST')
        self.assertTrue(any(isinstance(s['time_s'], float) for s in maria['sessions']), 'TIME_MS keeps milliseconds')
        self.assertTrue(all(s['memory_used'] is not None for s in maria['sessions']))
        processlist = next(sql for sql, _ in replay('mariadb-11.4').answers.items() if 'PROCESSLIST' in sql)
        self.assertIn('TIME_MS / 1000', processlist)
        self.assertIn('PROGRESS', processlist)

    def test_last_statement_of_idle_transactions(self):
        report = report_for('mysql-8.4')
        last = {s['id']: s['last_statement'] for s in by_flag(report, 'idle_in_trx')}
        self.assertIn('SELECT * FROM u WHERE id = 1', last.values())
        maria = report_for('mariadb-11.4')
        self.assertTrue(all(s['last_statement'] is None for s in maria['sessions']))
        self.assertTrue(any('needs Performance Schema' in n for n in maria['notes']))

    def test_performance_schema_off_uses_information_schema(self):
        report = sessions_report(replay('mysql-8.4-ps-off'))
        self.assertEqual(report['source'], 'information_schema.PROCESSLIST')
        self.assertFalse(report['performance_schema'])
        self.assertEqual(len(by_flag(report, 'idle_in_trx')), 1)
        self.assertEqual(len(by_flag(report, 'lock_wait')), 1)
        self.assertTrue(any('needs Performance Schema' in n for n in report['notes']))

    def test_without_process_only_own_sessions_and_no_transactions(self):
        for server in ('mysql-8.4-noproc', 'mariadb-11.4-noproc'):
            with self.subTest(server=server):
                report = sessions_report(replay(server), active_only=False)
                self.assertFalse(report['capabilities']['process_privilege'])
                self.assertFalse(report['capabilities']['transactions'])
                self.assertEqual({s['user'] for s in report['sessions']}, {'noproc'})
                self.assertTrue(all(s['trx'] is None for s in report['sessions']))
                self.assertIsNone(report['summary']['oldest_transaction'])
                self.assertIn('GRANT PROCESS', report['notes'][0])
                self.assertGreater(report['summary']['connections']['current'], report['summary']['total'])


class TestFilters(unittest.TestCase):
    def test_active_only_keeps_running_idle_in_trx_and_waiting(self):
        everything = report_for('mysql-8.4')
        active = sessions_report(replay('mysql-8.4'))
        self.assertEqual(active['filters']['active_only'], True)
        self.assertEqual(active['summary'], everything['summary'], 'the summary ignores the filters')
        ids = {s['id'] for s in active['sessions']}
        for s in everything['sessions']:
            keep = (s['command'] != 'Sleep' and s['thread_kind'] == 'client') or 'idle_in_trx' in s['flags'] \
                or 'lock_wait' in s['flags']
            self.assertEqual(s['id'] in ids, keep, s)

    def test_user_host_db_and_min_seconds(self):
        report = sessions_report(replay('mysql-8.4-noproc'), active_only=False, user='nobody')
        self.assertEqual((report['sessions'], report['matched']), ([], 0))
        everything = report_for('mysql-8.4')
        host = everything['sessions'][0]['client_host']
        port = everything['sessions'][0]['client_port']
        by_host = sessions_report(replay('mysql-8.4'), active_only=False, host=host)
        self.assertTrue(by_host['sessions'])
        self.assertTrue(all(s['client_host'] == host for s in by_host['sessions']))
        exact = sessions_report(replay('mysql-8.4'), active_only=False, host='%s:%s' % (host, port))
        self.assertEqual(len(exact['sessions']), 1)
        by_db = sessions_report(replay('mysql-8.4'), active_only=False, db='mf')
        self.assertTrue(all(s['db'] == 'mf' for s in by_db['sessions']))
        slow = sessions_report(replay('mysql-8.4'), active_only=False, min_seconds=60)
        self.assertTrue(slow['sessions'])
        self.assertTrue(all(s['time_s'] >= 60 for s in slow['sessions']))
        limited = sessions_report(replay('mysql-8.4'), active_only=False, limit=2)
        self.assertEqual(len(limited['sessions']), 2)
        self.assertTrue(limited['truncated'])
        self.assertEqual(limited['sessions'][0]['status'], 'problem', 'problems sort first')


def processlist_row(id_, user='app', host='10.0.0.5:5000', db='shop', command='Query', seconds=1, state='executing',
                    info='SELECT 1', length=None):
    return [id_, user, host, db, command, seconds, state, info, length if length is not None else len(info or '')]


def synthetic(rows, trx=(), version='8.4.11', ps='ON', status=None, history='12', events=''):
    status = status or [['Max_used_connections', '9'], ['Threads_connected', '8'], ['Threads_running', '3'],
                        ['Uptime', '1000']]
    answers = [('SELECT @@server_uuid', batch([['server-uuid-1']])),
               ('SELECT @@server_id, @@hostname, @@version', batch([['1', 'server-one', version]])),
               ('events_statements_current', events),
               ('SHOW GLOBAL VARIABLES', batch([['max_connections', '10'], ['performance_schema', ps],
                                                ['version', version], ['version_comment', 'MySQL']])),
               ('SHOW GLOBAL STATUS', batch(status)),
               ('processlist', batch(rows)), ('PROCESSLIST', batch(rows)),
               ('INNODB_TRX', batch(trx)),
               ('INNODB_METRICS', batch([[history, 'enabled']]))]

    class Conn(object):
        def __init__(self):
            self.calls = []

        def run(self, sql, extra_flags=None, timeout=None):
            self.calls.append(sql)
            for needle, text in answers:
                if needle in sql:
                    return text
            raise AssertionError(sql)
    return Conn()


def trx_row(conn_id, age, state='RUNNING', wait=None):
    return [conn_id, '100%d' % conn_id, state, '2026-10-14 10:00:00', age, '2026-10-14 10:00:00' if wait else None,
            wait, 1, 1, 2, 1, 'REPEATABLE READ', '0', None]


class TestRules(unittest.TestCase):
    def test_long_running_thresholds_and_own_connection(self):
        conn = synthetic([processlist_row(1, seconds=9), processlist_row(2, seconds=10), processlist_row(3, seconds=60),
                          processlist_row(4, seconds=500, info='/*myflames:capture*/ SELECT 1'),
                          processlist_row(5, seconds=500, command='Sleep', info=None)])
        report = sessions_report(conn, active_only=False)
        status = {s['id']: (s['status'], s['flags']) for s in report['sessions']}
        self.assertEqual(status[1], ('ok', []))
        self.assertEqual(status[2], ('watch', ['long_running']))
        self.assertEqual(status[3], ('problem', ['long_running']))
        self.assertEqual(status[4], ('ok', ['own_connection', 'long_running']))
        self.assertEqual(status[5], ('ok', []))
        self.assertEqual(report['summary']['long_running'], {'watch': 1, 'problem': 1})
        self.assertEqual([a['id'] for a in report['attention']['long_running']], [3, 2])

    def test_idle_in_transaction_graded_on_the_transaction_age(self):
        conn = synthetic([processlist_row(1, command='Sleep', seconds=2, info=None, state=None),
                          processlist_row(2, command='Sleep', seconds=2, info=None, state=None),
                          processlist_row(3, command='Sleep', seconds=2, info=None, state=None)],
                         trx=[trx_row(1, 5), trx_row(2, 30), trx_row(3, 90)],
                         events=batch([[2, 'UPDATE t SET a = 1']]))
        report = sessions_report(conn)
        status = {s['id']: s['status'] for s in report['sessions']}
        self.assertEqual(status, {1: 'ok', 2: 'watch', 3: 'problem'})
        self.assertEqual([a['id'] for a in report['attention']['idle_in_trx']], [3, 2, 1])
        self.assertEqual(next(s for s in report['sessions'] if s['id'] == 2)['last_statement'], 'UPDATE t SET a = 1')
        self.assertEqual(report['summary']['oldest_transaction']['connection_id'], 3)
        self.assertIn('Idle for 2 s inside a transaction open for 1 min 30 s',
                      next(s for s in report['sessions'] if s['id'] == 3)['why'])

    def test_idle_in_transaction_ignores_the_two_read_skew(self):
        # The process list saw the session idle for 5 s; by the time INNODB_TRX
        # was read it had started a new statement and transaction (age 0).
        conn = synthetic([processlist_row(1, command='Sleep', seconds=5, info=None, state=None),
                          processlist_row(2, command='Sleep', seconds=0, info=None, state=None)],
                         trx=[trx_row(1, 0), trx_row(2, 0)])
        report = sessions_report(conn, active_only=False)
        self.assertEqual([s['flags'] for s in report['sessions']], [[], []])

    def test_lock_wait_kinds(self):
        conn = synthetic([processlist_row(1, seconds=3, state='updating', info='UPDATE t SET a = 2'),
                          processlist_row(2, seconds=4, state='Waiting for table metadata lock', info='ALTER TABLE t'),
                          processlist_row(3, seconds=5, state='Waiting for table level lock', info='UPDATE m SET a=1'),
                          processlist_row(4, seconds=5, state='Waiting for global read lock', info='INSERT INTO t VALUES (1)')],
                         trx=[trx_row(1, 3, state='LOCK WAIT', wait=3)])
        report = sessions_report(conn)
        waits = {s['id']: s['lock_wait']['kind'] for s in report['sessions'] if s['lock_wait']}
        self.assertEqual(waits, {1: 'innodb', 2: 'metadata', 3: 'table', 4: 'metadata'})
        self.assertTrue(all(s['status'] == 'watch' for s in report['sessions']))
        self.assertEqual(report['summary']['lock_wait'], {'innodb': 1, 'metadata': 2, 'table': 1, 'total': 4})
        self.assertEqual(next(s for s in report['sessions'] if s['id'] == 1)['why'],
                         'Waiting for an InnoDB lock (row or table) for 3 s.')
        self.assertTrue(sessions._TRX_SQL.startswith(sessions.SYSTEM_TIME_ZONE))

    def test_connections_and_history_list_grades(self):
        conn = synthetic([processlist_row(1)], history='2000000',
                         status=[['Threads_connected', '10'], ['Threads_running', '1'], ['Max_used_connections', '10']])
        summary = sessions_report(conn)['summary']
        self.assertEqual(summary['connections']['used_pct'], 100.0)
        self.assertEqual(summary['connections']['status'], 'problem')
        self.assertEqual(summary['history_list_length']['status'], 'problem')
        self.assertEqual(summary['status'], 'problem')

    def test_truncated_statements_and_breakdowns(self):
        long_info = 'SELECT ' + 'x' * 1100
        conn = synthetic([processlist_row(i, user='u%d' % (i % 12), info=long_info[:1024], length=1107)
                          for i in range(1, 25)])
        report = sessions_report(conn, active_only=False)
        self.assertTrue(all(s['info_truncated'] for s in report['sessions']))
        self.assertTrue(report['sessions'][0]['info_short'].endswith('…'))
        self.assertLessEqual(len(report['sessions'][0]['info_short']), sessions.SHORT_WIDTH)
        self.assertTrue(any('1024 bytes' in n for n in report['notes']))
        by_user = report['summary']['by_user']
        self.assertEqual(len(by_user), sessions.TOP_N + 1)
        self.assertEqual(by_user[-1], {'key': None, 'other': True, 'count': 4})

    def test_mariadb_progress(self):
        row = processlist_row(7, info='ALTER TABLE big ENGINE=InnoDB', seconds='123.456') + [1, 2, '45.678', 4096, 99]
        conn = synthetic([row], version='11.4.13-MariaDB', ps='OFF',
                         status=[['Threads_connected', '2'], ['Innodb_history_list_length', '5']])
        report = sessions_report(conn)
        s = report['sessions'][0]
        self.assertEqual(s['progress'], {'stage': 1, 'max_stage': 2, 'pct': 45.7})
        self.assertEqual((s['memory_used'], s['rows_examined'], s['time_s']), (4096, 99, 123.456))
        self.assertEqual(report['summary']['history_list_length']['source'], 'Innodb_history_list_length')

    def test_processlist_denied_is_an_error(self):
        class Denied(object):
            def run(self, sql, extra_flags=None, timeout=None):
                if 'processlist' in sql:
                    raise ConnectorError('ERROR 1142 (42000): SELECT command denied to user')
                return synthetic([]).run(sql)
        with self.assertRaisesRegex(ValueError, 'cannot read performance_schema.processlist'):
            sessions_report(Denied())


class Closable(object):
    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class TestCLI(unittest.TestCase):
    def call(self, command, conn, *args):
        return run_cli(command, ['-h', 'db', '-u', 'admin', '-p' + PASSWORD] + list(args), conn)

    def test_text_summary_first_then_flagged_then_the_list(self):
        code, out, err, factory = self.call('sessions', replay('mysql-8.4'))
        self.assertEqual(code, 0, err)
        self.assertLess(out.index('SUMMARY'), out.index('FLAGGED'))
        self.assertLess(out.index('FLAGGED'), out.index('COMMAND'))
        self.assertIn('idle_in_trx', out)
        self.assertIn('last: SELECT * FROM u WHERE id = 1', out)
        self.assertIn('note: ', err)
        self.assertEqual(factory.options['query_timeout'], 12)
        code, _, err, _ = self.call('sessions', replay('mysql-8.4'), '-q')
        self.assertEqual((code, err), (0, ''))

    def test_json_filters_and_flags(self):
        code, out, _, _ = self.call('sessions', replay('mariadb-13.0'), '--json', '--all', '--session-user', 'root',
                                    '--schema', 'mf', '--min-seconds', '1.5', '--limit', '50')
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report['schema_version'], 'sessions-1.0')
        self.assertEqual(report['filters'], {'active_only': False, 'user': 'root', 'host': None, 'db': 'mf',
                                             'min_seconds': 1.5})
        self.assertEqual(report['limit'], 50)
        code, out, _, _ = self.call('sessions', replay('mariadb-13.0'), '--json', '--session-host', '172.17.0.1')
        self.assertEqual(json.loads(out)['filters']['host'], '172.17.0.1')

    def test_bad_input_exits_2(self):
        for args in (['--min-seconds', '-1'], ['--min-seconds', 'soon'], ['--limit', '0']):
            with self.subTest(args=args):
                code, out, _, factory = self.call('sessions', replay('mysql-8.4'), *args)
                self.assertEqual((code, out), (2, ''))
                self.assertIsNone(factory.options)
        conn = ReplayConn([], error=ConnectorError('ERROR 2003: cannot connect (password %s)' % PASSWORD))
        code, _, err, _ = self.call('sessions', conn)
        self.assertEqual(code, 2)
        self.assertNotIn(PASSWORD, err)

    def test_output_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 's.json')
            code, out, _, _ = self.call('sessions', replay('mysql-9.7'), '--json', '-o', path)
            self.assertEqual((code, out), (0, ''))
            with open(path, encoding='utf-8') as handle:
                self.assertEqual(json.load(handle)['schema_version'], 'sessions-1.0')

    def test_explain_moved_from_running(self):
        plan = json.dumps({'query': 'SELECT 1', 'operation': 'Rows fetched before execution',
                           'access_type': 'rows_fetched_before_execution'})

        class Explain(Closable):
            def run(self, sql, extra_flags=None, timeout=None):
                if 'FOR CONNECTION' in sql:
                    return plan
                if '@@version' in sql:
                    return batch([['8.4.3', 1]])
                return batch([[42, 'app', 'h:1', 'db', 'Query', 3, 'executing', 'SELECT 1']])

            def server_version(self):
                return '8.4.3'

            def is_mariadb(self):
                return False
        code, out, err, factory = self.call('sessions', Explain(), '--explain', '42')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['query'], 'SELECT 1')
        self.assertEqual(factory.options['query_timeout'], 40)

    def test_running_is_a_deprecated_alias_with_its_old_output(self):
        class Running(Closable):
            def run(self, sql, extra_flags=None, timeout=None):
                if '@@version' in sql:
                    return batch([['8.4.3', 1]])
                return batch([[9, 'app', 'h:1', 'db', 'Query', 3, 'x', 'SELECT 2']])
        code, out, err, _ = self.call('running', Running(), '--json')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['schema_version'], 'running-1.0')
        self.assertEqual(json.loads(out)['statements'][0]['id'], 9)
        self.assertIn("warning: 'running' is deprecated; use 'sessions'", err)
        code, out, err, _ = self.call('running', Running(), '--json', '-q')
        self.assertEqual((code, err), (0, ''), '-q also silences the deprecation warning')

    def test_root_dispatch_and_help(self):
        code, out, _ = run_main(['myflames', 'sessions', '-h', 'db', '-p' + PASSWORD, '--json'], replay('mysql-8.4'))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['schema_version'], 'sessions-1.0')
        code, out, _ = run_main(['myflames', '--help'])
        self.assertIn('sessions  Every connection', out)
        self.assertIn('running   Deprecated alias of sessions', out)


class TestSessionsKillCLI(unittest.TestCase):
    """The sessions alias retains the real locks confirmation and audit path."""

    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        patch = mock.patch.dict(os.environ, {'HOME': home.name, 'USERPROFILE': home.name,
                                            'MYFLAMES_SECRET_BACKEND': 'vault'})
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)

    def call(self, scenario, *args, **kwargs):
        import test_locks as fixtures
        from myflames import locks
        return run_cli('sessions', list(args), scenario.conn, patches=(
            mock.patch('sys.stdin', kwargs.get('stdin') or fixtures.FakeStdin('', tty=False)),
            mock.patch.object(locks.time, 'sleep')))

    def records(self):
        from myflames.workspace_store import WorkspaceStore
        return WorkspaceStore().audit_records()

    def test_confirmed_kill_json_quiet_output_file_and_audit(self):
        import test_locks as fixtures
        for version in ('8.4.6', '11.4.13-MariaDB'):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as tmp:
                scenario = fixtures.KillScenario(ma_waits=[], ma_locks=[], grants=(
                    fixtures.MARIADB_GRANTS if 'MariaDB' in version else fixtures.MYSQL_GRANTS))
                scenario.conn.version = version
                scenario.conn.responses[0] = (fixtures.VERSION_SQL, batch([[version, 1]]))
                path = os.path.join(tmp, 'kill.json')
                code, out, err, _ = self.call(scenario, '--kill', '41', '--yes', '--json', '-q', '-o', path)
                self.assertEqual((code, out, err), (0, '', ''))
                with open(path, encoding='utf-8') as handle:
                    result = json.load(handle)
                self.assertEqual((result['schema_version'], result['status']), ('kill-1.0', 'killed'))
                self.assertEqual(result['engine'], 'mariadb' if 'MariaDB' in version else 'mysql')
                self.assertEqual(scenario.kills(), [fixtures.KILL_TAG + 'KILL CONNECTION 41'])
                record = next(r for r in self.records() if r['id'] == result['audit_id'])
                self.assertEqual((record['status'], record['operation']), ('ok', 'kill'))
                self.assertIn('server identity', record['verified'])

    def test_query_mode_stops_only_the_running_statement(self):
        import test_locks as fixtures
        running = list(fixtures.K_TARGET)
        running[5], running[8] = 'Query', 'UPDATE t SET v = v + 1 WHERE id = 1'
        scenario = fixtures.KillScenario(target=running, after=[fixtures.K_TARGET],
                                        after_trx=fixtures.K_TARGET_TRX)
        code, out, err, _ = self.call(scenario, '--kill', '41', '--query', '--yes', '--json', '-q')
        self.assertEqual((code, err), (0, ''))
        result = json.loads(out)
        self.assertEqual((result['mode'], result['status']), ('query', 'killed'))
        self.assertEqual(scenario.kills(), [fixtures.KILL_TAG + 'KILL QUERY 41'])
        self.assertTrue(result['after']['present'])
        self.assertEqual(self.records()[0]['operation'], 'kill_query')

    def test_confirmation_decline_nonterminal_and_idle_query_refusal(self):
        import test_locks as fixtures
        for flags, stdin, status in (
                ([], fixtures.FakeStdin('n\n'), 'declined'),
                ([], fixtures.FakeStdin('y\n', tty=False), 'refused'),
                (['--query', '--yes'], fixtures.FakeStdin('', tty=False), 'refused')):
            with self.subTest(flags=flags, status=status):
                scenario = fixtures.KillScenario()
                code, out, err, _ = self.call(scenario, '--kill', '41', '--json', *flags, stdin=stdin)
                self.assertEqual((code, out, scenario.kills()), (2, '', []))
                self.assertTrue(err)
                record = self.records()[0]
                self.assertEqual(record['status'], status)

    def test_tty_confirmation_identity_change_refuses(self):
        import test_locks as fixtures
        scenario = fixtures.KillScenario()
        reads = []

        def identity(sql):
            reads.append(sql)
            return batch([['server-uuid-1' if len(reads) <= 2 else 'replacement-server']])

        scenario.conn.responses = [(p, identity if p == 'SELECT @@server_uuid' else value)
                                   for p, value in scenario.conn.responses]
        code, out, err, _ = self.call(scenario, '--kill', '41', stdin=fixtures.FakeStdin('yes\n'))
        self.assertEqual((code, out, scenario.kills()), (2, '', []))
        self.assertIn('[y/N]', err)
        self.assertIn('server identity', err)
        self.assertEqual(self.records()[0]['outcome'], 'identity')

    def test_read_only_refuses_before_password_and_connection(self):
        import test_locks as fixtures
        from myflames.workspace_store import WorkspaceStore
        WorkspaceStore().save('profile', {'name': 'readonly', 'host': 'db', 'read_only': True})
        scenario = fixtures.KillScenario()
        with mock.patch('myflames.workspace_cli.getpass.getpass', side_effect=AssertionError('password')):
            code, out, err, factory = self.call(scenario, '--profile', 'readonly', '-p',
                                               '--kill', '41', '--yes', '--json', '-q')
        self.assertEqual((code, out), (2, ''), err)
        self.assertIn('read-only', err)
        self.assertIsNone(factory.options)
        self.assertEqual(self.records()[0]['outcome'], 'read_only')

    def test_invalid_kill_flags_fail_before_connecting(self):
        import test_locks as fixtures
        for args in (['--query'], ['--yes'], ['--kill', '0'], ['--kill', 'x'],
                     ['--kill', '41', '--explain', '41']):
            with self.subTest(args=args):
                code, out, _, factory = self.call(fixtures.KillScenario(), *args)
                self.assertEqual((code, out), (2, ''))
                self.assertIsNone(factory.options)
        self.assertEqual(self.records(), [])


class TestAPI(unittest.TestCase):
    def call(self, conn, payload, path='/api/live/sessions'):
        workspace = mock.Mock()
        workspace.store.connection_with_saved_password.side_effect = lambda c: c
        with mock.patch('myflames.connector.MySQLConnection', FakeFactory(conn)):
            return ui.api_response(path, dict({'connection': {'host': '127.0.0.1', 'port': 3306, 'user': 'root'}},
                                              **payload), workspace)

    def test_route_defaults_to_active_only_and_passes_filters(self):
        report = self.call(replay('mysql-26.7'), {})
        self.assertEqual(report['schema_version'], 'sessions-1.0')
        self.assertTrue(report['filters']['active_only'])
        report = self.call(replay('mysql-26.7'), {'active_only': False, 'user': 'root', 'host': '',
                                                  'db': 'mf', 'min_seconds': 2, 'limit': 5})
        self.assertEqual(report['filters'], {'active_only': False, 'user': 'root', 'host': None, 'db': 'mf',
                                             'min_seconds': 2})
        self.assertEqual(report['limit'], 5)

    def test_bad_payloads_are_rejected(self):
        for payload in ({'active_only': 'false'}, {'limit': 'all'}, {'min_seconds': -5}):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    self.call(replay('mysql-26.7'), payload)

    def test_route_is_allowed_and_running_still_works(self):
        import inspect
        source = inspect.getsource(ui.WorkspaceHandler.do_POST)
        for path in ('/api/live/sessions', '/api/live/running'):
            self.assertIn(path, ui._SERVER_VIEW_PATHS)
            self.assertIn('"%s"' % path, source)


if __name__ == '__main__':
    unittest.main()
