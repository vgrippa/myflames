"""Table health: ``myflames tables``, the Tables tab API, the schema picker's
list, and the rules behind them.

Replays live captures (see _perf_replay) from throwaway MySQL 8.4 and 26.7
and MariaDB 11.4 and 13.0 containers with schemas ``mftab`` and ``mfother``:
tables with no key (no_key, odd$name, a partitioned table, a MyISAM table, a
system-versioned table on MariaDB), only a nullable unique key, a NOT NULL
unique key, two NOT NULL unique keys declared after a nullable one, a prefix,
functional, virtual generated (MySQL), stored generated and long hash
(MariaDB) unique key, a generated invisible primary key (MySQL), a sequence
and a view (never listed), and tables made fragmented by inserting and
deleting most rows, in a file-per-table tablespace, a general tablespace
(MySQL) and the system tablespace. ``mysql-8.4-cached`` read the statistics
cache from before the deletes; the ``fresh`` captures set
information_schema_stats_expiry = 0. MySQL 26.7 had
show_gipk_in_create_table_and_information_schema=OFF globally.
"""
import inspect
import json
import os
import re
import tempfile
import unittest
from unittest import mock

from _perf_replay import PASSWORD, FakeFactory, ReplayConn, batch, fixture, run_cli, run_main
from myflames import perf_common, schema_health, ui
from myflames.connector import ConnectorError
from myflames.perf_common import Server

TAG = '/*myflames:tables*/ '
SERVERS = ('mysql-8.4', 'mysql-8.4-cached', 'mysql-26.7', 'mariadb-11.4', 'mariadb-13.0')
SESSION_SETS = ('SET SESSION information_schema_stats_expiry = 0; ',
                'SET SESSION show_gipk_in_create_table_and_information_schema = ON; ')
MIB = 1 << 20
GIB = 1 << 30


def replay(name, edit=None, error=None):
    data = fixture('tables', name)
    # The count gate is new; synthesize its answer from the recorded table
    # names. The metadata rows and every existing replay remain unchanged.
    rows = next(text for sql, text, *rest in data['calls'] if 'SELECT TABLE_SCHEMA, TABLE_NAME, ENGINE' in sql)
    count = len([line for line in rows.split('\n') if line])
    srv = server('mariadb' if name.startswith('mariadb') else 'mysql')
    calls = list(data['calls'])
    for schemas in (None, ('shop',), ('mftab', 'mfother')):
        sql = perf_common._table_count_sql(srv, False, perf_common.scope_sql('TABLE_SCHEMA', schemas))
        calls.append([TAG + sql, batch([[count]])])
    return ReplayConn(calls, edit=edit, error=error), data


def assert_read_only(case, conn):
    """SELECT, SHOW GLOBAL and SHOW CREATE TABLE only, each tagged; the only
    SET is one of the two session settings, sent before a tagged SELECT in
    the same client call (so it lasts for that connection only)."""
    case.assertTrue(conn.calls)
    for sql, flags in conn.calls:
        case.assertTrue(sql.startswith(TAG), sql)
        body = sql[len(TAG):]
        for prefix in SESSION_SETS:
            if body.startswith(prefix):
                body = body[len(prefix):]
                case.assertTrue(body.startswith(TAG + 'SELECT '), sql)
                body = body[len(TAG):]
        case.assertRegex(body, r'^(SELECT|SHOW GLOBAL|SHOW CREATE TABLE) ', sql)
        case.assertIn('--comments', flags)
        case.assertNotRegex(re.sub(r'^SHOW (GLOBAL|CREATE TABLE)', '', body),
                            r'\b(UPDATE|SET|INSERT|DELETE|REPLACE|KILL|GRANT|FLUSH|ALTER|CREATE|DROP|TRUNCATE|'
                            r'OPTIMIZE|ANALYZE)\b')
        case.assertNotIn('@@', body)
        case.assertNotIn('INNODB_TABLES ', body + ' ', 'INNODB_TABLES loads every table into the dictionary cache')


def server(engine='mysql', version=None, **variables):
    version = version or ('8.4.11' if engine == 'mysql' else '11.4.13-MariaDB')
    values = {'version': version, 'performance_schema': 'ON', 'information_schema_stats_expiry': '86400',
              'show_gipk_in_create_table_and_information_schema': 'ON', 'lower_case_table_names': '0'}
    if engine == 'mariadb':
        values.pop('information_schema_stats_expiry')
        values.pop('show_gipk_in_create_table_and_information_schema')
    values.update(variables)
    return Server(values, {'Uptime': '86400'})


def table(name, engine='InnoDB', rows=10, data=16384, index=0, free=0, options='', schema='shop'):
    return [schema, name, engine, str(rows), str(data), str(index), str(free), options]


def key(tbl, index, seq, column, nullable='', sub_part=None, itype='BTREE', expression=None, schema='shop'):
    return [schema, tbl, index, str(seq), column, sub_part, nullable, itype, expression]


def raw(tables, keys=(), columns=(), spaces=None, show_create=None, fresh=False, gipk_session=False):
    return {'tables': [list(t) for t in tables], 'keys': [list(k) for k in keys], 'columns': [list(c) for c in columns],
            'spaces': spaces, 'show_create': show_create, 'fresh_applied': fresh, 'gipk_session': gipk_session}


def by_table(entries):
    return {e['table']: e for e in entries}


