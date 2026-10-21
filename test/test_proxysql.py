"""ProxySQL Admin reads and reviewed changes (myflames/proxysql.py,
myflames/reviewed_writes.py, /api/live/proxysql, /api/live/proxysql/plan and
/api/live/proxysql/action).

FakeAdmin stands in for ProxySQL Admin with SQLite, as Admin itself is: the
main, disk and stats schemas, runtime_* copies, LOAD and SAVE copying tables
between them, the exact-text match Admin applies to LOAD and SAVE (a leading
comment is a syntax error, as seen live on 3.0.11), and the client's "at line
N" error. Its tables and rows follow the live lab (ProxySQL 3.0.11 in front of
a MariaDB Galera cluster and a MySQL InnoDB Cluster, 2026-10-20). Every test
runs with a temporary HOME and the vault backend.
"""
import json
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from myflames import dbops, proxysql, reviewed_writes, ui
from myflames.connector import ConnectorError

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / 'docs' / 'schemas' / 'proxysql-1.0.json').read_text(encoding='utf-8'))
VERSION = '3.0.11-797-g7c91137'
SERVER_COLUMNS = ('hostgroup_id INT, hostname VARCHAR, port INT, gtid_port INT, status VARCHAR, weight INT, '
                  'compression INT, max_connections INT, max_replication_lag INT, use_ssl INT, max_latency_ms INT, '
                  'comment VARCHAR, PRIMARY KEY (hostgroup_id, hostname, port)')
HOSTGROUP_COLUMNS = ('writer_hostgroup INT PRIMARY KEY, backup_writer_hostgroup INT, reader_hostgroup INT, '
                     'offline_hostgroup INT, active INT, max_writers INT, writer_is_also_reader INT, '
                     'max_transactions_behind INT, comment VARCHAR')
RULE_COLUMNS = ('rule_id INT PRIMARY KEY, active INT, username VARCHAR, schemaname VARCHAR, flagIN INT, '
                'match_digest VARCHAR, match_pattern VARCHAR, negate_match_pattern INT, destination_hostgroup INT, '
                'apply INT, comment VARCHAR')
#: Tables of the servers module the lab leaves empty (3.0.11 has them all).
EMPTY_TABLES = (
    ('mysql_aws_aurora_hostgroups', 'writer_hostgroup INT, reader_hostgroup INT, active INT, aurora_port INT, '
                                    'domain_name VARCHAR, comment VARCHAR'),
    ('mysql_hostgroup_attributes', 'hostgroup_id INT, max_num_online_servers INT, comment VARCHAR'),
    ('mysql_servers_ssl_params', 'hostname VARCHAR, port INT, username VARCHAR, ssl_ca VARCHAR, comment VARCHAR'),
)
CONFIG = [
    (10, 'mf-galera-1', 3306, 0, 'ONLINE', 1000, 0, 1000, 0, 0, 0, 'galera-1'),
    (10, 'mf-galera-2', 3306, 0, 'ONLINE', 100, 0, 1000, 0, 0, 0, 'galera-2'),
    (10, 'mf-galera-3', 3306, 0, 'ONLINE', 100, 0, 1000, 0, 0, 0, 'galera-3'),
    (30, 'mf-ic-1', 3306, 0, 'ONLINE', 100, 0, 1000, 0, 1, 0, 'ic-1'),
    (30, 'mf-ic-2', 3306, 0, 'ONLINE', 100, 0, 1000, 0, 1, 0, 'ic-2'),
    (30, 'mf-ic-3', 3306, 0, 'ONLINE', 100, 0, 1000, 0, 1, 0, 'ic-3'),
]
GALERA = (10, 11, 12, 19, 1, 1, 2, 100, 'one writer')
GR = (30, 31, 32, 39, 1, 1, 0, 100, 'innodb cluster')
RULES = [
    (10, 1, None, None, 0, None, r'^/\* galera \*/', 0, 10, 1, 'galera'),
    (20, 1, None, None, 0, '^SELECT .* FOR UPDATE', None, 0, 30, 1, 'locking reads'),
    (30, 1, None, None, 0, '^SELECT', None, 0, 32, 1, 'reads'),
]


def place(config):
    """Runtime as the 3.0.11 monitors placed the lab's servers: Galera keeps
    max_writers (1) writer ONLINE by weight, the others SHUNNED in the writer
    hostgroup and ONLINE as backup writers and readers (writer_is_also_reader
    2); Group Replication keeps the primary (mf-ic-1) in 30, the secondaries in
    32. OFFLINE_HARD leaves runtime; Group Replication keeps OFFLINE_SOFT,
    Galera does not."""
    out = []
    galera = [r for r in config if r[0] == 10 and r[4] != 'OFFLINE_HARD']
    writer = max(galera, key=lambda r: r[5]) if galera else None
    for r in galera:
        if r is writer:
            out.append((10,) + r[1:4] + ('ONLINE',) + r[5:])
        else:
            out.append((10,) + r[1:4] + ('SHUNNED',) + r[5:])
            out.append((11,) + r[1:4] + ('ONLINE',) + r[5:])
            out.append((12,) + r[1:4] + ('ONLINE',) + r[5:])
    for r in config:
        if r[0] == 30 and r[4] != 'OFFLINE_HARD':
            hg = 30 if r[1] == 'mf-ic-1' else 32
            out.append((hg,) + r[1:4] + ('OFFLINE_SOFT' if r[4] == 'OFFLINE_SOFT' else 'ONLINE',) + r[5:])
        elif r[0] not in (10, 30) and r[4] != 'OFFLINE_HARD':
            out.append(r)
    return out


