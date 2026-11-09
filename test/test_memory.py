"""Memory accounting with explicitly synthetic, source-grounded engine fixtures.

No live capture is claimed: the coordinator records and validates these reads
in the lab. Arithmetic expectations below are hand-calculated independently.
"""
import copy
import inspect
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from myflames import dbops, memory, ui
from myflames.connector import ConnectorError
from myflames.perf_common import Server

FIXTURES = Path(__file__).parent / 'fixtures' / 'memory'
MIB = 1024 * 1024


class Clock(object):
    def __init__(self):
        self.value = 1000.0
    def monotonic(self):
        return self.value
    def sleep(self, seconds):
        self.value += seconds


def batch(rows):
    def cell(v):
        return 'NULL' if v is None else str(v).replace('\\', '\\\\').replace('\n', '\\n').replace('\t', '\\t')
    return ''.join('\t'.join(cell(v) for v in r) + '\n' for r in rows)


class FixtureConnection(object):
    def __init__(self, fixture, denied=()):
        self.data = copy.deepcopy(fixture)
        self.calls = []
        self.counts = {}
        self.denied = set(denied)

    def run(self, sql, extra_flags=None, timeout=None):
        assert sql.startswith(memory.TAG), sql
        statement = sql[len(memory.TAG):]
        self.calls.append((statement, extra_flags, timeout))
        n = self.counts.get(statement, 0)
        self.counts[statement] = n + 1
        if statement in self.denied:
            raise ConnectorError("ERROR 1142: SELECT command denied")
        d = self.data
        if statement.startswith('SHOW GLOBAL VARIABLES'):
            return batch(sorted(d['variables'].items()))
        if statement.startswith('SHOW GLOBAL STATUS') and statement != memory.STATUS_SQL:
            return batch([['Uptime', '1000']])
        if statement == memory.STATUS_SQL:
            return batch([['Uptime', str(d.get('uptimes', [1000, 1005])[min(n, 1)])],
                          ['Memory_used', d['status_memory'][min(n, 1)]]])
        if statement == memory.COVERAGE_SQL:
            if 'instruments' in d:
                return batch(d.get('instruments_after', d['instruments']) if n else d['instruments'])
            enabled, total = d.get('coverage_after', d['coverage']) if n else d['coverage']
            return batch([['memory/fixture/%04d' % i, 'YES' if i < enabled else 'NO'] for i in range(total)])
        if statement == memory.CONSUMER_SQL:
            return batch(d.get('consumers_after', d['consumers']) if n else d['consumers'])
        if statement == memory.THREAD_SQL:
            return batch([d['threads']])
        if statement == memory.TOTAL_SQL:
            return batch([d['totals'][min(n, 1)]])
        if statement == memory.CATEGORY_SQL:
            return batch(d['categories'][min(n, 1)])
        for query, key in ((memory.EVENT_SQL, 'events'), (memory.SESSION_SQL, 'sessions'),
                           (memory.ACCOUNT_SQL, 'accounts'), (memory.FALLBACK_SQL, 'fallback_sessions')):
            if statement == query:
                rows = d[key]
                if key in ('sessions', 'accounts'):
                    width = 7 if key == 'sessions' else 5
                    rows = [r + [1] if len(r) < width else r for r in rows]
                return batch(rows)
        raise AssertionError('Unexpected SQL: ' + statement)


def fixture(name):
    return json.loads((FIXTURES / (name + '.json')).read_text())


def collect(name='mysql-8.4', denied=(), edit=None):
    conn = FixtureConnection(fixture(name), denied)
    if edit:
        edit(conn.data)
    clock = Clock()
    report = memory.memory_report(conn, sleep=clock.sleep, clock=clock.monotonic)
    return report, conn


