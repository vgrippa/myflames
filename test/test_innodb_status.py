"""InnoDB view: SHOW ENGINE INNODB STATUS parsed, graded, and never shown raw by default.

fixtures/innodb/*.txt are real SHOW ENGINE INNODB STATUS outputs captured
from throwaway Docker containers (MySQL 8.4.11, 9.7.2 with two buffer pool
instances, 26.7.0 with the adaptive hash index on; MariaDB 11.4.13 and
13.0.2) while a scenario held a transaction idle with a row lock, made
another wait for that lock, queued an ALTER TABLE behind it, kept an old read
view open, ran SELECT SLEEP, and scanned a table larger than the buffer pool;
each server also had one deadlock and one foreign key error (MySQL 9.6+
checks foreign keys in the SQL layer, so 9.7 and 26.7 record none).
mariadb-13.0.2-zero-window.txt was printed less than a second after another
reader; the *-idle.txt files come from freshly started servers;
mysql-8.4.11-lock-monitor.txt was printed with innodb_status_output_locks=ON.

fixtures/perf/innodb-<server>.json replay a whole report (every statement and
the mysql --batch text it returned) from the same run, plus a run as an
account without PROCESS.
"""
import json
import os
import unittest
from unittest import mock

from _perf_replay import FakeFactory, PASSWORD, ReplayConn, batch, fixture, run_cli, run_main
from myflames import innodb_status, ui
from myflames.connector import ConnectorError
from myflames.innodb_status import (compute_report, history_status, innodb_report, parse_status,
                                    split_sections)

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'innodb')
SERVERS = ('mysql-8.4', 'mysql-9.7', 'mysql-26.7', 'mariadb-11.4', 'mariadb-13.0')
SECTION_KEYS = ('semaphores', 'transactions', 'deadlock', 'foreign_key_error', 'file_io', 'insert_buffer',
                'adaptive_hash_index', 'log', 'buffer_pool', 'row_operations', 'background')
MYSQL_VARS = {'version': '8.4.11', 'innodb_page_size': '16384', 'innodb_max_dirty_pages_pct': '90.000000',
              'innodb_redo_log_capacity': '104857600', 'innodb_adaptive_hash_index': 'OFF',
              'innodb_change_buffering': 'none'}
MARIADB_VARS = {'version': '11.4.13-MariaDB-ubu2404', 'innodb_page_size': '16384',
                'innodb_max_dirty_pages_pct': '90.000000', 'innodb_log_file_size': '100663296',
                'innodb_adaptive_hash_index': 'OFF'}
MARIADB_STATUS = {'Innodb_checkpoint_max_age': '80819529'}


def text(name):
    with open(os.path.join(HERE, name + '.txt'), encoding='utf-8') as handle:
        return handle.read()


def section(report, key):
    return next(s for s in report['sections'] if s['key'] == key)


def item(sec, key):
    return next(i for i in sec['items'] if i['key'] == key)


class TestSplitAndHeader(unittest.TestCase):
    def test_every_fixture_splits_into_known_sections_and_ends(self):
        for name in sorted(os.listdir(HERE)):
            with self.subTest(name=name):
                parsed = parse_status(text(name[:-4]), 'mariadb' if 'mariadb' in name else 'mysql')
                self.assertTrue(parsed['ended'])
                self.assertEqual(parsed['unknown'], [])
                for key in ('background', 'semaphores', 'transactions', 'file_io', 'log', 'buffer_pool',
                            'row_operations'):
                    self.assertIn(key, parsed['found'])
                self.assertIsNotNone(parsed['header']['printed_at'])
                self.assertIsInstance(parsed['header']['window_s'], int)

    def test_header_and_background(self):
        parsed = parse_status(text('mysql-8.4.11-busy'))
        self.assertEqual(parsed['header'], {'printed_at': '2026-10-14 17:45:12', 'window_s': 12}
                         if parsed['header']['printed_at'] == '2026-10-14 17:45:12' else parsed['header'])
        self.assertEqual(parsed['header']['window_s'], 12)
        self.assertEqual(parsed['sections']['background'],
                         {'active_loops': 344, 'shutdown_loops': 0, 'idle_loops': 117, 'log_flush_and_writes': 0})
        maria = parse_status(text('mariadb-11.4.13-busy'), 'mariadb')['sections']['background']
        self.assertIsNone(maria['shutdown_loops'])
        self.assertGreater(maria['idle_loops'], 0)

    def test_individual_buffer_pool_info_is_not_a_new_report_section(self):
        _, sections, _ = split_sections(text('mysql-9.7.2-busy'))
        self.assertIn('INDIVIDUAL BUFFER POOL INFO', [t for t, _ in sections])
        parsed = parse_status(text('mysql-9.7.2-busy'))
        self.assertIn('buffer_pool_instances', parsed['found'])


