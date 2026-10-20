"""Index health: ``myflames indexes``, the Indexes tab API, and the rules.

Replays live captures (see _perf_replay) of a schema ``mfshop`` with an
unused index (customers.k_country_created), a redundant prefix (customers.k_country
under k_country_created), a duplicate (orders.k_status_dup), a prefix index
(orders.k_note10), a foreign key backed by orders.k_customer, a table read
mostly by full scans (orders), a table never opened since the restart
(idle_table), and a name that needs filename encoding (odd$name).
"""
import inspect
import json
import unittest
from unittest import mock

from _perf_replay import (MARIADB_PS, MYSQL, PASSWORD, FakeFactory, ReplayConn, assert_read_only, batch, replay,
                          run_cli, run_main)
from myflames import indexes, ui, perf_common
from myflames.advisor import advise, find_redundant_indexes
from myflames.collectors import parse_show_create_table
from myflames.connector import ConnectorError
from myflames.perf_common import Server

TAG = '/*myflames:indexes*/ '


_recorded_replay = replay


def replay(view, name, **kwargs):
    conn, data = _recorded_replay(view, name, **kwargs)
    # Add the new lightweight scope counts from recorded index table names.
    text = next(text for sql, text, *rest in data['calls'] if 'FROM information_schema.STATISTICS' in sql)
    count = len({tuple(line.split('\t')[:2]) for line in text.split('\n') if line})
    srv = server('mariadb' if name.startswith('mariadb') else 'mysql')
    for schemas in (None, ('mfshop',)):
        sql = perf_common._table_count_sql(srv, False, perf_common.scope_sql('TABLE_SCHEMA', schemas))
        conn.answers[TAG + sql] = [batch([[count]])]
    return conn, data


def server(engine='mysql', ps='ON', userstat='OFF', uptime='86400'):
    version = '8.4.11' if engine == 'mysql' else '11.4.13-MariaDB'
    return Server({'version': version, 'performance_schema': ps, 'userstat': userstat}, {'Uptime': uptime})


def stat(table, index, seq, column, non_unique=1, sub_part=None, collation='A', itype='BTREE', visible='YES',
         expression=None, schema='shop', engine='mysql'):
    row = [schema, table, index, str(non_unique), str(seq), column, sub_part, collation, itype]
    if engine == 'mysql':
        return row + [visible, expression]
    return row + ['NO' if visible == 'YES' else 'YES']


#: The advisor test DDL (test_advisor_extensions.DDL) as information_schema.STATISTICS rows.
ADVISOR_DDL = """CREATE TABLE `orders` (
  `id` int NOT NULL,
  `user_id` int NOT NULL,
  `status` varchar(20) NOT NULL,
  `note` varchar(200) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `u_user_status` (`user_id`,`status`),
  KEY `k_user` (`user_id`),
  KEY `k_user_dup` (`user_id`),
  KEY `k_status_desc` (`status` DESC),
  KEY `k_status_created` (`status`,`id`),
  KEY `k_note10` (`note`(10)),
  KEY `k_note_status` (`note`,`status`),
  KEY `k_expr` ((lower(`status`))),
  FULLTEXT KEY `ft_note` (`note`)
) ENGINE=InnoDB"""
ADVISOR_ROWS = [
    stat('orders', 'PRIMARY', 1, 'id', 0), stat('orders', 'u_user_status', 1, 'user_id', 0),
    stat('orders', 'u_user_status', 2, 'status', 0), stat('orders', 'k_user', 1, 'user_id'),
    stat('orders', 'k_user_dup', 1, 'user_id'), stat('orders', 'k_status_desc', 1, 'status', collation='D'),
    stat('orders', 'k_status_created', 1, 'status'), stat('orders', 'k_status_created', 2, 'id'),
    stat('orders', 'k_note10', 1, 'note', sub_part='10'), stat('orders', 'k_note_status', 1, 'note'),
    stat('orders', 'k_note_status', 2, 'status'), stat('orders', 'k_expr', 1, None, expression='lower(`status`)'),
    stat('orders', 'ft_note', 1, 'note', itype='FULLTEXT', collation=None),
]


def raw(statistics, usage=None, fks=(), objects=None, actors=None, instrument=(('wait/io/table/sql/handler', 'YES'),),
        consumers=None, **extra):
    out = {'statistics': statistics, 'foreign_keys': list(fks), 'usage': usage,
           'instrument': [list(r) for r in instrument] if instrument is not None else None,
           'consumers': consumers if consumers is not None else {'global_instrumentation': True},
           'actors': actors if actors is not None else [['%', '%', '%', 'YES']],
           'objects': objects, 'index_stats': None, 'table_stats': None}
    out.update(extra)
    return out