class MemoryTests(unittest.TestCase):
    def test_both_engines_and_versions_retain_accounting_sources(self):
        for name in ('mysql-8.4', 'mysql-9.7', 'mysql-5.7', 'mariadb-11.8-on', 'mariadb-11.8-off'):
            with self.subTest(name=name):
                report, conn = collect(name)
                self.assertEqual(report['schema_version'], 'memory-1.0')
                self.assertTrue(report['available'])
                self.assertEqual(report['interval'], 5)
                json.dumps(report, allow_nan=False)
                for sql, flags, timeout in conn.calls:
                    self.assertTrue(sql.startswith(('SELECT ', 'SHOW GLOBAL ')), sql)
                    self.assertNotIn('sys.', sql)
                    self.assertEqual(dbops.classify(sql)['kind'], 'read')
                    self.assertEqual(flags, ['--comments'])
                    self.assertGreater(timeout, 0)
                    self.assertLessEqual(timeout, 5)
                    if sql.startswith('SELECT '):
                        self.assertIn('LIMIT ', sql)
                # 2 preflight, 2 + 2 coverage and consumers, 2 x 3 window
                # samples, events, sessions, accounts and one threads count.
                self.assertLessEqual(len(conn.calls), 16)

    def test_hand_calculated_stable_total_share_highs_and_zero_growth(self):
        r, _ = collect()
        self.assertEqual(r['current_bytes'], 1408 * MIB)
        self.assertEqual(r['high_bytes'], 2200 * MIB)
        self.assertEqual(r['growth_bytes_per_s'], 0)
        self.assertEqual(r['buffer_pool_share_pct'], 72.73)
        self.assertEqual(r['status'], 'ok')
        self.assertEqual(r['categories'][0]['key'], 'buffer_pool')
        self.assertIn('their sum is not a server peak', ' '.join(r['notes']))

    def test_version_independent_queries_and_percona_identity(self):
        for version, comment, engine, flavor in (
                ('8.0.44', 'MySQL Community Server', 'mysql', 'mysql'),
                ('26.7.0', 'MySQL Community Server', 'mysql', 'mysql'),
                ('8.4.6-6', 'Percona Server (GPL)', 'mysql', 'percona'),
                ('10.11.9-MariaDB', 'MariaDB Server', 'mariadb', 'mariadb')):
            def edit(d):
                d['variables'].update(version=version, version_comment=comment)
            with self.subTest(version=version):
                r, _ = collect(edit=edit)
                self.assertEqual((r['engine'], r['flavor']), (engine, flavor))
                self.assertEqual(r['current_bytes'], 1408 * MIB)

    def test_watch_rules_are_explicit_and_do_not_claim_a_leak(self):
        r, _ = collect('mysql-9.7')
        self.assertEqual(r['growth_bytes_per_s'], 572 * MIB / 5)
        self.assertEqual(r['status'], 'watch')
        self.assertEqual([f['key'] for f in r['findings']], ['growth', 'category:sort', 'session:42'])
        self.assertTrue(all(f['rule'].startswith('Rule of thumb: ') for f in r['findings']))
        self.assertIn('does not prove a leak', r['findings'][0]['reason'])

    def test_growth_watch_requires_both_strict_size_thresholds(self):
        for name in ('mysql-8.4', 'mariadb-11.8-on'):
            for start, delta, watch in ((100 * MIB, 16 * MIB, False),
                                        (4000 * MIB, 20 * MIB, False),
                                        (4000 * MIB, 40 * MIB, False),
                                        (4000 * MIB, 41 * MIB, True),
                                        (100 * MIB, 17 * MIB, True)):
                def edit(d):
                    d['totals'] = [[start, start, 4], [start + delta, start + delta, 4]]
                with self.subTest(engine=name, start=start, delta=delta):
                    r, _ = collect(name, edit=edit)
                    self.assertEqual('growth' in [f['key'] for f in r['findings']], watch)
                    if watch:
                        self.assertIn('1%', r['findings'][0]['rule'])
                        self.assertIn('16 MiB', r['findings'][0]['rule'])

    def test_ps_share_is_context_and_negative_categories_never_inflate_shares(self):
        for name in ('mysql-8.4', 'mariadb-11.8-on'):
            def edit(d):
                d['totals'] = [[999, 2000, 3]] * 2
                d['categories'] = [[['performance_schema', 1000, 1500], ['other', -1, 500]]] * 2
            r, _ = collect(name, edit=edit)
            # MariaDB adds Network as not collected (it never instruments NET::buff).
            expected = [100, 0] + ([None] if name.startswith('mariadb') else [])
            self.assertEqual([c['share_pct'] for c in r['categories']], expected)
            self.assertEqual([c['key'] for c in r['categories']][2:], ['network'] if name.startswith('mariadb') else [])
            self.assertEqual(r['status'], 'ok')
            self.assertEqual(r['findings'], [])

    def test_slow_category_reads_use_each_measurements_own_clock(self):
        for name in ('mysql-9.7', 'mariadb-11.8-on'):
            conn = FixtureConnection(fixture(name))
            clock = Clock()
            original = conn.run
            def run(sql, **kwargs):
                # The first category read costs 4 s; the second total costs 4 s.
                # Total completions: 1000, 1009. Categories: 1004, 1009.
                statement = sql[len(memory.TAG):]
                n = conn.counts.get(statement, 0)
                if (statement == memory.CATEGORY_SQL and n == 0 or
                        statement == memory.TOTAL_SQL and n == 1):
                    clock.value += 4
                return original(sql, **kwargs)
            conn.run = run
            conn.data['totals'] = [[1408 * MIB, 2200 * MIB, 4], [1980 * MIB, 3000 * MIB, 4]]
            conn.data['categories'][1][1][1] = conn.data['categories'][0][1][1] + 100 * MIB
            r = memory.memory_report(conn, sleep=clock.sleep, clock=clock.monotonic)
            self.assertEqual(r['elapsed_s'], 9)
            self.assertEqual(r['growth_bytes_per_s'], round(572 * MIB / 9, 2))
            sort = next(c for c in r['categories'] if c['key'] == 'sort')
            self.assertEqual(sort['growth_bytes_per_s'], 20 * MIB)
            self.assertEqual(sort['elapsed_s'], 5)

    def test_total_timestamp_precedes_unrelated_category_read(self):
        conn = FixtureConnection(fixture('mysql-9.7'))
        clock = Clock()
        original = conn.run
        def run(sql, **kwargs):
            if sql == memory.TAG + memory.CATEGORY_SQL:
                clock.value += 4
            return original(sql, **kwargs)
        conn.run = run
        snapshot = memory._snapshot(memory.MemoryReader(conn), 'performance_schema', [], clock.monotonic)
        self.assertEqual(snapshot['t'], 1000)
        self.assertTrue(all(c['t'] == 1004 for c in snapshot['categories']))

    def test_coverage_cap_is_unknown_and_reordered_names_are_comparable(self):
        r, _ = collect(edit=lambda d: d.update(coverage=[8193, 8193]))
        self.assertIsNone(r['coverage'])
        self.assertEqual(r['status'], 'not_judged')
        def edit(d):
            d['instruments'] = [['memory/sql/a', 'YES'], ['memory/sql/b', 'NO']]
            d['instruments_after'] = list(reversed(d['instruments']))
        r, _ = collect(edit=edit)
        self.assertEqual(r['window_status'], 'ok')

    def test_equal_count_instrument_swap_invalidates_window(self):
        for name in ('mysql-8.4', 'mariadb-11.8-on'):
            def edit(d):
                d['instruments'] = [['memory/sql/a', 'YES'], ['memory/sql/b', 'NO']]
                d['instruments_after'] = [['memory/sql/a', 'NO'], ['memory/sql/b', 'YES']]
            r, _ = collect(name, edit=edit)
            self.assertEqual(r['coverage'], {'enabled': 1, 'total': 2})
            self.assertEqual(r['window_status'], 'coverage_changed')
            self.assertIsNone(r['growth_bytes_per_s'])
            self.assertTrue(all(c['growth_bytes_per_s'] is None for c in r['categories']))

    def test_disabled_attribution_zero_is_uncollected_but_measured_zero_is_kept(self):
        for name in ('mysql-8.4', 'mariadb-11.8-on'):
            for enabled, expected in ((0, None), (1, 0)):
                def edit(d):
                    d['coverage'] = [70, 270]
                    d['sessions'] = [[52, 42, 'app', 'localhost', 0, 0, enabled]]
                    d['accounts'] = [['app', 'localhost', 0, 0, enabled]]
                r, _ = collect(name, edit=edit)
                for row in (r['sessions'][0], r['accounts'][0]):
                    self.assertEqual(row['current_bytes'], expected)
                    self.assertEqual(row['high_bytes'], expected)
                    if not enabled:
                        self.assertIn('instruments are disabled', row['collection_reason'])
                        self.assertIn('Server > Instruments', row['collection_reason'])
                self.assertEqual(r['status'], 'not_judged')

    def test_disabled_attribution_preserves_nonzero_history_not_unmeasured_zero(self):
        def edit(d):
            d['sessions'] = [[52, 42, 'app', 'localhost', 0, 100, 0]]
            d['accounts'] = [['app', 'localhost', 0, 100, 0]]
        r, _ = collect(edit=edit)
        for row in (r['sessions'][0], r['accounts'][0]):
            self.assertIsNone(row['current_bytes'])
            self.assertEqual(row['high_bytes'], 100)
            self.assertIn('instruments are disabled', row['collection_reason'])

    def test_mysql57_mostly_off_preserves_builtin_memory_but_never_says_ok(self):
        r, _ = collect('mysql-5.7')
        self.assertEqual(r['current_bytes'], 128 * MIB)
        self.assertEqual(r['status'], 'not_judged')
        self.assertEqual(r['coverage'], {'enabled': 79, 'total': 400})
        self.assertIn('Server > Instruments', r['gate'])
        self.assertFalse(any(f['key'].startswith('category:') for f in r['findings']))

    def test_mariadb_off_uses_status_and_processlist_without_any_ps_read(self):
        r, conn = collect('mariadb-11.8-off')
        self.assertEqual(r['current_bytes'], 1550 * MIB)
        self.assertEqual(r['growth_bytes_per_s'], 10 * MIB)
        self.assertEqual(r['source'], 'mariadb_status')
        self.assertEqual(r['sessions'][0]['current_bytes'], 80 * MIB)
        self.assertEqual(r['sessions'][0]['high_bytes'], 90 * MIB)
        self.assertIsNone(r['sessions'][0]['thread_id'])
        self.assertIsNone(r['high_bytes'])
        self.assertIsNone(r['buffer_pool_share_pct'])
        self.assertFalse(r['collection']['accounts'])
        self.assertTrue(all('FROM performance_schema' not in sql for sql, _, _ in conn.calls))
        self.assertIn('without PROCESS', ' '.join(r['notes']))

    def test_partial_privileges_keep_totals_and_mark_uncollected_sections(self):
        r, _ = collect(denied=(memory.SESSION_SQL, memory.ACCOUNT_SQL))
        self.assertEqual(r['current_bytes'], 1408 * MIB)
        self.assertFalse(r['collection']['sessions'])
        self.assertFalse(r['collection']['accounts'])
        self.assertEqual(r['status'], 'not_judged')
        self.assertIn('1142', ' '.join(r['notes']))

    def test_all_ps_denied_is_notes_and_null_not_a_zero_total(self):
        r, _ = collect(denied=(memory.TOTAL_SQL, memory.COVERAGE_SQL, memory.CONSUMER_SQL,
                              memory.EVENT_SQL, memory.SESSION_SQL, memory.ACCOUNT_SQL))
        self.assertFalse(r['available'])
        self.assertIsNone(r['current_bytes'])
        self.assertIsNone(r['growth_bytes_per_s'])
        self.assertEqual(r['status'], 'not_judged')

    def test_mariadb_missing_ps_tables_falls_back_without_mixing_accounting(self):
        r, _ = collect('mariadb-11.8-on', denied=(memory.TOTAL_SQL,))
        self.assertEqual(r['source'], 'mariadb_status')
        self.assertEqual(r['growth_bytes_per_s'], 0)  # Both fallback samples see 1550 MiB.
        self.assertEqual(r['current_bytes'], 1550 * MIB)
        self.assertEqual(r['sessions'][0]['source'], memory.PROCESS_TABLE)
        self.assertEqual(r['accounts'], [])

    def test_restart_and_collection_change_drop_growth(self):
        for edit, status in ((lambda d: d.update(uptimes=[1000, 2]), 'restarted'),
                             (lambda d: d.update(coverage_after=[399, 400]), 'coverage_changed'),
                             (lambda d: d.update(consumers_after=[['global_instrumentation', 'NO']]), 'coverage_changed')):
            with self.subTest(status=status):
                r, _ = collect('mysql-9.7', edit=edit)
                self.assertEqual(r['window_status'], status)
                self.assertIsNone(r['growth_bytes_per_s'])
                self.assertTrue(all(c['growth_bytes_per_s'] is None for c in r['categories']))

    def test_deallocation_is_negative_growth_not_counter_reset(self):
        def edit(d):
            d['totals'].reverse()
            d['categories'].reverse()
        r, _ = collect('mysql-9.7', edit=edit)
        self.assertEqual(r['growth_bytes_per_s'], -572 * MIB / 5)
        self.assertEqual(r['window_status'], 'ok')
        self.assertNotIn('growth', [f['key'] for f in r['findings']])

    def test_thresholds_are_strict_and_zero_denominator_is_unknown(self):
        def edit(d):
            d['totals'] = [[400, 900, 4]] * 2
            d['categories'] = [[['buffer_pool', 300, 600], ['sort', 100, 300]]] * 2
            d['sessions'][0][4] = 1 << 30
        r, _ = collect(edit=edit)
        self.assertEqual(r['status'], 'ok')
        self.assertEqual(r['findings'], [])
        r, _ = collect(edit=lambda d: d.update(totals=[[0, 0, 1]] * 2, categories=[[['other', 0, 0]]] * 2))
        self.assertIsNone(r['categories'][0]['share_pct'])
        self.assertEqual(r['current_bytes'], 0)

    def test_unknown_coverage_and_disabled_consumer_never_judge_ok(self):
        r, _ = collect(denied=(memory.COVERAGE_SQL,))
        self.assertEqual(r['status'], 'not_judged')
        r, _ = collect(edit=lambda d: d.update(consumers=[['global_instrumentation', 'NO'], ['thread_instrumentation', 'NO']]))
        self.assertEqual(r['status'], 'not_judged')
        self.assertIn('Thread instrumentation is off', ' '.join(r['notes']))

    def test_unknown_prefix_is_other_and_temp_mmap_does_not_claim_ram(self):
        for event, expected in (('memory/innodb/buf_buf_pool', 'buffer_pool'),
                                ('memory/innodb/not_buffer_pool', 'other'),
                                ('memory/temptable/physical_disk', 'temporary'),
                                ('memory/sql/Filesort_info::merge', 'sort'),
                                ('memory/sql/JOIN_CACHE', 'join'),
                                ('memory/sql/NET::buff', 'network'),
                                ('memory/performance_schema/accounts', 'performance_schema'),
                                ('memory/vendor/a', 'other')):
            self.assertEqual(memory.category(event), expected)

    # Join memory per engine (instrument names from each release's memory key
    # list): JOIN_CACHE fills on MySQL 5.7 and MariaDB only; hash joins and BKA
    # allocate hash_join on 8.0.18 to 8.1 (8.0.44 sql/psi_memory_key.cc:373) and
    # hashed_operation from 8.2 (8.4.6 :376, 9.7 :377), live on MySQL 9.7.2.
    JOIN_EVENTS = ('memory/sql/JOIN_CACHE', 'memory/sql/hash_join', 'memory/sql/hashed_operation')
    # Real names that share a prefix and must not be joins.
    NOT_JOIN_EVENTS = ('memory/sql/hash_index_key_buffer', 'memory/sql/hash_filo::lock', 'memory/sql/NET::compress_packet',
                       'memory/sql/Filesort_buffer::sort_keys', 'memory/sql/thd::main_mem_root')

    def test_join_category_holds_join_and_hash_memory_of_every_engine(self):
        """Before the fix, MySQL 8.0.20+ hash join memory landed in Other and
        "Join buffers" stayed empty."""
        for event in self.JOIN_EVENTS:
            self.assertEqual(memory.category(event), 'join', event)
        for event in self.NOT_JOIN_EVENTS:
            self.assertNotEqual(memory.category(event), 'join', event)
        self.assertEqual(memory.LABELS['join'], 'Join and hash buffers')

    def test_category_sql_groups_every_event_like_the_python_projection(self):
        """The server groups categories with CATEGORY_SQL's CASE and the event
        list uses category(); a prefix added to one but not the other would
        put the same bytes in two categories. Evaluated in SQLite with LEFT()
        defined as MySQL's leftmost-characters function."""
        import contextlib
        import sqlite3
        names = self.JOIN_EVENTS + self.NOT_JOIN_EVENTS + (
            'memory/innodb/buf_buf_pool', 'memory/temptable/physical_ram', 'memory/sql/Filesort_info::record_pointers',
            'memory/sql/NET::buff', 'memory/performance_schema/threads', 'memory/vendor/a', 'memory/sql/JOIN_CACHE_x')
        case = memory._CATEGORY_CASE
        with contextlib.closing(sqlite3.connect(':memory:')) as db:
            db.create_function('LEFT', 2, lambda text, n: text[:n])
            for name in names:
                (sql_category,) = db.execute('SELECT ' + case + ' FROM (SELECT ? AS EVENT_NAME)', (name,)).fetchone()
                self.assertEqual(sql_category, memory.category(name), name)
        self.assertEqual(len(memory.CATEGORIES), 7)
        self.assertIn('LIMIT 7', memory.CATEGORY_SQL)

    def test_hash_join_bytes_are_judged_as_join_memory(self):
        """A 9.7-shaped sample: hashed_operation rows are a join category that
        the 25% workload rule can judge, for MySQL and MariaDB alike."""
        for name in ('mysql-9.7', 'mariadb-11.8-on'):
            def edit(d):
                d['totals'] = [[1408 * MIB, 2200 * MIB, 4]] * 2
                d['categories'] = [[['buffer_pool', 600 * MIB, 600 * MIB], ['join', 500 * MIB, 700 * MIB],
                                    ['other', 300 * MIB, 300 * MIB]]] * 2
                d['events'] = [['memory/sql/hashed_operation', 500 * MIB, 700 * MIB]]
            with self.subTest(server=name):
                r, _ = collect(name, edit=edit)
                self.assertEqual(r['events'][0]['category'], 'join')
                join = next(c for c in r['categories'] if c['key'] == 'join')
                self.assertEqual(join['label'], 'Join and hash buffers')
                self.assertEqual(join['share_pct'], 35.71)  # 500 of 1,400 MiB
                rule = next(f for f in r['findings'] if f['key'] == 'category:join')
                self.assertIn('join and hash buffers', rule['rule'])
                self.assertTrue(rule['reason'].startswith('Join and hash buffers exceeds'))

    def test_mariadb_says_which_categories_it_cannot_fill(self):
        """MariaDB registers neither NET::buff nor Filesort_buffer::sort_keys
        (sql/mysqld.cc:10206,10226 on 13.0; 9688/9710 on 10.11.9, 9778/9800 on
        11.4.8, 9916/9936 on 11.8.3; absent from setup_instruments on 11.8.9):
        Network is shown as not collected and Sort carries an engine note."""
        r, _ = collect('mariadb-11.8-on')
        by_key = {c['key']: c for c in r['categories']}
        self.assertIn('Filesort_buffer::sort_keys', by_key['sort']['engine_note'])
        network = by_key['network']
        self.assertEqual((network['current_bytes'], network['high_bytes'], network['share_pct'],
                          network['growth_bytes_per_s']), (None, None, None, None))
        self.assertIn('NET::buff', network['collection_reason'])
        self.assertEqual(r['categories'][-1]['key'], 'network', 'an uncollected row sorts last')
        # The placeholder changes no measured share and no verdict.
        self.assertEqual(by_key['buffer_pool']['share_pct'], 72.73)
        self.assertEqual(r['status'], 'ok')
        json.dumps(r, allow_nan=False)
        for name in ('mysql-8.4', 'mysql-5.7'):
            with self.subTest(server=name):
                r, _ = collect(name)
                self.assertNotIn('network', [c['key'] for c in r['categories']])
                self.assertFalse(any('engine_note' in c for c in r['categories']))
        r, _ = collect('mariadb-11.8-off')
        self.assertEqual(r['categories'], [], 'Memory_used has no categories to annotate')

    def test_event_highs_are_upper_bounds_not_recorded_peaks(self):
        """HIGH = CURRENT + summed alloc headroom (MySQL pfs_stat.cc:440 over
        memory_full_aggregate :893-910; MariaDB table_helper.cc:780 and
        pfs_stat.h:870), so a note may never call it a peak."""
        for name in ('mysql-8.4', 'mariadb-11.8-on'):
            r, _ = collect(name)
            text = ' '.join(r['notes'])
            self.assertIn('Each event high is an upper bound', text)
            self.assertIn('not a recorded peak', text)
            self.assertNotIn('independent high-water marks', text)
        self.assertIn('upper', memory.__doc__)
        self.assertNotIn('independent peaks', memory.__doc__)

    def test_cross_thread_free_follows_engine_and_version(self):
        """pfs_memory_free_vc charges a free by a non-owner thread to the
        global aggregate from MySQL 8.0.23 (pfs.cc:8756-8800 on 9.7; absent in
        8.0.22); 5.7 and MariaDB (pfs.cc:6728-6755) charge the freeing thread."""
        for version, comment, expected in (('5.7.44', 'MySQL Community Server', 'freeing_thread'),
                                           ('8.0.22', 'MySQL Community Server', 'freeing_thread'),
                                           ('8.0.23', 'MySQL Community Server', 'global'),
                                           ('8.4.6-6', 'Percona Server (GPL)', 'global'),
                                           ('9.7.2', 'MySQL Community Server', 'global'),
                                           ('11.8.3-MariaDB', 'MariaDB Server', 'freeing_thread'),
                                           ('', 'MySQL Community Server', None)):
            with self.subTest(version=version):
                server = Server({'version': version, 'version_comment': comment}, {})
                self.assertEqual(memory.free_charged_to(server), expected)
        notes = lambda name: ' '.join(collect(name)[0]['notes'])
        self.assertIn('MySQL 8.0.23 and later charges a free made by another thread to the global total', notes('mysql-9.7'))
        for name in ('mysql-5.7', 'mariadb-11.8-on'):
            self.assertIn('the one that freed it can drop below 0', notes(name))
        self.assertNotIn('charges a free', notes('mariadb-11.8-off'), 'process-list memory is not Performance Schema')

    def test_session_rule_states_the_cross_thread_free_caveat(self):
        r, _ = collect('mysql-9.7')
        session = next(f for f in r['findings'] if f['key'] == 'session:42')
        self.assertIn('1 GiB', session['rule'])
        self.assertIn('memory another thread freed can still count here', session['rule'])

    def test_negative_session_bytes_stay_numbers_and_never_trip_a_rule(self):
        """A session that freed another thread's memory reads below 0 on 5.7
        and MariaDB; the value is kept (the UI says why) and judged as no growth."""
        for name in ('mysql-5.7', 'mariadb-11.8-on'):
            def edit(d):
                d['sessions'] = [[52, 42, 'app', 'localhost', -5 * MIB, 0, 1]]
            r, _ = collect(name, edit=edit)
            self.assertEqual(r['sessions'][0]['current_bytes'], -5 * MIB)
            self.assertFalse(any(f['key'].startswith('session:') for f in r['findings']))

    def test_uninstrumented_foreground_threads_make_coverage_partial_or_unjudged(self):
        """While thread_instrumentation is on, an INSTRUMENTED = NO thread's
        allocations reach no memory table (MySQL pfs.cc:8441-8450, MariaDB
        pfs.cc:6526-6538): any is a partial note, over 10% is not judged."""
        for name in ('mysql-8.4', 'mariadb-11.8-on'):
            for threads, partial, gated in (([0, 4], False, False), ([1, 10], True, False),
                                            ([2, 10], True, True), ([1, 9], True, True)):
                with self.subTest(server=name, threads=threads):
                    r, conn = collect(name, edit=lambda d: d.update(threads=threads))
                    self.assertEqual(r['thread_coverage'], {'uninstrumented': threads[0], 'foreground': threads[1]})
                    text = ' '.join(r['notes'])
                    expected = '%d of %d foreground threads are not instrumented' % tuple(threads)
                    if gated:
                        self.assertIn(expected, r['gate'])
                        self.assertEqual(r['gate_row'], 'actors')
                        self.assertEqual(r['status'], 'not_judged')
                        self.assertNotIn(expected, text, 'the gate says it once')
                    else:
                        self.assertIsNone(r['gate'])
                        self.assertEqual(r['status'], 'ok')
                        self.assertEqual(expected in text, partial)
                        if partial:
                            self.assertIn('totals, sessions and accounts are partial', text)
                    self.assertEqual(sum(sql == memory.THREAD_SQL for sql, _, _ in conn.calls), 1)

    def test_thread_count_is_read_only_while_thread_instrumentation_is_on(self):
        """With thread_instrumentation off, uninstrumented threads still reach
        the global table, so the count would mean nothing and is not read."""
        r, conn = collect(edit=lambda d: d.update(consumers=[['global_instrumentation', 'YES'], ['thread_instrumentation', 'NO']],
                                                  threads=[3, 4]))
        self.assertNotIn(memory.THREAD_SQL, [sql for sql, _, _ in conn.calls])
        self.assertIsNone(r['thread_coverage'])
        self.assertNotIn('foreground threads are not instrumented', ' '.join(r['notes']))
        r, conn = collect('mariadb-11.8-off')
        self.assertNotIn(memory.THREAD_SQL, [sql for sql, _, _ in conn.calls])

    def test_denied_thread_count_is_a_note_not_an_error(self):
        r, _ = collect(denied=(memory.THREAD_SQL,))
        self.assertIsNone(r['thread_coverage'])
        self.assertIn('Could not read performance_schema.threads', ' '.join(r['notes']))
        self.assertEqual(r['current_bytes'], 1408 * MIB)

    def test_gate_names_the_instruments_row_that_fixes_it(self):
        self.assertIsNone(collect()[0]['gate_row'])
        self.assertEqual(collect('mysql-5.7')[0]['gate_row'], 'group:memory')
        self.assertEqual(collect('mariadb-11.8-off')[0]['gate_row'], 'ps')

    def test_mariadb_memory_used_note_says_what_it_counts(self):
        """Memory_used moves only through my_malloc/my_realloc/my_free
        (mysys/my_malloc.c:112,178,209); InnoDB allocates with malloc
        (ut0new.h:375). The buffer pool counts only while it came from
        my_large_malloc after my_largepage.c started calling
        update_malloc_size (10.11.6, 11.4.0 ...) and before
        my_virtual_mem_reserve (10.11.12, 11.4.6, 11.8.2)."""
        for version, expected in (((10, 11, 5), False), ((10, 11, 6), True), ((10, 11, 11), True), ((10, 11, 12), False),
                                  ((11, 4, 0), True), ((11, 4, 5), True), ((11, 4, 6), False), ((11, 8, 0), True),
                                  ((11, 8, 1), True), ((11, 8, 2), False), ((11, 8, 9), False), ((12, 0, 1), False),
                                  ((13, 0, 1), False), ((0, 0, 0), None)):
            self.assertEqual(memory.pool_in_memory_used(version), expected, version)
        for version, phrase in (('11.8.3-MariaDB', 'the buffer pool included, is missing'),
                                ('10.11.9-MariaDB', 'but this release does count the buffer pool'),
                                ('11.4.8-MariaDB', 'the buffer pool included, is missing')):
            with self.subTest(version=version):
                r, _ = collect('mariadb-11.8-off', edit=lambda d: d['variables'].update(version=version))
                text = ' '.join(r['notes'])
                self.assertIn("MariaDB Memory_used counts only allocations made through the server's own allocator (my_malloc)", text)
                self.assertIn(phrase, text)
                self.assertNotIn('allocator accounting', text)

    def test_64bit_current_bytes_and_untrusted_text_survive_without_sql_interpolation(self):
        attack = "<img src=x onerror=alert(1)>\u202e'\\n\\! secret"
        def edit(d):
            d['accounts'][0][0] = attack
            d['accounts'][0][1] = ''
            d['sessions'][0][2] = None
            d['totals'] = [[2 ** 60 + 1, 2 ** 61, 4]] * 2
        r, conn = collect(edit=edit)
        self.assertEqual(r['current_bytes'], 2 ** 60 + 1)
        self.assertEqual(r['accounts'][0]['user'], attack)
        self.assertEqual(r['accounts'][0]['host'], '')
        self.assertIsNone(r['sessions'][0]['user'])
        self.assertTrue(all(attack not in sql for sql, _, _ in conn.calls))

    def test_invalid_intervals_do_not_read_or_interpolate_sql(self):
        for interval in (True, False, 1, 61, 5.5, '5; DROP TABLE x', None):
            conn = FixtureConnection(fixture('mysql-8.4'))
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                memory.memory_report(conn, interval)
            self.assertEqual(conn.calls, [])

    def test_optional_errors_never_leak_the_connection_password(self):
        conn = FixtureConnection(fixture('mysql-8.4'))
        conn.password = 'fixture-secret'
        original = conn.run
        def run(sql, **kwargs):
            if sql == memory.TAG + memory.ACCOUNT_SQL:
                raise ConnectorError('ERROR 1142: SELECT denied fixture-secret')
            return original(sql, **kwargs)
        conn.run = run
        clock = Clock()
        r = memory.memory_report(conn, sleep=clock.sleep, clock=clock.monotonic)
        self.assertNotIn('fixture-secret', json.dumps(r))
        self.assertIn('[redacted]', ' '.join(r['notes']))

    def test_top_lists_are_bounded_even_if_an_adapter_returns_extra_rows(self):
        def edit(d):
            d['events'] = d['events'] * 10
            d['sessions'] = d['sessions'] * 20
            d['accounts'] = d['accounts'] * 20
        r, _ = collect(edit=edit)
        self.assertEqual(len(r['events']), 15)
        self.assertEqual(len(r['sessions']), 10)
        self.assertEqual(len(r['accounts']), 10)

    def test_empty_aggregate_is_missing_not_zero_and_sparse_samples_do_not_invent_rates(self):
        r, _ = collect(edit=lambda d: d.update(totals=[[None, None, 0]] * 2))
        self.assertIsNone(r['current_bytes'])
        self.assertIsNone(r['growth_bytes_per_s'])
        r, _ = collect(edit=lambda d: d.update(categories=[[], [['sort', 128 * MIB, 300 * MIB]]]))
        self.assertIsNone(r['categories'][0]['growth_bytes_per_s'])

    def test_query_budget_refuses_more_work(self):
        reader = memory.MemoryReader(mock.Mock())
        reader.remaining = 0
        with self.assertRaisesRegex(ConnectorError, '30 s read budget'):
            reader.rows(memory.TOTAL_SQL)
        reader.conn.run.assert_not_called()

    def test_default_mysql_ps_off_has_no_fabricated_fallback(self):
        r, conn = collect(edit=lambda d: d['variables'].update(performance_schema='OFF'))
        self.assertFalse(r['available'])
        self.assertIsNone(r['source'])
        self.assertEqual(len(conn.calls), 2)


