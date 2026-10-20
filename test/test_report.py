"""Full analysis report (``myflames report``, ``/api/live/report``) and the
server checks it adds (server_checks.py).

Replays live recordings (fixtures/perf/report-*.json, the _perf_replay shape)
of a full analysis taken from throwaway Docker containers: a MySQL 8.4 replica
(GTID, read_only off), MySQL 8.0.46 (as root, and as an account with SELECT
on one schema only), MariaDB 11.4 with Performance Schema and the query cache
on, and MariaDB 11.4 with its defaults (Performance Schema off). Each server
held a schema with a table without a key, a table clustered on a unique key,
a MyISAM table, redundant and unused indexes, full scans and a join without an
index, accounts without a password, an anonymous account, a locked account and
a role, a long-running statement and a session idle in a transaction.
"""
import contextlib
import datetime
import inspect
import io
import json
import os
import re
import tempfile
import threading
import time
import unittest
from unittest import mock

from _perf_replay import (PASSWORD, FakeFactory, FakeTime, ReplayConn, _Refused, batch, replay as raw_replay, run_cli,
                          run_main)
from myflames import discover, report, server_checks, ui
from myflames.connector import ConnectorError, MySQLConnection
from myflames.perf_common import Server, _table_count_sql, scope_sql
from test_connector import option_file_client

RECORDINGS = ('mysql-8.4-replica', 'mysql-8.0', 'mariadb-11.4', 'mariadb-11.4-default', 'mysql-8.0-limited')
FINDING_FIELDS = {'id', 'family', 'status', 'title', 'headline', 'verdict', 'why', 'rule', 'caveat', 'action',
                  'numbers', 'settings', 'links', 'source', 'learn', 'impact', 'attribution'}
WRITE_WORDS = re.compile(r'\b(UPDATE|INSERT|DELETE|REPLACE|KILL|GRANT|FLUSH|ALTER|CREATE|DROP|TRUNCATE)\b', re.I)
READ_FORMS = re.compile(r"^(SELECT |SHOW (GLOBAL|ENGINE INNODB STATUS|REPLICA STATUS|SLAVE STATUS|ALL SLAVES STATUS)"
                        r"|SET SESSION time_zone = 'SYSTEM'; )")
HOSTILE = 'ev<img src=x onerror=alert(1)>|`*_[x](y)\x1b[31m'


ORIGINAL_ANALYSIS = report.analysis_report
ORIGINAL_VERSION_SUPPORT = server_checks._version_support


def replay(view, name, **kwargs):
    """Extend old report recordings with deterministic bounded-read answers.

    These are synthesized from the recorded rows, not new live captures.
    Keep the original recordings intact and the SQL allowlist exact: any
    other new statement still fails through ReplayConn.
    """
    conn, data = raw_replay(view, name, **kwargs)
    for sql, answers in list(conn.answers.items()):
        if sql.startswith('/*myflames:tables*/ SELECT TABLE_SCHEMA, TABLE_NAME, ENGINE,'):
            where = sql.split(' WHERE ', 1)[1]
            if name.startswith('mariadb'):
                where = where.split(' AND ', 1)[1]
            count = batch([[len(answers[0].splitlines())]])
            conn.answers.setdefault('/*myflames:report*/ SELECT COUNT(*) FROM information_schema.TABLES WHERE %s'
                                    % where, [count])
        if sql.startswith('/*myflames:io*/ SELECT ') and any(source in sql for source in (
                ' FROM performance_schema.table_io_waits_summary_by_table ',
                ' FROM performance_schema.file_summary_by_instance ',
                ' FROM performance_schema.file_summary_by_event_name ',
                ' FROM information_schema.TABLE_STATISTICS ')):
            # A denied count now falls back before the instance SELECT.
            if not isinstance(answers[0], _Refused):
                conn.answers[sql + ' LIMIT 2001'] = list(answers)
            del conn.answers[sql]
            if 'file_summary_by_event_name' not in sql:
                count_sql = '/*myflames:io*/ SELECT COUNT(*) FROM ' + sql.split(' FROM ', 1)[1]
                answer = answers[0]
                conn.answers.setdefault(count_sql, [answer if isinstance(answer, _Refused)
                                                    else batch([[len(answer.splitlines())]])])
    return conn, data


def run_report(name, edit=None, workers=1, **kwargs):
    clock = FakeTime()
    conn, data = replay('report', name, clock=clock, edit=edit)
    conn.password = PASSWORD
    with mock.patch.object(server_checks, '_version_support', side_effect=lambda server, as_of=None:
                           ORIGINAL_VERSION_SUPPORT(server, datetime.date(2026, 10, 15))):
        result = ORIGINAL_ANALYSIS(conn, data['args']['interval'], workers=workers, sleep=clock.sleep,
                                   clock=clock.monotonic, **kwargs)
    return result, conn, data


def finding(result, fid):
    return next(f for f in result['findings'] if f['id'] == fid)


class ReportShape(unittest.TestCase):
    maxDiff = None

    def assert_shape(self, result):
        json.dumps(result, allow_nan=False)
        self.assertEqual(result['schema_version'], 'report-1.0')
        for key in ('engine', 'server_version', 'taken_at', 'uptime_s', 'performance_schema', 'header', 'summary',
                    'look_first', 'findings', 'sections', 'attribution', 'checklist', 'notes'):
            self.assertIn(key, result)
        ids = [f['id'] for f in result['findings']]
        self.assertEqual(len(ids), len(set(ids)))
        for f in result['findings']:
            fields = FINDING_FIELDS | ({'state'} if (f.get('source') or {}).get('kind') == 'diagnostic' else set())
            self.assertEqual(set(f), fields, f['id'])
            self.assertIn(f['status'], report.STATUS_RANK)
            self.assertIn(f['family'], report.SECTION_TITLES)
            self.assertTrue(f['title'] and (f['verdict'] or f['headline']), f['id'])
            if f['rule']:
                self.assertFalse(f['verdict'].endswith('Rule of thumb: %s.' % f['rule'].rstrip('.')), f['id'])
            if f['status'] in ('watch', 'problem') and f['action']:
                self.assertTrue(f['action'].get('text') or f['action'].get('sql') or f['action'].get('config'),
                                f['id'])
            for n in f['numbers']:
                self.assertIn('label', n)
                self.assertIn('text', n)
            for link in f['links']:
                self.assertIn('view', link)
                self.assertNotEqual(link['view'], 'checks')
        self.assertEqual([s['key'] for s in result['sections']], [k for k, _, _ in report.SECTIONS])
        self.assertEqual(sorted(i for s in result['sections'] for i in s['findings']), sorted(ids))
        counts = {s: sum(1 for f in result['findings'] if f['status'] == s) for s in report.STATUSES}
        self.assertEqual({s: result['summary'][s] for s in report.STATUSES}, counts)
        # Ranked: status first, then family.
        ranks = [(report.STATUS_RANK[f['status']], report.FAMILY_RANK[f['family']]) for f in result['findings']]
        self.assertEqual(ranks, sorted(ranks))
        flagged = [f['id'] for f in result['findings'] if f['status'] in ('problem', 'watch')]
        self.assertEqual(result['look_first'], flagged[0] if flagged else None)
        self.assertEqual([v['key'] for v in result['header']['views']],
                         ['health', 'waits', 'io', 'config', 'innodb', 'sessions', 'instruments', 'checks',
                          'tables', 'indexes'])
        covered = {c['key'] for c in result['checklist']['covered']}
        self.assertTrue({'replication', 'security', 'query_cache', 'schema'} <= covered)
        self.assertTrue(any(n['label'].startswith('OS memory') for n in result['checklist']['not_checked']))

    def assert_read_only(self, conn):
        self.assertTrue(conn.calls)
        for sql, flags in conn.calls:
            match = re.match(r'^/\*myflames:(\w+)\*/ ', sql)
            self.assertTrue(match, sql)
            self.assertIn('--comments', flags, sql)
            body = sql[match.end():]
            self.assertRegex(body, READ_FORMS, sql)
            if match.group(1) != 'discover':
                self.assertNotRegex(body, WRITE_WORDS, sql)
            self.assertNotIn('authentication_string,', body)
            self.assertNotIn('Password', body)


class TestServerViewRegressions(unittest.TestCase):
    def test_health_diagnostic_state_survives_full_analysis(self):
        """Both engines use the same state contract; absent legacy states stay null."""
        for engine in ('mysql', 'mariadb'):
            for key, state in (('buffer_pool', 'room'), ('thread_cache', 'not_applicable'),
                               ('buffer_pool', None)):
                with self.subTest(engine=engine, key=key, state=state):
                    diagnostic = {'key': key, 'status': 'info', 'title': key}
                    if state is not None:
                        diagnostic['state'] = state
                    result = report._health_findings({'engine': engine, 'diagnostics': [diagnostic]})
                    self.assertEqual(len(result), 1)
                    self.assertEqual(result[0]['state'], state)
                    self.assertEqual(json.loads(json.dumps(result))[0]['state'], state)
                    self.assertEqual(result[0]['status'], 'info')

    def test_io_hot_spots_report_untimed_read_and_written_rows(self):
        # userstat is MariaDB-only; zero timed P_S activity must retain its units.
        for timing in (None, 0):
            with self.subTest(timing=timing):
                row = {'schema': 'shop', 'table': 'orders', 'total_ms_per_s': timing,
                       'ops': None if timing is None else 0, 'read_rows': 1234, 'write_rows': 0}
                result = report._io_findings({'elapsed_s': 5, 'window': {'tables': [row]}})[0]
                number = result['numbers'][0]
                if timing is None:
                    self.assertEqual(number['text'], '1,234 rows read, 0 rows written (timing unavailable)')
                    self.assertNotIn('unit', number)
                    self.assertNotIn('value', number)
                else:
                    self.assertEqual(number['text'], '0 per second, 0 rows')
                    self.assertEqual((number['value'], number['unit']), (0, 'ms/s'))

    def test_full_analysis_counts_table_scope_once(self):
        for server in ('mysql-8.4-replica', 'mariadb-11.4-default'):
            with self.subTest(server=server):
                result, conn, _ = run_report(server)
                counts = [sql for sql, _ in conn.calls if 'SELECT COUNT(*) FROM information_schema.TABLES' in sql]
                self.assertEqual(len(counts), 1, counts)
                self.assertTrue(counts[0].startswith(report.REPORT_TAG))
                statuses = {v['key']: v['status'] for v in result['header']['views']}
                self.assertNotIn(statuses['tables'], ('skipped', 'error'))
                self.assertNotIn(statuses['indexes'], ('skipped', 'error'))

    def test_full_analysis_gates_an_oversized_chosen_schema(self):
        for version in ('8.4.11', '11.4.13-MariaDB'):
            with self.subTest(version=version):
                server = Server({'version': version}, {})
                conn = mock.Mock(password=None)
                conn.run.return_value = batch([[18]])
                seen = []
                def run_children(conn, jobs, *args):
                    seen.extend(key for key, _ in jobs)
                    return {}
                with mock.patch.object(report, 'preflight', return_value=server), \
                        mock.patch.object(report, '_run_all', side_effect=run_children), \
                        mock.patch.object(report, '_attribution'), \
                        mock.patch.object(report, 'compose_report') as compose:
                    report.analysis_report(conn, 5, schemas=('shop',), require_choice_above=17)
                self.assertNotIn('tables', seen)
                self.assertNotIn('indexes', seen)
                self.assertIn('18 tables, more than 17', compose.call_args[1]['schema_gate'])
                conn.run.assert_called_once()
                self.assertIn("TABLE_SCHEMA = CONVERT(X'73686f70' USING utf8mb4)", conn.run.call_args[0][0])


class TestAdvisoryFeatures(unittest.TestCase):
    def test_an_advisory_feature_never_makes_views_not_ready(self):
        # The replication receiver guard is optional: off by default on both
        # engines, so it must not turn instruments.views into watch.
        from myflames import ps_instruments
        self.assertTrue(any(f.get("advisory") for f in ps_instruments.FEATURES
                            if f["key"] == "replication_receiver"))
        rep = {"available": True, "status": {}, "features": [
            {"key": "top", "label": "Top statements", "status": "ready"},
            {"key": "replication_receiver", "label": "Receiver guard", "status": "off", "advisory": True,
             "missing": ["stage/sql/Checking source version is off"]}]}
        found = report._instruments_findings(rep)[0]
        self.assertEqual((found["status"], found["headline"]), ("ok", "1 of 1 ready"))