class IndexCase(unittest.TestCase):
    maxDiff = None

    def report(self, name):
        conn, data = replay('indexes', name)
        result = indexes.index_report(conn, **data['args'])
        assert_read_only(self, conn, TAG)
        json.dumps(result, allow_nan=False)
        self.assertEqual(result['schema_version'], 'indexes-1.0')
        return result, conn


def names(entries):
    return sorted('%s.%s' % (e['table'], e['index']) for e in entries)


class TestLiveReplay(IndexCase):
    def test_performance_schema_servers(self):
        for name in MYSQL + MARIADB_PS:
            with self.subTest(server=name):
                result, conn = self.report(name)
                self.assertEqual(result['source'], 'performance_schema')
                self.assertTrue(result['available'])
                self.assertEqual(result['scope'], {'schema': 'mfshop', 'schemas': ['mfshop'], 'server_wide': False})
                unused = {'%s.%s' % (u['table'], u['index']): u for u in result['unused']}
                for index in ('customers.k_country_created', 'orders.k_status', 'orders.k_status_dup',
                              'orders.k_note10'):
                    self.assertEqual(unused[index]['verdict'], 'candidate', index)
                    self.assertIn('COUNT_STAR = 0', unused[index]['why'])
                    self.assertIn('INVISIBLE' if name.startswith('mysql') else 'IGNORED', unused[index]['test_sql'])
                    self.assertTrue(unused[index]['drop_sql'].startswith('ALTER TABLE `mfshop`.'))
                # The FK-backing index looks unused but is kept.
                fk = unused['orders.k_customer']
                self.assertEqual(fk['verdict'], 'keep')
                self.assertNotIn('drop_sql', fk)
                self.assertIn('fk_orders_customer', fk['why'])
                # PRIMARY and UNIQUE are never listed, on both engines.
                self.assertFalse([u for u in result['unused'] if u['index'] in ('PRIMARY', 'u_email')])
                # Used indexes are not listed: k_country by the query loop, k_created by the
                # scripted SELECT id ... LIMIT 1 (a covering scan), and k_a or k_ab (MySQL chose
                # k_a, MariaDB k_ab for the same query).
                self.assertNotIn('customers.k_country', unused)
                self.assertNotIn('customers.k_created', unused)
                self.assertEqual(len([k for k in unused if k.startswith('odd$name.')]), 1)
                self.assertEqual(result['summary']['keep_foreign_key'], 1)
                # The table never opened since the restart is no data, not unused.
                self.assertEqual([n['table'] for n in result['no_data']], ['idle_table'])
                if name.startswith('mysql'):
                    self.assertEqual(result['no_data'][0]['reason'], 'not_opened')
                self.assertNotIn('idle_table.k_v', unused)
                redundant = {(r['table'], r['index']): r for r in result['redundant']}
                self.assertEqual(redundant[('customers', 'k_country')]['dominant'], 'k_country_created')
                self.assertEqual(redundant[('customers', 'k_country')]['reason'], 'is a leftmost prefix of')
                self.assertEqual(redundant[('orders', 'k_status_dup')]['reason'], 'duplicates')
                self.assertEqual(redundant[('odd$name', 'k_a')]['dominant'], 'k_ab')
                self.assertEqual(redundant[('odd$name', 'k_a')]['test_sql'][:28], 'ALTER TABLE `mfshop`.`odd$na')
                scans = result['full_scans']
                self.assertEqual(scans[0]['table'], 'orders')
                self.assertTrue(scans[0]['mostly'])
                self.assertGreater(scans[0]['share_rows'], 0.9)
                self.assertGreater(scans[0]['fetch_ms_without_index'], 0)
                self.assertIn('rows read without an index', scans[0]['why'])
                # Usage is read before information_schema (MariaDB opens tables for STATISTICS).
                order = [i for i, (sql, _) in enumerate(conn.calls)
                         if 'by_index_usage' in sql or 'information_schema.STATISTICS' in sql]
                self.assertIn('by_index_usage', conn.calls[order[0]][0])

    def test_idle_table_on_mariadb_is_never_unused(self):
        # MariaDB's earlier information_schema reads opened idle_table, so
        # Performance Schema holds zero rows for it: no activity, not unused.
        for name in MARIADB_PS:
            with self.subTest(server=name):
                result, _ = self.report(name)
                idle = [n for n in result['no_data'] if n['table'] == 'idle_table']
                self.assertEqual(len(idle), 1)
                self.assertIn(idle[0]['reason'], ('not_opened', 'no_activity'))

    def test_userstat_fallback(self):
        result, conn = self.report('mariadb-11.4-userstat')
        self.assertEqual(result['source'], 'userstat')
        self.assertTrue(result['available'])
        unused = {'%s.%s' % (u['table'], u['index']): u['verdict'] for u in result['unused']}
        self.assertEqual(unused['orders.k_customer'], 'keep')
        self.assertEqual(unused['customers.k_country_created'], 'candidate')
        self.assertNotIn('customers.k_country', unused)
        self.assertIn('INDEX_STATISTICS', [u for u in result['unused'] if u['index'] == 'k_status'][0]['why'])
        scans = result['full_scans']
        self.assertEqual(scans[0]['table'], 'orders')
        self.assertIsNone(scans[0]['fetch_ms_without_index'])
        self.assertTrue(scans[0]['mostly'])
        self.assertEqual([n['table'] for n in result['no_data']], ['idle_table'])
        self.assertTrue(any('userstat=ON' in n for n in result['notes']))
        self.assertFalse(any('performance_schema.' in sql for sql, _ in conn.calls))

    def test_performance_schema_off_without_userstat(self):
        result, conn = self.report('mariadb-13.0-default')
        self.assertIsNone(result['source'])
        self.assertFalse(result['available'])
        self.assertEqual(result['unused'], [])
        self.assertEqual(result['full_scans'], [])
        # Redundant indexes need only information_schema.
        self.assertEqual(len(result['redundant']), 3)
        self.assertIn('SET GLOBAL userstat = ON', result['notes'][0])
        self.assertIn('performance_schema=ON', result['notes'][0])
        self.assertFalse(any('INDEX_STATISTICS' in sql for sql, _ in conn.calls))

    def test_text_output(self):
        result, _ = self.report('mysql-8.4')
        text = indexes.format_text(result)
        self.assertTrue(text.startswith('MySQL 8.4.11, up '))
        self.assertIn(', schema mfshop\n', text)
        self.assertRegex(text, r'mfshop\.orders +k_customer +customer_id +keep: backs a foreign key')
        self.assertIn('k_status_dup (status) duplicates k_status (status)', text)
        self.assertIn('NO DATA', text)