class TestLiveReplay(unittest.TestCase):
    maxDiff = None

    def report(self, name):
        conn, data = replay(name)
        result = schema_health.table_report(conn, **data['args'])
        assert_read_only(self, conn)
        json.dumps(result, allow_nan=False)
        self.assertEqual(result['schema_version'], 'schema-1.0')
        return result, conn

    def test_primary_keys_on_every_server(self):
        for name in SERVERS:
            with self.subTest(server=name):
                result, _ = self.report(name)
                mysql = name.startswith('mysql')
                missing = by_table(result['missing_primary_keys'])
                self.assertFalse({m['schema'] for m in result['missing_primary_keys']} - {'mftab', 'mfother'},
                                 'system schemas are never read by default')
                for t in ('no_key', 'odd$name', 'part_nopk', 'nullable_uk', 'prefix_uk', 'mixed_uk', 'other_nopk'):
                    self.assertEqual((missing[t]['kind'], missing[t]['status'], missing[t]['clustered_on']),
                                     ('no_usable_key', 'problem', 'hidden row ID'), t)
                self.assertTrue(missing['part_nopk']['partitioned'])
                self.assertIn('partitioning function', missing['part_nopk']['caveat'])
                # NOT NULL unique keys stand in; InnoDB clusters on the first in key order (uk_b, declared
                # after the nullable uk_c and before uk_a).
                self.assertEqual((missing['two_uk']['kind'], missing['two_uk']['status'],
                                  missing['two_uk']['clustered_on']), ('not_null_unique', 'watch', 'uk_b'))
                self.assertIn('ADD PRIMARY KEY (`b`), DROP INDEX `uk_b`, ALGORITHM=INPLACE, LOCK=NONE;',
                              missing['two_uk']['fixes'][0]['sql'])
                self.assertEqual(missing['notnull_uk']['clustered_on'], 'uk_a')
                # A nullable unique key: check for NULLs, then make it the primary key.
                nuk = missing['nullable_uk']
                self.assertEqual(nuk['checks'][0]['sql'], 'SELECT COUNT(*) FROM `mftab`.`nullable_uk` WHERE `a` IS NULL;')
                self.assertIn('ADD PRIMARY KEY (`a`), DROP INDEX `uk_a`', nuk['fixes'][0]['sql'])
                reasons = {t: missing[t]['unique_keys'][0]['why_not'] for t in ('prefix_uk', 'mixed_uk')}
                self.assertEqual(reasons, {'prefix_uk': 'a prefix of s (10)', 'mixed_uk': 'b allows NULL'})
                # no_key has an `id` column, so the surrogate is row_id; the rest get id.
                self.assertIn('ADD COLUMN `row_id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT INVISIBLE PRIMARY KEY '
                              'FIRST, ALGORITHM=INPLACE, LOCK=SHARED;', missing['no_key']['fixes'][-1]['sql'])
                self.assertIn('ADD COLUMN `id`', missing['odd$name']['fixes'][-1]['sql'])
                # Not InnoDB: no clustered index, no online clauses.
                myisam = missing['myisam_nopk']
                self.assertEqual((myisam['engine'], myisam['clustered_on']), ('MyISAM', None))
                self.assertTrue(all('ALGORITHM' not in f['sql'] for f in myisam['fixes']))
                self.assertIn('writes wait for the whole copy', myisam['caveat'])
                if name.startswith('mariadb'):
                    # MariaDB 11.2+ copies online, except when adding an AUTO_INCREMENT column.
                    self.assertIn('Writes continue during the copy', myisam['caveat'])
                else:
                    self.assertIn('MyISAM has no online ALTER', myisam['caveat'])
                self.assertNotIn('v_nopk', missing, 'views are not tables')
                self.assertNotIn('with_pk', missing)
                if mysql:
                    self.assertEqual([g['table'] for g in result['generated_primary_keys']], ['gipk_t'])
                    self.assertNotIn('gipk_t', missing)
                    self.assertEqual(missing['func_uk']['unique_keys'][0]['why_not'], 'a functional key part')
                else:
                    self.assertEqual(result['generated_primary_keys'], [])
                    self.assertNotIn('seq1', missing, 'sequences are not tables')
                    self.assertEqual(missing['versioned_nopk']['kind'], 'no_usable_key')
                    self.assertEqual(missing['long_uk']['unique_keys'][0]['why_not'], 'a hash (long) unique key')
                    self.assertEqual(missing['sg_uk']['unique_keys'][0]['why_not'], 'b allows NULL',
                                     'MariaDB makes generated columns nullable')
                if name == 'mysql-26.7':
                    self.assertEqual(missing['vg_uk']['unique_keys'][0]['why_not'],
                                     'b is a virtual generated column')

    def test_tablespaces_and_free_space(self):
        for name in SERVERS:
            with self.subTest(server=name):
                result, conn = self.report(name)
                free = by_table(result['fragmentation'])
                self.assertNotIn('myisam_nopk', free, 'only InnoDB tables are judged')
                fpt = free['frag_fpt']
                self.assertEqual(fpt['tablespace'], 'file_per_table')
                if name == 'mysql-8.4-cached':
                    # The cache still holds the numbers from before the deletes.
                    self.assertEqual(fpt['status'], 'ok')
                    self.assertLess(fpt['free_bytes'], 100 * MIB)
                    self.assertTrue(result['statistics']['cached'])
                    self.assertTrue(any('information_schema_stats_expiry' in n and '1 d 0 h' in n
                                        for n in result['notes']))
                else:
                    self.assertEqual(fpt['status'], 'watch')
                    self.assertGreater(fpt['free_bytes'], 200 * MIB)
                    self.assertGreater(fpt['free_pct'], 80)
                    self.assertEqual(fpt['fix_sql'], 'OPTIMIZE TABLE `mftab`.`frag_fpt`;')
                    self.assertEqual(fpt['online_sql'], 'ALTER TABLE `mftab`.`frag_fpt` FORCE, ALGORITHM=INPLACE, '
                                                        'LOCK=NONE;')
                    self.assertIn('ENGINE=InnoDB', fpt['caveat'])
                    self.assertEqual('OPTIMIZE_LOCAL_TABLE' in fpt['caveat'], name.startswith('mysql'))
                shared = {s['name']: s for s in result['shared_tablespaces']}
                if name.startswith('mysql'):
                    self.assertEqual((free['frag_gen']['status'], free['frag_gen']['tablespace'],
                                      free['frag_gen']['tablespace_name']), ('shared', 'general', 'mf_gen'))
                    self.assertEqual(sorted(shared['mf_gen']['tables']), ['mftab.frag_gen', 'mftab.gen_small'])
                    self.assertEqual(free['sys_small']['tablespace'], 'system')
                    self.assertTrue(any(s[0].endswith('SHOW CREATE TABLE `mftab`.`frag_gen`') for s in conn.calls))
                    self.assertNotIn('fix_sql', free['frag_gen'], 'no per-table fix in a shared tablespace')
                    # AUTOEXTEND_SIZE=256M: the unused tail is expected, so 250 MiB free is not flagged.
                    self.assertEqual((free['ae_t']['autoextend_bytes'], free['ae_t']['status']), (256 * MIB, 'ok'))
                    self.assertGreater(free['ae_t']['free_bytes'], 200 * MIB)
                    self.assertIn('AUTOEXTEND_SIZE 256.0 MiB', free['ae_t']['why'])
                    self.assertIn('the file does not shrink', shared['mf_gen']['why'])
                else:
                    self.assertEqual((free['frag_sys']['status'], free['frag_sys']['tablespace_name']),
                                     ('shared', 'innodb_system'))
                    self.assertEqual(sorted(shared['innodb_system']['tables']), ['mftab.frag_sys', 'mftab.sys_nopk'])
                    self.assertFalse(result['statistics']['cached'])
                    self.assertTrue(result['statistics']['fresh'])
                    self.assertIn(':autoshrink', shared['innodb_system']['why'])
                    self.assertIn('moves it into its own file', shared['innodb_system']['why'])

    def test_session_settings_only_when_needed(self):
        _, fresh = self.report('mysql-8.4')
        _, cached = self.report('mysql-8.4-cached')
        _, hidden = self.report('mysql-26.7')
        _, maria = self.report('mariadb-13.0')
        sent = lambda conn, text: [s for s, _ in conn.calls if text in s]  # noqa: E731
        self.assertEqual(len(sent(fresh, 'information_schema_stats_expiry = 0')), 1)
        self.assertEqual(sent(cached, 'SET SESSION'), [])
        self.assertEqual(len(sent(hidden, 'show_gipk_in_create_table_and_information_schema = ON')), 2)
        self.assertEqual(sent(maria, 'SET SESSION'), [])
        result, _ = self.report('mysql-26.7')
        self.assertTrue(any('turned it ON for its own connection' in n for n in result['notes']))

    def test_scoped_read_and_text(self):
        result, conn = self.report('mariadb-13.0')
        self.assertEqual(result['scope'], {'schemas': ['mftab', 'mfother'], 'all_user_schemas': False, 'excluded': []})
        self.assertTrue(any("TABLE_SCHEMA IN (CONVERT(X'%s' USING utf8mb4), CONVERT(X'%s' USING utf8mb4))"
                            % ('mftab'.encode().hex(), 'mfother'.encode().hex()) in s for s, _ in conn.calls))
        text = schema_health.format_text(result)
        self.assertIn('TABLES WITHOUT A PRIMARY KEY', text)
        self.assertIn('innodb_system (system)', text)
        self.assertIn('OPTIMIZE TABLE `mftab`.`frag_fpt`;', text)
        self.assertNotIn('col1, col2', text, 'the text output prints concrete statements, not templates')