class FakeAdmin(object):
    """ProxySQL Admin over SQLite (see the module docstring)."""

    def __init__(self, version=VERSION, missing=(), fail_line=None, fail_text='ProxySQL Admin Error: boom',
                 timeout_line=None, pool_columns=None, missing_text='ProxySQL Admin Error: no such table: %s'):
        self.version = version
        self.missing_text = missing_text
        self.db = sqlite3.connect(':memory:')
        for schema in ('disk', 'stats'):
            self.db.execute("ATTACH DATABASE ':memory:' AS %s" % schema)
        self.missing = set(missing)
        self.fail_line, self.fail_text, self.timeout_line = fail_line, fail_text, timeout_line
        self.calls, self.writes = [], []
        for schema in ('main', 'disk'):
            self.db.execute('CREATE TABLE %s.mysql_servers (%s)' % (schema, SERVER_COLUMNS))
            for t in ('mysql_galera_hostgroups', 'mysql_group_replication_hostgroups'):
                self.db.execute('CREATE TABLE %s.%s (%s)' % (schema, t, HOSTGROUP_COLUMNS))
            self.db.execute('CREATE TABLE %s.mysql_replication_hostgroups (writer_hostgroup INT PRIMARY KEY, '
                            'reader_hostgroup INT, check_type VARCHAR, comment VARCHAR)' % schema)
            self.db.execute('CREATE TABLE %s.mysql_query_rules (%s)' % (schema, RULE_COLUMNS))
            self.db.execute('CREATE TABLE %s.mysql_query_rules_fast_routing (username VARCHAR, schemaname '
                            'VARCHAR, flagIN INT, destination_hostgroup INT, comment VARCHAR)' % schema)
            for t, cols in EMPTY_TABLES:
                self.db.execute('CREATE TABLE %s.%s (%s)' % (schema, t, cols))
        for t, cols in (('mysql_servers', SERVER_COLUMNS), ('mysql_galera_hostgroups', HOSTGROUP_COLUMNS),
                        ('mysql_group_replication_hostgroups', HOSTGROUP_COLUMNS),
                        ('mysql_replication_hostgroups', 'writer_hostgroup INT, reader_hostgroup INT, '
                                                         'check_type VARCHAR, comment VARCHAR'),
                        ('mysql_query_rules', RULE_COLUMNS),
                        ('mysql_query_rules_fast_routing', 'username VARCHAR, schemaname VARCHAR, flagIN INT, '
                                                           'destination_hostgroup INT, comment VARCHAR')):
            plain = cols.replace(', PRIMARY KEY (hostgroup_id, hostname, port)', '').replace(' PRIMARY KEY', '')
            self.db.execute('CREATE TABLE main.runtime_%s (%s)' % (t, plain))
        for t, cols in EMPTY_TABLES:
            self.db.execute('CREATE TABLE main.runtime_%s (%s)' % (t, cols))
        self.db.execute('CREATE TABLE main.global_variables (variable_name VARCHAR, variable_value VARCHAR)')
        self.db.execute("INSERT INTO main.global_variables VALUES ('admin-version', ?)", (version,))
        self.db.execute('CREATE TABLE stats.stats_mysql_global (Variable_Name VARCHAR, Variable_Value VARCHAR)')
        for name, value in (('ProxySQL_Uptime', '187200'), ('Client_Connections_connected', '4'),
                            ('Questions', '1200')):
            self.db.execute('INSERT INTO stats.stats_mysql_global VALUES (?, ?)', (name, value))
        cols = pool_columns or ('hostgroup', 'srv_host', 'srv_port', 'status', 'ConnUsed', 'ConnFree', 'ConnOK',
                                'ConnERR', 'MaxConnUsed', 'Queries', 'Queries_GTID_sync', 'Bytes_data_sent',
                                'Bytes_data_recv', 'Latency_us')
        self.pool_columns = cols
        self.db.execute('CREATE TABLE stats.stats_mysql_connection_pool (%s)' % ', '.join(cols))
        self.db.execute('CREATE TABLE stats.stats_mysql_query_rules (rule_id INT, hits INT)')
        self.db.execute('CREATE TABLE stats.stats_mysql_query_digest (hostgroup INT, schemaname VARCHAR, '
                        'username VARCHAR, client_address VARCHAR, digest VARCHAR, digest_text VARCHAR, '
                        'count_star INT, first_seen INT, last_seen INT, sum_time INT, min_time INT, max_time INT, '
                        'sum_rows_affected INT, sum_rows_sent INT)')
        for schema in ('main', 'disk'):
            self.db.executemany('INSERT INTO %s.mysql_servers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)' % schema, CONFIG)
            self.db.execute('INSERT INTO %s.mysql_galera_hostgroups VALUES (?,?,?,?,?,?,?,?,?)' % schema, GALERA)
            self.db.execute('INSERT INTO %s.mysql_group_replication_hostgroups VALUES (?,?,?,?,?,?,?,?,?)'
                            % schema, GR)
            self.db.executemany('INSERT INTO %s.mysql_query_rules VALUES (?,?,?,?,?,?,?,?,?,?,?)' % schema, RULES)
        self.load('servers')
        self.load('query_rules')
        self.db.executemany('INSERT INTO stats.stats_mysql_query_rules VALUES (?, ?)', [(10, 6), (20, 5), (30, 41)])
        self.db.execute("INSERT INTO stats.stats_mysql_query_digest VALUES (32, 'testdb', 'mf_admin', '', "
                        "'0x9C01', 'SELECT COUNT(*) FROM orders WHERE status = ?', 20, 1, 2, 7706, 78, 5974, 0, 20)")

    # -- Admin behaviour ------------------------------------------------------------------
    def load(self, module):
        if module == 'servers':
            self.db.execute('DELETE FROM main.runtime_mysql_servers')
            config = self.db.execute('SELECT * FROM main.mysql_servers').fetchall()
            self.db.executemany('INSERT INTO main.runtime_mysql_servers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                                place(config))
            tables = ('mysql_galera_hostgroups', 'mysql_group_replication_hostgroups', 'mysql_replication_hostgroups')
            self.db.execute('DELETE FROM stats.stats_mysql_connection_pool')
            for i, r in enumerate(place(config)):
                values = dict(hostgroup=r[0], srv_host=r[1], srv_port=r[2], status=r[4], ConnUsed=i % 3, ConnFree=1,
                              ConnOK=1, ConnERR=0, MaxConnUsed=1, Queries=10 * i, Queries_GTID_sync=0,
                              Bytes_data_sent=400, Bytes_data_recv=67, BytesSent=1, Latency_us=450)
                cols = self.pool_columns
                self.db.execute('INSERT INTO stats.stats_mysql_connection_pool VALUES (%s)' % ','.join('?' * len(cols)),
                                [values.get(c) for c in cols])
        else:
            tables = ('mysql_query_rules', 'mysql_query_rules_fast_routing')
        for t in tables:
            self.db.execute('DELETE FROM main.runtime_%s' % t)
            # Runtime holds only active rules (ProxySQL_Admin.cpp: WHERE active=1).
            where = ' WHERE active = 1' if t == 'mysql_query_rules' else ''
            self.db.execute('INSERT INTO main.runtime_%s SELECT * FROM main.%s%s' % (t, t, where))

    def save(self, module):
        tables = (('mysql_servers', 'mysql_galera_hostgroups', 'mysql_group_replication_hostgroups',
                   'mysql_replication_hostgroups') if module == 'servers'
                  else ('mysql_query_rules', 'mysql_query_rules_fast_routing'))
        for t in tables:
            self.db.execute('DELETE FROM disk.%s' % t)
            self.db.execute('INSERT INTO disk.%s SELECT * FROM main.%s' % (t, t))

    def _error(self, line, text):
        return ConnectorError('mysql client exited 1: ERROR 1045 (28000) at line %d: %s' % (line, text))

    def _execute(self, sql, line=1):
        body = sql.strip()
        for name in self.missing:
            if name in body:
                raise self._error(line, self.missing_text % name if '%s' in self.missing_text else self.missing_text)
        commands = {proxysql.LOAD['servers']: ('load', 'servers'), proxysql.LOAD['query_rules']: ('load', 'query_rules'),
                    proxysql.SAVE['servers']: ('save', 'servers'), proxysql.SAVE['query_rules']: ('save', 'query_rules')}
        if body in commands:
            what, module = commands[body]
            getattr(self, what)(module)
            return []
        if re.search(r'\b(LOAD|SAVE)\b', body):
            # Admin matches LOAD and SAVE on the whole text; anything else goes to SQLite.
            raise self._error(line, 'ProxySQL Admin Error: near "%s": syntax error' % body.split()[0])
        try:
            cur = self.db.execute(body.rstrip(';'))
        except sqlite3.Error as exc:
            raise self._error(line, 'ProxySQL Admin Error: %s' % exc)
        if cur.description is None:
            return []
        return [[d[0] for d in cur.description]] + [list(r) for r in cur.fetchall()]

    @staticmethod
    def _xml(sql, rows):
        """The mysql client's --xml output (client/mysql.cc
        print_table_data_xml): SQL NULL is xsi:nil, any text, 'NULL' too, is
        the field's content; an empty result prints no row."""
        def enc(v):
            return str(v).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
        out = ['<?xml version="1.0"?>\n\n<resultset statement="%s\n" '
               'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">' % enc(sql)]
        for row in rows[1:]:
            out.append('\n  <row>\n')
            for name, value in zip(rows[0], row):
                out.append('\t<field name="%s" xsi:nil="true" />\n' % enc(name) if value is None else
                           '\t<field name="%s">%s</field>\n' % (enc(name), enc(value)))
            out.append('  </row>\n')
        out.append('</resultset>\n')
        return ''.join(out)

    # -- the connection API proxysql.py uses -------------------------------------------------
    def run(self, sql, extra_flags=None, timeout=None):
        self.calls.append(sql)
        hook = getattr(self, 'on_read', None)
        if hook is not None:
            self.on_read = None
            hook()
        if sql == 'SELECT @@version':
            return self.version + '\n'
        if '--xml' not in (extra_flags or ()):
            raise AssertionError('Admin tables are read as XML, which tells NULL from the text NULL: %s' % sql)
        return self._xml(sql, self._execute(sql))

    def run_write(self, sql, extra_flags=None, timeout=None):
        self.writes.append(sql)
        for line, statement in enumerate(sql.split('\n'), 1):
            if self.timeout_line == line:
                raise ConnectorError('Query timed out after 60s; the outcome is unknown.')
            if self.fail_line == line:
                raise self._error(line, self.fail_text)
            self._execute(statement, line)
        return ''

    def rows(self, sql):
        return self.db.execute(sql).fetchall()

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Factory(object):
    def __init__(self, conn):
        self.conn, self.options = conn, []

    def __call__(self, **options):
        self.options.append(options)
        return self.conn


class NotProxy(object):
    """A MySQL server, even one whose default schema has a mysql_servers table."""

    def __init__(self, code=1146):
        self.code = code
        self.calls = []

    def run(self, sql, extra_flags=None, timeout=None):
        self.calls.append(sql)
        if 'mysql_servers' in sql:
            return 'hostgroup_id\n1\n'
        if sql == 'SELECT @@version':
            return '8.4.11\n'
        raise ConnectorError("mysql client exited 1: ERROR %d (42S02) at line 1: Table 'app.global_variables' "
                             "doesn't exist" % self.code)

    def run_write(self, sql, extra_flags=None, timeout=None):
        raise AssertionError('a write reached a server that is not ProxySQL')

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


class Home(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {'HOME': self.tmp.name, 'USERPROFILE': self.tmp.name,
                                               'MYFLAMES_SECRET_BACKEND': 'vault'})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)