class TestLiveRecordings(ReportShape):
    def test_shared_attribution_keeps_every_full_recorded_report(self):
        """Compare complete envelopes, not only statuses or selected fields."""
        class FixedDatetime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 15, 12, tzinfo=tz)

        with mock.patch.object(datetime, 'datetime', FixedDatetime), \
                mock.patch.object(report.time, 'monotonic', return_value=1234.0):
            for name in RECORDINGS:
                with self.subTest(server=name):
                    with mock.patch.object(report, '_AttributionContext', side_effect=lambda conn: conn):
                        original, _, _ = run_report(name)
                    shared, _, _ = run_report(name)
                    self.assertEqual(shared, original)

    def test_attribution_reads_shared_metadata_once(self):
        """Each filter keeps its own digest read on both database engines."""
        for name, expected in (("mysql-8.4-replica", 9), ("mysql-8.0", 9),
                               ("mariadb-11.4", 8), ("mariadb-11.4-default", 0),
                               ("mysql-8.0-limited", 2)):
            with self.subTest(server=name):
                result, conn, _ = run_report(name, workers=3)
                calls = [sql for sql, _ in conn.calls if sql.startswith(discover.DISCOVER_TAG)]
                self.assertEqual(len(calls), expected)
                self.assertEqual(len(calls), len(set(calls)))
                if name.startswith("mysql-8") and expected == 9:
                    # Every shared statement is still discover's exact text.
                    self.assertTrue(discover.SHARED_METADATA_SQL.issubset(calls))
                if expected >= 8:
                    self.assertEqual(len(result["attribution"]["full_scans"]), 3)
                    self.assertEqual(sum("WHERE DIGEST IS NOT NULL" in sql for sql in calls), 4)

    def test_attribution_metadata_does_not_outlive_one_report(self):
        """A later report re-reads coverage, consumers and server metadata."""
        for name in ('mysql-8.4-replica', 'mariadb-11.4'):
            with self.subTest(server=name):
                clock = FakeTime()
                conn, data = replay('report', name, clock=clock)
                for _ in range(2):
                    report.analysis_report(conn, data['args']['interval'], workers=3,
                                           sleep=clock.sleep, clock=clock.monotonic)
                shared = [sql for sql in conn.seen if sql in discover.SHARED_METADATA_SQL]
                self.assertTrue(shared)
                self.assertEqual({conn.seen[sql] for sql in shared}, {2})

    def test_attribution_shared_coverage_errors_remain_best_effort(self):
        """A denied metadata query is shared without swallowing digest errors."""
        status = "SHOW GLOBAL STATUS WHERE Variable_name IN ('Uptime', 'Performance_schema_digest_lost')"

        def refuse(sql, text, n):
            if sql == discover.DISCOVER_TAG + status or (sql.startswith(discover.DISCOVER_TAG)
                                                        and 'WHERE DIGEST IS NULL' in sql):
                return _Refused('Access denied')
            return text

        for name in ('mysql-8.4-replica', 'mariadb-11.4'):
            with self.subTest(server=name):
                result, conn, _ = run_report(name, edit=refuse, workers=3)
                attribution = result['attribution']
                self.assertTrue(attribution['available'])
                self.assertIsNone(attribution['coverage']['uptime_s'])
                self.assertIsNone(attribution['coverage']['overflow'])
                self.assertTrue(attribution['full_joins']['statements'])
                self.assertEqual(conn.seen[discover.DISCOVER_TAG + status], 1)

    def test_every_recording_replays_to_the_recorded_findings(self):
        for name in RECORDINGS:
            with self.subTest(server=name):
                result, conn, data = run_report(name)
                self.assert_shape(result)
                self.assert_read_only(conn)
                self.assertEqual({f['id']: f['status'] for f in result['findings']}, data['summary']['findings'])
                self.assertEqual(result['summary'], data['summary']['status'])
                self.assertEqual(result['look_first'], data['summary']['look_first'])
                self.assertEqual({v['key']: v['status'] for v in result['header']['views']},
                                 data['summary']['views'])
                self.assertEqual([sql for sql in conn.answers if sql not in conn.seen], [])
                self.assertEqual(result['header']['failures'], [])
                for fmt in ('text', 'digest', 'markdown', 'html', 'json'):
                    self.assertTrue(report.export(result, fmt))

    def test_health_metrics_keep_every_status(self):
        """Findings drop ok, info and unknown Health metrics; health_metrics keeps
        all of them, so the Tuning view never reads an absent finding as ok.
        Both engines (MySQL 8.0 and 8.4, MariaDB 11.4 with and without P_S)."""
        for name in RECORDINGS:
            with self.subTest(server=name):
                result = run_report(name)[0]
                metrics = result['health_metrics']
                keys = [m['key'] for m in metrics]
                self.assertEqual(len(keys), len(set(keys)))
                self.assertTrue({'bp_dirty', 'bp_wait_free', 'aborted_clients', 'connections_used',
                                 'select_full_join'} <= set(keys))
                findings = {f['id'] for f in result['findings']}
                for m in metrics:
                    self.assertEqual(set(m), set(report._HEALTH_METRIC_FIELDS))
                    self.assertIn(m['status'], report.STATUSES)
                    if m['status'] in ('watch', 'problem') and m['key'] in report._METRIC_FAMILY:
                        self.assertIn('health.%s' % m['key'], findings)
                # At least one measured metric exists only here, not as a finding.
                self.assertTrue([m for m in metrics if m['status'] == 'ok' and 'health.%s' % m['key'] not in findings])
        self.assertEqual(report.health_metrics(None), [])
        self.assertEqual(report.health_metrics({'metrics': [{'key': 'x', 'status': 'ok', 'value': 1}]}),
                         [dict({k: None for k in report._HEALTH_METRIC_FIELDS}, key='x', status='ok', value=1)])

    def test_bounded_replays_preserve_original_samples_and_reject_other_sql(self):
        for name in RECORDINGS:
            with self.subTest(server=name):
                original, recorded = raw_replay('report', name)
                before = json.dumps(recorded, sort_keys=True)
                conn, data = replay('report', name)
                self.assertEqual(json.dumps(data, sort_keys=True), before)
                self.assertIn('synthetic', data['extension_provenance'])
                self.assertNotIn('superseded_calls', data)
                # Only the helper supplies bounded reads and count gates.
                self.assertFalse(any(sql.endswith(' LIMIT 2001') for sql in original.answers))
                self.assertFalse(any(sql.startswith((
                    '/*myflames:io*/ SELECT COUNT(*)',
                    '/*myflames:tables*/ SELECT COUNT(*)',
                    '/*myflames:indexes*/ SELECT COUNT(*)')) for sql in original.answers))
                for sql, answers in original.answers.items():
                    bounded = sql + ' LIMIT 2001'
                    if bounded not in conn.answers:
                        continue
                    # Keep both snapshots, including their order and counters.
                    self.assertEqual(len(answers), 2, sql)
                    self.assertEqual([conn.run(bounded), conn.run(bounded)], answers)
                    self.assertNotIn(sql, conn.answers)
                    for unexpected in (sql, bounded + ' LIMIT 2001', sql + ' LIMIT 2000'):
                        with self.assertRaisesRegex(AssertionError, 'unexpected SQL:'):
                            conn.run(unexpected)

    def test_limited_io_count_reuses_the_recorded_denial(self):
        original, _ = raw_replay('report', 'mysql-8.0-limited')
        conn, _ = replay('report', 'mysql-8.0-limited')
        sql = next(s for s in original.answers if
                   'FROM performance_schema.table_io_waits_summary_by_table ' in s)
        error = original.answers[sql][0]
        self.assertIsInstance(error, _Refused)
        count_sql = '/*myflames:io*/ SELECT COUNT(*) FROM ' + sql.split(' FROM ', 1)[1]
        with self.assertRaises(ConnectorError) as raised:
            conn.run(count_sql)
        self.assertEqual(str(raised.exception), str(error))
        self.assertNotIn(sql + ' LIMIT 2001', conn.answers)

    def test_every_recording_matches_the_published_schema(self):
        from test_chart_data import validate
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'docs', 'schemas',
                            'report-1.0.json')
        with open(path, encoding='utf-8') as handle:
            schema = json.load(handle)
        for name in RECORDINGS:
            with self.subTest(server=name):
                validate(json.loads(report.export(run_report(name)[0], 'json')), schema)
        with self.assertRaises(AssertionError):
            validate(dict(run_report('mysql-8.0')[0], extra=1), schema)

    def test_mysql_84_replica(self):
        result, conn, _ = run_report('mysql-8.4-replica')
        self.assertEqual(result['engine'], 'mysql')
        replication = finding(result, 'checks.replication')
        self.assertEqual(replication['status'], 'ok')
        self.assertEqual(replication['numbers'][0]['label'], 'Default channel')
        self.assertIn('0 is not proof', replication['caveat'])
        self.assertIn('/*myflames:checks*/ SHOW REPLICA STATUS', [sql for sql, _ in conn.calls])
        self.assertIn('--xml', dict(conn.calls)['/*myflames:checks*/ SHOW REPLICA STATUS'])
        writable = finding(result, 'checks.replica_read_only')
        self.assertEqual(writable['status'], 'watch')
        self.assertEqual(writable['action']['sql'], 'SET PERSIST super_read_only = ON;')
        self.assertIn('db-operations confirmation', writable['action']['text'])
        # Accounts: names only; the locked account and the role are skipped.
        nopass = finding(result, 'checks.no_password')
        self.assertEqual(nopass['status'], 'problem')
        self.assertEqual([n['text'] for n in nopass['numbers']], ["'nopass'@'%'", "''@'localhost'"])
        self.assertEqual(finding(result, 'checks.anonymous')['status'], 'watch')
        self.assertEqual(finding(result, 'checks.root_any_host')['status'], 'watch')
        self.assertNotIn('locked_nopass', json.dumps(result))
        self.assertNotIn('$A$005$', json.dumps(result))
        # No query cache on MySQL: the section says so.
        section = next(s for s in result['sections'] if s['key'] == 'query_cache')
        self.assertEqual((section['findings'], section['notes']), ([], ['MySQL 8.0 and later have no query cache.']))

    def test_attribution_links_health_and_indexes_to_digests(self):
        result, conn, _ = run_report('mysql-8.4-replica')
        a = result['attribution']
        self.assertTrue(a['available'])
        self.assertIsNone(a['gate'])
        joins = a['full_joins']['statements']
        self.assertEqual(len(joins), 1)
        self.assertGreater(joins[0]['select_full_join'], 0)
        self.assertIn('JOIN `customers`', joins[0]['digest_text'])
        self.assertIsNone(a['coverage']['overflow'])
        self.assertIsNotNone(a['coverage']['uptime_s'])
        self.assertEqual(a['label'], 'text match, not proof')
        orders = next(t for t in a['full_scans'] if t['table'] == 'orders')
        kinds = sorted(s['table_match']['kind'] for s in orders['statements'])
        self.assertIn('qualified', kinds)
        self.assertIn('unqualified', kinds)
        for item in orders['statements']:
            self.assertEqual(item['table_match']['label'], 'text match, not proof')
            self.assertGreater(item['no_index_used'] + item['select_scan'], 0)
        self.assertTrue(any("ORDER BY SUM_SELECT_FULL_JOIN DESC" in sql for sql, _ in conn.calls))
        metric = finding(result, 'health.select_full_join')
        self.assertEqual(metric['attribution'], 'full_joins')
        self.assertIn({'view': 'top', 'order': 'full_join', 'label': 'Open Top statements by joins without an index'},
                      metric['links'])
        scans = finding(result, 'indexes.full_scans')
        self.assertEqual(scans['attribution'], 'full_scans')
        self.assertIn('text match, not proof', scans['numbers'][0]['text'])
        self.assertIn({'view': 'top', 'order': 'no_index', 'table': 'mfshop.orders',
                       'label': 'Open Top statements that name mfshop.orders'}, scans['links'])

    def test_mysql_80(self):
        result, conn, _ = run_report('mysql-8.0')
        self.assertTrue(result['server_version'].startswith('8.0.'))
        self.assertEqual(finding(result, 'checks.replication')['status'], 'info')
        self.assertIn('not an asynchronous replica', finding(result, 'checks.replication')['verdict'])
        self.assertEqual(finding(result, 'sessions.long_running')['status'], 'problem')
        self.assertIn('the longest for', finding(result, 'sessions.long_running')['verdict'])
        engines = finding(result, 'tables.engines')
        self.assertEqual(engines['status'], 'watch')
        self.assertIn('REPAIR TABLE; myisam_recover_options is off by default', engines['verdict'])
        self.assertTrue(result['attribution']['available'])
        # Two problems in the first family (connections): the larger impact first.
        self.assertEqual(result['look_first'], result['findings'][0]['id'])
        self.assertTrue(result['look_first'].startswith('sessions.'))

    def test_mysql_80_with_a_limited_account_degrades_with_notes(self):
        result, _, _ = run_report('mysql-8.0-limited')
        self.assertIn('SELECT command denied', finding(result, 'checks.memory_budget')['verdict'])
        for fid in ('checks.no_password', 'checks.anonymous', 'checks.root_any_host'):
            self.assertEqual(finding(result, fid)['status'], 'unknown')
            self.assertIn('needs SELECT on mysql.user', finding(result, fid)['verdict'])
        self.assertIn('REPLICATION CLIENT', finding(result, 'checks.replication')['verdict'])
        self.assertIn('PROCESS', finding(result, 'innodb.unavailable')['verdict'])
        a = result['attribution']
        self.assertFalse(a['available'])
        self.assertEqual(a['gate'], 'This user cannot read performance_schema.')
        self.assertIn('GRANT SELECT ON performance_schema', a['fix'])
        self.assertEqual({v['key']: v['status'] for v in result['header']['views']}['innodb'], 'limited')

    def test_mariadb_114(self):
        result, conn, _ = run_report('mariadb-11.4')
        self.assertEqual(result['engine'], 'mariadb')
        self.assertIn('/*myflames:checks*/ SHOW ALL SLAVES STATUS', [sql for sql, _ in conn.calls])
        cache = finding(result, 'checks.query_cache')
        self.assertEqual(cache['status'], 'watch')
        share = next(n for n in cache['numbers'] if n['label'] == 'Hit share of SELECTs')
        self.assertTrue(0 < share['value'] < 100)
        self.assertIn('SET GLOBAL query_cache_type = OFF;', cache['action']['sql'])
        self.assertEqual(finding(result, 'checks.binlog_expiry')['headline'], '10 d 0 h')
        # mysql.global_priv gives the lock state, so the locked account is skipped.
        self.assertTrue(any('FROM mysql.global_priv' in sql for sql, _ in conn.calls))
        self.assertNotIn('locked_nopass', json.dumps(result))
        self.assertNotIn('mariadb.sys', json.dumps(result))
        self.assertIn('MariaDB repairs it when it is opened, by default', finding(result, 'tables.engines')['verdict'])
        uk = next(t for t in result['attribution']['full_scans'] if t['table'] == 'uk_only')
        self.assertEqual(sorted(s['table_match']['kind'] for s in uk['statements']), ['qualified', 'unqualified'])

    def test_mariadb_defaults_performance_schema_off(self):
        result, _, _ = run_report('mariadb-11.4-default')
        self.assertFalse(result['performance_schema'])
        a = result['attribution']
        self.assertFalse(a['available'])
        self.assertEqual(a['gate'], 'Performance Schema is off, so no statement can be linked to these numbers.')
        self.assertIn('performance_schema=ON', a['fix'])
        self.assertEqual(finding(result, 'indexes.usage')['status'], 'unknown')
        self.assertEqual(finding(result, 'waits.top')['status'], 'unknown')
        self.assertEqual(finding(result, 'instruments.views')['status'], 'watch')
        self.assertEqual({v['key']: v['status'] for v in result['header']['views']}['waits'], 'limited')
        self.assertIn('attribution: Performance Schema is off', report.format_digest(result))

    def test_mariadb_without_global_priv_falls_back_to_the_user_view(self):
        clock = FakeTime()
        conn, data = replay('report', 'mariadb-11.4', clock=clock)
        sql = next(s for s in conn.answers if 'FROM mysql.global_priv' in s)
        conn.answers[sql] = [_Refused("mysql client exited 1: ERROR 1142 (42000): SELECT command denied for table "
                                      "'global_priv'")]
        conn.answers[server_checks.CHECKS_TAG + server_checks._ACCOUNT_SQL['mariadb_view']] = [batch([
            ['root', 'localhost', 'mysql_native_password', 0, None, 'N'],
            ['mariadb.sys', 'localhost', 'mysql_native_password', 1, None, 'N'],
            ['app_role', '', '', 1, None, 'Y'],
            ['nopass', '%', 'mysql_native_password', 1, None, 'N'],
            ['sock', 'localhost', 'unix_socket', 1, None, 'N']])]
        result = report.analysis_report(conn, 5, workers=1, sleep=clock.sleep, clock=clock.monotonic)
        nopass = finding(result, 'checks.no_password')
        self.assertEqual([n['text'] for n in nopass['numbers']], ["'nopass'@'%'"])
        self.assertIn('lock state is not readable', nopass['verdict'])
        self.assertEqual(finding(result, 'checks.anonymous')['status'], 'ok')

    def test_mysql_before_8022_reads_show_slave_status(self):
        for version, sql in (('8.0.21', 'SHOW SLAVE STATUS'), ('8.0.22', 'SHOW REPLICA STATUS'),
                             ('8.4.11', 'SHOW REPLICA STATUS'), ('11.4.13-MariaDB', 'SHOW ALL SLAVES STATUS')):
            with self.subTest(version=version):
                self.assertEqual(server_checks._replica_sql(Server({'version': version}, {})), sql)
        server = Server({'version': '8.0.21', 'read_only': 'OFF'}, {})
        checks = server_checks.compute_checks(server, {'replicas': [{
            'Slave_IO_Running': 'Yes', 'Slave_SQL_Running': 'Yes', 'Seconds_Behind_Master': '5',
            'Channel_Name': '', 'Last_IO_Errno': '0', 'Last_SQL_Errno': '0'}], 'accounts': []})['checks']
        self.assertEqual(checks[0]['status'], 'ok')
        self.assertIn('Seconds_Behind_Master', checks[0]['sources'])