class TestRules(unittest.TestCase):
    def test_gipk_needs_invisible_auto_increment(self):
        rows = [table('g'), table('fake')]
        keys = [key('g', 'PRIMARY', 1, 'my_row_id'), key('fake', 'PRIMARY', 1, 'my_row_id')]
        columns = [['shop', 'g', 'my_row_id', 'auto_increment INVISIBLE'], ['shop', 'fake', 'my_row_id', '']]
        report = schema_health.compute_tables(server(), raw(rows, keys, columns))
        self.assertEqual([g['table'] for g in report['generated_primary_keys']], ['g'])
        self.assertEqual(report['missing_primary_keys'], [], 'a visible my_row_id primary key is still a key')

    def test_unique_keys_both_engines(self):
        rows = [table('nn'), table('nul'), table('mem', engine='MEMORY'), table('h'), table('vg')]
        keys = [key('nn', 'uk', 1, 'a'), key('nn', 'uk', 2, 'b'),
                key('nul', 'uk', 1, 'a'), key('nul', 'uk', 2, 'b', nullable='YES'),
                key('mem', 'uk', 1, 'a', itype='HASH'), key('h', 'uk', 1, 't', itype='HASH'),
                key('vg', 'uk', 1, 'v')]
        columns = [['shop', 'vg', 'v', 'VIRTUAL GENERATED']]
        for engine in ('mysql', 'mariadb'):
            with self.subTest(engine=engine):
                m = by_table(schema_health.compute_tables(server(engine), raw(rows, keys, columns))
                             ['missing_primary_keys'])
                self.assertEqual((m['nn']['kind'], m['nn']['clustered_on']), ('not_null_unique', 'uk'))
                self.assertEqual((m['nul']['kind'], m['nul']['unique_keys'][0]['why_not']),
                                 ('no_usable_key', 'b allows NULL'))
                self.assertEqual(m['mem']['kind'], 'not_null_unique', 'MEMORY HASH unique keys identify rows')
                self.assertIsNone(m['mem']['clustered_on'])
                self.assertEqual(m['h']['unique_keys'][0]['why_not'], 'a hash (long) unique key')
                self.assertEqual(m['vg']['unique_keys'][0]['why_not'], 'v is a virtual generated column')

    def test_row_id_text_by_version(self):
        self.assertIn("global dictionary mutex", schema_health.row_id_text(server('mysql', '8.4.11')))
        self.assertIn("atomic since MySQL 9.6", schema_health.row_id_text(server('mysql', '9.7.0')))
        self.assertIn("one atomic counter shared", schema_health.row_id_text(server('mariadb', '10.11.16-MariaDB')))
        self.assertIn("per table", schema_health.row_id_text(server('mariadb', '11.4.13-MariaDB')))
        why = {w['key']: w for w in schema_health.why_texts(server('mariadb'))}
        self.assertIn('Galera', why['clusters']['title'])
        self.assertIn('until it finds the row', why['replication']['text'])
        why = {w['key']: w for w in schema_health.why_texts(server())}
        self.assertIn('external plugin', why['clusters']['text'])
        self.assertIn('up to one full table scan per event', why['replication']['text'])
        self.assertIn('virtual generated column', why['clustering']['text'])
        maria = {w['key']: w for w in schema_health.why_texts(server('mariadb'))}
        self.assertIn('a generated column or a long hash key', maria['clustering']['text'])

    def test_free_space_thresholds(self):
        cases = [
            ('ok_small', 50 * MIB, 50 * MIB, 'ok'),            # 50%, below 100 MiB
            ('ok_share', 1000 * MIB, 105 * MIB, 'ok'),         # 9.5%
            ('watch', 900 * MIB, 100 * MIB, 'watch'),          # exactly 10% and 100 MiB
            ('watch_big', 9 * GIB, 2 * GIB, 'watch'),          # 18%
            ('watch_pct', 100 * MIB, 500 * MIB, 'watch'),      # 83% but under 1 GiB
            ('problem', 3 * GIB, 1 * GIB, 'problem'),          # exactly 25% and 1 GiB
        ]
        rows = [table(n, data=d, free=f) for n, d, f, _ in cases]
        keys = [key(n, 'PRIMARY', 1, 'id') for n, _, _, _ in cases]
        spaces = [['shop/%s' % n, 'Single'] for n, _, _, _ in cases]
        report = schema_health.compute_tables(server(), raw(rows, keys, spaces=spaces))
        got = {f['table']: f['status'] for f in report['fragmentation']}
        self.assertEqual(got, {n: s for n, _, _, s in cases})
        self.assertEqual(report['summary']['free_watch'], 3)
        self.assertEqual(report['summary']['free_problem'], 1)
        self.assertEqual([f['table'] for f in report['fragmentation']][:4], ['problem', 'watch_big', 'watch_pct', 'watch'],
                         'worst first, then the most free space')

    def test_tablespace_classification(self):
        rows = [table('fpt', free=200 * MIB), table('gen', free=300 * MIB), table('sysx', free=50 * MIB),
                table('odd$name', free=5 * MIB), table('part', free=500 * MIB, options='partitioned'),
                table('missing', free=1 * MIB), table('empty', free=0)]
        mysql_spaces = [['shop/fpt', 'Single'], ['shop/odd$name', 'Single'], ['shop/part#p#p0', 'Single'],
                        ['shop/part#p#p1', 'Single']]
        # MySQL without general tablespaces: anything not file-per-table is in the system tablespace.
        report = schema_health.compute_tables(server(), raw(rows, spaces=mysql_spaces))
        f = by_table(report['fragmentation'])
        self.assertEqual({t: (f[t]['tablespace'], f[t]['status']) for t in f}, {
            'fpt': ('file_per_table', 'watch'), 'gen': ('system', 'shared'), 'sysx': ('system', 'shared'),
            'odd$name': ('file_per_table', 'ok'), 'part': ('file_per_table', 'watch'),
            'missing': ('system', 'shared'), 'empty': (None, 'ok')})
        # With a general tablespace, SHOW CREATE TABLE names it; past the cap it stays unnamed.
        spaces = mysql_spaces + [['g1', 'General']]
        report = schema_health.compute_tables(server(), raw(rows, spaces=spaces, show_create={
            'shop.gen': 'g1', 'shop.sysx': 'innodb_system', 'shop.missing': ''}))
        f = by_table(report['fragmentation'])
        self.assertEqual((f['gen']['tablespace'], f['gen']['tablespace_name']), ('general', 'g1'))
        self.assertEqual(f['sysx']['tablespace'], 'system')
        self.assertEqual(f['missing']['tablespace'], 'system', 'no TABLESPACE clause: the system tablespace')
        report = schema_health.compute_tables(server(), raw(rows, spaces=spaces, show_create={}))
        self.assertEqual(by_table(report['fragmentation'])['gen']['tablespace'], 'shared')
        self.assertIn('more than %d tables to name' % schema_health.MAX_SHOW_CREATE,
                      report['shared_tablespaces'][0]['why'])
        # MariaDB: SYS_TABLES names are filename-encoded; #P# partitions; space 0 is the system tablespace.
        maria = [['shop/fpt', 'Single'], ['shop/odd@0024name', 'Single'], ['shop/part#P#p0', 'Single'],
                 ['shop/part#P#p1', 'System'], ['shop/sysx', 'System'], ['shop/gen', 'Single']]
        report = schema_health.compute_tables(server('mariadb'), raw(rows, spaces=maria))
        f = by_table(report['fragmentation'])
        self.assertEqual(f['odd$name']['tablespace'], 'file_per_table')
        self.assertEqual(f['part']['status'], 'shared', 'one partition in the system tablespace')
        self.assertEqual(f['missing']['status'], 'unknown')
        # PROCESS denied: free space shown, not judged.
        report = schema_health.compute_tables(server(), raw(rows, spaces=None))
        self.assertEqual({x['status'] for x in report['fragmentation'] if x['free_bytes']}, {'unknown'})
        self.assertFalse(report['tablespaces_known'])

    def test_autoextend_and_zero_free(self):
        rows = [table('big', data=16384, free=3 * GIB), table('ae', data=16384, free=3 * GIB), table('z', free=0)]
        spaces = [['shop/big', 'Single', '0'], ['shop/ae', 'Single', str(4 * GIB)], ['shop/z', 'Single', None]]
        f = by_table(schema_health.compute_tables(server(), raw(rows, spaces=spaces))['fragmentation'])
        self.assertEqual(f['big']['status'], 'problem')
        self.assertEqual((f['ae']['status'], f['ae']['autoextend_bytes']), ('ok', 4 * GIB))
        self.assertIn('no free space beyond what InnoDB holds back', f['z']['why'])

    def test_lower_case_table_names_folds(self):
        rows = [table('Orders', free=500 * MIB, schema='Shop')]
        spaces = [['shop/orders', 'Single']]
        exact = schema_health.compute_tables(server(), raw(rows, spaces=spaces))
        self.assertEqual(exact['fragmentation'][0]['tablespace'], 'system')
        folded = schema_health.compute_tables(server(lower_case_table_names='1'), raw(rows, spaces=spaces))
        self.assertEqual(folded['fragmentation'][0]['tablespace'], 'file_per_table')

    def test_surrogate_name_avoids_existing_columns(self):
        rows = [table('a'), table('b'), table('c')]
        columns = [['shop', 'a', 'id', ''], ['shop', 'b', 'id', ''], ['shop', 'b', 'row_id', ''],
                   ['shop', 'c', 'ID', ''], ['shop', 'c', 'row_id', ''], ['shop', 'c', 'pk_id', '']]
        m = by_table(schema_health.compute_tables(server(), raw(rows, columns=columns))['missing_primary_keys'])
        self.assertIn('ADD COLUMN `row_id`', m['a']['fixes'][-1]['sql'])
        self.assertIn('ADD COLUMN `pk_id`', m['b']['fixes'][-1]['sql'])
        self.assertIn('ADD COLUMN `row_pk`', m['c']['fixes'][-1]['sql'])

    def test_notes_for_settings_and_empty_scope(self):
        report = schema_health.compute_tables(
            server(sql_require_primary_key='ON', sql_generate_invisible_primary_key='ON',
                   group_replication_group_name='aaaa'), raw([], gipk_session=True), ['nothing'])
        notes = report['notes']
        self.assertTrue(notes[0].startswith('No base tables in schema nothing'))
        for text in ('sql_require_primary_key is ON', 'sql_generate_invisible_primary_key is ON',
                     'Group Replication is configured', 'turned it ON for its own connection'):
            self.assertTrue(any(text in n for n in notes), text)
        maria = schema_health.compute_tables(server('mariadb', innodb_force_primary_key='ON', wsrep_on='ON'),
                                             raw([table('t', free=2 * GIB, data=GIB)],
                                                 [key('t', 'PRIMARY', 1, 'id')], spaces=[['shop/t', 'Single']]),
                                             fresh=True)
        notes = maria['notes']
        self.assertTrue(any('MariaDB has no information_schema statistics cache' in n for n in notes))
        self.assertTrue(any('innodb_force_primary_key is ON' in n for n in notes))
        self.assertTrue(any('Galera node' in n for n in notes))
        self.assertNotIn('OPTIMIZE_LOCAL_TABLE', maria['fragmentation'][0]['caveat'])
        defrag = schema_health.compute_tables(server('mariadb', '10.11.16-MariaDB', innodb_defragment='ON',
                                                     innodb_optimize_fulltext_only='ON'),
                                              raw([table('t', free=2 * GIB, data=GIB)], [key('t', 'PRIMARY', 1, 'id')],
                                                  spaces=[['shop/t', 'Single']]))
        self.assertTrue(any('innodb_defragment is ON' in n for n in defrag['notes']))
        self.assertTrue(any('innodb_optimize_fulltext_only is ON' in n for n in defrag['notes']))
        self.assertIn('the file does not shrink', schema_health._shared_rebuild_text(server('mariadb', '10.11.16-MariaDB')))
        self.assertIn('index on a virtual column', defrag['fragmentation'][0]['caveat'])
        # DATA_FREE comes from the MySQL cache unless fresh; MariaDB reads it every time.
        cached = schema_health.compute_tables(server(), raw([table('t')], [key('t', 'PRIMARY', 1, 'id')]))
        self.assertTrue(any('comes from the same cache unless Fresh numbers is on' in n for n in cached['notes']))
        self.assertTrue(any('read from the tablespace every time' in n for n in maria['notes']))
        gipk97 = schema_health.compute_tables(server('mysql', '9.7.0', sql_generate_invisible_primary_key='ON',
                                                     sql_require_primary_key='ON'), raw([]))
        self.assertTrue(any('since 9.7, not when the table declares a unique key' in n for n in gipk97['notes']))
        self.assertTrue(any('since 9.7 a unique key on NOT NULL columns also satisfies it' in n for n in gipk97['notes']))
        # Many tables read by opening each one: say so.
        many = [table('t%04d' % i) for i in range(2001)]
        report = schema_health.compute_tables(server('mariadb'), raw(many))
        self.assertTrue(any('covered 2,001 tables and MariaDB opened each of them' in n for n in report['notes']))
        report = schema_health.compute_tables(server(), raw(many))
        self.assertFalse(any('covered 2,001' in n for n in report['notes']), 'cached MySQL statistics open nothing')

    def test_storage_engine_inventory(self):
        """summary.engines counts every table read per engine (the Full
        analysis report's storage engine family), with no extra statement."""
        rows = [table('a'), table('b', data=1000, index=24), table('m', engine='MyISAM', data=100),
                table('x', engine='Aria', data=None, index=None)]
        report = schema_health.compute_tables(server(), raw(rows))
        self.assertEqual(report['summary']['engines'], [
            {'engine': 'InnoDB', 'tables': 2, 'bytes': 16384 + 1024},
            {'engine': 'Aria', 'tables': 1, 'bytes': 0},
            {'engine': 'MyISAM', 'tables': 1, 'bytes': 100}])
        self.assertEqual(sum(e['tables'] for e in report['summary']['engines']), report['summary']['tables'])
        self.assertEqual(schema_health.compute_tables(server(), raw([]))['summary']['engines'], [])

    def test_at_or_worse_and_limits(self):
        rows = [table('t%02d' % i) for i in range(12)] + [table('nn')]
        keys = [key('nn', 'uk', 1, 'a')]
        report = schema_health.compute_tables(server(), raw(rows, keys), limit=5)
        self.assertEqual(len(report['missing_primary_keys']), 5)
        self.assertEqual(report['more']['missing_primary_keys'], 8)
        self.assertEqual(report['summary']['no_usable_key'], 12)
        self.assertEqual(len(schema_health.at_or_worse(report, 'problem')), 5)
        full = schema_health.compute_tables(server(), raw(rows, keys))
        self.assertEqual(len(schema_health.at_or_worse(full, 'watch')), 13)
        self.assertEqual(full['summary']['status'], 'problem')


