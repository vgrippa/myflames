"""Processlist blocking graph and bounded per-session history contracts."""
import json
import os
import unittest
from unittest import mock

from _perf_replay import batch
from test_sessions import synthetic, processlist_row, trx_row
import test_locks as lock_fixtures
from myflames import locks, sessions, ui
from myflames.connector import ConnectorError
from myflames.perf_common import Reader, Server


def session(cid, wait=None):
    return {'id': cid, 'lock_wait': {'kind': 'innodb', 'wait_s': wait} if wait is not None else None,
            'active': False, 'is_locked': False}


class TestBlockingGraph(unittest.TestCase):
    def test_chain_unique_direct_counts_and_transitive_wait(self):
        rows = [session(1), session(2, 4), session(3, 12)]
        roots = sessions._blocking_graph(rows, [(2, 1), (3, 2), (3, 2), (None, 1), (1, 1)])
        self.assertEqual(roots, [{'id': 1, 'blocks_total': 2, 'longest_wait_s': 12}])
        self.assertEqual([s['blocked_by'] for s in rows], [[], [1], [2]])
        self.assertEqual([s['blocks'] for s in rows], [1, 1, 0])
        self.assertEqual([s['blocks_total'] for s in rows], [2, 1, 0])
        self.assertEqual(rows[0]['blocks_ids'], [2])
        self.assertTrue(rows[0]['active'])
        self.assertTrue(rows[0]['is_locked'])

    def test_cycle_excludes_self_and_has_no_root(self):
        rows = [session(1, 3), session(2, 8), session(3, 1)]
        self.assertEqual(sessions._blocking_graph(rows, [(2, 1), (1, 2), (3, 2)]), [])
        self.assertEqual([s['blocks_total'] for s in rows], [2, 2, 0])
        self.assertNotIn(1, rows[0]['blocks_ids'])
        self.assertEqual(rows[0]['longest_wait_s'], 8)

    def test_long_chain_serializes_each_direct_edge_only_once(self):
        rows = [session(cid, cid if cid > 1 else None) for cid in range(1, 151)]
        sessions._blocking_graph(rows, [(cid, cid - 1) for cid in range(2, 151)])
        self.assertEqual(rows[0]['blocks_total'], 149)
        self.assertEqual(rows[0]['longest_wait_s'], 150)
        self.assertEqual(sum(len(s['blocks_ids']) for s in rows), 149)

    def test_several_roots_and_shared_waiter(self):
        rows = [session(1), session(2), session(3, 10), session(4, 20)]
        roots = sessions._blocking_graph(rows, [(3, 1), (3, 2), (4, 3)])
        self.assertEqual([r['id'] for r in roots], [1, 2])
        self.assertEqual(rows[2]['blocked_by'], [1, 2])
        self.assertEqual(rows[0]['longest_wait_s'], 20)

    def test_unresolved_waiter_never_becomes_a_false_root(self):
        rows = [session(1, 7), session(2, 10)]
        self.assertEqual(sessions._blocking_graph(rows, [(2, 1)]), [])
        self.assertIsNone(sessions._blocking_graph([session(1), session(2)], [(2, 1)])[0]['longest_wait_s'])

    def test_no_waits_adds_no_lock_queries(self):
        conn = synthetic([processlist_row(1)])
        with mock.patch.object(locks, 'session_wait_edges', side_effect=AssertionError('extra read')):
            report = sessions.sessions_report(conn)
        self.assertEqual(report['summary']['root_blockers'], [])
        self.assertEqual(report['sessions'][0]['blocks_total'], 0)
        self.assertEqual(report['blocking_edges'], [])

    def test_snapshot_reuses_mysql_edges_and_mdl_compatibility(self):
        # Read actual Locks fixtures, with their exact server row layouts.
        conn = lock_fixtures.mysql_conn()
        server = Server({'version': '8.4.11', 'performance_schema': 'ON'}, {})
        rows = [session(28, 15), session(29, 10)]
        rows[0]['lock_wait']['kind'] = rows[1]['lock_wait']['kind'] = 'metadata'
        edges, notes = locks.session_wait_edges(Reader(conn, sessions.SESSIONS_TAG), server, rows, [], True)
        self.assertIn((24, 23), edges)
        self.assertIn((27, 24), edges)
        self.assertIn((28, 26), edges)
        self.assertIn((29, 28), edges)
        sql = '\n'.join(call[0] for call in conn.calls)
        self.assertEqual(sql.count('FROM performance_schema.data_lock_waits'), 1)
        self.assertNotIn('FROM information_schema.INNODB_TRX', sql)
        self.assertNotIn('SHOW ENGINE', sql)

    def test_mariadb_and_mysql57_use_legacy_waits(self):
        for version in ('10.11.16-MariaDB', '5.7.44'):
            conn = lock_fixtures.ma_conn() if 'MariaDB' in version else lock_fixtures.mysql_conn(version=version,
                ma_waits=lock_fixtures.MA_WAITS, ma_locks=lock_fixtures.MA_LOCKS)
            server = Server({'version': version, 'performance_schema': 'OFF'}, {})
            trx = [{'id': t[0], 'connection_id': t[7]} for t in lock_fixtures.MA_TRX]
            edges, _ = locks.session_wait_edges(Reader(conn, sessions.SESSIONS_TAG), server, [], trx, True)
            self.assertIn((6, 5), edges)
            self.assertNotIn('performance_schema.data_lock_waits', '\n'.join(c[0] for c in conn.calls))

    def test_denied_edges_remain_a_readable_report_note(self):
        conn = lock_fixtures.mysql_conn(waits=ConnectorError('ERROR 1142: SELECT command denied'))
        server = Server({'version': '8.4.11', 'performance_schema': 'ON'}, {})
        edges, notes = locks.session_wait_edges(Reader(conn, sessions.SESSIONS_TAG), server, [], [], False)
        self.assertEqual(edges, [])
        self.assertTrue(any('cannot read performance_schema.data_lock_waits' in n for n in notes))

    def test_legacy_without_process_skips_wait_reads(self):
        conn = lock_fixtures.ma_conn()
        server = Server({'version': '10.11.16-MariaDB', 'performance_schema': 'OFF'}, {})
        edges, _ = locks.session_wait_edges(Reader(conn, sessions.SESSIONS_TAG), server, [], [], False)
        self.assertEqual(edges, [])
        self.assertEqual(conn.calls, [])

    def test_wait_cap_is_reported(self):
        conn = lock_fixtures.mysql_conn(waits=[lock_fixtures.MYSQL_WAITS[0]] * 1001)
        server = Server({'version': '8.4.11', 'performance_schema': 'ON'}, {})
        edges, notes = locks.session_wait_edges(Reader(conn, sessions.SESSIONS_TAG), server, [], [], True)
        self.assertEqual(len(edges), 1000)
        self.assertTrue(any('capped lock snapshot' in n for n in notes))

    def test_graph_precedes_locked_filter_and_limit(self):
        conn = synthetic([processlist_row(1, command='Sleep', info=None), processlist_row(2), processlist_row(3)],
                         trx=[trx_row(2, 4, state='LOCK WAIT', wait=4), trx_row(3, 12, state='LOCK WAIT', wait=12)])
        with mock.patch.object(locks, 'session_wait_edges', return_value=([(2, 1), (3, 2)], [])) as read:
            report = sessions.sessions_report(conn, active_only=False, locked_only=True, limit=2)
        self.assertEqual(read.call_count, 1)
        self.assertEqual([r['id'] for r in report['sessions']], [1, 2])
        self.assertEqual(report['summary']['root_blockers'][0]['blocks_total'], 2)
        self.assertEqual(report['blocking_edges'], [{'waiter': 2, 'blocker': 1},
                                                    {'waiter': 3, 'blocker': 2}])

    def test_adjacency_survives_hidden_middle_session(self):
        """Both engines retain the 1 -> 2 -> 3 path when filters hide 2."""
        for version in ('8.4.11', '10.11.16-MariaDB'):
            with self.subTest(version=version):
                conn = synthetic([processlist_row(1, seconds=60), processlist_row(2, seconds=4),
                                  processlist_row(3, seconds=22)], version=version,
                                 trx=[trx_row(2, 4, state='LOCK WAIT', wait=4),
                                      trx_row(3, 22, state='LOCK WAIT', wait=22)])
                with mock.patch.object(locks, 'session_wait_edges', return_value=(
                        [(2, 1), (3, 2), (3, 2), (None, 1), (1, 1)], [])):
                    report = sessions.sessions_report(conn, active_only=False, locked_only=True,
                                                      min_seconds=10)
                self.assertEqual([s['id'] for s in report['sessions']], [1, 3])
                self.assertEqual(report['sessions'][0]['blocks_total'], 2)
                self.assertEqual(json.loads(json.dumps(report))['blocking_edges'],
                                 [{'waiter': 2, 'blocker': 1}, {'waiter': 3, 'blocker': 2}])

    def test_adjacency_and_counts_share_the_edge_budget(self):
        conn = synthetic([processlist_row(1), processlist_row(2)],
                         trx=[trx_row(2, 4, state='LOCK WAIT', wait=4)])
        with mock.patch.object(locks, 'MAX_TREE_EDGES', 2), mock.patch.object(
                locks, 'session_wait_edges', return_value=([(2, 1), (3, 2), (4, 3)], [])):
            report = sessions.sessions_report(conn, active_only=False)
        self.assertEqual(report['blocking_edges'], [{'waiter': 2, 'blocker': 1},
                                                    {'waiter': 3, 'blocker': 2}])
        self.assertEqual(next(s for s in report['sessions'] if s['id'] == 1)['blocks_total'], 2)
        self.assertTrue(any('edge budget' in note for note in report['notes']))