class TestServerChecks(unittest.TestCase):
    MYSQL = {'version': '8.4.11', 'version_comment': 'MySQL Community Server - GPL', 'log_bin': 'ON',
             'binlog_expire_logs_seconds': '2592000', 'binlog_expire_logs_auto_purge': 'ON', 'read_only': 'OFF',
             'super_read_only': 'OFF'}
    MARIADB = {'version': '11.4.13-MariaDB', 'log_bin': 'OFF', 'expire_logs_days': '0.000000',
               'binlog_expire_logs_seconds': '0', 'query_cache_type': 'OFF', 'query_cache_size': '1048576',
               'have_query_cache': 'YES', 'read_only': 'OFF'}

    def checks(self, variables=None, engine='mysql', **raw):
        base = dict(self.MYSQL if engine == 'mysql' else self.MARIADB)
        base.update(variables or {})
        data = {'status': {}, 'replicas': [], 'accounts': [], 'accounts_source': engine}
        data.update(raw)
        result = server_checks.compute_checks(Server(base, {'Uptime': '100'}), data)
        json.dumps(result, allow_nan=False)
        return {c['key']: c for c in result['checks']}

    def channel(self, **row):
        base = {'Channel_Name': '', 'Replica_IO_Running': 'Yes', 'Replica_SQL_Running': 'Yes',
                'Seconds_Behind_Source': '0', 'Last_IO_Errno': '0', 'Last_SQL_Errno': '0'}
        base.update(row)
        return base

    def test_replication_statuses(self):
        for row, status in (({}, 'ok'), ({'Replica_IO_Running': 'Connecting'}, 'watch'),
                            ({'Replica_IO_Running': 'No'}, 'problem'), ({'Replica_SQL_Running': 'No',
                                                                         'Seconds_Behind_Source': None}, 'problem'),
                            ({'Seconds_Behind_Source': '59'}, 'ok'), ({'Seconds_Behind_Source': '60'}, 'watch'),
                            ({'Seconds_Behind_Source': '600'}, 'problem'),
                            ({'Last_SQL_Errno': '1062', 'Last_SQL_Error': 'Duplicate entry'}, 'problem'),
                            ({'Last_IO_Errno': '2003', 'Last_IO_Error': "Can't connect"}, 'problem')):
            with self.subTest(row=row):
                check = self.checks(replicas=[self.channel(**row)])['replication']
                self.assertEqual(check['status'], status)
                if status != 'ok':
                    self.assertTrue(check['action'])
        two = self.checks(replicas=[self.channel(Channel_Name='a'), self.channel(Channel_Name='b',
                                                                                 Replica_SQL_Running='No')])
        self.assertEqual(two['replication']['status'], 'problem')
        self.assertEqual([n['label'] for n in two['replication']['numbers']], ['Channel a', 'Channel b'])
        self.assertIn('lag unknown', self.checks(replicas=[self.channel(Seconds_Behind_Source=None)])
                      ['replication']['numbers'][0]['text'])
        mariadb = self.checks(engine='mariadb', replicas=[{
            'Connection_name': 'm1', 'Slave_IO_Running': 'Yes', 'Slave_SQL_Running': 'Yes',
            'Seconds_Behind_Master': '0', 'Last_IO_Errno': '0', 'Last_SQL_Errno': '0'}])
        self.assertEqual(mariadb['replication']['status'], 'ok')
        self.assertEqual(mariadb['replica_read_only']['action']['sql'], 'SET GLOBAL read_only = ON;')
        self.assertEqual(self.checks(variables={'read_only': 'ON'}, replicas=[self.channel()])
                         ['replica_read_only']['status'], 'ok')
        self.assertNotIn('replica_read_only', self.checks(replicas=[]))
        denied = self.checks(replicas=None, replica_error='Access denied')['replication']
        self.assertEqual(denied['status'], 'unknown')
        self.assertIn('SUPER or REPLICATION CLIENT', denied['why'])

    def test_group_replication_channels_are_not_judged(self):
        gr = [self.channel(Channel_Name='group_replication_applier', Replica_IO_Running='No',
                           Seconds_Behind_Source=None),
              self.channel(Channel_Name='group_replication_recovery', Replica_IO_Running='No',
                           Replica_SQL_Running='No', Seconds_Behind_Source=None)]
        checks = self.checks(replicas=gr)
        self.assertEqual(checks['replication']['status'], 'info')
        self.assertIn('Group Replication member', checks['replication']['why'])
        self.assertNotIn('replica_read_only', checks)
        mixed = self.checks(replicas=gr + [self.channel(Channel_Name='async1', Seconds_Behind_Source='5')])
        self.assertEqual(mixed['replication']['status'], 'ok')
        self.assertEqual([n['label'] for n in mixed['replication']['numbers']], ['Channel async1'])
        self.assertEqual(mixed['replica_read_only']['status'], 'watch')

    def test_lag_caveat_and_privilege_texts_follow_the_engine_and_version(self):
        mysql = self.checks(replicas=[self.channel()])['replication']['caveat']
        self.assertIn('while the receiver is not connected (on MySQL once the applier has caught up)', mysql)
        row = {'Connection_name': '', 'Slave_IO_Running': 'Yes', 'Slave_SQL_Running': 'Yes',
               'Seconds_Behind_Master': '0', 'Last_IO_Errno': '0', 'Last_SQL_Errno': '0'}
        maria = self.checks(engine='mariadb', replicas=[row])
        self.assertIn('(always on MariaDB)', maria['replication']['caveat'])
        self.assertIn('before MariaDB 12.0 no setting stops them', maria['replica_read_only']['caveat'])
        new = self.checks({'version': '12.0.2-MariaDB'}, engine='mariadb', replicas=[row])
        self.assertIn('read_only=NO_LOCK_NO_ADMIN stops them too', new['replica_read_only']['caveat'])
        for version, need in (('10.11.16-MariaDB', 'SLAVE MONITOR or SUPER privilege'),
                              ('11.4.13-MariaDB', 'the SLAVE MONITOR privilege')):
            with self.subTest(version=version):
                denied = self.checks({'version': version}, engine='mariadb', replicas=None, replica_error='denied')
                self.assertIn(need, denied['replication']['why'])
        self.assertIn('the SUPER or REPLICATION CLIENT privilege',
                      self.checks(replicas=None, replica_error='denied')['replication']['why'])

    def test_accounts(self):
        rows = [['root', 'localhost', 'caching_sha2_password', '0', 'N', 'N'],
                ['root', '%', 'caching_sha2_password', '0', 'N', 'N'],
                ['nopass', '%', 'caching_sha2_password', '1', 'N', 'N'],
                ['', 'localhost', 'mysql_native_password', '1', 'N', 'N'],
                ['locked', '%', 'caching_sha2_password', '1', 'Y', 'N'],
                ['sock', 'localhost', 'auth_socket', '1', 'N', 'N'],
                ["o'hara", '%', 'caching_sha2_password', '1', 'N', 'N']]
        checks = self.checks(accounts=rows)
        self.assertEqual(checks['no_password']['status'], 'problem')
        self.assertEqual([n['text'] for n in checks['no_password']['numbers']],
                         ["'nopass'@'%'", "''@'localhost'", "'o''hara'@'%'"])
        self.assertIn("ALTER USER 'nopass'@'%' IDENTIFIED BY", checks['no_password']['action']['sql'])
        self.assertEqual(checks['anonymous']['status'], 'watch')
        self.assertEqual(checks['root_any_host']['status'], 'watch')
        self.assertIn("DROP USER 'root'@'%';", checks['root_any_host']['action']['sql'])
        clean = self.checks(accounts=rows[:1])
        self.assertEqual([clean[k]['status'] for k in ('no_password', 'anonymous', 'root_any_host')], ['ok'] * 3)
        many = self.checks(accounts=[['u%d' % i, '%', 'caching_sha2_password', '1', 'N', 'N'] for i in range(14)])
        self.assertEqual(many['no_password']['numbers'][-1], {'label': 'More', 'text': '4 more', 'value': None})
        # MariaDB: ed25519 and parsec never store '' for a password, so an
        # empty value means nobody can log in with it, not "no password".
        maria = self.checks(engine='mariadb', accounts_source='mariadb', accounts=[
            ['ed', '%', 'ed25519', '1', 'N', 'N'], ['pa', '%', 'parsec', '1', 'N', 'N'],
            ['native', '%', 'mysql_native_password', '1', 'N', 'N'], ['old', '%', '', '1', 'N', 'N']])
        self.assertEqual([n['text'] for n in maria['no_password']['numbers']], ["'native'@'%'", "'old'@'%'"])
        unreadable = self.checks(accounts=None, accounts_error='denied')
        self.assertEqual({unreadable[k]['status'] for k in ('no_password', 'anonymous', 'root_any_host')}, {'unknown'})

    def test_account_sql_never_selects_a_password_hash(self):
        for sql in server_checks._ACCOUNT_SQL.values():
            selected = sql.split(' FROM ')[0]
            compared = re.sub(r"IFNULL\(JSON_VALUE\(Priv, '\$\.authentication_string'\), ''\) = ''|"
                              r"authentication_string = ''", '', selected)
            self.assertNotEqual(compared, selected)
            self.assertNotIn('authentication_string', compared)
            self.assertNotIn('Password', selected)

    def test_query_cache(self):
        self.assertEqual(self.checks()['binlog_expiry']['key'], 'binlog_expiry')
        self.assertNotIn('query_cache', self.checks())
        self.assertEqual(self.checks(engine='mariadb')['query_cache']['status'], 'ok')
        self.assertEqual(self.checks({'query_cache_type': 'ON', 'query_cache_size': '0'}, engine='mariadb')
                         ['query_cache']['status'], 'ok')
        self.assertEqual(self.checks({'have_query_cache': 'NO'}, engine='mariadb')['query_cache']['status'], 'info')
        on = self.checks({'query_cache_type': 'DEMAND', 'query_cache_size': '16777216'}, engine='mariadb',
                         status={'Qcache_hits': '300', 'Com_select': '100', 'Qcache_inserts': '50',
                                 'Qcache_lowmem_prunes': '7'})['query_cache']
        self.assertEqual(on['status'], 'watch')
        self.assertIn('75.0% of SELECTs', on['why'])
        self.assertNotIn('only be turned on', on['caveat'])
        self.assertIn('a non-zero query_cache_size at startup turns query_cache_type ON', on['caveat'])

    def test_binlog_expiry(self):
        self.assertEqual(self.checks()['binlog_expiry']['status'], 'ok')
        self.assertEqual(self.checks()['binlog_expiry']['value'], '30 d 0 h')
        never = self.checks({'binlog_expire_logs_seconds': '0', 'expire_logs_days': '0'})['binlog_expiry']
        self.assertEqual(never['status'], 'watch')
        self.assertEqual(never['action']['sql'], 'SET PERSIST binlog_expire_logs_seconds = <seconds>;')
        self.assertEqual(self.checks({'binlog_expire_logs_seconds': '0', 'expire_logs_days': '7'})['binlog_expiry']
                         ['status'], 'ok')
        self.assertEqual(self.checks({'binlog_expire_logs_auto_purge': 'OFF'})['binlog_expiry']['status'], 'watch')
        self.assertEqual(self.checks(engine='mariadb')['binlog_expiry']['status'], 'info')
        # A total size limit purges too: MariaDB 11.4+ max_binlog_total_size, Percona binlog_space_limit.
        for name in ('max_binlog_total_size', 'binlog_space_limit'):
            with self.subTest(name=name):
                sized = self.checks({'log_bin': 'ON', name: str(10 << 30)}, engine='mariadb')['binlog_expiry']
                self.assertEqual(sized['status'], 'ok')
                self.assertIn('exceed 10.0 GiB (%s)' % name, sized['why'])
                self.assertIn(name, sized['sources'])
        both = self.checks({'binlog_space_limit': str(1 << 30)})['binlog_expiry']
        self.assertIn('30 d 0 h, or sooner when together they exceed 1.0 GiB', both['why'])
        self.assertEqual(self.checks({'log_bin': 'ON', 'max_binlog_total_size': '0'}, engine='mariadb')
                         ['binlog_expiry']['status'], 'watch')
        maria = self.checks({'log_bin': 'ON'}, engine='mariadb')['binlog_expiry']
        self.assertEqual(maria['status'], 'watch')
        self.assertEqual(maria['action']['sql'], 'SET GLOBAL binlog_expire_logs_seconds = <seconds>;')

    def test_replica_status_is_read_by_name_from_xml(self):
        xml = ('<?xml version="1.0"?>\n\n<resultset statement="SHOW REPLICA STATUS\n" '
               'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n  <row>\n'
               '\t<field name="Replica_IO_Running">Yes</field>\n'
               '\t<field name="Seconds_Behind_Source" xsi:nil="true" />\n'
               '\t<field name="Last_SQL_Error">a &lt;b&gt; &amp; c</field>\n  </row>\n</resultset>\n')
        conn = ReplayConn([[server_checks.CHECKS_TAG + 'SHOW REPLICA STATUS', xml]])
        rows = server_checks._named_rows(conn, 'SHOW REPLICA STATUS')
        self.assertEqual(rows, [{'Replica_IO_Running': 'Yes', 'Seconds_Behind_Source': None,
                                 'Last_SQL_Error': 'a <b> & c'}])
        self.assertEqual(conn.calls[0][1], ['--comments', '--xml'])
        empty = ReplayConn([[server_checks.CHECKS_TAG + 'SHOW REPLICA STATUS',
                             '<?xml version="1.0"?>\n<resultset statement="x"></resultset>\n']])
        self.assertEqual(server_checks._named_rows(empty, 'SHOW REPLICA STATUS'), [])
        evil = ReplayConn([[server_checks.CHECKS_TAG + 'SHOW REPLICA STATUS',
                            '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "b">]><resultset/>']])
        with self.assertRaises(ValueError):
            server_checks._named_rows(evil, 'SHOW REPLICA STATUS')