class TestSampling(unittest.TestCase):
    def conn(self, engine='mysql', show_gipk='ON', expiry='86400', denied=False):
        """A scripted connection: answers by statement shape."""
        version = '8.4.11' if engine == 'mysql' else '11.4.13-MariaDB'
        variables = [['version', version], ['performance_schema', 'ON']]
        if engine == 'mysql':
            variables += [['show_gipk_in_create_table_and_information_schema', show_gipk]]
            if expiry is not None:
                variables += [['information_schema_stats_expiry', expiry]]

        class Conn(ReplayConn):
            def run(self, sql, extra_flags=None, timeout=None):
                self.calls.append((sql, list(extra_flags or [])))
                if 'SHOW GLOBAL VARIABLES' in sql:
                    return batch(variables)
                if 'SHOW GLOBAL STATUS' in sql:
                    return batch([['Uptime', '100']])
                if 'COUNT(*)' in sql:
                    return batch([['1']])
                if 'information_schema.TABLES' in sql:
                    return batch([table('t', free=500 * MIB, data=GIB)])
                if 'INNODB_TABLESPACES' in sql or 'INNODB_SYS_TABLES' in sql:
                    if denied:
                        raise ConnectorError('mysql client exited 1: ERROR 1227 (42000): Access denied; you need '
                                             '(at least one of) the PROCESS privilege(s)')
                    return batch([['shop/t', 'Single']])
                return ''
        return Conn([])

    def test_fresh_and_gipk_session_settings(self):
        conn = self.conn()
        report = schema_health.table_report(conn, ['shop'], fresh=True)
        sqls = [s for s, _ in conn.calls]
        self.assertTrue(any(s.startswith(TAG + 'SET SESSION information_schema_stats_expiry = 0; ' + TAG + 'SELECT ')
                            and 'information_schema.TABLES' in s for s in sqls))
        self.assertFalse(any('show_gipk' in s and 'SET' in s for s in sqls))
        self.assertTrue(report['statistics']['fresh'])
        conn = self.conn(show_gipk='OFF')
        schema_health.table_report(conn, ['shop'])
        sqls = [s for s, _ in conn.calls]
        self.assertFalse(any('information_schema_stats_expiry = 0' in s for s in sqls))
        self.assertTrue(any('SET SESSION show_gipk_in_create_table_and_information_schema = ON' in s
                            and 'information_schema.STATISTICS' in s for s in sqls))
        for engine, expiry in (('mariadb', None), ('mysql', None)):
            conn = self.conn(engine, expiry=expiry)
            schema_health.table_report(conn, None, fresh=True)
            self.assertFalse(any('SET SESSION' in s for s, _ in conn.calls), (engine, expiry))
            self.assertTrue(any("NOT IN ('mysql', 'performance_schema', 'information_schema', 'sys')" in s
                                for s, _ in conn.calls))
        for conn in (self.conn(), self.conn('mariadb')):
            schema_health.table_report(conn, None, fresh=True)
            assert_read_only(self, conn)

    def test_older_mysql_without_autoextend_column(self):
        conn = self.conn()
        original = conn.run

        def run(sql, extra_flags=None, timeout=None):
            if 'AUTOEXTEND_SIZE FROM information_schema.INNODB_TABLESPACES' in sql:
                conn.calls.append((sql, list(extra_flags or [])))
                raise ConnectorError("mysql client exited 1: ERROR 1054 (42S22): Unknown column 'AUTOEXTEND_SIZE'")
            return original(sql, extra_flags, timeout)
        conn.run = run
        report = schema_health.table_report(conn, ['shop'])
        self.assertTrue(any('INNODB_TABLESPACES_BRIEF' in s for s, _ in conn.calls))
        self.assertEqual(report['fragmentation'][0]['tablespace'], 'file_per_table')

    def test_process_denied_is_a_note(self):
        conn = self.conn(denied=True)
        report = schema_health.table_report(conn, ['shop'])
        self.assertEqual(report['fragmentation'][0]['status'], 'unknown')
        self.assertTrue(any('PROCESS privilege' in n for n in report['notes']))

    def test_validation(self):
        for kwargs in ({'schemas': 'shop'}, {'schemas': ['x' * 65]}, {'schemas': ['a\nb']}, {'fresh': 'yes'},
                       {'limit': 0}, {'schemas': ['s%d' % i for i in range(201)]}):
            with self.subTest(**{k: str(v)[:20] for k, v in kwargs.items()}):
                conn = ReplayConn([])
                with self.assertRaises(ValueError):
                    schema_health.table_report(conn, **kwargs)
                self.assertEqual(conn.calls, [])

    def test_connection_errors_are_value_errors(self):
        conn = ReplayConn([], error=ConnectorError('mysql client exited 1: ERROR 2003: cannot connect'))
        with self.assertRaisesRegex(ValueError, 'Could not read table health: ERROR 2003'):
            schema_health.table_report(conn)