class TestRules(IndexCase):
    def test_redundant_indexes_match_the_advisor(self):
        tables = indexes._tables(ADVISOR_ROWS, 'mysql')
        parsed = parse_show_create_table(ADVISOR_DDL)
        ours = sorted((i['name'], o['name'], why) for i, o, why in find_redundant_indexes(
            indexes._ordered(tables[('shop', 'orders')])))
        theirs = sorted((i['name'], o['name'] or 'PRIMARY', why) for i, o, why in find_redundant_indexes(parsed['indexes']))
        self.assertEqual(ours, theirs)
        self.assertEqual(ours, [('k_user', 'u_user_status', 'is a leftmost prefix of'),
                                ('k_user_dup', 'k_user', 'duplicates')])
        flagged = {name for name, _, _ in ours}
        # Prefix lengths, direction, functional parts and FULLTEXT keep their indexes; PK never flagged.
        for kept in ('PRIMARY', 'u_user_status', 'k_status_desc', 'k_note10', 'k_expr', 'ft_note'):
            self.assertNotIn(kept, flagged)
        self.assertIn('k_user_dup', flagged)
        # The report and the advisor rule agree on the same table.
        report = indexes.compute_indexes(server(), raw(ADVISOR_ROWS, usage=[]), 'shop')
        analysis = {}
        advise(analysis, schema={'shop.orders': parsed})
        advisor = sorted(f['warning'] for f in analysis['environment_findings'] if f['rule_id'] == 'REDUNDANT_INDEX')
        self.assertEqual(len(advisor), len(report['redundant']))
        for entry in report['redundant']:
            self.assertTrue(any('%s (%s) %s' % (entry['index'], entry['columns'], entry['reason']) in w for w in advisor),
                            entry)

    def test_unique_duplicates_and_hash_indexes(self):
        rows = [stat('t', 'PRIMARY', 1, 'id', 0), stat('t', 'u_a', 1, 'a', 0), stat('t', 'u_a2', 1, 'a', 0),
                stat('t', 'k_a', 1, 'a'), stat('t', 'h_b', 1, 'b', itype='HASH'), stat('t', 'k_b', 1, 'b'),
                stat('t', 'k_id', 1, 'id')]
        report = indexes.compute_indexes(server(), raw(rows, usage=[]), 'shop')
        found = {(r['index'], r['dominant'], r['reason']) for r in report['redundant']}
        self.assertEqual(found, {('u_a2', 'u_a', 'duplicates'), ('k_a', 'u_a', 'duplicates'),
                                 ('k_id', 'PRIMARY', 'duplicates')})

    def test_unused_excludes_primary_unique_and_marks_foreign_keys(self):
        rows = [stat('child', 'PRIMARY', 1, 'id', 0), stat('child', 'u_code', 1, 'code', 0),
                stat('child', 'k_parent', 1, 'parent_id'), stat('child', 'k_parent', 2, 'kind'),
                stat('child', 'k_kind', 1, 'kind'), stat('child', 'k_prefix', 1, 'parent_id', sub_part='4'),
                stat('parent', 'PRIMARY', 1, 'id', 0), stat('parent', 'k_ref', 1, 'ref'),
                stat('parent', 'k_other', 1, 'other')]
        usage = [['shop', 'child', 'PRIMARY', '0', '0', '0'], ['shop', 'child', 'u_code', '0', '0', '0'],
                 ['shop', 'child', 'k_parent', '0', '0', '0'], ['shop', 'child', 'k_kind', '0', '0', '0'],
                 ['shop', 'child', 'k_prefix', '0', '0', '0'], ['shop', 'child', None, '50', '40', '1000'],
                 ['shop', 'parent', 'PRIMARY', '9', '9', '9'], ['shop', 'parent', 'k_ref', '0', '0', '0'],
                 ['shop', 'parent', 'k_other', '0', '0', '0'], ['shop', 'parent', None, '0', '0', '0']]
        fks = [['shop', 'child', 'fk_parent', 'parent_id', '1', 'shop', 'parent', 'ref']]
        report = indexes.compute_indexes(server(), raw(rows, usage=usage, fks=fks), 'shop')
        verdicts = {(u['table'], u['index']): u['verdict'] for u in report['unused']}
        self.assertEqual(verdicts, {('child', 'k_parent'): 'keep', ('child', 'k_kind'): 'candidate',
                                    ('child', 'k_prefix'): 'candidate', ('parent', 'k_ref'): 'keep',
                                    ('parent', 'k_other'): 'candidate'})
        keep = [u for u in report['unused'] if u['index'] == 'k_ref'][0]
        self.assertIn('parent side of fk_parent (from shop.child)', keep['why'])
        # Candidates first, then the ones to keep.
        self.assertEqual([u['verdict'] for u in report['unused']], ['candidate'] * 3 + ['keep'] * 2)
        self.assertEqual(report['foreign_keys'], 1)

    def test_mariadb_unique_indexes_are_excluded_too(self):
        rows = [stat('t', 'PRIMARY', 1, 'id', 0, engine='mariadb'), stat('t', 'u_a', 1, 'a', 0, engine='mariadb'),
                stat('t', 'k_b', 1, 'b', engine='mariadb', visible='NO')]
        usage = [['shop', 't', 'PRIMARY', '1', '1', '1'], ['shop', 't', 'u_a', '0', '0', '0'],
                 ['shop', 't', 'k_b', '0', '0', '0'], ['shop', 't', None, '0', '0', '0']]
        report = indexes.compute_indexes(server('mariadb'), raw(rows, usage=usage), 'shop')
        self.assertEqual([u['index'] for u in report['unused']], ['k_b'])
        self.assertFalse(report['unused'][0]['visible'])
        self.assertIn('already ignored', report['unused'][0]['why'])
        self.assertIn('IGNORED', report['unused'][0]['test_sql'])

    def test_tables_without_activity_or_instrumentation_are_no_data(self):
        rows = [stat('quiet', 'k_a', 1, 'a'), stat('off', 'k_a', 1, 'a'), stat('busy', 'k_a', 1, 'a'),
                stat('absent', 'k_a', 1, 'a')]
        usage = [['shop', 'quiet', 'k_a', '0', '0', '0'], ['shop', 'quiet', None, '0', '0', '0'],
                 ['shop', 'off', 'k_a', '0', '0', '0'], ['shop', 'busy', 'k_a', '0', '0', '0'],
                 ['shop', 'busy', None, '3', '3', '3']]
        objects = [['%', '%', 'YES'], ['shop', 'off', 'NO'], ['mysql', '%', 'NO']]
        report = indexes.compute_indexes(server(), raw(rows, usage=usage, objects=objects), 'shop')
        self.assertEqual({(n['table'], n['reason']) for n in report['no_data']},
                         {('quiet', 'no_activity'), ('off', 'not_instrumented'), ('absent', 'not_opened')})
        self.assertEqual([u['table'] for u in report['unused']], ['busy'])
        self.assertTrue(any('setup_objects turns table instrumentation off' in n for n in report['notes']))

    def test_foreign_key_backing_rules(self):
        rows = [stat('child', 'PRIMARY', 1, 'id', 0), stat('child', 'k_a', 1, 'a'), stat('child', 'ft_a', 1, 'a', itype='FULLTEXT'),
                stat('parent', 'PRIMARY', 1, 'id', 0), stat('parent', 'k_code', 1, 'code')]
        usage = [['shop', t, i, '0', '0', '0'] for t, i in (('child', 'k_a'), ('child', 'ft_a'), ('parent', 'k_code'))] + \
                [['shop', 'child', None, '9', '9', '9'], ['shop', 'parent', None, '9', '9', '9']]
        # FK child (a, id) -> parent (code, id): KEY(a) backs it only where InnoDB appends the
        # primary key (MariaDB on the child side, both engines on the parent side).
        fks = [['shop', 'child', 'fk', 'a', '1', 'shop', 'parent', 'code'],
               ['shop', 'child', 'fk', 'id', '2', 'shop', 'parent', 'id']]
        for engine, child in (('mysql', 'candidate'), ('mariadb', 'keep')):
            with self.subTest(engine=engine):
                rows_e = [r[:9] + ([r[9], r[10]] if engine == 'mysql' else ['NO']) for r in rows]
                report = indexes.compute_indexes(server(engine), raw(rows_e, usage=usage, fks=fks), 'shop')
                verdicts = {(u['table'], u['index']): u['verdict'] for u in report['unused']}
                self.assertEqual(verdicts[('child', 'k_a')], child)
                self.assertEqual(verdicts[('parent', 'k_code')], 'keep')
        # FULLTEXT never backs a foreign key.
        fulltext = indexes._tables([stat('t', 'ft', 1, 'a', itype='FULLTEXT')], 'mysql')[('shop', 't')]['ft']
        self.assertFalse(indexes._starts_with(fulltext, ['a']))

    def test_setup_objects_precedence(self):
        # The server never consults a ('%', table) row.
        self.assertTrue(indexes._object_enabled([['%', '%', 'YES'], ['%', 'x', 'NO']], 'shop', 'x'))
        objects = [['%', '%', 'YES'], ['shop', '%', 'NO'], ['shop', 'keep', 'YES']]
        self.assertFalse(indexes._object_enabled(objects, 'shop', 'x'))
        self.assertTrue(indexes._object_enabled(objects, 'shop', 'keep'))
        self.assertTrue(indexes._object_enabled(objects, 'other', 'x'))
        self.assertFalse(indexes._object_enabled([['shop', '%', 'YES']], 'other', 'x'), 'no match: not instrumented')
        self.assertTrue(indexes._object_enabled(None, 'a', 'b'))

    def test_setup_actors_and_instrument_off(self):
        rows = [stat('t', 'k_a', 1, 'a')]
        usage = [['shop', 't', 'k_a', '0', '0', '0'], ['shop', 't', None, '5', '5', '5']]
        actors = [['%', '%', '%', 'YES'], ['%', 'batch', '%', 'NO']]
        report = indexes.compute_indexes(server(), raw(rows, usage=usage, actors=actors), 'shop')
        self.assertTrue(any('batch@% off' in n for n in report['notes']))
        empty = indexes.compute_indexes(server(), raw(rows, usage=usage, actors=[]), 'shop')
        self.assertTrue(any('setup_actors is empty' in n for n in empty['notes']))
        off = indexes.compute_indexes(server(), raw(rows, usage=usage, instrument=[('wait/io/table/sql/handler', 'NO')]),
                                      'shop')
        self.assertFalse(off['available'])
        self.assertEqual(off['unused'], [])
        self.assertIn("NAME = 'wait/io/table/sql/handler'", off['notes'][0])
        consumer = indexes.compute_indexes(server(), raw(rows, usage=usage, consumers={'global_instrumentation': False}),
                                           'shop')
        self.assertFalse(consumer['available'])

    def test_full_scan_share_uses_fetch_time(self):
        rows = [stat('t', 'k_a', 1, 'a'), stat('u', 'k_a', 1, 'a')]
        usage = [['shop', 't', 'k_a', '100', '100', str(int(9e9))], ['shop', 't', None, '200', '200', str(int(1e9))],
                 ['shop', 'u', 'k_a', '10', '10', str(int(1e8))], ['shop', 'u', None, '20', '20', str(int(3e9))]]
        report = indexes.compute_indexes(server(), raw(rows, usage=usage), 'shop')
        by_table = {f['table']: f for f in report['full_scans']}
        self.assertEqual([f['table'] for f in report['full_scans']], ['u', 't'])
        self.assertEqual(by_table['t']['share_time'], 0.1)
        self.assertFalse(by_table['t']['mostly'], 'most rows, but little of the read time')
        self.assertTrue(by_table['u']['mostly'])
        self.assertEqual(by_table['u']['fetch_ms_without_index'], 3)

    def test_full_scan_text_hint_uses_first_table_only(self):
        for engine in ('mysql', 'mariadb'):
            with self.subTest(engine=engine):
                rows = [stat('t', 'k_a', 1, 'a'), stat('u', 'k_a', 1, 'a')]
                usage = [['shop', 't', None, '10', '10', '1000000'],
                         ['shop', 'u', None, '20', '20', '3000000']]
                report = indexes.compute_indexes(server(engine), raw(rows, usage=usage), 'shop')
                text = indexes.format_text(report)
                self.assertEqual(text.count('Statements: '), 1)
                self.assertIn('Statements: myflames top --order no_index --table shop.u', text)
                self.assertGreater(text.index('Statements: '), text.index('TABLES READ WITHOUT AN INDEX'))
                report['full_scans'] = []
                self.assertNotIn('Statements: ', indexes.format_text(report))
                report['available'] = False
                self.assertNotIn('Statements: ', indexes.format_text(report))

    def test_workspace_retains_every_scoped_index_both_engines(self):
        for engine in ('mysql', 'mariadb'):
            rows = [stat('t%03d' % i, 'k_a', 1, 'a') for i in range(251)]
            usage = [['shop', 't%03d' % i, 'k_a', '0', '0', '0'] for i in range(251)] + \
                    [['shop', 't%03d' % i, None, '1', '1', '1'] for i in range(251)]
            report = indexes.compute_indexes(server(engine), raw(rows, usage=usage), 'shop', limit=None)
            self.assertEqual(len(report['unused']), 251)
            self.assertEqual(len(report['indexes']), 251)
            self.assertEqual(report['more']['indexes'], 0)
            self.assertEqual(report['more']['unused'], 0)
            limited = indexes.compute_indexes(server(engine), raw(rows, usage=usage), 'shop', limit=200)
            self.assertEqual((len(limited['unused']), limited['more']['unused']), (200, 51))

    def test_workspace_browse_ceiling_refuses_instead_of_truncating(self):
        self.assertEqual(indexes.MAX_BROWSE_ROWS, 100000)
        with mock.patch.object(indexes, 'MAX_BROWSE_ROWS', 2):
            with self.assertRaisesRegex(ValueError, 'Choose fewer databases'):
                indexes.compute_indexes(server(), raw([stat('t%d' % i, 'k', 1, 'a') for i in range(3)]), limit=None)

    def test_limits_and_short_uptime(self):
        rows = [stat('t%03d' % i, 'k_a', 1, 'a') for i in range(30)]
        usage = [['shop', 't%03d' % i, 'k_a', '0', '0', '0'] for i in range(30)] + \
                [['shop', 't%03d' % i, None, '1', '1', '1'] for i in range(30)]
        report = indexes.compute_indexes(server(uptime='3600'), raw(rows, usage=usage), 'shop', limit=10)
        self.assertEqual(len(report['unused']), 10)
        self.assertEqual(report['more']['unused'], 20)
        self.assertEqual(report['summary']['unused'], 30)
        self.assertTrue(any('up only 1 h 0 min' in n for n in report['notes']))