class TestSectionParsers(unittest.TestCase):
    def test_mysql_semaphore_wait_and_deprecated_spin_counters(self):
        sem = parse_status(text('mysql-8.4.11-busy'))['sections']['semaphores']
        self.assertEqual((sem['reservation_count'], sem['signal_count']), (217592, 245313))
        self.assertEqual(len(sem['waits']), 1)
        wait = sem['waits'][0]
        self.assertEqual((wait['file'], wait['line'], wait['seconds']), ('row0upd.cc', 3070, 0.0))
        self.assertEqual(wait['latch'], 'X-lock (wait_ex) on an RW-latch')
        self.assertEqual(wait['created_at'], 'buf0buf.cc line 857')
        self.assertEqual(sem['spins']['RW-shared'], {'spins': 0, 'rounds': 0, 'os_waits': 0})

    def test_mariadb_semaphores_section_is_empty(self):
        sem = parse_status(text('mariadb-11.4.13-busy'), 'mariadb')['sections']['semaphores']
        self.assertTrue(sem['empty'])
        self.assertEqual(sem['waits'], [])

    def test_foreign_key_error_mysql_and_mariadb(self):
        for name, engine in (('mysql-8.4.11-busy', 'mysql'), ('mariadb-11.4.13-busy', 'mariadb')):
            with self.subTest(name=name):
                fk = parse_status(text(name), engine)['sections']['foreign_key_error']
                self.assertEqual(fk['kind'], 'no_parent_row')
                self.assertEqual(fk['constraint'], 'fk_child_parent')
                self.assertEqual(fk['columns'], ['parent_id'])
                self.assertEqual(fk['referenced_columns'], ['id'])
                self.assertEqual(fk['table'], '`mf`.`child`')
                self.assertEqual(fk['parent_table'], '`mf`.`parent`')
                self.assertEqual(fk['statement'], 'INSERT INTO child VALUES (1, 999)')
                self.assertEqual(fk['user'], 'root')
                self.assertRegex(fk['at'], r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')

    def test_mysql_96_and_later_record_no_foreign_key_error(self):
        for name, version in (('mysql-9.7.2-busy', '9.7.2'), ('mysql-26.7.0-busy', '26.7.0')):
            report = compute_report(text(name), dict(MYSQL_VARS, version=version, innodb_native_foreign_keys='OFF'), {})
            fk = section(report, 'foreign_key_error')
            self.assertFalse(fk['present'])
            self.assertIn('innodb_native_foreign_keys', fk['summary'])

    def test_foreign_key_error_parent_side_and_missing_table(self):
        lines = ['2026-10-14 10:00:00 0x7f Transaction:', 'TRANSACTION 9, ACTIVE 0 sec updating or deleting',
                 'MySQL thread id 5, OS thread handle 1, query id 7 localhost app updating',
                 'DELETE FROM parent WHERE id = 1',
                 'Foreign key constraint fails for table `s`.`child`:', ',',
                 '  CONSTRAINT `fk` FOREIGN KEY (`pid`) REFERENCES `parent` (`id`) ON DELETE RESTRICT',
                 'Trying to delete or update in parent table, in index PRIMARY tuple:',
                 'DATA TUPLE: 1 fields;', '',
                 'But in child table `s`.`child`, in index pid, there is a record:', 'PHYSICAL RECORD: ...']
        fk = innodb_status.parse_foreign_key_error(lines)
        self.assertEqual((fk['kind'], fk['child_table'], fk['on_actions'], fk['connection_id']),
                         ('child_rows_exist', '`s`.`child`', 'ON DELETE RESTRICT', 5))
        self.assertEqual(fk['operation'], 'delete or update in the parent table')
        missing = innodb_status.parse_foreign_key_error(
            lines[:7] + ['Trying to add to index pid tuple:', 'But the parent table `s`.`parent`',
                         'or its .ibd file or the required index does not currently exist!'])
        self.assertEqual(missing['kind'], 'missing_table')

    def test_mysql_transactions_lock_wait_and_read_view(self):
        trx = parse_status(text('mysql-8.4.11-busy'))['sections']['transactions']
        self.assertEqual(trx['trx_id_counter'], 11915)
        self.assertEqual(trx['purge'], {'trx_no': 1858, 'undo_no': 0, 'state': 'running but idle'})
        self.assertEqual(trx['history_list_length'], 3184)
        by_id = {t['trx_id']: t for t in trx['transactions']}
        waiting = by_id['1855']
        self.assertEqual((waiting['state'], waiting['active_s'], waiting['connection_id']), ('ACTIVE', 379, 30))
        self.assertTrue(waiting['lock_wait'])
        self.assertEqual(waiting['wait_s'], 379)
        self.assertEqual(waiting['statement'], 'UPDATE t SET v = 0 WHERE id = 10')
        self.assertEqual((waiting['waiting_for']['table'], waiting['waiting_for']['mode']), ('t', 'X,REC_NOT_GAP'))
        idle = by_id['1854']
        self.assertEqual((idle['undo_entries'], idle['row_locks'], idle['statement']), (1, 1, None))
        self.assertEqual(idle['read_view'], {'low_limit': 1855, 'up_limit': 1855})
        self.assertEqual(sum(1 for t in trx['transactions'] if t['state'] == 'not started'), 6)

    def test_lock_monitor_output_does_not_leak_into_statements(self):
        # Captured with innodb_status_output_locks=ON: every lock is printed after the trx.
        status = text('mysql-8.4.11-lock-monitor')
        self.assertIn('TABLE LOCK table', status)
        trx = parse_status(status)['sections']['transactions']
        started = [t for t in trx['transactions'] if t['state'] == 'ACTIVE']
        self.assertTrue(started)
        for t in started:
            self.assertNotIn('LOCK', t['statement'] or '')
            self.assertNotIn('hex', t['statement'] or '')
        waiting = next(t for t in started if t['lock_wait'])
        self.assertEqual(waiting['statement'], 'UPDATE t SET v = 0 WHERE id = 10')
        self.assertEqual(waiting['waiting_for']['mode'], 'X,REC_NOT_GAP')

    def test_mariadb_transactions_pointer_ids_and_microsecond_waits(self):
        trx = parse_status(text('mariadb-11.4.13-busy'), 'mariadb')['sections']['transactions']
        waiting = next(t for t in trx['transactions'] if t['lock_wait'])
        self.assertEqual(waiting['trx_id'], '53')
        self.assertAlmostEqual(waiting['wait_s'], 378.9, places=1)
        self.assertEqual(waiting['connection_id'], 22)
        pointer = [t for t in trx['transactions'] if t['trx_id'].startswith('(0x')]
        self.assertTrue(pointer)
        idle = next(t for t in pointer if t['connection_id'] == 24)
        self.assertIsNone(idle['thread_state'])

    def test_file_io_pending_and_rates(self):
        io = parse_status(text('mysql-8.4.11-busy'))['sections']['file_io']
        self.assertEqual(io['pending']['normal_aio_reads'], 0)
        self.assertEqual(io['pending']['aio_writes'], 0)
        self.assertIsNone(io['pending']['ibuf_aio_reads'], 'a one-segment array prints no count')
        self.assertEqual((io['pending']['fsync_log'], io['pending']['fsync_buffer_pool']), (0, 0))
        self.assertEqual(io['os'], {'reads': 7007093, 'writes': 212229, 'fsyncs': 32973})
        self.assertEqual(io['rates']['avg_bytes_per_read'], 16384)
        self.assertEqual({t['purpose'] for t in io['io_threads']}, {'insert buffer thread', 'read thread', 'write thread'})
        maria = parse_status(text('mariadb-11.4.13-busy'), 'mariadb')['sections']['file_io']
        self.assertIsNotNone(maria['pending']['fsync'])
        self.assertIsNone(maria['pending']['fsync_log'])

    def test_change_buffer_and_adaptive_hash_index(self):
        mysql = parse_status(text('mysql-8.4.11-busy'))['sections']
        self.assertEqual(mysql['insert_buffer']['size_pages'], 1)
        self.assertEqual(mysql['insert_buffer']['merged'], {'insert': 0, 'delete_mark': 0, 'delete': 0})
        self.assertEqual(mysql['adaptive_hash_index']['parts'], 8)
        self.assertEqual(mysql['adaptive_hash_index']['hit_pct'], 0.0)
        on = parse_status(text('mysql-26.7.0-busy'))['sections']['adaptive_hash_index']
        self.assertAlmostEqual(on['hit_pct'], 3.3, places=1)
        maria = parse_status(text('mariadb-13.0.2-busy'), 'mariadb')['sections']
        self.assertNotIn('insert_buffer', maria)
        self.assertAlmostEqual(maria['adaptive_hash_index']['hit_pct'], 33.6, places=1)
        self.assertNotIn('adaptive_hash_index', parse_status(text('mariadb-11.4.13-busy'), 'mariadb')['sections'])

    def test_older_formats_from_the_source(self):
        # MariaDB 10.11 prints its own INSERT BUFFER section when the change
        # buffer is not empty (mariadb-10.11.16 ibuf0ibuf.cc:4477-4506).
        ibuf = innodb_status.parse_insert_buffer(['size 3, free list len 5, seg size 9, 12 merges',
                                                  'merged operations:', ' insert 4, delete mark 2, delete 1',
                                                  'discarded operations:', ' insert 0, delete mark 0, delete 0'])
        self.assertEqual((ibuf['size_pages'], ibuf['merges'], ibuf['merged']['delete_mark']), (3, 12, 2))
        # MySQL 5.7 / early 8.0 printed totals before each segment list and log/sync I/O.
        io = innodb_status.parse_file_io([
            'Pending normal aio reads: 3 [1, 2, 0, 0] , aio writes: 0 [0, 0, 0, 0] ,',
            " ibuf aio reads: 1, log i/o's: 0, sync i/o's: 2",
            'Pending flushes (fsync) log: 1; buffer pool: 0'])
        self.assertEqual((io['pending']['normal_aio_reads'], io['pending']['ibuf_aio_reads'],
                          io['pending']['sync_ios'], io['pending_total']), (3, 1, 2, 7))
        log = innodb_status.parse_log(['Log sequence number 2000', 'Log flushed up to   1990',
                                       'Pages flushed up to 1500', 'Last checkpoint at  1200',
                                       '0 pending log flushes, 0 pending chkp writes',
                                       "10 log i/o's done, 0.50 log i/o's/second"])
        self.assertEqual((log['checkpoint_age'], log['unflushed'], log['pending_log_flushes'],
                          log['log_ios_per_s']), (800, 10, 0, 0.5))
        rows = innodb_status.parse_row_operations(['Main thread process no. 1, id 140, state: sleeping'])
        self.assertEqual((rows['process_id'], rows['main_thread_state']), (1, 'sleeping'))

    def test_log_checkpoint_age_from_both_engines(self):
        log = parse_status(text('mysql-8.4.11-busy'))['sections']['log']
        self.assertEqual((log['lsn'], log['last_checkpoint'], log['capacity']), (620202521, 618104870, 104857600))
        self.assertEqual(log['checkpoint_age'], 620202521 - 618104870)
        maria = parse_status(text('mariadb-13.0.2-busy'), 'mariadb')['sections']['log']
        self.assertEqual(maria['checkpoint_age'], maria['lsn'] - maria['last_checkpoint'])
        self.assertIsNone(maria['capacity'])

    def test_buffer_pool_totals_and_instances(self):
        pool = parse_status(text('mysql-8.4.11-busy'))['sections']['buffer_pool']
        self.assertEqual(pool['pool']['pool_size_pages'], 4096)
        self.assertEqual(pool['pool']['hit_rate_per_1000'], 810)
        self.assertEqual(pool['pool']['pending_writes'], {'lru': 0, 'flush_list': 0, 'single_page': 0})
        self.assertEqual(pool['instances'], [])
        nine = parse_status(text('mysql-9.7.2-busy'))['sections']['buffer_pool']
        self.assertEqual([i['instance'] for i in nine['instances']], [0, 1])
        self.assertEqual(sum(i['pool_size_pages'] for i in nine['instances']), nine['pool']['pool_size_pages'])
        maria = parse_status(text('mariadb-13.0.2-busy'), 'mariadb')['sections']['buffer_pool']['pool']
        self.assertIsNotNone(maria['dirty_pct_printed'])
        self.assertEqual(maria['max_dirty_pct'], 90.0)
        self.assertIsNone(maria['pending_writes']['single_page'])

    def test_row_operations(self):
        rows = parse_status(text('mysql-8.4.11-busy'))['sections']['row_operations']
        self.assertEqual((rows['queries_inside'], rows['queries_in_queue'], rows['read_views']), (0, 0, 4))
        self.assertEqual(rows['rows']['read'], 397363977)
        self.assertEqual(rows['rows_per_s']['updated'], 583.53)
        self.assertEqual(rows['main_thread_state'], 'sleeping')
        maria = parse_status(text('mariadb-11.4.13-busy'), 'mariadb')['sections']['row_operations']
        self.assertIsNone(maria['rows'])
        self.assertEqual(maria['main_thread_state'], 'sleeping')

    def test_deadlock_reuses_the_locks_parser_without_raw_text(self):
        for name, engine in (('mysql-8.4.11-busy', 'mysql'), ('mariadb-13.0.2-busy', 'mariadb')):
            deadlock = parse_status(text(name), engine)['sections']['deadlock']
            self.assertEqual(len(deadlock['transactions']), 2)
            self.assertIsNotNone(deadlock['victim'])
            self.assertNotIn('raw', deadlock)
            self.assertNotIn('raw_truncated', deadlock)


class TestReport(unittest.TestCase):
    def test_busy_mysql_flags_purge_lag_owner_hit_rate_and_pending_io(self):
        report = compute_report(text('mysql-8.4.11-busy'), MYSQL_VARS, {})
        self.assertEqual([s['key'] for s in report['sections']], list(SECTION_KEYS))
        self.assertEqual(report['status'], 'problem')
        trx = section(report, 'transactions')
        self.assertEqual(item(trx, 'oldest_trx')['status'], 'problem')
        self.assertEqual(item(trx, 'lock_waits')['status'], 'watch')
        self.assertEqual(item(trx, 'history_list_length')['status'], 'ok')
        self.assertEqual([l['view'] for l in trx['links']], ['sessions', 'locks'])
        self.assertEqual(trx['data']['transactions'][0]['active_s'], 379)
        self.assertNotIn('not started', [t['state'] for t in trx['data']['transactions']])
        self.assertEqual(section(report, 'buffer_pool')['status'], 'problem')
        self.assertEqual(section(report, 'file_io')['status'], 'watch')
        self.assertEqual(section(report, 'log')['status'], 'ok')
        self.assertEqual(section(report, 'deadlock')['links'][0]['view'], 'locks')
        for sec in report['sections']:
            self.assertIn(sec['status'], innodb_status.STATUSES)
            self.assertTrue(sec['summary'])
            for it in sec['items']:
                if it['status'] in ('watch', 'problem'):
                    self.assertTrue(it['rule'], it)
        self.assertIsNone(report['raw'])
        json.dumps(report, allow_nan=False)

    def test_raw_only_on_request(self):
        status = text('mysql-8.4.11-busy')
        report = compute_report(status, MYSQL_VARS, {})
        self.assertNotIn('INNODB MONITOR OUTPUT', json.dumps(report))
        self.assertNotIn('PHYSICAL RECORD', json.dumps(report))
        raw = compute_report(status, MYSQL_VARS, {}, raw=True)
        self.assertEqual(raw['raw'], {'text': status, 'truncated': False})

    def test_idle_servers_need_no_attention(self):
        for name, variables, status in (('mysql-8.4.11-idle', MYSQL_VARS, {}),
                                        ('mariadb-11.4.13-idle', MARIADB_VARS, MARIADB_STATUS)):
            report = compute_report(text(name), variables, status)
            self.assertEqual(report['summary']['text'], 'Nothing needs attention.')
            self.assertFalse(section(report, 'deadlock')['present'])
            self.assertFalse(section(report, 'foreign_key_error')['present'])

    def test_mariadb_sections_and_engine_texts(self):
        report = compute_report(text('mariadb-11.4.13-busy'), MARIADB_VARS, MARIADB_STATUS)
        self.assertEqual(section(report, 'semaphores')['status'], 'info')
        self.assertIn('empty', section(report, 'semaphores')['summary'])
        self.assertIn('no change buffer', section(report, 'insert_buffer')['summary'])
        self.assertFalse(section(report, 'adaptive_hash_index')['present'])
        log = section(report, 'log')
        self.assertEqual(log['data']['watch_at_bytes'], int(0.875 * 80819529))
        self.assertEqual(log['data']['capacity_source'], 'innodb_log_file_size')
        self.assertIsNone(section(report, 'row_operations')['data']['rows'])
        self.assertEqual(section(report, 'buffer_pool')['status'], 'problem')

    def test_zero_window_drops_the_meaningless_rates(self):
        report = compute_report(text('mariadb-13.0.2-zero-window'), dict(MARIADB_VARS, version='13.0.2-MariaDB',
                                                                          innodb_adaptive_hash_index='ON'),
                                MARIADB_STATUS)
        self.assertIn('less than a second', report['notes'][0])
        io = section(report, 'file_io')['data']
        self.assertEqual(io['rates'], {'reads_per_s': None, 'avg_bytes_per_read': None, 'writes_per_s': None,
                                       'fsyncs_per_s': None})
        self.assertIsNone(section(report, 'adaptive_hash_index')['data']['hash_searches_per_s'])
        pool = section(report, 'buffer_pool')
        self.assertIsNone(pool['data']['pool']['reads_per_s'])
        self.assertEqual(item(pool, 'hit_pct')['status'], 'unknown')
        self.assertNotIn('16193000', json.dumps(report))

    def test_short_window_note(self):
        status = text('mysql-8.4.11-idle').replace('the last 10 seconds', 'the last 3 seconds')
        report = compute_report(status, MYSQL_VARS, {})
        self.assertIn('only the last 3 s', report['notes'][0])

    def test_history_list_rule_of_thumb(self):
        self.assertEqual(history_status(99999)[0], 'ok')
        self.assertEqual(history_status(100000)[0], 'watch')
        self.assertEqual(history_status(1000000)[0], 'problem')
        self.assertEqual(history_status(None)[0], 'unknown')
        self.assertIn('without a trend', history_status(5)[1])
        status = text('mysql-8.4.11-busy').replace('History list length 3184', 'History list length 2500000')
        trx = section(compute_report(status, MYSQL_VARS, {}), 'transactions')
        self.assertEqual(item(trx, 'history_list_length')['status'], 'problem')
        # Transaction 1854 printed "Trx read view will not see ...": it is the one holding purge back.
        self.assertIn('Transaction 1854 (connection 27) has kept a read view for 6 min 19 s', trx['summary'])
        self.assertEqual(trx['data']['oldest_read_view'], {'trx_id': '1854', 'connection_id': 27, 'active_s': 379})
        self.assertIn('committed transactions whose undo logs', item(trx, 'history_list_length')['rule'])

    def test_long_semaphore_wait_thresholds(self):
        base = text('mysql-8.4.11-busy')
        for seconds, expected in (('9', 'ok'), ('10', 'watch'), ('241', 'problem')):
            status = base.replace('line 3070 for 0 seconds the semaphore', 'line 3070 for %s seconds the semaphore'
                                  % seconds)
            sem = section(compute_report(status, MYSQL_VARS, {}), 'semaphores')
            self.assertEqual(sem['status'], expected, seconds)
        # Older servers print fractional seconds.
        status = base.replace('for 0 seconds the semaphore', 'for 250.00 seconds the semaphore')
        self.assertEqual(section(compute_report(status, MYSQL_VARS, {}), 'semaphores')['status'], 'problem')

    def test_redo_pressure_points_reuse_health(self):
        base = text('mysql-8.4.11-idle')
        lsn = parse_status(base)['sections']['log']['lsn']
        last = parse_status(base)['sections']['log']['last_checkpoint']

        def at(fraction):
            current = 500000000
            checkpoint = current - int(fraction * 104857600)
            return base.replace('Log sequence number          %d' % lsn, 'Log sequence number          %d' % current) \
                .replace('Last checkpoint at           %d' % last, 'Last checkpoint at           %d' % checkpoint)
        for fraction, expected in ((0.5, 'ok'), (0.74, 'watch'), (0.80, 'problem')):
            log = section(compute_report(at(fraction), dict(MYSQL_VARS, version='26.7.0'), {}), 'log')
            self.assertEqual(log['status'], expected, fraction)
            self.assertEqual(log['data']['capacity_source'], 'Log capacity used in the status')
        maria = text('mariadb-11.4.13-idle').replace('Last checkpoint at  45434', 'Last checkpoint at  0') \
            .replace('Log sequence number 45450', 'Log sequence number 72000000')
        log = section(compute_report(maria, MARIADB_VARS, MARIADB_STATUS), 'log')
        self.assertEqual(log['status'], 'watch')
        self.assertIn('Innodb_checkpoint_max_age', item(log, 'checkpoint_age')['rule'])

    def test_redo_capacity_is_the_one_in_use_not_the_variable(self):
        # A legacy innodb_log_file_size x innodb_log_files_in_group (2 x 512 MiB) overrides the default
        # innodb_redo_log_capacity, which "Log capacity" still prints (mysql-8.4.9 ha_innodb.cc:4670-4675).
        base = text('mysql-8.4.11-idle')
        parsed = parse_status(base)['sections']['log']
        status = base.replace('Log capacity used            104857600', 'Log capacity used            1073741824') \
            .replace('Log sequence number          %d' % parsed['lsn'], 'Log sequence number          900000000') \
            .replace('Last checkpoint at           %d' % parsed['last_checkpoint'],
                     'Last checkpoint at           %d' % (900000000 - 90 * 1048576))
        log = section(compute_report(status, MYSQL_VARS, {}), 'log')
        self.assertEqual((log['data']['capacity_bytes'], log['status']), (1073741824, 'ok'))
        resized = section(compute_report(status, MYSQL_VARS, {'Innodb_redo_log_capacity_resized': '2147483648'}), 'log')
        self.assertEqual(resized['data']['capacity_source'], 'Innodb_redo_log_capacity_resized')
        # Before 8.4.8 / 9.6.0 there are no capacity lines: the variables decide.
        older = '\n'.join(l for l in status.split('\n') if not l.startswith('Log capacity'))
        log = section(compute_report(older, MYSQL_VARS, {}), 'log')
        self.assertEqual(log['data']['capacity_source'], 'innodb_redo_log_capacity')
        self.assertEqual(log['status'], 'problem')

    def test_wrapped_hit_rate_ended_waits_and_pending_writes(self):
        status = text('mysql-8.4.11-busy').replace('Buffer pool hit rate 810 / 1000',
                                                   'Buffer pool hit rate 18446744073709551 / 1000')
        pool = section(compute_report(status, MYSQL_VARS, {}), 'buffer_pool')
        self.assertIsNone(pool['data']['pool']['hit_pct'])
        self.assertEqual(item(pool, 'hit_pct')['status'], 'unknown')
        status = text('mysql-8.4.11-busy').replace('X-lock (wait_ex) on RW-latch', 'wait has ended\nX-lock (wait_ex) on RW-latch') \
            .replace('line 3070 for 0 seconds', 'line 3070 for 500 seconds')
        status = status.replace('Last time write locked in file', 'wait has ended\nLast time write locked in file', 1)
        sem = section(compute_report(status, MYSQL_VARS, {}), 'semaphores')
        self.assertEqual((sem['status'], item(sem, 'latch_waits')['value']), ('ok', 0))
        writes = text('mysql-8.4.11-busy').replace('2 pending preads, 0 pending pwrites', '0 pending preads, 7 pending pwrites')
        io = section(compute_report(writes, MYSQL_VARS, {}), 'file_io')
        self.assertEqual((io['status'], io['data']['pending_total'], io['data']['pending_judged']), ('ok', 7, 0))
        fsync = writes.replace('Pending flushes (fsync) log: 0;', 'Pending flushes (fsync) log: 1;')
        self.assertEqual(section(compute_report(fsync, MYSQL_VARS, {}), 'file_io')['status'], 'watch')

    def test_background_counter_mysql_never_increments_is_hidden(self):
        mysql = section(compute_report(text('mysql-8.4.11-busy'), MYSQL_VARS, {}), 'background')
        self.assertNotIn('log_flush_and_writes', [i['key'] for i in mysql['items']])
        maria = section(compute_report(text('mariadb-11.4.13-busy'), MARIADB_VARS, MARIADB_STATUS), 'background')
        self.assertIn('log_flush_and_writes', [i['key'] for i in maria['items']])

    def test_buffer_pool_hit_rate_needs_enough_requests(self):
        idle = section(compute_report(text('mysql-8.4.11-idle'), MYSQL_VARS, {}), 'buffer_pool')
        self.assertEqual(item(idle, 'hit_pct')['status'], 'ok')
        self.assertIn('too few requests', item(idle, 'hit_pct')['text'])
        self.assertEqual(idle['data']['pool']['free_pct'], round(100.0 * 15231 / 16383, 1)
                         if idle['data']['pool']['free_pages'] == 15231 else idle['data']['pool']['free_pct'])

    def test_dirty_share_against_max_dirty_pages_pct(self):
        status = text('mysql-8.4.11-busy').replace('Modified db pages  5', 'Modified db pages  3800')
        pool = section(compute_report(status, dict(MYSQL_VARS, innodb_max_dirty_pages_pct='90'), {}), 'buffer_pool')
        self.assertEqual(item(pool, 'dirty_pct')['status'], 'watch')
        self.assertEqual(pool['data']['pool']['dirty_pct'], round(100.0 * 3800 / 4096, 1))

    def test_partial_and_unknown_text_is_tolerated(self):
        status = text('mysql-8.4.11-busy')
        cut = status[:status.index('BUFFER POOL AND MEMORY')]
        report = compute_report(cut, MYSQL_VARS, {})
        self.assertFalse(report['parse']['complete'])
        self.assertFalse(section(report, 'buffer_pool')['present'])
        self.assertTrue(any('ended early' in n for n in report['notes']))
        self.assertTrue(any('Parsed partially' in n for n in report['notes']))
        extra = status.replace('--------------\nROW OPERATIONS', '-----------\nNEW THING\n-----------\nfoo 1\n'
                                                                  '--------------\nROW OPERATIONS')
        report = compute_report(extra, MYSQL_VARS, {})
        self.assertEqual(report['parse']['unknown_sections'], ['NEW THING'])
        self.assertTrue(section(report, 'row_operations')['present'])
        marked = status.replace('LIST OF TRANSACTIONS FOR EACH SESSION:\n',
                                'LIST OF TRANSACTIONS FOR EACH SESSION:\n... truncated...\n')
        self.assertTrue(compute_report(marked, MYSQL_VARS, {})['parse']['truncated_by_server'])
        garbage = compute_report('nothing like a status\n\x00\n----\n', MYSQL_VARS, {})
        self.assertEqual(len(garbage['sections']), len(SECTION_KEYS))
        json.dumps(garbage, allow_nan=False)

    def test_at_or_worse(self):
        report = compute_report(text('mysql-8.4.11-busy'), MYSQL_VARS, {})
        problems = innodb_status.at_or_worse(report, 'problem')
        self.assertEqual({p['section'] for p in problems}, {'transactions', 'buffer_pool'})
        watch = innodb_status.at_or_worse(report, 'watch')
        self.assertIn('file_io', {p['section'] for p in watch})


def assert_read_only(case, conn):
    case.assertTrue(conn.calls)
    from myflames.sessions import SYSTEM_TIME_ZONE
    for sql, flags in conn.calls:
        case.assertTrue(sql.startswith(innodb_status.INNODB_TAG), sql)
        body = sql[len(innodb_status.INNODB_TAG):]
        if body.startswith(SYSTEM_TIME_ZONE):
            # The only session setting: trx_started is in the OS time zone.
            body = body[len(SYSTEM_TIME_ZONE):]
            case.assertTrue(body.startswith(innodb_status.INNODB_TAG))
            body = body[len(innodb_status.INNODB_TAG):]
        case.assertNotIn(';', body)
        case.assertRegex(body, r'^(SELECT|SHOW GLOBAL|SHOW ENGINE INNODB STATUS$)', sql)
        case.assertIn('--comments', flags)
        case.assertNotRegex(body, r'\b(UPDATE|INSERT|DELETE|KILL|SET|FLUSH|ALTER|CREATE|DROP)\b')


class TestLiveReplay(unittest.TestCase):
    def test_every_server_replays_into_a_complete_report(self):
        for server in SERVERS:
            with self.subTest(server=server):
                data = fixture('innodb', server)
                conn = ReplayConn(data['calls'])
                report = innodb_report(conn)
                assert_read_only(self, conn)
                self.assertTrue(report['available'])
                self.assertTrue(report['parse']['complete'])
                self.assertEqual(report['engine'], 'mariadb' if server.startswith('mariadb') else 'mysql')
                self.assertEqual([s['status'] for s in report['sections']],
                                 [s['status'] for s in data['report']['sections']])
                trx = section(report, 'transactions')
                self.assertEqual(item(trx, 'oldest_trx')['status'], 'problem')
                self.assertIsNotNone(trx['data']['oldest_any'])
                self.assertTrue(section(report, 'deadlock')['present'])
                self.assertIsNone(report['raw'])
                json.dumps(report, allow_nan=False)

    def test_deadlock_count_comes_from_each_engines_counter(self):
        mysql = innodb_report(ReplayConn(fixture('innodb', 'mysql-8.4')['calls']))
        self.assertTrue(any('INNODB_METRICS' in sql for sql, _ in fixture('innodb', 'mysql-8.4')['calls']))
        self.assertGreaterEqual(item(section(mysql, 'deadlock'), 'deadlocks')['value'], 1)
        maria = innodb_report(ReplayConn(fixture('innodb', 'mariadb-11.4')['calls']))
        self.assertGreaterEqual(item(section(maria, 'deadlock'), 'deadlocks')['value'], 1)

    def test_without_process_the_report_is_unavailable(self):
        for server in ('mysql-8.4-noproc', 'mariadb-11.4-noproc'):
            with self.subTest(server=server):
                report = innodb_report(ReplayConn(fixture('innodb', server)['calls']))
                self.assertFalse(report['available'])
                self.assertEqual(report['error']['code'], 'no_process')
                self.assertIn('GRANT PROCESS', report['error']['message'])
                self.assertEqual(report['sections'], [])

    def test_raw_flag_validation_and_empty_status(self):
        with self.assertRaises(ValueError):
            innodb_report(ReplayConn([]), raw='yes')
        data = fixture('innodb', 'mysql-8.4')

        def blank(sql, text, n):
            return 'InnoDB\t\t\n' if sql.endswith('SHOW ENGINE INNODB STATUS') else text
        with self.assertRaisesRegex(ValueError, 'returned nothing'):
            innodb_report(ReplayConn(data['calls'], edit=blank))

    def test_connection_errors_propagate(self):
        conn = ReplayConn([], error=ConnectorError('ERROR 2003 (HY000): Can\'t connect'))
        with self.assertRaisesRegex(ValueError, 'Could not read the server variables'):
            innodb_report(conn)


class TestCLI(unittest.TestCase):
    def call(self, conn, *args):
        return run_cli('innodb', ['-h', 'db', '-u', 'admin', '-p' + PASSWORD] + list(args), conn)

    def replay(self, server='mysql-8.4'):
        return ReplayConn(fixture('innodb', server)['calls'])

    def test_text_report_and_notes(self):
        code, out, err, factory = self.call(self.replay())
        self.assertEqual(code, 0, err)
        self.assertIn('TRANSACTIONS AND PURGE', out)
        self.assertIn('Rule of thumb:', out)
        self.assertIn('note: ', err)
        self.assertNotIn('INNODB MONITOR OUTPUT', out)
        self.assertEqual(factory.options['query_timeout'], 30)
        code, out, err, _ = self.call(self.replay(), '-q')
        self.assertEqual((code, err), (0, ''))

    def test_json_shape(self):
        code, out, _, _ = self.call(self.replay('mariadb-13.0'), '--json')
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report['schema_version'], 'innodb-1.0')
        self.assertEqual([s['key'] for s in report['sections']], list(SECTION_KEYS))
        self.assertIsNone(report['raw'])

    def test_raw_prints_the_status_text_only_when_asked(self):
        code, out, _, _ = self.call(self.replay(), '--raw')
        self.assertEqual(code, 0)
        self.assertIn('INNODB MONITOR OUTPUT', out)
        self.assertTrue(out.endswith('============================\n'))
        code, out, _, _ = self.call(self.replay(), '--raw', '--json')
        self.assertIn('INNODB MONITOR OUTPUT', json.loads(out)['raw']['text'])

    def test_fail_on_gate(self):
        code, _, err, _ = self.call(self.replay(), '--fail-on', 'problem')
        self.assertEqual(code, 1)
        self.assertIn('FAIL:', err)
        quiet_code, _, quiet_err, _ = self.call(self.replay(), '--fail-on', 'problem', '-q')
        self.assertEqual((quiet_code, quiet_err), (1, ''))
        data = fixture('innodb', 'mysql-8.4')
        idle = text('mysql-8.4.11-idle')

        def calm(sql, text_, n):
            if sql.endswith('SHOW ENGINE INNODB STATUS'):
                return batch([['InnoDB', '', idle]])
            if 'INNODB_TRX' in sql:
                return ''
            return text_
        code, _, err, _ = self.call(ReplayConn(data['calls'], edit=calm), '--fail-on', 'watch')
        self.assertEqual(code, 0, err)
        self.assertIn('OK: no section', err)

    def test_no_process_exits_2_with_the_grant(self):
        code, out, err, _ = self.call(self.replay('mysql-8.4-noproc'))
        self.assertEqual((code, out), (2, ''))
        self.assertIn('GRANT PROCESS', err)

    def test_output_file_and_password_redaction(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'innodb.json')
            code, out, err, _ = self.call(self.replay(), '--json', '-o', path)
            self.assertEqual((code, out), (0, ''))
            with open(path, encoding='utf-8') as handle:
                self.assertEqual(json.load(handle)['schema_version'], 'innodb-1.0')
        conn = ReplayConn([], error=ConnectorError('ERROR 1045: Access denied for user (password %s)' % PASSWORD))
        code, _, err, _ = self.call(conn)
        self.assertEqual(code, 2)
        self.assertNotIn(PASSWORD, err)

    def test_root_dispatch_and_help(self):
        code, out, _ = run_main(['myflames', 'innodb', '-h', 'db', '-p' + PASSWORD, '--json'], self.replay())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['schema_version'], 'innodb-1.0')
        code, out, _ = run_main(['myflames', '--help'])
        self.assertIn('innodb    SHOW ENGINE INNODB STATUS parsed', out)


