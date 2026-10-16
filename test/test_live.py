"""Deterministic query capture tests; no server or mysql client required."""
import copy
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from myflames.connector import ConnectorError, MySQLConnection
from myflames import dbops
from myflames.live import (LiveJob, LiveJobs, bind_query, capture_problem, connection_options,
                           measurement_summary, _sections, _status)
from myflames.parser import parse_explain

PLAN = (Path(__file__).parent / 'mysql-explain-json-sample.json').read_text()
MARIA = (Path(__file__).parent / 'mariadb-explain-join.json').read_text()


def payload(**extra):
    value = {'connection': {'host': 'localhost', 'password': 'private-secret'},
             'query': 'SELECT * FROM test.joinit WHERE i > :minimum',
             'parameters': {'minimum': 50}, 'mode': 'analyze'}
    value.update(extra)
    return value


def captured(marker, raw=PLAN, statistics=True, trace=False):
    values = [('connection', '42'), ('before', 'Handler_read_next\t10\nSort_rows\t2'),
              ('plan', raw), ('after', 'Handler_read_next\t18\nSort_rows\t5\nNew_counter\t9'),
              ('statistics', json.dumps({'rows_examined': 8, 'rows_sent': 3}) if statistics else '')]
    if trace:
        values.append(('trace', '{\n "steps": [{"condition": "tab\\tvalue"}]\n}\t12\t0'))
    values.append(('done', ''))
    return ''.join(marker + ':' + key + '\n' + value + '\n' for key, value in values)