class TestSQL(IndexCase):
    def test_schema_scope_uses_a_literal_and_server_wide_skips_system_schemas(self):
        mysql = server()
        scoped = indexes._statistics_sql(mysql, "it's")
        self.assertIn("TABLE_SCHEMA = CONVERT(X'%s' USING utf8mb4)" % "it's".encode().hex(), scoped)
        self.assertIn('IS_VISIBLE, EXPRESSION', scoped)
        self.assertNotIn('CARDINALITY', scoped)
        wide = indexes._usage_sql(None)
        self.assertIn("OBJECT_SCHEMA NOT IN ('mysql', 'performance_schema', 'information_schema', 'sys')", wide)
        self.assertIn('IGNORED', indexes._statistics_sql(server('mariadb'), 'x'))

    def test_schema_and_limit_validation(self):
        for kwargs in ({'schema': 'x' * 65}, {'schema': ' '}, {'schema': 'a\nb'}, {'limit': 0}, {'limit': 5001}):
            with self.subTest(**kwargs):
                conn = ReplayConn([])
                with self.assertRaises(ValueError):
                    indexes.index_report(conn, **kwargs)
                self.assertEqual(conn.calls, [])

    def test_denied_usage_keeps_the_redundant_list(self):
        def edit(sql, text, n):
            if 'by_index_usage' in sql:
                raise ConnectorError("mysql client exited 1: ERROR 1142 (42000): SELECT command denied to user")
            return text
        conn, data = replay('indexes', 'mysql-8.4', edit=edit)
        report = indexes.index_report(conn, **data['args'])
        self.assertFalse(report['available'])
        self.assertEqual(len(report['redundant']), 3)
        self.assertTrue(any('Could not read performance_schema.table_io_waits_summary_by_index_usage' in n
                            for n in report['notes']))


