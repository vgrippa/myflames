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
import unicodedata
from pathlib import Path
from unittest import mock

from myflames import dbops, proxysql, reviewed_writes, ui
from myflames.connector import ConnectorError, PartialReadError

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / 'docs' / 'schemas' / 'proxysql-1.0.json').read_text(encoding='utf-8'))
VERSION = '3.0.11-797-g7c91137'
# Synthetic persisted instance identity. The lab recording predates this read.
ADMIN_UUID = '11111111-2222-3333-4444-555555555555'
OTHER_ADMIN_UUID = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
IDENTITY_SQL = "SELECT variable_value FROM disk.global_settings WHERE variable_name = 'uuid'"
# Exact diagnostic from ProxySQL 3.0.11 Admin and the mysql 8.4 batch client.
GUARD_MISMATCH_ERROR = ('ERROR 1045 (28000) at line 1: ProxySQL Admin Error: '
                        'NOT NULL constraint failed: global_variables.variable_name')
SERVER_COLUMNS = ('hostgroup_id INT, hostname VARCHAR, port INT, gtid_port INT, status VARCHAR, weight INT, '
                  'compression INT, max_connections INT, max_replication_lag INT, use_ssl INT, max_latency_ms INT, '
                  'comment VARCHAR, PRIMARY KEY (hostgroup_id, hostname, port)')
HOSTGROUP_COLUMNS = ('writer_hostgroup INT PRIMARY KEY, backup_writer_hostgroup INT, reader_hostgroup INT, '
                     'offline_hostgroup INT, active INT, max_writers INT, writer_is_also_reader INT, '
                     'max_transactions_behind INT, comment VARCHAR')
RULE_COLUMNS = ('rule_id INT PRIMARY KEY, active INT, username VARCHAR, schemaname VARCHAR, flagIN INT, '
                'match_digest VARCHAR, match_pattern VARCHAR, negate_match_pattern INT, destination_hostgroup INT, '
                'apply INT, comment VARCHAR, re_modifiers VARCHAR DEFAULT \'CASELESS\', flagOUT INT')
#: The columns the lab's rules set (re_modifiers and flagOUT keep their defaults).
RULE_NAMES = ('rule_id, active, username, schemaname, flagIN, match_digest, match_pattern, negate_match_pattern, '
              'destination_hostgroup, apply, comment')
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
#: stats_mysql_query_digest as the live lab had it (2026-10-21), by total time:
#: SELECT @@hostname ran on 32 by rule 30 and on 10 when tagged /* galera */
#: (rule 10 tests the query text, which the digest does not keep); SHOW and the
#: transaction statements went to mf_admin's default hostgroup 30.
DIGESTS = [
    (30, 'information_schema', 'mf_admin', '', '0x48BE884F70034786', 'SHOW GLOBAL VARIABLES LIKE ?', 5, 1792537332, 1792543106, 16763, 787, 6343, 0, 5),
    (32, 'information_schema', 'mf_admin', '', '0xA8027DF9CE26E2FF', 'SELECT @@hostname', 5, 1792537332, 1792543106, 13162, 192, 6686, 0, 5),
    (32, 'information_schema', 'mf_admin', '', '0x9C012E222EE5AA4E', 'SELECT COUNT(*) FROM testdb.orders WHERE status = ? AND customer_id = ?', 20, 1792537331, 1792537331, 7706, 78, 5974, 0, 20),
    (32, 'information_schema', 'mf_admin', '', '0xC54E85FD66BA43DA', 'SELECT name,country FROM testdb.customers WHERE id = ?', 20, 1792537331, 1792537331, 6889, 69, 5369, 0, 20),
    (30, 'information_schema', 'mf_admin', '', '0xA64D4A3B64005014', 'START TRANSACTION', 5, 1792537331, 1792537331, 5194, 52, 4947, 0, 0),
    (10, 'information_schema', 'mf_admin', '', '0xA8027DF9CE26E2FF', 'SELECT @@hostname', 5, 1792537332, 1792543106, 1888, 203, 650, 0, 5),
    (30, 'information_schema', 'mf_admin', '', '0xABB02DE3FAD6872B', 'SHOW GLOBAL STATUS LIKE ?', 5, 1792537331, 1792537331, 1329, 198, 462, 0, 5),
    (10, 'information_schema', 'mf_admin', '', '0x4A8BCC5DA1E63EFB', 'SELECT COUNT(*) FROM testdb.orders WHERE product_id = ?', 5, 1792537331, 1792537331, 1315, 136, 553, 0, 5),
    (30, 'information_schema', 'mf_admin', '', '0x7499BD50A3EBE746', 'SELECT id,status FROM testdb.orders WHERE id = ? FOR UPDATE', 5, 1792537331, 1792537331, 505, 86, 141, 0, 5),
    (30, 'information_schema', 'mf_admin', '', '0x49474B60312C20A7', 'ROLLBACK', 5, 1792537331, 1792537331, 362, 62, 86, 0, 0),
]
#: runtime_global_variables the routing evaluation reads (live 3.0.11 values).
ADMIN_VARIABLES = [('mysql-query_processor_regex', '1'), ('mysql-query_processor_iterations', '0'),
                   ('mysql-query_digests', 'true'), ('mysql-query_digests_track_hostname', 'false')]


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
                 timeout_line=None, pool_columns=None, missing_text='ProxySQL Admin Error: no such table: %s',
                 server_uuid=ADMIN_UUID):
        self.version = version
        self.missing_text = missing_text
        self.db = sqlite3.connect(':memory:')
        for schema in ('disk', 'stats'):
            self.db.execute("ATTACH DATABASE ':memory:' AS %s" % schema)
        self.missing = set(missing)
        self.fail_line, self.fail_text, self.timeout_line = fail_line, fail_text, timeout_line
        self.calls, self.read_flags, self.writes = [], [], []
        self.db.execute('CREATE TABLE disk.global_settings '
                        '(variable_name VARCHAR NOT NULL PRIMARY KEY, variable_value VARCHAR NOT NULL)')
        if server_uuid is not None:
            self.db.execute("INSERT INTO disk.global_settings VALUES ('uuid', ?)", (server_uuid,))
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
        self.db.execute('CREATE TABLE main.global_variables '
                        '(variable_name VARCHAR NOT NULL PRIMARY KEY, variable_value VARCHAR NOT NULL)')
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
            self.db.executemany('INSERT INTO %s.mysql_query_rules (%s) VALUES (?,?,?,?,?,?,?,?,?,?,?)'
                                % (schema, RULE_NAMES), RULES)
        self.load('servers')
        self.load('query_rules')
        self.db.executemany('INSERT INTO stats.stats_mysql_query_rules VALUES (?, ?)', [(10, 6), (20, 5), (30, 41)])
        self.db.executemany('INSERT INTO stats.stats_mysql_query_digest VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', DIGESTS)
        self.db.execute('CREATE TABLE main.runtime_global_variables (variable_name VARCHAR, variable_value VARCHAR)')
        self.db.executemany('INSERT INTO main.runtime_global_variables VALUES (?, ?)', ADMIN_VARIABLES)
        # runtime_mysql_users keeps a frontend and a backend row per user.
        self.db.execute('CREATE TABLE main.runtime_mysql_users (username VARCHAR, password VARCHAR, active INT, '
                        'default_hostgroup INT, transaction_persistent INT, frontend INT, backend INT)')
        self.db.executemany('INSERT INTO main.runtime_mysql_users VALUES (?,?,?,?,?,?,?)',
                            [('mf_admin', '*SECRET', 1, 30, 1, 1, 0), ('mf_admin', '*SECRET', 1, 30, 1, 0, 1)])

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
        if body.rstrip(';') == 'SELECT @@version':
            return [['@@version'], [self.version]]
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
    def run(self, sql, extra_flags=None, timeout=None, retain_partial_stdout=False):
        self.calls.append(sql)
        self.read_flags.append((sql, tuple(extra_flags or ())))
        hook = getattr(self, 'on_read', None)
        if hook is not None:
            self.on_read = None
            hook()
        if sql == 'SELECT @@version':
            return self.version + '\n'
        if '--xml' not in (extra_flags or ()):
            raise AssertionError('Admin tables are read as XML, which tells NULL from the text NULL: %s' % sql)
        # Each Admin evidence batch carries its UUID reads in the same
        # client login. Like mysql --xml, emit one document per SELECT.
        statements = dbops.split_statements(sql)
        output = []
        for index, unit in enumerate(statements, 1):
            try:
                rows = self._execute(unit['text'], index)
            except ConnectorError as exc:
                if retain_partial_stdout:
                    raise PartialReadError(str(exc), ''.join(output)) from None
                raise
            output.append(self._xml(unit['text'], rows))
        return ''.join(output)

    def run_write(self, sql, extra_flags=None, timeout=None):
        self.writes.append(sql)
        hook = getattr(self, 'on_write', None)
        if hook is not None:
            self.on_write = None
            hook()
        action_line = 0
        for line, statement in enumerate(sql.split('\n'), 1):
            # Existing failure fixtures name reviewed actions, while client
            # errors must retain physical input lines including the guard.
            guard = statement.startswith('INSERT INTO main.global_variables ') and 'disk.global_settings' in statement
            if not guard:
                action_line += 1
            if not guard and self.timeout_line == action_line:
                raise ConnectorError('Query timed out after 60s; the outcome is unknown.')
            if not guard and self.fail_line == action_line:
                raise self._error(line, self.fail_text)
            self._execute(statement, line)
        return ''

    def rows(self, sql):
        return self.db.execute(sql).fetchall()

    def close(self):
        # API calls reuse this simulated server through successive client sessions.
        # Home.admin owns its SQLite lifetime and closes it at test cleanup.
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

    def admin(self, *args, **kwargs):
        """Keep the simulated server alive across clients, but only for this test."""
        conn = FakeAdmin(*args, **kwargs)
        self.addCleanup(conn.db.close)
        return conn

    def other_instance_read(self, conn, evidence):
        """Route one whole read login to B, then return the endpoint to A.

        The surrounding UUID logins both see A. Only an identity inside
        the evidence batch catches this switch, including optional reads.
        """
        original = conn.run

        def run(sql, *args, **kwargs):
            if evidence not in sql:
                return original(sql, *args, **kwargs)
            conn.db.execute("UPDATE disk.global_settings SET variable_value = ? WHERE variable_name = 'uuid'",
                            (OTHER_ADMIN_UUID,))
            try:
                return original(sql, *args, **kwargs)
            finally:
                conn.db.execute("UPDATE disk.global_settings SET variable_value = ? WHERE variable_name = 'uuid'",
                                (ADMIN_UUID,))
        return mock.patch.object(conn, 'run', side_effect=run)

    def unidentified_optional_read(self, conn, mode):
        """Fail all copies of an existing optional table in an unbound login.

        A missing identity table is not proof that the module table is
        absent. Completed stdout is separate from the server diagnostic,
        matching the real connector's PartialReadError contract.
        """
        original = conn.run

        def run(sql, *args, **kwargs):
            if 'mysql_hostgroup_attributes' not in sql:
                return original(sql, *args, **kwargs)
            prefix = conn._xml(IDENTITY_SQL, [['variable_value'], [ADMIN_UUID]])
            line, reason, output = 2, 'no such table: main.mysql_hostgroup_attributes', prefix
            if mode == 'missing_identity_prefix':
                line, reason, output = 1, 'no such table: disk.global_settings', ''
            elif mode == 'missing_identity_suffix':
                evidence = next(unit['text'] for unit in dbops.split_statements(sql)
                                if 'mysql_hostgroup_attributes' in unit['text'])
                line, reason = 3, 'no such table: disk.global_settings'
                output += conn._xml(evidence, conn._execute(evidence))
            elif mode == 'different_prefix':
                output = conn._xml(IDENTITY_SQL, [['variable_value'], [OTHER_ADMIN_UUID]])
            elif mode == 'empty_prefix':
                output = conn._xml(IDENTITY_SQL, [])
            elif mode == 'missing_prefix':
                output = ''
            if mode == 'transport':
                error = 'mysql client exited 1: ERROR 2013 (HY000) at line 2: Lost connection to server'
            else:
                error = str(conn._error(line, 'ProxySQL Admin Error: ' + reason))
            raise PartialReadError(error, output)
        return mock.patch.object(conn, 'run', side_effect=run)


class TestFixtureCleanup(unittest.TestCase):
    def test_admin_databases_close_at_test_cleanup(self):
        owner = Home('runTest')
        self.addCleanup(owner.doCleanups)
        connections = [owner.admin(), owner.admin(missing=('runtime_mysql_servers',))]
        for conn in connections:
            # Closing a simulated client must not destroy the server state.
            with conn:
                self.assertEqual(conn.rows('SELECT 1'), [(1,)])
            conn.close()
            self.assertEqual(conn.rows('SELECT 1'), [(1,)])
        self.assertTrue(owner.doCleanups())
        for conn in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.rows('SELECT 1')