class TestSchemaPicker(unittest.TestCase):
    def test_check_schemas_and_scope(self):
        self.assertIsNone(perf_common.check_schemas(None))
        self.assertIsNone(perf_common.check_schemas([]))
        self.assertEqual(perf_common.check_schemas(['a', 'b', 'a', '']), ('a', 'b'))
        self.assertEqual(perf_common.scope_sql('S', ('a',)), "S = CONVERT(X'61' USING utf8mb4)")
        self.assertEqual(perf_common.scope_sql('S', ('a', 'b')),
                         "S IN (CONVERT(X'61' USING utf8mb4), CONVERT(X'62' USING utf8mb4))")
        self.assertEqual(perf_common.scope_sql('S', None),
                         "S NOT IN ('mysql', 'performance_schema', 'information_schema', 'sys')")
        for name in ('mysql', 'SYS', 'performance_schema', 'information_schema', '#mysql50#lost+found'):
            self.assertTrue(perf_common.is_system_schema(name), name)
        self.assertFalse(perf_common.is_system_schema('shop'))

    def catalog_conn(self, engine, user_tables=3):
        version = '8.4.11' if engine == 'mysql' else '11.4.13-MariaDB'

        class Conn(ReplayConn):
            def run(self, sql, extra_flags=None, timeout=None):
                self.calls.append((sql, list(extra_flags or [])))
                if 'SHOW GLOBAL VARIABLES' in sql:
                    return batch([['version', version]])
                if 'SHOW GLOBAL STATUS' in sql:
                    return batch([['Uptime', '5']])
                if 'SCHEMATA' in sql:
                    return batch([['shop'], ['mysql'], ['empty'], ['#mysql50#lost+found'], ['sys']])
                if 'GROUP BY TABLE_SCHEMA' in sql:
                    return batch([['shop', str(user_tables)], ['mysql', '38'], ['sys', '1']])
                if 'COUNT(*)' in sql:
                    return batch([[str(user_tables)]])
                raise AssertionError(sql)
        return Conn([])

    def test_catalog_both_engines(self):
        for engine in ('mysql', 'mariadb'):
            with self.subTest(engine=engine):
                conn = self.catalog_conn(engine)
                cat = perf_common.schema_catalog(conn)
                self.assertEqual(cat['schema_version'], 'schemas-1.0')
                self.assertEqual([(s['name'], s['tables'], s['system']) for s in cat['schemas']], [
                    ('empty', 0, False), ('shop', 3, False), ('#mysql50#lost+found', 0, True), ('mysql', 38, True),
                    ('sys', 1, True)])
                self.assertEqual((cat['user_tables'], cat['all_limit']), (3, 2000))
                self.assertEqual('can evict cached table definitions' in cat['all_note'], engine == 'mariadb')
                count_sql = [s for s, _ in conn.calls if 'GROUP BY' in s][0]
                # MariaDB: TABLE_TYPE would open each table's .frm; name columns only.
                self.assertEqual('TABLE_TYPE' in count_sql, engine == 'mysql')

    def test_require_choice(self):
        server_ = server()
        reader = perf_common.Reader(self.catalog_conn('mysql', 2001), '/*t*/ ')
        with self.assertRaisesRegex(ValueError, r'The selected scope \(every user schema\) has 2,001 tables, more than 2,000'):
            perf_common.require_choice(reader, server_, None)
        reader = perf_common.Reader(self.catalog_conn('mysql', 2000), '/*t*/ ')
        self.assertEqual(perf_common.require_choice(reader, server_, None), 2000)
        conn = self.catalog_conn('mysql', 99999)
        with self.assertRaisesRegex(ValueError, r'selected scope \(schema shop\) has [\d,]+ tables'):
            perf_common.require_choice(perf_common.Reader(conn, ''), server_, ('shop',))
        self.assertIsNone(perf_common.require_choice(perf_common.Reader(conn, ''), server_, None, limit=None))
        self.assertEqual(len(conn.calls), 1)