class TestRankingAndFailures(ReportShape):
    def test_rank_and_look_first(self):
        def f(fid, status, family, impact=None):
            return report._finding(fid, family, status, fid, impact=impact)
        findings = report.rank_findings([f('a', 'ok', 'redo'), f('b', 'watch', 'io'), f('c', 'problem', 'indexes'),
                                         f('d', 'watch', 'redo', 1), f('e', 'watch', 'redo', 50),
                                         f('g', 'unknown', 'replication'), f('h', 'problem', 'replication')])
        self.assertEqual([x['id'] for x in findings], ['h', 'c', 'e', 'd', 'b', 'g', 'a'])
        self.assertEqual(report.look_first(findings), 'h')
        self.assertEqual(report.look_first(report.rank_findings([f('a', 'watch', 'io'), f('b', 'info', 'redo')])),
                         'a')
        self.assertIsNone(report.look_first([f('a', 'ok', 'io'), f('b', 'unknown', 'redo')]))

    def test_at_or_worse(self):
        result, _, _ = run_report('mysql-8.4-replica')
        problems = report.at_or_worse(result, 'problem')
        self.assertEqual({f['id'] for f in problems}, {f['id'] for f in result['findings'] if f['status'] == 'problem'})
        self.assertTrue({'checks.no_password', 'tables.no_primary_key'} <= {f['id'] for f in problems})
        watch = report.at_or_worse(result, 'watch')
        self.assertEqual(len(watch), result['summary']['problem'] + result['summary']['watch'])

    def test_a_failing_view_keeps_the_rest_and_redacts_the_password(self):
        with mock.patch('myflames.innodb_status.innodb_report', side_effect=ValueError('boom ' + PASSWORD)), \
                mock.patch('myflames.server_checks.server_checks', side_effect=RuntimeError('bug ' + PASSWORD)):
            result, _, _ = run_report('mysql-8.4-replica')
        failures = {f['view']: f for f in result['header']['failures']}
        self.assertEqual(set(failures), {'innodb', 'checks'})
        self.assertEqual(failures['innodb']['error'], 'boom [redacted]')
        self.assertIn('internal error (RuntimeError)', failures['checks']['error'])
        self.assertNotIn(PASSWORD, json.dumps(result))
        self.assertFalse([f for f in result['findings'] if f['id'].startswith(('innodb.', 'checks.'))])
        security = next(s for s in result['sections'] if s['key'] == 'security')
        self.assertEqual(security['status'], 'unknown')
        self.assertIn('Server checks', security['notes'][0])
        self.assertTrue(any('2 of 10 views did not run or failed' in n for n in result['notes']))
        self.assertIn('VIEWS NOT READ', report.format_text(result))

    def test_too_many_tables_skips_indexes_and_tables_with_a_note(self):
        result, conn, _ = run_report('mysql-8.4-replica', require_choice_above=1)
        status = {v['key']: v['status'] for v in result['header']['views']}
        self.assertEqual((status['indexes'], status['tables']), ('skipped', 'skipped'))
        self.assertFalse(any(sql.startswith(('/*myflames:indexes*/', '/*myflames:tables*/')) for sql, _ in conn.calls))
        self.assertTrue(any('Indexes and Tables were skipped; choose databases '
                            '(or use --schema on the command line) to include them.' in n for n in result['notes']))
        # The attribution still lists joins without an index.
        self.assertTrue(result['attribution']['full_joins']['statements'])
        self.assertEqual(result['attribution']['full_scans'], [])

    def test_bad_arguments_are_refused_before_connecting(self):
        conn = ReplayConn([])
        for kwargs in ({'interval': 1}, {'interval': 61}, {'interval': '5'}, {'workers': 4}, {'workers': 0},
                       {'schemas': 'shop'}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    report.analysis_report(conn, **dict({'interval': 5}, **kwargs))
        self.assertEqual(conn.calls, [])

    def test_connection_failure_is_one_error(self):
        conn = ReplayConn([], error=ConnectorError('mysql client exited 1: ERROR 2003: cannot connect ' + PASSWORD))
        conn.password = PASSWORD
        with self.assertRaises(ValueError) as caught:
            report.analysis_report(conn, 5)
        self.assertIn('Could not read the server', str(caught.exception))
        self.assertNotIn(PASSWORD, str(caught.exception))


class TestConcurrency(unittest.TestCase):
    def test_threaded_report_uses_real_client_with_private_option_files(self):
        # Feed recorded SQL answers through actual subprocesses, rather than
        # ReplayConn.run(), so missing/unreadable credentials cannot be hidden.
        for engine in ('mysql-8.4-replica', 'mariadb-11.4'):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as directory, \
                    mock.patch.object(tempfile, 'tempdir', directory):
                source, _ = replay('report', engine)
                answers = {sql: ({'error': str(values[0])} if isinstance(values[0], _Refused)
                                 else {'stdout': values[0]}) for sql, values in source.answers.items()}
                clock = FakeTime()
                with option_file_client(directory, answers) as binary, \
                        MySQLConnection('h', password=PASSWORD, binary=binary) as conn:
                    result = report.analysis_report(conn, 5, workers=3,
                                                    sleep=clock.sleep, clock=clock.monotonic)
                self.assertEqual(result['header']['failures'], [])
                self.assertEqual(len(result['header']['views']), 10)
                paths = []
                for name in os.listdir(directory):
                    if name.startswith('record-'):
                        with open(os.path.join(directory, name)) as handle:
                            paths.append(json.load(handle))
                self.assertGreater(len(paths), 20)
                self.assertEqual(len(paths), len(set(paths)))
                self.assertTrue(all(not os.path.exists(path) for path in paths))
                self.assertFalse(any(name.endswith('.cnf') for name in os.listdir(directory)))

    def test_at_most_three_views_at_once_windowed_views_first(self):
        conn, data = replay('report', 'mysql-8.4-replica')
        lock = threading.Lock()
        state = {'now': 0, 'max': 0, 'order': []}
        original = conn.run

        def run(sql, extra_flags=None, timeout=None):
            tag = sql.split('*/')[0][len('/*myflames:'):]
            with lock:
                state['now'] += 1
                state['max'] = max(state['max'], state['now'])
                if tag not in state['order']:
                    state['order'].append(tag)
            try:
                time.sleep(0.002)
                return original(sql, extra_flags, timeout)
            finally:
                with lock:
                    state['now'] -= 1
        conn.run = run
        result = report.analysis_report(conn, 5, sleep=lambda s: None)
        self.assertLessEqual(state['max'], report.MAX_WORKERS)
        self.assertGreater(state['max'], 1)
        self.assertEqual(state['order'][0], 'report')
        self.assertEqual(set(state['order'][1:4]), {'health', 'waits', 'io'})
        self.assertEqual(result['header']['failures'], [])

    def test_interrupt_cancels_queued_views_and_wakes_sleepers(self):
        cancel = threading.Event()
        started = []

        def job(name, fail=False):
            def run(conn):
                started.append(name)
                if fail:
                    raise KeyboardInterrupt
                cancel.wait(5)
                return {}
            return run
        jobs = [('a', job('a', fail=True)), ('b', job('b')), ('c', job('c')), ('d', job('d'))]
        t0 = time.monotonic()
        with self.assertRaises(KeyboardInterrupt):
            report._run_all(object(), jobs, 1, cancel, None)
        self.assertTrue(cancel.is_set())
        self.assertLess(time.monotonic() - t0, 4)
        self.assertEqual(started[0], 'a')
        self.assertNotIn('d', started)

    def test_no_statement_starts_after_a_cancel(self):
        cancel = threading.Event()
        conn = ReplayConn([['/*myflames:x*/ SELECT 1', '1\n']])
        guarded = report._Guarded(conn, cancel)
        self.assertEqual(guarded.run('/*myflames:x*/ SELECT 1'), '1\n')
        self.assertEqual(guarded.server_version(), '')
        cancel.set()
        with self.assertRaisesRegex(ConnectorError, 'Cancelled'):
            guarded.run('/*myflames:x*/ SELECT 1')
        self.assertEqual(len(conn.calls), 1)

    def test_single_flight_joins_identical_requests_and_refuses_others(self):
        gate, calls = threading.Event(), []

        def work():
            calls.append(1)
            gate.wait(5)
            return {'schema_version': 'report-1.0', 'n': len(calls)}
        results = []
        first = threading.Thread(target=lambda: results.append(report.run_single_flight('k', (5,), work)))
        first.start()
        for _ in range(200):
            if 'k' in report._FLIGHTS:
                break
            time.sleep(0.005)
        second = threading.Thread(target=lambda: results.append(report.run_single_flight('k', (5,), work)))
        second.start()
        with self.assertRaisesRegex(ValueError, 'already running'):
            report.run_single_flight('k', (10,), work)
        gate.set()
        first.join(5)
        second.join(5)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertIsNot(results[0], results[1])
        self.assertNotIn('k', report._FLIGHTS)
        # Errors reach every waiter, and the key is free again afterwards.
        with self.assertRaisesRegex(ValueError, 'nope'):
            report.run_single_flight('k', (5,), lambda: (_ for _ in ()).throw(ValueError('nope')))
        self.assertEqual(report.run_single_flight('k', (5,), lambda: 7), 7)

    def test_single_flight_callers_own_nested_results(self):
        started, release, joined = threading.Event(), threading.Event(), threading.Event()
        source = {'sections': [{'findings': ['original']}]}
        results, errors, calls = {}, [], []

        def work():
            calls.append(1)
            started.set()
            if not release.wait(5):
                raise RuntimeError('owner was not released')
            return source

        def run(name):
            try:
                results[name] = report.run_single_flight('copy-test', (5,), work)
            except BaseException as exc:
                errors.append(exc)

        owner = threading.Thread(target=run, args=('owner',))
        waiter = threading.Thread(target=run, args=('waiter',))
        owner.start()
        try:
            self.assertTrue(started.wait(5))
            done = report._FLIGHTS['copy-test'].done
            wait = done.wait

            def joined_wait(*args, **kwargs):
                joined.set()
                return wait(*args, **kwargs)

            with mock.patch.object(done, 'wait', side_effect=joined_wait):
                waiter.start()
                self.assertTrue(joined.wait(5))
                release.set()
                owner.join(5)
                waiter.join(5)
        finally:
            release.set()
            owner.join(5)
            if waiter.ident is not None:
                waiter.join(5)
        self.assertFalse(owner.is_alive())
        self.assertFalse(waiter.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(calls, [1])
        self.assertEqual(results['owner'], source)
        self.assertEqual(results['waiter'], source)
        results['owner']['sections'][0]['findings'].append('owner')
        self.assertEqual(results['waiter']['sections'][0]['findings'], ['original'])
        results['waiter']['sections'][0]['findings'].append('waiter')
        self.assertEqual(results['owner']['sections'][0]['findings'], ['original', 'owner'])
        self.assertEqual(source['sections'][0]['findings'], ['original'])
        self.assertNotIn('copy-test', report._FLIGHTS)


class TestFormatsEscapeServerStrings(unittest.TestCase):
    """A hostile table name (HTML, Markdown and a terminal escape) travels from
    the server answers through every format."""

    @classmethod
    def setUpClass(cls):
        def edit(sql, text, n):
            return text.replace('events_log', HOSTILE) if isinstance(text, str) and text else text
        cls.result, _, _ = run_report('mysql-8.4-replica', edit=edit)

    def test_the_name_reaches_the_report(self):
        nokey = finding(self.result, 'tables.no_primary_key')
        self.assertIn('mfshop.' + HOSTILE, nokey['verdict'])
        data = json.loads(report.export(self.result, 'json'))
        self.assertEqual(finding(data, 'tables.no_primary_key')['verdict'], nokey['verdict'])

    def test_html_is_static_and_escaped(self):
        page = report.export(self.result, 'html')
        self.assertTrue(page.startswith('<!DOCTYPE html>'))
        self.assertNotIn('<img', page)
        self.assertIn('ev&lt;img src=x onerror=alert(1)&gt;|`*_[x](y)\\u001b[31m', page)
        self.assertNotIn('\x1b', page)
        # The only script element holds JSON data, with < escaped.
        self.assertEqual(page.count('<script'), 1)
        self.assertIn('<script type="application/json" id="myflames-report">', page)
        data = page.split('id="myflames-report">', 1)[1].split('</script>', 1)[0]
        self.assertNotIn('<', data)
        self.assertEqual(json.loads(data)['schema_version'], 'report-1.0')
        self.assertIn("Inspired by Brendan Gregg&rsquo;s FlameGraph and Tanel Poder&rsquo;s SQL Plan FlameGraphs",
                      page)

    def test_markdown_escapes_html_and_markdown(self):
        text = report.export(self.result, 'markdown')
        self.assertTrue(text.startswith('# Full analysis: MySQL 8.4.11'))
        self.assertNotIn('<img', text)
        self.assertIn('ev&lt;img src=x onerror=alert(1)&gt;\\|\\`\\*\\_\\[x\\](y)\\u001b\\[31m', text)
        self.assertNotIn('\x1b', text)
        # Every row of a table keeps the header's column count.
        blocks, block = [], []
        for line in text.splitlines() + ['']:
            if line.startswith('|'):
                block.append(len(re.findall(r'(?<!\\)\|', line)))
            elif block:
                blocks.append(block)
                block = []
        self.assertTrue(blocks)
        for counts in blocks:
            self.assertEqual(len(set(counts)), 1, counts)
        self.assertTrue(any('ev&lt;img' in line and line.startswith('|') for line in text.splitlines()))
        self.assertIn("Inspired by Brendan Gregg's FlameGraph and Tanel Poder's SQL Plan FlameGraphs", text)

    def test_markdown_code_fences_outgrow_backticks(self):
        self.assertEqual(report.md_code('a ``` b'), '````\na ``` b\n````')
        self.assertEqual(report.md('1. x'), '1\\. x')
        self.assertEqual(report.md('# x'), '\\# x')
        self.assertEqual(report.md('0.01 s'), '0.01 s')

    def test_terminal_output_escapes_control_characters(self):
        def edit(sql, text, n):
            return text.replace('events_log', HOSTILE) if isinstance(text, str) and text else text
        code, out, err, _ = replay_cli('mysql-8.4-replica', ['-q'], edit=edit)
        self.assertEqual(code, 0, err)
        self.assertNotIn('\x1b', out)
        self.assertIn('\\u001b[31m', out)
        self.assertIn('ev<img src=x onerror=alert(1)>', out)

    def test_digest_is_compact(self):
        text = report.export(self.result, 'digest')
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith('server: MySQL 8.4.11'))
        self.assertIn('look_first: %s' % self.result['look_first'], lines)
        self.assertFalse([l for l in lines if l.startswith('[ok]')])
        self.assertTrue(any(l.startswith('ok: ') for l in lines))
        self.assertTrue(any(l.startswith('full_join digest ') for l in lines))
        self.assertTrue(any('(text match, not proof)' in l for l in lines))
        self.assertLess(len(text), len(report.export(self.result, 'text')))


def replay_cli(name, argv, edit=None):
    """Run myflames report with a replayed connection and a fake clock."""
    clock = FakeTime()
    conn, _ = replay('report', name, clock=clock, edit=edit)

    def analysis(c, interval, **options):
        return ORIGINAL_ANALYSIS(c, 5, workers=1, sleep=clock.sleep, clock=clock.monotonic, **options)
    with mock.patch('myflames.report.analysis_report', side_effect=analysis):
        return run_cli('report', argv, conn)


class TestReportCLI(unittest.TestCase):
    def test_all_views_failed_exit_2_without_output_or_file_replacement(self):
        def broken(conn, jobs, workers, cancel, password):
            def fail(_conn):
                raise ConnectorError('client could not open its defaults file')
            return {key: report._timed(key, fail, conn, password) for key, _ in jobs}

        for flags in ([], ['--json'], ['--json', '-q', '--fail-on', 'watch']):
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as directory:
                output = os.path.join(directory, 'report.json')
                with open(output, 'w') as handle:
                    handle.write('keep existing report')
                with mock.patch.object(report, '_run_all', side_effect=broken):
                    code, out, err, _ = replay_cli('mysql-8.4-replica', flags + ['-o', output])
                self.assertEqual((code, out), (2, ''))
                self.assertIn('all report views failed', err)
                self.assertIn('client could not open its defaults file', err)
                with open(output) as handle:
                    self.assertEqual(handle.read(), 'keep existing report')

    def test_partial_failures_keep_report_and_warn_on_stderr(self):
        original = report._run_all

        def partial(conn, jobs, *args):
            def fail(_conn):
                raise ConnectorError('health unavailable')
            return original(conn, [(key, fail if key == 'health' else fn) for key, fn in jobs], *args)

        for flags, expected_code in ((['--json'], 0), (['--json', '-q'], 0),
                                     (['--json', '--fail-on', 'problem'], 1)):
            with self.subTest(flags=flags), mock.patch.object(report, '_run_all', side_effect=partial):
                code, out, err, _ = replay_cli('mysql-8.4-replica', flags)
            self.assertEqual(code, expected_code)
            self.assertEqual([f['view'] for f in json.loads(out)['header']['failures']], ['health'])
            self.assertEqual(err.count('warning: Report views failed: Health\n'), 1)

    def test_formats_and_flags(self):
        code, out, err, factory = replay_cli('mysql-8.4-replica', ['-u', 'app', '-p' + PASSWORD, '--json'])
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(json.loads(out)['schema_version'], 'report-1.0')
        self.assertEqual(factory.options['query_timeout'], 60)
        code, out, err, _ = replay_cli('mysql-8.4-replica', [])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith('Full analysis: MySQL 8.4.11'))
        for block in ('LOOK AT FIRST', 'FINDINGS (problem and watch, ranked)', 'SECTIONS',
                      'STATEMENTS BEHIND THE NUMBERS', 'NOT CHECKED'):
            self.assertIn(block, out)
        self.assertIn('note: Statuses are rules of thumb', err)
        code, out, err, _ = replay_cli('mysql-8.4-replica', ['-q'])
        self.assertEqual((code, err), (0, ''))
        for flag, start in (('--markdown', '# Full analysis'), ('--html', '<!DOCTYPE html>'),
                            ('--digest', 'server: MySQL 8.4.11')):
            code, out, err, _ = replay_cli('mysql-8.4-replica', [flag])
            self.assertEqual(code, 0)
            self.assertTrue(out.startswith(start), flag)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'r.html')
            code, out, err, _ = replay_cli('mariadb-11.4', ['--html', '-o', path])
            self.assertEqual((code, out), (0, ''))
            self.assertIn('Written to', err)
            with open(path, encoding='utf-8') as handle:
                self.assertIn('Query cache', handle.read())

    def test_fail_on_exit_codes(self):
        code, out, err, _ = replay_cli('mysql-8.4-replica', ['--fail-on', 'problem', '--json'])
        self.assertEqual(code, 1)
        self.assertIn('FAIL: 5 finding(s) at --fail-on problem or worse:', err)
        self.assertIn('  [problem] Accounts without a password: 2', err)
        code, out, err, _ = replay_cli('mysql-8.4-replica', ['--fail-on', 'watch', '-q'])
        self.assertEqual((code, err), (1, ''))
        clean, _, _ = run_report('mysql-8.4-replica')
        clean['findings'] = [f for f in clean['findings'] if f['status'] not in ('problem', 'watch')]
        with mock.patch('myflames.report.analysis_report', return_value=clean):
            code, out, err, _ = run_cli('report', ['--fail-on', 'watch', '--json'], ReplayConn([]))
        self.assertEqual(code, 0)
        self.assertIn('OK: no finding at --fail-on watch', err)

    def test_bad_input_and_failures_exit_2(self):
        for argv in (['--interval', '1'], ['--interval', '61'], ['--fail-on', 'warn'], ['--json', '--html']):
            with self.subTest(argv=argv):
                code, out, _, factory = run_cli('report', argv, ReplayConn([]))
                self.assertEqual((code, out, factory.options), (2, '', None))
        conn = ReplayConn([], error=ConnectorError('mysql client exited 1: ERROR 1045: denied ' + PASSWORD))
        code, out, err, _ = run_cli('report', ['-p' + PASSWORD, '--fail-on', 'problem'], conn)
        self.assertEqual((code, out), (2, ''))
        self.assertNotIn(PASSWORD, err)
        self.assertIn('Could not read the server', err)

    def test_schema_flags_reach_the_views(self):
        seen = {}

        def analysis(conn, interval, **options):
            seen.update(options, interval=interval)
            return run_report('mysql-8.4-replica')[0]
        with mock.patch('myflames.report.analysis_report', side_effect=analysis):
            run_cli('report', ['--schema', 'a', '--schema', 'b', '--interval', '7', '-q'], ReplayConn([]))
        self.assertEqual((seen['schemas'], seen['interval'], seen['require_choice_above']), (['a', 'b'], 7, 2000))
        with mock.patch('myflames.report.analysis_report', side_effect=analysis):
            run_cli('report', ['--all-schemas', '-q'], ReplayConn([]))
        # With --schema, --all-schemas lifts the 2,000-table limit for those schemas only (the API's all_schemas).
        seen.clear()
        with mock.patch('myflames.report.analysis_report', side_effect=analysis):
            code, _out, err, _ = run_cli('report', ['--schema', 'a', '--all-schemas', '-q'], ReplayConn([]))
        self.assertEqual(code, 0, err)
        self.assertEqual((seen['schemas'], seen['require_choice_above']), (['a'], None))
        self.assertIsNone(seen['require_choice_above'])

    def test_help(self):
        code, out, _ = run_main(['myflames', '--help'])
        self.assertIn('  report    Full analysis: every Server view once, ranked findings, sections (--html, --fail-on)',
                      out)
        code, out, _ = run_main(['myflames', 'report', '--help'])
        self.assertEqual(code, 0)
        text = ' '.join(out.split())
        for phrase in ('--fail-on LEVEL', '--markdown', '--html', '--digest', '1 a finding reached the --fail-on level',
                       'never password hashes'):
            self.assertIn(phrase, text)