class TestRead(Home):
    def test_reads_the_lab_shape_and_validates(self):
        from test_chart_data import validate
        conn = self.admin()
        report = proxysql.read_status(conn)
        validate(report, SCHEMA)
        self.assertEqual((report['is_proxysql'], report['version'], report['uptime_s']), (True, VERSION, 187200))
        self.assertEqual(report['server_identity'], {'uuid': ADMIN_UUID})
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
        orders = next(d for d in report['query_digests'] if d['digest'] == '0x9C012E222EE5AA4E')
        self.assertEqual((orders['avg_time_us'], orders['key']),
                         (385, 'dg[32,"information_schema","mf_admin","","0x9C012E222EE5AA4E"]'))
        self.assertEqual(report['query_digests'][0]['digest_text'], 'SHOW GLOBAL VARIABLES LIKE ?', 'by total time')
        self.assertFalse(conn.writes, 'reading sends no write')
        for sql in conn.calls:
            self.assertTrue(sql.startswith('SELECT'), sql)

    def test_every_evidence_batch_reads_its_instance_identity(self):
        conn = self.admin()
        report = proxysql.read_status(conn)
        self.assertIsNone(report['message'])
        evidence = [sql for sql in conn.calls if 'SELECT * FROM' in sql or
                    'runtime_global_variables' in sql or 'runtime_mysql_users' in sql]
        self.assertTrue(evidence)
        for batch in evidence:
            with self.subTest(batch=batch):
                statements = dbops.split_statements(batch)
                self.assertEqual(len(statements), 3, 'UUID and evidence must share one client login')
                self.assertEqual(statements[0]['text'].strip().rstrip(';'), IDENTITY_SQL)
                self.assertEqual(statements[-1]['text'].strip().rstrip(';'), IDENTITY_SQL)

    def test_product_identification_is_bound_before_the_configuration_reads(self):
        conn = self.admin()
        report = proxysql.read_status(conn)
        self.assertEqual(report['version'], VERSION)
        for query in (proxysql._ADMIN_VERSION_SQL, 'SELECT @@version'):
            batches = [(sql, flags) for sql, flags in conn.read_flags if query in sql]
            self.assertEqual(len(batches), 1)
            sql, flags = batches[0]
            statements = [s['text'].strip().rstrip(';') for s in dbops.split_statements(sql)]
            self.assertEqual(statements, [query, IDENTITY_SQL] if query == proxysql._ADMIN_VERSION_SQL
                             else [IDENTITY_SQL, query, IDENTITY_SQL])
            self.assertIn('--xml', flags)
            self.assertIn('--skip-reconnect', flags)

    def test_standalone_identification_binds_the_version_to_the_instance(self):
        for query in (proxysql._ADMIN_VERSION_SQL, 'SELECT @@version'):
            with self.subTest(query=query):
                conn = self.admin()
                with self.other_instance_read(conn, query), self.assertRaises(ConnectorError) as caught:
                    proxysql.identify(conn)
                self.assertEqual(str(caught.exception), proxysql._EVIDENCE_IDENTITY_BLOCK)
                self.assertEqual(conn.rows(IDENTITY_SQL), [(ADMIN_UUID,)])
        conn = self.admin()
        self.assertEqual(proxysql.identify(conn), (True, VERSION))
        self.assertEqual(len(conn.calls), 2, 'UUID discovery must not add a client login')
        for query in (proxysql._ADMIN_VERSION_SQL, 'SELECT @@version'):
            sql, flags = next((sql, flags) for sql, flags in conn.read_flags if query in sql)
            self.assertEqual([s['text'].strip().rstrip(';') for s in dbops.split_statements(sql)],
                             [query, IDENTITY_SQL] if query == proxysql._ADMIN_VERSION_SQL
                             else [IDENTITY_SQL, query, IDENTITY_SQL])
            self.assertIn('--skip-reconnect', flags)

    def test_unavailable_or_invalid_uuid_adds_no_identification_login(self):
        for options in ({'server_uuid': None}, {'server_uuid': ''}, {'server_uuid': 'invalid'},
                        {'missing': ('disk.global_settings',)},
                        {'missing': ('disk.global_settings',), 'missing_text': 'ProxySQL Admin Error: permission denied'}):
            with self.subTest(options=options):
                conn = self.admin(**options)
                reads = proxysql._Reads(conn)
                reads.bind_identity()
                self.assertEqual(proxysql.identify(conn, reads), (True, VERSION))
                self.assertIsNone(reads.uuid)
                self.assertEqual(len(conn.calls), 2, 'consume the completed version result, never retry it')
                self.assertIn(proxysql._ADMIN_VERSION_SQL, conn.calls[0])
                self.assertIn(IDENTITY_SQL, conn.calls[0])
                self.assertEqual(conn.calls[1], 'SELECT @@version')

    def test_initial_version_and_uuid_login_failures_are_never_retried(self):
        from myflames import proxysql_digests
        failures = ('ERROR 2003 (HY000): Cannot connect', 'ERROR 2013 (HY000): Lost connection',
                    'Query timed out after 30s.', 'ERROR 1045 (28000): Access denied for user operator',
                    'ERROR 1227 (42000) at line 2: access denied for UUID')
        for entry in ('bind_identity', 'identify', 'status', 'digests'):
            for query in (proxysql._ADMIN_VERSION_SQL, IDENTITY_SQL):
                for message in failures:
                    with self.subTest(entry=entry, query=query, message=message):
                        conn = self.admin()
                        execute = conn._execute

                        def fail(sql, line=1):
                            if sql.strip().rstrip(';') == query:
                                raise ConnectorError(message)
                            return execute(sql, line)

                        with mock.patch.object(conn, '_execute', side_effect=fail):
                            with self.assertRaises(ConnectorError) as caught:
                                if entry == 'bind_identity':
                                    proxysql._Reads(conn).bind_identity()
                                elif entry == 'identify':
                                    proxysql.identify(conn)
                                elif entry == 'status':
                                    proxysql.read_status(conn)
                                else:
                                    proxysql_digests.read_digests(conn)
                        self.assertEqual(str(caught.exception), message)
                        self.assertEqual(len(conn.calls), 1, 'a failed login must not trigger another login')
                        self.assertEqual(conn.writes, [])

    def test_connection_wrapper_without_partial_output_still_binds_identification(self):
        conn = self.admin()

        class Wrapper(object):
            def run(self, sql, extra_flags=None, timeout=None):
                return conn.run(sql, extra_flags=extra_flags, timeout=timeout)

        self.assertEqual(proxysql.identify(Wrapper()), (True, VERSION))
        with self.other_instance_read(conn, 'SELECT @@version'), self.assertRaises(ConnectorError) as caught:
            proxysql.identify(Wrapper())
        self.assertEqual(str(caught.exception), proxysql._EVIDENCE_IDENTITY_BLOCK)

    def test_identification_from_another_instance_refuses_the_whole_report(self):
        for query in (proxysql._ADMIN_VERSION_SQL, 'SELECT @@version'):
            with self.subTest(query=query):
                conn = self.admin()
                # B reports exactly A's version and data, but has a distinct UUID.
                with self.other_instance_read(conn, query), self.assertRaises(ConnectorError) as caught:
                    proxysql.read_status(conn)
                self.assertEqual(str(caught.exception), proxysql._EVIDENCE_IDENTITY_BLOCK)
                self.assertEqual(conn.writes, [])

    def test_identification_rejects_incomplete_or_wrong_uuid_framing(self):
        for query in (proxysql._ADMIN_VERSION_SQL, 'SELECT @@version'):
            for mode in ('missing_prefix', 'missing_suffix', 'different_suffix', 'malformed'):
                with self.subTest(query=query, mode=mode):
                    conn = self.admin()
                    original = conn.run

                    def corrupt(sql, *args, **kwargs):
                        output = original(sql, *args, **kwargs)
                        if query not in sql:
                            return output
                        documents = re.findall(r'(?:<\?xml[^>]*>\s*)?<resultset\b[^>]*>.*?</resultset>',
                                               output, re.S)
                        if mode.startswith('missing_'):
                            documents.pop(0 if mode.endswith('prefix') else -1)
                        elif mode == 'different_suffix':
                            documents[-1] = conn._xml(IDENTITY_SQL, [['variable_value'], [OTHER_ADMIN_UUID]])
                        else:
                            documents[-1] = '<resultset><row></resultset>'
                        return '\n'.join(documents)

                    with mock.patch.object(conn, 'run', side_effect=corrupt), self.assertRaises(ConnectorError) as caught:
                        proxysql.read_status(conn)
                    self.assertEqual(str(caught.exception), proxysql._EVIDENCE_IDENTITY_BLOCK)

    def test_an_alternating_evidence_instance_invalidates_the_report(self):
        # Exercise config, runtime, disk, optional statistics and routing
        # inputs. Identical version/configuration cannot substitute for UUID.
        evidence = ('SELECT * FROM main.mysql_servers', 'SELECT * FROM main.runtime_mysql_servers',
                    'SELECT * FROM disk.mysql_servers', 'SELECT * FROM main.mysql_query_rules',
                    'SELECT * FROM stats_mysql_global', 'SELECT * FROM stats_mysql_connection_pool',
                    'SELECT * FROM stats_mysql_query_rules', 'SELECT * FROM stats_mysql_query_digest',
                    'FROM runtime_global_variables', 'FROM runtime_mysql_users')
        for query in evidence:
            with self.subTest(query=query):
                conn = self.admin()
                with self.other_instance_read(conn, query), self.assertRaises(ConnectorError) as caught:
                    proxysql.read_status(conn)
                self.assertEqual(conn.rows(IDENTITY_SQL), [(ADMIN_UUID,)], 'the endpoint returned to A')
                self.assertEqual(str(caught.exception), proxysql._EVIDENCE_IDENTITY_BLOCK)
                self.assertEqual(conn.writes, [])

    def test_incomplete_or_invalid_identity_result_sets_invalidate_the_report(self):
        cases = ('missing_prefix', 'missing_suffix', 'empty_prefix', 'empty_suffix', 'different_suffix',
                 'malformed_suffix', 'duplicate_prefix', 'invalid_xml')
        for case in cases:
            with self.subTest(case=case):
                conn = self.admin()
                original = conn.run

                def corrupt(sql, *args, **kwargs):
                    output = original(sql, *args, **kwargs)
                    if 'SELECT * FROM stats_mysql_global' not in sql:
                        return output
                    # Real clients emit separate XML result documents.
                    documents = re.findall(r'(?:<\?xml[^>]*>\s*)?<resultset\b[^>]*>.*?</resultset>',
                                           output, re.S)
                    if len(documents) != 3:
                        return output
                    if case.startswith('missing_'):
                        documents.pop(0 if case.endswith('prefix') else -1)
                    elif case.startswith('empty_'):
                        documents[0 if case.endswith('prefix') else -1] = conn._xml(IDENTITY_SQL, [])
                    elif case == 'different_suffix':
                        documents[-1] = conn._xml(IDENTITY_SQL, [['variable_value'], [OTHER_ADMIN_UUID]])
                    elif case == 'malformed_suffix':
                        documents[-1] = conn._xml(IDENTITY_SQL, [['variable_value'], ['not-a-uuid']])
                    elif case == 'duplicate_prefix':
                        documents[0] = conn._xml(IDENTITY_SQL,
                                                 [['variable_value'], [ADMIN_UUID], [ADMIN_UUID]])
                    else:
                        documents[-1] = '<resultset><row></resultset>'
                    return '\n'.join(documents)

                with mock.patch.object(conn, 'run', side_effect=corrupt), self.assertRaises(ConnectorError) as caught:
                    proxysql.read_status(conn)
                self.assertEqual(str(caught.exception), proxysql._EVIDENCE_IDENTITY_BLOCK)
                self.assertEqual(conn.writes, [])

    def test_optional_table_errors_cannot_hide_an_unidentified_evidence_login(self):
        modes = ('missing_identity_prefix', 'missing_identity_suffix', 'different_prefix', 'empty_prefix',
                 'missing_prefix', 'transport')
        for mode in modes:
            with self.subTest(mode=mode):
                conn = self.admin()
                # This table exists on A. A failed UUID read on B must not
                # mark all copies absent and omit it from LOAD/SAVE review.
                conn.db.execute("INSERT INTO main.mysql_hostgroup_attributes VALUES (30, 2, 'unreviewed')")
                with self.unidentified_optional_read(conn, mode), self.assertRaises(ConnectorError) as caught:
                    proxysql.read_status(conn)
                self.assertEqual(conn.rows(IDENTITY_SQL), [(ADMIN_UUID,)])
                self.assertEqual(str(caught.exception), proxysql._EVIDENCE_IDENTITY_BLOCK)
                self.assertEqual(conn.writes, [])

    def test_a_valid_partial_identity_prefix_preserves_optional_table_fallback(self):
        conn = self.admin(missing=('mysql_hostgroup_attributes',))
        report = proxysql.read_status(conn)
        self.assertEqual(report['server_identity'], {'uuid': ADMIN_UUID})
        self.assertIsNone(report['message'])
        self.assertEqual(report['pending']['servers']['runtime'], [])
        self.assertEqual(proxysql.plan_action(report, 'servers_load')['statements'], [proxysql.LOAD['servers']])
        self.assertEqual(conn.writes, [])

    def test_missing_tables_and_columns_are_unknown_not_a_crash(self):
        # 2.x without mysql_servers_ssl_params, a stats-only account, an older pool.
        conn = self.admin(missing=('stats_mysql_query_digest', 'stats_mysql_query_rules', 'mysql_servers_ssl_params'),
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
        stats_only = self.admin(missing=('main.mysql_servers', 'main.mysql_query_rules'))
        report = proxysql.read_status(stats_only)
        self.assertFalse(report['config_readable'])
        self.assertEqual(report['actions'], [])
        self.assertTrue(all(not s['actions'] for s in report['servers']))
        self.assertIsNone(report['pending']['servers']['runtime'])

    def test_missing_status_or_weight_columns_offer_nothing_that_needs_them(self):
        # Codex review: an absent status column crashed the comparison; an absent weight offered Change weight.
        conn = self.admin()
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
        conn = self.admin()
        conn.version = 'other'
        self.assertEqual(proxysql.identify(conn)[0], False)

    def test_a_connection_failure_is_an_error(self):
        class Down(object):
            def run(self, sql, extra_flags=None, timeout=None):
                raise ConnectorError("mysql client exited 1: ERROR 2003 (HY000): Can't connect to MySQL server")
        with self.assertRaises(ConnectorError):
            proxysql.read_status(Down())

    def test_mysql_81_dollar_quote_sync_error_gives_actionable_guidance(self):
        raw = "mysql client exited 1: ERROR 2014 (HY000) at line 1: Commands out of sync; you can't run this command now"

        class OutOfSync(object):
            def __init__(self, client_use=None, binary_cached=None):
                self._client_use = client_use
                self._binary_cached = binary_cached

            def run(self, sql, extra_flags=None, timeout=None):
                raise ConnectorError(raw)

        for label, conn in (
            ('unsettled_first_contact', OutOfSync()),
            ('mysql_8_4', OutOfSync(client_use={'engine': 'mysql', 'version_tuple': (8, 4, 13)})),
            ('mysql_8_1', OutOfSync(client_use={'engine': 'mysql', 'version_tuple': (8, 1, 0)})),
        ):
            with self.subTest(case=label):
                with mock.patch('myflames.client_tools.probe') as probe, \
                        self.assertRaises(ConnectorError) as ctx:
                    proxysql.read_status(conn)
                probe.assert_not_called()
                message = str(ctx.exception)
                self.assertNotIn('\n', message)
                self.assertIn('ERROR 2014', message)
                self.assertIn('2.6.0', message)
                self.assertIn('MySQL 8.0', message)
                self.assertIn('MariaDB', message)
                self.assertIn('Settings > Client tools', message)
                self.assertIn('set the MySQL client to Automatic', message)
                self.assertIn('If this is a ProxySQL Admin connection', message)
                self.assertIn('TLS Disabled', message)

    @unittest.skipIf(os.name == 'nt', 'POSIX executable trust and symlinks')
    def test_diagnostic_does_not_probe_a_client_replaced_during_failed_login(self):
        from _client_tools_fixture import private_client_dir
        from myflames import connector

        raw = 'mysql client exited 1: ERROR 2014 (HY000) at line 1: Commands out of sync'
        folder = private_client_dir(Path(self.tmp.name).resolve() / 'bin')
        binary = folder / 'mysql'
        replacement = folder / 'replacement'
        replacement.write_text('untrusted replacement')
        replacement.chmod(0o777)

        for mode in ('replaced_file', 'redirected_symlink'):
            with self.subTest(mode=mode):
                if binary.is_symlink():
                    binary.unlink()
                binary.write_text('previously trusted client')
                binary.chmod(0o700)
                connector.checked_program(str(binary))

                class OutOfSync(object):
                    _client_use = None
                    _binary_cached = str(binary)

                    def run(self, sql, extra_flags=None, timeout=None):
                        # Another OS user replaces the client while the failed
                        # login is running. Diagnostics must not launch it.
                        binary.unlink()
                        if mode == 'redirected_symlink':
                            binary.symlink_to(replacement)
                        else:
                            binary.write_text('untrusted replacement')
                            binary.chmod(0o777)
                        raise ConnectorError(raw)

                with mock.patch('myflames.client_tools.probe', return_value={'available': False}) as probe, \
                        mock.patch('myflames.connector.subprocess.run') as child, \
                        mock.patch('myflames.connector._run_bounded') as bounded_child, \
                        self.assertRaises(ConnectorError) as ctx:
                    proxysql.identify(OutOfSync())
                probe.assert_not_called()
                child.assert_not_called()
                bounded_child.assert_not_called()
                self.assertEqual(str(ctx.exception), proxysql._DOLLAR_QUOTE_SYNC_MESSAGE)

    def test_other_2014_errors_and_older_or_mariadb_clients_keep_raw_error(self):
        raw = "mysql client exited 1: ERROR 2014 (HY000) at line 1: Commands out of sync; you can't run this command now"
        line2 = "mysql client exited 1: ERROR 2014 (HY000) at line 2: Commands out of sync; you can't run this command now"
        echoed = "mysql client exited 1: --------------\nSELECT 1\n--------------\n\nERROR 2014 (HY000) at line 1: Commands out of sync"

        class Fail(object):
            def __init__(self, msg, client_use=None):
                self._msg = msg
                self._client_use = client_use

            def run(self, sql, extra_flags=None, timeout=None):
                raise ConnectorError(self._msg)

        for label, msg, use in (
            ('line_2', line2, None),
            ('mariadb_echo', echoed, None),
            ('mysql_8_0_use', raw, {'engine': 'mysql', 'version_tuple': (8, 0, 45)}),
            ('mariadb_11_4_use', raw, {'engine': 'mariadb', 'version_tuple': (11, 4, 13)}),
        ):
            with self.subTest(case=label):
                with mock.patch('myflames.client_tools.probe') as probe, \
                        self.assertRaises(ConnectorError) as ctx:
                    proxysql.identify(Fail(msg, use))
                probe.assert_not_called()
                self.assertEqual(str(ctx.exception), msg)

    def test_pending_changes_follow_the_monitors(self):
        conn = self.admin()
        conn.db.execute("UPDATE main.mysql_servers SET weight = 7 WHERE hostname = 'mf-ic-2'")
        conn.db.execute("UPDATE main.mysql_query_rules SET active = 0 WHERE rule_id = 30")
        report = proxysql.read_status(conn)
        self.assertEqual(report['pending']['servers']['runtime'], ['hostgroup 30, mf-ic-2:3306: weight 100 → 7'])
        self.assertEqual(report['pending']['servers']['disk'], ['hostgroup 30, mf-ic-2:3306: weight 100 → 7'])
        self.assertEqual(report['pending']['query_rules']['runtime'], ['rule 30: active 1 → 0'])
        # OFFLINE_SOFT under Galera is put back in traffic by the monitor: not pending forever.
        conn = self.admin()
        conn.db.execute("UPDATE main.mysql_servers SET status = 'OFFLINE_SOFT' WHERE hostname = 'mf-galera-3'")
        conn.load('servers')
        self.assertEqual(proxysql.read_status(conn)['pending']['servers']['runtime'], [])

    def test_a_runtime_row_the_monitor_keeps_is_pending(self):
        # Live on 3.0.11: a row moved from 30 to 50 stayed ONLINE in reader hostgroup 32 too.
        conn = self.admin()
        conn.db.execute("UPDATE main.mysql_servers SET hostgroup_id = 50 WHERE hostname = 'mf-ic-3'")
        conn.db.execute("INSERT INTO main.runtime_mysql_servers VALUES (50, 'mf-ic-3', 3306, 0, 'ONLINE', 100, 0, 1000, "
                        "0, 1, 0, 'ic-3')")
        report = proxysql.read_status(conn)
        self.assertEqual(report['pending']['servers']['runtime'], [
            "hostgroup 32, mf-ic-3:3306: in runtime (the Group Replication monitor's hostgroups), not in memory"])

    def test_offline_hard_configuration_is_shown_without_runtime(self):
        conn = self.admin()
        conn.db.execute("UPDATE main.mysql_servers SET status = 'OFFLINE_HARD' WHERE hostname = 'mf-ic-3'")
        conn.load('servers')
        report = proxysql.read_status(conn)
        row = next(s for s in report['servers'] if s['hostname'] == 'mf-ic-3')
        self.assertEqual((row['key'], row['runtime_status'], row['tone']), ('cfg|30|mf-ic-3|3306', None, 'watch'))
        self.assertEqual(report['pending']['servers']['runtime'], [])

    def test_hostgroup_without_online_server_and_rule_to_nowhere_are_problems(self):
        conn = self.admin()
        conn.db.execute("UPDATE main.mysql_servers SET status = 'OFFLINE_SOFT' WHERE hostname IN ('mf-ic-2', 'mf-ic-3')")
        conn.db.execute("INSERT INTO main.mysql_query_rules (%s) VALUES (40," % RULE_NAMES + " 1, NULL, NULL, 0, '^INSERT', NULL, 0, 99, 1, '')")
        conn.load('servers')
        conn.load('query_rules')
        report = proxysql.read_status(conn)
        self.assertEqual(report['status'], 'problem')
        self.assertIn('No server is ONLINE in hostgroup 32, so ProxySQL cannot send its traffic anywhere.',
                      report['status_why'])
        self.assertIn('Active rule 40 sends to hostgroup 99, which has no server in runtime.', report['status_why'])

    def test_routing_is_judged_by_the_rules_proxysql_runs(self):
        # Codex review: rule 40 routes to an empty hostgroup in runtime while memory already has it inactive.
        conn = self.admin()
        conn.db.execute("INSERT INTO main.mysql_query_rules (%s) VALUES (40," % RULE_NAMES + " 1, NULL, NULL, 0, '^INSERT', NULL, 0, 99, 1, '')")
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
        conn = self.admin()
        conn.db.execute("INSERT INTO main.mysql_query_rules (%s) VALUES (40," % RULE_NAMES + " 1, NULL, NULL, 0, '^INSERT', NULL, 0, 99, 1, '')")
        report = proxysql.read_status(conn)
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(len(report['pending']['query_rules']['runtime']), 1)

    def test_unread_runtime_routing_is_unknown(self):
        conn = self.admin(missing=('runtime_mysql_query_rules',))
        report = proxysql.read_status(conn)
        self.assertEqual(report['status'], 'unknown')
        self.assertTrue(any('could not be read (runtime_mysql_query_rules)' in w for w in report['status_why']),
                        report['status_why'])
        # A problem still shows over unknown.
        conn = self.admin(missing=('runtime_mysql_query_rules',))
        conn.db.execute("UPDATE main.mysql_servers SET status = 'OFFLINE_SOFT' WHERE hostname IN ('mf-ic-2', 'mf-ic-3')")
        conn.load('servers')
        self.assertEqual(proxysql.read_status(conn)['status'], 'problem')
        # Neither runtime_mysql_servers nor the pool: no hostgroup is known to have a server.
        conn = self.admin(missing=('runtime_mysql_servers', 'stats_mysql_connection_pool'))
        report = proxysql.read_status(conn)
        self.assertEqual(report['status'], 'unknown')
        self.assertFalse(any(w.startswith('Active rule') for w in report['status_why']), report['status_why'])

    def test_sql_null_and_the_text_null_are_different_values(self):
        # Codex review: batch output prints both as NULL; the client's XML output does not.
        conn = self.admin()
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

    def test_a_control_character_in_a_value_is_read_exactly_never_misread(self):
        # The client prints C0 controls raw, which XML forbids; perf_common.named_rows
        # escapes them reversibly, so the value is read exactly (never replaced).
        conn = self.admin()
        conn.db.execute("UPDATE main.mysql_query_rules SET comment = 'a' || char(1) || 'b' WHERE rule_id = 30")
        report = proxysql.read_status(conn)
        self.assertTrue(report['config_readable'])
        rule = next(r for r in report['query_rules'] if r['rule_id'] == 30)
        self.assertEqual(rule['comment'], 'a\x01b')
        self.assertFalse([n for n in report['notes'] if n.startswith('Not read:')])

    def test_a_table_is_absent_only_when_every_copy_says_no_such_table(self):
        # Codex review: memory and runtime unread, disk readable, was taken for a table this version lacks.
        conn = self.admin(missing=('main.mysql_query_rules_fast_routing', 'runtime_mysql_query_rules_fast_routing'))
        conn.db.execute("INSERT INTO main.mysql_query_rules_fast_routing VALUES ('app', 'db', 0, 99, '')")
        report = proxysql.read_status(conn)
        self.assertIsNone(report['pending']['query_rules']['runtime'])
        self.assertEqual([a['id'] for a in report['query_rules'][0]['actions']], [])
        self.assertEqual([a['id'] for a in report['actions']], ['servers_load', 'servers_save'])
        for action, target in (('rule_disable', {'rule_id': 30}), ('query_rules_load', None), ('query_rules_save', None)):
            with self.assertRaises(ValueError, msg=action):
                proxysql.plan_action(report, action, target)
        # Every copy unreadable for another reason (not "no such table"): unknown too.
        conn = self.admin(missing=('mysql_servers_ssl_params',), missing_text='ProxySQL Admin Error: database is locked')
        report = proxysql.read_status(conn)
        self.assertIsNone(report['pending']['servers']['runtime'])
        self.assertIsNone(report['pending']['servers']['disk'])
        with self.assertRaises(ValueError):
            proxysql.plan_action(report, 'server_weight', {'config': [30, 'mf-ic-2', 3306]}, 50)
        self.assertTrue(all(s['actions'] == [] for s in report['servers']))
        self.assertEqual([a['id'] for a in report['actions']], ['query_rules_load', 'query_rules_save'])
        # Every copy "no such table" (a version without it): passed over.
        conn = self.admin(missing=('mysql_servers_ssl_params',))
        report = proxysql.read_status(conn)
        self.assertEqual(report['pending']['servers'], {'runtime': [], 'disk': []})
        proxysql.plan_action(report, 'server_weight', {'config': [30, 'mf-ic-2', 3306]}, 50)


class TestActionsOffered(Home):
    def setUp(self):
        super().setUp()
        self.report = proxysql.read_status(self.admin())

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

    @staticmethod
    def catalog_targets(report):
        """Every control location in the place, including duplicate runtime rows."""
        return [(None, report['actions'])] + [
            ({'config': s['config_key']}, s['actions']) for s in report['servers'] if s.get('config_key')
        ] + [({'rule_id': r['rule_id']}, r['actions']) for r in report['query_rules']]

    def assert_identity_blocks_changes_only(self, conn, baseline=None):
        from test_chart_data import validate
        baseline = self.report if baseline is None else baseline
        report = proxysql.read_status(conn)
        validate(report, SCHEMA)
        self.assertTrue(report['available'])
        self.assertTrue(report['is_proxysql'])
        self.assertTrue(report['config_readable'])
        self.assertEqual(report['summary'], baseline['summary'])
        self.assertEqual(report['query_digests'], baseline['query_digests'])
        self.assertEqual(report['pending'], baseline['pending'])
        # The shared block must not remove server, rule, Load or Save controls.
        self.assertEqual(self.catalog_targets(report), self.catalog_targets(baseline))
        for target, entries in self.catalog_targets(report):
            self.assertTrue(entries, target)
            self.assertEqual(proxysql.action_catalog(report, target), entries)
            self.assertEqual(proxysql.available_actions(report, target), [])
            for entry in entries:
                with self.subTest(target=target, action=entry['id']):
                    with self.assertRaises(reviewed_writes.ActionUnavailable) as caught:
                        proxysql.plan_action(report, entry['id'], target, 50)
                    self.assertEqual(str(caught.exception), report['message'])
        self.assertEqual(conn.writes, [])
        return report

    def test_before_240_keeps_every_change_listed_with_upgrade_reason(self):
        for version in ('2.0.6', '2.3.2-1-gabcdef'):
            for missing in ((), ('disk.global_settings',)):
                with self.subTest(version=version, missing=missing):
                    report = self.assert_identity_blocks_changes_only(
                        self.admin(version=version, server_uuid=None, missing=missing))
                    self.assertEqual(report['server_identity'], {})
                    self.assertIn('does not report an instance UUID', report['message'])
                    self.assertIn('ProxySQL 2.4.0 or later', report['message'])
                    self.assertIn('Upgrade ProxySQL to change it here.', report['message'])

    def test_240_and_3x_need_a_readable_uuid_even_when_the_version_is_supported(self):
        for version in ('2.4.0', VERSION):
            for missing, error in (((), 'ProxySQL Admin Error: no such table: %s'),
                                   (('disk.global_settings',), 'ProxySQL Admin Error: no such table: %s'),
                                   (('disk.global_settings',), 'ProxySQL Admin Error: permission denied')):
                with self.subTest(version=version, missing=missing, error=error):
                    report = self.assert_identity_blocks_changes_only(
                        self.admin(version=version, server_uuid=None, missing=missing, missing_text=error))
                    self.assertEqual(report['server_identity'], {})
                    self.assertIn('UUID could not be read', report['message'])
                    self.assertIn('disk.global_settings', report['message'])
                    self.assertIn('refresh to change it here.', report['message'])
                    self.assertNotIn('Upgrade ProxySQL', report['message'])

    def test_malformed_persisted_uuid_keeps_catalog_but_cannot_plan_any_change(self):
        for value in ('', 'not-a-uuid', ADMIN_UUID + '\n', ADMIN_UUID + '\r',
                      ADMIN_UUID.replace('-', ''), 'g' + ADMIN_UUID[1:],
                      ADMIN_UUID + "'; DROP TABLE mysql_servers; --"):
            with self.subTest(uuid=value):
                report = self.assert_identity_blocks_changes_only(self.admin(server_uuid=value))
                self.assertIn('valid ProxySQL instance UUID', report['message'])
                if value:
                    self.assertNotIn(value, report['message'])

    def test_uuid_block_also_keeps_online_move_and_enable_controls(self):
        conn = self.admin()
        conn.db.execute("INSERT INTO main.mysql_servers VALUES "
                        "(50, 'mf-app-1', 3306, 0, 'OFFLINE_SOFT', 1, 0, 100, 0, 0, 0, 'app')")
        conn.db.execute('UPDATE main.mysql_query_rules SET active = 0 WHERE rule_id = 30')
        conn.load('servers')
        conn.load('query_rules')
        baseline = proxysql.read_status(conn)
        conn.db.execute("DELETE FROM disk.global_settings WHERE variable_name = 'uuid'")
        report = self.assert_identity_blocks_changes_only(conn, baseline)
        self.assertEqual({a['id'] for _, entries in self.catalog_targets(report) for a in entries},
                         {'server_online', 'server_offline_soft', 'server_offline_hard', 'server_weight',
                          'server_hostgroup', 'rule_enable', 'rule_disable', 'servers_load', 'servers_save',
                          'query_rules_load', 'query_rules_save'})

    def test_valid_uuid_enables_the_same_catalog_on_240_later_2x_and_3x(self):
        uuid = 'ABCDEF01-2345-6789-ABCD-EF0123456789'
        for version in ('2.4.0', '2.7.3', VERSION):
            with self.subTest(version=version):
                report = proxysql.read_status(self.admin(version=version, server_uuid=uuid))
                self.assertIsNone(report['message'])
                for target, entries in self.catalog_targets(report):
                    self.assertEqual(proxysql.available_actions(report, target), entries)
                    for entry in entries:
                        plan = proxysql.plan_action(report, entry['id'], target, 50)
                        self.assertEqual(plan['expect']['server_identity'], {'uuid': uuid})
                        self.assertIn(uuid, plan['session_guard'][0])

    def test_unsafe_host_names_offer_nothing(self):
        conn = self.admin()
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
        self.conn = self.admin()
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
        self.conn = self.admin()
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
                                                 '/api/live/readyset/plan', '/api/live/readyset/action',
                                                 '/api/live/cluster/plan', '/api/live/cluster/action'})
        self.assertIn('/api/live/proxysql', ui._SERVER_VIEW_PATHS)
        self.assertIn('/api/live/proxysql/action', ui._WRITE_PATHS)
        self.assertNotIn('/api/live/proxysql/action', ui._SERVER_VIEW_PATHS)
        self.assertNotIn('/api/live/proxysql/plan', ui._WRITE_PATHS)

    def test_reviewed_statements_are_the_sent_statements(self):
        plan = self.plan('server_offline_soft', {'config': [30, 'mf-ic-2', 3306]})
        self.assertEqual(plan['policy']['action'], 'confirm')
        variables = self.conn.rows('SELECT * FROM main.global_variables')
        result = self.send(plan)
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(plan['expect']['server_identity'], {'uuid': ADMIN_UUID})
        self.assertEqual(self.conn.writes, ['\n'.join(plan['session_guard'] + plan['statements'])])
        self.assertEqual(result['session_guard'], plan['session_guard'])
        self.assertEqual(self.conn.rows('SELECT * FROM main.global_variables'), variables,
                         'the matching guard inserts no rows')
        self.assertEqual(self.conn.rows("SELECT status FROM main.mysql_servers WHERE hostname='mf-ic-2'"),
                         [('OFFLINE_SOFT',)])
        self.assertEqual(self.conn.rows("SELECT status FROM disk.mysql_servers WHERE hostname='mf-ic-2'"),
                         [('ONLINE',)], 'nothing is saved to disk')
        self.assertEqual(result['after']['runtime'], [{'hostgroup_id': 32, 'status': 'OFFLINE_SOFT', 'weight': 100}])
        self.assertEqual(result['next']['action'], 'servers_save')
        (record,) = self.audit()
        self.assertEqual((record['status'], record['outcome'], record['operation']), ('ok', 'applied', 'proxysql_action'))
        self.assertEqual(record['statements'], plan['session_guard'] + plan['statements'])
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

    def test_instance_changed_after_review_is_refused_before_write_login(self):
        plan = self.plan('server_weight', {'config': [30, 'mf-ic-2', 3306]}, 50)
        self.conn.db.execute("UPDATE disk.global_settings SET variable_value = ? WHERE variable_name = 'uuid'",
                             ('aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',))
        result = self.send(plan)
        self.assertEqual((result['status'], result['outcome']), ('refused', 'changed'))
        self.assertEqual(self.conn.writes, [])
        self.assertEqual(self.conn.rows("SELECT weight FROM main.mysql_servers WHERE hostname='mf-ic-2'"), [(100,)])
        (record,) = self.audit()
        self.assertEqual((record['status'], record['outcome']), ('refused', 'changed'))

    def test_presend_refusals_audit_only_the_reviewed_statements(self):
        for failure in ('changed_instance', 'typed_guard', 'confirmation'):
            with self.subTest(failure=failure):
                self.conn = self.admin()
                plan = self.plan('server_offline_hard', {'config': [30, 'mf-ic-2', 3306]})
                previous = len(self.audit())
                if failure == 'changed_instance':
                    self.conn.db.execute("UPDATE disk.global_settings SET variable_value = ? WHERE variable_name = 'uuid'",
                                         (OTHER_ADMIN_UUID,))
                typed = 'wrong-host' if failure == 'typed_guard' else plan['typed_guard']
                result = self.send(plan, typed=typed, confirmed=failure != 'confirmation')
                self.assertEqual(result['status'], 'refused', result)
                self.assertEqual(self.conn.writes, [])
                records = self.audit()
                self.assertEqual(len(records), previous + 1)
                self.assertEqual(records[0]['status'], 'refused')
                self.assertEqual(records[0]['statements'], plan['statements'])

    def test_a_mixed_instance_fresh_read_refuses_without_a_write_login(self):
        for query in ('SELECT * FROM main.mysql_servers', 'SELECT * FROM disk.mysql_query_rules',
                      'SELECT * FROM stats_mysql_query_rules', 'FROM runtime_mysql_users'):
            with self.subTest(query=query):
                self.conn = self.admin()
                plan = self.plan('server_weight', {'config': [30, 'mf-ic-2', 3306]}, 50)
                previous_audits = len(self.audit())
                with self.other_instance_read(self.conn, query):
                    result = self.send(plan)
                self.assertEqual(result['status'], 'refused', result)
                self.assertEqual(result['sent'], [])
                self.assertEqual(result['states'], ['not_sent'] * len(plan['statements']))
                self.assertRegex(result['message'].lower(), r'(uuid|identity|identified)')
                self.assertEqual(self.conn.writes, [])
                self.assertEqual(self.conn.rows("SELECT weight FROM main.mysql_servers WHERE hostname='mf-ic-2'"),
                                 [(100,)])
                records = self.audit()
                self.assertEqual(len(records), previous_audits + 1)
                self.assertEqual(records[0]['status'], 'refused')

    def test_a_mixed_status_read_returns_an_error_instead_of_empty_counts(self):
        for query in (proxysql._ADMIN_VERSION_SQL, 'SELECT @@version', 'SELECT * FROM main.mysql_servers'):
            with self.subTest(query=query), self.other_instance_read(self.conn, query), \
                    self.assertRaises(ConnectorError) as caught:
                self.api('/api/live/proxysql', {'connection': self.connection})
            self.assertEqual(str(caught.exception), proxysql._EVIDENCE_IDENTITY_BLOCK)

    def test_optional_table_errors_with_unbound_prefix_refuse_the_fresh_apply_read(self):
        for mode in ('missing_identity_prefix', 'missing_identity_suffix', 'different_prefix', 'empty_prefix',
                     'missing_prefix', 'transport'):
            with self.subTest(mode=mode):
                self.conn = self.admin()
                plan = self.plan('servers_load')
                with self.unidentified_optional_read(self.conn, mode):
                    result = self.send(plan)
                self.assertEqual(result['status'], 'refused', result)
                self.assertEqual(result['sent'], [])
                self.assertEqual(result['states'], ['not_sent'])
                self.assertEqual(self.conn.writes, [])
                self.assertEqual(self.audit()[0]['status'], 'refused')

    def test_after_state_requires_the_same_valid_instance_uuid(self):
        for identity in (OTHER_ADMIN_UUID, None, 'not-a-uuid', 'unreadable'):
            with self.subTest(identity=identity):
                self.conn = self.admin()
                plan = self.plan('server_weight', {'config': [30, 'mf-ic-2', 3306]}, 50)
                original = self.conn.run_write

                def switch_after_write(sql, *args, **kwargs):
                    answer = original(sql, *args, **kwargs)
                    if identity == 'unreadable':
                        self.conn.missing.add('disk.global_settings')
                    elif identity is None:
                        self.conn.db.execute("DELETE FROM disk.global_settings WHERE variable_name = 'uuid'")
                    else:
                        self.conn.db.execute("UPDATE disk.global_settings SET variable_value = ? "
                                             "WHERE variable_name = 'uuid'", (identity,))
                    return answer

                with mock.patch.object(self.conn, 'run_write', side_effect=switch_after_write):
                    result = self.send(plan)
                self.assertEqual(result['status'], 'done', result)
                self.assertEqual(result['states'], ['done'] * len(plan['statements']))
                self.assertEqual(result['sent'], plan['statements'])
                self.assertIsNone(result['after'], 'another instance must not be shown as the after-state')
                self.assertIsNone(result['effect'], 'an unbound readback proves no effect')
                self.assertIsNone(result['next'])
                self.assertRegex(result['message'].lower(), r'(read|after)')
                self.assertRegex(result['message'].lower(), r'(uuid|identity|instance)')
                self.assertIn('refresh', result['message'].lower())
                self.assertEqual(self.conn.rows("SELECT weight FROM main.mysql_servers WHERE hostname='mf-ic-2'"),
                                 [(50,)], 'the acknowledged write remains applied')
                record = self.audit()[0]
                self.assertEqual((record['status'], record['outcome']), ('ok', 'applied'))
                self.assertIsNone(record['after'])
                self.assertEqual(record['message'], result['message'])

    def test_a_failed_or_non_admin_readback_keeps_the_acknowledged_write_result(self):
        for mode in ('not_proxy', 'connection_failure'):
            with self.subTest(mode=mode):
                self.conn = self.admin()
                plan = self.plan('server_weight', {'config': [30, 'mf-ic-2', 3306]}, 50)
                original_read, original_write = self.conn.run, self.conn.run_write
                wrote = [False]
                other = NotProxy()

                def write(sql, *args, **kwargs):
                    answer = original_write(sql, *args, **kwargs)
                    wrote[0] = True
                    return answer

                def read(sql, *args, **kwargs):
                    if not wrote[0]:
                        return original_read(sql, *args, **kwargs)
                    if mode == 'not_proxy':
                        return other.run(sql, *args, **kwargs)
                    raise ConnectorError('mysql client exited 1: ERROR 2013 (HY000): Lost connection to server')

                with mock.patch.object(self.conn, 'run_write', side_effect=write), \
                        mock.patch.object(self.conn, 'run', side_effect=read):
                    result = self.send(plan)
                self.assertEqual(result['status'], 'done', result)
                self.assertEqual(result['sent'], plan['statements'])
                self.assertIsNone(result['after'])
                self.assertIsNone(result['effect'])
                self.assertIsNone(result['next'])
                self.assertIn('read after', result['message'].lower())
                self.assertIn('refresh', result['message'].lower())
                self.assertEqual(len(self.conn.writes), 1, 'a failed readback must never retry the write')
                self.assertEqual(self.audit()[0]['message'], result['message'])

    def test_after_state_rejects_coerced_identity_metadata(self):
        # The UUID and its real Admin guard remain valid. Additional reviewed
        # identity fields must preserve their JSON types on the readback too.
        original_read = proxysql.read_status
        for reviewed, current in ((True, 1), (True, 1.0), (1, 1.0)):
            with self.subTest(reviewed=reviewed, current=current, kind=type(current).__name__):
                self.conn = self.admin()

                def read(*args, **kwargs):
                    report = original_read(*args, **kwargs)
                    report['server_identity']['evidence'] = [{'value': current if self.conn.writes else reviewed}]
                    return report

                with mock.patch.object(proxysql, 'read_status', side_effect=read):
                    plan = self.plan('server_weight', {'config': [30, 'mf-ic-2', 3306]}, 50)
                    result = self.send(plan)
                self.assertEqual((result['status'], result['sent']), ('done', plan['statements']))
                self.assertEqual(len(self.conn.writes), 1)
                self.assertIsNone(result['after'])
                self.assertIsNone(result['effect'])
                self.assertIsNone(result['next'])
                self.assertIn('different ProxySQL instance', result['message'])
                self.assertIsNone(self.audit()[0]['after'])

    def test_a_mixed_instance_after_read_cannot_claim_an_effect(self):
        plan = self.plan('server_weight', {'config': [30, 'mf-ic-2', 3306]}, 50)
        original_read = self.conn.run
        original_write = self.conn.run_write
        wrote = [False]

        def write(sql, *args, **kwargs):
            answer = original_write(sql, *args, **kwargs)
            wrote[0] = True
            return answer

        def read(sql, *args, **kwargs):
            if wrote[0] and 'SELECT * FROM main.mysql_servers' in sql:
                self.conn.db.execute("UPDATE disk.global_settings SET variable_value = ? WHERE variable_name = 'uuid'",
                                     (OTHER_ADMIN_UUID,))
                try:
                    return original_read(sql, *args, **kwargs)
                finally:
                    self.conn.db.execute("UPDATE disk.global_settings SET variable_value = ? "
                                         "WHERE variable_name = 'uuid'", (ADMIN_UUID,))
            return original_read(sql, *args, **kwargs)

        with mock.patch.object(self.conn, 'run_write', side_effect=write), \
                mock.patch.object(self.conn, 'run', side_effect=read):
            result = self.send(plan)
        self.assertEqual(result['status'], 'done', result)
        self.assertIsNone(result['after'])
        self.assertIsNone(result['effect'])
        self.assertIsNone(result['next'])
        self.assertRegex(result['message'].lower(), r'(uuid|identity|instance)')
        self.assertIsNone(self.audit()[0]['after'])

    def test_unread_state_after_a_valid_identity_offers_no_followup(self):
        actions = (('server_weight', {'config': [30, 'mf-ic-2', 3306]}, 50, 'mysql_servers'),
                   ('rule_disable', {'rule_id': 30}, None, 'mysql_query_rules'))
        for action, target, value, table in actions:
            for copy in ('main.%s', 'runtime_%s', 'disk.%s'):
                with self.subTest(action=action, copy=copy):
                    self.conn = self.admin()
                    plan = self.plan(action, target, value)
                    self.assertIsNotNone(plan['next'])
                    original = self.conn.run_write

                    def make_copy_unreadable(sql, *args, **kwargs):
                        answer = original(sql, *args, **kwargs)
                        self.conn.missing.add(copy % table)
                        return answer

                    with mock.patch.object(self.conn, 'run_write', side_effect=make_copy_unreadable):
                        result = self.send(plan)
                    self.assertEqual(result['status'], 'done', result)
                    self.assertIsNone(result['next'])
                    self.assertIsNone(result['after'])
                    self.assertIsNone(result['effect'])
                    self.assertIn('refresh', result['message'].lower())
                    self.assertEqual(self.audit()[0]['message'], result['message'])

    def test_verified_readback_keeps_the_reviewed_followup(self):
        cases = (('server_weight', {'config': [30, 'mf-ic-2', 3306]}, 50, 'servers_save'),
                 ('rule_disable', {'rule_id': 30}, None, 'query_rules_save'))
        for action, target, value, next_action in cases:
            with self.subTest(action=action):
                self.conn = self.admin()
                result = self.send(self.plan(action, target, value))
                self.assertEqual(result['status'], 'done', result)
                self.assertIsNotNone(result['after'])
                self.assertEqual(result['next']['action'], next_action)

    def test_verified_rule_enable_and_hostgroup_move_keep_their_followups(self):
        self.conn.db.execute('UPDATE main.mysql_query_rules SET active = 0 WHERE rule_id = 30')
        self.conn.load('query_rules')
        plan = self.plan('rule_enable', {'rule_id': 30})
        result = self.send(plan)
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(result['next']['action'], 'query_rules_save')

        self.conn = self.admin()
        self.conn.db.execute("INSERT INTO main.mysql_servers VALUES "
                             "(50, 'mf-app-1', 3306, 0, 'ONLINE', 100, 0, 100, 0, 0, 0, 'app')")
        self.conn.load('servers')
        plan = self.plan('server_hostgroup', {'config': [50, 'mf-app-1', 3306]}, 70)
        result = self.send(plan, typed=plan['typed_guard'])
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(result['after']['config']['hostgroup_id'], 70)
        self.assertEqual(result['next']['action'], 'servers_save')

    def test_offline_hard_allows_a_verified_absent_runtime_row(self):
        plan = self.plan('server_offline_hard', {'config': [30, 'mf-ic-2', 3306]})
        result = self.send(plan, typed='mf-ic-2:3306')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(result['after']['runtime'], [])
        self.assertEqual(result['next']['action'], 'servers_save')

    def test_recognized_runtime_lag_status_does_not_make_state_unreadable(self):
        plan = self.plan('server_weight', {'config': [30, 'mf-ic-2', 3306]}, 50)
        original = self.conn.run_write

        def write(sql, *args, **kwargs):
            answer = original(sql, *args, **kwargs)
            self.conn.db.execute("UPDATE main.runtime_mysql_servers SET status = 'SHUNNED_REPLICATION_LAG' "
                                 "WHERE hostname = 'mf-ic-2'")
            return answer

        with mock.patch.object(self.conn, 'run_write', side_effect=write):
            result = self.send(plan)
        self.assertEqual(result['status'], 'done', result)
        self.assertIsNotNone(result['after'])
        self.assertEqual(result['next']['action'], 'servers_save')

    def test_missing_or_malformed_rule_active_readback_is_not_verified(self):
        for copy, action in (('main.mysql_query_rules', 'rule_disable'),
                             ('main.runtime_mysql_query_rules', 'rule_enable')):
            for value in ('missing', None, 'not-a-boolean'):
                with self.subTest(copy=copy, value=value):
                    self.conn = self.admin()
                    if action == 'rule_enable':
                        self.conn.db.execute('UPDATE main.mysql_query_rules SET active = 0 WHERE rule_id = 30')
                        self.conn.load('query_rules')
                    plan = self.plan(action, {'rule_id': 30})
                    original_read, original_write = self.conn._execute, self.conn.run_write
                    wrote = [False]

                    def write(sql, *args, **kwargs):
                        answer = original_write(sql, *args, **kwargs)
                        wrote[0] = True
                        return answer

                    def read(sql, *args, **kwargs):
                        rows = original_read(sql, *args, **kwargs)
                        if wrote[0] and sql.strip().rstrip(';') == 'SELECT * FROM ' + copy:
                            active = rows[0].index('active')
                            if value == 'missing':
                                rows = [row[:active] + row[active + 1:] for row in rows]
                            else:
                                rule_id = rows[0].index('rule_id')
                                for row in rows[1:]:
                                    if row[rule_id] == 30:
                                        row[active] = value
                        return rows

                    with mock.patch.object(self.conn, 'run_write', side_effect=write), \
                            mock.patch.object(self.conn, '_execute', side_effect=read):
                        result = self.send(plan)
                    self.assertEqual(result['status'], 'done', result)
                    self.assertIsNone(result['after'])
                    self.assertIsNone(result['effect'])
                    self.assertIsNone(result['next'])
                    self.assertIn('refresh', result['message'].lower())

    def test_followup_is_suppressed_if_the_readable_postcondition_has_changed(self):
        cases = (
            ('server_weight', {'config': [30, 'mf-ic-2', 3306]}, 50,
             "UPDATE main.mysql_servers SET weight = 60 WHERE hostname = 'mf-ic-2'", 'servers'),
            ('server_offline_soft', {'config': [30, 'mf-ic-2', 3306]}, None,
             "UPDATE main.mysql_servers SET status = 'ONLINE' WHERE hostname = 'mf-ic-2'", 'servers'),
            ('rule_disable', {'rule_id': 30}, None,
             'UPDATE main.mysql_query_rules SET active = 1 WHERE rule_id = 30', 'query_rules'),
            ('rule_enable', {'rule_id': 30}, None,
             'UPDATE main.mysql_query_rules SET active = 0 WHERE rule_id = 30', 'query_rules'),
        )
        for action, target, value, drift, module in cases:
            with self.subTest(action=action):
                self.conn = self.admin()
                if action == 'rule_enable':
                    self.conn.db.execute('UPDATE main.mysql_query_rules SET active = 0 WHERE rule_id = 30')
                    self.conn.load('query_rules')
                plan = self.plan(action, target, value)
                original = self.conn.run_write

                def change_after_write(sql, *args, **kwargs):
                    answer = original(sql, *args, **kwargs)
                    self.conn.db.execute(drift)
                    self.conn.load(module)
                    return answer

                with mock.patch.object(self.conn, 'run_write', side_effect=change_after_write):
                    result = self.send(plan)
                self.assertEqual(result['status'], 'done', result)
                self.assertIsNotNone(result['after'], 'the actual, verified state remains visible')
                self.assertIsNone(result['next'])
                self.assertIn('does not confirm the expected', result['message'])
                self.assertIn('refresh', result['message'].lower())
                self.assertEqual(self.audit()[0]['message'], result['message'])

    def test_followup_requires_the_whole_reviewed_module_to_remain_unchanged(self):
        cases = (
            ('server_weight', {'config': [30, 'mf-ic-2', 3306]}, 50,
             "UPDATE main.mysql_servers SET weight = 77 WHERE hostname = 'mf-ic-3'", 'servers'),
            ('rule_disable', {'rule_id': 30}, None,
             "UPDATE main.mysql_query_rules SET comment = 'different' WHERE rule_id = 20", 'query_rules'),
            ('servers_load', None, None,
             "UPDATE main.mysql_servers SET weight = 77 WHERE hostname = 'mf-ic-3'", 'servers'),
            ('query_rules_load', None, None,
             "UPDATE main.mysql_query_rules SET comment = 'different' WHERE rule_id = 20", 'query_rules'),
        )
        for action, target, value, drift, module in cases:
            with self.subTest(action=action):
                self.conn = self.admin()
                if action == 'servers_load':
                    self.conn.db.execute("UPDATE main.mysql_servers SET weight = 50 WHERE hostname = 'mf-ic-2'")
                elif action == 'query_rules_load':
                    self.conn.db.execute('UPDATE main.mysql_query_rules SET active = 0 WHERE rule_id = 30')
                plan = self.plan(action, target, value)
                original = self.conn.run_write

                def change_after_write(sql, *args, **kwargs):
                    answer = original(sql, *args, **kwargs)
                    self.conn.db.execute(drift)
                    self.conn.load(module)
                    return answer

                with mock.patch.object(self.conn, 'run_write', side_effect=change_after_write):
                    result = self.send(plan)
                self.assertEqual(result['status'], 'done', result)
                self.assertIsNotNone(result['after'])
                self.assertIsNone(result['next'])
                self.assertIn('does not confirm the expected', result['message'])
                self.assertIn('refresh', result['message'].lower())

    def test_unread_runtime_server_fields_do_not_offer_save(self):
        for column in ('weight', 'status', 'hostgroup_id'):
            with self.subTest(column=column):
                self.conn = self.admin()
                plan = self.plan('server_weight', {'config': [30, 'mf-ic-2', 3306]}, 50)
                original_read, original_write = self.conn._execute, self.conn.run_write
                wrote = [False]

                def write(sql, *args, **kwargs):
                    answer = original_write(sql, *args, **kwargs)
                    wrote[0] = True
                    return answer

                def read(sql, *args, **kwargs):
                    rows = original_read(sql, *args, **kwargs)
                    if wrote[0] and sql.strip().rstrip(';') == 'SELECT * FROM main.runtime_mysql_servers':
                        index = rows[0].index(column)
                        rows = [row[:index] + row[index + 1:] for row in rows]
                    return rows

                with mock.patch.object(self.conn, 'run_write', side_effect=write), \
                        mock.patch.object(self.conn, '_execute', side_effect=read):
                    result = self.send(plan)
                self.assertEqual(result['status'], 'done', result)
                self.assertIsNone(result['next'])
                self.assertIn('refresh', result['message'].lower())

    def test_module_load_without_rule_active_evidence_offers_no_save(self):
        for copy in ('main.mysql_query_rules', 'main.runtime_mysql_query_rules', 'disk.mysql_query_rules'):
            with self.subTest(copy=copy):
                self.conn = self.admin()
                self.conn.db.execute('UPDATE main.mysql_query_rules SET active = 0 WHERE rule_id = 30')
                plan = self.plan('query_rules_load')
                original_read, original_write = self.conn._execute, self.conn.run_write
                wrote = [False]

                def write(sql, *args, **kwargs):
                    answer = original_write(sql, *args, **kwargs)
                    wrote[0] = True
                    return answer

                def read(sql, *args, **kwargs):
                    rows = original_read(sql, *args, **kwargs)
                    if wrote[0] and sql.strip().rstrip(';') == 'SELECT * FROM ' + copy:
                        index = rows[0].index('active')
                        rows = [row[:index] + row[index + 1:] for row in rows]
                    return rows

                with mock.patch.object(self.conn, 'run_write', side_effect=write), \
                        mock.patch.object(self.conn, '_execute', side_effect=read):
                    result = self.send(plan)
                self.assertEqual(result['status'], 'done', result)
                self.assertIsNone(result['next'])
                self.assertIsNone(result['after'])
                self.assertIn('refresh', result['message'].lower())
                self.assertNotIn('Loaded to runtime', result['message'])

    def test_followup_requires_non_key_columns_seen_before_the_send(self):
        cases = (
            ('query_rules_load', 'runtime_mysql_query_rules', 'destination_hostgroup'),
            ('query_rules_load', 'disk.mysql_query_rules', 'apply'),
            ('query_rules_load', 'mysql_query_rules', 'match_digest'),
            ('server_weight', 'main.runtime_mysql_servers', 'max_connections'),
            ('server_weight', 'disk.mysql_servers', 'use_ssl'),
            ('server_weight', 'main.runtime_mysql_galera_hostgroups', 'active'),
        )
        for action, table, column in cases:
            with self.subTest(action=action, table=table, column=column):
                self.conn = self.admin()
                if action == 'query_rules_load':
                    self.conn.db.execute('UPDATE main.mysql_query_rules SET active = 0 WHERE rule_id = 30')
                    plan = self.plan(action)
                else:
                    plan = self.plan(action, {'config': [30, 'mf-ic-2', 3306]}, 50)
                original_read, original_write = self.conn._execute, self.conn.run_write
                wrote = [False]

                def write(sql, *args, **kwargs):
                    answer = original_write(sql, *args, **kwargs)
                    wrote[0] = True
                    return answer

                def read(sql, *args, **kwargs):
                    rows = original_read(sql, *args, **kwargs)
                    if wrote[0] and table in sql and column in rows[0]:
                        index = rows[0].index(column)
                        rows = [row[:index] + row[index + 1:] for row in rows]
                    return rows

                with mock.patch.object(self.conn, 'run_write', side_effect=write), \
                        mock.patch.object(self.conn, '_execute', side_effect=read):
                    result = self.send(plan)
                self.assertEqual(result['status'], 'done', result)
                self.assertIsNone(result['next'])
                self.assertIsNone(result['after'])
                self.assertIn('refresh', result['message'].lower())
                self.assertIn('acknowledged', result['message'])

    def test_missing_monitor_active_value_offers_no_save(self):
        self.conn.db.execute('UPDATE main.mysql_servers SET weight = 50 WHERE hostname = \'mf-ic-2\'')
        plan = self.plan('servers_load')
        original_read, original_write = self.conn._execute, self.conn.run_write
        wrote = [False]

        def write(sql, *args, **kwargs):
            answer = original_write(sql, *args, **kwargs)
            wrote[0] = True
            return answer

        def read(sql, *args, **kwargs):
            rows = original_read(sql, *args, **kwargs)
            if wrote[0] and sql.strip().rstrip(';') == 'SELECT * FROM main.runtime_mysql_galera_hostgroups':
                index = rows[0].index('active')
                for row in rows[1:]:
                    row[index] = None
            return rows

        with mock.patch.object(self.conn, 'run_write', side_effect=write), \
                mock.patch.object(self.conn, '_execute', side_effect=read):
            result = self.send(plan)
        self.assertEqual(result['status'], 'done', result)
        self.assertIsNone(result['next'])
        self.assertIsNone(result['after'])
        self.assertIn('refresh', result['message'].lower())

    def test_table_disappearing_after_the_send_is_not_treated_as_never_present(self):
        self.conn = self.admin()
        plan = self.plan('server_weight', {'config': [30, 'mf-ic-2', 3306]}, 50)
        original = self.conn.run_write

        def remove_table(sql, *args, **kwargs):
            answer = original(sql, *args, **kwargs)
            self.conn.missing.add('mysql_servers_ssl_params')
            return answer

        with mock.patch.object(self.conn, 'run_write', side_effect=remove_table):
            result = self.send(plan)
        self.assertEqual(result['status'], 'done', result)
        self.assertIsNone(result['after'])
        self.assertIsNone(result['next'])
        self.assertIn('refresh', result['message'].lower())

        # A table absent on both sides of the send remains a supported fallback.
        self.conn = self.admin(missing=('mysql_servers_ssl_params',))
        result = self.send(self.plan('server_weight', {'config': [30, 'mf-ic-2', 3306]}, 50))
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(result['next']['action'], 'servers_save')

    def test_instance_swapped_after_reread_refuses_every_action_before_any_change(self):
        actions = [('server_offline_soft', {'config': [30, 'mf-ic-2', 3306]}),
                   ('rule_disable', {'rule_id': 30}), ('servers_load', None), ('query_rules_load', None),
                   ('servers_save', None), ('query_rules_save', None)]
        for action, target in actions:
            with self.subTest(action=action):
                self.conn = self.admin()
                if action.startswith('servers_'):
                    self.conn.db.execute("UPDATE main.mysql_servers SET weight = 50 WHERE hostname='mf-ic-2'")
                elif action.startswith('query_rules_'):
                    self.conn.db.execute('UPDATE main.mysql_query_rules SET active = 0 WHERE rule_id = 30')
                plan = self.plan(action, target)
                tables = ('main.global_variables', 'main.mysql_servers', 'main.runtime_mysql_servers', 'disk.mysql_servers',
                          'main.mysql_query_rules', 'main.runtime_mysql_query_rules', 'disk.mysql_query_rules')
                before = [self.conn.rows('SELECT * FROM ' + table) for table in tables]
                previous_audits = len(self.audit())

                def swap():
                    self.conn.db.execute("UPDATE disk.global_settings SET variable_value = ? WHERE variable_name = 'uuid'",
                                         ('aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',))
                self.conn.on_write = swap
                with mock.patch.object(self.conn, 'load', wraps=self.conn.load) as load, \
                        mock.patch.object(self.conn, 'save', wraps=self.conn.save) as save:
                    result = self.send(plan)
                self.assertEqual((result['status'], result['outcome']), ('refused', 'identity_mismatch'), result)
                self.assertEqual(result['sent'], [])
                self.assertEqual(result['not_sent'], plan['statements'])
                self.assertEqual(result['states'], ['not_sent'] * len(plan['statements']))
                self.assertEqual(self.conn.writes, ['\n'.join(plan['session_guard'] + plan['statements'])])
                self.assertEqual([self.conn.rows('SELECT * FROM ' + table) for table in tables], before)
                load.assert_not_called()
                save.assert_not_called()
                records = self.audit()
                self.assertEqual(len(records), previous_audits + 1, 'complete the pending record exactly once')
                self.assertEqual((records[0]['status'], records[0]['outcome']), ('refused', 'identity_mismatch'))
                self.assertEqual(records[0]['statements'], plan['session_guard'] + plan['statements'])
                self.assertEqual(records[0]['target']['server_identity'], {'uuid': ADMIN_UUID})

    def test_local_login_refusal_removes_only_the_unsent_guard_from_the_pending_row(self):
        from myflames.connection_groups import ProfileReadError
        plan = self.plan('server_weight', {'config': [30, 'mf-ic-2', 3306]}, 50)
        attempted = []

        def refuse_login(sql, **kwargs):
            (record,) = self.audit()
            self.assertEqual(record['status'], 'pending')
            self.assertEqual(record['statements'], plan['session_guard'] + plan['statements'])
            attempted.append(record['id'])
            raise ProfileReadError('Group consent changed.')

        with mock.patch.object(self.conn, 'run_write', side_effect=refuse_login) as send:
            result = self.send(plan)
        send.assert_called_once()
        self.assertEqual(self.conn.writes, [])
        self.assertEqual((result['status'], result['outcome'], result['sent']), ('refused', 'changed', []))
        self.assertEqual(result['states'], ['not_sent'] * len(plan['statements']))
        self.assertIsNone(result['after'])
        (record,) = self.audit()
        self.assertEqual(attempted, [record['id']])
        self.assertEqual(record['id'], result['audit_id'])
        self.assertEqual((record['status'], record['outcome']), ('refused', 'changed'))
        self.assertEqual(record['statements'], plan['statements'])

    def test_identity_disappearing_in_write_login_refuses_without_loading(self):
        for removed, outcome in (('row', 'identity_mismatch'), ('table', 'identity_unavailable')):
            with self.subTest(removed=removed):
                self.conn = self.admin()
                plan = self.plan('server_offline_soft', {'config': [30, 'mf-ic-2', 3306]})
                before = self.conn.rows('SELECT * FROM main.mysql_servers')
                previous_audits = len(self.audit())
                self.conn.on_write = lambda: self.conn.db.execute(
                    "DELETE FROM disk.global_settings WHERE variable_name = 'uuid'" if removed == 'row' else
                    'DROP TABLE disk.global_settings')
                with mock.patch.object(self.conn, 'load', wraps=self.conn.load) as load:
                    result = self.send(plan)
                self.assertEqual((result['status'], result['outcome'], result['sent']), ('refused', outcome, []))
                self.assertIn('not the one you reviewed' if removed == 'row' else 'identity could not be checked',
                              result['message'])
                self.assertEqual(result['not_sent'], plan['statements'])
                self.assertEqual(self.conn.rows('SELECT * FROM main.mysql_servers'), before)
                load.assert_not_called()
                records = self.audit()
                self.assertEqual(len(records), previous_audits + 1)
                self.assertEqual((records[0]['status'], records[0]['outcome']), ('refused', outcome))

    def test_guard_stops_the_batch_with_the_exact_live_constraint_error(self):
        plan = self.plan('server_offline_soft', {'config': [30, 'mf-ic-2', 3306]})
        self.assertEqual(plan['session_guard'], [
            "INSERT INTO main.global_variables (variable_name, variable_value) SELECT NULL, NULL WHERE "
            "(SELECT variable_value FROM disk.global_settings WHERE variable_name = 'uuid') IS NOT '%s';"
            % ADMIN_UUID])
        self.conn.db.execute("UPDATE disk.global_settings SET variable_value = ? WHERE variable_name = 'uuid'",
                             ('aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',))
        variables = self.conn.rows('SELECT * FROM main.global_variables')
        with self.assertRaises(ConnectorError) as error:
            self.conn.run_write('\n'.join(plan['session_guard'] + plan['statements']))
        self.assertEqual(str(error.exception), 'mysql client exited 1: ' + GUARD_MISMATCH_ERROR)
        self.assertEqual(self.conn.rows('SELECT * FROM main.global_variables'), variables)
        self.assertEqual(self.conn.rows("SELECT status FROM main.mysql_servers WHERE hostname='mf-ic-2'"),
                         [('ONLINE',)], 'the statement after the guard never ran')

    def test_guard_error_text_distinguishes_mismatch_from_unavailable_identity(self):
        errors = ((GUARD_MISMATCH_ERROR, 'identity_mismatch', 'not the one you reviewed'),
                  ('--------------\nSELECT \'NOT NULL constraint failed: global_variables.variable_name\';\n'
                   '--------------\n' + GUARD_MISMATCH_ERROR,
                   'identity_mismatch', 'not the one you reviewed'),
                  ('ERROR 1045 (28000) at line 1: ProxySQL Admin Error: integer overflow',
                   'identity_unavailable', 'identity could not be checked'),
                  ('ERROR 1045 (28000) at line 1: ProxySQL Admin Error: '
                   'NOT NULL constraint failed: global_variables.variable_value',
                   'identity_unavailable', 'identity could not be checked'),
                  ('ERROR 1045 (28000) at line 1: ProxySQL Admin Error: no such table: disk.global_settings',
                   'identity_unavailable', 'identity could not be checked'),
                  ('ERROR 1045 (28000) at line 1: ProxySQL Admin Error: '
                   'NOT NULL constraint failed: global_variables.variable_name_suffix',
                   'identity_unavailable', 'identity could not be checked'),
                  ('ERROR 1045 (28000) at line 1: ProxySQL Admin Error: '
                   'NOT NULL constraint failed: global_variables.variable_name (different diagnostic)',
                   'identity_unavailable', 'identity could not be checked'),
                  ('ERROR 1045 (28000) at line 1: ProxySQL Admin Error: unrelated reason: '
                   'NOT NULL constraint failed: global_variables.variable_name',
                   'identity_unavailable', 'identity could not be checked'),
                  ('ERROR 1064 (28000) at line 1: ProxySQL Admin Error: '
                   'NOT NULL constraint failed: global_variables.variable_name',
                   'identity_unavailable', 'identity could not be checked'),
                  ('--------------\nSELECT \'NOT NULL constraint failed: global_variables.variable_name\';\n'
                   '--------------\nERROR 1045 (28000) at line 1: ProxySQL Admin Error: '
                   'attempt to write a readonly database',
                   'identity_unavailable', 'identity could not be checked'))
        for text, outcome, message in errors:
            with self.subTest(error=text):
                self.conn = self.admin()
                plan = self.plan('server_offline_soft', {'config': [30, 'mf-ic-2', 3306]})

                def fail_guard():
                    raise ConnectorError(text)
                self.conn.on_write = fail_guard
                result = self.send(plan)
                self.assertEqual((result['status'], result['outcome'], result['sent']), ('refused', outcome, []))
                self.assertEqual(result['states'], ['not_sent'] * len(plan['statements']))
                self.assertEqual(result['not_sent'], plan['statements'])
                self.assertIn(message, result['message'])
                record = self.audit()[0]
                self.assertEqual((record['status'], record['outcome']), ('refused', outcome))

    def test_admin_readonly_mode_explains_why_the_matching_guard_cannot_run(self):
        plan = self.plan('server_weight', {'config': [30, 'mf-ic-2', 3306]}, 50)
        variables = self.conn.rows('SELECT * FROM main.global_variables')
        self.conn.on_write = lambda: self.conn.db.execute('PRAGMA query_only = 1')
        try:
            result = self.send(plan)
        finally:
            self.conn.db.execute('PRAGMA query_only = 0')
        self.assertEqual((result['status'], result['outcome'], result['sent']),
                         ('refused', 'identity_unavailable', []))
        self.assertIn('attempt to write a readonly database', result['message'])
        self.assertEqual(result['not_sent'], plan['statements'])
        self.assertEqual(result['states'], ['not_sent'] * len(plan['statements']))
        self.assertEqual(self.conn.rows('SELECT * FROM main.global_variables'), variables)
        self.assertEqual(self.conn.rows("SELECT weight FROM main.mysql_servers WHERE hostname='mf-ic-2'"), [(100,)])
        record = self.audit()[0]
        self.assertEqual((record['status'], record['outcome']), ('refused', 'identity_unavailable'))
        self.assertIn('attempt to write a readonly database', record['message'])

    def test_identity_unavailable_reason_redacts_connection_and_audit_passwords(self):
        for password_source in ('connection', 'audit', 'different_passwords'):
            with self.subTest(password_source=password_source):
                self.conn = self.admin()
                if password_source == 'connection':
                    self.conn.password = self.connection['password']
                elif password_source == 'different_passwords':
                    self.conn.password = 'connection-only-secret'
                plan = self.plan('server_weight', {'config': [30, 'mf-ic-2', 3306]}, 50)

                def fail_guard():
                    raise ConnectorError('ERROR 1045 (28000) at line 1: ProxySQL Admin Error: '
                                         'attempt to write a readonly database; password pw-secret; '
                                         'other connection-only-secret; '
                                         "SOURCE_PASSWORD='replica-secret'")
                self.conn.on_write = fail_guard
                result = self.send(plan)
                self.assertEqual((result['status'], result['outcome']), ('refused', 'identity_unavailable'))
                self.assertIn('attempt to write a readonly database', result['message'])
                for value in (result, self.audit()[0]):
                    text = json.dumps(value)
                    self.assertNotIn('pw-secret', text)
                    if password_source == 'different_passwords':
                        self.assertNotIn('connection-only-secret', text)
                    self.assertNotIn('replica-secret', text)
                    self.assertIn('[redacted]', text)

    def test_identity_refusal_strips_controls_before_message_and_audit(self):
        self.conn.password = 'connection-only-secret'
        plan = self.plan('server_weight', {'config': [30, 'mf-ic-2', 3306]}, 50)
        reason = ('ProxySQL Admin Error: readonly database \x1b]52;c;YQ==\x07 '
                  '\x00\x7f\x9b\u202e password pw-\x1bsecret '
                  'other connection-\x00only-secret SOURCE_PASS\x1bWORD=\'replica-secret\'')

        def fail_guard():
            raise ConnectorError('ERROR 1045 (28000) at line 1: ' + reason)

        self.conn.on_write = fail_guard
        result = self.send(plan)
        self.assertEqual((result['status'], result['outcome'], result['sent']),
                         ('refused', 'identity_unavailable', []))
        record = self.audit()[0]
        for text in (result['message'], record['message']):
            self.assertIn('readonly database', text)
            self.assertIn('[redacted]', text)
            for secret in ('pw-secret', 'connection-only-secret', 'replica-secret'):
                self.assertNotIn(secret, text)
            self.assertFalse([ch for ch in text if unicodedata.category(ch) in ('Cc', 'Cf', 'Zl', 'Zp')])

    def test_identity_refusal_with_only_controls_uses_the_empty_reason(self):
        plan = self.plan('server_weight', {'config': [30, 'mf-ic-2', 3306]}, 50)

        def fail_guard():
            raise ConnectorError('ERROR 1045 (28000) at line 1: \x1b\x07\x9b')

        self.conn.on_write = fail_guard
        result = self.send(plan)
        self.assertEqual((result['status'], result['outcome']), ('refused', 'identity_unavailable'))
        self.assertIn('no server reason was reported', result['message'])
        self.assertEqual(self.audit()[0]['message'], result['message'])

    def test_temp_global_variables_cannot_shadow_the_guard_target(self):
        plan = self.plan('server_weight', {'config': [30, 'mf-ic-2', 3306]}, 50)
        variables = self.conn.rows('SELECT * FROM main.global_variables')

        def shadow_and_switch():
            # A permissive TEMP shadow would let an unqualified INSERT
            # succeed and permit the reviewed changes on the wrong node.
            self.conn.db.execute('CREATE TEMP TABLE global_variables (variable_name, variable_value)')
            self.conn.db.execute("UPDATE disk.global_settings SET variable_value = ? WHERE variable_name = 'uuid'",
                                 (OTHER_ADMIN_UUID,))
        self.conn.on_write = shadow_and_switch
        result = self.send(plan)
        self.assertEqual((result['status'], result['outcome'], result['sent']),
                         ('refused', 'identity_mismatch', []))
        self.assertEqual(result['states'], ['not_sent'] * len(plan['statements']))
        self.assertEqual(self.conn.rows('SELECT * FROM temp.global_variables'), [])
        self.assertEqual(self.conn.rows('SELECT * FROM main.global_variables'), variables)
        self.assertEqual(self.conn.rows("SELECT weight FROM main.mysql_servers WHERE hostname='mf-ic-2'"), [(100,)])
        self.assertEqual(self.audit()[0]['outcome'], 'identity_mismatch')

    def test_guard_constraint_error_after_line_one_is_an_action_failure(self):
        for action_line, status in ((1, 'failed'), (2, 'partial')):
            with self.subTest(action_line=action_line):
                self.conn = self.admin(fail_line=action_line, fail_text='ProxySQL Admin Error: '
                                       'NOT NULL constraint failed: global_variables.variable_name')
                plan = self.plan('server_offline_soft', {'config': [30, 'mf-ic-2', 3306]})
                result = self.send(plan)
                self.assertEqual(result['status'], status)
                self.assertEqual(result['sent'], plan['statements'][:action_line - 1])
                self.assertEqual(result['states'], ['failed', 'not_run'] if action_line == 1 else ['done', 'failed'])
                self.assertEqual(self.audit()[0]['status'], 'error')

    def test_missing_or_unsafe_reviewed_uuid_refuses_before_any_login(self):
        invalid = (None, {}, {'uuid': None}, {'uuid': ''}, {'uuid': 123}, {'uuid': 'not-a-uuid'},
                   {'uuid': ADMIN_UUID + "'; DROP TABLE mysql_servers; --"},
                   {'uuid': ADMIN_UUID + '\n'}, {'uuid': ADMIN_UUID + '\r'},
                   {'uuid': ADMIN_UUID.replace('-', '')}, {'uuid': 'g' + ADMIN_UUID[1:]})
        for identity in invalid:
            with self.subTest(identity=identity):
                plan = {'action': 'servers_load', 'statements': [proxysql.LOAD['servers']],
                        'expect': {'server_identity': identity}}
                audit = dbops.AuditTrail(self.workspace.store, 'proxysql_action', 'ui')
                with mock.patch.object(proxysql, 'read_status', side_effect=AssertionError('unexpected login')) as read:
                    result = proxysql.execute_action(self.conn, plan, audit=audit)
                read.assert_not_called()
                self.assertEqual(self.conn.calls, [])
                self.assertEqual(self.conn.writes, [])
                self.assertEqual((result['status'], result['outcome'], result['sent']),
                                 ('refused', 'identity_unavailable', []))
                self.assertEqual(result['states'], ['not_sent'])
                self.assertIn('UUID could not be read', result['message'])
                record = self.audit()[0]
                self.assertEqual((record['status'], record['outcome']), ('refused', 'identity_unavailable'))

    def test_missing_or_unsafe_uuid_never_creates_an_action_plan(self):
        for value in (None, '', 'not-a-uuid', ADMIN_UUID + "'; DROP TABLE mysql_servers; --", ADMIN_UUID + '\n'):
            with self.subTest(uuid=value):
                conn = self.admin(server_uuid=value)
                with self.assertRaisesRegex(ValueError, 'UUID could not be read'):
                    self.api('/api/live/proxysql/plan', {'connection': self.connection,
                                                       'action': 'server_offline_soft',
                                                       'target': {'config': [30, 'mf-ic-2', 3306]}}, conn=conn)
                self.assertEqual(conn.writes, [])
                self.assertEqual(len(conn.rows('SELECT * FROM main.mysql_servers')), len(CONFIG))

    def test_review_binds_the_visible_identity_guard(self):
        plan = self.plan('rule_disable', {'rule_id': 30})
        result = self.send(dict(plan, session_guard=['SELECT 1;']))
        self.assertEqual((result['status'], result['outcome']), ('refused', 'review'))
        self.assertEqual(self.conn.writes, [])

    def test_lost_reply_during_the_guard_remains_unknown(self):
        plan = self.plan('server_offline_soft', {'config': [30, 'mf-ic-2', 3306]})

        def disconnect():
            raise ConnectorError('mysql client exited 1: ERROR 2013 (HY000) at line 1: Lost connection to server')
        self.conn.on_write = disconnect
        result = self.send(plan)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['states'], ['unknown', 'unknown'])
        self.assertEqual(len(self.conn.writes), 1)
        (record,) = self.audit()
        self.assertEqual(record['status'], 'unknown')

    def test_typed_guard(self):
        plan = self.plan('server_offline_hard', {'config': [30, 'mf-ic-2', 3306]})
        self.assertEqual(self.send(plan, typed='mf-ic-2')['status'], 'refused')
        plan = self.plan('server_offline_hard', {'config': [30, 'mf-ic-2', 3306]})
        self.assertEqual(self.send(plan, typed='mf-ic-2:3306')['status'], 'done')

    def test_a_timeout_is_unknown_and_never_retried(self):
        conn = self.admin(timeout_line=2)
        plan = self.plan('server_offline_soft', {'config': [30, 'mf-ic-2', 3306]})
        result = self.send(plan, conn=conn)
        self.assertEqual(result['status'], 'unknown')
        self.assertIn('unknown', result['message'])
        self.assertEqual(len(conn.writes), 1)
        (record,) = self.audit()
        self.assertEqual((record['status'], record['outcome']), ('unknown', 'timeout'))

    def test_load_failing_after_the_update_is_partial(self):
        conn = self.admin(fail_line=2, fail_text='ProxySQL Admin Error: lock held')
        plan = self.plan('server_offline_soft', {'config': [30, 'mf-ic-2', 3306]})
        result = self.send(plan, conn=conn)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['sent'], plan['statements'][:1])
        self.assertIn('ProxySQL still runs the previous configuration', result['message'])
        (record,) = self.audit()
        self.assertEqual((record['status'], record['outcome']), ('error', 'partial'))
        first = self.admin(fail_line=1, fail_text='ProxySQL Admin Error: constraint failed')
        plan = self.plan('server_offline_soft', {'config': [30, 'mf-ic-2', 3306]})
        result = self.send(plan, conn=first)
        self.assertEqual((result['status'], result['sent']), ('failed', []))

    def test_a_comment_before_load_is_what_admin_refuses(self):
        conn = self.admin()
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
        self.conn.db.execute("INSERT INTO main.mysql_query_rules (%s) VALUES (40," % RULE_NAMES + " 1, NULL, NULL, 0, '^INSERT', NULL, 0, 10, 1, '')")
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
        conn = self.admin(missing=('runtime_mysql_query_rules_fast_routing',))
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