class TestTablesCLI(unittest.TestCase):
    def call(self, name, *argv, conn=None):
        if conn is None:
            conn, _ = replay(name)
        return run_cli('tables', argv, conn) + (conn,)

    def test_json_and_scope_flags(self):
        code, out, err, factory, conn = self.call('mariadb-13.0', '--schema', 'mftab', '--schema', 'mfother', '--json',
                                                  '-u', 'app', '-p' + PASSWORD)
        self.assertEqual((code, err), (0, ''))
        data = json.loads(out)
        self.assertEqual(data['schema_version'], 'schema-1.0')
        self.assertEqual(data['scope']['schemas'], ['mftab', 'mfother'])
        self.assertEqual(factory.options['query_timeout'], 60)
        assert_read_only(self, conn)

    def test_fresh_text_notes_and_quiet(self):
        code, out, err, _, _ = self.call('mysql-8.4', '--fresh')
        self.assertEqual(code, 0)
        self.assertIn('statistics fresh', out.split('\n')[0])
        self.assertIn('note: Fresh numbers', err)
        code, out, err, _, _ = self.call('mysql-8.4', '--fresh', '-q')
        self.assertEqual((code, err), (0, ''))

    def test_fail_on_exit_codes(self):
        code, out, err, _, _ = self.call('mysql-8.4', '--fresh', '--fail-on', 'problem')
        self.assertEqual(code, 1)
        self.assertIn('FAIL:', err)
        self.assertIn('[problem] mftab.no_key: no primary key', err)
        code, _, err, _, _ = self.call('mysql-8.4', '--fresh', '--fail-on', 'watch', '-q')
        self.assertEqual((code, err), (1, ''))
        class Clean(ReplayConn):
            def run(self, sql, extra_flags=None, timeout=None):
                self.calls.append((sql, list(extra_flags or [])))
                if 'SHOW GLOBAL VARIABLES' in sql:
                    return batch([['version', '8.4.11']])
                if 'COUNT(*)' in sql:
                    return batch([['1']])
                if 'information_schema.TABLES' in sql:
                    return batch([table('t')])
                if 'STATISTICS' in sql:
                    return batch([key('t', 'PRIMARY', 1, 'id')])
                return ''
        code, out, err, _, _ = self.call('', '--fail-on', 'watch', conn=Clean([]))
        self.assertEqual(code, 0)
        self.assertIn('OK: no table at --fail-on watch', err)

    def test_output_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'tables.json')
            code, out, err, _, _ = self.call('mariadb-11.4', '--json', '-o', path)
            self.assertEqual((code, out), (0, ''))
            self.assertIn('Written to', err)
            with open(path, encoding='utf-8') as handle:
                self.assertEqual(json.load(handle)['schema_version'], 'schema-1.0')

    def test_bad_arguments_and_failures_exit_2(self):
        for argv in (['--limit', '0'], ['--fail-on', 'bad'], ['--schema', 'x' * 65]):
            with self.subTest(argv=argv[0]):
                conn = ReplayConn([])
                code, out, _, factory, _ = self.call('', *argv, conn=conn)
                self.assertEqual((code, out), (2, ''))
                self.assertEqual(conn.calls, [])
        conn = ReplayConn([], error=ConnectorError('mysql client exited 1: ERROR 1045: denied ' + PASSWORD))
        code, out, err, _, _ = self.call('', '-p' + PASSWORD, conn=conn)
        self.assertEqual((code, out), (2, ''))
        self.assertNotIn(PASSWORD, err)
        self.assertIn('Could not read table health', err)

    def test_help(self):
        code, out, _ = run_main(['myflames', '--help'])
        self.assertIn('  tables    Tables without a primary key or usable unique key; InnoDB free space (--fresh)', out)
        code, out, _ = run_main(['myflames', 'tables', '--help'])
        self.assertEqual(code, 0)
        for flag in ('--schema NAME', '--all-schemas', '--fresh', '--fail-on LEVEL', '--json', '--profile', '-o PATH',
                     '-q', 'Exit codes: 0 success'):
            self.assertIn(flag, out)