class MemoryAPITests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory(dir=str(Path(__file__).resolve().parents[1]))
        self.env = mock.patch.dict(os.environ, {'HOME': self.home.name, 'USERPROFILE': self.home.name,
                                               'MYFLAMES_SECRET_BACKEND': 'vault'})
        self.env.start()
        self.old_passphrase = os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)

    def tearDown(self):
        self.env.stop()
        self.home.cleanup()

    def test_route_saved_password_timeout_default_and_allowlists(self):
        class Store(object):
            def connection_with_saved_password(self, connection):
                return dict(connection, password='fixture-secret')
        workspace = mock.Mock(store=Store())
        conn = FixtureConnection(fixture('mysql-8.4'))
        clock = Clock()
        with mock.patch('myflames.connector.MySQLConnection') as factory, mock.patch('myflames.perf_common.time', clock):
            factory.return_value.__enter__.return_value = conn
            result = ui.api_response('/api/live/memory', {'connection': {'host': 'localhost', 'profile_id': 'p'}}, workspace)
        self.assertEqual(result['interval'], 5)
        self.assertEqual(factory.call_args[1]['password'], 'fixture-secret')
        self.assertEqual(factory.call_args[1]['query_timeout'], 5)
        self.assertIn('/api/live/memory', ui._SERVER_VIEW_PATHS)
        self.assertIn('/api/live/memory', ui._CONNECTION_PATHS)
        self.assertIn('"/api/live/memory"', inspect.getsource(ui.WorkspaceHandler.do_POST))
        self.assertNotIn('fixture-secret', json.dumps(result))

    def test_route_redacts_connection_failures(self):
        with mock.patch('myflames.connector.MySQLConnection') as factory:
            factory.return_value.__enter__.return_value.run.side_effect = ConnectorError('ERROR 1045: bad fixture-secret')
            with self.assertRaises(ConnectorError) as caught:
                ui.api_response('/api/live/memory', {'connection': {'host': 'localhost', 'password': 'fixture-secret'}}, object())
        self.assertNotIn('fixture-secret', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