class TestReportAPI(unittest.TestCase):
    def test_analysis_checks_scope_once_and_disables_both_child_gates(self):
        # Full analysis owns the cap, including explicit opt-out and chosen
        # schemas. Children must not repeat the expensive count query.
        for schemas in (None, ('shop',)):
            for limit in (None, 17, 2000):
                with self.subTest(schemas=schemas, limit=limit), \
                        mock.patch('myflames.report.preflight', return_value=object()), \
                        mock.patch('myflames.report.require_choice') as gate, \
                        mock.patch('myflames.schema_health.table_report') as tables, \
                        mock.patch('myflames.indexes.index_report') as indexes, \
                        mock.patch('myflames.report._attribution'), \
                        mock.patch('myflames.report.compose_report'):
                    def run_children(conn, jobs, *args):
                        for key, read in jobs:
                            if key in ('tables', 'indexes'):
                                read(conn)
                        return {}
                    conn = mock.Mock(password=None)
                    with mock.patch('myflames.report._run_all', side_effect=run_children):
                        report.analysis_report(conn, 5, schemas=schemas, require_choice_above=limit)
                    if limit is None:
                        gate.assert_not_called()
                    else:
                        gate.assert_called_once_with(mock.ANY, mock.ANY, schemas, limit, 'the full analysis')
                    tables.assert_called_once_with(conn, schemas, False, report.LIST_LIMIT,
                                                   require_choice_above=None)
                    indexes.assert_called_once_with(conn, schemas=schemas, limit=report.LIST_LIMIT,
                                                    require_choice_above=None)

    def connection(self):
        return {'host': '127.0.0.1', 'port': 3306, 'user': 'app', 'password': PASSWORD}

    def call(self, payload, name='mariadb-11.4'):
        clock = FakeTime()
        conn, _ = replay('report', name, clock=clock)
        if payload.get('schemas'):
            schemas = tuple(payload['schemas'])
            server = mock.Mock(engine='mariadb' if name.startswith('mariadb') else 'mysql')
            sql = report.REPORT_TAG + _table_count_sql(server, False, scope_sql('TABLE_SCHEMA', schemas))
            inventory = next(answers[0] for query, answers in conn.answers.items()
                             if query.startswith('/*myflames:tables*/ SELECT TABLE_SCHEMA, TABLE_NAME, ENGINE,'))
            count = sum(row.split('\t')[0] in schemas for row in inventory.splitlines())
            conn.answers[sql] = [batch([[count]])]
        factory = FakeFactory(conn)

        def analysis(c, interval, **options):
            return ORIGINAL_ANALYSIS(c, 5, workers=1, sleep=clock.sleep, clock=clock.monotonic, **options)
        with mock.patch('myflames.connector.MySQLConnection', factory), \
                mock.patch('myflames.report.analysis_report', side_effect=analysis) as patched:
            result = ui.api_response('/api/live/report', dict(payload, connection=self.connection()),
                                     workspace=object())
        return result, factory, patched

    def test_route(self):
        result, factory, patched = self.call({'interval': 5, 'schemas': ['mfshop']})
        self.assertEqual(result['schema_version'], 'report-1.0')
        self.assertEqual(factory.options['query_timeout'], 60)
        args, kwargs = patched.call_args
        self.assertEqual((args[1], kwargs['schemas'], kwargs['require_choice_above']), (5, ('mfshop',), 2000))
        result, _, patched = self.call({'all_schemas': True})
        self.assertIsNone(patched.call_args[1]['require_choice_above'])
        self.assertEqual(patched.call_args[0][1], report.DEFAULT_INTERVAL)
        self.assertIn('/api/live/report', ui._SERVER_VIEW_PATHS)
        self.assertIn('/api/live/report', ui._CONNECTION_PATHS)
        source = inspect.getsource(ui.WorkspaceHandler.do_POST)
        self.assertIn('"/api/live/report"', source)
        self.assertIn('"/api/report/export"', source)

    def test_errors_are_redacted(self):
        conn = ReplayConn([], error=ConnectorError('mysql client exited 1: ERROR 1045: denied ' + PASSWORD))
        with mock.patch('myflames.connector.MySQLConnection', FakeFactory(conn)):
            with self.assertRaises(ValueError) as caught:
                ui.api_response('/api/live/report', {'connection': self.connection()}, workspace=object())
        self.assertNotIn(PASSWORD, str(caught.exception))
        with self.assertRaisesRegex(ValueError, 'sampling interval'):
            with mock.patch('myflames.connector.MySQLConnection', FakeFactory(ReplayConn([]))):
                ui.api_response('/api/live/report', {'connection': self.connection(), 'interval': 0},
                                workspace=object())

    def test_export(self):
        result, _, _ = run_report('mariadb-11.4')
        for fmt, mime, ext in (('markdown', 'text/markdown', '.md'), ('html', 'text/html', '.html'),
                               ('json', 'application/json', '.json'), ('text', 'text/plain', '.txt'),
                               ('digest', 'text/plain', '.txt')):
            with self.subTest(fmt=fmt):
                out = ui.api_response('/api/report/export', {'report': result, 'format': fmt})
                self.assertEqual((out['format'], out['mime']), (fmt, mime))
                self.assertTrue(out['filename'].startswith('myflames-report-') and out['filename'].endswith(ext))
                self.assertEqual(out['content'], report.export(result, fmt))
        for payload in ({'report': result, 'format': 'pdf'}, {'report': {'schema_version': 'top-1.0'},
                                                              'format': 'html'}, {'format': 'html'}):
            with self.assertRaises(ValueError):
                ui.api_response('/api/report/export', payload)

    def test_export_names_its_connection_in_every_format(self):
        # The workspace sends the connection the report was read from (it may
        # no longer be the one in use); every format names it, never a password.
        result, _, _ = run_report('mariadb-11.4')
        source = {'label': 'Prod (analyst@db.prod:3306)', 'host': 'db.prod', 'port': 3306, 'user': 'analyst',
                  'type': 'production', 'password': PASSWORD, 'ssl_ca': '/secret/ca.pem'}
        line = 'Prod (analyst@db.prod:3306), production connection'
        for fmt in ('text', 'digest', 'markdown', 'html', 'json'):
            with self.subTest(fmt=fmt):
                out = ui.api_response('/api/report/export', {'report': result, 'format': fmt, 'source': source})
                self.assertNotIn(PASSWORD, out['content'])
                self.assertNotIn('/secret/ca.pem', out['content'])
                if fmt == 'json':
                    self.assertEqual(json.loads(out['content'])['source'], {
                        'label': 'Prod (analyst@db.prod:3306)', 'host': 'db.prod', 'port': 3306, 'user': 'analyst',
                        'type': 'production'})
                else:
                    self.assertIn(line, out['content'])
        # Without a source nothing is added; an unusable one is dropped.
        self.assertNotIn('Connection:', report.export(result, 'text'))
        self.assertNotIn('source', json.loads(report.export(result, 'json', {'port': 'x', 'password': PASSWORD})))
        # A report re-exported with its source keeps it, still checked.
        exported = json.loads(report.export(result, 'json', source))
        self.assertIn(line, report.export(exported, 'markdown'))
        from test_chart_data import validate
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'docs', 'schemas',
                            'report-1.0.json')
        with open(path, encoding='utf-8') as handle:
            validate(exported, json.load(handle))