class TestIndexesCLI(IndexCase):
    def call(self, name, *argv, conn=None):
        if conn is None:
            conn, _ = replay('indexes', name)
        return run_cli('indexes', argv, conn) + (conn,)

    def test_json_with_schema(self):
        code, out, err, factory, conn = self.call('mysql-9.7', '--schema', 'mfshop', '--json', '-u', 'app',
                                                  '-p' + PASSWORD)
        self.assertEqual((code, err), (0, ''))
        data = json.loads(out)
        self.assertEqual(data['scope']['schema'], 'mfshop')
        self.assertEqual(factory.options['query_timeout'], 60)
        assert_read_only(self, conn, TAG)

    def test_server_wide_warns_unless_quiet(self):
        conn, data = replay('indexes', 'mysql-8.4')
        # Replay the schema-scoped answers for the server-wide statements.
        conn.answers = {sql.replace("= CONVERT(X'%s' USING utf8mb4)" % 'mfshop'.encode().hex(),
                                    "NOT IN ('mysql', 'performance_schema', 'information_schema', 'sys')"): v
                        for sql, v in conn.answers.items()}
        code, out, err, _, _ = self.call('', conn=conn)
        self.assertEqual(code, 0)
        self.assertIn('note: no --schema, so every schema except the system ones is read', err)
        self.assertIn('every schema', out.split('\n')[0])
        conn.seen = {}
        code, out, err, _, _ = self.call('', '-q', conn=conn)
        self.assertEqual((code, err), (0, ''))

    def test_repeated_schema_and_all_schemas(self):
        conn, data = replay('indexes', 'mysql-8.4')
        code, out, err, _, _ = self.call('', '--schema', 'mfshop', '--schema', 'mfshop', '-q', conn=conn)
        self.assertEqual((code, err), (0, ''), 'a repeated name is read once (the replay matches)')
        self.assertIn('schema mfshop', out.split('\n')[0])
        conn = TestIndexesAPI.scripted(None, 0)
        code, out, err, _, _ = self.call('', '--schema', 'a', '--schema', 'b', '--json', conn=conn)
        self.assertEqual(json.loads(out)['scope']['schemas'], ['a', 'b'])
        conn = TestIndexesAPI.scripted(None, 99999)
        code, out, err, _, _ = self.call('', '--all-schemas', conn=conn)
        self.assertEqual(code, 0)
        self.assertNotIn('no --schema', err, '--all-schemas is an explicit choice: no note, no table-count guard')
        self.assertFalse(any('COUNT(*)' in sql for sql, _ in conn.calls))
        # --all-schemas with --schema lifts the limit for that scope only (the API's all_schemas): the
        # gate's own message asks for it, and argparse used to refuse the pair (found live, 3,000 tables).
        conn = TestIndexesAPI.scripted(None, 99999)
        code, out, err, _, _ = self.call('', '--schema', 'a', '--all-schemas', '--json', conn=conn)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['scope']['schemas'], ['a'])
        self.assertFalse(any('COUNT(*)' in sql for sql, _ in conn.calls))

    def test_bad_arguments_and_failures_exit_2(self):
        for argv in (['--limit', '0'], ['--limit', 'x']):
            conn = ReplayConn([])
            code, out, _, factory, _ = self.call('', *argv, conn=conn)
            self.assertEqual((code, out, factory.options), (2, '', None))
        conn = ReplayConn([], error=ConnectorError('mysql client exited 1: ERROR 1045: denied ' + PASSWORD))
        code, out, err, _, _ = self.call('', '--schema', 'x', '-p' + PASSWORD, conn=conn)
        self.assertEqual((code, out), (2, ''))
        self.assertNotIn(PASSWORD, err)
        self.assertIn('Could not read index health', err)

    def test_root_help(self):
        code, out, _ = run_main(['myflames', '--help'])
        self.assertIn('  indexes   Unused and redundant indexes, tables read mostly by full scans (--schema)', out)
        code, out, _ = run_main(['myflames', 'indexes', '--help'])
        self.assertEqual(code, 0)
        for flag in ('--schema NAME', '--all-schemas', '--limit N', '--json', '--profile'):
            self.assertIn(flag, out)