class HistoryConnection(object):
    def __init__(self, engine='mysql', ps='ON', missing=None, error_at=None, thread=None):
        path = os.path.join(os.path.dirname(__file__), 'fixtures', 'perf', 'sessions-history-%s.json' % engine)
        with open(path, encoding='utf-8') as handle:
            self.fixture = json.load(handle)
        self.ps, self.missing, self.error_at, self.calls = ps, missing, error_at, []
        self.thread = [['101', 'YES', 'YES']] if thread is None else thread

    def run(self, sql, extra_flags=None, timeout=None):
        self.calls.append(sql)
        if self.error_at and self.error_at in sql:
            raise ConnectorError('ERROR 1142: SELECT command denied')
        if 'SHOW GLOBAL VARIABLES' in sql:
            return batch([['version', self.fixture['version']], ['performance_schema', self.ps]])
        if 'setup_consumers' in sql:
            return batch([[n, 'NO' if n == self.missing else 'YES']
                          for n in sessions.HISTORY_CONSUMERS + ('statement_instruments',)])
        if 'SELECT THREAD_ID' in sql:
            return batch(self.thread)
        if 'FROM performance_schema.events_statements_history' in sql:
            return batch(self.fixture['rows'])
        raise AssertionError(sql)


class TestHistory(unittest.TestCase):
    def test_both_engine_rows_and_exact_bounded_sql(self):
        for engine in ('mysql', 'mariadb'):
            conn = HistoryConnection(engine)
            report = sessions.sessions_history(conn, 42)
            self.assertTrue(report['available'])
            first, second = report['statements']
            self.assertEqual(first['duration_ms'], 1234)
            self.assertEqual((first['rows_examined'], first['rows_sent'], first['errors'], first['errno']), (5, 0, 1, 1205))
            self.assertIsNone(second['duration_ms'])
            self.assertIn('</script>', second['sql'])
            self.assertEqual(len(conn.calls), 4)
            self.assertIn('ORDER BY s.EVENT_ID DESC LIMIT 10', conn.calls[-1])
            self.assertIn('t.PROCESSLIST_ID = 42 AND t.THREAD_ID = 101', conn.calls[-1])
            self.assertIn('LEFT(s.SQL_TEXT, %d)' % locks.TEXT_CAP, conn.calls[-1])
            self.assertTrue(all(sql.startswith(sessions.SESSIONS_TAG) for sql in conn.calls))

    def test_prerequisites_do_not_read_history(self):
        for missing in sessions.HISTORY_CONSUMERS + ('statement_instruments',):
            conn = HistoryConnection(missing=missing)
            report = sessions.sessions_history(conn, 42)
            self.assertFalse(report['available'])
            self.assertIn(missing, report['reason'])
            self.assertEqual(len(conn.calls), 2)
        conn = HistoryConnection('mariadb', ps='OFF')
        self.assertEqual(sessions.sessions_history(conn, 42)['reason'], 'Performance Schema is off; recent statements are not recorded.')
        self.assertEqual(len(conn.calls), 1)

    def test_missing_thread_disabled_thread_and_empty_ring(self):
        for thread in ([], [['101', 'NO', 'YES']], [['101', 'YES', 'NO']]):
            conn = HistoryConnection(thread=thread)
            self.assertFalse(sessions.sessions_history(conn, 42)['available'])
            self.assertEqual(len(conn.calls), 3)
        conn = HistoryConnection()
        conn.fixture['rows'] = []
        report = sessions.sessions_history(conn, 42)
        self.assertTrue(report['available'])
        self.assertIn('No completed statements', report['reason'])

    def test_denials_at_every_read_are_one_sentence(self):
        for table in ('setup_consumers', 'SELECT THREAD_ID', 'FROM performance_schema.events_statements_history'):
            report = sessions.sessions_history(HistoryConnection(error_at=table), 42)
            self.assertFalse(report['available'])
            self.assertNotIn('ERROR', report['reason'])
            self.assertEqual(report['statements'], [])

    def test_invalid_id_is_rejected_before_sql_or_connection(self):
        for cid in ('42 OR 1=1', -1, 0, True, 1.2, None, 4294967296):
            conn = HistoryConnection()
            with self.assertRaises(ValueError):
                sessions.sessions_history(conn, cid)
            self.assertEqual(conn.calls, [])
            with mock.patch('myflames.connector.MySQLConnection', side_effect=AssertionError('connection opened')):
                with self.assertRaises(ValueError):
                    ui.api_response('/api/live/sessions/history', {'connection': {}, 'id': cid})

    def test_defensive_output_cap_and_nulls(self):
        conn = HistoryConnection()
        conn.fixture['rows'] = [['1', '界' * (locks.TEXT_CAP + 5), None, None, None, None, None, None]] * 11
        report = sessions.sessions_history(conn, 42)
        self.assertEqual(len(report['statements']), 10)
        self.assertEqual(len(report['statements'][0]['sql']), locks.TEXT_CAP)
        self.assertIsNone(report['statements'][0]['rows_examined'])

    def test_connection_failure_remains_an_error(self):
        conn = HistoryConnection()
        with mock.patch.object(conn, 'run', side_effect=ConnectorError('ERROR 2013: Lost connection')):
            with self.assertRaises(ConnectorError):
                sessions.sessions_history(conn, 42)


if __name__ == '__main__':
    unittest.main()