class TestReviewGate(unittest.TestCase):
    """Regression tests for the review gate on the report feature."""

    def test_single_flight_key_separates_credentials_tls_and_profiles(self):
        base = {'host': 'db', 'port': 3306, 'user': 'app', 'password': 'pw1', 'ssl_ca': '/a.pem',
                'connect_timeout': 8, 'query_timeout': 60}
        key = report.flight_key(base, 'p1')
        self.assertEqual(key, report.flight_key(dict(base, query_timeout=12), 'p1'))
        for change, profile in (({'password': 'pw2'}, 'p1'), ({'ssl_ca': '/b.pem'}, 'p1'),
                                ({'ssl_mode': 'REQUIRED'}, 'p1'), ({'database': 'shop'}, 'p1'), ({}, 'p2'),
                                ({}, None), ({'password': None}, 'p1')):
            with self.subTest(change=change, profile=profile):
                self.assertNotEqual(key, report.flight_key(dict(base, **change), profile))
        self.assertNotIn('pw1', repr(key))

    def test_concurrent_requests_with_other_credentials_never_share_a_report(self):
        gate, calls = threading.Event(), []

        def analysis(conn, interval, **options):
            calls.append(conn.password)
            gate.wait(5)
            return {'schema_version': 'report-1.0', 'password_seen': conn.password}

        class Conn(object):
            def __init__(self, **options):
                self.password = options.get('password')

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        results = {}

        def call(name, password):
            payload = {'connection': {'host': '127.0.0.1', 'user': 'app', 'password': password}, 'interval': 5}
            results[name] = ui.api_response('/api/live/report', payload, workspace=object())
        with mock.patch('myflames.connector.MySQLConnection', Conn), \
                mock.patch('myflames.report.analysis_report', side_effect=analysis):
            threads = [threading.Thread(target=call, args=('good', 'right')),
                       threading.Thread(target=call, args=('bad', 'wrong')),
                       threading.Thread(target=call, args=('same', 'right'))]
            threads[0].start()
            for _ in range(200):
                if calls:
                    break
                time.sleep(0.005)
            threads[1].start()
            threads[2].start()
            for _ in range(200):
                if len(calls) == 2:
                    break
                time.sleep(0.005)
            gate.set()
            for t in threads:
                t.join(5)
        self.assertEqual(sorted(calls), ['right', 'wrong'])
        self.assertEqual(results['bad']['password_seen'], 'wrong')
        self.assertEqual(results['good']['password_seen'], 'right')
        self.assertEqual(results['same'], results['good'])
        self.assertIsNot(results['same'], results['good'])

    def test_unreadable_tablespaces_make_fragmentation_unknown(self):
        result, _, _ = run_report('mysql-8.0-limited')
        frag = finding(result, 'tables.fragmentation')
        self.assertEqual((frag['status'], frag['headline']), ('unknown', 'not judged'))
        self.assertIn('needs the PROCESS privilege', frag['verdict'])
        base = {'scope': {'schemas': None}, 'summary': {'unknown_tablespace': 2}, 'tablespaces_known': True,
                'notes': [], 'fragmentation': []}
        frag = next(f for f in report._tables_findings(base) if f['id'] == 'tables.fragmentation')
        self.assertEqual(frag['status'], 'unknown')
        self.assertIn('2 tables could not be matched to a tablespace', frag['verdict'])
        observed = dict(base, fragmentation=[{'schema': 's', 'table': 't', 'status': 'info', 'free_bytes': 1 << 30,
                                           'free_pct': 40, 'caveat': 'c', 'online_sql': 'x'}])
        frag = next(f for f in report._tables_findings(observed) if f['id'] == 'tables.fragmentation')
        self.assertEqual(frag['status'], 'unknown')
        self.assertIn('could not be matched', frag['verdict'])
        clean = dict(base, summary={'unknown_tablespace': 0})
        self.assertEqual(next(f for f in report._tables_findings(clean)
                              if f['id'] == 'tables.fragmentation')['status'], 'ok')

    def test_reusable_allocation_remains_informational_in_full_report(self):
        # Large allocation is evidence, not a capacity or rebuild threshold.
        base = {'scope': {'schemas': ['app']}, 'summary': {'unknown_tablespace': 0},
                'tablespaces_known': True, 'fragmentation': [
                    {'schema': 'app', 'table': 'large', 'status': 'info',
                     'free_bytes': 238 * (1 << 30), 'free_pct': 12,
                     'fix_sql': 'OPTIMIZE TABLE `app`.`large`'}]}
        item = next(f for f in report._tables_findings(base) if f['id'] == 'tables.fragmentation')
        self.assertEqual(item['status'], 'info')
        self.assertIn('238', item['headline'])
        self.assertIn('reusable allocation', item['verdict'])
        self.assertNotIn('sql', item['action'])
        self.assertIn('Tables', item['action']['text'])

    def test_unknown_reusable_space_ratio_does_not_print_none_percent(self):
        base = {'scope': {'schemas': ['app']}, 'summary': {'unknown_tablespace': 0},
                'tablespaces_known': True, 'fragmentation': [
                    {'schema': 'app', 'table': 'partial', 'status': 'info',
                     'free_bytes': 100, 'free_pct': None}]}
        item = next(f for f in report._tables_findings(base) if f['id'] == 'tables.fragmentation')
        text = json.dumps(item)
        self.assertIn('percentage unknown', text)
        self.assertNotIn('None%', text)
        self.assertNotIn('null%', text)

    def test_statuses_keep_unknowns_visible(self):
        self.assertEqual([report._worst(x) for x in (['ok', 'unknown'], ['info', 'ok'], ['unknown', 'watch'],
                                                     ['problem', 'unknown'], [])],
                         ['unknown', 'info', 'watch', 'problem', 'ok'])

        def f(fid, status, family='security'):
            return report._finding(fid, family, status, fid.title())
        summary = report.summarize(report.rank_findings([f('a.ok', 'ok'), f('b.unk', 'unknown'), f('c.info', 'info')]))
        self.assertEqual(summary['status'], 'unknown')
        self.assertIn('1 unknown', summary['text'])
        self.assertEqual(report.summarize([f('a.ok', 'ok'), f('c.info', 'info')])['status'], 'ok')
        self.assertEqual(report.summarize([f('a.ok', 'ok')], [{'view': 'innodb'}])['status'], 'unknown')
        result, _, _ = run_report('mysql-8.0-limited')
        security = next(sec for sec in result['sections'] if sec['key'] == 'security')
        self.assertEqual(security['status'], 'unknown')
        self.assertTrue(security['summary'].startswith('3 unknown: '))
        memory = next(sec for sec in result['sections'] if sec['key'] == 'memory')
        self.assertEqual(memory['status'], 'unknown')
        self.assertIn('2 unknown: Total memory budget, Buffer pool size', memory['summary'])
        self.assertIn('ok', memory['summary'])
        with mock.patch('myflames.innodb_status.innodb_report', side_effect=ValueError('boom')):
            failed, _, _ = run_report('mariadb-11.4-default')
        innodb = next(sec for sec in failed['sections'] if sec['key'] == 'innodb')
        self.assertEqual(innodb['status'], 'unknown')
        self.assertIn('1 view not read', failed['summary']['text'])

    def test_unknown_findings_are_explained_in_every_format(self):
        result, _, _ = run_report('mysql-8.0-limited')
        text = report.export(result, 'text')
        self.assertIn('NOT JUDGED (unknown)', text)
        self.assertIn('needs SELECT on mysql.user', ' '.join(text.split()))
        self.assertIn('SUPER or REPLICATION CLIENT', ' '.join(text.split()))
        markdown = report.export(result, 'markdown')
        self.assertIn('## Not judged', markdown)
        self.assertIn('needs SELECT on mysql.user', markdown)
        page = report.export(result, 'html')
        self.assertIn('id="not-judged"', page)
        visible = page.split('<script', 1)[0] + page.split('</script>', 1)[1]
        self.assertIn('needs SELECT on mysql.user', visible)
        self.assertIn('[unknown] checks.no_password', report.export(result, 'digest'))

    def coverage_report(self):
        result, _, _ = run_report('mysql-8.4-replica')
        a = result['attribution']
        a['coverage'] = dict(a['coverage'], first_seen='2026-10-01 10:00:00', digest_lost=12,
                             overflow={'executions': 1234, 'total_ms': 5000.0})
        a['health_window'] = {'select_full_join': 7, 'rate': 1.4, 'elapsed_s': 5, 'status': 'watch'}
        a['full_scans'][0]['capped'] = True
        a['full_scans'][1].update(statements=[], error='ERROR 1142: denied')
        a['full_scans'][2]['statements'] = []
        return result

    def test_attribution_coverage_appears_in_every_format(self):
        result = self.coverage_report()
        page = report.export(result, 'html')
        visible = page.split('<script', 1)[0] + page.split('</script>', 1)[1]
        for fmt, out in (('text', report.export(result, 'text')), ('markdown', report.export(result, 'markdown')),
                         ('html', visible), ('digest', report.export(result, 'digest'))):
            flat = ' '.join(out.replace('\\', '').split())
            with self.subTest(fmt=fmt):
                self.assertIn('cumulative since the server started or the digest table was truncated', flat)
                self.assertIn('first seen 2026-10-01 10:00:00', flat)
                self.assertIn("Health counted 7 joins without an index in its 5 s window", flat)
                self.assertIn('1,234 executions', flat)
                self.assertIn('Performance_schema_digest_lost is 12', flat)
                self.assertIn('only the 500 heaviest digests that name it were checked', flat)
                self.assertIn('text match, not proof', flat)
        markdown = report.export(result, 'markdown')
        self.assertIn('view, a stored routine or a trigger can read the table', markdown)

    def test_attribution_errors_are_shown_not_called_empty(self):
        result = self.coverage_report()
        failed, empty = result['attribution']['full_scans'][1], result['attribution']['full_scans'][2]
        page = report.export(result, 'html')
        for fmt, out in (('text', report.export(result, 'text')), ('markdown', report.export(result, 'markdown')),
                         ('html', page), ('digest', report.export(result, 'digest'))):
            with self.subTest(fmt=fmt):
                self.assertIn('Could not read: ERROR 1142: denied', out.replace('\\', ''))
                self.assertIn('No recorded statement names it', out)
                self.assertNotIn('None matched', out)
        self.assertEqual((failed['statements'], empty['error']), ([], None))

    def test_null_lag_with_running_threads_is_unknown(self):
        server = Server({'version': '8.4.11', 'read_only': 'ON'}, {})
        row = {'Channel_Name': '', 'Replica_IO_Running': 'Yes', 'Replica_SQL_Running': 'Yes',
               'Seconds_Behind_Source': None, 'Last_IO_Errno': '0', 'Last_SQL_Errno': '0'}
        check = server_checks.compute_checks(server, {'replicas': [row], 'accounts': []})['checks'][0]
        self.assertEqual(check['status'], 'unknown')
        self.assertIn('lag unknown (NULL)', check['why'])
        worse = dict(row, Replica_IO_Running='Connecting')
        self.assertEqual(server_checks.compute_checks(server, {'replicas': [worse], 'accounts': []})
                         ['checks'][0]['status'], 'watch')
        two = server_checks.compute_checks(server, {'replicas': [dict(row, Channel_Name='a'), dict(
            row, Channel_Name='b', Seconds_Behind_Source='3')], 'accounts': []})['checks'][0]
        self.assertEqual(two['status'], 'unknown')