class TestRead(Home):
    def test_reads_the_lab_shape_and_validates(self):
        from test_chart_data import validate
        conn = FakeAdmin()
        report = proxysql.read_status(conn)
        validate(report, SCHEMA)
        self.assertEqual((report['is_proxysql'], report['version'], report['uptime_s']), (True, VERSION, 187200))
        self.assertEqual(report['summary'], {'backends': 10, 'ONLINE': 8, 'SHUNNED': 2, 'OFFLINE_SOFT': 0,
                                             'OFFLINE_HARD': 0, 'not_in_runtime': 0})
        ic2 = next(s for s in report['servers'] if s['key'] == 'rt|32|mf-ic-2|3306')
        # The pool's Bytes_data_* columns (Gemini read BytesSent and lost the whole pool).
        self.assertEqual((ic2['pool']['bytes_sent'], ic2['pool']['latency_us']), (400, 450))
        self.assertEqual((ic2['config_key'], ic2['role'], ic2['monitor']), ([30, 'mf-ic-2', 3306], 'reader',
                                                                           'group_replication'))
        standby = next(s for s in report['servers'] if s['key'] == 'rt|10|mf-galera-2|3306')
        self.assertEqual((standby['runtime_status'], standby['status_detail'], standby['tone']),
                         ('SHUNNED', 'standby writer', 'ok'))
        self.assertEqual(report['pending'], {'servers': {'runtime': [], 'disk': []},
                                             'query_rules': {'runtime': [], 'disk': []}})
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(report['notes'], [])
        self.assertEqual([(r['rule_id'], r['hits'], r['runtime_active']) for r in report['query_rules']],
                         [(10, 6, True), (20, 5, True), (30, 41, True)])
        self.assertEqual(report['query_digests'][0]['avg_time_us'], 385)
        self.assertFalse(conn.writes, 'reading sends no write')
        for sql in conn.calls:
            self.assertTrue(sql.startswith('SELECT'), sql)

    def test_missing_tables_and_columns_are_unknown_not_a_crash(self):
        # 2.x without mysql_servers_ssl_params, a stats-only account, an older pool.
        conn = FakeAdmin(missing=('stats_mysql_query_digest', 'stats_mysql_query_rules', 'mysql_servers_ssl_params'),
                         pool_columns=('hostgroup', 'srv_host', 'srv_port', 'status', 'ConnUsed', 'ConnFree',
                                       'ConnOK', 'ConnERR', 'Queries'))
        report = proxysql.read_status(conn)
        self.assertTrue(report['is_proxysql'])
        self.assertEqual(report['query_digests'], [])
        self.assertIsNone(report['query_rules'][0]['hits'])
        pool = report['servers'][0]['pool']
        self.assertNotIn('bytes_sent', pool)
        self.assertTrue(any('stats_mysql_query_digest' in n for n in report['notes']))
        self.assertEqual(report['pending']['servers']['runtime'], [], 'a secondary table missing on both sides is skipped')
        stats_only = FakeAdmin(missing=('main.mysql_servers', 'main.mysql_query_rules'))
        report = proxysql.read_status(stats_only)
        self.assertFalse(report['config_readable'])
        self.assertEqual(report['actions'], [])
        self.assertTrue(all(not s['actions'] for s in report['servers']))
        self.assertIsNone(report['pending']['servers']['runtime'])

    def test_missing_status_or_weight_columns_offer_nothing_that_needs_them(self):
        # Codex review: an absent status column crashed the comparison; an absent weight offered Change weight.
        conn = FakeAdmin()
        conn.db.execute('CREATE TABLE main.nostatus AS SELECT hostgroup_id, hostname, port, gtid_port, weight, '
                        'compression, max_connections, max_replication_lag, use_ssl, max_latency_ms, comment '
                        'FROM main.runtime_mysql_servers')
        conn.db.execute('DROP TABLE main.runtime_mysql_servers')
        conn.db.execute('ALTER TABLE main.nostatus RENAME TO runtime_mysql_servers')
        conn.db.execute('CREATE TABLE main.noweight AS SELECT hostgroup_id, hostname, port, gtid_port, status, '
                        'compression, max_connections, max_replication_lag, use_ssl, max_latency_ms, comment '
                        'FROM main.mysql_servers')
        conn.db.execute('DROP TABLE main.mysql_servers')
        conn.db.execute('ALTER TABLE main.noweight RENAME TO mysql_servers')
        report = proxysql.read_status(conn)
        for s in report['servers']:
            self.assertNotIn('server_weight', [a['id'] for a in s['actions']], s['key'])

    def test_a_mysql_server_with_a_mysql_servers_table_is_not_proxysql(self):
        for code in (1146, 1046):
            report = proxysql.read_status(NotProxy(code))
            self.assertFalse(report['is_proxysql'])
            self.assertEqual(report['actions'], [])

    def test_identity_needs_the_echoed_version(self):
        conn = FakeAdmin()
        conn.version = 'other'
        self.assertEqual(proxysql.identify(conn)[0], False)

    def test_a_connection_failure_is_an_error(self):
        class Down(object):
            def run(self, sql, extra_flags=None, timeout=None):
                raise ConnectorError("mysql client exited 1: ERROR 2003 (HY000): Can't connect to MySQL server")
        with self.assertRaises(ConnectorError):
            proxysql.read_status(Down())

    def test_pending_changes_follow_the_monitors(self):
        conn = FakeAdmin()
        conn.db.execute("UPDATE main.mysql_servers SET weight = 7 WHERE hostname = 'mf-ic-2'")
        conn.db.execute("UPDATE main.mysql_query_rules SET active = 0 WHERE rule_id = 30")
        report = proxysql.read_status(conn)
        self.assertEqual(report['pending']['servers']['runtime'], ['hostgroup 30, mf-ic-2:3306: weight 100 → 7'])
        self.assertEqual(report['pending']['servers']['disk'], ['hostgroup 30, mf-ic-2:3306: weight 100 → 7'])
        self.assertEqual(report['pending']['query_rules']['runtime'], ['rule 30: active 1 → 0'])
        # OFFLINE_SOFT under Galera is put back in traffic by the monitor: not pending forever.
        conn = FakeAdmin()
        conn.db.execute("UPDATE main.mysql_servers SET status = 'OFFLINE_SOFT' WHERE hostname = 'mf-galera-3'")
        conn.load('servers')
        self.assertEqual(proxysql.read_status(conn)['pending']['servers']['runtime'], [])

    def test_a_runtime_row_the_monitor_keeps_is_pending(self):
        # Live on 3.0.11: a row moved from 30 to 50 stayed ONLINE in reader hostgroup 32 too.
        conn = FakeAdmin()
        conn.db.execute("UPDATE main.mysql_servers SET hostgroup_id = 50 WHERE hostname = 'mf-ic-3'")
        conn.db.execute("INSERT INTO main.runtime_mysql_servers VALUES (50, 'mf-ic-3', 3306, 0, 'ONLINE', 100, 0, 1000, "
                        "0, 1, 0, 'ic-3')")
        report = proxysql.read_status(conn)
        self.assertEqual(report['pending']['servers']['runtime'], [
            "hostgroup 32, mf-ic-3:3306: in runtime (the Group Replication monitor's hostgroups), not in memory"])

    def test_offline_hard_configuration_is_shown_without_runtime(self):
        conn = FakeAdmin()
        conn.db.execute("UPDATE main.mysql_servers SET status = 'OFFLINE_HARD' WHERE hostname = 'mf-ic-3'")
        conn.load('servers')
        report = proxysql.read_status(conn)
        row = next(s for s in report['servers'] if s['hostname'] == 'mf-ic-3')
        self.assertEqual((row['key'], row['runtime_status'], row['tone']), ('cfg|30|mf-ic-3|3306', None, 'watch'))
        self.assertEqual(report['pending']['servers']['runtime'], [])

    def test_hostgroup_without_online_server_and_rule_to_nowhere_are_problems(self):
        conn = FakeAdmin()
        conn.db.execute("UPDATE main.mysql_servers SET status = 'OFFLINE_SOFT' WHERE hostname IN ('mf-ic-2', 'mf-ic-3')")
        conn.db.execute("INSERT INTO main.mysql_query_rules VALUES (40, 1, NULL, NULL, 0, '^INSERT', NULL, 0, 99, 1, '')")
        conn.load('servers')
        conn.load('query_rules')
        report = proxysql.read_status(conn)
        self.assertEqual(report['status'], 'problem')
        self.assertIn('No server is ONLINE in hostgroup 32, so ProxySQL cannot send its traffic anywhere.',
                      report['status_why'])
        self.assertIn('Active rule 40 sends to hostgroup 99, which has no server in runtime.', report['status_why'])

    def test_routing_is_judged_by_the_rules_proxysql_runs(self):
        # Codex review: rule 40 routes to an empty hostgroup in runtime while memory already has it inactive.
        conn = FakeAdmin()
        conn.db.execute("INSERT INTO main.mysql_query_rules VALUES (40, 1, NULL, NULL, 0, '^INSERT', NULL, 0, 99, 1, '')")
        conn.load('query_rules')
        conn.db.execute("UPDATE main.mysql_query_rules SET active = 0 WHERE rule_id = 40")
        report = proxysql.read_status(conn)
        self.assertEqual(report['status'], 'problem')
        self.assertIn('Active rule 40 sends to hostgroup 99, which has no server in runtime.', report['status_why'])
        self.assertEqual(report['pending']['query_rules']['runtime'], ['rule 40: active 1 → 0'])
        # Deleted from memory and still in runtime: still routing.
        conn.db.execute("DELETE FROM main.mysql_query_rules WHERE rule_id = 40")
        self.assertEqual(proxysql.read_status(conn)['status'], 'problem')
        # Active in memory but not loaded yet: it routes nothing, the pending change says so.
        conn = FakeAdmin()
        conn.db.execute("INSERT INTO main.mysql_query_rules VALUES (40, 1, NULL, NULL, 0, '^INSERT', NULL, 0, 99, 1, '')")
        report = proxysql.read_status(conn)
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(len(report['pending']['query_rules']['runtime']), 1)

    def test_unread_runtime_routing_is_unknown(self):
        conn = FakeAdmin(missing=('runtime_mysql_query_rules',))
        report = proxysql.read_status(conn)
        self.assertEqual(report['status'], 'unknown')
        self.assertTrue(any('could not be read (runtime_mysql_query_rules)' in w for w in report['status_why']),
                        report['status_why'])
        # A problem still shows over unknown.
        conn = FakeAdmin(missing=('runtime_mysql_query_rules',))
        conn.db.execute("UPDATE main.mysql_servers SET status = 'OFFLINE_SOFT' WHERE hostname IN ('mf-ic-2', 'mf-ic-3')")
        conn.load('servers')
        self.assertEqual(proxysql.read_status(conn)['status'], 'problem')
        # Neither runtime_mysql_servers nor the pool: no hostgroup is known to have a server.
        conn = FakeAdmin(missing=('runtime_mysql_servers', 'stats_mysql_connection_pool'))
        report = proxysql.read_status(conn)
        self.assertEqual(report['status'], 'unknown')
        self.assertFalse(any(w.startswith('Active rule') for w in report['status_why']), report['status_why'])

    def test_sql_null_and_the_text_null_are_different_values(self):
        # Codex review: batch output prints both as NULL; the client's XML output does not.
        conn = FakeAdmin()
        conn.db.execute("UPDATE main.mysql_query_rules SET username = 'NULL', comment = '' WHERE rule_id = 20")
        report = proxysql.read_status(conn)
        rules = dict((r['rule_id'], r) for r in report['query_rules'])
        self.assertIsNone(rules[10]['username'])
        self.assertEqual(rules[20]['username'], 'NULL')
        self.assertEqual(rules[20]['fields']['comment'], '')
        self.assertIn("rule 20: username NULL → 'NULL', comment locking reads → ''",
                      report['pending']['query_rules']['runtime'])
        self.assertNotEqual(report['fingerprints']['query_rules_memory'], report['fingerprints']['query_rules_disk'])
        self.assertTrue(all(sql.startswith('SELECT') for sql in conn.calls))

    def test_a_value_the_client_cannot_write_as_xml_is_unread_never_misread(self):
        conn = FakeAdmin()
        conn.db.execute("UPDATE main.mysql_query_rules SET comment = 'a' || char(1) || 'b' WHERE rule_id = 30")
        report = proxysql.read_status(conn)
        self.assertFalse(report['config_readable'])
        self.assertEqual(report['actions'], [])
        self.assertIn('Not read: main.mysql_query_rules (a value the client could not print as XML (such as a control '
                      'character)).', report['notes'])

    def test_a_table_is_absent_only_when_every_copy_says_no_such_table(self):
        # Codex review: memory and runtime unread, disk readable, was taken for a table this version lacks.
        conn = FakeAdmin(missing=('main.mysql_query_rules_fast_routing', 'runtime_mysql_query_rules_fast_routing'))
        conn.db.execute("INSERT INTO main.mysql_query_rules_fast_routing VALUES ('app', 'db', 0, 99, '')")
        report = proxysql.read_status(conn)
        self.assertIsNone(report['pending']['query_rules']['runtime'])
        self.assertEqual([a['id'] for a in report['query_rules'][0]['actions']], [])
        self.assertEqual([a['id'] for a in report['actions']], ['servers_load', 'servers_save'])
        for action, target in (('rule_disable', {'rule_id': 30}), ('query_rules_load', None), ('query_rules_save', None)):
            with self.assertRaises(ValueError, msg=action):
                proxysql.plan_action(report, action, target)
        # Every copy unreadable for another reason (not "no such table"): unknown too.
        conn = FakeAdmin(missing=('mysql_servers_ssl_params',), missing_text='ProxySQL Admin Error: database is locked')
        report = proxysql.read_status(conn)
        self.assertIsNone(report['pending']['servers']['runtime'])
        self.assertIsNone(report['pending']['servers']['disk'])
        with self.assertRaises(ValueError):
            proxysql.plan_action(report, 'server_weight', {'config': [30, 'mf-ic-2', 3306]}, 50)
        self.assertTrue(all(s['actions'] == [] for s in report['servers']))
        self.assertEqual([a['id'] for a in report['actions']], ['query_rules_load', 'query_rules_save'])
        # Every copy "no such table" (a version without it): passed over.
        conn = FakeAdmin(missing=('mysql_servers_ssl_params',))
        report = proxysql.read_status(conn)
        self.assertEqual(report['pending']['servers'], {'runtime': [], 'disk': []})
        proxysql.plan_action(report, 'server_weight', {'config': [30, 'mf-ic-2', 3306]}, 50)