class TestIndexesAPI(IndexCase):
    connection = {'host': '127.0.0.1', 'port': 3306, 'user': 'app', 'password': PASSWORD}

    def scripted(self, user_tables):
        """Answers the preflight, the table count, and empty index reads."""
        class Conn(ReplayConn):
            def run(self, sql, extra_flags=None, timeout=None):
                self.calls.append((sql, list(extra_flags or [])))
                if 'SHOW GLOBAL VARIABLES' in sql:
                    return batch([['version', '8.4.11'], ['performance_schema', 'ON']])
                if 'COUNT(*)' in sql:
                    return batch([[str(user_tables)]])
                return ''
        return Conn([])

    def test_all_user_schemas_by_default_up_to_the_limit(self):
        # Above 2,000 tables in user schemas the workspace asks for a choice.
        conn = self.scripted(2001)
        factory = FakeFactory(conn)
        with mock.patch('myflames.connector.MySQLConnection', factory):
            with self.assertRaisesRegex(ValueError, r'\(every user schema\) has 2,001 tables, more than 2,000, so Index health '
                                                    r'needs a choice of schemas'):
                ui.api_response('/api/live/indexes', {'connection': self.connection}, workspace=object())
        self.assertFalse(any('STATISTICS' in sql for sql, _ in conn.calls), 'nothing heavy was read')
        # Up to the limit, every user schema is read.
        conn = self.scripted(2000)
        with mock.patch('myflames.connector.MySQLConnection', FakeFactory(conn)):
            result = ui.api_response('/api/live/indexes', {'connection': self.connection}, workspace=object())
        self.assertEqual(result['scope'], {'schema': None, 'schemas': None, 'server_wide': True})
        # Only the explicit override skips the count; selected schemas are gated too.
        for payload in ({'all_schemas': True}, {'schemas': ['a', 'b']}, {'schema': 'a'}):
            conn = self.scripted(99999 if payload.get('all_schemas') else 2000)
            with mock.patch('myflames.connector.MySQLConnection', FakeFactory(conn)):
                result = ui.api_response('/api/live/indexes', dict(payload, connection=self.connection),
                                         workspace=object())
            self.assertEqual(any('COUNT(*)' in sql for sql, _ in conn.calls), not payload.get('all_schemas', False), payload)
        self.assertEqual(result['scope']['schema'], 'a', 'the older single schema still works')
        for payload in ({'schemas': 'a'}, {'schemas': ['x' * 65]}):
            conn = ReplayConn([])
            with mock.patch('myflames.connector.MySQLConnection', FakeFactory(conn)):
                with self.assertRaises(ValueError):
                    ui.api_response('/api/live/indexes', dict(payload, connection=self.connection), workspace=object())
            self.assertEqual(conn.calls, [])

    def test_several_schemas(self):
        conn = self.scripted(0)
        result = indexes.index_report(conn, schemas=['a', 'b'])
        self.assertEqual(result['scope'], {'schema': None, 'schemas': ['a', 'b'], 'server_wide': False})
        self.assertTrue(any("TABLE_SCHEMA IN (CONVERT(X'61' USING utf8mb4), CONVERT(X'62' USING utf8mb4))" in sql
                            for sql, _ in conn.calls))
        self.assertIn('schemas a, b', indexes.format_text(result).split('\n')[0])
        self.assertTrue(any('reference a, b are not checked' in n for n in result['notes']))

    def test_scoped_read(self):
        conn, _ = replay('indexes', 'mariadb-13.0')
        factory = FakeFactory(conn)
        with mock.patch('myflames.connector.MySQLConnection', factory):
            result = ui.api_response('/api/live/indexes', {'connection': self.connection, 'schema': 'mfshop'},
                                     workspace=object())
        self.assertEqual(result['schema_version'], 'indexes-1.0')
        self.assertEqual(factory.options['query_timeout'], 60)
        assert_read_only(self, conn, TAG)

    def test_allowlist(self):
        self.assertIn('"/api/live/indexes"', inspect.getsource(ui.WorkspaceHandler.do_POST))
        self.assertIn('/api/live/indexes', ui._CONNECTION_PATHS)