class TestIdentityWrappers(Home):
    def test_admin_wrapper_exposes_only_the_guarded_send_and_password(self):
        conn = mock.Mock(password='connection-secret')
        conn.run_write.return_value = 'answered'
        guard = ['INSERT INTO main.global_variables SELECT NULL, NULL WHERE 0;']
        bound = proxysql._IdentityBoundAdmin(conn, guard)
        self.assertEqual(bound.password, 'connection-secret')
        self.assertEqual(bound.run_write('LOAD MYSQL SERVERS TO RUNTIME;', extra_flags=['--comments'], timeout=60),
                         'answered')
        conn.run_write.assert_called_once_with('\n'.join(guard + ['LOAD MYSQL SERVERS TO RUNTIME;']),
                                               extra_flags=['--comments'], timeout=60)
        for name in ('run', 'run_stdin', 'close', 'load', 'save', 'arbitrary_method', 'host'):
            with self.subTest(name=name), self.assertRaises(AttributeError):
                getattr(bound, name)

    def test_audit_wrapper_forwards_an_explicit_guarded_lifecycle(self):
        audit = mock.Mock(password='audit-secret')
        guard, statements = ['SELECT guard;'], ['LOAD MYSQL SERVERS TO RUNTIME;']
        bound = proxysql._IdentityAudit(audit, guard)
        self.assertEqual(bound.password, 'audit-secret')
        bound.attempt(statements, target={'uuid': ADMIN_UUID})
        audit.attempt.assert_called_once_with(guard + statements, target={'uuid': ADMIN_UUID})
        bound.outcome(statements, 'refused', outcome='changed')
        audit.outcome.assert_called_once_with(statements, 'refused', outcome='changed')
        bound.complete('ok', outcome='applied', after=None)
        audit.complete.assert_called_once_with('ok', outcome='applied', after=None)
        bound.complete_unsent('refused', statements, outcome='changed', message='Consent revoked.')
        audit.complete_unsent.assert_called_once_with('refused', statements, outcome='changed',
                                                      message='Consent revoked.')
        error = ConnectorError('interrupted')
        bound.fail(error, statements)
        audit.fail.assert_called_once_with(error, guard + statements)
        for name in ('run', 'run_write', 'run_stdin', 'store', 'arbitrary_method', 'purge'):
            with self.subTest(name=name), self.assertRaises(AttributeError):
                getattr(bound, name)

    def test_guard_refusals_explicitly_record_that_the_guard_was_sent(self):
        for message, outcome in ((GUARD_MISMATCH_ERROR, 'identity_mismatch'),
                                 ('ERROR 1045 (28000) at line 1: ProxySQL Admin Error: '
                                  'no such table: disk.global_settings', 'identity_unavailable')):
            with self.subTest(outcome=outcome):
                conn = mock.Mock(password=None)
                conn.run_write.side_effect = ConnectorError(message)
                guard = ['INSERT INTO main.global_variables SELECT NULL, NULL WHERE 0;']
                bound = proxysql._IdentityBoundAdmin(conn, guard)
                with self.assertRaises(reviewed_writes.Refusal) as caught:
                    bound.run_write('LOAD MYSQL SERVERS TO RUNTIME;')
                self.assertEqual(caught.exception.outcome, outcome)
                self.assertIs(getattr(caught.exception, 'guard_sent', False), True)
                conn.run_write.assert_called_once_with('\n'.join(guard + ['LOAD MYSQL SERVERS TO RUNTIME;']),
                                                       extra_flags=None, timeout=None)

    def test_unsent_audit_delegates_state_and_never_restores_the_guard(self):
        from myflames.workspace_store import WorkspaceStore
        store = WorkspaceStore()
        trail = dbops.AuditTrail(store, 'proxysql_action', 'ui')
        guard, statements = ['SELECT guard;'], ['LOAD MYSQL SERVERS TO RUNTIME;']
        bound = proxysql._IdentityAudit(trail, guard)
        self.assertEqual((bound.pending, bound.finished, bound.id), (False, False, None))
        audit_id = bound.attempt(statements)
        self.assertEqual((bound.pending, bound.finished, bound.id), (True, False, audit_id))
        self.assertEqual(dbops.audit_report(store)['records'][0]['statements'], guard + statements)
        bound.complete_unsent('refused', statements, outcome='changed', message='Consent revoked.')
        self.assertEqual((bound.pending, bound.finished, bound.id), (False, True, audit_id))
        (record,) = dbops.audit_report(store)['records']
        self.assertEqual((record['status'], record['outcome']), ('refused', 'changed'))
        self.assertEqual(record['statements'], statements)
        bound.complete_unsent('refused', ['SAVE MYSQL SERVERS TO DISK;'], outcome='changed')
        self.assertEqual(dbops.audit_report(store)['records'], [record])

    def test_unsent_audit_supports_an_underlying_complete_only_stub(self):
        class Audit(object):
            def __init__(self):
                self.completed = []

            def complete(self, status, **kwargs):
                self.completed.append((status, kwargs))

        trail = Audit()
        bound = proxysql._IdentityAudit(trail, ['SELECT guard;'])
        bound.complete_unsent('refused', ['LOAD MYSQL SERVERS TO RUNTIME;'], outcome='changed')
        self.assertEqual(trail.completed, [('refused', {'outcome': 'changed'})])