class TestActionsOffered(Home):
    def setUp(self):
        super().setUp()
        self.report = proxysql.read_status(FakeAdmin())

    def offered(self, key):
        return [a['id'] for a in next(s for s in self.report['servers'] if s['key'] == key)['actions']]

    def test_galera_servers_get_no_offline_soft_and_monitored_rows_no_move(self):
        self.assertEqual(self.offered('rt|11|mf-galera-2|3306'), ['server_offline_hard', 'server_weight'])
        self.assertEqual(self.offered('rt|32|mf-ic-2|3306'), ['server_offline_soft', 'server_offline_hard',
                                                                'server_weight'])
        galera = next(s for s in self.report['servers'] if s['key'] == 'rt|11|mf-galera-2|3306')
        self.assertEqual(len(galera['action_notes']), 2)
        ic = next(s for s in self.report['servers'] if s['key'] == 'rt|32|mf-ic-2|3306')
        self.assertIn('Move to hostgroup is not offered', ic['action_notes'][0])

    def test_rules_and_modules(self):
        self.assertEqual([a['id'] for a in self.report['query_rules'][0]['actions']], ['rule_disable'])
        self.assertEqual([a['id'] for a in self.report['actions']],
                         ['servers_load', 'servers_save', 'query_rules_load', 'query_rules_save'])

    def test_unsafe_host_names_offer_nothing(self):
        conn = FakeAdmin()
        conn.db.execute("INSERT INTO main.mysql_servers VALUES (50, 'db''1', 3306, 0, 'ONLINE', 1, 0, 10, 0, 0, 0, '')")
        conn.db.execute("INSERT INTO main.mysql_servers VALUES (50, 'db\\1', 3306, 0, 'ONLINE', 1, 0, 10, 0, 0, 0, '')")
        conn.load('servers')
        report = proxysql.read_status(conn)
        for s in report['servers']:
            if s['hostgroup_id'] == 50:
                self.assertEqual(s['actions'], [], s['hostname'])
        with self.assertRaises(ValueError):
            proxysql.plan_action(report, 'server_offline_soft', {'config': [50, "db'1", 3306]})

    def test_targets_of_the_wrong_type_offer_nothing(self):
        for target in ('rule_id', ['config'], 5, {'rule_id': True}, {'rule_id': '10'}, {'config': [30, 'mf-ic-2']}):
            self.assertEqual(proxysql.available_actions(self.report, target), [], target)