class TestScaleGate(unittest.TestCase):
    def test_all_schema_gate_explains_mariadb_table_cache_cost(self):
        for engine in ('mysql', 'mariadb'):
            conn = TestIndexesAPI().scripted(5000)
            with self.subTest(engine=engine), mock.patch.object(indexes, 'preflight', return_value=server(engine)):
                with self.assertRaises(ValueError) as caught:
                    indexes.index_report(conn)
                message = str(caught.exception)
                self.assertEqual('can evict cached table definitions' in message, engine == 'mariadb')
                if engine == 'mariadb':
                    self.assertIn('opens every table in scope', message)
                self.assertNotIn('\u2014', message)
                self.assertEqual(len(conn.calls), 1, 'the gate must precede metadata reads')

    def test_selected_schema_gate_and_explicit_override(self):
        for engine in ('mysql', 'mariadb'):
            conn = TestIndexesAPI().scripted(5000)
            with self.subTest(engine=engine), mock.patch.object(indexes, 'preflight', return_value=server(engine)):
                with self.assertRaisesRegex(ValueError, r'selected scope \(schema big\) has [\d,]+ tables, more than 2,000, so Index health needs fewer schemas'):
                    indexes.index_report(conn, schema='big')
                self.assertEqual(len(conn.calls), 1)
                self.assertIn('COUNT(*)', conn.calls[0][0])
                conn.calls = []
                indexes.index_report(conn, schema='big', require_choice_above=None)
                self.assertFalse(any('COUNT(*)' in sql for sql, _ in conn.calls))

    def test_cli_gate_and_all_schemas_override(self):
        conn = TestIndexesAPI().scripted(5000)
        code, out, err, _ = run_cli('indexes', ['--schema', 'big', '-p' + PASSWORD], conn)
        self.assertEqual(code, 2)
        self.assertIn('5,000 tables', err)
        self.assertFalse(any('STATISTICS' in sql for sql, _ in conn.calls))
        conn = TestIndexesAPI().scripted(5000)
        code, out, err, _ = run_cli('indexes', ['--all-schemas', '--json', '-p' + PASSWORD], conn)
        self.assertEqual(code, 0, err)
        self.assertFalse(any('COUNT(*)' in sql for sql, _ in conn.calls))


if __name__ == '__main__':
    unittest.main()