class TestTablesAPI(unittest.TestCase):
    connection = {'host': '127.0.0.1', 'port': 3306, 'user': 'app', 'password': PASSWORD}

    def call(self, path, payload, conn):
        factory = FakeFactory(conn)
        with mock.patch('myflames.connector.MySQLConnection', factory):
            result = ui.api_response(path, dict(payload, connection=self.connection), workspace=object())
        return result, factory

    def test_tables_endpoint(self):
        conn, _ = replay('mariadb-13.0')
        result, factory = self.call('/api/live/tables', {'schemas': ['mftab', 'mfother']}, conn)
        self.assertEqual(result['schema_version'], 'schema-1.0')
        self.assertEqual(factory.options['query_timeout'], 60)
        assert_read_only(self, conn)
        conn, _ = replay('mysql-8.4')
        result, _ = self.call('/api/live/tables', {'fresh': True}, conn)
        self.assertTrue(result['statistics']['fresh'])

    def test_bad_payloads(self):
        for payload in ({'schemas': 'mftab'}, {'schemas': ['a\nb']}, {'fresh': 'yes'}):
            with self.subTest(payload=payload):
                conn = ReplayConn([])
                with self.assertRaises(ValueError):
                    self.call('/api/live/tables', payload, conn)
                self.assertEqual(conn.calls, [])

    def test_schemas_endpoint(self):
        conn = ReplayConn(fixture('schemas', 'mysql-8.4')['calls'])
        result, _ = self.call('/api/live/schemas', {}, conn)
        self.assertEqual(result['schema_version'], 'schemas-1.0')
        names = [s['name'] for s in result['schemas'] if not s['system']]
        self.assertEqual(names, ['mfother', 'mftab'])
        self.assertTrue(all(s['system'] for s in result['schemas'] if s['name'] in ('mysql', 'sys')))

    def test_allowlist_and_saved_password(self):
        source = inspect.getsource(ui.WorkspaceHandler.do_POST)
        for path in ('/api/live/tables', '/api/live/schemas'):
            self.assertIn('"%s"' % path, source)
            self.assertIn(path, ui._CONNECTION_PATHS, 'a saved password is filled in for this endpoint')
            self.assertIn(path, ui._SERVER_VIEW_PATHS)