class TestPlans(Home):
    def setUp(self):
        super().setUp()
        self.conn = FakeAdmin()
        self.report = proxysql.read_status(self.conn)

    def plan(self, action, target=None, value=None):
        return proxysql.plan_action(self.report, action, target, value)

    def test_status_statements_are_exact_and_load_is_bare(self):
        plan = self.plan('server_offline_soft', {'config': [30, 'mf-ic-2', 3306]})
        self.assertEqual(plan['statements'], [
            "/*myflames:proxysql_op*/ UPDATE mysql_servers SET status = 'OFFLINE_SOFT' WHERE hostgroup_id = 30 AND "
            "hostname = 'mf-ic-2' AND port = 3306;",
            'LOAD MYSQL SERVERS TO RUNTIME;'])
        self.assertNotIn('SAVE', ' '.join(plan['statements']), 'saving to disk is its own reviewed step')
        self.assertEqual(plan['next'], {'action': 'servers_save', 'label': 'Save servers to disk…'})
        self.assertIsNone(plan['typed_guard'])
        self.assertIn('hostgroup 32 ONLINE', dict(plan['facts'])['Runtime'])

    def test_offline_hard_and_last_online_need_the_typed_name(self):
        self.assertEqual(self.plan('server_offline_hard', {'config': [30, 'mf-ic-2', 3306]})['typed_guard'],
                         'mf-ic-2:3306')
        plan = self.plan('server_offline_soft', {'config': [30, 'mf-ic-1', 3306]})
        self.assertEqual(plan['typed_guard'], 'mf-ic-1:3306')
        self.assertTrue(any('only ONLINE server in hostgroup 30' in w for w in plan['warnings']))

    def test_values_are_validated_never_defaulted(self):
        target = {'config': [30, 'mf-ic-2', 3306]}
        for value in (None, 'abc', '5', True, 1.5, -1, proxysql.WEIGHT_MAX + 1, 100):
            with self.assertRaises(ValueError, msg=repr(value)):
                self.plan('server_weight', target, value)
        plan = self.plan('server_weight', target, 0)
        self.assertEqual(plan['statements'][0], "/*myflames:proxysql_op*/ UPDATE mysql_servers SET weight = 0 WHERE "
                                                "hostgroup_id = 30 AND hostname = 'mf-ic-2' AND port = 3306;")
        self.assertEqual(plan['value'], 0)
        # A monitored row is never moved: it would stay in the monitor's runtime hostgroups (live, 3.0.11).
        with self.assertRaises(ValueError):
            self.plan('server_hostgroup', target, 50)
        self.assertIsNone(self.plan('server_offline_soft', target, 7)['value'], 'no value on a status change')
        self.conn.db.execute("INSERT INTO main.mysql_servers VALUES (50, 'mf-app-1', 3306, 0, 'ONLINE', 1, 0, 100, 0, 0, "
                             "0, 'app')")
        self.conn.db.execute("INSERT INTO main.mysql_servers VALUES (60, 'mf-app-1', 3306, 0, 'ONLINE', 1, 0, 100, 0, 0, "
                             "0, 'app')")
        self.conn.load('servers')
        self.report = proxysql.read_status(self.conn)
        app = {'config': [50, 'mf-app-1', 3306]}
        for value in (None, -1, 50, 60, 10.0, False, '70'):
            with self.assertRaises(ValueError, msg=repr(value)):
                self.plan('server_hostgroup', app, value)
        move = self.plan('server_hostgroup', app, 70)
        self.assertEqual(move['statements'][0], "/*myflames:proxysql_op*/ UPDATE mysql_servers SET hostgroup_id = 70 "
                                                "WHERE hostgroup_id = 50 AND hostname = 'mf-app-1' AND port = 3306;")
        self.assertIn("hostgroup_id = 70 AND hostname = 'mf-app-1'", move['undo'])
        self.assertEqual(move['typed_guard'], 'mf-app-1:3306', 'the only ONLINE server of hostgroup 50')
        into = self.plan('server_hostgroup', app, 32)
        self.assertTrue(any('Group Replication monitor' in w for w in into['warnings']))

    def test_a_disabled_rule_is_not_pending_and_can_be_enabled_again(self):
        # Live on 3.0.11: after Disable, runtime has no row for the rule.
        self.conn.db.execute("UPDATE main.mysql_query_rules SET active = 0 WHERE rule_id = 30")
        self.conn.load('query_rules')
        report = proxysql.read_status(self.conn)
        self.assertEqual(report['pending']['query_rules']['runtime'], [])
        rule = next(r for r in report['query_rules'] if r['rule_id'] == 30)
        self.assertEqual((rule['active'], rule['runtime_active']), (False, False))
        plan = proxysql.plan_action(report, 'rule_enable', {'rule_id': 30})
        self.assertEqual(plan['statements'][0], '/*myflames:proxysql_op*/ UPDATE mysql_query_rules SET active = 1 WHERE '
                                                'rule_id = 30;')

    def test_rule_toggle_names_the_rule_and_checks_it_exists(self):
        plan = self.plan('rule_disable', {'rule_id': 30})
        self.assertEqual(plan['statements'], ['/*myflames:proxysql_op*/ UPDATE mysql_query_rules SET active = 0 '
                                              'WHERE rule_id = 30;', 'LOAD MYSQL QUERY RULES TO RUNTIME;'])
        for bad in ({'rule_id': 31}, {'rule_id': 0}, None):
            with self.assertRaises(ValueError):
                self.plan('rule_disable', bad)
        with self.assertRaises(ValueError):
            self.plan('rule_enable', {'rule_id': 30})

    def test_row_changes_refuse_while_other_changes_wait(self):
        self.conn.db.execute("UPDATE main.mysql_servers SET weight = 7 WHERE hostname = 'mf-ic-3'")
        report = proxysql.read_status(self.conn)
        with self.assertRaises(ValueError) as caught:
            proxysql.plan_action(report, 'server_offline_soft', {'config': [30, 'mf-ic-2', 3306]})
        self.assertIn('mf-ic-3:3306: weight 100 → 7', str(caught.exception))
        load = proxysql.plan_action(report, 'servers_load')
        self.assertEqual(load['changes'], ['hostgroup 30, mf-ic-3:3306: weight 100 → 7'])
        self.assertEqual(load['statements'], ['LOAD MYSQL SERVERS TO RUNTIME;'])

    def test_save_lists_memory_versus_disk_and_says_memory(self):
        self.conn.db.execute("UPDATE main.mysql_query_rules SET active = 0 WHERE rule_id = 20")
        self.conn.load('query_rules')
        report = proxysql.read_status(self.conn)
        plan = proxysql.plan_action(report, 'query_rules_save')
        self.assertEqual(plan['statements'], ['SAVE MYSQL QUERY RULES TO DISK;'])
        self.assertEqual(plan['changes'], ['rule 20: active 1 → 0'])
        self.assertIn('in memory', plan['description'])
        self.assertNotIn('runtime', plan['description'].split('.')[0])
        self.assertEqual(plan['undo'], 'Cannot be undone: the disk copy before this save is replaced.')

    def test_every_statement_is_risky_for_the_policy(self):
        for action, target, value in (('server_online', None, None), ('servers_load', None, None),
                                      ('query_rules_save', None, None), ('server_weight', None, 5),
                                      ('rule_disable', {'rule_id': 10}, None)):
            if action.startswith('server_') and action != 'servers_load':
                target = {'config': [30, 'mf-ic-2', 3306]}
            if action == 'server_online':
                continue
            plan = self.plan(action, target, value)
            self.assertEqual(reviewed_writes.worst_kind(plan['statements'])['kind'], 'risky', action)
            decision = dbops.decide(reviewed_writes.worst_kind(plan['statements']), 'development')
            self.assertEqual(decision['action'], 'confirm', action)


