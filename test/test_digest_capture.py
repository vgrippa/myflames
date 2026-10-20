"""Capture straight from a Performance Schema digest.

Covers the placeholder conversion shared with the workspace (the cases in
apps/workspace/test/fixtures/digest-placeholders.json also run in
apps/workspace/test/placeholders.mjs), the SELECT-only check reused for
Top statements rows, lookup by digest, the optional capture ``source``, and
``myflames capture --digest``. Fake connections stand in for MySQL 8.4 and
MariaDB 11.4, so no server is needed.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

from myflames import discover, ui, workspace_cli  # noqa: E402
from myflames.live import (LiveJob, bind_query, capture_problem,  # noqa: E402
                           normalize_capture, normalize_source)
from test_discover import (CONSUMERS_ON, MAX_TEXT, MYSQL_V2_PLAN, PASSWORD,  # noqa: E402
                           FakeConn, FakeFactory, batch, digest_row, server)

SHARED = json.loads((Path(__file__).resolve().parent.parent / 'apps' / 'workspace' / 'test'
                     / 'fixtures' / 'digest-placeholders.json').read_text(encoding='utf-8'))
MYSQL_DIGEST = '028d37443170c0c809af33862240c71df0e4e4556a9d0ba65f1c1834b83cca23'
MARIA_DIGEST = 'd05d74f87385d5c8a7b6e4f3a2b1c0d9'


def named_row(text, digest, **kwargs):
    row = digest_row(text, **kwargs)
    row[1] = digest
    return row


def mysql_conn(rows, version='8.4.3'):
    return FakeConn(version, [server(version), CONSUMERS_ON, MAX_TEXT,
                              ('events_statements_summary_by_digest', batch(rows))])


def maria_conn(rows, version='11.4.3-MariaDB-ubu2404'):
    return FakeConn(version, [server(version), CONSUMERS_ON,
                              ('events_statements_summary_by_digest', batch(rows))])


class TestSharedPlaceholderCases(unittest.TestCase):
    def test_parameterize_matches_the_workspace_module(self):
        for case in SHARED['parameterize']:
            with self.subTest(case['name']):
                self.assertEqual(discover.parameterize(case['text']),
                                 (case['sql'], case['names'], case['lists']))

    def test_digest_query_matches_the_workspace_module(self):
        for case in SHARED['digest_query']:
            with self.subTest(case['name']):
                result = discover.digest_query(case['row'])
                self.assertEqual((result['sql'], result['names'], result['from_sample'], result['reason']),
                                 (case['sql'], case['names'], case['from_sample'], case['reason']))

    def test_join_function_calls_matches_the_workspace_module(self):
        for case in SHARED['join_function_calls']:
            with self.subTest(case['name']):
                self.assertEqual(discover.join_function_calls(case['text']), case['sql'])

    def test_converted_sql_binds_with_capture_parameters(self):
        sql, names, lists = discover.parameterize(
            'SELECT * FROM `orders` WHERE `customer_id` IN (...) AND `created_at` > ? LIMIT ?')
        self.assertEqual((names, lists), (['p1', 'p2', 'p3'], ['p1']))
        bound = bind_query(sql, {'p1': 5, 'p2': '2026-01-01', 'p3': 10})
        self.assertIn('IN (5)', bound)
        self.assertIn('LIMIT 10', bound)
        self.assertIn("CONVERT(X'%s' USING utf8mb4)" % '2026-01-01'.encode().hex(), bound)


class TestCaptureProblem(unittest.TestCase):
    def test_same_rule_and_messages_as_bind_query(self):
        for query in ('UPDATE t SET a = 1', 'SELECT 1; SELECT 2', 'SELECT * FROM t FOR UPDATE',
                      'SELECT a INTO @x FROM t', 'SHOW TABLES', 'SELECT @a := 1'):
            with self.subTest(query):
                problem = capture_problem(query)
                self.assertTrue(problem)
                with self.assertRaises(ValueError) as caught:
                    bind_query(query)
                self.assertEqual(str(caught.exception), problem)

    def test_placeholders_and_trailing_semicolon_pass(self):
        for query in ('SELECT * FROM `t` WHERE `a` = ?', 'SELECT * FROM t WHERE a = :p1;',
                      'WITH c AS (SELECT 1) SELECT * FROM c', 'select * from t where a in (...)'):
            with self.subTest(query):
                self.assertIsNone(capture_problem(query))
        self.assertEqual(capture_problem(''), 'No statement text was recorded.')
        self.assertEqual(capture_problem(None), 'No statement text was recorded.')

    def test_top_rows_carry_the_reason(self):
        conn = mysql_conn([
            digest_row('SELECT * FROM `orders` WHERE `status` = ?', sample="SELECT * FROM orders WHERE status = 'new'"),
            digest_row('UPDATE `orders` SET `status` = ?', sample="UPDATE orders SET status = 'x'"),
            digest_row('SELECT * FROM `t` FOR UPDATE', sample='SELECT * FROM t FOR UPDATE'),
        ])
        rows = discover.top_statements(conn)
        self.assertIsNone(rows[0]['capture_problem'])
        self.assertIsNone(rows[1]['capture_problem'])
        self.assertEqual(rows[1]['analysis_sql'], 'SELECT * FROM orders')
        self.assertIn('original statement can change data', rows[1]['analysis_warning'])
        self.assertEqual(rows[1]['sample_text'], "UPDATE orders SET status = 'x'")
        self.assertIn('locking clauses', rows[2]['capture_problem'])
        maria = maria_conn([digest_row('SELECT * FROM `t` WHERE `id` = ? ', with_sample=False)])
        self.assertIsNone(discover.top_statements(maria)[0]['capture_problem'])


class TestDigestTextAggregates(unittest.TestCase):
    """Digest text prints keyword tokens with a trailing space (MariaDB and
    MySQL gen_lex_token.cc), so built-in aggregates read ``SUM (``. A stored
    function is an identifier token and prints quoted."""
    MARIADB_TEXT = ('SELECT `p` . `name` , SUM ( `oi` . `quantity` ) `q` FROM `testdb` . `products` `p` '
                    'JOIN `testdb` . `order_items` `oi` ON `oi` . `product_id` = `p` . `id` '
                    'GROUP BY `p` . `id` ORDER BY `q` DESC LIMIT ?')

    def test_built_in_aggregates_in_digest_text_are_capturable(self):
        for text in (self.MARIADB_TEXT, 'SELECT COUNT ( * ) FROM `t`',
                     'SELECT `u` . `country` , COUNT ( DISTINCTROW `o` . `id` ) FROM `u` GROUP BY `u` . `country`'):
            with self.subTest(text=text):
                query = discover.digest_query({'digest_text': text})
                self.assertIsNone(discover.digest_capture_problem(query))
                self.assertIsNone(discover.top_statements(maria_conn([digest_row(text, with_sample=False)]))[0]['capture_problem'])

    def test_raw_sql_and_quoted_functions_stay_refused(self):
        self.assertIn('not a verified native reader', capture_problem('SELECT SUM (x) FROM t'))
        query = discover.digest_query({'digest_text': 'SELECT `sum` ( ? ) FROM `t`'})
        self.assertIn('not a verified native reader', discover.digest_capture_problem(query))
        query = discover.digest_query({'digest_text': 'SELECT `db` . SUM ( ? ) FROM `t`'})
        self.assertIn('not a verified native reader', discover.digest_capture_problem(query))


class TestFindStatement(unittest.TestCase):
    def test_exact_digest_and_query_shape(self):
        conn = mysql_conn([named_row('SELECT * FROM `orders` WHERE STATUS = ?', MYSQL_DIGEST,
                                     sample="SELECT * FROM orders WHERE status = 'new'")])
        found = discover.find_statement(conn, MYSQL_DIGEST.upper())
        self.assertEqual(found['statement']['digest'], MYSQL_DIGEST)
        self.assertEqual(found['statement']['sample_text'], "SELECT * FROM orders WHERE status = 'new'")
        self.assertIsNone(found['statement']['capture_problem'])
        self.assertEqual((found['engine'], found['server_version']), ('mysql', '8.4.3'))
        sql = conn.sql('events_statements_summary_by_digest')[0]
        self.assertIn("DIGEST LIKE '%s%%'" % MYSQL_DIGEST, sql)
        self.assertIn('ORDER BY SUM_TIMER_WAIT DESC', sql)
        # A lookup reads every row of that digest, including tagged samples.
        self.assertNotIn("LOCATE('/*myflames:'", sql)

    def test_prefix_and_schema_preference(self):
        rows = [named_row('SELECT ?', MYSQL_DIGEST, schema='busy', sample='SELECT 1'),
                named_row('SELECT ?', MYSQL_DIGEST, schema='shop', sample='SELECT 2')]
        self.assertEqual(discover.find_statement(mysql_conn(rows), MYSQL_DIGEST[:12])['statement']['schema'], 'busy')
        found = discover.find_statement(mysql_conn(rows), MYSQL_DIGEST[:12], schema='shop')
        self.assertEqual(found['statement']['sample_text'], 'SELECT 2')
        self.assertEqual(found['schemas'], ['busy', 'shop'])

    def test_not_found_ambiguous_and_invalid(self):
        with self.assertRaisesRegex(ValueError, 'No statement digest starting with 028d37443170'):
            discover.find_statement(mysql_conn([]), MYSQL_DIGEST[:12])
        other = 'f' * 64
        rows = [named_row('SELECT ?', MYSQL_DIGEST[:20] + 'a' * 44, sample='SELECT 1'),
                named_row('SELECT ?', MYSQL_DIGEST[:20] + 'b' * 44, sample='SELECT 2')]
        with self.assertRaisesRegex(ValueError, 'matches 2 statements'):
            discover.find_statement(mysql_conn(rows), MYSQL_DIGEST[:12])
        for bad in ('abc', 'xyz12345678', "0123456789'", other + 'f', None):
            conn = FakeConn('8.4.3')
            with self.assertRaisesRegex(ValueError, '8 to 64 hexadecimal'):
                discover.find_statement(conn, bad)
            self.assertEqual(conn.calls, [])

    def test_mariadb_without_performance_schema(self):
        conn = FakeConn('11.4.3-MariaDB', [server('11.4.3-MariaDB', ps_on=False)])
        with self.assertRaisesRegex(ValueError, 'MariaDB disables it by default'):
            discover.find_statement(conn, MARIA_DIGEST)


class TestCaptureSource(unittest.TestCase):
    def capture(self, **extra):
        value = {'raw': MYSQL_V2_PLAN, 'query': 'SELECT 1', 'mode': 'explain'}
        value.update(extra)
        return value

    def test_valid_sources_are_normalized_and_kept(self):
        for digest, schema in ((MYSQL_DIGEST.upper(), 'shop'), (MARIA_DIGEST, None), (MARIA_DIGEST, '')):
            source = normalize_capture(self.capture(source={'kind': 'digest', 'digest': digest,
                                                            'schema': schema}))['source']
            self.assertEqual(source, {'kind': 'digest', 'digest': digest.lower(), 'schema': schema or None})
        self.assertNotIn('source', normalize_capture(self.capture()))
        self.assertNotIn('source', normalize_capture(self.capture(source=None)))

    def test_text_labels_pass_through(self):
        # import-shell labels its captures with a plain string.
        self.assertEqual(normalize_capture(self.capture(source='mysqlsh-diagnostics'))['source'],
                         'mysqlsh-diagnostics')

    def test_invalid_sources_are_rejected(self):
        for source in ('', 'x' * 101, 'bad\nlabel', 5, [], {'kind': 'running', 'digest': MYSQL_DIGEST},
                       {'kind': 'digest', 'digest': 'abc'}, {'kind': 'digest', 'digest': MYSQL_DIGEST[:40]},
                       {'kind': 'digest', 'digest': MYSQL_DIGEST, 'schema': 'x' * 65},
                       {'kind': 'digest', 'digest': MYSQL_DIGEST, 'schema': 5}):
            with self.subTest(source=source):
                with self.assertRaisesRegex(ValueError, 'Capture source'):
                    normalize_capture(self.capture(source=source))

    def test_analyze_endpoint_and_investigations_keep_the_source(self):
        source = {'kind': 'digest', 'digest': MYSQL_DIGEST, 'schema': 'shop'}
        result = ui.api_response('/api/analyze', {'plan': MYSQL_V2_PLAN, 'capture': self.capture(source=source)})
        self.assertEqual(result['capture']['source'], source)
        from myflames.workspace_store import WorkspaceStore
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                'HOME': tmp, 'USERPROFILE': tmp, 'MYFLAMES_SECRET_BACKEND': 'vault'}):
            os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
            store = WorkspaceStore(os.path.join(tmp, 'workspace.sqlite3'))
            saved = store.save('investigation', {
                'schema_version': 'investigation-1.0', 'name': 'digest', 'notes': '', 'tags': [],
                'baseline_id': 'a', 'plans': [{'id': 'a', 'name': 'Top #1 · orders', 'raw': MYSQL_V2_PLAN,
                                               'capture': self.capture(source=source)}]})
            reopened = [item for item in store.list('investigation') if item['id'] == saved['id']][0]
            self.assertEqual(reopened['plans'][0]['capture']['source'], source)

    def test_live_job_validates_and_records_the_source(self):
        connection = {'host': '127.0.0.1', 'password': PASSWORD}
        with self.assertRaisesRegex(ValueError, 'Capture source'):
            LiveJob({'connection': connection, 'query': 'SELECT 1', 'source': {'kind': 'digest'}})
        job = LiveJob({'connection': connection, 'query': 'SELECT 1', 'mode': 'explain',
                       'source': {'kind': 'digest', 'digest': MYSQL_DIGEST, 'schema': 'shop'}})
        self.assertEqual(job.source['digest'], MYSQL_DIGEST)
        marker_plan = MYSQL_V2_PLAN

        def script(conn, sql, marker):
            sections = [('connection', '7'), ('before', ''), ('plan', marker_plan), ('after', ''),
                        ('statistics', ''), ('done', '')]
            return ''.join('%s:%s\n%s\n' % (marker, key, value) for key, value in sections), ''

        with mock.patch('myflames.live.MySQLConnection') as factory, \
                mock.patch('myflames.live.collect_schema', return_value={}), \
                mock.patch('myflames.live.collect_stats', return_value={}), \
                mock.patch('myflames.live.collect_stats_freshness', return_value={}), \
                mock.patch('myflames.live.collect_session_variables', return_value={}), \
                mock.patch.object(job, '_run_script', side_effect=script):
            conn = factory.return_value.__enter__.return_value
            conn.server_version.return_value = '8.4.3'
            conn.is_mariadb.return_value = False
            job.run()
        self.assertEqual(job.state, 'complete', job.error)
        self.assertEqual(job.result['source'], {'kind': 'digest', 'digest': MYSQL_DIGEST, 'schema': 'shop'})
        self.assertEqual(normalize_capture(job.result)['source'], job.result['source'])
        plain = LiveJob({'connection': connection, 'query': 'SELECT 1'})
        self.assertIsNone(plain.source)

    def test_normalize_source_directly(self):
        self.assertIsNone(normalize_source(None))
        with self.assertRaises(ValueError):
            normalize_source([])


class FakeJob(object):
    """Stands in for LiveJob in CLI tests: records payloads, echoes the source."""
    payloads = []

    def __init__(self, payload):
        FakeJob.payloads.append(payload)
        self.state, self.error = 'complete', ''
        self.result = {'schema_version': 'capture-1.0', 'raw': MYSQL_V2_PLAN,
                       'query': payload['query'], 'mode': payload['mode']}
        if payload.get('source'):
            self.result['source'] = payload['source']

    def run(self):
        pass

    def cancel(self):
        pass


class TestCaptureDigestCLI(unittest.TestCase):
    def setUp(self):
        FakeJob.payloads = []

    def call(self, conn, *args):
        factory = FakeFactory(conn)
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch('myflames.connector.MySQLConnection', factory), \
                mock.patch.object(workspace_cli, 'LiveJob', FakeJob), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                workspace_cli.cmd_capture(list(args))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue(), factory

    def sample_conn(self, **kwargs):
        return mysql_conn([named_row('SELECT * FROM `orders` WHERE STATUS = ? ORDER BY `total` DESC LIMIT ?',
                                     MYSQL_DIGEST, sample="SELECT * FROM orders WHERE status = 'new' LIMIT 10",
                                     **kwargs)])

    def test_sample_text_runs_in_the_digest_schema_and_records_the_source(self):
        code, out, err, factory = self.call(self.sample_conn(), '-u', 'app', '-p' + PASSWORD,
                                            '--digest', MYSQL_DIGEST[:12], '--mode', 'analyze')
        self.assertEqual(code, 0, err)
        payload = FakeJob.payloads[0]
        self.assertEqual(payload['query'], "SELECT * FROM orders WHERE status = 'new' LIMIT 10")
        self.assertEqual(payload['connection']['database'], 'shop')
        self.assertEqual(payload['mode'], 'analyze')
        self.assertEqual(payload['source'], {'kind': 'digest', 'digest': MYSQL_DIGEST, 'schema': 'shop'})
        data = json.loads(out)
        self.assertEqual(data['source']['digest'], MYSQL_DIGEST)
        self.assertNotIn(PASSWORD, out + err)
        # The lookup itself used the same connection settings.
        self.assertEqual(factory.options['password'], PASSWORD)

    def test_connection_schema_wins_over_the_digest_schema(self):
        code, _, err, _ = self.call(self.sample_conn(), '-D', 'reporting', '--digest', MYSQL_DIGEST)
        self.assertEqual(code, 0, err)
        self.assertEqual(FakeJob.payloads[0]['connection']['database'], 'reporting')
        self.assertEqual(FakeJob.payloads[0]['source']['schema'], 'shop')

    def test_normalized_text_exits_2_with_parameterized_sql(self):
        conn = maria_conn([named_row('SELECT * FROM `orders` WHERE `customer_id` IN (...) AND `created_at` > ? ',
                                     MARIA_DIGEST, with_sample=False)])
        code, out, err, _ = self.call(conn, '-p' + PASSWORD, '--digest', MARIA_DIGEST)
        self.assertEqual((code, out), (2, ''))
        self.assertEqual(FakeJob.payloads, [])
        self.assertIn('Digest d05d74f87385 (schema shop) has no runnable sample statement', err)
        self.assertIn('only normalized text', err)
        self.assertIn("-D shop -e 'SELECT * FROM `orders` WHERE `customer_id` IN (:p1) AND `created_at` > :p2'", err)
        self.assertIn('--parameters \'{"p1": null, "p2": null}\'', err)
        self.assertIn('p1 replaces a collapsed list', err)
        self.assertNotIn(PASSWORD, err)

    def test_truncated_sample_exits_2(self):
        long_sample = 'SELECT * FROM orders WHERE id IN (' + ', '.join(str(i) for i in range(400))
        conn = mysql_conn([named_row('SELECT * FROM `orders` WHERE `id` IN (...)', MYSQL_DIGEST,
                                     sample=long_sample[:1024])])
        code, out, err, _ = self.call(conn, '--digest', MYSQL_DIGEST)
        self.assertEqual((code, out), (2, ''))
        self.assertIn('cut off at performance_schema_max_sql_text_length', err)
        self.assertIn('IN (:p1)', err)

    def test_non_select_digest_exits_2(self):
        conn = mysql_conn([named_row('UPDATE `orders` SET STATUS = ?', MYSQL_DIGEST,
                                     sample="UPDATE orders SET status = 'x'")])
        code, out, err, _ = self.call(conn, '--digest', MYSQL_DIGEST)
        self.assertEqual((code, out), (2, ''))
        self.assertIn('cannot be captured: Capture accepts one SELECT', err)

    def test_usage_errors_exit_2_without_connecting(self):
        invalid_args = [[]]
        for flag in ('--statement-digest', '--digest'):
            invalid_args.extend(([flag, MYSQL_DIGEST, '-e', 'SELECT 1'], [flag, 'zz'],
                                 [flag, '1234567']))
        for argv in invalid_args:
            with self.subTest(argv=argv):
                code, out, err, factory = self.call(self.sample_conn(), *argv)
                self.assertEqual((code, out), (2, ''))
                self.assertIsNone(factory.options)
        _, _, err, _ = self.call(self.sample_conn())
        self.assertIn('one of the arguments -e/--execute --statement-digest/--digest is required', err)

    def test_lookup_failures_exit_2(self):
        code, out, err, _ = self.call(mysql_conn([]), '--digest', MYSQL_DIGEST)
        self.assertEqual((code, out), (2, ''))
        self.assertIn('No statement digest starting with', err)
        code, _, err, _ = self.call(FakeConn('11.4.3-MariaDB', [server('11.4.3-MariaDB', ps_on=False)]),
                                    '--digest', MARIA_DIGEST)
        self.assertEqual(code, 2)
        self.assertIn('performance_schema=ON', err)

    def test_help_documents_digest(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            workspace_cli.cmd_capture(['--help'])
        text = ' '.join(out.getvalue().split())
        self.assertIn('--digest HASH', text)
        self.assertIn('source.kind "digest"', text)

    def test_top_table_shows_the_short_digest(self):
        conn = mysql_conn([named_row('SELECT 1', MYSQL_DIGEST, sample='SELECT 1')])
        factory = FakeFactory(conn)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch('myflames.connector.MySQLConnection', factory), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            workspace_cli.cmd_top(['-q'])
        header, row = out.getvalue().splitlines()[:2]
        self.assertIn('DIGEST', header)
        self.assertIn(MYSQL_DIGEST[:12], row)
        self.assertNotIn(MYSQL_DIGEST[:13], row)


if __name__ == '__main__':
    unittest.main()