class TestScaleLimits(unittest.TestCase):
    def test_all_schema_gate_explains_mariadb_table_cache_cost(self):
        for engine in ('mysql', 'mariadb'):
            conn = TestSchemaPicker().catalog_conn(engine, 5000)
            with self.subTest(engine=engine), mock.patch.object(schema_health, 'preflight', return_value=server(engine)):
                with self.assertRaises(ValueError) as caught:
                    schema_health.table_report(conn)
                message = str(caught.exception)
                self.assertEqual('can evict cached table definitions' in message, engine == 'mariadb')
                if engine == 'mariadb':
                    self.assertIn('opens every table in scope', message)
                self.assertNotIn('\u2014', message)
                self.assertEqual(len(conn.calls), 1, 'the gate must precede metadata reads')

    def test_selected_scope_gate_before_metadata(self):
        for engine in ('mysql', 'mariadb'):
            conn = TestSchemaPicker().catalog_conn(engine, 5000)
            with self.subTest(engine=engine), mock.patch.object(schema_health, 'preflight', return_value=server(engine)):
                with self.assertRaisesRegex(ValueError, r'selected scope \(schema shop\) has [\d,]+ tables'):
                    schema_health.table_report(conn, ['shop'])
                self.assertEqual(len(conn.calls), 1)
                sql = conn.calls[0][0]
                self.assertIn('COUNT(*)', sql)
                self.assertIn("TABLE_SCHEMA = CONVERT(X'73686f70' USING utf8mb4)", sql)
                self.assertEqual('TABLE_TYPE' in sql, engine == 'mysql')

    def test_show_create_aggregate_deadline(self):
        # General spaces require per-table SHOW CREATE only on MySQL.
        conn = TestSampling().conn()
        original = conn.run
        clock = [0.0]
        timeouts = []

        def run(sql, extra_flags=None, timeout=None):
            if 'SELECT TABLE_SCHEMA, TABLE_NAME, ENGINE' in sql:
                return batch([table('t%d' % i, free=500 * MIB, data=GIB) for i in range(3)])
            if 'FROM information_schema.INNODB_TABLESPACES' in sql:
                return batch([['g', 'General', '0']])
            if 'SHOW CREATE TABLE' in sql:
                timeouts.append(timeout)
                clock[0] += 20
                return batch([['t', 'CREATE TABLE t (id int) TABLESPACE `g`']])
            return original(sql, extra_flags, timeout)
        conn.run = run
        with mock.patch.object(schema_health.time, 'monotonic', side_effect=lambda: clock[0]):
            report = schema_health.table_report(conn, ['shop'])
        self.assertEqual(timeouts, [30.0, 10.0])
        self.assertEqual(report['tablespace_collection'],
                         {'show_create_limit': 50, 'deadline_s': 30.0, 'unresolved': 1})
        unknown = by_table(report['fragmentation'])['t2']
        self.assertEqual(unknown['status'], 'unknown')
        self.assertTrue(any('deadline reached' in note for note in report['notes']))

    def test_tables_cli_gate_and_explicit_override(self):
        for engine in ('mysql', 'mariadb'):
            conn = TestSchemaPicker().catalog_conn(engine, 5000)
            code, out, err, _ = run_cli('tables', ['--schema', 'shop', '-p' + PASSWORD], conn)
            self.assertEqual(code, 2)
            self.assertIn('5,000 tables', err)
            self.assertFalse(any('STATISTICS' in sql for sql, _ in conn.calls))
            conn = TestSampling().conn(engine)
            code, out, err, _ = run_cli('tables', ['--all-schemas', '--json', '-p' + PASSWORD], conn)
            self.assertEqual(code, 0, err)
            self.assertFalse(any('COUNT(*)' in sql for sql, _ in conn.calls))

    def test_tables_http_gate_for_selected_schema(self):
        conn = TestSchemaPicker().catalog_conn('mysql', 5000)
        with mock.patch('myflames.connector.MySQLConnection', FakeFactory(conn)):
            with self.assertRaisesRegex(ValueError, r'selected scope \(schema shop\) has [\d,]+ tables'):
                ui.api_response('/api/live/tables',
                                {'connection': {'host': 'db', 'user': 'app', 'password': PASSWORD}, 'schemas': ['shop']},
                                workspace=object())
        self.assertEqual(len([sql for sql, _ in conn.calls if 'COUNT(*)' in sql]), 1)


    def test_reader_crlf_preserves_value_carriage_returns(self):
        class Conn(object):
            def run(self, sql, **kwargs):
                return 'performance_schema\tON\r\nuserstat\tON\r\ntext\ta\rb\\r\r\nnull\tNULL\r\n'
        reader = perf_common.Reader(Conn(), '')
        pairs = reader.pairs('SHOW GLOBAL VARIABLES')
        self.assertTrue(Server(pairs, {}).ps_on)
        self.assertTrue(Server(pairs, {}).userstat)
        self.assertEqual(pairs['text'], 'a\rb\\r')
        self.assertIsNone(pairs['null'])
        self.assertEqual(reader.rows('SELECT 1')[-1], ['null', None])


if __name__ == '__main__':
    unittest.main()