class TestReviewedWrites(unittest.TestCase):
    def test_refused_text_customizes_the_result_and_audit(self):
        plan = {'action': 'x', 'statements': ['UPDATE t SET a = 1;'], 'expect': {}}
        error = 'mysql client exited 1: ERROR 3910 at line 1: replica channel running'
        for custom in (False, True):
            with self.subTest(custom=custom):
                conn = mock.Mock(password=None)
                conn.run_write.side_effect = ConnectorError(error)
                audit = mock.Mock(password=None)
                callback = mock.Mock(return_value='No change is visible here; check the server.')
                out = reviewed_writes.execute(conn, plan, lambda: plan, 'x-1.0', audit=audit,
                                              verify_on_error=True, read_after=lambda: {},
                                              effect=lambda fresh, after: None,
                                              refused_text=callback if custom else None)
                expected = (callback.return_value if custom else
                            'The server refused UPDATE t SET a = 1;: replica channel running. Nothing changed.')
                self.assertEqual((out['status'], out['message']), ('failed', expected))
                self.assertEqual(audit.complete.call_args[1]['message'], out['message'])
                self.assertEqual(audit.complete.call_args[1]['outcome'], 'refused_by_server')
                if custom:
                    callback.assert_called_once_with(plan['statements'][0], 'replica channel running')
                else:
                    callback.assert_not_called()

    def test_failed_line_and_server_message(self):
        text = 'mysql client exited 1: ERROR 1045 (28000) at line 2: ProxySQL Admin Error: near "LOAD": syntax error'
        self.assertEqual(reviewed_writes.failed_line(text), 2)
        self.assertEqual(reviewed_writes.server_message(text), 'ProxySQL Admin Error: near "LOAD": syntax error')
        self.assertIsNone(reviewed_writes.failed_line('Query timed out after 60s; the outcome is unknown.'))

    def test_check_reviewed_rejects_bad_shapes(self):
        for bad in (None, [], {'action': 'x'}, {'action': 'x', 'statements': []},
                    {'action': 'x', 'statements': ['a\nb']}, {'action': 'x', 'statements': ['a\rb']},
                    {'action': 'x', 'statements': ['UPDATE t SET x=1; DELETE FROM t']},
                    {'action': 1, 'statements': ['a']}):
            with self.assertRaises(ValueError):
                reviewed_writes.check_reviewed(bad)

    def test_admin_apply_rejects_multiline_even_with_a_spoofed_sql_engine_or_shell_sender(self):
        for extra in ({'expect': {'engine': 'mysql'}}, {'via': 'mysqlsh'}):
            plan = dict(action='x', statements=['UPDATE mysql_servers\nSET weight = 1'], **extra)
            with self.subTest(extra=extra), self.assertRaises(ValueError), \
                    mock.patch.object(proxysql, 'read_status', side_effect=AssertionError('unexpected login')):
                proxysql.execute_action(None, plan)

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