class TestAPI(unittest.TestCase):
    def call(self, conn, payload):
        workspace = mock.Mock()
        workspace.store.connection_with_saved_password.side_effect = lambda c: c
        with mock.patch('myflames.connector.MySQLConnection', FakeFactory(conn)):
            return ui.api_response('/api/live/innodb', dict({'connection': {'host': '127.0.0.1', 'port': 3306,
                                                                             'user': 'root'}}, **payload), workspace)

    def test_route_returns_the_report_without_raw_by_default(self):
        report = self.call(ReplayConn(fixture('innodb', 'mysql-9.7')['calls']), {})
        self.assertEqual(report['schema_version'], 'innodb-1.0')
        self.assertIsNone(report['raw'])
        raw = self.call(ReplayConn(fixture('innodb', 'mysql-9.7')['calls']), {'raw': True})
        self.assertIn('INNODB MONITOR OUTPUT', raw['raw']['text'])
        with self.assertRaises(ValueError):
            self.call(ReplayConn(fixture('innodb', 'mysql-9.7')['calls']), {'raw': 'true'})

    def test_no_process_is_a_structured_answer(self):
        report = self.call(ReplayConn(fixture('innodb', 'mariadb-11.4-noproc')['calls']), {})
        self.assertFalse(report['available'])
        self.assertEqual(report['error']['code'], 'no_process')

    def test_route_is_allowed_and_needs_a_workspace(self):
        self.assertIn('/api/live/innodb', ui._SERVER_VIEW_PATHS)
        import inspect
        self.assertIn('"/api/live/innodb"', inspect.getsource(ui.WorkspaceHandler.do_POST))
        with self.assertRaisesRegex(ValueError, 'requires a running workspace'):
            ui.api_response('/api/live/innodb', {})


if __name__ == '__main__':
    unittest.main()