class Endpoints(Home):
    """Through ui.api_response with a temporary workspace store."""

    def setUp(self):
        super().setUp()
        from myflames.workspace_store import WorkspaceStore
        self.workspace = type('Workspace', (), {'store': WorkspaceStore(os.path.join(self.tmp.name, 'ws.sqlite3'))})()
        self.conn = FakeAdmin()
        self.connection = {'host': '127.0.0.1', 'port': 6032, 'user': 'radmin', 'password': 'pw-secret'}

    def api(self, path, body, conn=None):
        with mock.patch('myflames.connector.MySQLConnection', Factory(conn or self.conn)):
            return ui.api_response(path, body, self.workspace)

    def plan(self, action, target=None, value=None, connection=None):
        body = {'connection': connection or self.connection, 'action': action, 'target': target}
        if value is not None:
            body['value'] = value
        return self.api('/api/live/proxysql/plan', body)

    def send(self, plan, typed=None, connection=None, token=None, confirmed=True, conn=None):
        plan = dict(plan)
        own = plan.pop('review_token', None)
        return self.api('/api/live/proxysql/action', {'connection': connection or self.connection, 'plan': plan,
                                                       'review_token': own if token is None else token,
                                                       'typed': typed, 'confirmed': confirmed}, conn)

    def audit(self):
        return dbops.audit_report(self.workspace.store)['records']

    def test_read_endpoint_and_paths(self):
        report = self.api('/api/live/proxysql', {'connection': self.connection})
        self.assertTrue(report['is_proxysql'])
        # The HTTP handler serves them (its allowlist answers 404 otherwise).
        import inspect
        source = inspect.getsource(ui.WorkspaceHandler.do_POST)
        self.assertIn('"/api/live/proxysql"', source)
        self.assertIn('_ACTION_PATHS', source)
        self.assertEqual(set(ui._ACTION_PATHS), {'/api/live/proxysql/plan', '/api/live/proxysql/action',
                                                 '/api/live/cluster/plan', '/api/live/cluster/action'})
        self.assertIn('/api/live/proxysql', ui._SERVER_VIEW_PATHS)
        self.assertIn('/api/live/proxysql/action', ui._WRITE_PATHS)
        self.assertNotIn('/api/live/proxysql/action', ui._SERVER_VIEW_PATHS)
        self.assertNotIn('/api/live/proxysql/plan', ui._WRITE_PATHS)

    def test_reviewed_statements_are_the_sent_statements(self):
        plan = self.plan('server_offline_soft', {'config': [30, 'mf-ic-2', 3306]})
        self.assertEqual(plan['policy']['action'], 'confirm')
        result = self.send(plan)
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(self.conn.writes, ['\n'.join(plan['statements'])])
        self.assertEqual(self.conn.rows("SELECT status FROM main.mysql_servers WHERE hostname='mf-ic-2'"),
                         [('OFFLINE_SOFT',)])
        self.assertEqual(self.conn.rows("SELECT status FROM disk.mysql_servers WHERE hostname='mf-ic-2'"),
                         [('ONLINE',)], 'nothing is saved to disk')
        self.assertEqual(result['after']['runtime'], [{'hostgroup_id': 32, 'status': 'OFFLINE_SOFT', 'weight': 100}])
        self.assertEqual(result['next']['action'], 'servers_save')
        (record,) = self.audit()
        self.assertEqual((record['status'], record['outcome'], record['operation']), ('ok', 'applied', 'proxysql_action'))
        self.assertEqual(record['statements'], plan['statements'])
        self.assertEqual(record['target']['config']['hostname'], 'mf-ic-2')
        self.assertNotIn('pw-secret', json.dumps(record))
        # Then the separate save, reviewed on its own.
        save = self.plan('servers_save')
        self.assertEqual(save['changes'], ['hostgroup 30, mf-ic-2:3306: status ONLINE → OFFLINE_SOFT'])
        self.assertEqual(self.send(save)['status'], 'done')
        self.assertEqual(self.conn.rows("SELECT status FROM disk.mysql_servers WHERE hostname='mf-ic-2'"),
                         [('OFFLINE_SOFT',)])

    def test_value_and_rule_are_bound_to_the_review(self):
        plan = self.plan('rule_disable', {'rule_id': 30})
        tampered = dict(plan, target={'rule_id': 10})
        result = self.send(tampered, token=plan['review_token'])
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(self.conn.writes, [])
        weight = self.plan('server_weight', {'config': [30, 'mf-ic-2', 3306]}, 50)
        result = self.send(dict(weight, value=1), token=weight['review_token'])
        self.assertEqual(result['status'], 'refused')
        result = self.send(dict(weight, statements=['LOAD MYSQL SERVERS TO RUNTIME;']), token=weight['review_token'])
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(self.conn.writes, [])
        self.assertEqual(self.send(weight)['status'], 'done')
        self.assertEqual(self.conn.rows("SELECT weight FROM main.runtime_mysql_servers WHERE hostname='mf-ic-2'"),
                         [(50,)])
        statuses = [(r['status'], r['outcome']) for r in self.audit()]
        self.assertEqual(statuses.count(('refused', 'review')), 3)

    def test_no_token_or_used_token_sends_nothing(self):
        plan = self.plan('server_offline_soft', {'config': [30, 'mf-ic-2', 3306]})
        self.assertEqual(self.send(plan, token='')['status'], 'refused')
        missing = dict(plan)
        missing.pop('review_token')
        result = self.api('/api/live/proxysql/action', {'connection': self.connection, 'plan': missing,
                                                         'confirmed': True})
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(self.conn.writes, [])
        self.assertEqual(self.send(plan)['status'], 'done')
        again = self.send(plan)
        self.assertEqual(again['status'], 'refused', 'a used review never sends twice')
        self.assertEqual(len(self.conn.writes), 1)

    def test_read_only_and_unconfirmed_refuse_before_connecting_and_are_audited(self):
        plan = self.plan('server_offline_soft', {'config': [30, 'mf-ic-2', 3306]})
        factory = Factory(self.conn)
        with mock.patch('myflames.connector.MySQLConnection', factory):
            ro = dict(self.connection, read_only=True)
            body = dict(plan)
            token = body.pop('review_token')
            result = ui.api_response('/api/live/proxysql/action', {'connection': ro, 'plan': body, 'review_token': token,
                                                                    'confirmed': True}, self.workspace)
            self.assertEqual(result['status'], 'refused')
            result = ui.api_response('/api/live/proxysql/action', {'connection': self.connection, 'plan': body,
                                                                    'review_token': token}, self.workspace)
            self.assertEqual(result['status'], 'refused')
        self.assertEqual(factory.options, [], 'no connection opened')
        outcomes = sorted(r['outcome'] for r in self.audit())
        self.assertEqual(outcomes, ['needs_confirmation', 'read_only'])
        read_only_plan = self.plan('server_offline_soft', {'config': [30, 'mf-ic-2', 3306]},
                                   connection=dict(self.connection, read_only=True))
        self.assertEqual(read_only_plan['policy']['action'], 'refuse')

    def test_drift_after_review_sends_nothing(self):
        plan = self.plan('server_weight', {'config': [30, 'mf-ic-2', 3306]}, 50)
        self.conn.db.execute("UPDATE main.mysql_servers SET weight = 60 WHERE hostname = 'mf-ic-2'")
        self.conn.load('servers')
        result = self.send(plan)
        self.assertEqual(result['status'], 'refused')
        self.assertIn('nothing was sent', result['message'])
        self.assertEqual(self.conn.writes, [])
        (record,) = self.audit()
        self.assertEqual((record['status'], record['outcome']), ('refused', 'changed'))

    def test_typed_guard(self):
        plan = self.plan('server_offline_hard', {'config': [30, 'mf-ic-2', 3306]})
        self.assertEqual(self.send(plan, typed='mf-ic-2')['status'], 'refused')
        plan = self.plan('server_offline_hard', {'config': [30, 'mf-ic-2', 3306]})
        self.assertEqual(self.send(plan, typed='mf-ic-2:3306')['status'], 'done')

    def test_a_timeout_is_unknown_and_never_retried(self):
        conn = FakeAdmin(timeout_line=2)
        plan = self.plan('server_offline_soft', {'config': [30, 'mf-ic-2', 3306]})
        result = self.send(plan, conn=conn)
        self.assertEqual(result['status'], 'unknown')
        self.assertIn('unknown', result['message'])
        self.assertEqual(len(conn.writes), 1)
        (record,) = self.audit()
        self.assertEqual((record['status'], record['outcome']), ('unknown', 'timeout'))

    def test_load_failing_after_the_update_is_partial(self):
        conn = FakeAdmin(fail_line=2, fail_text='ProxySQL Admin Error: lock held')
        plan = self.plan('server_offline_soft', {'config': [30, 'mf-ic-2', 3306]})
        result = self.send(plan, conn=conn)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['sent'], plan['statements'][:1])
        self.assertIn('ProxySQL still runs the previous configuration', result['message'])
        (record,) = self.audit()
        self.assertEqual((record['status'], record['outcome']), ('error', 'partial'))
        first = FakeAdmin(fail_line=1, fail_text='ProxySQL Admin Error: constraint failed')
        plan = self.plan('server_offline_soft', {'config': [30, 'mf-ic-2', 3306]})
        result = self.send(plan, conn=first)
        self.assertEqual((result['status'], result['sent']), ('failed', []))

    def test_a_comment_before_load_is_what_admin_refuses(self):
        conn = FakeAdmin()
        with self.assertRaises(ConnectorError):
            conn.run_write(proxysql.ACTION_TAG + proxysql.LOAD['servers'])
        for plan in (proxysql.plan_action(proxysql.read_status(conn), 'servers_load'),
                     proxysql.plan_action(proxysql.read_status(conn), 'query_rules_save')):
            self.assertFalse(plan['statements'][-1].startswith('/*'))

    def test_not_proxysql_refuses_at_plan_and_send(self):
        with self.assertRaises(ValueError):
            self.api('/api/live/proxysql/plan', {'connection': self.connection, 'action': 'servers_load'}, NotProxy())
        plan = self.plan('servers_load')
        result = self.send(plan, conn=NotProxy())
        self.assertEqual(result['status'], 'refused')

    def test_policy_made_read_only_during_the_fresh_read_sends_nothing(self):
        # Codex review: the saved policy is read again right before sending.
        saved = ui.api_response('/api/profiles/save', {'value': {'name': 'lab', 'host': '127.0.0.1', 'port': 6032,
                                                                'user': 'radmin', 'database': '', 'ssl_mode': '',
                                                                'ssl_ca': '', 'type': 'development'}}, self.workspace)
        connection = {'host': '127.0.0.1', 'port': 6032, 'user': 'radmin', 'profile_id': saved['id'],
                      'type': 'development', 'read_only': False}
        plan = self.plan('server_offline_soft', {'config': [30, 'mf-ic-2', 3306]}, connection=connection)
        self.assertEqual(plan['policy']['action'], 'confirm')

        def make_read_only():
            value = dict(self.workspace.store.get('profile', saved['id']), read_only=True)
            ui.api_response('/api/profiles/save', {'id': saved['id'], 'value': value}, self.workspace)
        self.conn.on_read = make_read_only
        result = self.send(plan, connection=connection)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(self.conn.writes, [])
        (record,) = self.audit()
        self.assertEqual((record['status'], record['outcome']), ('refused', 'policy_changed'))

    def test_load_binds_every_row_not_only_the_difference_lines(self):
        # Codex review: an added rule changed between review and send.
        self.conn.db.execute("INSERT INTO main.mysql_query_rules VALUES (40, 1, NULL, NULL, 0, '^INSERT', NULL, 0, 10, 1, '')")
        plan = self.plan('query_rules_load')
        self.assertEqual(plan['changes'], ['rule 40: in memory, not in runtime (active 1, match_digest ^INSERT, '
                                           'match_pattern NULL, destination_hostgroup 10, apply 1)'])
        # A column the difference line does not show: only the bound fingerprint sees it.
        self.conn.db.execute("UPDATE main.mysql_query_rules SET flagIN = 7 WHERE rule_id = 40")
        result = self.send(plan)
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(self.conn.writes, [])
        self.conn.db.execute("UPDATE main.mysql_query_rules SET flagIN = 0, destination_hostgroup = 99 WHERE rule_id = 40")
        self.assertEqual(self.send(self.plan('query_rules_load'))['status'], 'done')

    def test_a_copy_that_cannot_be_read_offers_no_load_or_row_change(self):
        # Codex review: runtime_mysql_query_rules_fast_routing unreadable while memory has a change there.
        conn = FakeAdmin(missing=('runtime_mysql_query_rules_fast_routing',))
        conn.db.execute("INSERT INTO main.mysql_query_rules_fast_routing VALUES ('app', 'db', 0, 99, '')")
        report = proxysql.read_status(conn)
        self.assertIsNone(report['pending']['query_rules']['runtime'])
        self.assertTrue(any('mysql_query_rules_fast_routing could not be read' in n for n in report['notes']))
        for action, target in (('rule_disable', {'rule_id': 30}), ('query_rules_load', None)):
            with self.assertRaises(ValueError):
                proxysql.plan_action(report, action, target)
        self.assertEqual(report['pending']['servers']['runtime'], [], 'the servers module is unaffected')

    def test_a_null_becoming_the_text_null_is_drift(self):
        # Codex review: SQL NULL to 'NULL' between review and send produced the same fingerprint.
        plan = self.plan('rule_disable', {'rule_id': 10})
        self.conn.db.execute("UPDATE main.mysql_query_rules SET username = 'NULL' WHERE rule_id = 10")
        self.conn.load('query_rules')
        result = self.send(plan)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(self.conn.writes, [])
        (record,) = self.audit()
        self.assertEqual((record['status'], record['outcome']), ('refused', 'changed'))

    def test_a_client_that_ends_without_an_answer_is_unknown(self):
        # Codex review: a client killed after UPDATE and LOAD ran read as "Nothing changed".
        sent = self.conn.run_write

        def killed(sql, extra_flags=None, timeout=None):
            sent(sql, extra_flags, timeout)
            raise ConnectorError('mysql client exited -9: no stderr')
        self.conn.run_write = killed
        result = self.send(self.plan('rule_disable', {'rule_id': 30}))
        self.assertEqual(result['status'], 'unknown', result)
        self.assertNotIn('Nothing changed', result['message'])
        self.assertEqual(len(self.conn.writes), 1, 'never sent again')
        (record,) = self.audit()
        self.assertEqual((record['status'], record['outcome']), ('unknown', 'no_reply'))

    def test_local_refusals_are_recorded_as_refused(self):
        answer = ui.api_response('/api/audit/declined', {'operation': 'proxysql_action', 'statements': ['LOAD MYSQL SERVERS TO RUNTIME;'],
                                                          'connection': self.connection, 'reason': 'policy_changed'},
                                 self.workspace)
        self.assertEqual(answer['status'], 'refused')
        (record,) = self.audit()
        self.assertEqual((record['status'], record['outcome']), ('refused', 'policy_changed'))
        with self.assertRaises(ValueError):
            ui.api_response('/api/audit/declined', {'operation': 'x', 'statements': ['y'], 'reason': 'other'},
                            self.workspace)

    def test_declined_is_recorded(self):
        plan = self.plan('rule_disable', {'rule_id': 30})
        answer = ui.api_response('/api/audit/declined', {'operation': 'proxysql_action', 'statements': plan['statements'],
                                                          'connection': self.connection,
                                                          'target': {'action': plan['action']}}, self.workspace)
        self.assertEqual(answer['status'], 'declined')
        (record,) = self.audit()
        self.assertEqual((record['status'], record['statements']), ('declined', plan['statements']))
        self.assertEqual(self.conn.writes, [])