class TestQueryValidation(unittest.TestCase):
    def test_defaults_tls_and_credentials(self):
        options = connection_options({'port': '3307', 'password': 'new\nline',
                                      'ssl_mode': 'VERIFY_IDENTITY', 'ssl_ca': '/tmp/ca.pem'})
        self.assertEqual(options['host'], '127.0.0.1')
        self.assertEqual(options['port'], 3307)
        self.assertEqual(options['password'], 'new\nline')
        self.assertEqual(options['ssl_mode'], 'VERIFY_IDENTITY')
        self.assertNotIn('binary', connection_options({'binary': '/tmp/evil'}))
        for value in (None, [], {'port': 0}, {'port': 65536}, {'port': -1}, {'port': 'bad'},
                      {'port': True}, {'port': 3306.5}, {'port': float('inf')},
                      {'host': 'a\nuser=b'}, {'user': 123}, {'database': 'a\0b'},
                      {'ssl_mode': 'invalid'}, {'host': 'x' * 4097}):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                connection_options(value)

    def test_named_parameters_are_typed_and_never_sql_fragments(self):
        sql = bind_query('SELECT :text, :n, :f, :yes, :no, :nil',
                         {'text': "a'; DROP TABLE users; -- ☃", 'n': 42, 'f': 1.5,
                          'yes': True, 'no': False, 'nil': None})
        self.assertIn("CONVERT(X'", sql)
        self.assertNotIn('DROP', sql)
        self.assertTrue(sql.endswith(', 42, 1.5, 1, 0, NULL'))
        for parameters in ({}, {'x': []}, {'x': {}}, {'x': float('nan')}, {'x': float('inf')}, {'x': 10 ** 400}):
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                bind_query('SELECT :x', parameters)
        for parameters in ([], False, 0, 'text'):
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                bind_query('SELECT 1', parameters)

    def test_quotes_identifiers_and_comments_are_not_parameterized(self):
        query = "SELECT ':missing', \"a;:missing\", `:missing`, :x /* :missing */ -- :missing\n# :missing\n"
        result = bind_query(query, {'x': 4})
        self.assertIn('`:missing`, 4', result)
        self.assertIn("':missing'", result)
        self.assertIn('/* :missing */', result)
        self.assertEqual(bind_query('WITH c AS (SELECT 1 n) SELECT n FROM c;'),
                         'WITH c AS (SELECT 1 n) SELECT n FROM c')

    def test_rejects_multiple_statements_and_write_capabilities(self):
        queries = ('DELETE FROM t', 'SELECT 1; SELECT 2', 'SELECT * FROM t FOR UPDATE',
                   "SELECT 1 INTO OUTFILE '/tmp/out'", 'WITH c AS (SELECT 1) DELETE FROM t',
                   'SELECT 1 /*! INTO OUTFILE "/tmp/out" */',
                   'SELECT 1 /*M! INTO OUTFILE "/tmp/out" */',
                   'SELECT @x := 1', 'SELECT 1\n\\! touch /tmp/out',
                   'SELECT 1\nDELIMITER $$\nSELECT 2', 'SELECT 1\x00')
        for query in queries:
            with self.subTest(query=query), self.assertRaises(ValueError):
                bind_query(query)

    def test_capture_rejects_function_side_effects_before_connecting(self):
        from myflames.live import capture_problem
        for query in (
                'SELECT group_replication_switch_to_multi_primary_mode()',
                'SELECT asynchronous_connection_failover_reset()',
                'SELECT unknown_function()', 'SELECT app.COUNT(1)',
                'SELECT `app`.`mutate_rows`()', 'SELECT "mutate_rows"()',
                'SELECT COALESCE(mutate_rows(),0)',
                'WITH c AS (SELECT mutate_rows()) SELECT * FROM c',
                'SELECT NEXTVAL(s)', 'SELECT SETVAL(s,42)', 'SELECT NEXT VALUE FOR s',
                'SELECT s.NEXTVAL', 'SELECT "s"."NEXTVAL"',
                "SELECT GET_LOCK('n',0)", "SELECT RELEASE_LOCK('n')", 'SELECT RELEASE_ALL_LOCKS()',
                'SELECT LAST_INSERT_ID(42)', 'SELECT COUNT /* comment */ (1)'):
            with self.subTest(query=query):
                self.assertIsNotNone(capture_problem(query))
                with self.assertRaises(ValueError):
                    bind_query(query)
                with patch('myflames.live.MySQLConnection') as connection:
                    with self.assertRaises(ValueError):
                        LiveJob(payload(query=query))
                    connection.assert_not_called()

    def test_capture_common_native_readers_and_ctes(self):
        from myflames.live import capture_problem
        for query in (
                'SELECT COUNT(*), COALESCE(SUM(x),0) FROM t',
                "SELECT JSON_EXTRACT(doc,'$.n'), ABS(n), DATE_FORMAT(NOW(),'%Y') FROM t",
                'WITH c(n) AS (SELECT 1) SELECT * FROM c WHERE n IN (1,2)',
                'WITH `c`(`n`) AS (SELECT 1) SELECT * FROM `c`',
                'SELECT ROW_NUMBER() OVER (ORDER BY id) FROM t',
                'SELECT CAST(n AS DECIMAL(10,2)) FROM t',
                "SELECT 'mutate_rows()', `mutate_rows` FROM t"):
            with self.subTest(query=query):
                self.assertIsNone(capture_problem(query))
                self.assertEqual(bind_query(query), query)

    def test_backslash_quote_cannot_split_statements_under_no_backslash_escapes(self):
        """With sql_mode NO_BACKSLASH_ESCAPES the mysql client reads '\\' as a
        whole string, so the text below is five statements to it; with
        backslash escapes it is one. Text that the two readings split
        differently is refused before anything runs."""
        from myflames.live import capture_problem
        for query in ("SELECT '\\'; COMMIT; SET SESSION TRANSACTION READ WRITE; DROP TABLE victim; SELECT 'x'",
                      'SELECT "\\"; COMMIT; DROP TABLE victim; SELECT "x"',
                      "SELECT 'it\\'s'", "SELECT 'a\\''"):
            with self.subTest(query=query):
                problem = capture_problem(query)
                self.assertIn('NO_BACKSLASH_ESCAPES', problem)
                self.assertIn("doubling it ('it''s')", problem)
                with self.assertRaisesRegex(ValueError, 'NO_BACKSLASH_ESCAPES'):
                    bind_query(query)

    def test_escapes_that_mean_the_same_under_both_readings_are_accepted(self):
        from myflames.live import capture_problem
        for query in ("SELECT 'it''s'", 'SELECT "say ""hi"""', "SELECT 'a\\nb', 'C:\\\\dir'",
                      "SELECT * FROM t WHERE a = '\\\\'", "SELECT `odd``name` FROM t"):
            with self.subTest(query=query):
                self.assertIsNone(capture_problem(query))
                self.assertEqual(bind_query(query), query)

    def test_dash_comments_need_client_white_space(self):
        """The client reads "--" as a comment only before ASCII white space
        (my_isspace); Python's \\s also matches \\x1c-\\x1f and \\xa0. Before
        such a character the client sees code and splits at the ';', so the
        DROP TABLE below would run as its own statement. Before a control
        character the server sees a comment and the client code: refused."""
        from myflames.live import TOKEN, TOKEN_NO_BACKSLASH, capture_problem
        for dash in ('\x1c', '\x1d', '\x1e', '\x1f', '\x01', '\x7f'):
            query = 'SELECT 1 --%s; DROP TABLE victim' % dash
            with self.subTest(dash=repr(dash)):
                self.assertIn('space after the dashes', capture_problem(query))
                with self.assertRaises(ValueError):
                    bind_query(query)
                for token in (TOKEN, TOKEN_NO_BACKSLASH):
                    self.assertFalse(any(m.group().startswith('--') for m in token.finditer(query)))
        for query in ('SELECT 1 --\xa0; DROP TABLE victim', 'SELECT 1 --\u2028; DROP TABLE victim'):
            with self.subTest(query=repr(query)):
                self.assertIn('one SELECT', capture_problem(query))
        for space in (' ', '\t', '\n', '\r', '\f', '\v'):
            query = 'SELECT 1 --%sx\nFROM dual' % space
            with self.subTest(space=repr(space)):
                self.assertIsNone(capture_problem(query))
        self.assertIsNone(capture_problem('SELECT 1 --'))
        self.assertIsNone(capture_problem('SELECT 1--1'))

    def test_unclosed_strings_and_comments_are_refused(self):
        for query in ("SELECT 'abc", 'SELECT "abc', 'SELECT `abc', 'SELECT 1 /* open'):
            with self.subTest(query=query), self.assertRaisesRegex(ValueError, 'Close every'):
                bind_query(query)

    def test_query_and_job_limits(self):
        for query in (None, '', ' ', 'SELECT ' + 'x' * 200000):
            with self.subTest(query=str(query)[:30]), self.assertRaises(ValueError):
                bind_query(query)
        for extra in ({'repeat': 0}, {'repeat': 11}, {'repeat': 1.5}, {'repeat': True},
                      {'timeout': 0}, {'timeout': 301}, {'timeout': 1.2}, {'mode': 'execute'}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                LiveJob(payload(**extra))
        self.assertEqual(LiveJob(payload(mode='explain', repeat=10)).repeat, 1)


class TestCaptureProtocol(unittest.TestCase):
    def setUp(self):
        self.connection_patch = patch('myflames.live.MySQLConnection')
        self.connection_type = self.connection_patch.start()
        self.addCleanup(self.connection_patch.stop)
        self.conn = self.connection_type.return_value.__enter__.return_value
        self.conn.server_version.return_value = '9.7.0'
        self.conn.is_mariadb.return_value = False
        for name in ('collect_schema', 'collect_stats', 'collect_session_variables'):
            patcher = patch('myflames.live.' + name, return_value={'collected': name})
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_sections_keep_multiline_raw_json_and_status_deltas(self):
        sections = _sections(captured('marker'), 'marker')
        self.assertEqual(json.loads(sections['plan']), json.loads(PLAN))
        self.assertEqual(_status('Rows\t3\nnon-numeric\tbad\nnoise'), {'Rows': 3})
        job = LiveJob(payload(trace=True, repeat=3))
        scripts = []
        def run(conn, script, marker):
            self.assertIs(conn, self.conn)
            scripts.append(script)
            return captured(marker, trace=True), ''
        with patch.object(job, '_run_script', side_effect=run):
            job.run()
        self.assertEqual(job.state, 'complete', job.error)
        self.assertEqual(job.progress, 3)
        self.assertEqual(len(scripts), 3)
        for script in scripts:
            self.assertIn('START TRANSACTION READ ONLY;', script)
            self.assertIn('EXPLAIN ANALYZE FORMAT=JSON SELECT', script)
            self.assertIn('SET SESSION explain_json_format_version=2;', script)
            self.assertIn('performance_schema.events_statements_history', script)
            self.assertIn('CONNECTION_ID()', script)
            self.assertIn('ROLLBACK;', script)
            self.assertNotIn('private-secret', script)
            statement = script.index('EXPLAIN ANALYZE FORMAT=JSON SELECT')
            disabled = script.index("/*myflames:capture*/ SET SESSION optimizer_trace='enabled=off';", statement)
            next_marker = script.index("SELECT '__myflames_", statement)
            self.assertLess(disabled, next_marker)
            self.assertEqual(script[statement:disabled].splitlines()[-1], ';')
        result = job.result
        self.assertEqual(result['runs'][0]['session_status_delta'], {'Handler_read_next': 8, 'Sort_rows': 3})
        self.assertEqual(result['runs'][0]['statistics']['rows_examined'], 8)
        self.assertEqual(result['trace']['missing_bytes'], 12)
        self.assertEqual(result['trace']['data']['steps'][0]['condition'], 'tab\tvalue')
        self.assertEqual(result['measurements']['median_ms'], parse_explain(PLAN)['total_time'])
        self.assertEqual(result['measurements']['stdev_ms'], 0)
        self.assertNotIn('private-secret', json.dumps(job.snapshot()))
        self.assertNotIn('password', job.options)

    def test_safety_statements_precede_the_query_and_json_version_needs_8_3(self):
        """Every statement before the query must succeed on a supported
        server, because the client now stops at the first error: the
        explain_json_format_version SET (added in MySQL 8.3.0) is sent only
        where it exists."""
        for version, expected in (('8.0.36', False), ('8.2.0', False), ('8.3.0', True),
                                  ('8.4.6-log', True), ('9.7.0', True)):
            self.conn.server_version.return_value = version
            job = LiveJob(payload())
            scripts = []
            def run(conn, script, marker):
                scripts.append(script)
                return captured(marker), ''
            with self.subTest(version=version), patch.object(job, '_run_script', side_effect=run):
                job.run()
                self.assertEqual(job.state, 'complete', job.error)
                script = scripts[0]
                self.assertEqual('explain_json_format_version' in script, expected)
                query = script.index('EXPLAIN ANALYZE FORMAT=JSON SELECT')
                self.assertLess(script.index('SET SESSION TRANSACTION READ ONLY;'), query)
                self.assertLess(script.index('START TRANSACTION READ ONLY;'), query)

    def test_missing_optional_statistics_and_trace_are_warnings(self):
        job = LiveJob(payload(trace=True))
        with patch.object(job, '_run_script', side_effect=lambda c, s, m: (captured(m, statistics=False), '')):
            job.run()
        self.assertEqual(job.state, 'complete', job.error)
        self.assertIsNone(job.result['runs'][0]['statistics'])
        self.assertIsNone(job.result['trace'])
        self.assertEqual(len(job.result['warnings']), 2)

    def test_diagnostic_warnings_do_not_expose_connection_password(self):
        job = LiveJob(payload())
        with patch.object(job, '_run_script', side_effect=lambda c, s, m: (captured(m), 'warning private-secret')):
            job.run()
        self.assertEqual(job.state, 'complete', job.error)
        self.assertNotIn('private-secret', json.dumps(job.snapshot()))

    def test_metadata_permission_error_does_not_discard_plan(self):
        job = LiveJob(payload())
        with patch.object(job, '_run_script', side_effect=lambda c, s, m: (captured(m), '')), \
                patch('myflames.live.collect_schema', side_effect=ConnectorError('Access denied')):
            job.run()
        self.assertEqual(job.state, 'complete', job.error)
        self.assertNotIn('schema', job.result)
        self.assertIn('table_stats', job.result)
        self.assertTrue(any('schema' in warning for warning in job.result['warnings']))

    def test_explain_has_no_fabricated_measurements(self):
        job = LiveJob(payload(mode='explain'))
        with patch.object(job, '_run_script', side_effect=lambda c, s, m: (captured(m), '')):
            job.run()
        self.assertEqual(job.state, 'complete', job.error)
        self.assertIsNone(job.result['runs'][0]['total_time_ms'])
        self.assertIsNone(job.result['measurements']['median_ms'])
        self.assertEqual(job.result['measurements']['count'], 0)

    def test_mariadb_uses_its_analyze_syntax(self):
        self.conn.is_mariadb.return_value = True
        job = LiveJob(payload())
        scripts = []
        def run(conn, script, marker):
            scripts.append(script)
            return captured(marker, MARIA), ''
        with patch.object(job, '_run_script', side_effect=run):
            job.run()
        self.assertEqual(job.state, 'complete', job.error)
        self.assertEqual(job.result['engine'], 'mariadb')
        self.assertIn(' ANALYZE FORMAT=JSON SELECT', scripts[0])
        self.assertNotIn('EXPLAIN ANALYZE', scripts[0])
        self.assertNotIn('explain_json_format_version', scripts[0])

    def test_invalid_plan_fails_and_error_redacts_password(self):
        job = LiveJob(payload())
        with patch.object(job, '_run_script', side_effect=lambda c, s, m: (captured(m, '{}'), 'denied private-secret')):
            job.run()
        self.assertEqual(job.state, 'failed')
        self.assertNotIn('private-secret', job.error)
        self.assertIn('[redacted]', job.error)
        self.assertIsNone(job.result)

    def test_cancel_before_running_never_launches_query(self):
        job = LiveJob(payload())
        job.cancel()
        with patch.object(job, '_run_script') as run:
            job.run()
        run.assert_not_called()
        self.assertEqual(job.state, 'cancelled')
        self.assertNotIn('password', job.options)

    def test_cancel_targets_only_owned_connection_and_process(self):
        job = LiveJob(payload())
        job.connection_id = 42
        job.process = MagicMock()
        job.process.poll.return_value = None
        job.cancel()
        self.conn.run.assert_called_once_with('KILL QUERY 42', timeout=5)
        job.process.terminate.assert_called_once_with()
        self.assertTrue(job.cancelled.is_set())

    def test_raw_stdin_capture_uses_binary_mode_and_timer_cleanup(self):
        job = LiveJob(payload())
        self.conn._build_argv.return_value = ['mysql', '--raw', '--binary-mode', '-e', '']
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdout = io.BytesIO(captured('marker').encode())
        proc.stderr = io.BytesIO()
        proc.poll.return_value = 0
        proc.returncode = 0
        proc.wait.return_value = 0
        with patch('myflames.live.subprocess.Popen', return_value=proc) as popen, \
                patch('myflames.live.threading.Timer') as timer:
            output, diagnostics = job._run_script(self.conn, 'SELECT 1;', 'marker')
        self.assertIn(PLAN.strip(), output)
        self.assertEqual(diagnostics, '')
        self.assertNotIn('-e', popen.call_args[0][0])
        self.assertIn('--binary-mode', popen.call_args[0][0])
        flags = self.conn._build_argv.call_args[0][1]
        self.assertIn('--comments', flags)
        # Without --force the client stops at a failed SET SESSION TRANSACTION
        # READ ONLY / START TRANSACTION READ ONLY instead of running the query.
        self.assertNotIn('--force', flags)
        self.assertNotIn('-f', flags)
        self.assertNotIn('private-secret', str(popen.call_args))
        proc.stdin.write.assert_called_once_with(b'SELECT 1;')
        timer.assert_called_once_with(job.timeout, job.cancel)
        timer.return_value.cancel.assert_called_once_with()
        self.assertIsNone(job.process)
        self.assertIsNone(job.connection_id)

    def test_capture_isolates_client_environment_until_process_exits(self):
        import os
        job = LiveJob(payload())
        self.conn._build_argv.return_value = ['mysql', '-e', '']
        proc = MagicMock()
        proc.stdout = io.BytesIO(captured('marker').encode())
        proc.stderr = io.BytesIO()
        proc.poll.return_value = proc.returncode = 0
        homes = []
        def launch(argv, **kwargs):
            env = kwargs['env']
            homes.append(env['HOME'])
            self.assertTrue(os.path.isdir(env['HOME']))
            self.assertEqual(env['USERPROFILE'], env['HOME'])
            self.assertFalse(os.path.exists(env['MYSQL_TEST_LOGIN_FILE']))
            for key in ('MYSQL_PWD', 'MYSQL_UNIX_PORT', 'LIBMYSQL_ENABLE_CLEARTEXT_PLUGIN',
                        'MYFLAMES_VAULT_PASSPHRASE'):
                self.assertNotIn(key, env)
            return proc
        with patch.dict(os.environ, {'MYSQL_PWD': 'ambient', 'MYSQL_UNIX_PORT': '/other.sock',
                                     'LIBMYSQL_ENABLE_CLEARTEXT_PLUGIN': '1',
                                     'MYFLAMES_VAULT_PASSPHRASE': 'vault-secret'}), \
                patch('myflames.live.subprocess.Popen', side_effect=launch), \
                patch('myflames.live.threading.Timer'):
            job._run_script(self.conn, 'SELECT 1;', 'marker')
        self.assertTrue(homes)
        self.assertTrue(all(not os.path.exists(home) for home in homes))

    def _interrupted_client(self, events, marker='marker'):
        """A client that printed its connection ID and is still running when
        Ctrl+C arrives (stderr.read raises KeyboardInterrupt)."""
        self.conn._build_argv.return_value = ['mysql', '-e', '']
        self.conn.run.side_effect = lambda sql, timeout=None: events.append(('server', sql))
        proc = MagicMock()
        proc.stdout = io.BytesIO((marker + ':connection\n42\n').encode())
        proc.stderr.read.side_effect = KeyboardInterrupt
        proc.poll.return_value = None
        proc.terminate.side_effect = lambda: events.append('terminate')
        proc.kill.side_effect = lambda: events.append('kill client')
        return proc

    class _InlineThread:
        """Runs the stdout reader to completion on start(), so the
        connection ID is known before the interrupt, without sleeping."""
        def __init__(self, target, daemon=None):
            self.target = target
        def start(self):
            self.target()
        def join(self, timeout=None):
            pass

    def test_ctrl_c_sends_kill_query_before_the_client_is_ended(self):
        job = LiveJob(payload())
        events = []
        proc = self._interrupted_client(events)
        with patch('myflames.live.subprocess.Popen', return_value=proc), \
                patch('myflames.live.threading.Thread', self._InlineThread), \
                patch('myflames.live.threading.Timer'):
            with self.assertRaises(KeyboardInterrupt):
                job._run_script(self.conn, 'SELECT 1;', 'marker')
        self.assertEqual(events[0], ('server', 'KILL QUERY 42'))
        self.assertIn('terminate', events)
        self.assertGreater(events.index('terminate'), 0)
        # The control connection still had the password when KILL was sent.
        self.assertEqual(self.connection_type.call_args[1].get('password'), 'private-secret')
        self.assertTrue(job.cancelled.is_set())
        self.assertIsNone(job.process)
        self.assertIsNone(job.connection_id)

    def test_capture_cli_ctrl_c_reaches_the_server(self):
        from myflames import workspace_cli
        events = []
        proc = self._interrupted_client(events, '__myflames_fixed')
        stderr = io.StringIO()
        with patch('myflames.live.uuid.uuid4', return_value=MagicMock(hex='fixed')), \
                patch('myflames.live.subprocess.Popen', return_value=proc), \
                patch('myflames.live.threading.Thread', self._InlineThread), \
                patch('myflames.live.threading.Timer'), patch('sys.stderr', stderr):
            with self.assertRaises(SystemExit) as caught:
                workspace_cli.cmd_capture(['-h', 'db.internal', '-u', 'app', '-pprivate-secret',
                                           '-e', 'SELECT 1', '--mode', 'analyze'])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn('Capture cancelled.', stderr.getvalue())
        self.assertEqual([e for e in events if isinstance(e, tuple)], [('server', 'KILL QUERY 42')])
        self.assertNotIn('private-secret', stderr.getvalue())

    def test_timeout_cancellation_prevents_partial_success(self):
        job = LiveJob(payload())
        self.conn._build_argv.return_value = ['mysql', '-e', '']
        proc = MagicMock()
        proc.stdout = io.BytesIO(captured('marker').encode())
        proc.stderr = io.BytesIO()
        proc.poll.return_value = 0
        timer = MagicMock()
        timer.start.side_effect = job.cancelled.set
        with patch('myflames.live.subprocess.Popen', return_value=proc), \
                patch('myflames.live.threading.Timer', return_value=timer):
            with self.assertRaisesRegex(ConnectorError, 'cancelled|time limit'):
                job._run_script(self.conn, 'SELECT 1;', 'marker')
        timer.cancel.assert_called_once_with()
        self.assertIsNone(job.process)

    def test_client_failure_without_output_surfaces_diagnostic(self):
        job = LiveJob(payload())
        self.conn._build_argv.return_value = ['mysql', '-e', '']
        proc = MagicMock()
        proc.stdout = io.BytesIO()
        proc.stderr = io.BytesIO(b'Unknown option: unsupported')
        proc.poll.return_value = 1
        proc.returncode = 1
        with patch('myflames.live.subprocess.Popen', return_value=proc), \
                patch('myflames.live.threading.Timer'):
            with self.assertRaisesRegex(ConnectorError, 'Unknown option'):
                job._run_script(self.conn, 'SELECT 1;', 'marker')
        self.assertIsNone(job.process)


class TestCaptureManager(unittest.TestCase):
    def test_repeat_statistics_do_not_treat_missing_as_zero(self):
        result = measurement_summary([{'total_time_ms': n} for n in (1, None, 9, 5)])
        self.assertEqual(result, {'count': 3, 'median_ms': 5, 'min_ms': 1, 'max_ms': 9, 'stdev_ms': 4})
        self.assertEqual(measurement_summary([])['median_ms'], None)
        self.assertEqual(measurement_summary([{'total_time_ms': 0}])['median_ms'], 0)

    def test_capacity_lookup_and_close(self):
        manager = LiveJobs()
        with patch('myflames.live.threading.Thread'):
            first = manager.start(payload())
            manager.start(payload())
            with self.assertRaisesRegex(ValueError, 'Two captures'):
                manager.start(payload())
        identity = dbops.identity_endpoint(dbops.connection_identity(payload()['connection']))
        self.assertEqual(manager.get(first['id'], identity).id, first['id'])
        with self.assertRaisesRegex(ValueError, 'not found'):
            manager.get('missing', identity)
        manager.close()
        self.assertTrue(all(job.cancelled.is_set() for job in manager.jobs.values()))

    def test_job_lookup_requires_the_starting_endpoint(self):
        manager = LiveJobs()
        request = payload(connection={'host': 'localhost', 'port': 3306, 'user': 'app', 'profile_id': 'saved',
                                      'ssl_mode': 'REQUIRED', 'ssl_ca': 'ca.pem', 'password': 'private-secret'})
        identity = dbops.identity_endpoint(dbops.connection_identity(request['connection']))
        with patch('myflames.live.threading.Thread'):
            started = manager.start(request)
        job = manager.jobs[started['id']]
        self.assertEqual(job.identity, identity)
        self.assertNotIn('private-secret', job.identity)
        with self.assertRaisesRegex(ValueError, 'not found') as unknown:
            manager.get('missing', identity)
        for other in (None, dbops.identity_endpoint(dbops.connection_identity({'host': 'other'}))):
            with self.subTest(identity=other), self.assertRaises(ValueError) as caught:
                manager.get(job.id, other)
            self.assertEqual(str(caught.exception), str(unknown.exception))
        with self.assertRaisesRegex(ValueError, 'not found'):
            manager.get(job.id)
        # The binding survives caller edits and the worker clearing its password.
        request['connection']['host'] = 'edited'
        job.options.pop('password', None)
        self.assertIs(manager.get(job.id, identity), job)
        self.assertNotIn('identity', job.snapshot())


class TestCaptureNormalization(unittest.TestCase):
    def normalize(self, value):
        from myflames.live import normalize_capture
        return normalize_capture(value)

    def test_incomplete_capture_has_renderable_defaults(self):
        value = {'mode': 'analyze'}
        result = self.normalize(value)
        self.assertEqual(value, {'mode': 'analyze'})
        self.assertEqual(result['mode'], 'analyze')
        self.assertEqual(result['runs'], [])
        self.assertEqual(result['warnings'], [])
        self.assertEqual(result['measurements'], {'count': 0, 'median_ms': None,
                         'min_ms': None, 'max_ms': None, 'stdev_ms': None})
        for key in ('schema', 'table_stats', 'variables'):
            self.assertEqual(result[key], {})
        self.assertIsNone(result['trace'])
        for key in ('raw', 'query', 'engine', 'server_version', 'captured_at', 'conditions'):
            self.assertIsInstance(result[key], str)

    def test_measurements_recomputed_and_credentials_filtered_without_mutation(self):
        value = {'mode': 'analyze', 'password': 'private-secret',
                 'connection': {'password': 'private-secret'},
                 'measurements': {'count': 900, 'median_ms': float('nan')},
                 'runs': [{'total_time_ms': time, 'plan': PLAN,
                           'password': 'private-secret', 'connection': {'password': 'private-secret'}}
                          for time in (1, 5, 9)]}
        original = copy.deepcopy(value)
        result = self.normalize(value)
        self.assertEqual(len(value['runs']), len(original['runs']))
        self.assertEqual(value['runs'][0], original['runs'][0])
        self.assertNotIn('private-secret', json.dumps(result))
        self.assertEqual(result['measurements'], {'count': 3, 'median_ms': 5,
                         'min_ms': 1, 'max_ms': 9, 'stdev_ms': 4})
        self.assertIsNone(result['runs'][0]['statistics'])
        self.assertEqual(result['runs'][0]['session_status_delta'], {})

    def test_wrong_capture_field_types_rejected(self):
        for value in ([], 'capture', {'runs': {}}, {'runs': [None]}, {'warnings': 'warning'},
                      {'warnings': [{}]}, {'schema': []}, {'table_stats': 'text'}, {'variables': []},
                      {'query': []}, {'mode': 'execute'}, {'trace': 'trace'}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.normalize(value)

    def test_invalid_run_measurements_and_plan_shapes_rejected(self):
        for time in ('4', True, -1, float('nan'), float('inf'), 10 ** 400):
            with self.subTest(time=time), self.assertRaises(ValueError):
                self.normalize({'mode': 'analyze', 'runs': [{'total_time_ms': time, 'plan': PLAN}]})
        for raw in ('{}', '[]', 'not json', '{"operation":"Scan","actual_rows":NaN}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.normalize({'raw': raw})
            with self.subTest(run_plan=raw), self.assertRaises(ValueError):
                self.normalize({'runs': [{'plan': raw}]})

    def test_zero_and_missing_measurements_remain_distinct(self):
        result = self.normalize({'mode': 'analyze', 'runs': [{'total_time_ms': 0}, {'total_time_ms': None}]})
        self.assertEqual(result['measurements']['count'], 1)
        self.assertEqual(result['measurements']['median_ms'], 0)
        self.assertIsNone(result['runs'][1]['total_time_ms'])

    def test_run_limits_counters_and_trace_availability_validated(self):
        bad_runs = ([{}] * 11, [{'statistics': []}], [{'session_status_delta': []}],
                    [{'statistics': {'rows_examined': 'many'}}],
                    [{'session_status_delta': {'Sort_rows': float('nan')}}])
        for runs in bad_runs:
            with self.subTest(runs=runs), self.assertRaises(ValueError):
                self.normalize({'runs': runs})
        for trace in ({}, {'data': {}, 'missing_bytes': -1},
                      {'data': {}, 'insufficient_privileges': 2}):
            with self.subTest(trace=trace), self.assertRaises(ValueError):
                self.normalize({'trace': trace})

    def test_explain_import_does_not_claim_measured_runtime(self):
        result = self.normalize({'mode': 'explain', 'runs': [{'total_time_ms': 12, 'plan': PLAN}]})
        self.assertIsNone(result['runs'][0]['total_time_ms'])
        self.assertEqual(result['measurements']['count'], 0)