class TestAdditionalCheckFamilies(ReportShape):
    """Synthetic SQL replays complement the unchanged live observations.

    New fixture provenance explicitly distinguishes modeled test values from
    captured server output. No database or host RAM is required for these tests.
    """

    def scenario(self, name='mysql84', edit=None):
        conn, data = replay('report', 'checks-' + name, edit=edit)
        with mock.patch.object(server_checks, '_version_support', side_effect=lambda server, as_of=None:
                               ORIGINAL_VERSION_SUPPORT(server, datetime.date(2026, 10, 15))):
            checks = server_checks.server_checks(conn)
        items = list(checks['checks']) + server_checks.engine_cache_checks(checks, data['inventory'])
        self.assert_read_only(conn)
        json.dumps(checks, allow_nan=False)
        return {c['key']: c for c in items}, conn, data, checks

    def values(self, check):
        return {n['label']: n.get('value') for n in check['numbers']}

    def memory(self, name='mysql84', **raw):
        _, _, data, _ = self.scenario(name)
        variables = dict(line.split('\t', 1) for line in data['calls'][0][1].splitlines())
        status_sql = server_checks.CHECKS_TAG + server_checks.STATUS_SQL
        text = next(c[1] for c in data['calls'] if c[0] == status_sql)
        inputs = {'ps_memory_bytes': 4096, 'status': dict(line.split('\t', 1) for line in text.splitlines())}
        inputs.update(raw)
        return server_checks._memory_budget(Server(variables, {}), inputs), variables, inputs

    def test_mysql_and_mariadb_replays_and_schema_stay_compatible(self):
        for name, lifecycle, caches, wsrep in (
                ('mysql80', 'problem', {'myisam_cache': 'watch'}, None),
                ('mysql84', 'ok', {'myisam_cache': 'ok'}, None),
                ('mariadb114', 'ok', {'myisam_cache': 'watch', 'aria_cache': 'ok'}, 'watch'),
                ('mariadb114-no-engines', 'ok', {'myisam_cache': 'ok', 'aria_cache': 'ok'}, 'watch')):
            with self.subTest(scenario=name):
                items, _, _, checks = self.scenario(name)
                self.assertEqual(items['memory_budget']['status'], 'unknown')
                self.assertEqual(items['version_support']['status'], lifecycle)
                self.assertEqual({k: items[k]['status'] for k in caches}, caches)
                self.assertEqual(items.get('wsrep', {}).get('status'), wsrep)
                for key in ('memory_budget', 'version_support') + tuple(caches) + (('wsrep',) if wsrep else ()):
                    self.assertTrue(items[key]['numbers'], key)
                    self.assertTrue(items[key]['rule'], key)
                    self.assertTrue(items[key]['caveat'], key)
                # Reuse the current fields/families, no report schema migration.
                mapped = report._check_findings('checks', list(items.values()), {})
                for f in mapped:
                    self.assertEqual(set(f), FINDING_FIELDS)
                    self.assertIn(f['family'], report.SECTION_TITLES)
                self.assertEqual(checks['schema_version'], 'checks-1.0')

    def test_memory_formula_mysql_temptable_shared_once(self):
        check, variables, _ = self.memory()
        values = self.values(check)
        global_bytes = 1024 + 2048 + 3072 + 32768 + 4096
        # Binary logging is OFF: no transaction or statement binlog cache.
        # Two initial network/result buffers; shared TempTable is not multiplied.
        session = 4096 + 5120 + 6144 + 7168 + 8192 + 2 * 10240
        self.assertEqual(values['Known global capacity bytes'], global_bytes)
        self.assertEqual(values['Per-connection scenario bytes'], session)
        self.assertEqual(values['Budget at max_connections'], global_bytes + 20 * session)
        self.assertEqual(values['Budget at Max_used_connections'], global_bytes + 3 * session)
        self.assertEqual(values['temptable_max_mmap'], 65536)
        self.assertIsNone(values['Physical memory bytes'])
        self.assertIn('physical RAM is unknown', check['why'])
        self.assertIn('allocated at connect', check['caveat'])
        self.assertIn('thread creation', check['caveat'])
        self.assertIn('allocated on demand', check['caveat'])
        self.assertIn('not a hard upper bound', check['caveat'])
        self.assertIn('its own internal buffers', check['caveat'])
        self.assertIn("WHERE EVENT_NAME LIKE 'memory/performance_schema/%'", server_checks.PS_MEMORY_SQL)

    def test_memory_formula_mariadb_and_mysql_memory_engine(self):
        check, variables, inputs = self.memory('mariadb114')
        numbers = self.values(check)
        expected_global = 1024 + 2 * 2048 + 3072 + 1024 + 32768 + 4096
        expected_session = 4096 + 5120 + 6144 + 7168 + 8192 + 2 * 10240 + 8192
        self.assertEqual(numbers['Known global capacity bytes'], expected_global)
        self.assertEqual(numbers['One internal MEMORY table per connection'], 8192)
        self.assertEqual(numbers['Budget at max_connections'], expected_global + expected_session * 20)
        self.assertEqual(numbers['Budget at Max_used_connections'], expected_global + expected_session * 3)
        _, mysql, inputs = self.memory()
        mysql['internal_tmp_mem_storage_engine'] = 'MEMORY'
        check = server_checks._memory_budget(Server(mysql, {}), inputs)
        numbers = self.values(check)
        self.assertEqual(numbers['Known global capacity bytes'], 1024 + 2048 + 3072 + 4096)
        self.assertEqual(numbers['One internal MEMORY table per connection'], 8192)
        self.assertNotIn('temptable_max_ram', numbers)

    def test_binlog_memory_both_caches_only_when_enabled_on_both_engines(self):
        for name in ('mysql84', 'mariadb114'):
            _, variables, inputs = self.memory(name)
            variables.update(binlog_cache_size='9216', binlog_stmt_cache_size='11264')
            for log_bin, extra in (('ON', 9216 + 11264), ('OFF', 0)):
                with self.subTest(engine=name, log_bin=log_bin):
                    variables['log_bin'] = log_bin
                    result = server_checks._memory_budget(Server(variables, {}), inputs)
                    values = self.values(result)
                    expected_session = 4096 + 5120 + 6144 + 7168 + 8192 + 2 * 10240 + extra
                    if name == 'mariadb114':
                        expected_session += 8192
                    self.assertEqual(values['Per-connection scenario bytes'], expected_session)
                    global_bytes = values['Known global capacity bytes']
                    self.assertEqual(values['Budget at max_connections'], global_bytes + 20 * expected_session)
                    self.assertEqual(values['Budget at Max_used_connections'], global_bytes + 3 * expected_session)
                    self.assertIn('first binlog write', result['caveat'])
                    self.assertIn('binlog_stmt_cache_size', result['sources'])
            # Missing caches do not make an OFF scenario partial, but must do so
            # with ON or an unreadable log_bin (never assume logging is OFF).
            variables.pop('binlog_cache_size')
            variables.pop('binlog_stmt_cache_size')
            for log_bin, partial in (('OFF', False), ('ON', True), (None, True)):
                variables['log_bin'] = log_bin
                result = server_checks._memory_budget(Server(variables, {}), inputs)
                self.assertEqual('(partial)' in str(result['numbers']), partial)
                if log_bin is None:
                    self.assertIn('log_bin', result['why'])
                elif log_bin == 'ON':
                    self.assertIn('binlog_cache_size', result['why'])
                    self.assertIn('binlog_stmt_cache_size', result['why'])

    def test_mariadb_two_redo_buffers_with_mmap_caveat(self):
        _, variables, inputs = self.memory('mariadb114')
        variables['innodb_log_buffer_size'] = '4096'
        result = server_checks._memory_budget(Server(variables, {}), inputs)
        self.assertEqual(self.values(result)['Known global capacity bytes'],
                         1024 + 2 * 4096 + 3072 + 1024 + 32768 + 4096)
        self.assertIn('two redo buffers', result['caveat'])
        self.assertIn('memory-mapped', result['caveat'])

    def test_memory_ram_comparison_and_partial_inputs(self):
        check, variables, raw = self.memory()
        values = self.values(check)
        global_bytes, maximum = values['Known global capacity bytes'], values['Budget at max_connections']
        for ram, status in ((global_bytes - 1, 'problem'), (global_bytes, 'watch'),
                            (maximum - 1, 'watch'), (maximum, 'ok'), (maximum * 2, 'ok'), (0, 'unknown')):
            with self.subTest(ram=ram):
                result = server_checks._memory_budget(Server(variables, {}), dict(raw, physical_memory_bytes=ram))
                self.assertEqual(result['status'], status)
        for name in ('sort_buffer_size', 'max_connections', 'internal_tmp_mem_storage_engine'):
            with self.subTest(missing=name):
                incomplete = dict(variables)
                incomplete.pop(name)
                result = server_checks._memory_budget(Server(incomplete, {}), dict(raw, physical_memory_bytes=maximum))
                self.assertEqual(result['status'], 'unknown')
                self.assertIn(name, result['why'])
        _, maria, maria_inputs = self.memory('mariadb114')
        maria.pop('tmp_table_size')
        incomplete = server_checks._memory_budget(Server(maria, {}), maria_inputs)
        self.assertEqual(incomplete['status'], 'unknown')
        self.assertIsNone(self.values(incomplete)['One internal MEMORY table per connection'])
        for invalid in ('-1', 'NaN', None):
            result = server_checks._memory_budget(Server(dict(variables, sort_buffer_size=invalid), {}), raw)
            self.assertEqual(result['status'], 'unknown')
        partial = server_checks._memory_budget(Server(variables, {}), dict(raw, ps_memory_bytes=None,
                                               ps_memory_error='SELECT denied on performance_schema'))
        self.assertIn('SELECT denied', partial['why'])
        self.assertTrue(any('(partial)' in n['label'] for n in partial['numbers']))

    def test_version_table_edges_and_unknown_series(self):
        for engine, rows in server_checks.VERSION_SUPPORT.items():
            for series, (_, end) in rows.items():
                version = '%d.%d.99' % series + ('-MariaDB' if engine == 'mariadb' else '')
                server = Server({'version': version}, {})
                if end is None:
                    check = ORIGINAL_VERSION_SUPPORT(server, datetime.date(2026, 10, 15))
                    self.assertEqual(check['status'], 'unknown')
                    self.assertNotIn('Maintenance ends', self.values(check))
                    continue
                end_date = datetime.date(*[int(n) for n in end.split('-')])
                for before, expected in ((server_checks.EOL_WATCH_DAYS + 1, 'ok'),
                                         (server_checks.EOL_WATCH_DAYS, 'watch'), (1, 'watch'),
                                         (0, 'problem'), (-1, 'problem')):
                    with self.subTest(engine=engine, series=series, before=before):
                        check = ORIGINAL_VERSION_SUPPORT(server, end_date - datetime.timedelta(days=before))
                        self.assertEqual(check['status'], expected)
                        self.assertEqual(self.values(check)['Days until maintenance ends'], before)
                        self.assertIn(server_checks.VERSION_LAST_REVIEWED, str(check['numbers']))
        for version in ('99.1.0', '11.9.0-MariaDB', ''):
            check = ORIGINAL_VERSION_SUPPORT(Server({'version': version}, {}), datetime.date(2026, 10, 15))
            self.assertEqual(check['status'], 'unknown')
        for version, classification in (('8.4.11', 'supported'), ('9.7.2', 'current'),
                                        ('8.0.46', 'past end of life'), ('9.6.1', 'past end of life'),
                                        ('11.8.9-MariaDB', 'supported'), ('12.3.3-MariaDB', 'unknown')):
            check = ORIGINAL_VERSION_SUPPORT(Server({'version': version}, {}), datetime.date(2026, 10, 15))
            self.assertEqual(check['value'], classification)
        self.assertEqual(server_checks.VERSION_SUPPORT['mysql'][(8, 0)][1], '2026-04-21')

    def test_unknown_support_is_not_an_actionable_report_warning(self):
        for version, expected in (('26.7.0', 'unknown'), ('12.3.2-MariaDB', 'unknown'),
                                  ('13.0.2-MariaDB', 'unknown'), ('99.1.0', 'unknown'),
                                  ('8.0.46', 'problem'), ('10.6.25-MariaDB', 'problem')):
            with self.subTest(version=version):
                check = ORIGINAL_VERSION_SUPPORT(Server({'version': version}, {}),
                                                 datetime.date(2026, 10, 19))
                finding = report._check_findings('checks', [check], {})[0]
                self.assertEqual(finding['status'], expected)
                if expected == 'unknown' and version != '99.1.0':
                    self.assertIn('support unknown', finding['verdict'])
                    self.assertIn('not been verified', finding['verdict'])

    def test_unproved_version_lifecycles_stay_unknown_with_source_notes(self):
        for version, engine, series, note in (
                ('26.7.0', 'mysql', (26, 7), 'not been verified'),
                ('13.0.2-MariaDB', 'mariadb', (13, 0), 'not been verified'),
                ('12.3.1-MariaDB', 'mariadb', (12, 3), 'not been verified')):
            with self.subTest(version=version):
                self.assertIn(series, server_checks.VERSION_SUPPORT[engine])
                check = ORIGINAL_VERSION_SUPPORT(Server({'version': version}, {}), datetime.date(2026, 10, 15))
                self.assertEqual(check['status'], 'unknown')
                self.assertEqual(check['value'], 'unknown')
                self.assertIn(note, check['why'])
                self.assertNotIn('Maintenance ends', self.values(check))
        for minor in (3, 5, 6, 7):
            self.assertIn((11, minor), server_checks.VERSION_SUPPORT['mariadb'])
        for minor in (0, 1, 2):
            self.assertIn((12, minor), server_checks.VERSION_SUPPORT['mariadb'])
        self.assertNotEqual(server_checks.VERSION_CURRENT['mariadb'], (12, 3))
        self.assertEqual(server_checks.VERSION_LAST_REVIEWED, '2026-10-15')

    def test_cache_miss_thresholds_zero_activity_and_high_water(self):
        _, _, data, checks = self.scenario('mysql80')
        for requests, reads, expected in ((999, 999, 'ok'), (1000, 49, 'ok'), (1000, 50, 'watch'),
                                          (0, 0, 'unknown'), (None, 0, 'unknown'), (100, 101, 'unknown')):
            with self.subTest(requests=requests, reads=reads):
                evidence = dict(checks['cache_evidence'])
                evidence['status'] = dict(evidence['status'], Key_read_requests=requests, Key_reads=reads,
                                          Key_blocks_used='50000')
                rep = dict(checks, cache_evidence=evidence)
                item = server_checks.engine_cache_checks(rep, data['inventory'])[0]
                self.assertEqual(item['status'], expected)
                self.assertIn('high-water mark', item['caveat'])
                self.assertNotIn('occupancy', self.values(item))
                self.assertEqual(self.values(item)['Unused block bytes'], 2048)
        missing = server_checks.engine_cache_checks(checks, None, 'SELECT denied on TABLES')[0]
        self.assertEqual(missing['status'], 'unknown')
        self.assertIn('SELECT denied', missing['why'])
        for inventory in ({'available': False, 'summary': {'engines': []}},
                          {'summary': {'engines': [{'engine': 'unknown', 'tables': 1}]}}):
            self.assertEqual(server_checks.engine_cache_checks(checks, inventory)[0]['status'], 'unknown')
        _, _, maria_data, maria_checks = self.scenario('mariadb114')
        evidence = dict(maria_checks['cache_evidence'])
        evidence['status'] = dict(evidence['status'], Aria_pagecache_reads='50')
        aria = server_checks.engine_cache_checks(dict(maria_checks, cache_evidence=evidence), maria_data['inventory'])[1]
        self.assertEqual(aria['status'], 'watch')
        self.assertEqual(self.values(aria)['Read miss percent'], 5.0)
        self.assertEqual(self.values(aria)['Unused block bytes'], 16384)
        self.assertIn('Aria_pagecache_blocks_used are high-water marks', aria['caveat'])
        denied = dict(checks['cache_evidence'], status_error='Access denied to global status')
        item = server_checks.engine_cache_checks(dict(checks, cache_evidence=denied), data['inventory'])[0]
        self.assertEqual(item['status'], 'unknown')
        self.assertIn('Access denied', item['why'])

    def test_wsrep_states_and_missing_privileges(self):
        for changes, expected in (({}, 'ok'), ({'wsrep_flow_control_paused': '0.099'}, 'ok'),
                                  ({'wsrep_flow_control_paused': '0.10'}, 'watch'),
                                  ({'wsrep_local_cert_failures': '1'}, 'watch'),
                                  ({'wsrep_connected': 'OFF'}, 'problem'), ({'wsrep_ready': 'OFF'}, 'problem'),
                                  ({'wsrep_cluster_status': 'Non-Primary'}, 'problem'),
                                  ({'wsrep_flow_control_paused': '1.1'}, 'unknown'),
                                  ({'wsrep_local_state': '2'}, 'watch'), ({'wsrep_cluster_size': '1'}, 'watch'),
                                  ({'wsrep_cluster_size': None}, 'unknown')):
            def edit(sql, text, n):
                if sql == server_checks.CHECKS_TAG + server_checks.STATUS_SQL:
                    pairs = dict(line.split('\t', 1) for line in text.splitlines())
                    pairs.update(wsrep_flow_control_paused='0', wsrep_local_cert_failures='0')
                    pairs.update(changes)
                    return batch(pairs.items())
                return text
            with self.subTest(changes=changes):
                items, _, _, _ = self.scenario('mariadb114', edit)
                self.assertEqual(items['wsrep']['status'], expected)
                self.assertIn('not a current failure rate', items['wsrep']['caveat'])
                self.assertIn('since provider start or last FLUSH STATUS', items['wsrep']['caveat'])
                self.assertIn('provider-dependent', items['wsrep']['caveat'])
        def denied(sql, text, n):
            if sql == server_checks.CHECKS_TAG + server_checks.STATUS_SQL:
                return _Refused('ERROR 1227: Access denied to global status')
            if sql == server_checks.CHECKS_TAG + server_checks.PS_MEMORY_SQL:
                return _Refused('ERROR 1142: SELECT command denied for memory_summary_global_by_event_name')
            return text
        items, _, _, checks = self.scenario('mariadb114', denied)
        self.assertEqual(items['wsrep']['status'], 'unknown')
        self.assertIn('Access denied', items['wsrep']['why'])
        self.assertIn('SELECT on performance_schema', items['memory_budget']['why'])
        self.assertIn('SELECT command denied', items['memory_budget']['why'])
        self.assertIn('Access denied', items['myisam_cache']['why'])
        self.assertEqual(items['myisam_cache']['status'], 'unknown')
        self.assertEqual(server_checks._wsrep(Server({'version': '8.4.11'}, {}), {'status': {}}), [])

    def test_wsrep_disabled_or_without_provider_is_omitted(self):
        for variables in ({'wsrep_provider': 'none'}, {'wsrep_provider': 'NONE'}, {'wsrep_provider': ''},
                          {'wsrep_on': 'OFF'}, {'wsrep_on': '0'},
                          {'wsrep_on': 'OFF', 'wsrep_provider': '/usr/lib/libgalera_smm.so'},
                          {'wsrep_on': 'ON', 'wsrep_provider': 'none'}):
            for raw in ({'status': {}}, {'status': {'wsrep_connected': 'ON', 'wsrep_cluster_size': '3'}},
                        {'status': {}, 'status_error': 'Access denied to global status'}):
                with self.subTest(variables=variables, raw=raw):
                    server_ = Server(dict(variables, version='11.4.13-MariaDB'), {})
                    self.assertEqual(server_checks._wsrep(server_, raw), [])
                    checks = server_checks.compute_checks(server_, raw)
                    self.assertNotIn('wsrep', [c['key'] for c in checks['checks']])

    def test_wsrep_unreadable_activation_is_not_assumed_disabled(self):
        for variables in ({'wsrep_on': None}, {'wsrep_provider': None}, {'wsrep_on': 'ON'}):
            with self.subTest(variables=variables):
                check = server_checks._wsrep(Server(dict(variables, version='11.4.13-MariaDB'), {}),
                                            {'status': {}, 'status_error': 'Access denied'})[0]
                self.assertEqual(check['status'], 'unknown')

    def test_inventory_gate_and_readonly_ps_off(self):
        result, conn, _ = run_report('mariadb-11.4-default', require_choice_above=1)
        for fid in ('checks.myisam_cache', 'checks.aria_cache'):
            self.assertEqual(finding(result, fid)['status'], 'unknown')
            self.assertIn('skipped', finding(result, fid)['verdict'])
        self.assertFalse(any(server_checks.PS_MEMORY_SQL in sql for sql, _ in conn.calls))
        self.assertIn('Performance Schema is off', finding(result, 'checks.memory_budget')['verdict'])