class TestReviewedWrites(unittest.TestCase):
    def test_failed_line_and_server_message(self):
        text = 'mysql client exited 1: ERROR 1045 (28000) at line 2: ProxySQL Admin Error: near "LOAD": syntax error'
        self.assertEqual(reviewed_writes.failed_line(text), 2)
        self.assertEqual(reviewed_writes.server_message(text), 'ProxySQL Admin Error: near "LOAD": syntax error')
        self.assertIsNone(reviewed_writes.failed_line('Query timed out after 60s; the outcome is unknown.'))

    def test_check_reviewed_rejects_bad_shapes(self):
        for bad in (None, [], {'action': 'x'}, {'action': 'x', 'statements': []},
                    {'action': 'x', 'statements': ['a\nb']}, {'action': 1, 'statements': ['a']}):
            with self.assertRaises(ValueError):
                reviewed_writes.check_reviewed(bad)

    def test_only_a_numbered_error_decides_failed_or_partial(self):
        class Conn(object):
            def __init__(self, error):
                self.error, self.writes = error, []

            def run_write(self, sql, extra_flags=None, timeout=None):
                self.writes.append(sql)
                raise ConnectorError(self.error)

        class Audit(object):
            def __init__(self):
                self.completed = None

            def attempt(self, statements, target=None, verified=None):
                return 'a1'

            def complete(self, status, outcome=None, **kw):
                self.completed = (status, outcome)

        plan = {'action': 'x', 'statements': ['UPDATE t SET a = 1;', 'LOAD MYSQL SERVERS TO RUNTIME;'], 'expect': {}}
        cases = (
            ('mysql client exited -9: no stderr', 'unknown', ('unknown', 'no_reply')),
            ('mysql client exited 1: ', 'unknown', ('unknown', 'no_reply')),
            ('Query timed out after 60s; the outcome is unknown.', 'unknown', ('unknown', 'timeout')),
            ('mysql client exited 1: ERROR 2013 (HY000) at line 2: Lost connection to MySQL server during query',
             'unknown', ('unknown', 'no_reply')),
            ('mysql client exited 1: ERROR 2006 (HY000) at line 1: MySQL server has gone away', 'unknown',
             ('unknown', 'no_reply')),
            ('mysql client exited 1: ERROR 1045 (28000) at line 7: x', 'unknown', ('unknown', 'no_reply')),
            ("mysql client exited 1: ERROR 2003 (HY000): Can't connect to MySQL server on '127.0.0.1:6032' (61)",
             'failed', ('error', 'not_connected')),
            ("mysql client exited 1: ERROR 1045 (28000): Access denied for user 'radmin'", 'failed',
             ('error', 'not_connected')),
            ('mysql client exited 1: ERROR 1045 (28000) at line 1: ProxySQL Admin Error: constraint failed', 'failed',
             ('error', 'refused_by_server')),
            ('mysql client exited 1: ERROR 3910 (HY000) at line 1: replica channel running', 'failed',
             ('error', 'refused_by_server')),
            ('mysql client exited 1: ERROR 1045 (28000) at line 2: ProxySQL Admin Error: lock held', 'partial',
             ('error', 'partial')),
        )
        for error, status, audited in cases:
            conn, audit = Conn(error), Audit()
            out = reviewed_writes.execute(conn, plan, lambda: plan, 'x-1.0', audit=audit)
            self.assertEqual((out['status'], audit.completed), (status, audited), error)
            self.assertEqual(len(conn.writes), 1, error)
            if status == 'unknown':
                self.assertIn('unknown', out['message'], error)
            if status == 'failed':
                self.assertEqual(out['sent'], [], error)
        self.assertEqual([reviewed_writes.client_error(c) for c in (None, 1045, 1999, 2000, 2013, 2999, 3000, 3910)],
                         [False, False, False, True, True, True, False, False])


if __name__ == '__main__':
    unittest.main()