class TestUnknownVersionSupportText(unittest.TestCase):
    """Live review: GA MariaDB 12.3 and 13.0 and MySQL 26.7 have no proved
    maintenance date, so Version support is unknown. The text report showed
    it in a WATCH section line beside the titles to watch ("1 to watch, 1
    unknown: Slow query log, Version support"); it must read as not judged."""

    def report_for(self, version, name):
        def support(server, as_of=None):
            fake = Server(dict(server.variables, version=version), {})
            return ORIGINAL_VERSION_SUPPORT(fake, datetime.date(2026, 10, 15))
        clock = FakeTime()
        conn, data = replay('report', name, clock=clock)
        conn.password = PASSWORD
        with mock.patch.object(server_checks, '_version_support', side_effect=support):
            return ORIGINAL_ANALYSIS(conn, data['args']['interval'], workers=1, sleep=clock.sleep,
                                     clock=clock.monotonic)

    def test_unknown_version_support_is_never_watch_in_text(self):
        for version, name in (('12.3.1-MariaDB', 'mariadb-11.4'), ('13.0.2-MariaDB', 'mariadb-11.4'),
                              ('26.7.0', 'mysql-8.4-replica')):
            with self.subTest(version=version):
                result = self.report_for(version, name)
                check = finding(result, 'checks.version_support')
                self.assertEqual(check['status'], 'unknown')
                self.assertIn('support unknown (unverified)', check['verdict'])
                section = next(s for s in result['sections'] if s['key'] == 'observability')
                # Each title sits under its own status, unknown after watch.
                self.assertIn('1 unknown: Version support', section['summary'])
                watch = re.search(r'\d+ to watch: ([^;]*)', section['summary'])
                if watch:
                    self.assertNotIn('Version support', watch.group(1))
                text = report.format_text(result)
                lines = text.splitlines()
                self.assertIn('  ?  Version support', lines)
                not_judged = text.split('NOT JUDGED (unknown)\n', 1)[1].split('\n\n', 1)[0]
                self.assertIn('Version support', not_judged)
                findings = text.split('FINDINGS (problem and watch, ranked)\n', 1)
                if len(findings) == 2:
                    self.assertNotIn('Version support', findings[1].split('\n\n', 1)[0])
                self.assertFalse([l for l in lines if 'WATCH' in l and re.search(
                    r'to watch: [^;]*Version support', l)])
                self.assertIn('[unknown] checks.version_support', report.format_digest(result))


if __name__ == '__main__':
    unittest.main()
