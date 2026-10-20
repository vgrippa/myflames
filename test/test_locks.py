"""Lock report: ``myflames locks``, the Locks tab API, and the deadlock parser.

Fake connections answer each statement by a distinctive substring with canned
``mysql --batch`` text, so no server is needed. The row and metadata lock
scenario mirrors a live MySQL 8.4.11 capture: connection 23 sits idle in a
transaction and blocks 24 (which blocks 27) and 25's insert; connection 26 is
idle with a read-only transaction whose SHARED_READ metadata lock keeps 28's
ALTER TABLE waiting, and 29's SELECT queues behind that ALTER. The SHOW ENGINE
INNODB STATUS samples in fixtures/locks/ come from MySQL 8.4.11 / 26.7.0 and
MariaDB 11.4.13 / 13.0.2.
"""
import contextlib
import inspect
import io
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

from myflames import locks, ui, workspace_cli
from myflames.connector import ConnectorError

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'locks')
PASSWORD = 'hunter2-not-real'
VERSION_SQL = 'SELECT @@version, @@performance_schema'
# Literal tags (not the module constants) so a changed or empty tag fails here;
# ``top`` excludes statements that start with '/*myflames:'.
LOCKS_TAG = '/*myflames:locks*/ '
KILL_TAG = '/*myflames:kill*/ '
DISCOVER_TAG = '/*myflames:discover*/ '
# A lock report only reads. Word boundaries keep 'setup_instruments' and
# 'wait/lock/...' from matching.
WRITE_WORDS = re.compile(r'\b(UPDATE|SET|INSERT|DELETE|REPLACE|KILL|GRANT|FLUSH|ALTER|CREATE|DROP|'
                         r'TRUNCATE)\b', re.I)


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding='utf-8') as handle:
        return handle.read()


def process_denied():
    return ConnectorError('mysql client exited 1: ERROR 1227 (42000) at line 1: Access denied; you need '
                          '(at least one of) the PROCESS privilege(s) for this operation')


def batch(rows):
    """Render rows like ``mysql --batch --skip-column-names`` (NULL, \\n, \\t escaped)."""
    def cell(value):
        if value is None:
            return 'NULL'
        return str(value).replace('\\', '\\\\').replace('\n', '\\n').replace('\t', '\\t')
    return ''.join('\t'.join(cell(v) for v in row) + '\n' for row in rows)


class FakeConn(object):
    """Answers SQL by substring; records every statement and client flags."""

    def __init__(self, version, responses=(), **kwargs):
        self.version = version
        self.responses = list(responses)
        self.calls = []
        self.kwargs = kwargs
        self.host, self.port, self.user = "127.0.0.1", 3306, None
        self.ssl_mode = self.ssl_ca = None

    def run(self, sql, extra_flags=None, timeout=None):
        self.calls.append((sql, list(extra_flags or [])))
        for pattern, result in self.responses:
            if pattern in sql:
                if isinstance(result, Exception):
                    raise result
                return result(sql) if callable(result) else result
        raise AssertionError('unexpected SQL: ' + sql)

    def server_version(self):
        return self.version

    def is_mariadb(self):
        return 'mariadb' in self.version.lower()

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def sql(self, needle):
        return [sql for sql, _ in self.calls if needle in sql]


class FakeFactory(object):
    """Stands in for MySQLConnection: records options, hands back one FakeConn."""

    def __init__(self, conn):
        self.conn = conn
        self.options = None

    def __call__(self, **options):
        self.options = options
        for key in ("host", "port", "user", "ssl_mode", "ssl_ca"):
            setattr(self.conn, key, options.get(key))
        return self.conn


# Answer key -> substring of the statement. Order matters for plain substrings:
# data_lock_waits and INNODB_LOCK_WAITS also join the lock tables, the
# metadata_locks name contains 'data_locks', the setup query names
# events_statements_current, and the metadata and last-statement queries join
# performance_schema.threads. The 'FROM ' prefixes keep each one unambiguous.
PATTERNS = (
    # Kill-time reads come first: the session read appends CONNECTION_ID() and
    # the transaction read filters on one thread, so neither matches below.
    ('target', ', CONNECTION_ID() FROM '),
    ('target_trx', 'WHERE trx_mysql_thread_id = '),
    ('kill', KILL_TAG),
    ('target_last', 'ORDER BY EVENT_ID DESC LIMIT 1'),
    ('ddl', "LIKE 'Waiting for %metadata lock'"),
    ('who', 'SELECT USER(), CURRENT_USER()'),
    ('grants', 'SHOW GRANTS'),
    ('mli', 'FROM information_schema.METADATA_LOCK_INFO'),
    ('trx', 'FROM information_schema.INNODB_TRX'),
    ('waits', 'FROM performance_schema.data_lock_waits'),
    ('setup', 'FROM performance_schema.setup_instruments'),
    ('mdl', 'FROM performance_schema.metadata_locks'),
    ('last', 'FROM performance_schema.events_statements_current'),
    ('threads', 'FROM performance_schema.threads'),
    ('processlist', 'FROM information_schema.PROCESSLIST'),
    ('locks', 'FROM performance_schema.data_locks LIMIT'),
    ('ma_waits', 'FROM information_schema.INNODB_LOCK_WAITS'),
    ('ma_locks', 'FROM information_schema.INNODB_LOCKS LIMIT'),
    ('status', 'SHOW ENGINE INNODB STATUS'),
)
FULL_LOCK_LIST = 'FROM performance_schema.data_locks LIMIT'
# A privileged account by default (no privilege notes); tests vary it.
WHO_APP = [['app@localhost', 'app@%']]
MYSQL_GRANTS = [['GRANT SELECT, PROCESS ON *.* TO `app`@`%`'],
                ['GRANT CONNECTION_ADMIN,SYSTEM_USER ON *.* TO `app`@`%`']]
MARIADB_GRANTS = [['GRANT PROCESS, CONNECTION ADMIN ON *.* TO `app`@`%`']]
NO_MLI = ConnectorError("mysql client exited 1: ERROR 1109 (42S02) at line 1: Unknown table "
                        "'METADATA_LOCK_INFO' in information_schema")


def fake(version, ps_on=True, **answers):
    """A FakeConn for *version*; each answer is rows, batch text, a callable, or an exception."""
    mariadb = 'mariadb' in version.lower()
    for key, value in (('ddl', []), ('who', WHO_APP), ('grants', MARIADB_GRANTS if mariadb else MYSQL_GRANTS),
                       ('mli', NO_MLI), ('target_last', [])):
        answers.setdefault(key, value)
    responses = [(VERSION_SQL, batch([[version, 1 if ps_on else 0]])),
                 ('SELECT @@server_uuid', batch([['server-uuid-1']])),
                 ('SELECT @@server_id, @@hostname, @@version', batch([['1', 'server-one', '11.4.13-MariaDB']]))]
    for key, pattern in PATTERNS:
        if key in answers:
            value = answers.pop(key)
            responses.append((pattern, batch(value) if isinstance(value, list) else value))
    if answers:
        raise TypeError('unknown answer keys: %s' % sorted(answers))
    return FakeConn(version, responses)


def status_row(text):
    """SHOW ENGINE INNODB STATUS returns one row: Type, Name, Status."""
    return [['InnoDB', '', text]]


def ids_in(sql, column):
    match = re.search(r'\b%s IN \(([^)]*)\)' % column, sql)
    return [int(x) for x in match.group(1).split(',')] if match else []


def lookup_sql(conn, table):
    """The session lookups by id on *table*, not the metadata lock waiter query."""
    return [sql for sql in conn.sql('FROM ' + table) if 'Waiting for' not in sql]


def threads_answer(rows):
    """performance_schema.threads, filtered by the requested PROCESSLIST_ID / THREAD_ID lists."""
    def answer(sql):
        conns, threads = ids_in(sql, 'PROCESSLIST_ID'), ids_in(sql, 'THREAD_ID')
        return batch([r for r in rows if r[1] in conns or r[0] in threads])
    return answer


def processlist_answer(rows):
    def answer(sql):
        wanted = ids_in(sql, 'ID')
        return batch([r for r in rows if r[1] in wanted])
    return answer


def last_answer(rows):
    def answer(sql):
        wanted = ids_in(sql, 'PROCESSLIST_ID')
        return batch([r for r in rows if r[0] in wanted])
    return answer


def _without_deadlock(text):
    start = text.index('------------------------\nLATEST DETECTED DEADLOCK')
    end = text.index('------------\nTRANSACTIONS\n')
    return text[:start] + text[end:]


NO_DEADLOCK = _without_deadlock(fixture('mysql-8.4.11-innodb-status.txt'))
SETUP_ON = [['instrument', 'YES'], ['global_instrumentation', 'YES'], ['events_statements_current', 'YES']]


# ---- MySQL 8.4 scenario (mirrors the live 8.4.11 capture) -------------------

def dl(lock_id, trx, thread, index, lock_type, mode, status, data, table='t'):
    """A performance_schema.data_locks row: ENGINE_LOCK_ID ... LENGTH(LOCK_DATA)."""
    return [lock_id, trx, thread, 'lk', table, None, index, lock_type, mode, status, data,
            None if data is None else len(data)]


def dlw(req, blk):
    """A data_lock_waits row joined with the requesting and blocking data_locks rows."""
    return [req[1], req[2], req[0], blk[1], blk[2], blk[0]] + req[3:12] + blk[3:12]


T_23 = dl('1913:1063', '1913', 63, None, 'TABLE', 'IX', 'GRANTED', None)
PK1_23 = dl('1913:6:4:2', '1913', 63, 'PRIMARY', 'RECORD', 'X,REC_NOT_GAP', 'GRANTED', '1')
# An implicit lock InnoDB converted while connection 24's thread scanned: the
# lock belongs to trx 1913 (connection 23) but carries thread 64 (connection 24).
PK3_23 = dl('1913:6:4:4', '1913', 64, 'PRIMARY', 'RECORD', 'X,REC_NOT_GAP', 'GRANTED', '3')
K60_23 = dl('1913:6:5:3', '1913', 63, 'k', 'RECORD', 'X', 'GRANTED', '60, 6')
K70_23 = dl('1913:6:5:4', '1913', 63, 'k', 'RECORD', 'X', 'GRANTED', '70, 7')
T_24 = dl('1914:1063', '1914', 64, None, 'TABLE', 'IX', 'GRANTED', None)
PK2_24 = dl('1914:6:4:3', '1914', 64, 'PRIMARY', 'RECORD', 'X,REC_NOT_GAP', 'GRANTED', '2')
PK1_24 = dl('1914:6:4:2', '1914', 64, 'PRIMARY', 'RECORD', 'X,REC_NOT_GAP', 'WAITING', '1')
T_25 = dl('1915:1063', '1915', 65, None, 'TABLE', 'IX', 'GRANTED', None)
K60_25 = dl('1915:6:5:3', '1915', 65, 'k', 'RECORD', 'X,GAP,INSERT_INTENTION', 'WAITING', '60, 6')
T_27 = dl('1916:1063', '1916', 67, None, 'TABLE', 'IX', 'GRANTED', None)
PK2_27 = dl('1916:6:4:3', '1916', 67, 'PRIMARY', 'RECORD', 'X,REC_NOT_GAP', 'WAITING', '2')
MYSQL_DATA_LOCKS = [T_23, PK1_23, PK3_23, K60_23, K70_23, T_24, PK2_24, PK1_24, T_25, K60_25, T_27, PK2_27]
MYSQL_WAITS = [dlw(PK1_24, PK1_23), dlw(PK2_27, PK2_24), dlw(K60_25, K60_23)]

# INNODB_TRX: trx_id, state, started, age, requested lock, wait started, wait s,
# thread, query, operation state, in use, locked, lock structs, rows locked,
# rows modified, isolation level, read only.
MYSQL_TRX = [
    ['1913', 'RUNNING', '2026-10-12 21:25:00', 300, None, None, None, 23, None, None,
     0, 1, 4, 4, 1, 'REPEATABLE READ', 0],
    ['562947965813544', 'RUNNING', '2026-10-12 21:26:40', 200, None, None, None, 26, None, None,
     0, 0, 0, 0, 0, 'REPEATABLE READ', 1],
    ['1914', 'LOCK WAIT', '2026-10-12 21:28:00', 120, '1914:6:4:2', '2026-10-12 21:29:00', 60, 24,
     'UPDATE t SET v=1 WHERE id=1', 'starting index read', 1, 1, 3, 2, 1, 'REPEATABLE READ', 0],
    ['1915', 'LOCK WAIT', '2026-10-12 21:29:10', 50, '1915:6:5:3', '2026-10-12 21:29:20', 40, 25,
     'INSERT INTO t VALUES (11,0,55)', 'inserting', 1, 1, 2, 1, 0, 'REPEATABLE READ', 0],
    ['1916', 'LOCK WAIT', '2026-10-12 21:29:30', 30, '1916:6:4:3', '2026-10-12 21:29:40', 20, 27,
     'UPDATE t SET v=2 WHERE id=2', 'starting index read', 1, 1, 2, 1, 0, 'READ COMMITTED', 0],
]


def mdl(object_type, schema, name, lock_type, status, thread, conn, duration='TRANSACTION'):
    """A metadata_locks row joined with threads.PROCESSLIST_ID."""
    return [object_type, schema, name, lock_type, duration, status, 'sql_base.cc:3300', thread, conn]


# The server orders PENDING first (ORDER BY m.LOCK_STATUS = 'PENDING' DESC).
MYSQL_MDL = [
    mdl('TABLE', 'lk', 'm', 'EXCLUSIVE', 'PENDING', 68, 28),
    mdl('TABLE', 'lk', 'm', 'SHARED_READ', 'PENDING', 69, 29),
    mdl('TABLE', 'lk', 'm', 'SHARED_READ', 'GRANTED', 66, 26),
    mdl('TABLE', 'lk', 'm', 'SHARED_UPGRADABLE', 'GRANTED', 68, 28),
    mdl('SCHEMA', 'lk', None, 'INTENTION_EXCLUSIVE', 'GRANTED', 68, 28),
    mdl('GLOBAL', None, None, 'INTENTION_EXCLUSIVE', 'GRANTED', 68, 28, 'STATEMENT'),
    mdl('TABLE', 'lk', 't', 'SHARED_WRITE', 'GRANTED', 63, 23),
    mdl('TABLE', 'lk', 't', 'SHARED_WRITE', 'GRANTED', 64, 24),
]
# threads: THREAD_ID, PROCESSLIST_ID, USER, HOST, DB, COMMAND, TIME, STATE, INFO
MYSQL_THREADS = [
    [63, 23, 'app', 'localhost', 'lk', 'Sleep', 300, None, None],
    [64, 24, 'app', 'localhost', 'lk', 'Query', 60, 'updating', 'UPDATE t SET v=1 WHERE id=1'],
    [65, 25, 'app', 'localhost', 'lk', 'Query', 40, 'update', 'INSERT INTO t VALUES (11,0,55)'],
    [66, 26, 'report', '10.0.0.7', 'lk', 'Sleep', 200, None, None],
    [67, 27, 'app', 'localhost', 'lk', 'Query', 20, 'updating', 'UPDATE t SET v=2 WHERE id=2'],
    [68, 28, 'dba', 'localhost', 'lk', 'Query', 15, 'Waiting for table metadata lock',
     'ALTER TABLE m ADD COLUMN c INT'],
    [69, 29, 'app', 'localhost', 'lk', 'Query', 10, 'Waiting for table metadata lock', 'SELECT * FROM m'],
]
MYSQL_PROCESSLIST = [[None] + row[1:] for row in MYSQL_THREADS]
# events_statements_current: PROCESSLIST_ID, SQL_TEXT, EVENT_ID (ascending).
MYSQL_LAST = [[26, 'SELECT COUNT(*) FROM m', 40], [23, 'SELECT * FROM t WHERE k >= 60 FOR UPDATE', 95]]


def mysql_conn(version='8.4.6', ps_on=True, **overrides):
    """The MySQL scenario; override any section with rows, a callable, or an exception."""
    answers = {'trx': MYSQL_TRX, 'waits': MYSQL_WAITS, 'locks': MYSQL_DATA_LOCKS, 'setup': SETUP_ON,
               'mdl': MYSQL_MDL, 'threads': threads_answer(MYSQL_THREADS),
               'last': last_answer(MYSQL_LAST),
               'status': status_row(fixture('mysql-8.4.11-innodb-status.txt'))}
    answers.update(overrides)
    return fake(version, ps_on, **answers)


def empty_mysql(version='8.4.6', ps_on=True, **overrides):
    answers = {'trx': [], 'waits': [], 'locks': [], 'setup': SETUP_ON, 'mdl': [], 'threads': [],
               'last': [], 'processlist': [], 'status': status_row(NO_DEADLOCK)}
    answers.update(overrides)
    return fake(version, ps_on, **answers)


# ---- MariaDB scenario ---------------------------------------------------------

def ma_lock(lock_id, trx, index, lock_type, mode, data):
    """An INNODB_LOCKS row: lock_id, lock_trx_id, NULL, lock_table ... LENGTH(lock_data)."""
    return [lock_id, trx, None, '`lk`.`t`', index, lock_type, mode, data, None if data is None else len(data)]


def ma_wait(req, blk):
    """An INNODB_LOCK_WAITS row joined with the requesting and blocking INNODB_LOCKS rows."""
    return [req[1], req[0], blk[1], blk[0]] + req[3:9] + blk[3:9]


MA_PK1_40 = ma_lock('40:9:3:2', '40', 'PRIMARY', 'RECORD', 'X', '1')
MA_PK1_41 = ma_lock('41:9:3:2', '41', 'PRIMARY', 'RECORD', 'X', '1')
MA_K60_40 = ma_lock('40:9:4:3', '40', 'k', 'RECORD', 'X', '60, 6')
MA_K60_42 = ma_lock('42:9:4:3', '42', 'k', 'RECORD', 'X,GAP', '60, 6')
# lock_trx_id is lock->trx->id, which is 0 for a transaction that has not written.
MA_PK5_0 = ma_lock('0:9:3:6', '0', 'PRIMARY', 'RECORD', 'S', '5')
MA_PK5_43 = ma_lock('43:9:3:6', '43', 'PRIMARY', 'RECORD', 'X', '5')
MA_LOCKS = [MA_PK1_40, MA_PK1_41, MA_K60_40, MA_K60_42, MA_PK5_0, MA_PK5_43]
MA_WAITS = [ma_wait(MA_PK1_41, MA_PK1_40), ma_wait(MA_K60_42, MA_K60_40), ma_wait(MA_PK5_43, MA_PK5_0)]
MA_TRX = [
    ['40', 'RUNNING', '2026-10-12 21:25:00', 300, None, None, None, 5, None, None,
     0, 1, 3, 2, 1, 'REPEATABLE READ', 0],
    # Two sessions whose transactions have not written: both report trx_id 0.
    ['0', 'RUNNING', '2026-10-12 21:25:50', 250, None, None, None, 8, None, None,
     0, 1, 2, 1, 0, 'REPEATABLE READ', 0],
    ['0', 'RUNNING', '2026-10-12 21:26:00', 240, None, None, None, 9, None, None,
     0, 0, 0, 0, 0, 'REPEATABLE READ', 1],
    ['41', 'LOCK WAIT', '2026-10-12 21:29:00', 60, '41:9:3:2', '2026-10-12 21:29:00', 60, 6,
     'UPDATE t SET v=1 WHERE id=1', 'starting index read', 1, 1, 2, 1, 0, 'REPEATABLE READ', 0],
    ['42', 'LOCK WAIT', '2026-10-12 21:29:20', 40, '42:9:4:3', '2026-10-12 21:29:20', 40, 7,
     'INSERT INTO t VALUES (11,0,55)', 'inserting', 1, 1, 2, 1, 0, 'REPEATABLE READ', 0],
    ['43', 'LOCK WAIT', '2026-10-12 21:29:30', 30, '43:9:3:6', '2026-10-12 21:29:30', 30, 10,
     'UPDATE t SET v=3 WHERE id=5', 'starting index read', 1, 1, 2, 1, 0, 'REPEATABLE READ', 0],
]
MA_SESSIONS = [  # connection, command, seconds, state, info
    (5, 'Sleep', 300, '', None), (6, 'Query', 60, 'Updating', 'UPDATE t SET v=1 WHERE id=1'),
    (7, 'Query', 40, 'Update', 'INSERT INTO t VALUES (11,0,55)'), (8, 'Sleep', 250, '', None),
    (9, 'Sleep', 240, '', None), (10, 'Query', 30, 'Updating', 'UPDATE t SET v=3 WHERE id=5'),
]
# information_schema.PROCESSLIST with TIME_MS / 1000 (P_S off, the MariaDB default).
MA_PROCESSLIST = [[None, c, 'app', 'localhost', 'lk', cmd, '%.4f' % s, state, info]
                  for c, cmd, s, state, info in MA_SESSIONS]
MA_THREADS = [[100 + c, c, 'app', 'localhost', 'lk', cmd, s, state, info]
              for c, cmd, s, state, info in MA_SESSIONS]
MARIADB_VERSIONS = ('10.11.9-MariaDB', '11.4.13-MariaDB-ubu2404', '11.8.3-MariaDB')


def ma_conn(version='11.4.13-MariaDB-ubu2404', ps_on=False, **overrides):
    answers = {'trx': MA_TRX, 'ma_waits': MA_WAITS, 'ma_locks': MA_LOCKS,
               'processlist': processlist_answer(MA_PROCESSLIST),
               'status': status_row(fixture('mariadb-11.4.13-innodb-status.txt'))}
    if ps_on:
        answers.update({'setup': SETUP_ON, 'mdl': [], 'threads': threads_answer(MA_THREADS), 'last': []})
    answers.update(overrides)
    return fake(version, ps_on, **answers)


def empty_mariadb(version='11.4.13-MariaDB-ubu2404', ps_on=True, **overrides):
    answers = {'trx': [], 'ma_waits': [], 'ma_locks': [], 'setup': SETUP_ON, 'mdl': [], 'threads': [],
               'last': [], 'processlist': [], 'status': status_row(NO_DEADLOCK)}
    answers.update(overrides)
    return fake(version, ps_on, **answers)


def by_lock_id(report):
    return {lock['lock_id']: lock for lock in report['row_locks']}


def by_trx_id(report):
    return {t['trx_id']: t for t in report['transactions']}


def mdl_pairs(report):
    """{(waiting connection, short mode): ([(reason, blocker connection, short)], certain)}"""
    return {(w['waiting']['connection_id'], w['waiting']['short']):
            ([(b['reason'], b['lock']['connection_id'], b['lock']['short']) for b in w['blocked_by']],
             w['certain'])
            for w in report['metadata_locks']['waits']}


def has_note(report, text):
    return any(text in note for note in report['notes'])


@contextlib.contextmanager
def private_home():
    """A temporary HOME (and the vault secret backend): a KILL writes an audit
    record to ~/.myflames/workspace.sqlite3, which must never be the real one."""
    with tempfile.TemporaryDirectory() as home, \
            mock.patch.dict(os.environ, {'HOME': home, 'USERPROFILE': home, 'MYFLAMES_SECRET_BACKEND': 'vault'}):
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        yield home


class LockCase(unittest.TestCase):
    maxDiff = None

    def run(self, result=None):
        # Every lock test, whatever its own setUp does, runs with a private HOME.
        with private_home() as home:
            self.home = home
            return super(LockCase, self).run(result)

    def assert_read_only(self, conn):
        """Every statement is a read, carries a myflames tag, and keeps it with --comments."""
        self.assertTrue(conn.calls, 'no SQL was sent')
        self.assertEqual(conn.calls[0][0], DISCOVER_TAG + VERSION_SQL)
        for sql, flags in conn.calls:
            self.assertIn('--comments', flags, sql)
            self.assert_read_statement(sql)
        for sql, _ in conn.calls[1:]:
            self.assertTrue(sql.startswith(LOCKS_TAG), sql)

    def assert_read_statement(self, sql):
        body = sql[len(LOCKS_TAG):] if sql.startswith(LOCKS_TAG) else sql
        zone = "SET SESSION time_zone = 'SYSTEM'; "
        if body.startswith(zone):
            # Only the module-owned InnoDB transaction-age reads may set a session zone.
            body = body[len(zone):]
            self.assertTrue(body.startswith(LOCKS_TAG))
            body = body[len(LOCKS_TAG):]
            self.assertTrue(body.startswith('SELECT trx_id, trx_state, trx_started, TIMESTAMPDIFF('))
            self.assertIn('FROM information_schema.INNODB_TRX', body)
            self.assertNotIn(';', body)
        self.assertNotRegex(body, WRITE_WORDS)

    def report(self, conn, **kwargs):
        result = locks.lock_report(conn, **kwargs)
        self.assert_read_only(conn)
        json.dumps(result, allow_nan=False)
        return result


# ---- 1. server versions and the data_locks latch ---------------------------------

class TestTransactionTimeZone(unittest.TestCase):
    def test_transaction_ages_use_system_time_zone(self):
        for engine, version in (('mysql', '8.4.11'), ('mariadb', '11.4.13-MariaDB')):
            with self.subTest(engine=engine):
                def answer(sql):
                    # Emulate a session zone one hour ahead of SYSTEM.
                    system = sql.startswith(LOCKS_TAG + "SET SESSION time_zone = 'SYSTEM'; ")
                    age, wait = (60, 10) if system else (3660, 3610)
                    return batch([['7', 'LOCK WAIT', '2026-10-19 10:00:00', age, 'lock-1',
                                   '2026-10-19 10:00:50', wait, 420, 'SELECT 1', None,
                                   1, 1, 1, 1, 0, 'REPEATABLE READ', '0']])
                conn = FakeConn(version, [('FROM information_schema.INNODB_TRX', answer)])
                result = locks._Report(engine, version, (11, 4, 13) if engine == 'mariadb' else (8, 4, 11),
                                       True, 100, 'all')
                locks._read_transactions(conn, result)
                self.assertEqual((result.trx[0]['age_s'], result.trx[0]['wait_s']), (60, 10))
                self.assertEqual(len(conn.calls), 1)
                self.assertIn("; " + LOCKS_TAG + 'SELECT ', conn.calls[0][0])

    def test_in_session_review_converts_the_clock_without_changing_time_zone(self):
        # ALTER review reuses the editor session, whose time_zone must stay intact.
        for engine in ('mysql', 'mariadb'):
            with self.subTest(engine=engine):
                seen = []
                class Reader(object):
                    def query_lock_rows(self, sql):
                        seen.append(sql)
                        converted = "CONVERT_TZ(UTC_TIMESTAMP(), '+00:00', 'SYSTEM')" in sql
                        return [['7', 'RUNNING', '2026-10-19 10:00:00', '60' if converted else '3660',
                                 None, None, None, '420']]
                result = locks._Report(engine, '', (11, 4, 13) if engine == 'mariadb' else (8, 4, 11),
                                       True, 100, 'all')
                locks._read_transactions(Reader(), result)
                self.assertEqual(result.trx[0]['age_s'], 60)
                self.assertTrue(seen[0].startswith(LOCKS_TAG + 'SELECT '))
                self.assertNotIn('SET SESSION', seen[0])
                self.assertNotIn('NOW()', seen[0])

    def test_target_transaction_age_uses_system_time_zone(self):
        for engine in ('mysql', 'mariadb'):
            with self.subTest(engine=engine):
                def answer(sql):
                    system = sql.startswith(LOCKS_TAG + "SET SESSION time_zone = 'SYSTEM'; ")
                    return batch([['7', 'RUNNING', '2026-10-19 10:00:00', 60 if system else 3660, 1, 0]])
                conn = FakeConn('', [('FROM information_schema.INNODB_TRX', answer),
                                     ('FROM information_schema.PROCESSLIST',
                                      batch([[420, 'app', 'localhost', 'shop', 'Query', 60, 'executing',
                                              'SELECT 1', None, 999]]))])
                result, own = locks.read_target(conn, engine, False, 420)
                self.assertEqual((result['trx']['age_s'], own), (60, 999))
                self.assertTrue(result['trx_checked'])
                self.assertIn("; " + LOCKS_TAG + 'SELECT ', conn.calls[-1][0])


class TestVersions(LockCase):
    def test_report_captures_server_identity_once_for_both_engines(self):
        for conn, expected in (
                (empty_mysql(), {'server_uuid': 'server-uuid-1'}),
                (empty_mariadb(), {'server_id': '1', 'hostname': 'server-one', 'version': '11.4.13-MariaDB'})):
            with self.subTest(expected=expected):
                self.assertEqual(self.report(conn, identity=True)['server_identity'], expected)
                self.assertEqual(sum('SELECT @@server_' in sql for sql, _ in conn.calls), 1)

    def test_identity_is_read_only_when_asked(self):
        # The CLI report and the Full analysis report do not need it; only the
        # workspace's Kill sheet does (ui.py passes identity=True).
        conn = empty_mysql()
        self.assertEqual(self.report(conn)['server_identity'], {})
        self.assertFalse(any('@@server_' in sql for sql, _ in conn.calls))

    def test_identity_unreadable_or_incomplete_does_not_hide_locks(self):
        for answer in ('', batch([[None]]), batch([['one', 'extra']]), process_denied()):
            with self.subTest(answer=answer):
                conn = empty_mysql()
                conn.responses[1] = ('SELECT @@server_uuid', answer)
                report = self.report(conn, identity=True)
                self.assertEqual(report['server_identity'], {})
                self.assertIn('blocking_tree', report)

    def test_data_locks_latch_caution_boundaries(self):
        cases = {(8, 0, 20): True, (8, 0, 35): True, (8, 0, 39): True, (8, 0, 40): False,
                 (8, 0, 44): False, (8, 1, 0): True, (8, 3, 0): True, (8, 4, 2): True,
                 (8, 4, 3): False, (8, 4, 6): False, (9, 0, 0): True, (9, 0, 1): True,
                 (9, 1, 0): False, (9, 7, 0): False, (26, 7, 0): False}
        for ver, expected in cases.items():
            with self.subTest(ver=ver):
                self.assertIs(locks.data_locks_latch_caution('mysql', ver), expected)
        for ver in ((8, 0, 35), (9, 0, 1), (10, 11, 9), (11, 4, 13), (11, 8, 3)):
            with self.subTest(mariadb=ver):
                self.assertIs(locks.data_locks_latch_caution('mariadb', ver), False)

    def test_latch_note_follows_the_server_version(self):
        for version, latch in (('8.0.35', True), ('8.4.6', False), ('9.0.1', True), ('9.1.0', False),
                               ('26.7.0', False)):
            with self.subTest(version=version):
                r = self.report(empty_mysql(version))
                self.assertEqual((r['engine'], r['server_version']), ('mysql', version))
                self.assertIs(r['capabilities']['data_locks_global_latch'], latch)
                notes = [n for n in r['notes'] if 'global lock-system latch' in n]
                if latch:
                    self.assertEqual(len(notes), 1)
                    self.assertTrue(r['notes'][0].startswith(
                        'Reading performance_schema.data_locks and data_lock_waits on MySQL %s holds'
                        % version), r['notes'][0])
                    self.assertIn('--waits-only skips the full lock list', r['notes'][0])
                    self.assertIn('8.0.40, 8.4.3 and 9.1.0', r['notes'][0])
                else:
                    self.assertEqual(notes, [])

    def test_latch_note_is_skipped_when_data_locks_is_not_read(self):
        r = self.report(empty_mysql('8.0.35'), deadlock_only=True)
        self.assertTrue(r['capabilities']['data_locks_global_latch'])
        self.assertFalse(has_note(r, 'global lock-system latch'))
        r = self.report(empty_mysql('9.0.1', ps_on=False))
        self.assertFalse(has_note(r, 'global lock-system latch'))

    def test_mariadb_never_gets_the_latch_note(self):
        for version in MARIADB_VERSIONS:
            r = self.report(empty_mariadb(version))
            self.assertEqual(r['engine'], 'mariadb')
            self.assertFalse(r['capabilities']['data_locks_global_latch'])
            self.assertFalse(has_note(r, 'global lock-system latch'))


# ---- 2. the full MySQL 8.4 scenario ----------------------------------------------

class TestMySQLScenario(LockCase):
    def setUp(self):
        self.conn = mysql_conn()
        self.r = self.report(self.conn)

    def test_envelope_and_capabilities(self):
        r = self.r
        self.assertEqual((r['schema_version'], r['engine'], r['server_version'], r['scope'], r['limit']),
                         ('locks-1.1', 'mysql', '8.4.6', 'all', 500))
        self.assertTrue(r['captured_at'].endswith('+00:00'))
        self.assertEqual(r['capabilities'], {
            'performance_schema': True, 'process_privilege': True, 'transactions': True,
            'row_locks': 'all', 'lock_waits': True, 'metadata_locks': True, 'mdl_instrument': True,
            'last_statement': True, 'deadlock': True, 'data_locks_global_latch': False,
            'metadata_lock_info': None})
        self.assertFalse(r['row_locks_truncated'])
        self.assertFalse(r['waits_truncated'])
        self.assertFalse(r['metadata_locks']['truncated'])
        for note in (locks.KILL_NOTE, locks.IMPLICIT_NOTE, locks.SNAPSHOT_NOTE):
            self.assertIn(note, r['notes'])
        self.assertTrue(has_note(r, 'Metadata lock blockers follow the server'))
        for absent in ('PROCESS privilege', 'global lock-system latch', 'possible blocker',
                       'LOCK_DATA is empty', 'Long LOCK_DATA', 'raise the limit'):
            self.assertFalse(has_note(r, absent), absent)

    def test_blocking_tree_roots_chain_and_kill(self):
        tree = self.r['blocking_tree']
        # Root order: most sessions blocked first.
        self.assertEqual([n['connection_id'] for n in tree], [23, 26])
        first, second = tree
        self.assertEqual((first['blocked_count'], second['blocked_count']), (3, 2))
        for root in tree:
            self.assertTrue(root['root'])
            self.assertFalse(root['cycle'])
            self.assertTrue(root['idle_in_transaction'])
            self.assertEqual(root['command'], 'Sleep')
            self.assertIsNone(root['query'])
            self.assertIsNone(root['waits_for'])
        # An idle blocker has no statement to stop: only KILL <id> helps.
        self.assertEqual(first['kill'], {'query': None, 'connection': 'KILL 23'})
        self.assertEqual(second['kill'], {'query': None, 'connection': 'KILL 26'})
        self.assertEqual((first['trx_id'], first['trx_age_s'], first['rows_locked'], first['user']),
                         ('1913', 300, 4, 'app'))
        self.assertEqual(second['trx_id'], '562947965813544')
        self.assertEqual(first['last_statement'], 'SELECT * FROM t WHERE k >= 60 FOR UPDATE')
        self.assertEqual(second['last_statement'], 'SELECT COUNT(*) FROM m')
        # 24 waited 60 s, 25 waited 40 s: longest first. 27 waits for 24: 23 -> 24 -> 27.
        self.assertEqual([c['connection_id'] for c in first['blocked']], [24, 25])
        via24 = first['blocked'][0]
        self.assertFalse(via24['root'])
        self.assertIsNone(via24['kill'])
        self.assertEqual(via24['query'], 'UPDATE t SET v=1 WHERE id=1')
        self.assertEqual([c['connection_id'] for c in via24['blocked']], [27])
        self.assertEqual(via24['blocked'][0]['blocked'], [])
        self.assertEqual(via24['blocked'][0]['waits_for'][0]['wait_s'], 20)
        self.assertEqual(len(via24['waits_for']), 1)
        edge = via24['waits_for'][0]
        self.assertEqual((edge['kind'], edge['reason'], edge['wait_s'], edge['statement_s']),
                         ('row', 'granted', 60, 60.0))
        self.assertEqual((edge['lock']['mode'], edge['lock']['index'], edge['lock']['lock_data'],
                          edge['lock']['status'], edge['lock']['mode_label']),
                         ('X,REC_NOT_GAP', 'PRIMARY', '1', 'WAITING', 'record only'))
        self.assertEqual((edge['blocking_lock']['lock_id'], edge['blocking_lock']['status']),
                         (PK1_23[0], 'GRANTED'))
        insert = first['blocked'][1]['waits_for'][0]
        self.assertEqual((insert['lock']['mode'], insert['lock']['mode_label'], insert['lock']['lock_data'],
                          insert['lock']['index']),
                         ('X,GAP,INSERT_INTENTION', 'insert intention', '60, 6', 'k'))
        self.assertEqual((insert['blocking_lock']['mode'], insert['blocking_lock']['mode_label']),
                         ('X', 'next-key'))

    def test_metadata_branch_queues_the_select_behind_the_alter(self):
        children = self.r['blocking_tree'][1]['blocked']
        self.assertEqual([c['connection_id'] for c in children], [28])
        alter = children[0]
        self.assertFalse(alter['idle_in_transaction'])
        self.assertEqual(alter['query'], 'ALTER TABLE m ADD COLUMN c INT')
        # Its own granted SHARED_UPGRADABLE is not a blocker: one edge, from 26.
        self.assertEqual(len(alter['waits_for']), 1)
        edge = alter['waits_for'][0]
        self.assertEqual((edge['kind'], edge['reason'], edge['certain'], edge['statement_s']),
                         ('metadata', 'granted', True, 15.0))
        self.assertEqual(edge['lock'], {'lock_type': 'EXCLUSIVE', 'mode': 'X', 'object_type': 'TABLE',
                                        'schema': 'lk', 'table': 'm'})
        self.assertEqual(edge['blocking_lock'], {'lock_type': 'SHARED_READ', 'mode': 'SR', 'status': 'GRANTED'})
        self.assertEqual([c['connection_id'] for c in alter['blocked']], [29])
        select = alter['blocked'][0]['waits_for'][0]
        self.assertEqual(select['reason'], 'queued')
        self.assertEqual(select['blocking_lock'], {'lock_type': 'EXCLUSIVE', 'mode': 'X', 'status': 'PENDING'})
        self.assertEqual(mdl_pairs(self.r), {(28, 'X'): ([('granted', 26, 'SR')], True),
                                             (29, 'SR'): ([('queued', 28, 'X')], True)})
        waits = {w['waiting']['connection_id']: w for w in self.r['metadata_locks']['waits']}
        self.assertEqual(waits[28]['statement'], 'ALTER TABLE m ADD COLUMN c INT')
        self.assertEqual(waits[28]['state'], 'Waiting for table metadata lock')
        self.assertIsNone(waits[28]['blocked_by'][0]['statement'])
        held = self.r['metadata_locks']['locks']
        self.assertEqual(len(held), 8)
        self.assertEqual([m['status'] for m in held[:2]], ['PENDING', 'PENDING'])
        self.assertEqual(held[0]['explain'], locks.MDL_EXPLAIN['EXCLUSIVE'])

    def test_summary_counts(self):
        self.assertEqual(self.r['summary'], {
            'transactions': 5, 'row_lock_waits': 3, 'metadata_lock_waits': 2, 'blocked_sessions': 5,
            'root_blockers': 2, 'idle_in_transaction_blockers': 2, 'longest_wait_s': 60,
            'oldest_transaction_s': 300, 'row_locks': 12, 'deadlock': True, 'ddl_waits': 1})

    def test_row_locks_waiting_first_and_owner_from_the_transaction(self):
        rows = self.r['row_locks']
        self.assertEqual([lock['status'] for lock in rows], ['WAITING'] * 3 + ['GRANTED'] * 9)
        self.assertEqual([lock['lock_id'] for lock in rows[:3]], [PK1_24[0], PK2_27[0], K60_25[0]])
        # Table locks sort before record locks within a status.
        self.assertEqual([lock['lock_type'] for lock in rows[3:7]], ['TABLE'] * 4)
        owners = {lock['lock_id']: lock['connection_id'] for lock in rows}
        self.assertEqual(owners, {T_23[0]: 23, PK1_23[0]: 23, PK3_23[0]: 23, K60_23[0]: 23, K70_23[0]: 23,
                                  T_24[0]: 24, PK2_24[0]: 24, PK1_24[0]: 24, T_25[0]: 25, K60_25[0]: 25,
                                  T_27[0]: 27, PK2_27[0]: 27})
        # The converted implicit lock carries thread 64 (connection 24), but
        # ENGINE_TRANSACTION_ID 1913 -> INNODB_TRX names connection 23.
        converted = by_lock_id(self.r)[PK3_23[0]]
        self.assertEqual((converted['ps_thread_id'], converted['connection_id'], converted['trx_id']),
                         (64, 23, '1913'))
        table = by_lock_id(self.r)[T_23[0]]
        self.assertEqual((table['lock_type'], table['mode'], table['mode_label'], table['index']),
                         ('TABLE', 'IX', 'intention exclusive', None))
        self.assertIsNone(table['lock_data'])
        self.assertFalse(table['lock_data_missing'])
        self.assertFalse(table['lock_data_truncated'])
        record = by_lock_id(self.r)[K60_23[0]]
        self.assertEqual((record['schema'], record['table'], record['partition'], record['index'],
                          record['lock_data'], record['mode_label']),
                         ('lk', 't', None, 'k', '60, 6', 'next-key'))

    def test_transactions_list(self):
        trx = self.r['transactions']
        self.assertEqual([t['trx_id'] for t in trx], ['1913', '562947965813544', '1914', '1915', '1916'])
        t = by_trx_id(self.r)
        idle = t['1913']
        self.assertEqual({k: idle[k] for k in (
            'connection_id', 'state', 'started', 'age_s', 'user', 'host', 'db', 'command', 'time_s',
            'current_statement', 'last_statement', 'idle_in_transaction', 'blocking', 'blocked_by',
            'kill', 'isolation_level', 'read_only', 'rows_locked', 'lock_structs', 'rows_modified',
            'tables_in_use', 'tables_locked', 'wait_s', 'query', 'query_truncated')}, {
            'connection_id': 23, 'state': 'RUNNING', 'started': '2026-10-12 21:25:00', 'age_s': 300,
            'user': 'app', 'host': 'localhost', 'db': 'lk', 'command': 'Sleep', 'time_s': 300.0,
            'current_statement': None, 'last_statement': 'SELECT * FROM t WHERE k >= 60 FOR UPDATE',
            'idle_in_transaction': True, 'blocking': ['c24', 'c25'], 'blocked_by': [],
            'kill': {'query': None, 'connection': 'KILL 23'}, 'isolation_level': 'REPEATABLE READ',
            'read_only': False, 'rows_locked': 4, 'lock_structs': 4, 'rows_modified': 1,
            'tables_in_use': 0, 'tables_locked': 1, 'wait_s': None, 'query': None, 'query_truncated': False})
        waiting = t['1914']
        self.assertEqual({k: waiting[k] for k in (
            'state', 'requested_lock_id', 'wait_started', 'wait_s', 'current_statement', 'last_statement',
            'idle_in_transaction', 'blocking', 'blocked_by', 'kill', 'operation_state')}, {
            'state': 'LOCK WAIT', 'requested_lock_id': '1914:6:4:2', 'wait_started': '2026-10-12 21:29:00',
            'wait_s': 60, 'current_statement': 'UPDATE t SET v=1 WHERE id=1', 'last_statement': None,
            'idle_in_transaction': False, 'blocking': ['c27'], 'blocked_by': ['c23'],
            # A running statement can be stopped with KILL QUERY.
            'kill': {'query': 'KILL QUERY 24', 'connection': 'KILL 24'},
            'operation_state': 'starting index read'})
        reader = t['562947965813544']
        self.assertEqual((reader['connection_id'], reader['read_only'], reader['idle_in_transaction'],
                          reader['blocking'], reader['user'], reader['host']),
                         (26, True, True, ['c28'], 'report', '10.0.0.7'))
        self.assertEqual((t['1916']['isolation_level'], t['1916']['blocked_by']), ('READ COMMITTED', ['c24']))

    def test_waits_list_longest_first(self):
        waits = self.r['waits']
        self.assertEqual([(w['requesting']['connection_id'], w['blocking']['connection_id'], w['wait_s'])
                          for w in waits], [(24, 23, 60), (25, 23, 40), (27, 24, 20)])
        first = waits[0]
        self.assertEqual((first['statement'], first['statement_s'], first['wait_started']),
                         ('UPDATE t SET v=1 WHERE id=1', 60.0, '2026-10-12 21:29:00'))
        self.assertEqual((first['requesting']['ps_thread_id'], first['blocking']['ps_thread_id']), (64, 63))
        self.assertEqual((first['requesting']['lock_id'], first['blocking']['lock_id']), (PK1_24[0], PK1_23[0]))

    def test_queries_are_bounded_and_exclude_this_session(self):
        for sql, _ in self.conn.calls[1:]:
            body = sql[len(LOCKS_TAG):]
            if body in ('SHOW ENGINE INNODB STATUS', 'SHOW GRANTS', 'SELECT @@server_uuid'):
                continue
            self.assertTrue(' LIMIT ' in body or re.search(r' IN \([^)]+\)', body), body)
        trx_sql = self.conn.sql('FROM information_schema.INNODB_TRX')[0]
        self.assertIn('trx_mysql_thread_id <> CONNECTION_ID()', trx_sql)
        self.assertIn('LEFT(trx_query, 4096)', trx_sql)
        self.assertIn('LIMIT 501', trx_sql)
        self.assertIn('t.PROCESSLIST_ID <> CONNECTION_ID()', self.conn.sql('FROM performance_schema.metadata_locks')[0])
        for needle in ('FROM performance_schema.data_lock_waits', FULL_LOCK_LIST,
                       'FROM performance_schema.metadata_locks'):
            self.assertIn('LIMIT 501', self.conn.sql(needle)[0])
        self.assertIn('LEFT(LOCK_DATA, 513)', self.conn.sql(FULL_LOCK_LIST)[0])
        threads = lookup_sql(self.conn, 'performance_schema.threads')[0]
        self.assertEqual(ids_in(threads, 'PROCESSLIST_ID'), [23, 26, 24, 25, 27, 28, 29])
        self.assertEqual(sorted(ids_in(threads, 'THREAD_ID')), [63, 64, 65, 67])
        # Only the idle sessions' last statements are looked up.
        self.assertEqual(ids_in(self.conn.sql('FROM performance_schema.events_statements_current')[0],
                                'PROCESSLIST_ID'), [23, 26])
        self.assertEqual(lookup_sql(self.conn, 'information_schema.PROCESSLIST'), [])

    def test_running_root_gets_kill_query(self):
        threads = [list(row) for row in MYSQL_THREADS]
        threads[0] = [63, 23, 'app', 'localhost', 'lk', 'Query', 300, 'updating', 'UPDATE t SET v=9 WHERE k > 50']
        conn = mysql_conn(threads=threads_answer(threads))
        r = self.report(conn)
        root = r['blocking_tree'][0]
        self.assertEqual(root['connection_id'], 23)
        self.assertFalse(root['idle_in_transaction'])
        self.assertEqual(root['query'], 'UPDATE t SET v=9 WHERE k > 50')
        self.assertIsNone(root['last_statement'])
        self.assertEqual(root['kill'], {'query': 'KILL QUERY 23', 'connection': 'KILL 23'})
        self.assertEqual(r['summary']['idle_in_transaction_blockers'], 1)
        self.assertEqual(ids_in(conn.sql('FROM performance_schema.events_statements_current')[0],
                                'PROCESSLIST_ID'), [26])

    def test_wait_cycle_picks_the_oldest_transaction_as_root(self):
        # innodb_deadlock_detect=OFF: 23 and 24 wait for each other until the lock wait timeout.
        pk2_23 = dl('1913:6:4:3', '1913', 63, 'PRIMARY', 'RECORD', 'X,REC_NOT_GAP', 'WAITING', '2')
        pk2_24 = dl('1914:6:4:3', '1914', 64, 'PRIMARY', 'RECORD', 'X,REC_NOT_GAP', 'GRANTED', '2')
        pk1_24 = dl('1914:6:4:2', '1914', 64, 'PRIMARY', 'RECORD', 'X,REC_NOT_GAP', 'WAITING', '1')
        trx = [list(MYSQL_TRX[0]), list(MYSQL_TRX[2])]
        trx[0][1], trx[0][3] = 'LOCK WAIT', 100
        trx[1][3] = 50
        threads = [list(row) for row in MYSQL_THREADS]
        threads[0] = [63, 23, 'app', 'localhost', 'lk', 'Query', 10, 'updating', 'UPDATE t SET v=1 WHERE id=2']
        conn = mysql_conn(trx=trx, waits=[dlw(pk1_24, PK1_23), dlw(pk2_23, pk2_24)], locks=[], mdl=[],
                          threads=threads_answer(threads))
        r = self.report(conn)
        tree = r['blocking_tree']
        self.assertEqual([n['connection_id'] for n in tree], [23])
        root = tree[0]
        self.assertTrue(root['cycle'])
        self.assertEqual(root['blocked_count'], 1)
        self.assertEqual(root['kill'], {'query': 'KILL QUERY 23', 'connection': 'KILL 23'})
        child = root['blocked'][0]
        self.assertEqual((child['connection_id'], child['cycle']), (24, True))
        back = child['blocked'][0]
        self.assertEqual((back['connection_id'], back.get('repeat')), (23, True))
        self.assertEqual(back['blocked'], [])
        text = locks.format_text(r)
        self.assertIn('ROOT BLOCKER (in a wait cycle), blocks 1', text)
        self.assertIn('(shown above)', text)


# ---- 3. MySQL without PROCESS ----------------------------------------------------------

class TestMySQLWithoutProcess(LockCase):
    def conn(self):
        return mysql_conn(trx=process_denied(), status=process_denied())

    def test_row_locks_waits_and_metadata_locks_still_shown(self):
        r = self.report(self.conn())
        caps = r['capabilities']
        self.assertEqual((caps['process_privilege'], caps['transactions'], caps['deadlock']), (False, False, False))
        self.assertEqual((caps['row_locks'], caps['lock_waits'], caps['metadata_locks']), ('all', True, True))
        self.assertTrue(has_note(r, 'Without the PROCESS privilege MySQL hides information_schema.INNODB_TRX'))
        self.assertTrue(has_note(r, 'GRANT PROCESS ON *.* TO <user>;'))
        self.assertIsNone(r['deadlock'])
        self.assertEqual(r['transactions'], [])
        self.assertIsNone(r['summary']['oldest_transaction_s'])
        self.assertEqual(len(r['row_locks']), 12)
        self.assertEqual(len(r['waits']), 3)
        # Owners come from performance_schema.threads: THREAD_ID -> PROCESSLIST_ID.
        owners = {lock['lock_id']: lock['connection_id'] for lock in r['row_locks']}
        self.assertEqual(owners[PK1_23[0]], 23)
        self.assertEqual(owners[PK1_24[0]], 24)
        self.assertEqual(owners[K60_25[0]], 25)
        # Without INNODB_TRX the converted implicit lock is attributed to its
        # thread's session, as the PROCESS note warns.
        self.assertEqual(owners[PK3_23[0]], 24)
        tree = r['blocking_tree']
        self.assertEqual([(n['connection_id'], n['blocked_count']) for n in tree], [(23, 3), (26, 2)])
        for root in tree:
            self.assertTrue(root['idle_in_transaction'])
            self.assertEqual(root['kill']['query'], None)
        self.assertEqual(tree[0]['trx_id'], '1913')
        self.assertEqual(tree[0]['last_statement'], 'SELECT * FROM t WHERE k >= 60 FOR UPDATE')

    def test_threads_lookup_uses_thread_ids_and_metadata_owners(self):
        conn = self.conn()
        self.report(conn)
        threads = lookup_sql(conn, 'performance_schema.threads')[0]
        self.assertEqual(sorted(ids_in(threads, 'THREAD_ID')), [63, 64, 65, 67])
        self.assertEqual(sorted(ids_in(threads, 'PROCESSLIST_ID')), [23, 24, 26, 28, 29])

    def test_cli_exits_0(self):
        code, out, err, _ = TestLocksCLI.call(self, self.conn())
        self.assertEqual(code, 0, err)
        self.assertIn('BLOCKING TREE', out)
        self.assertIn('TRANSACTIONS (0)\nNot available (see notes).', out)
        self.assertIn('note: Without the PROCESS privilege MySQL hides', err)


# ---- 4. MariaDB ----------------------------------------------------------------------------

class TestMariaDB(LockCase):
    def test_waits_only_row_locks_explanations_and_trx_id_zero(self):
        for version in MARIADB_VERSIONS:
            with self.subTest(version=version):
                conn = ma_conn(version)
                r = self.report(conn)
                caps = r['capabilities']
                self.assertEqual(r['engine'], 'mariadb')
                self.assertEqual(caps, {
                    'performance_schema': False, 'process_privilege': True, 'transactions': True,
                    'row_locks': 'waits_only', 'lock_waits': True, 'metadata_locks': False,
                    'mdl_instrument': None, 'last_statement': False, 'deadlock': True,
                    'data_locks_global_latch': False, 'metadata_lock_info': None})
                self.assertTrue(has_note(r, 'MariaDB lists only the row locks involved in a wait'))
                self.assertTrue(any("performance-schema-instrument='wait/lock/metadata/sql/mdl=ON'" in n
                                    and 'the MariaDB default' in n for n in r['notes']))
                self.assertTrue(has_note(r, "An idle transaction's last statement needs Performance Schema"))
                locks_by_id = by_lock_id(r)
                record = locks_by_id[MA_PK1_40[0]]
                self.assertEqual((record['mode'], record['mode_label'], record['status'], record['connection_id'],
                                  record['schema'], record['table'], record['index'], record['lock_data']),
                                 ('X', 'record or next-key', 'GRANTED', 5, 'lk', 't', 'PRIMARY', '1'))
                self.assertIn('record-only and next-key locks both as X', record['explain'])
                self.assertIn('(exclusive)', record['explain'])
                gap = locks_by_id[MA_K60_42[0]]
                self.assertEqual((gap['mode'], gap['mode_label'], gap['status'], gap['connection_id']),
                                 ('X,GAP', 'gap or insert intention', 'WAITING', 7))
                self.assertIn('insert intention', gap['explain'])
                self.assertIn('(shared)', locks_by_id[MA_PK5_0[0]]['explain'])
                self.assertEqual([lock['status'] for lock in r['row_locks']], ['WAITING'] * 3 + ['GRANTED'] * 3)
                # trx_id 0 is shared by connections 8 and 9: the lock has no known owner.
                self.assertIsNone(locks_by_id[MA_PK5_0[0]]['connection_id'])
                tree = r['blocking_tree']
                self.assertEqual([n['key'] for n in tree], ['c5', 't0'])
                self.assertEqual(tree[0]['kill'], {'query': None, 'connection': 'KILL 5'})
                self.assertTrue(tree[0]['idle_in_transaction'])
                self.assertEqual([c['connection_id'] for c in tree[0]['blocked']], [6, 7])
                self.assertEqual(tree[0]['blocked'][0]['waits_for'][0]['lock']['mode_label'], 'record or next-key')
                self.assertEqual((tree[1]['connection_id'], tree[1]['trx_id'], tree[1]['kill']), (None, '0', None))
                self.assertEqual([c['connection_id'] for c in tree[1]['blocked']], [10])
                for t in r['transactions']:
                    if t['trx_id'] == '0':
                        self.assertEqual((t['blocking'], t['blocked_by']), ([], []))
                self.assertEqual([t['connection_id'] for t in r['transactions'] if t['trx_id'] == '0'], [8, 9])
                blocked_by_zero = [w for w in r['waits'] if w['requesting']['connection_id'] == 10][0]
                self.assertIsNone(blocked_by_zero['blocking']['connection_id'])
                self.assertEqual(r['summary']['root_blockers'], 2)
                self.assertEqual(r['summary']['idle_in_transaction_blockers'], 1)
                self.assertEqual(r['transactions'][0]['time_s'], 300.0)
                self.assertIn('TIME_MS / 1000', lookup_sql(conn, 'information_schema.PROCESSLIST')[0])
                self.assertFalse(any('performance_schema.' in sql for sql, _ in conn.calls))
                self.assertEqual(r['deadlock']['victim_trx_id'], '37')
                text = locks.format_text(r)
                self.assertIn('ROW LOCKS (6, waits only)', text)
                self.assertIn('Transaction 0 (no session) ROOT BLOCKER, blocks 1', text)

    def test_performance_schema_on_with_the_mdl_instrument_off(self):
        conn = ma_conn('11.4.13-MariaDB-ubu2404', ps_on=True, setup=[
            ['instrument', 'NO'], ['global_instrumentation', 'YES'], ['events_statements_current', 'NO']])
        r = self.report(conn)
        caps = r['capabilities']
        self.assertEqual((caps['performance_schema'], caps['mdl_instrument'], caps['metadata_locks'],
                          caps['last_statement']), (True, False, False, False))
        note = [n for n in r['notes'] if 'metadata lock instrument' in n]
        self.assertEqual(len(note), 1)
        self.assertIn('disabled (the MariaDB default)', note[0])
        self.assertIn("performance-schema-instrument='wait/lock/metadata/sql/mdl=ON'", note[0])
        self.assertTrue(has_note(r, 'events_statements_current and thread_instrumentation consumers; '
                                    'MariaDB disables the first by default'))
        self.assertEqual(conn.sql('FROM performance_schema.metadata_locks'), [])
        self.assertEqual(conn.sql('FROM performance_schema.events_statements_current'), [])
        self.assertEqual(lookup_sql(conn, 'information_schema.PROCESSLIST'), [])
        threads = lookup_sql(conn, 'performance_schema.threads')[0]
        self.assertEqual(ids_in(threads, 'PROCESSLIST_ID'), [5, 8, 9, 6, 7, 10])
        self.assertEqual(ids_in(threads, 'THREAD_ID'), [])
        self.assertEqual(by_trx_id(r)['40']['user'], 'app')

    def test_without_process_only_metadata_locks_remain(self):
        mdl_rows = [mdl('TABLE', 'lk', 'm', 'EXCLUSIVE', 'PENDING', 111, 11),
                    mdl('TABLE', 'lk', 'm', 'SHARED_READ', 'GRANTED', 108, 8),
                    mdl('TABLE', 'lk', 'm', 'SHARED_UPGRADABLE', 'GRANTED', 111, 11)]
        threads = MA_THREADS + [[111, 11, 'dba', 'localhost', 'lk', 'Query', 12,
                                 'Waiting for table metadata lock', 'ALTER TABLE m ADD COLUMN c INT']]
        conn = fake('11.8.3-MariaDB', True, trx=process_denied(), ma_waits=process_denied(),
                    ma_locks=process_denied(), status=process_denied(), setup=SETUP_ON, mdl=mdl_rows,
                    threads=threads_answer(threads), last=last_answer([[8, 'SELECT * FROM m LIMIT 1', 7]]))
        r = self.report(conn)
        caps = r['capabilities']
        self.assertEqual({k: caps[k] for k in ('process_privilege', 'transactions', 'row_locks', 'lock_waits',
                                                'deadlock', 'metadata_locks', 'mdl_instrument')},
                         {'process_privilege': False, 'transactions': False, 'row_locks': 'none',
                          'lock_waits': False, 'deadlock': False, 'metadata_locks': True, 'mdl_instrument': True})
        self.assertTrue(has_note(r, 'Without the PROCESS privilege MariaDB hides information_schema.INNODB_TRX, '
                                    'INNODB_LOCKS and INNODB_LOCK_WAITS'))
        self.assertEqual(conn.sql('INNODB_LOCK'), [])
        self.assertEqual(conn.sql('SHOW ENGINE'), [])
        self.assertEqual((r['transactions'], r['row_locks'], r['waits'], r['deadlock']), ([], [], [], None))
        self.assertFalse(has_note(r, 'MariaDB lists only the row locks'))
        self.assertEqual(mdl_pairs(r), {(11, 'X'): ([('granted', 8, 'SR')], True)})
        tree = r['blocking_tree']
        self.assertEqual([(n['connection_id'], n['blocked_count']) for n in tree], [(8, 1)])
        self.assertTrue(tree[0]['idle_in_transaction'])
        self.assertEqual(tree[0]['last_statement'], 'SELECT * FROM m LIMIT 1')
        self.assertEqual(tree[0]['kill'], {'query': None, 'connection': 'KILL 8'})


# ---- 5. MySQL with Performance Schema off ---------------------------------------------------

class TestMySQLPerformanceSchemaOff(LockCase):
    def test_transactions_still_shown_from_the_process_list(self):
        conn = mysql_conn(ps_on=False, processlist=processlist_answer(MYSQL_PROCESSLIST))
        r = self.report(conn)
        self.assertFalse(any('performance_schema.' in sql for sql, _ in conn.calls))
        self.assertEqual(conn.sql('data_lock'), [])
        caps = r['capabilities']
        self.assertEqual({k: caps[k] for k in ('performance_schema', 'transactions', 'row_locks', 'lock_waits',
                                                'metadata_locks', 'mdl_instrument', 'last_statement', 'deadlock')},
                         {'performance_schema': False, 'transactions': True, 'row_locks': 'none',
                          'lock_waits': False, 'metadata_locks': False, 'mdl_instrument': None,
                          'last_statement': False, 'deadlock': True})
        self.assertTrue(has_note(r, 'Performance Schema is off. MySQL lists row locks and lock waits only'))
        self.assertNotIn(locks.IMPLICIT_NOTE, r['notes'])
        sql = lookup_sql(conn, 'information_schema.PROCESSLIST')[0]
        self.assertEqual(ids_in(sql, 'ID'), [23, 26, 24, 25, 27])
        self.assertNotIn('TIME_MS', sql)
        self.assertEqual(len(r['transactions']), 5)
        idle = by_trx_id(r)['1913']
        self.assertEqual((idle['user'], idle['command'], idle['idle_in_transaction'], idle['last_statement']),
                         ('app', 'Sleep', True, None))
        self.assertEqual(r['blocking_tree'], [])
        self.assertEqual((r['row_locks'], r['waits']), ([], []))
        self.assertEqual(r['deadlock']['victim_trx_id'], '1909')


# ---- 6. metadata lock compatibility -----------------------------------------------------------

class TestMetadataLockRules(LockCase):
    def mysql(self, rows):
        return self.report(empty_mysql(mdl=rows))

    def test_pending_shared_read_waits_for_a_granted_exclusive(self):
        r = self.mysql([mdl('TABLE', 'lk', 'm', 'SHARED_READ', 'PENDING', 71, 31),
                        mdl('TABLE', 'lk', 'm', 'EXCLUSIVE', 'GRANTED', 70, 30)])
        self.assertEqual(mdl_pairs(r), {(31, 'SR'): ([('granted', 30, 'X')], True)})

    def test_pending_exclusive_waits_for_other_owners_only(self):
        r = self.mysql([mdl('TABLE', 'lk', 'm', 'EXCLUSIVE', 'PENDING', 68, 28),
                        mdl('TABLE', 'lk', 'm', 'SHARED_READ', 'GRANTED', 66, 26),
                        mdl('TABLE', 'lk', 'm', 'SHARED_UPGRADABLE', 'GRANTED', 68, 28),
                        mdl('TABLE', 'lk', 'm', 'SHARED_WRITE', 'GRANTED', 63, 23)])
        self.assertEqual(mdl_pairs(r), {(28, 'X'): ([('granted', 26, 'SR'), ('granted', 23, 'SW')], True)})

    def test_same_owner_upgrade_is_not_self_blocking(self):
        r = self.mysql([mdl('TABLE', 'lk', 'm', 'EXCLUSIVE', 'PENDING', 68, 28),
                        mdl('TABLE', 'lk', 'm', 'SHARED_UPGRADABLE', 'GRANTED', 68, 28)])
        self.assertEqual(mdl_pairs(r), {(28, 'X'): ([], True)})
        self.assertEqual(r['blocking_tree'], [])
        self.assertEqual((r['summary']['metadata_lock_waits'], r['summary']['root_blockers']), (1, 0))
        self.assertIn('blocked by no conflicting lock found', locks.format_text(r))

    def test_requests_queue_behind_a_pending_exclusive(self):
        r = self.mysql([mdl('TABLE', 'lk', 'm', 'EXCLUSIVE', 'PENDING', 68, 28),
                        mdl('TABLE', 'lk', 'm', 'SHARED_READ', 'PENDING', 69, 29),
                        mdl('TABLE', 'lk', 'm', 'SHARED_WRITE', 'PENDING', 70, 30),
                        mdl('TABLE', 'lk', 'm', 'SHARED_HIGH_PRIO', 'PENDING', 71, 31),
                        mdl('TABLE', 'lk', 'm', 'SHARED_READ', 'GRANTED', 66, 26)])
        self.assertEqual(mdl_pairs(r), {
            (28, 'X'): ([('granted', 26, 'SR')], True),
            (29, 'SR'): ([('queued', 28, 'X')], True),
            (30, 'SW'): ([('queued', 28, 'X')], True),
            # High-priority shared requests jump the queue.
            (31, 'SH'): ([], True)})
        self.assertEqual([(n['connection_id'], n['blocked_count']) for n in r['blocking_tree']], [(26, 3)])
        text = locks.format_text(r)
        self.assertIn('(queued behind a waiting request)', text)
        self.assertIn('blocked by X by connection 28 (queued)', text)

    def test_mariadb_backup_locks_list_every_possible_blocker(self):
        rows = [mdl('BACKUP', None, None, 'BACKUP_FTWRL1', 'PENDING', 81, 41, 'EXPLICIT'),
                mdl('BACKUP', None, None, 'BACKUP_DML', 'GRANTED', 80, 40, 'STATEMENT'),
                mdl('BACKUP', None, None, 'BACKUP_DML', 'GRANTED', 81, 41, 'STATEMENT')]
        r = self.report(empty_mariadb('11.4.13-MariaDB-ubu2404', mdl=rows))
        self.assertEqual(mdl_pairs(r), {(41, 'BACKUP_FTWRL1'): ([('granted', 40, 'BACKUP_DML')], False)})
        self.assertTrue(has_note(r, 'MariaDB BACKUP locks) follow rules myflames does not model'))
        self.assertTrue(has_note(r, 'possible blocker'))
        edge = r['blocking_tree'][0]['blocked'][0]['waits_for'][0]
        self.assertFalse(edge['certain'])
        self.assertEqual(edge['lock']['object_type'], 'BACKUP')

    def test_scoped_global_intention_exclusive_and_shared(self):
        r = self.mysql([mdl('GLOBAL', None, None, 'SHARED', 'PENDING', 91, 51, 'EXPLICIT'),
                        mdl('GLOBAL', None, None, 'INTENTION_EXCLUSIVE', 'PENDING', 92, 52, 'STATEMENT'),
                        mdl('GLOBAL', None, None, 'INTENTION_EXCLUSIVE', 'GRANTED', 90, 50, 'STATEMENT')])
        # FLUSH TABLES WITH READ LOCK waits for the running write; a later write queues behind it.
        self.assertEqual(mdl_pairs(r), {(51, 'S'): ([('granted', 50, 'IX')], True),
                                        (52, 'IX'): ([('queued', 51, 'S')], True)})
        self.assertEqual([(n['connection_id'], n['blocked_count']) for n in r['blocking_tree']], [(50, 2)])
        r = self.mysql([mdl('GLOBAL', None, None, 'SHARED', 'GRANTED', 91, 51, 'EXPLICIT'),
                        mdl('GLOBAL', None, None, 'INTENTION_EXCLUSIVE', 'PENDING', 92, 52, 'STATEMENT')])
        self.assertEqual(mdl_pairs(r), {(52, 'IX'): ([('granted', 51, 'S')], True)})
        # Intention exclusive locks are compatible with each other.
        r = self.mysql([mdl('GLOBAL', None, None, 'INTENTION_EXCLUSIVE', 'GRANTED', 90, 50, 'STATEMENT'),
                        mdl('GLOBAL', None, None, 'INTENTION_EXCLUSIVE', 'PENDING', 92, 52, 'STATEMENT')])
        self.assertEqual(mdl_pairs(r), {(52, 'IX'): ([], True)})


# ---- 7. deadlock parsing ------------------------------------------------------------------------

# fixture: (engine, version, detected_at, victim, victim trx, transactions)
# transaction: (n, trx_id, connection, statement, operation, held mode, held key, waited mode, waited key)
DEADLOCKS = {
    'mysql-8.4.11-innodb-status.txt': ('mysql', '8.4.11', '2026-10-12 21:30:37', 2, '1909', [
        (1, '1910', 21, 'UPDATE d SET v=2 WHERE id=1', 'starting index read', 'X,REC_NOT_GAP', 2, 'X,REC_NOT_GAP', 1),
        (2, '1909', 22, 'UPDATE d SET v=1 WHERE id=2', 'starting index read', 'X,REC_NOT_GAP', 1, 'X,REC_NOT_GAP', 2)]),
    'mysql-26.7.0-gap-deadlock-status.txt': ('mysql', '26.7.0', '2026-10-12 21:33:53', 2, '2116', [
        (1, '2118', 37, 'INSERT INTO g VALUES (16,1)', 'inserting', 'X,GAP', 20, 'X,GAP,INSERT_INTENTION', 20),
        (2, '2116', 38, 'INSERT INTO g VALUES (15,1)', 'inserting', 'X,GAP', 20, 'X,GAP,INSERT_INTENTION', 20)]),
    'mariadb-11.4.13-innodb-status.txt': ('mariadb', '11.4.13-MariaDB', '2026-10-12 21:31:12', 1, '37', [
        (1, '37', 5, 'UPDATE d SET v=1 WHERE id=2', 'starting index read', 'X,REC_NOT_GAP', 1, 'X,REC_NOT_GAP', 2),
        (2, '38', 6, 'UPDATE d SET v=2 WHERE id=1', 'starting index read', 'X,REC_NOT_GAP', 2, 'X,REC_NOT_GAP', 1)]),
    'mariadb-13.0.2-gap-deadlock-status.txt': ('mariadb', '13.0.2-MariaDB', '2026-10-12 21:33:53', 1, '53', [
        (1, '53', 34, 'INSERT INTO g VALUES (15,1)', 'inserting', 'X,GAP', 20, 'X,GAP,INSERT_INTENTION', 20),
        (2, '54', 35, 'INSERT INTO g VALUES (16,1)', 'inserting', 'X,GAP', 20, 'X,GAP,INSERT_INTENTION', 20)]),
}
LABELS = {'X,REC_NOT_GAP': 'record only', 'X,GAP': 'gap', 'X,GAP,INSERT_INTENTION': 'insert intention'}


class TestDeadlockParsing(LockCase):
    def test_all_four_fixtures(self):
        for name, (engine, _, detected, victim, victim_trx, expected) in DEADLOCKS.items():
            with self.subTest(fixture=name):
                d = locks.parse_latest_deadlock(fixture(name), engine)
                self.assertEqual((d['detected_at'], d['victim'], d['victim_trx_id'], d['raw_truncated']),
                                 (detected, victim, victim_trx, False))
                self.assertEqual(len(d['transactions']), 2)
                for trx, (n, trx_id, conn_id, statement, operation, held, held_key, waited, waited_key) in zip(
                        d['transactions'], expected):
                    self.assertEqual({k: trx[k] for k in (
                        'n', 'trx_id', 'connection_id', 'user', 'host', 'statement', 'operation', 'lock_structs',
                        'row_locks', 'tables_in_use', 'tables_locked', 'lock_wait', 'victim', 'active_s')},
                        {'n': n, 'trx_id': trx_id, 'connection_id': conn_id, 'user': 'root', 'host': 'localhost',
                         'statement': statement, 'operation': operation, 'lock_structs': 3, 'row_locks': 2,
                         'tables_in_use': 1, 'tables_locked': 1, 'lock_wait': True, 'victim': n == victim,
                         'active_s': trx['active_s']})
                    self.assertIsInstance(trx['active_s'], int)
                    self.assertEqual(len(trx['holds']), 1)
                    hold = trx['holds'][0]
                    self.assertEqual((hold['mode'], hold['mode_label'], hold['trx_id'], hold['waiting'],
                                      hold['lock_type'], hold['schema'], hold['index']),
                                     (held, LABELS[held], trx_id, False, 'RECORD', 'lk', 'PRIMARY'))
                    self.assertEqual(hold['records'][0]['as_signed_int'], held_key)
                    self.assertFalse(hold['records'][0]['supremum'])
                    wait = trx['waiting_for']
                    self.assertEqual((wait['mode'], wait['mode_label'], wait['trx_id'], wait['waiting']),
                                     (waited, LABELS[waited], trx_id, True))
                    self.assertEqual(wait['records'][0]['as_signed_int'], waited_key)
                raw = d['raw']
                self.assertTrue(raw.startswith(detected), raw[:40])
                self.assertTrue(raw.endswith('*** WE ROLL BACK TRANSACTION (%d)' % victim), raw[-60:])
                for outside in ('Trx id counter', 'BACKGROUND THREAD', 'LIST OF TRANSACTIONS'):
                    self.assertNotIn(outside, raw)

    def test_update_deadlock_keys_and_record_details(self):
        d = locks.parse_latest_deadlock(fixture('mysql-8.4.11-innodb-status.txt'), 'mysql')
        first = d['transactions'][0]
        self.assertEqual((first['query_id'], first['state'], first['conflicts_with']), (95, 'updating', []))
        hold = first['holds'][0]
        self.assertEqual((hold['space_id'], hold['page_no'], hold['table'], hold['partition']), (6, 4, 'd', None))
        record = hold['records'][0]
        self.assertEqual((record['heap_no'], record['fields'][0]['hex'], record['as_signed_int']), (3, '80000002', 2))
        self.assertEqual(len(record['fields']), 3)
        self.assertEqual(first['waiting_for']['records'][0]['fields'][0]['hex'], '80000001')

    def test_mariadb_conflicts_map_into_the_other_transactions_holds(self):
        d = locks.parse_latest_deadlock(fixture('mariadb-11.4.13-innodb-status.txt'), 'mariadb')
        first, second = d['transactions']
        self.assertEqual((first['state'], first['query_id']), ('Updating', 21))
        self.assertEqual([lock['trx_id'] for lock in first['conflicts_with']], ['38'])
        self.assertEqual([lock['trx_id'] for lock in second['conflicts_with']], ['37'])
        self.assertIn(first['conflicts_with'][0], second['holds'])
        self.assertIn(second['conflicts_with'][0], first['holds'])
        gap = locks.parse_latest_deadlock(fixture('mariadb-13.0.2-gap-deadlock-status.txt'), 'mariadb')
        # MariaDB also lists the waiter's own gap lock under CONFLICTING WITH; it is not a conflict.
        self.assertEqual([lock['trx_id'] for lock in gap['transactions'][0]['conflicts_with']], ['53', '54'])
        self.assertEqual([[lock['trx_id'] for lock in t['holds']] for t in gap['transactions']], [['53'], ['54']])

    def test_report_reads_the_status_through_batch_mode(self):
        for name, (engine, version, _, _, victim_trx, _) in DEADLOCKS.items():
            with self.subTest(fixture=name):
                text = fixture(name)
                maker = empty_mariadb if engine == 'mariadb' else empty_mysql
                r = self.report(maker(version, status=status_row(text)), deadlock_only=True)
                self.assertEqual(r['deadlock'], locks.parse_latest_deadlock(text, engine))
                self.assertEqual(r['deadlock']['victim_trx_id'], victim_trx)
                self.assertTrue(r['summary']['deadlock'])
                self.assertTrue(r['capabilities']['deadlock'])

    def test_raw_text_is_capped(self):
        long_statement = 'UPDATE d SET v=2 WHERE id=1 /* %s */' % ('x' * 70000)
        text = fixture('mysql-8.4.11-innodb-status.txt').replace('UPDATE d SET v=2 WHERE id=1\n',
                                                                long_statement + '\n')
        r = self.report(empty_mysql(status=status_row(text)), deadlock_only=True)
        d = r['deadlock']
        self.assertTrue(d['raw_truncated'])
        self.assertEqual(len(d['raw']), locks.RAW_DEADLOCK_CAP)
        self.assertTrue(d['raw'].endswith('…'))
        statement = d['transactions'][0]['statement']
        self.assertEqual(len(statement), locks.TEXT_CAP)
        self.assertTrue(statement.startswith('UPDATE d SET v=2 WHERE id=1 /* xxx'))
        self.assertEqual(d['victim_trx_id'], '1909')

    def test_no_deadlock_section(self):
        for empty in (None, '', NO_DEADLOCK, fixture('mariadb-11.4.13-innodb-status.txt').split(
                '------------------------\nLATEST DETECTED DEADLOCK')[0]):
            self.assertIsNone(locks.parse_latest_deadlock(empty, 'mysql'))
        r = self.report(empty_mysql(), deadlock_only=True)
        self.assertIsNone(r['deadlock'])
        self.assertTrue(r['capabilities']['deadlock'])
        self.assertFalse(r['summary']['deadlock'])
        self.assertEqual(locks.format_text(r), 'LATEST DETECTED DEADLOCK\nNone recorded since the server started.\n')

    def test_supremum_record_is_not_read_as_an_integer(self):
        text = fixture('mysql-26.7.0-gap-deadlock-status.txt').replace(
            'Record lock, heap no 3 PHYSICAL RECORD: n_fields 4; compact format; info bits 0\n'
            ' 0: len 4; hex 80000014; asc     ;;',
            'Record lock, heap no 1 PHYSICAL RECORD: n_fields 1; compact format; info bits 0\n'
            ' 0: len 8; hex 73757072656d756d; asc supremum;;', 1)
        record = locks.parse_latest_deadlock(text, 'mysql')['transactions'][0]['holds'][0]['records'][0]
        self.assertEqual((record['heap_no'], record['supremum'], record['as_signed_int']), (1, True, None))

    def test_deadlock_text_output(self):
        r = self.report(empty_mariadb(status=status_row(fixture('mariadb-11.4.13-innodb-status.txt'))),
                        deadlock_only=True)
        text = locks.format_text(r)
        self.assertTrue(text.startswith('LATEST DETECTED DEADLOCK\nat 2026-10-12 21:31:12; rolled back transaction (1)\n'))
        self.assertIn('(1) trx 37, connection 5 [VICTIM]: UPDATE d SET v=1 WHERE id=2', text)
        self.assertIn('    holds   X,REC_NOT_GAP (record only) on lk.d index PRIMARY', text)
        self.assertIn('*** CONFLICTING WITH:', text)
        self.assertNotIn('BLOCKING TREE', text)


# ---- 8. LOCK_DATA ---------------------------------------------------------------------------------

class TestLockData(LockCase):
    def setUp(self):
        rows = [
            dl('9:1063', '1913', 63, None, 'TABLE', 'IX', 'GRANTED', None),
            # A record lock whose page is not in the buffer pool: LOCK_DATA is NULL.
            dl('9:6:4:2', '1913', 63, 'PRIMARY', 'RECORD', 'X,REC_NOT_GAP', 'GRANTED', None),
            ['9:6:5:2', '1913', 63, 'lk', 't', None, 'k_long', 'RECORD', 'X', 'GRANTED', 'v' * 600, 600],
            ['9:6:5:3', '1913', 63, 'lk', 't', None, 'k_long', 'RECORD', 'X', 'GRANTED', 'w' * 512, 512],
            ['9:6:5:4', '1913', 63, 'lk', 't', None, 'k_huge', 'RECORD', 'X', 'GRANTED', 'h' * 20, 8188],
            ['9:6:5:5', '1913', 63, 'lk', 't', None, 'k_huge', 'RECORD', 'X', 'GRANTED', 'g' * 20, 8187],
            dl('9:6:4:1', '1913', 63, 'PRIMARY', 'RECORD', 'X', 'GRANTED', 'supremum pseudo-record'),
        ]
        self.r = self.report(empty_mysql(locks=rows))
        self.locks = by_lock_id(self.r)

    def test_null_lock_data(self):
        table = self.locks['9:1063']
        self.assertEqual((table['lock_data'], table['lock_data_missing'], table['lock_data_truncated']),
                         (None, False, False))
        record = self.locks['9:6:4:2']
        self.assertEqual((record['lock_data'], record['lock_data_missing']), (None, True))
        self.assertTrue(has_note(self.r, 'LOCK_DATA is empty for a record lock when its page is not in the '
                                         'buffer pool'))

    def test_long_lock_data_is_cut_and_flagged(self):
        long = self.locks['9:6:5:2']
        self.assertEqual(len(long['lock_data']), locks.LOCK_DATA_CAP)
        self.assertTrue(long['lock_data'].endswith('…'))
        self.assertTrue(long['lock_data_truncated'])
        exact = self.locks['9:6:5:3']
        self.assertEqual((exact['lock_data'], exact['lock_data_truncated']), ('w' * 512, False))
        # The server cuts LOCK_DATA at 8 KB; a LENGTH at the server cap flags it.
        self.assertTrue(self.locks['9:6:5:4']['lock_data_truncated'])
        self.assertFalse(self.locks['9:6:5:5']['lock_data_truncated'])
        self.assertTrue(has_note(self.r, 'Long LOCK_DATA values are cut: the server keeps 8 KB and myflames '
                                         'shows 512 characters.'))

    def test_supremum_explanation(self):
        supremum = self.locks['9:6:4:1']
        self.assertEqual(supremum['mode_label'], 'next-key')
        self.assertIn('supremum', supremum['explain'])
        self.assertNotIn('supremum', self.locks['9:6:5:3']['explain'])


class TestExplainLockMode(unittest.TestCase):
    def test_mysql_record_modes(self):
        cases = {
            'X,REC_NOT_GAP': ('record only', '(exclusive)'),
            'S,REC_NOT_GAP': ('record only', '(shared)'),
            'X,GAP': ('gap', 'Gap locks never conflict with each other'),
            'X,GAP,INSERT_INTENTION': ('insert intention', 'inserts into the same gap do not block'),
            'X': ('next-key', 'and the gap before it'),
            'S': ('next-key', '(shared)'),
            'S,PRDT_PAGE': ('predicate', 'spatial'),
            ' x , rec_not_gap ': ('record only', '(exclusive)'),
        }
        for mode, (label, phrase) in cases.items():
            with self.subTest(mode=mode):
                got_label, why = locks.explain_lock_mode('mysql', 'RECORD', mode)
                self.assertEqual(got_label, label)
                self.assertIn(phrase, why)

    def test_table_modes(self):
        for engine in ('mysql', 'mariadb'):
            self.assertEqual(locks.explain_lock_mode(engine, 'TABLE', 'IX')[0], 'intention exclusive')
            self.assertEqual(locks.explain_lock_mode(engine, 'TABLE', 'IS')[0], 'intention shared')
            self.assertEqual(locks.explain_lock_mode(engine, 'table', 'AUTO_INC')[0], 'AUTO-INC')
            self.assertIn('released when the inserting statement ends',
                          locks.explain_lock_mode(engine, 'TABLE', 'AUTO_INC')[1])

    def test_mariadb_modes_name_both_possibilities(self):
        label, why = locks.explain_lock_mode('mariadb', 'RECORD', 'X')
        self.assertEqual(label, 'record or next-key')
        self.assertIn('record-only and next-key locks both as X', why)
        label, why = locks.explain_lock_mode('mariadb', 'RECORD', 'X,GAP')
        self.assertEqual(label, 'gap or insert intention')
        self.assertIn('both as X,GAP', why)

    def test_supremum(self):
        for engine, mode in (('mysql', 'X'), ('mysql', 'X,GAP'), ('mariadb', 'X'), ('mariadb', 'X,GAP')):
            with self.subTest(engine=engine, mode=mode):
                why = locks.explain_lock_mode(engine, 'RECORD', mode, 'supremum pseudo-record')[1]
                self.assertIn('supremum', why)
                self.assertIn('end of the range', why)


# ---- 9. limits and truncation -------------------------------------------------------------------

class TestLimits(LockCase):
    def test_bad_limits_raise_before_any_sql(self):
        for bad in (0, 5001, -1, True, False, '5', 5.0, None):
            with self.subTest(limit=bad):
                conn = FakeConn('8.4.6')
                with self.assertRaises(ValueError):
                    locks.lock_report(conn, limit=bad)
                self.assertEqual(conn.calls, [])
        conn = FakeConn('8.4.6')
        with self.assertRaises(ValueError):
            locks.lock_report(conn, waits_only=True, deadlock_only=True)
        self.assertEqual(conn.calls, [])

    def test_limit_boundaries_are_accepted(self):
        for limit in (1, locks.MAX_LIMIT):
            conn = empty_mysql()
            r = self.report(conn, limit=limit)
            self.assertEqual(r['limit'], limit)
            self.assertIn('LIMIT %d' % (limit + 1), conn.sql('FROM information_schema.INNODB_TRX')[0])

    def mdl_pages(self, first_page, contested):
        def answer(sql):
            if 'm.OBJECT_TYPE = CONVERT(' in sql:
                return batch(contested)
            return batch(first_page)
        return answer

    def test_truncation_flags_and_notes(self):
        first_page = [mdl('TABLE', 'lk', 'm', 'EXCLUSIVE', 'PENDING', 68, 28),
                      mdl('TABLE', 'lk', 'm', 'SHARED_READ', 'PENDING', 69, 29),
                      mdl('TABLE', 'lk', 'm', 'SHARED_READ', 'GRANTED', 66, 26)]
        contested = [mdl('TABLE', 'lk', 'm', 'SHARED_READ', 'GRANTED', 66, 26),
                     mdl('TABLE', 'lk', 'm', 'SHARED_UPGRADABLE', 'GRANTED', 68, 28)]
        conn = mysql_conn(trx=MYSQL_TRX[:3], waits=MYSQL_WAITS, locks=MYSQL_DATA_LOCKS[:3],
                          mdl=self.mdl_pages(first_page, contested), status=status_row(NO_DEADLOCK))
        r = self.report(conn, limit=2)
        self.assertEqual(len(r['transactions']), 2)
        self.assertEqual(len(r['waits']), 2)
        self.assertTrue(r['row_locks_truncated'])
        self.assertTrue(r['waits_truncated'])
        self.assertTrue(r['metadata_locks']['truncated'])
        for what in ('transactions', 'row locks', 'lock waits', 'metadata locks'):
            self.assertIn('Showing the first 2 %s; raise the limit to see more.' % what, r['notes'])
        # Every lock taking part in a shown wait is listed, even past the row limit.
        ids = {lock['lock_id'] for lock in r['row_locks']}
        self.assertEqual(ids, {T_23[0], PK1_23[0], PK1_24[0], PK2_27[0], PK2_24[0]})
        for needle in ('FROM information_schema.INNODB_TRX', 'FROM performance_schema.data_lock_waits',
                       FULL_LOCK_LIST):
            self.assertIn('LIMIT 3', conn.sql(needle)[0])
        first, second = conn.sql('FROM performance_schema.metadata_locks')
        self.assertIn('LIMIT 3', first)
        # The granted locks on the contested object come from a second, bounded query.
        self.assertIn("m.OBJECT_TYPE = CONVERT(X'5441424c45' USING utf8mb4)", second)
        self.assertIn("m.OBJECT_SCHEMA <=> CONVERT(X'6c6b' USING utf8mb4)", second)
        self.assertIn("m.OBJECT_NAME <=> CONVERT(X'6d' USING utf8mb4)", second)
        self.assertTrue(second.endswith('LIMIT 8'))
        self.assertEqual(mdl_pairs(r), {(28, 'X'): ([('granted', 26, 'SR')], True),
                                        (29, 'SR'): ([('queued', 28, 'X')], True)})
        self.assertEqual(len(r['metadata_locks']['locks']), 4)

    def test_session_lookup_in_lists_are_bounded(self):
        first_page = [mdl('TABLE', 'lk', 'm', 'EXCLUSIVE', 'PENDING', 68, 28),
                      mdl('TABLE', 'lk', 'm', 'SHARED_READ', 'PENDING', 69, 29)]
        contested = [mdl('TABLE', 'lk', 'm', 'SHARED_READ', 'GRANTED', 66, 26),
                     mdl('TABLE', 'lk', 'm', 'SHARED_UPGRADABLE', 'GRANTED', 68, 28)]
        conn = mysql_conn(trx=MYSQL_TRX[:2], waits=MYSQL_WAITS[:2], locks=MYSQL_DATA_LOCKS[:2],
                          mdl=self.mdl_pages(first_page, contested), status=status_row(NO_DEADLOCK))
        self.report(conn, limit=1)
        threads = lookup_sql(conn, 'performance_schema.threads')[0]
        # limit 1: at most 2 connection ids and 2 thread ids per lookup.
        self.assertEqual(ids_in(threads, 'PROCESSLIST_ID'), [23, 28])
        self.assertLessEqual(len(ids_in(threads, 'THREAD_ID')), 2)
        for sql in conn.sql('FROM performance_schema.events_statements_current'):
            self.assertLessEqual(len(ids_in(sql, 'PROCESSLIST_ID')), 1)


# ---- 10. CLI --------------------------------------------------------------------------------------

class TestLocksCLI(LockCase):
    def call(self, conn, *args):
        factory = FakeFactory(conn)
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch('myflames.connector.MySQLConnection', factory), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                workspace_cli.cmd_locks(list(args))
            except SystemExit as exc:
                code = exc.code
        if conn.calls:
            LockCase.assert_read_only(self, conn)
        return code, out.getvalue(), err.getvalue(), factory

    def test_json_shape_and_connection_flags(self):
        code, out, err, factory = self.call(mysql_conn(), '-h', 'db', '-P', '3307', '-u', 'app',
                                            '-p' + PASSWORD, '--json', '--limit', '50')
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        for key in ('schema_version', 'engine', 'server_version', 'captured_at', 'scope', 'limit',
                    'capabilities', 'notes', 'summary', 'blocking_tree', 'transactions', 'row_locks',
                    'waits', 'metadata_locks', 'deadlock'):
            self.assertIn(key, data)
        self.assertEqual(set(data['metadata_locks']), {'locks', 'waits', 'truncated', 'blocker_groups',
                                                     'total_edges', 'totals_scope', 'materialized_edges', 'edge_limit', 'waits_truncated'})
        self.assertEqual((data['schema_version'], data['scope'], data['limit']), ('locks-1.1', 'all', 50))
        self.assertEqual(data['deadlock']['victim_trx_id'], '1909')
        self.assertEqual((factory.options['host'], factory.options['port'], factory.options['user'],
                          factory.options['password']), ('db', 3307, 'app', PASSWORD))
        self.assertNotIn(PASSWORD, out)
        # Notes travel inside the JSON; stderr stays empty for machine callers.
        self.assertIn(locks.KILL_NOTE, data['notes'])
        self.assertEqual(err, '')
        self.assertIn('LIMIT 51', factory.conn.sql('FROM information_schema.INNODB_TRX')[0])

    def test_human_output_order_and_notes(self):
        code, out, err, _ = self.call(mysql_conn())
        self.assertEqual(code, 0, err)
        positions = [out.index(h) for h in ('DDL WAITING FOR A METADATA LOCK', 'BLOCKING TREE', 'TRANSACTIONS (5)',
                                            'ROW LOCKS (12)', 'METADATA LOCK WAITS (2)', 'LATEST DETECTED DEADLOCK')]
        self.assertEqual(positions, sorted(positions))
        self.assertTrue(out.startswith('DDL WAITING FOR A METADATA LOCK\nConnection 28 (dba@localhost) ALTER TABLE '
                                       'on lk.m waits ~15s for EXCLUSIVE: ALTER TABLE m ADD COLUMN c INT\n'), out)
        self.assertIn('\nBLOCKING TREE\nConnection 23 (app@localhost, lk) ROOT BLOCKER, blocks 3\n', out)
        for line in ('   idle in transaction for 5m00s (command Sleep)',
                     '   last statement: SELECT * FROM t WHERE k >= 60 FOR UPDATE',
                     '   to end it yourself: KILL 23',
                     '└─ connection 24 (app@localhost, lk) waits 1m00s for row lock X,REC_NOT_GAP (record only) '
                     'on lk.t index PRIMARY [1]',
                     '  └─ connection 27 (app@localhost, lk) waits 20s for row lock',
                     '└─ connection 25 (app@localhost, lk) waits 40s for row lock X,GAP,INSERT_INTENTION '
                     '(insert intention) on lk.t index k [60, 6]',
                     'Connection 26 (report@10.0.0.7, lk) ROOT BLOCKER, blocks 2',
                     '└─ connection 28 (dba@localhost, lk) waits ~15s for metadata EXCLUSIVE on TABLE lk.m',
                     'connection 29 waits 10s for SR on TABLE lk.m; blocked by X by connection 28 (queued)',
                     'at 2026-10-12 21:30:37; rolled back transaction (2)'):
            self.assertIn(line, out)
        # The raw status text appears only in --deadlock mode.
        self.assertNotIn('*** WE ROLL BACK TRANSACTION', out)
        self.assertIn('note: ' + locks.KILL_NOTE, err)

    def test_quiet_silences_notes(self):
        code, out, err, _ = self.call(mysql_conn(), '-q')
        self.assertEqual((code, err), (0, ''))
        self.assertIn('BLOCKING TREE', out)

    def test_output_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'locks.txt')
            code, out, err, _ = self.call(mysql_conn(), '-o', path)
            self.assertEqual((code, out), (0, ''))
            with open(path, encoding='utf-8') as handle:
                self.assertIn('\nBLOCKING TREE\n', handle.read())
            self.assertIn('Written to %s' % path, err)
            json_path = os.path.join(tmp, 'locks.json')
            code, out, err, _ = self.call(mysql_conn(), '--json', '-o', json_path, '-q')
            self.assertEqual((code, out, err), (0, '', ''))
            with open(json_path, encoding='utf-8') as handle:
                self.assertEqual(json.load(handle)['schema_version'], 'locks-1.1')

    def test_waits_only_skips_the_full_lock_list_and_the_deadlock(self):
        extra = ['1920', 'RUNNING', '2026-10-12 21:29:50', 10, None, None, None, 30, None, None,
                 0, 0, 1, 0, 0, 'REPEATABLE READ', 0]
        conn = mysql_conn(trx=MYSQL_TRX + [extra])
        code, out, err, _ = self.call(conn, '--waits-only', '--json')
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data['scope'], 'waits')
        self.assertEqual(conn.sql(FULL_LOCK_LIST), [])
        self.assertEqual(conn.sql('SHOW ENGINE'), [])
        self.assertEqual(len(conn.sql('FROM performance_schema.data_lock_waits')), 1)
        self.assertIsNone(data['deadlock'])
        self.assertEqual((data['capabilities']['row_locks'], data['capabilities']['deadlock']), ('none', False))
        self.assertEqual({lock['lock_id'] for lock in data['row_locks']},
                         {PK1_24[0], PK1_23[0], PK2_27[0], PK2_24[0], K60_25[0], K60_23[0]})
        # Only sessions in a wait; the uninvolved transaction 1920 is left out.
        self.assertEqual([t['trx_id'] for t in data['transactions']],
                         ['1913', '562947965813544', '1914', '1915', '1916'])
        self.assertEqual({(m['schema'], m['name']) for m in data['metadata_locks']['locks']}, {('lk', 'm')})
        self.assertEqual(data['summary']['root_blockers'], 2)
        self.assertNotIn(locks.SNAPSHOT_NOTE, data['notes'])
        code, out, _, _ = self.call(mysql_conn(), '--waits-only', '-q')
        self.assertIn('BLOCKING TREE', out)
        self.assertNotIn('LATEST DETECTED DEADLOCK', out)

    def test_deadlock_only_reads_version_identity_and_status(self):
        conn = mysql_conn('8.0.35')
        code, out, err, _ = self.call(conn, '--deadlock', '--json')
        self.assertEqual(code, 0, err)
        self.assertEqual([sql for sql, _ in conn.calls],
                         [DISCOVER_TAG + VERSION_SQL, LOCKS_TAG + 'SHOW ENGINE INNODB STATUS'])
        data = json.loads(out)
        self.assertEqual(data['scope'], 'deadlock')
        self.assertEqual(data['deadlock']['victim_trx_id'], '1909')
        self.assertEqual((data['blocking_tree'], data['transactions'], data['row_locks']), ([], [], []))
        self.assertFalse(any('global lock-system latch' in n for n in data['notes']))
        code, out, err, _ = self.call(mysql_conn(), '--deadlock')
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith('LATEST DETECTED DEADLOCK\nat 2026-10-12 21:30:37; '
                                       'rolled back transaction (2)\n'))
        self.assertIn('(2) trx 1909, connection 22 [VICTIM]: UPDATE d SET v=1 WHERE id=2', out)
        self.assertIn('*** WE ROLL BACK TRANSACTION (2)', out)
        self.assertNotIn('BLOCKING TREE', out)

    def test_bad_arguments_exit_2_without_connecting(self):
        for argv in (['--waits-only', '--deadlock'], ['--limit', '0'], ['--limit', '5001'], ['--limit', 'x']):
            with self.subTest(argv=argv):
                conn = mysql_conn()
                code, out, _, factory = self.call(conn, *argv)
                self.assertEqual((code, out), (2, ''))
                self.assertIsNone(factory.options)
                self.assertEqual(conn.calls, [])

    def test_failures_exit_2_and_redact_the_password(self):
        leaking = FakeConn('8.4.6', [('@@version', ConnectorError(
            "mysql client exited 1: ERROR 1045 (28000): Access denied for user 'app' " + PASSWORD))])
        code, out, err, _ = self.call(leaking, '-u', 'app', '-p' + PASSWORD)
        self.assertEqual((code, out), (2, ''))
        self.assertNotIn(PASSWORD, err)
        self.assertIn('[redacted]', err)
        lost = mysql_conn(trx=ConnectorError('mysql client exited 1: ERROR 2013 (HY000): Lost connection '
                                             'to MySQL server during query'))
        code, out, err, _ = self.call(lost, '-p' + PASSWORD)
        self.assertEqual((code, out), (2, ''))
        self.assertIn('Could not read information_schema.INNODB_TRX: ERROR 2013', err)

    def test_root_help_lists_locks_and_dispatch_reaches_cmd_locks(self):
        from myflames import cli

        def run(argv, factory=None):
            out, err = io.StringIO(), io.StringIO()
            code = 0
            patches = [mock.patch.object(sys, 'argv', argv)]
            if factory is not None:
                patches.append(mock.patch('myflames.connector.MySQLConnection', factory))
            with contextlib.ExitStack() as stack:
                for patch in patches:
                    stack.enter_context(patch)
                stack.enter_context(contextlib.redirect_stdout(out))
                stack.enter_context(contextlib.redirect_stderr(err))
                try:
                    cli.main()
                except SystemExit as exc:
                    code = exc.code
            return code, out.getvalue(), err.getvalue()

        code, out, _ = run(['myflames', '--help'])
        self.assertEqual(code, 0)
        self.assertIn('locks     Blocking tree, waiting DDL, transactions, row/metadata locks, deadlock; --kill ID', out)
        self.assertIn('myflames locks -h localhost -u admin -p --kill 42      # end one blocker after you confirm', out)
        self.assertIn('myflames locks -h localhost -u admin -p --waits-only', out)
        code, out, _ = run(['myflames', 'locks', '--help'])
        self.assertEqual(code, 0)
        self.assertIn('myflames locks', out)
        self.assertIn('--waits-only', out)
        self.assertIn('--deadlock', out)
        conn = empty_mysql()
        code, out, err = run(['myflames', 'locks', '--json', '-q'], FakeFactory(conn))
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(json.loads(out)['schema_version'], 'locks-1.1')
        self.assertTrue(conn.calls)


# ---- 11. UI endpoint ------------------------------------------------------------------------------

class TestLocksAPI(LockCase):
    def call(self, conn, payload):
        factory = FakeFactory(conn)
        with mock.patch('myflames.connector.MySQLConnection', factory):
            result = ui.api_response('/api/live/locks', payload, workspace=object())
        self.assert_read_only(conn)
        return result, factory

    def connection(self):
        return {'host': '127.0.0.1', 'port': 3306, 'user': 'app', 'password': PASSWORD}

    def test_returns_the_report_with_limit_and_waits_only(self):
        conn = mysql_conn()
        result, factory = self.call(conn, {'connection': self.connection(), 'limit': 25, 'waits_only': True})
        self.assertEqual((result['schema_version'], result['limit'], result['scope']), ('locks-1.1', 25, 'waits'))
        self.assertIn('LIMIT 26', conn.sql('FROM information_schema.INNODB_TRX')[0])
        self.assertEqual(conn.sql(FULL_LOCK_LIST), [])
        self.assertEqual(factory.options['password'], PASSWORD)
        json.dumps(result, allow_nan=False)

    def test_defaults_and_strict_waits_only(self):
        for payload in ({}, {'waits_only': False}, {'waits_only': 'true'}, {'waits_only': 1}):
            with self.subTest(payload=payload):
                payload = dict(payload, connection=self.connection())
                result, _ = self.call(mysql_conn(), payload)
                self.assertEqual((result['scope'], result['limit']), ('all', 500))

    def test_bad_limit_is_rejected_before_any_sql(self):
        for limit in (0, 5001, '5', True):
            with self.subTest(limit=limit):
                conn = mysql_conn()
                with mock.patch('myflames.connector.MySQLConnection', FakeFactory(conn)):
                    with self.assertRaises(ValueError):
                        ui.api_response('/api/live/locks', {'connection': self.connection(), 'limit': limit},
                                        workspace=object())
                self.assertEqual(conn.calls, [])

    def test_needs_a_workspace_and_connection(self):
        with self.assertRaisesRegex(ValueError, 'requires a running workspace'):
            ui.api_response('/api/live/locks', {'connection': self.connection()})
        with self.assertRaisesRegex(ValueError, 'connection settings'):
            ui.api_response('/api/live/locks', {}, workspace=object())

    def test_errors_never_echo_the_password(self):
        failures = [('@@version', ValueError('oops ' + PASSWORD)),
                    ('@@version', ConnectorError('denied ' + PASSWORD)),
                    ('INNODB_TRX', ConnectorError('mysql client exited 1: ERROR 2013 (HY000): lost ' + PASSWORD))]
        for needle, error in failures:
            with self.subTest(error=error):
                responses = [] if needle == '@@version' else [(VERSION_SQL, batch([['8.4.6', 1]]))]
                conn = FakeConn('8.4.6', responses + [('SELECT @@server_uuid', batch([['server-uuid-1']])), (needle, error)])
                with mock.patch('myflames.connector.MySQLConnection', FakeFactory(conn)):
                    with self.assertRaises((ValueError, ConnectorError)) as caught:
                        ui.api_response('/api/live/locks', {'connection': self.connection()}, workspace=object())
                self.assertNotIn(PASSWORD, str(caught.exception))
                self.assertIn('[redacted]', str(caught.exception))

    def test_route_is_allowlisted_and_takes_saved_profiles(self):
        self.assertIn('"/api/live/locks"', inspect.getsource(ui.WorkspaceHandler.do_POST))
        self.assertIn('/api/live/locks', ui._CONNECTION_PATHS)


# ---- parser robustness ---------------------------------------------------------------------------

class TestParserRobustness(LockCase):
    """Regressions: CRLF statement text and the deadlock phrase inside a statement."""

    def test_carriage_return_in_a_statement_keeps_the_row_whole(self):
        # mysql --batch escapes only \0, \t, \n and \\ (client/mysql.cc
        # tee_write; same in MariaDB), so a statement sent with CRLF line endings
        # arrives with a raw '\r'. str.splitlines() would split the row there;
        # locks._q splits on '\n' only.
        query = 'UPDATE t\r\nSET v=1 WHERE id=1'
        row = list(MYSQL_TRX[2])
        row[8] = query
        r = self.report(empty_mysql(trx=[row]))
        self.assertEqual([t['trx_id'] for t in r['transactions']], ['1914'])
        self.assertEqual(r['transactions'][0]['query'], query)
        self.assertEqual(r['transactions'][0]['isolation_level'], 'REPEATABLE READ')

    def test_carriage_return_in_a_deadlock_statement_keeps_the_status_whole(self):
        # Same root cause: SHOW ENGINE INNODB STATUS prints the statement text
        # verbatim, so a CRLF statement cuts the status column at the '\r' and the
        # second transaction and the victim are lost.
        text = fixture('mysql-8.4.11-innodb-status.txt').replace('UPDATE d SET v=2 WHERE id=1\n',
                                                                'UPDATE d\r\nSET v=2 WHERE id=1\n')
        r = self.report(empty_mysql(status=status_row(text)), deadlock_only=True)
        self.assertEqual(len(r['deadlock']['transactions']), 2)
        self.assertEqual(r['deadlock']['victim_trx_id'], '1909')
        self.assertIn('SET v=2 WHERE id=1', r['deadlock']['transactions'][0]['statement'])

    def test_deadlock_phrase_inside_a_statement_is_not_a_deadlock(self):
        # The section is found by its dashed header, so a statement in the
        # TRANSACTIONS section that mentions the phrase is not a deadlock.
        text = NO_DEADLOCK + ('\n---TRANSACTION 1925, ACTIVE 3 sec\nmysql tables in use 1, locked 0\n'
                              'MySQL thread id 40, OS thread handle 1, query id 9 localhost root executing\n'
                              "SELECT * FROM audit WHERE note LIKE '%LATEST DETECTED DEADLOCK%'\n")
        self.assertIsNone(locks.parse_latest_deadlock(text, 'mysql'))


class TestStatusTextVariants(LockCase):
    """Variants of SHOW ENGINE INNODB STATUS text, from the print code in both servers."""

    def deadlock(self, thread_line, header='TRANSACTION 1910, ACTIVE 1 sec starting index read',
                 index='PRIMARY', field=' 0: len 4; hex 80000002; asc     ;;'):
        text = ('------------------------\nLATEST DETECTED DEADLOCK\n------------------------\n'
                '2026-10-12 21:30:37 281472292351744\n*** (1) TRANSACTION:\n' + header + '\n'
                'LOCK WAIT 3 lock struct(s), heap size 1128, 2 row lock(s)\n' + thread_line + '\n'
                'UPDATE d SET v=2 WHERE id=1\n\n*** (1) HOLDS THE LOCK(S):\n'
                'RECORD LOCKS space id 6 page no 4 n bits 72 index ' + index + ' of table `lk`.`d` '
                'trx id 1910 lock_mode X locks rec but not gap\n'
                'Record lock, heap no 3 PHYSICAL RECORD: n_fields 4; compact format; info bits 0\n'
                + field + '\n\n*** WE ROLL BACK TRANSACTION (1)\n------------\nTRANSACTIONS\n------------\n')
        return locks.parse_latest_deadlock(text, 'mysql')

    def test_tcp_client_ip_is_not_taken_for_the_user(self):
        # host, then the IP for TCP clients, then user, then state.
        t = self.deadlock('MySQL thread id 9, OS thread handle 1, query id 3 app-1 172.17.0.5 app updating')
        t = t['transactions'][0]
        self.assertEqual((t['host'], t['user'], t['state']), ('app-1', 'app', 'updating'))
        t = self.deadlock('MariaDB thread id 9, OS thread handle 1, query id 3 localhost root')['transactions'][0]
        self.assertEqual((t['host'], t['user'], t['state']), ('localhost', 'root', None))
        t = self.deadlock('MySQL thread id 9, OS thread handle 1, query id 3 ::1 ::1 cafe Sending data')
        t = t['transactions'][0]
        self.assertEqual((t['host'], t['user'], t['state']), ('::1', 'cafe', 'Sending data'))

    def test_unnumbered_and_prepared_transactions(self):
        # MariaDB prints the trx pointer when the id is 0; XA PREPARED adds "(PREPARED)".
        t = self.deadlock('MariaDB thread id 9, OS thread handle 1, query id 3 localhost root',
                          header='TRANSACTION (0xffff8f933900), ACTIVE (PREPARED) 7 sec')['transactions'][0]
        self.assertEqual((t['trx_id'], t['active_s'], t['connection_id']), ('(0xffff8f933900)', 7, 9))

    def test_index_names_with_spaces_long_and_null_fields(self):
        line = 'MySQL thread id 9, OS thread handle 1, query id 3 localhost root updating'
        lock = self.deadlock(line, index='my key')['transactions'][0]['holds'][0]
        self.assertEqual((lock['index'], lock['table'], lock['mode']), ('my key', 'd', 'X,REC_NOT_GAP'))
        for field, expected in ((' 0: len 100; hex 6161; asc aa; (total 100 bytes);', '6161'),
                                (' 0: SQL NULL;', None), (' 0: SQL NULL, size 4 ;', None),
                                (' 0: SQL DEFAULT;', None)):
            record = self.deadlock(line, field=field)['transactions'][0]['holds'][0]['records'][0]
            self.assertEqual(record['fields'][0]['hex'], expected, field)
            self.assertFalse(record['supremum'])

    def test_last_statement_needs_the_thread_instrumentation_consumer(self):
        setup = [row for row in SETUP_ON] + [['thread_instrumentation', 'NO']]
        r = self.report(mysql_conn(setup=setup))
        self.assertFalse(r['capabilities']['last_statement'])
        self.assertTrue(has_note(r, 'thread_instrumentation'))
        self.assertTrue(all(n['last_statement'] is None for n in r['blocking_tree']))



# ---- DDL waiting for a metadata lock -------------------------------------------------------------

class TestDDLStatement(unittest.TestCase):
    def test_kinds_and_tables(self):
        cases = {
            'ALTER TABLE t ADD COLUMN c INT': ('ALTER TABLE', None, 't'),
            '/* app */ alter online table `lk`.`my``t` force': ('ALTER TABLE', 'lk', 'my`t'),
            '-- note\nALTER IGNORE TABLE IF EXISTS lk . t ENGINE=InnoDB': ('ALTER TABLE', 'lk', 't'),
            'CREATE UNIQUE INDEX k ON lk.t (a)': ('CREATE INDEX', 'lk', 't'),
            'CREATE INDEX IF NOT EXISTS k USING BTREE ON t (a)': ('CREATE INDEX', None, 't'),
            'DROP INDEX k ON `t`': ('DROP INDEX', None, 't'),
            'DROP TEMPORARY TABLE IF EXISTS a, b': ('DROP TABLE', None, 'a'),
            'RENAME TABLE t TO t_old, t_new TO t': ('RENAME TABLE', None, 't'),
            'TRUNCATE t': ('TRUNCATE TABLE', None, 't'),
            'truncate table lk.t': ('TRUNCATE TABLE', 'lk', 't'),
            'OPTIMIZE NO_WRITE_TO_BINLOG TABLE t': ('OPTIMIZE TABLE', None, 't'),
            'CREATE DEFINER=`root`@`%` TRIGGER tr BEFORE INSERT ON lk.t FOR EACH ROW SET @x=1':
                ('CREATE TRIGGER', 'lk', 't'),
            'CREATE TABLE t2 AS SELECT * FROM t': ('CREATE TABLE', None, None),
            'DROP DATABASE lk': ('DROP DATABASE', None, None),
        }
        for sql, expected in cases.items():
            with self.subTest(sql=sql):
                self.assertEqual(locks.ddl_statement(sql), expected)
        for sql in (None, '', 'SELECT * FROM t', 'UPDATE t SET v=1', '/* ALTER TABLE t */ SELECT 1',
                    'INSERT INTO t VALUES (1)', 'LOCK TABLES t WRITE'):
            with self.subTest(sql=sql):
                self.assertIsNone(locks.ddl_statement(sql))

    def test_wait_states(self):
        for state in ('Waiting for table metadata lock', 'Waiting for schema metadata lock',
                      'Waiting for global read lock', 'Waiting for backup lock', 'Waiting for commit lock'):
            self.assertTrue(locks.is_mdl_wait_state(state), state)
        for state in (None, '', 'updating', 'Waiting for table flush', 'Sending data'):
            self.assertFalse(locks.is_mdl_wait_state(state), state)


def ddl_rows(threads, ids):
    """The metadata lock waiter query: the given sessions as threads rows."""
    return [row for row in threads if row[1] in ids]


class TestDDLBlockers(LockCase):
    def test_mysql_exact_blocker_is_the_idle_transaction_and_the_select_queues(self):
        conn = mysql_conn(ddl=ddl_rows(MYSQL_THREADS, (28, 29)))
        r = self.report(conn)
        self.assertEqual(len(r['ddl_waits']), 1)
        d = r['ddl_waits'][0]
        self.assertEqual({k: d[k] for k in ('connection_id', 'user', 'kind', 'schema', 'table', 'object_type',
                                            'requested', 'requested_short', 'precision', 'wait_s', 'state',
                                            'blockers_truncated')},
                         {'connection_id': 28, 'user': 'dba', 'kind': 'ALTER TABLE', 'schema': 'lk', 'table': 'm',
                          'object_type': 'TABLE', 'requested': 'EXCLUSIVE', 'requested_short': 'X',
                          'precision': 'exact', 'wait_s': 15.0, 'state': 'Waiting for table metadata lock',
                          'blockers_truncated': False})
        self.assertEqual([b['connection_id'] for b in d['blockers']], [26])
        b = d['blockers'][0]
        self.assertEqual({k: b[k] for k in ('reason', 'certain', 'locks', 'idle_in_transaction', 'culprit',
                                            'running', 'last_statement', 'statement', 'trx_id', 'trx_started',
                                            'trx_age_s', 'rows_modified', 'user', 'host', 'ps_thread_id',
                                            'kill_allowed', 'waits_for')},
                         {'reason': 'holds', 'certain': True,
                          'locks': [{'lock_type': 'SHARED_READ', 'short': 'SR', 'duration': 'TRANSACTION',
                                     'status': 'GRANTED'}],
                          'idle_in_transaction': True, 'culprit': True, 'running': False,
                          'last_statement': 'SELECT COUNT(*) FROM m', 'statement': None,
                          'trx_id': '562947965813544', 'trx_started': '2026-10-12 21:26:40', 'trx_age_s': 200,
                          'rows_modified': 0, 'user': 'report', 'host': '10.0.0.7', 'ps_thread_id': 66,
                          'kill_allowed': True, 'waits_for': []})
        self.assertEqual([(q['connection_id'], q['short'], q['certain'], q['statement']) for q in d['queued']],
                         [(29, 'SR', True, 'SELECT * FROM m')])
        self.assertIn(locks.DDL_QUEUE_NOTE, r['notes'])
        self.assertEqual(r['summary']['ddl_waits'], 1)
        # The metadata lock waiter query reads threads, bounded, excluding this session.
        sql = conn.sql("LIKE 'Waiting for %metadata lock'")[0]
        self.assertIn('FROM performance_schema.threads', sql)
        self.assertIn('PROCESSLIST_ID <> CONNECTION_ID()', sql)
        self.assertIn("PROCESSLIST_STATE IN ('Waiting for global read lock', 'Waiting for backup lock', "
                      "'Waiting for commit lock')", sql)
        self.assertIn('LIMIT 501', sql)
        text = locks.format_text(r)
        for line in ('DDL WAITING FOR A METADATA LOCK',
                     'Connection 28 (dba@localhost) ALTER TABLE on lk.m waits ~15s for EXCLUSIVE: '
                     'ALTER TABLE m ADD COLUMN c INT',
                     '  blocked by:',
                     '  - connection 26 (report@10.0.0.7), holds SHARED_READ (transaction), idle in transaction '
                     '3m20s, transaction open 3m20s, 0 rows changed',
                     '    usual culprit: a transaction left open after using the table',
                     '    last statement: SELECT COUNT(*) FROM m',
                     '    end it: myflames locks --kill 26',
                     '  queued behind it: connection 29 SELECT * FROM m (10s)'):
            self.assertIn(line, text)

    def test_found_from_the_pending_lock_when_the_process_list_query_misses_it(self):
        # The default fake answers the waiter query with no rows.
        r = self.report(mysql_conn())
        self.assertEqual([(d['connection_id'], d['precision']) for d in r['ddl_waits']], [(28, 'exact')])
        self.assertEqual(r['ddl_waits'][0]['queued'][0]['connection_id'], 29)

    def test_chain_lists_the_root_blocker_behind_a_waiting_blocker(self):
        # 30's ALTER on lk.u waits for 24's SHARED_READ; 24 itself waits for 23's row lock.
        mdl_rows = MYSQL_MDL + [mdl('TABLE', 'lk', 'u', 'EXCLUSIVE', 'PENDING', 70, 30),
                                mdl('TABLE', 'lk', 'u', 'SHARED_UPGRADABLE', 'GRANTED', 70, 30),
                                mdl('TABLE', 'lk', 'u', 'SHARED_READ', 'GRANTED', 64, 24)]
        threads = MYSQL_THREADS + [[70, 30, 'dba', 'localhost', 'lk', 'Query', 9,
                                    'Waiting for table metadata lock', 'ALTER TABLE u ADD INDEX (a)']]
        r = self.report(mysql_conn(mdl=mdl_rows, threads=threads_answer(threads),
                                   ddl=ddl_rows(threads, (28, 29, 30))))
        d = {x['connection_id']: x for x in r['ddl_waits']}[30]
        self.assertEqual([(b['connection_id'], b['reason'], b['via'], b['culprit']) for b in d['blockers']],
                         [(24, 'holds', None, False), (23, 'chain', 24, True)])
        self.assertEqual(d['blockers'][0]['waits_for'], [23])
        self.assertTrue(d['blockers'][0]['running'])
        self.assertEqual(d['blockers'][1]['last_statement'], 'SELECT * FROM t WHERE k >= 60 FOR UPDATE')
        text = locks.format_text(r)
        self.assertIn('connection 24 (app@localhost), holds SHARED_READ (transaction), running 1m00s', text)
        self.assertIn('itself waits for connection 23', text)
        self.assertIn('connection 23 (app@localhost), blocks connection 24, which the DDL waits for, '
                      'idle in transaction 5m00s', text)
        self.assertIn('    usual culprit: an idle transaction at the start of the chain', text)
        # Oldest DDL first.
        self.assertEqual([x['connection_id'] for x in r['ddl_waits']], [28, 30])

    def test_no_connection_blocker_is_listed_once_per_thread_without_a_kill(self):
        # MySQL keeps a disconnected prepared XA transaction's locks under a thread with no session.
        mdl_rows = [mdl('TABLE', 'lk', 'm', 'EXCLUSIVE', 'PENDING', 68, 28),
                    mdl('TABLE', 'lk', 'm', 'SHARED_WRITE', 'GRANTED', 5, None),
                    mdl('TABLE', 'lk', 'm', 'SHARED_READ', 'GRANTED', 5, None)]
        r = self.report(mysql_conn(mdl=mdl_rows, trx=[], waits=[], locks=[]))
        blockers = r['ddl_waits'][0]['blockers']
        self.assertEqual(len(blockers), 1)
        b = blockers[0]
        self.assertEqual((b['connection_id'], b['ps_thread_id'], b['kill_allowed'], b['culprit'], b['xa_prepared']),
                         (None, 5, None, False, None))
        self.assertEqual([l['short'] for l in b['locks']], ['SW', 'SR'])
        text = locks.format_text(r)
        self.assertIn('a thread with no client connection (server thread 5), holds SHARED_WRITE, SHARED_READ', text)
        self.assertIn('XA RECOVER lists prepared XA transactions (it needs XA_RECOVER_ADMIN)', text)
        self.assertIn(locks.XA_NOTE, r['notes'])

    def test_mariadb_no_connection_blocker_is_a_background_thread(self):
        # MariaDB releases a detached XA transaction's metadata locks, so only server threads remain.
        mdl_rows = [mdl('TABLE', 'lk', 't', 'EXCLUSIVE', 'PENDING', 111, 11),
                    mdl('TABLE', 'lk', 't', 'SHARED_READ', 'GRANTED', 7, None)]
        threads = MA_THREADS + [[111, 11, 'dba', 'localhost', 'lk', 'Query', 12,
                                 'Waiting for table metadata lock', 'ALTER TABLE t ADD COLUMN c INT']]
        conn = ma_conn(ps_on=True, mdl=mdl_rows, threads=threads_answer(threads),
                       last=last_answer([[5, "XA PREPARE 'x'", 3]]))
        r = self.report(conn)
        self.assertIn('    It belongs to a background server thread.\n', locks.format_text(r))
        self.assertNotIn(locks.XA_NOTE, r['notes'])
        self.assertTrue(all(b['xa_prepared'] is None for d in r['ddl_waits'] for b in d['blockers']))

    def mariadb_ddl(self, **overrides):
        # 11's ALTER waits 12 s; 12's SELECT queued 5 s ago; 13 waited 20 s on another table
        # (before the ALTER); 14 opened a transaction 3 s ago and is idle.
        processlist = MA_PROCESSLIST + [
            [None, 11, 'dba', 'localhost:4001', 'lk', 'Query', '12.0000', 'Waiting for table metadata lock',
             'ALTER TABLE t ADD COLUMN c INT'],
            [None, 12, 'app', 'localhost:4002', 'lk', 'Query', '5.0000', 'Waiting for table metadata lock',
             'SELECT * FROM t'],
            [None, 13, 'app', 'localhost:4003', 'lk', 'Query', '20.0000', 'Waiting for table metadata lock',
             'SELECT * FROM other'],
            [None, 14, 'app', 'localhost:4004', 'lk', 'Sleep', '3.0000', '', None]]
        trx = MA_TRX + [['0', 'RUNNING', '2026-10-12 21:29:57', 3, None, None, None, 14, None, None,
                         0, 0, 0, 0, 0, 'REPEATABLE READ', 0]]
        answers = {'processlist': processlist_answer(processlist), 'trx': trx,
                   'ddl': [row for row in processlist if row[1] in (11, 12, 13)]}
        answers.update(overrides)
        return ma_conn(**answers)

    def test_mariadb_without_metadata_locks_lists_likely_blockers(self):
        conn = self.mariadb_ddl()
        r = self.report(conn)
        self.assertEqual(len(r['ddl_waits']), 1)
        d = r['ddl_waits'][0]
        self.assertEqual((d['connection_id'], d['precision'], d['schema'], d['table'], d['requested']),
                         (11, 'likely', 'lk', 't', None))
        self.assertEqual([(b['connection_id'], b['culprit'], b['idle_in_transaction'], b['started_before_ddl'])
                          for b in d['blockers']],
                         [(5, True, True, True), (8, True, True, True), (9, True, True, True),
                          (14, False, True, False), (6, False, False, True), (7, False, False, True),
                          (10, False, False, True)])
        self.assertTrue(all(b['reason'] == 'open_transaction' and not b['certain'] for b in d['blockers']))
        self.assertEqual(d['blockers'][0]['host'], 'localhost')
        # 12 queued after the ALTER and names t; 13 waited longer than the ALTER has.
        self.assertEqual([(q['connection_id'], q['certain']) for q in d['queued']], [(12, False)])
        self.assertTrue(has_note(r, 'MariaDB is not recording metadata locks here'))
        self.assertTrue(has_note(r, "INSTALL SONAME 'metadata_lock_info'"))
        self.assertTrue(has_note(r, 'myflames changes neither'))
        self.assertNotIn(locks.MARIADB_PS_OFF_MDL_NOTE, r['notes'])
        self.assertEqual(r['capabilities']['metadata_lock_info'], False)
        self.assertFalse(has_note(r, 'METADATA_LOCK_INFO lists granted'))
        sql = conn.sql("LIKE 'Waiting for %metadata lock'")[0]
        self.assertIn('FROM information_schema.PROCESSLIST', sql)
        self.assertIn('TIME_MS / 1000', sql)
        self.assertIn('ID <> CONNECTION_ID()', sql)
        self.assertEqual(len(conn.sql('FROM information_schema.METADATA_LOCK_INFO')), 1)
        text = locks.format_text(r)
        self.assertIn('  likely blockers (no metadata lock data; see notes):', text)
        self.assertIn('started before the DDL', text)
        self.assertIn('  queued behind it (likely): connection 12 SELECT * FROM t (5s)', text)

    def test_waits_only_keeps_the_transactions_of_likely_blockers(self):
        r = self.report(self.mariadb_ddl(), waits_only=True)
        ids = {t['connection_id'] for t in r['transactions']}
        self.assertTrue({5, 8, 9, 14}.issubset(ids), ids)

    def test_mariadb_metadata_lock_info_names_the_holders(self):
        mli = [[5, 'MDL_SHARED_READ', 'Table metadata lock', 'lk', 't'],
               [5, 'MDL_SHARED_WRITE', 'Table metadata lock', 'lk', 't'],
               [11, 'MDL_SHARED_UPGRADABLE', 'Table metadata lock', 'lk', 't'],
               [9, 'MDL_SHARED_READ', 'Table metadata lock', 'lk', 'other']]
        conn = self.mariadb_ddl(mli=mli)
        r = self.report(conn)
        d = r['ddl_waits'][0]
        self.assertEqual(d['precision'], 'granted_locks')
        self.assertEqual([b['connection_id'] for b in d['blockers']], [5])
        self.assertEqual([l['short'] for l in d['blockers'][0]['locks']], ['SR', 'SW'])
        self.assertTrue(d['blockers'][0]['culprit'])
        self.assertTrue(r['capabilities']['metadata_lock_info'])
        self.assertIn(locks.MLI_NOTE, r['notes'])
        self.assertFalse(has_note(r, 'MariaDB is not recording metadata locks here'))
        sql = conn.sql('FROM information_schema.METADATA_LOCK_INFO')[0]
        self.assertIn("LOCK_TYPE = 'Table metadata lock'", sql)
        self.assertIn('THREAD_ID <> CONNECTION_ID()', sql)
        self.assertIn('sessions holding a lock on the table:', locks.format_text(r))

    def test_metadata_lock_info_with_no_holder_falls_back_to_likely(self):
        r = self.report(self.mariadb_ddl(mli=[[11, 'MDL_SHARED_UPGRADABLE', 'Table metadata lock', 'lk', 't']]))
        self.assertEqual(r['ddl_waits'][0]['precision'], 'likely')

    def test_metadata_lock_info_errors_other_than_missing_are_noted(self):
        denied = ConnectorError('mysql client exited 1: ERROR 1227 (42000): Access denied')
        r = self.report(self.mariadb_ddl(mli=denied))
        self.assertFalse(r['capabilities']['metadata_lock_info'])
        self.assertTrue(has_note(r, 'Could not read information_schema.METADATA_LOCK_INFO'))

    def test_metadata_lock_info_is_not_read_without_a_waiting_ddl(self):
        conn = ma_conn()
        self.report(conn)
        self.assertEqual(conn.sql('METADATA_LOCK_INFO'), [])

    def test_mariadb_without_process_cannot_list_likely_blockers(self):
        processlist = [[None, 11, 'dba', 'localhost:4001', 'lk', 'Query', '12.0000',
                        'Waiting for table metadata lock', 'ALTER TABLE t ADD COLUMN c INT']]
        conn = ma_conn(trx=process_denied(), ma_waits=process_denied(), ma_locks=process_denied(),
                       status=process_denied(), processlist=processlist_answer(processlist), ddl=processlist)
        r = self.report(conn)
        self.assertEqual((r['ddl_waits'][0]['precision'], r['ddl_waits'][0]['blockers']), ('likely', []))
        self.assertTrue(has_note(r, 'Without PROCESS, open transactions are hidden'))
        self.assertIn('  no blocker found (see notes)', locks.format_text(r))

    def test_mysql_with_the_instrument_disabled_falls_back_to_likely(self):
        setup = [['instrument', 'NO'], ['global_instrumentation', 'YES'], ['events_statements_current', 'YES']]
        r = self.report(mysql_conn(setup=setup, ddl=ddl_rows(MYSQL_THREADS, (28, 29))))
        d = r['ddl_waits'][0]
        self.assertEqual(d['precision'], 'likely')
        self.assertEqual(d['blockers'][0]['connection_id'], 23)
        self.assertEqual([q['connection_id'] for q in d['queued']], [29])
        self.assertTrue(has_note(r, 'Performance Schema is not recording metadata locks here'))
        self.assertTrue(has_note(r, 'MySQL enables both by default'))

    def test_prepared_xa_blocker_gets_xa_guidance_instead_of_kill(self):
        # Connection 26's last statement was XA PREPARE: ending it would release nothing (MySQL).
        last = [[26, "XA PREPARE 'mf1'", 40], [23, 'SELECT * FROM t WHERE k >= 60 FOR UPDATE', 95]]
        conn = mysql_conn(ddl=ddl_rows(MYSQL_THREADS, (28, 29)), last=last_answer(last))
        r = self.report(conn)
        expected = {'xid': "'mf1'", 'commit': "XA COMMIT 'mf1'", 'rollback': "XA ROLLBACK 'mf1'"}
        b = r['ddl_waits'][0]['blockers'][0]
        self.assertEqual((b['connection_id'], b['xa_prepared'], b['culprit']), (26, expected, True))
        self.assertEqual({n['connection_id']: n['xa_prepared'] for n in r['blocking_tree']},
                         {23: None, 26: expected})
        self.assertIn(locks.XA_NOTE, r['notes'])
        self.assertEqual(conn.sql('events_transactions_current'), [], 'only tables the report already reads')
        text = locks.format_text(r)
        self.assertIn("    prepared XA transaction: ending the session releases nothing; end it with "
                      "XA COMMIT 'mf1' or XA ROLLBACK 'mf1'", text)
        self.assertIn("   prepared XA transaction: ending the session releases nothing; end it with "
                      "XA COMMIT 'mf1' or XA ROLLBACK 'mf1'", text)
        self.assertNotIn('myflames locks --kill 26', text)
        self.assertNotIn('to end it yourself: KILL 26', text, 'a KILL would release nothing')
        self.assertIn('to end it yourself: KILL 23', text)

    def test_an_xid_that_is_not_plain_gets_generic_guidance(self):
        last = [[26, 'XA PREPARE @x', 40], [23, 'SELECT * FROM t WHERE k >= 60 FOR UPDATE', 95]]
        r = self.report(mysql_conn(last=last_answer(last)))
        self.assertEqual(r['ddl_waits'][0]['blockers'][0]['xa_prepared'],
                         {'xid': None, 'commit': None, 'rollback': None})
        self.assertIn('end it with XA COMMIT or XA ROLLBACK', locks.format_text(r))

    def test_no_xa_note_without_a_prepared_xa_transaction(self):
        r = self.report(mysql_conn())
        self.assertNotIn(locks.XA_NOTE, r['notes'])
        self.assertTrue(all(n['xa_prepared'] is None for n in r['blocking_tree']))

    def test_foreign_key_alter_waits_on_the_parent_table(self):
        # MySQL: ALTER TABLE child ADD FOREIGN KEY also takes EXCLUSIVE on the parent table.
        mdl_rows = [mdl('TABLE', 'lk', 'parent', 'EXCLUSIVE', 'PENDING', 70, 30),
                    mdl('TABLE', 'lk', 'child', 'SHARED_UPGRADABLE', 'GRANTED', 70, 30),
                    mdl('TABLE', 'lk', 'parent', 'SHARED_READ', 'GRANTED', 66, 26)]
        threads = MYSQL_THREADS + [[70, 30, 'dba', 'localhost', 'lk', 'Query', 9, 'Waiting for table metadata lock',
                                    'ALTER TABLE child ADD FOREIGN KEY (p) REFERENCES parent (id)']]
        r = self.report(mysql_conn(mdl=mdl_rows, threads=threads_answer(threads), waits=[], locks=[],
                                   trx=[MYSQL_TRX[1]]))
        d = r['ddl_waits'][0]
        self.assertEqual((d['connection_id'], d['schema'], d['table'], d['statement_schema'], d['statement_table']),
                         (30, 'lk', 'parent', 'lk', 'child'))
        self.assertEqual([b['connection_id'] for b in d['blockers']], [26])
        self.assertIn('Connection 30 (dba@localhost) ALTER TABLE on lk.child waits ~9s for EXCLUSIVE on lk.parent: '
                      'ALTER TABLE child ADD FOREIGN KEY (p) REFERENCES parent (id)', locks.format_text(r))

    def test_likely_notes_name_parent_tables_and_non_innodb_transactions(self):
        setup = [['instrument', 'NO'], ['global_instrumentation', 'YES'], ['events_statements_current', 'YES']]
        r = self.report(mysql_conn(setup=setup, ddl=ddl_rows(MYSQL_THREADS, (28, 29))))
        self.assertTrue(has_note(r, 'adds a foreign key or renames the table also locks the parent tables'))
        self.assertTrue(has_note(r, 'Transactions that used only non-InnoDB tables (MyISAM, MEMORY, Aria) hold '
                                    'metadata locks too but are not in INNODB_TRX'))
        r = self.report(TestDDLBlockers.mariadb_ddl(self))
        self.assertTrue(has_note(r, 'are not in INNODB_TRX, so they are missing from this list'))
        self.assertFalse(has_note(r, 'parent tables'))

    def test_deadlock_only_skips_the_ddl_reads(self):
        r = self.report(mysql_conn(), deadlock_only=True)
        self.assertEqual((r['ddl_waits'], r['kill_privilege']), ([], None))


# ---- may this account end other sessions? ----------------------------------------------------------

class TestKillPrivilege(LockCase):
    def parse(self, engine, ver, *lines):
        return locks.parse_kill_privilege(engine, ver, list(lines), 'u', 'u@%')

    def test_mysql_variants(self):
        cases = (
            (['GRANT PROCESS, SUPER ON *.* TO `u`@`%`'], True, 'SUPER', False),
            (['GRANT USAGE ON *.* TO `u`@`%`', 'GRANT CONNECTION_ADMIN ON *.* TO `u`@`%`'], True,
             'CONNECTION_ADMIN', False),
            # An active role's privileges are merged into the user's lines.
            (['GRANT PROCESS ON *.* TO `u`@`%`', 'GRANT CONNECTION_ADMIN,SYSTEM_USER ON *.* TO `u`@`%`',
              'GRANT `killer`@`%` TO `u`@`%`'], True, 'CONNECTION_ADMIN', True),
            # A granted role that is not active shows only as a role grant.
            (['GRANT PROCESS ON *.* TO `u`@`%`', 'GRANT `killer`@`%` TO `u`@`%`'], False, None, False),
            (['GRANT ALL PRIVILEGES ON *.* TO `u`@`%` WITH GRANT OPTION'], True, 'ALL PRIVILEGES', None),
            (['GRANT ALL PRIVILEGES ON `lk`.* TO `u`@`%`', 'GRANT PROCESS ON *.* TO `u`@`%`'], False, None, False),
            (["GRANT PROXY ON ''@'' TO 'u'@'%' WITH GRANT OPTION"], False, None, False),
            ([], False, None, False),
        )
        for lines, allowed, via, system_user in cases:
            with self.subTest(lines=lines):
                p = self.parse('mysql', (8, 4, 6), *lines)
                self.assertEqual((p['checked'], p['can_kill_others'], p['via'], p['system_user'], p['super_only']),
                                 (True, allowed, via, system_user, False))

    def test_mariadb_variants(self):
        cases = (
            ((11, 4, 13), ['GRANT PROCESS, CONNECTION ADMIN ON *.* TO `u`@`%`'], True, 'CONNECTION ADMIN', False),
            # Before 11.0 SUPER covers it; from 11.0 it does not.
            ((10, 11, 9), ['GRANT SUPER ON *.* TO `u`@`%`'], True, 'SUPER', False),
            ((11, 4, 13), ['GRANT SUPER ON *.* TO `u`@`%`'], False, None, True),
            ((13, 0, 2), ['GRANT PROCESS, SUPER ON *.* TO `u`@`%`'], False, None, True),
            # The current role's grants follow the user's.
            ((11, 4, 13), ['GRANT PROCESS ON *.* TO `u`@`%`', 'GRANT `killer_role` TO `u`@`%`',
                           'GRANT CONNECTION ADMIN ON *.* TO `killer_role`'], True, 'CONNECTION ADMIN', False),
            ((11, 4, 13), ["GRANT ALL PRIVILEGES ON *.* TO `root`@`localhost` IDENTIFIED VIA "
                           "mysql_native_password USING '*ABC' OR unix_socket WITH GRANT OPTION"],
             True, 'ALL PRIVILEGES', False),
            ((11, 4, 13), ['GRANT USAGE ON *.* TO `u`@`%`'], False, None, False),
        )
        for ver, lines, allowed, via, super_only in cases:
            with self.subTest(ver=ver, lines=lines):
                p = self.parse('mariadb', ver, *lines)
                self.assertEqual((p['can_kill_others'], p['via'], p['system_user'], p['super_only']),
                                 (allowed, via, None, super_only))

    def test_kill_allowed(self):
        p = self.parse('mysql', (8, 4, 6), 'GRANT PROCESS ON *.* TO `u`@`%`')
        self.assertIs(locks.kill_allowed(p, 'u'), True)
        self.assertIs(locks.kill_allowed(p, 'other'), False)
        self.assertIsNone(locks.kill_allowed(p, None))
        self.assertIsNone(locks.kill_allowed({'checked': False}, 'u'))
        self.assertIsNone(locks.kill_allowed(None, 'u'))
        self.assertIs(locks.kill_allowed(self.parse('mysql', (8, 4, 6), 'GRANT SUPER ON *.* TO `u`@`%`'), 'x'), True)

    def test_report_marks_who_can_be_ended_and_explains(self):
        grants = [['GRANT SELECT, PROCESS ON *.* TO `app`@`%`']]
        r = self.report(mysql_conn(grants=grants))
        self.assertEqual(r['kill_privilege'], {'checked': True, 'user': 'app', 'account': 'app@%',
                                               'can_kill_others': False, 'via': None, 'system_user': False,
                                               'super_only': False})
        tree = r['blocking_tree']
        self.assertEqual([(n['connection_id'], n['kill_allowed']) for n in tree], [(23, True), (26, False)])
        self.assertEqual(r['ddl_waits'][0]['blockers'][0]['kill_allowed'], False)
        self.assertTrue(has_note(r, "This account (app@%) can end only sessions of its own user, app. Ending "
                                    "another user's session needs CONNECTION_ADMIN or SUPER."))
        self.assertEqual([t['kill_allowed'] for t in r['transactions']], [True, False, True, True, True])
        text = locks.format_text(r)
        self.assertIn('or, after a confirmation: myflames locks --kill 23', text)
        self.assertNotIn('myflames locks --kill 26', text)
        self.assertIn('ending it needs another account (see notes): KILL 26', text)

    def test_mysql_system_user_note(self):
        r = self.report(mysql_conn(grants=[['GRANT CONNECTION_ADMIN ON *.* TO `app`@`%`']]))
        self.assertTrue(has_note(r, 'lacks SYSTEM_USER, so MySQL refuses to end a session of an account that '
                                    'has SYSTEM_USER'))

    def test_mariadb_super_only_note(self):
        conn = ma_conn('11.8.3-MariaDB', grants=[['GRANT PROCESS, SUPER ON *.* TO `app`@`%`']],
                       who=[['app@localhost', 'app@%']])
        r = self.report(conn)
        self.assertTrue(has_note(r, 'needs CONNECTION ADMIN (SUPER also works before MariaDB 11.0)'))
        self.assertTrue(has_note(r, 'SUPER granted on 11.0 or later no longer covers it'))

    def test_unreadable_grants_hide_the_action(self):
        r = self.report(mysql_conn(grants=ConnectorError('mysql client exited 1: ERROR 1044: denied')))
        self.assertEqual((r['kill_privilege']['checked'], r['kill_privilege']['can_kill_others']), (False, None))
        self.assertTrue(all(n['kill_allowed'] is None for n in r['blocking_tree']))
        self.assertTrue(has_note(r, "Could not read this account's grants"))

    def test_no_privilege_notes_without_blockers(self):
        r = self.report(empty_mysql(grants=[['GRANT USAGE ON *.* TO `app`@`%`']]))
        self.assertFalse(has_note(r, 'This account'))


# ---- the one write: KILL -------------------------------------------------------------------------------

# A DDL scenario: 41 is idle in a transaction that read and changed lk.t, 42's
# ALTER TABLE waits for EXCLUSIVE, 43's SELECT queues behind it. After the KILL
# of 41, the ALTER and the SELECT finish and nothing waits.
K_THREADS = [
    [81, 41, 'app', '172.17.0.1', 'lk', 'Sleep', 120, None, None],
    [82, 42, 'dba', '172.17.0.1', 'lk', 'Query', 30, 'Waiting for table metadata lock', 'ALTER TABLE t ADD COLUMN c INT'],
    [83, 43, 'app', '172.17.0.1', 'lk', 'Query', 20, 'Waiting for table metadata lock', 'SELECT * FROM t'],
]
K_MDL = [mdl('TABLE', 'lk', 't', 'EXCLUSIVE', 'PENDING', 82, 42),
         mdl('TABLE', 'lk', 't', 'SHARED_READ', 'PENDING', 83, 43),
         mdl('TABLE', 'lk', 't', 'SHARED_READ', 'GRANTED', 81, 41),
         mdl('TABLE', 'lk', 't', 'SHARED_WRITE', 'GRANTED', 81, 41),
         mdl('TABLE', 'lk', 't', 'SHARED_UPGRADABLE', 'GRANTED', 82, 42)]
K_TRX = [['1837', 'RUNNING', '2026-10-14 10:00:00', 120, None, None, None, 41, None, None,
          0, 1, 2, 1, 1, 'REPEATABLE READ', 0]]
K_TARGET = [81, 41, 'app', '172.17.0.1', 'lk', 'Sleep', 120, None, None, 99]
K_TARGET_TRX = [['1837', 'RUNNING', '2026-10-14 10:00:00', 120, 1, 1]]
K_EXPECT = {'user': 'app', 'host': '172.17.0.1', 'trx_started': '2026-10-14 10:00:00', 'ps_thread_id': 81}


def bound_kill_expect(conn, expect=None, connection=None, safety=None):
    from myflames import dbops
    if connection is None:
        connection = {key: getattr(conn, key, None) for key in ('host', 'port', 'user', 'ssl_mode', 'ssl_ca')}
    version = conn.responses[0][1]
    identity = ({'server_id': '1', 'hostname': 'server-one', 'version': '11.4.13-MariaDB'}
                if 'MariaDB' in version else {'server_uuid': 'server-uuid-1'})
    return dict(K_EXPECT if expect is None else expect,
                connection_identity=dbops.connection_identity(connection, safety), server_identity=identity)


class KillScenario(object):
    """Answers that change once the KILL is sent: the blocker and both waits disappear."""

    def __init__(self, target=None, target_trx=None, kill=None, after=None, after_trx=None, **overrides):
        self.killed = False
        self.target = K_TARGET if target is None else target
        self.target_trx = K_TARGET_TRX if target_trx is None else target_trx
        self.after = after if after is not None else []
        self.after_trx = after_trx if after_trx is not None else []
        self.kill_error = kill
        answers = {'trx': self.phase(K_TRX, []), 'waits': [], 'locks': [], 'setup': SETUP_ON,
                   'mdl': self.phase(K_MDL, []), 'threads': self.threads,
                   'ddl': self.phase(ddl_rows(K_THREADS, (42, 43)), []),
                   'last': last_answer([[41, 'UPDATE t SET v = v + 1 WHERE id = 1', 10]]),
                   'status': status_row(NO_DEADLOCK), 'target': self.read_target,
                   'target_trx': self.read_trx, 'kill': self.kill}
        answers.update(overrides)
        self.conn = fake('8.4.6', True, **answers)

    def phase(self, before, after):
        return lambda sql: batch(after if self.killed else before)

    def threads(self, sql):
        rows = [r for r in K_THREADS if r[1] != 41] if self.killed else K_THREADS
        return threads_answer(rows)(sql)

    def read_target(self, sql):
        rows = self.after if self.killed else ([self.target] if self.target else [])
        if isinstance(rows, Exception):
            raise rows
        return batch(rows)

    def read_trx(self, sql):
        rows = self.after_trx if self.killed else self.target_trx
        if isinstance(rows, Exception):
            raise rows
        return batch(rows)

    def kill(self, sql):
        if self.kill_error is not None:
            raise self.kill_error
        self.killed = True
        return ''

    def kills(self):
        return [sql for sql, _ in self.conn.calls if sql.startswith(KILL_TAG)]


class TestKillSession(LockCase):
    def kill(self, scenario=None, mode='connection', expect=None, connection_id=41):
        scenario = scenario or KillScenario()
        result = locks.kill_session(scenario.conn, connection_id, mode, dict(bound_kill_expect(scenario.conn), **(expect or {})), settle_s=0, confirmed=True)
        json.dumps(result, allow_nan=False)
        for sql, flags in scenario.conn.calls:
            self.assertIn('--comments', flags, sql)
            if sql.startswith(KILL_TAG):
                self.assertRegex(sql[len(KILL_TAG):], r'^KILL (CONNECTION|QUERY) \d+$')
            else:
                self.assert_read_statement(sql)
        self.assertLessEqual(len(scenario.kills()), 1, 'never more than one KILL')
        return result, scenario

    def test_kill_connection_after_checking_the_identity(self):
        result, scenario = self.kill()
        self.assertEqual(scenario.kills(), [KILL_TAG + 'KILL CONNECTION 41'])
        self.assertEqual({k: result[k] for k in ('schema_version', 'engine', 'connection_id', 'mode', 'statement',
                                                 'status', 'verified', 'after')},
                         {'schema_version': 'kill-1.0', 'engine': 'mysql', 'connection_id': 41, 'mode': 'connection',
                          'statement': 'KILL CONNECTION 41', 'status': 'killed',
                          'verified': ['connection', 'server identity', 'user', 'host', 'not this session', 'server thread', 'transaction'],
                          'after': {'present': False}})
        self.assertEqual(result['message'], 'Ended connection 41. Its open transaction was rolled back and its '
                                            'locks were released.')
        self.assertEqual(result['target']['trx']['rows_modified'], 1)
        # The identity is re-read right before the KILL, from threads, bounded.
        target_sql = scenario.conn.sql(', CONNECTION_ID() FROM ')
        self.assertIn('FROM performance_schema.threads WHERE PROCESSLIST_ID = 41 LIMIT 1', target_sql[0])
        self.assertIn('WHERE trx_mysql_thread_id = 41 LIMIT 1', scenario.conn.sql('trx_mysql_thread_id = ')[0])

    def test_a_reused_id_or_a_changed_transaction_is_refused(self):
        cases = (
            ({'user': 'other'}, 'now a different session (user app, not other)'),
            ({'host': '10.0.0.9'}, 'now a different session (host 172.17.0.1, not 10.0.0.9)'),
            ({'ps_thread_id': 5}, 'now a different session (another server thread)'),
            ({'trx_started': '2026-10-14 09:00:00'}, 'its open transaction now started at 2026-10-14 10:00:00'),
        )
        for expect, message in cases:
            with self.subTest(expect=expect):
                result, scenario = self.kill(expect=expect)
                self.assertEqual((result['status'], scenario.kills(), result['statement']), ('refused', [], None))
                self.assertIn(message, result['message'])
                self.assertIn('Nothing was ended' if 'different' in message else 'nothing was ended',
                              result['message'])
        result, scenario = self.kill(KillScenario(target_trx=[]))
        self.assertEqual((result['status'], scenario.kills()), ('refused', []))
        self.assertIn('it has no open transaction now', result['message'])

    def test_host_with_a_client_port_matches(self):
        target = list(K_TARGET)
        target[3] = '172.17.0.1:5555'
        result, _ = self.kill(KillScenario(target=target))
        self.assertEqual(result['status'], 'killed')
        self.assertTrue(locks._same_host('::1:5555', '::1'))
        self.assertFalse(locks._same_host('10.0.0.12:5555', '10.0.0.1'))

    def test_refusals_before_any_kill(self):
        def target(**changes):
            row = list(K_TARGET)
            for index, value in changes.items():
                row[int(index[1:])] = value
            return row
        cases = (
            (KillScenario(target=[]), 'gone', 'no longer connected'),
            (KillScenario(target=target(c9=41)), 'refused', 'the session myflames is using'),
            (KillScenario(target=target(c5='Query', c8='/*myflames:capture*/ SELECT 1')), 'refused',
             'running a myflames statement'),
            (KillScenario(target=target(c2='system user', c5='Connect')), 'refused', 'is a server thread (system user)'),
            (KillScenario(target=target(c2=None, c5='Daemon')), 'refused', 'is a server thread'),
        )
        for scenario, status, message in cases:
            with self.subTest(message=message):
                expect = {'user': 'system user'} if 'system user' in message else None
                result, scenario = self.kill(scenario, expect=expect)
                self.assertEqual((result['status'], scenario.kills()), (status, []))
                self.assertIn(message, result['message'])

    def test_a_killed_command_does_not_block_kill_connection(self):
        # MariaDB shows Killed after KILL QUERY on an idle session; the transaction is still open.
        target = list(K_TARGET)
        target[5] = 'Killed'
        result, scenario = self.kill(KillScenario(target=target))
        self.assertEqual((result['status'], scenario.kills()), ('killed', [KILL_TAG + 'KILL CONNECTION 41']))
        result, scenario = self.kill(KillScenario(target=target), mode='query')
        self.assertEqual((result['status'], scenario.kills()), ('refused', []))
        self.assertIn('already received a KILL. If it was KILL CONNECTION, it is rolling back its transaction',
                      result['message'])
        self.assertIn('On MariaDB it may instead have received KILL QUERY while idle', result['message'])

    def test_a_transaction_rolling_back_is_already_ending(self):
        rolling = [['1837', 'ROLLING BACK', '2026-10-14 10:00:00', 120, 1, 1]]
        result, scenario = self.kill(KillScenario(target_trx=rolling))
        self.assertEqual((result['status'], scenario.kills()), ('refused', []))
        self.assertIn('already rolling back its transaction and keeps its locks until that finishes',
                      result['message'])

    def test_a_prepared_xa_transaction_is_refused_with_its_xid(self):
        for statement, xid in (("XA PREPARE 'mf1'", "'mf1'"), ("xa prepare 'it''s','b',2", "'it''s','b',2"),
                               ("XA PREPARE X'00ff';", "X'00ff'")):
            with self.subTest(statement=statement):
                result, scenario = self.kill(KillScenario(target_last=[[statement]]))
                self.assertEqual((result['status'], scenario.kills()), ('refused', []))
                self.assertIn('ran XA PREPARE: its locks belong to the prepared XA transaction', result['message'])
                self.assertIn('End the transaction with XA COMMIT %s or XA ROLLBACK %s.' % (xid, xid),
                              result['message'])
                self.assertEqual(result['target']['xa_prepared']['xid'], xid)
                sql = scenario.conn.sql('ORDER BY EVENT_ID DESC LIMIT 1')[0]
                self.assertIn('FROM performance_schema.events_statements_current WHERE THREAD_ID = 81', sql)
        result, scenario = self.kill(KillScenario(target_last=[['XA PREPARE @x']]))
        self.assertEqual(result['status'], 'refused')
        self.assertIn('XA RECOVER lists the xid; it needs XA_RECOVER_ADMIN', result['message'])
        # A running session is not checked: its last statement is the one running.
        running = list(K_TARGET)
        running[5], running[8] = 'Query', 'UPDATE t SET v = 2'
        _, scenario = self.kill(KillScenario(target=running, target_last=[["XA PREPARE 'x'"]]), mode='query')
        self.assertEqual(scenario.conn.sql('ORDER BY EVENT_ID DESC LIMIT 1'), [])

    def test_kill_query_only_for_a_running_statement(self):
        result, scenario = self.kill(mode='query')
        self.assertEqual((result['status'], scenario.kills()), ('refused', []))
        self.assertIn('KILL QUERY would do nothing: its open transaction would keep its locks', result['message'])
        running = list(K_TARGET)
        running[5], running[8] = 'Query', 'UPDATE t SET v = 2'
        after = [running]
        result, scenario = self.kill(KillScenario(target=running, after=[[81, 41, 'app', '172.17.0.1', 'lk',
                                                                          'Sleep', 0, None, None, 99]],
                                                  after_trx=K_TARGET_TRX), mode='query')
        self.assertEqual(scenario.kills(), [KILL_TAG + 'KILL QUERY 41'])
        self.assertEqual(result['status'], 'killed')
        self.assertIn('stays open with its locks', result['message'])
        result, _ = self.kill(KillScenario(target=running, after=after, after_trx=K_TARGET_TRX), mode='query')
        self.assertIn('The statement is still stopping', result['message'])

    def test_rollback_in_progress(self):
        after = [[81, 41, 'app', '172.17.0.1', 'lk', 'Killed', 121, 'rolling back', None, 99]]
        after_trx = [['1837', 'ROLLING BACK', '2026-10-14 10:00:00', 121, 1, 250000]]
        result, _ = self.kill(KillScenario(after=after, after_trx=after_trx))
        self.assertEqual(result['after'], {'present': True, 'command': 'Killed', 'state': 'rolling back',
                                           'statement': None, 'trx_state': 'ROLLING BACK', 'rows_modified': 250000})
        self.assertIn('is ending: it is rolling back its transaction (1 row changed)', result['message'])

    def test_server_errors(self):
        cases = (
            ('mysql', '8.4.6', 'ERROR 1094 (HY000) at line 1: Unknown thread id: 41', 'gone', 'ended before the KILL'),
            ('mysql', '8.4.6', 'ERROR 1095 (HY000) at line 1: You are not owner of thread 41', 'denied',
             'needs CONNECTION_ADMIN or SUPER, and ending a session of an account that has SYSTEM_USER'),
            ('mariadb', '11.4.13-MariaDB', 'ERROR 1095 (HY000) at line 1: You are not owner of thread 41', 'denied',
             'needs CONNECTION ADMIN (or SUPER before MariaDB 11.0); on a Galera node, applier and high-priority '
             'threads cannot be ended at all'),
        )
        for engine, version, error, status, message in cases:
            with self.subTest(error=error, engine=engine):
                scenario = KillScenario(kill=ConnectorError('mysql client exited 1: ' + error))
                scenario.conn.responses[0] = (VERSION_SQL, batch([[version, 1]]))
                result, _ = self.kill(scenario)
                self.assertEqual(result['status'], status)
                self.assertIn(message, result['message'])
                self.assertIsNone(result['after'])
        scenario = KillScenario(kill=ConnectorError('mysql client exited 1: ERROR 2013 (HY000): Lost connection'))
        with self.assertRaisesRegex(ValueError, 'Could not end connection 41: ERROR 2013'):
            locks.kill_session(scenario.conn, 41, 'connection', bound_kill_expect(scenario.conn), settle_s=0, confirmed=True)

    def test_bad_arguments_raise_before_any_sql(self):
        bad = ((0, 'connection', K_EXPECT), (True, 'connection', K_EXPECT), ('41', 'connection', K_EXPECT),
               (2 ** 63, 'connection', K_EXPECT), (41, 'hard', K_EXPECT), (41, 'connection', None),
               (41, 'connection', {'user': 'app'}), (41, 'connection', {'user': '', 'host': 'h'}),
               (41, 'connection', dict(K_EXPECT, trx_started=5)),
               (41, 'connection', dict(K_EXPECT, ps_thread_id='81')),
               (41, 'connection', dict(K_EXPECT, ps_thread_id=True)))
        for args in bad:
            with self.subTest(args=args):
                scenario = KillScenario()
                with self.assertRaises(ValueError):
                    locks.kill_session(scenario.conn, *args, settle_s=0)
                self.assertEqual(scenario.conn.calls, [])

    def test_falls_back_to_the_process_list_and_works_without_process(self):
        denied = ConnectorError("mysql client exited 1: ERROR 1142 (42000): SELECT command denied to user "
                                "'app'@'%' for table 'threads'")
        scenario = KillScenario(target_trx=process_denied())
        calls = []

        def target(sql):
            calls.append(sql)
            if 'performance_schema.threads' in sql:
                raise denied
            return batch([] if scenario.killed else [[None] + K_TARGET[1:3] + ['172.17.0.1:5555'] + K_TARGET[4:]])
        scenario.conn.responses = [(p, target if p == ', CONNECTION_ID() FROM ' else a)
                                   for p, a in scenario.conn.responses]
        result, _ = self.kill(scenario, expect={'ps_thread_id': None})
        self.assertEqual(result['status'], 'killed')
        self.assertIn('FROM information_schema.PROCESSLIST WHERE ID = 41 LIMIT 1', calls[1])
        self.assertEqual(result['verified'], ['connection', 'server identity', 'user', 'host', 'not this session'])
        self.assertFalse(result['target']['trx_checked'])


class TestXaPrepare(unittest.TestCase):
    def test_xid_is_copied_only_when_well_formed(self):
        cases = {
            "XA PREPARE 'mf1'": "'mf1'",
            "/* app */ xa prepare 'g','b',2;": "'g','b',2",
            "XA PREPARE X'00ff'": "X'00ff'",
            "XA PREPARE 'it''s'": "'it''s'",
            "XA PREPARE 0x6162, 0x63, 7": "0x6162, 0x63, 7",
            "XA PREPARE 'a'; DROP TABLE t": None,
            'XA PREPARE @x': None,
            "XA PREPARE 'a','b','c','d'": None,
        }
        for statement, xid in cases.items():
            with self.subTest(statement=statement):
                xa = locks.xa_prepared_from(statement)
                self.assertEqual(xa['xid'], xid)
                self.assertEqual(xa['rollback'], 'XA ROLLBACK %s' % xid if xid else None)
        for statement in (None, '', 'SELECT 1', "XA COMMIT 'x'", "/* XA PREPARE 'x' */ SELECT 1"):
            self.assertIsNone(locks.xa_prepared_from(statement), statement)


class TestKillAPI(LockCase):
    def connection(self):
        return {'host': '127.0.0.1', 'port': 3306, 'user': 'app', 'password': PASSWORD}

    def workspace(self):
        from myflames.workspace_store import WorkspaceStore
        return type('Workspace', (), {'store': WorkspaceStore()})()

    def call(self, scenario, payload):
        payload = dict(payload, confirmed=True)
        if isinstance(payload.get('expect'), dict):
            payload['expect'] = bound_kill_expect(scenario.conn, payload['expect'], self.connection())
        with mock.patch('myflames.connector.MySQLConnection', FakeFactory(scenario.conn)), \
                mock.patch.object(locks.time, 'sleep'):
            workspace = self.workspace()
            body = dict({'connection': self.connection()}, **payload)
            body['review_token'] = ui.api_response('/api/live/kill/prepare', body, workspace)['review_token']
            return ui.api_response('/api/live/kill', body, workspace=workspace)

    def test_kills_with_the_expected_identity_from_the_report(self):
        from myflames import dbops
        report_scenario = KillScenario()
        with mock.patch('myflames.connector.MySQLConnection', FakeFactory(report_scenario.conn)):
            report = ui.api_response('/api/live/locks', {'connection': self.connection()}, workspace=object())
        blocker = report['ddl_waits'][0]['blockers'][0]
        expect = {k: blocker[k] for k in ('user', 'host', 'trx_started', 'ps_thread_id')}
        self.assertEqual(expect, K_EXPECT)
        expect.update(connection_identity=dbops.connection_identity(self.connection()),
                      server_identity=report['server_identity'])
        scenario = KillScenario()
        with mock.patch('myflames.connector.MySQLConnection', FakeFactory(scenario.conn)), \
                mock.patch.object(locks.time, 'sleep'):
            workspace = self.workspace()
            body = {'connection': self.connection(), 'connection_id': 41, 'mode': 'connection',
                    'expect': expect, 'confirmed': True}
            body['review_token'] = ui.api_response('/api/live/kill/prepare', body, workspace)['review_token']
            result = ui.api_response('/api/live/kill', body, workspace=workspace)
        self.assertEqual((result['status'], scenario.kills()), ('killed', [KILL_TAG + 'KILL CONNECTION 41']))

    def test_mode_defaults_to_connection_and_refusals_are_results(self):
        scenario = KillScenario()
        result = self.call(scenario, {'connection_id': 41, 'expect': K_EXPECT})
        self.assertEqual(result['mode'], 'connection')
        scenario = KillScenario()
        result = self.call(scenario, {'connection_id': 41, 'mode': 'query', 'expect': K_EXPECT})
        self.assertEqual((result['status'], scenario.kills()), ('refused', []))

    def test_bad_input_is_an_error_and_never_echoes_the_password(self):
        for payload in ({'connection_id': 41}, {'connection_id': 'x', 'expect': K_EXPECT},
                        {'connection_id': 41, 'mode': 'all', 'expect': K_EXPECT}):
            with self.subTest(payload=payload):
                scenario = KillScenario()
                with self.assertRaises(ValueError):
                    self.call(scenario, payload)
                self.assertEqual(scenario.kills(), [])
        scenario = KillScenario(kill=ConnectorError('mysql client exited 1: ERROR 2013: lost ' + PASSWORD))
        with self.assertRaises(ValueError) as caught:
            self.call(scenario, {'connection_id': 41, 'expect': K_EXPECT})
        self.assertNotIn(PASSWORD, str(caught.exception))
        self.assertIn('[redacted]', str(caught.exception))

    def test_route_is_allowlisted_takes_saved_profiles_and_is_not_a_read_only_view(self):
        self.assertIn('"/api/live/kill"', inspect.getsource(ui.WorkspaceHandler.do_POST))
        self.assertIn('/api/live/kill', ui._CONNECTION_PATHS)
        self.assertNotIn('/api/live/kill', ui._SERVER_VIEW_PATHS)
        with self.assertRaisesRegex(ValueError, 'requires a running workspace'):
            ui.api_response('/api/live/kill', {'connection': self.connection()})


class FakeStdin(object):
    def __init__(self, answer, tty=True):
        self.answer, self.tty, self.read = answer, tty, 0

    def isatty(self):
        return self.tty

    def readline(self):
        self.read += 1
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


class TestKillCLI(LockCase):
    def call(self, scenario, *args, stdin=None):
        factory = FakeFactory(scenario.conn)
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch('myflames.connector.MySQLConnection', factory), \
                mock.patch.object(locks.time, 'sleep'), \
                mock.patch.object(sys, 'stdin', stdin or FakeStdin('', tty=False)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                workspace_cli.cmd_locks(list(args))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_tty_confirm_kills_and_shows_the_outcome(self):
        scenario = KillScenario()
        stdin = FakeStdin('y\n')
        code, out, err = self.call(scenario, '--kill', '41', stdin=stdin)
        self.assertEqual(code, 0, err)
        self.assertEqual((stdin.read, scenario.kills()), (1, [KILL_TAG + 'KILL CONNECTION 41']))
        for line in ('Connection 41: app@172.17.0.1, db lk', '  Sleep for 2m00s (idle in transaction)',
                     '  transaction open 2m00s, RUNNING, 1 row locked, 1 row changed',
                     '  last statement: UPDATE t SET v = v + 1 WHERE id = 1',
                     '  blocking DDL: connection 42, ALTER TABLE t ADD COLUMN c INT',
                     '  blocking connection 43',
                     '  KILL CONNECTION 41 closes the connection and rolls back its open transaction.',
                     'End it with KILL CONNECTION 41? [y/N] '):
            self.assertIn(line, err)
        self.assertEqual(out, 'Ended connection 41. Its open transaction was rolled back and its locks were released.\n'
                              'The ALTER TABLE on connection 42 no longer waits for a lock; it is running or done.\n'
                              'Connection 43 no longer waits.\n')

    def test_target_text_cannot_rewrite_the_confirmation(self):
        """The target's last statement comes from the server: a carriage
        return and an erase-line sequence in it must not overwrite what the
        user confirms."""
        spoof = 'UPDATE t SET v = 1 /*\x1b[2K\rConnection 99: harmless\x07*/'
        scenario = KillScenario(last=last_answer([[41, spoof, 10]]))
        code, out, err = self.call(scenario, '--kill', '41', stdin=FakeStdin('n\n'))
        self.assertEqual((code, scenario.kills()), (2, []))
        self.assertIsNone(re.search(r'[\x00-\x08\x0b-\x1f\x7f-\x9f]', err))
        # describe_target already folds the carriage return into a space.
        self.assertIn('last statement: UPDATE t SET v = 1 /*\\u001b[2K Connection 99: harmless\\u0007*/', err)
        self.assertIn('End it with KILL CONNECTION 41? [y/N] ', err)

    def test_tty_decline_and_interrupt_send_nothing(self):
        for answer in ('n\n', '\n', 'yes please\n', KeyboardInterrupt(), EOFError()):
            with self.subTest(answer=answer):
                scenario = KillScenario()
                code, out, err = self.call(scenario, '--kill', '41', stdin=FakeStdin(answer))
                self.assertEqual((code, out, scenario.kills()), (2, '', []))

    def test_non_tty_refuses_without_yes(self):
        scenario = KillScenario()
        stdin = FakeStdin('y\n', tty=False)
        code, out, err = self.call(scenario, '--kill', '41', stdin=stdin)
        self.assertEqual((code, scenario.kills(), stdin.read), (2, [], 0))
        self.assertIn('Not sent: stdin is not a terminal', err)
        self.assertIn('Re-run with --yes to end connection 41.', err)
        self.assertIn('Connection 41: app@172.17.0.1', err, 'the details are shown even when refusing')

    def test_yes_skips_the_prompt_and_json_and_quiet(self):
        scenario = KillScenario()
        code, out, err = self.call(scenario, '--kill', '41', '--yes', '--json', '-q')
        self.assertEqual((code, err), (0, ''))
        result = json.loads(out)
        self.assertEqual((result['schema_version'], result['status'], result['statement']),
                         ('kill-1.0', 'killed', 'KILL CONNECTION 41'))
        self.assertEqual(result['outcome'], [
            {'connection_id': 42, 'waiting': False, 'blocked_by': [], 'ddl': 'ALTER TABLE', 'precision': None},
            {'connection_id': 43, 'waiting': False, 'blocked_by': [], 'ddl': None, 'precision': None}])

    def test_query_mode_on_an_idle_session_is_refused(self):
        scenario = KillScenario()
        code, out, err = self.call(scenario, '--kill', '41', '--query', '--yes')
        self.assertEqual((code, scenario.kills()), (2, []))
        self.assertIn('KILL QUERY would do nothing', err)

    def test_privilege_hint_refuses_before_asking(self):
        scenario = KillScenario(grants=[['GRANT PROCESS ON *.* TO `viewer`@`%`']],
                                who=[['viewer@localhost', 'viewer@%']])
        stdin = FakeStdin('y\n')
        code, out, err = self.call(scenario, '--kill', '41', stdin=stdin)
        self.assertEqual((code, scenario.kills(), stdin.read), (2, [], 0))
        self.assertIn("Not sent: connection 41 belongs to user app. This account (viewer@%) can end only sessions "
                      "of its own user, viewer.", err)

    def test_server_denial_and_gone_exit_1(self):
        for error, text in (('ERROR 1095 (HY000) at line 1: You are not owner of thread 41', 'The server refused'),
                            ('ERROR 1094 (HY000) at line 1: Unknown thread id: 41', 'ended before the KILL')):
            scenario = KillScenario(kill=ConnectorError('mysql client exited 1: ' + error))
            code, out, _ = self.call(scenario, '--kill', '41', '--yes')
            self.assertEqual(code, 1)
            self.assertIn(text, out)

    def test_usage_and_failures_exit_2(self):
        for argv in (['--query'], ['--yes'], ['--kill', '41', '--waits-only'], ['--kill', '41', '--deadlock'],
                     ['--kill', '0'], ['--kill', 'x']):
            with self.subTest(argv=argv):
                scenario = KillScenario()
                code, out, _ = self.call(scenario, *argv)
                self.assertEqual((code, out, scenario.conn.calls), (2, '', []))
        scenario = KillScenario(kill=ConnectorError('mysql client exited 1: ERROR 2013 (HY000): Lost connection'))
        code, out, err = self.call(scenario, '--kill', '41', '--yes', '-p' + PASSWORD)
        self.assertEqual((code, out), (2, ''))
        self.assertIn('Could not end connection 41', err)
        leaking = FakeConn('8.4.6', [('@@version', ConnectorError('Access denied for ' + PASSWORD))])
        code, out, err = self.call(type('S', (), {'conn': leaking})(), '--kill', '41', '--yes', '-p' + PASSWORD)
        self.assertEqual(code, 2)
        self.assertNotIn(PASSWORD, err)


class TestKillAudit(LockCase):
    """locks --kill and POST /api/live/kill go through the write policy
    (dbops.decide) and write one audit record per attempt."""

    def store(self):
        from myflames.workspace_store import WorkspaceStore
        return WorkspaceStore()

    def records(self):
        return self.store().audit_records()

    def cli(self, scenario, *args, stdin=None):
        return TestKillCLI.call(self, scenario, *args, stdin=stdin)

    def profile(self, **safety):
        return self.store().save('profile', dict({'name': 'prod', 'host': '127.0.0.1', 'user': 'app'}, **safety))

    def test_cli_kill_is_recorded_pending_then_ok(self):
        scenario = KillScenario()
        seen = []
        real = locks.kill_session

        def spy(conn, *args, **kwargs):
            # The pending record exists before the KILL reaches the server.
            original_run = conn.run

            def run(sql, extra_flags=None, timeout=None):
                if sql.startswith(KILL_TAG):
                    seen.append([(r['status'], r['outcome_unknown']) for r in self.records()])
                return original_run(sql, extra_flags, timeout)
            conn.run = run
            try:
                return real(conn, *args, **kwargs)
            finally:
                conn.run = original_run
        with mock.patch.object(locks, 'kill_session', spy):
            code, out, err = self.cli(scenario, '--kill', '41', '--yes', '--json', '-p' + PASSWORD)
        self.assertEqual(code, 0, err)
        self.assertEqual(seen, [[('pending', True)]])
        result = json.loads(out)
        [record] = self.records()
        self.assertEqual(result['audit_id'], record['id'])
        self.assertEqual((record['status'], record['outcome'], record['surface'], record['operation']),
                         ('ok', 'killed', 'cli', 'kill'))
        self.assertEqual(record['statements'], [KILL_TAG + 'KILL CONNECTION 41'])
        self.assertEqual(record['target'], dict(bound_kill_expect(scenario.conn), connection_id=41, mode='connection'))
        self.assertEqual(record['verified'], ['connection', 'server identity', 'user', 'host', 'not this session', 'server thread', 'transaction'])
        self.assertEqual(record['policy'], {'action': 'confirm', 'kind': 'risky', 'category': 'kill',
                                            'connection_type': 'production', 'read_only': False})
        self.assertEqual({k: record['connection'][k] for k in ('type', 'host', 'port', 'user', 'engine')},
                         {'type': 'production', 'host': '127.0.0.1', 'port': 3306, 'user': None, 'engine': 'mysql'})
        self.assertNotIn(PASSWORD.encode(), self.store().path.read_bytes())

    def test_cli_declined_refused_and_failed_attempts_are_recorded(self):
        cases = (
            (KillScenario(), ['--kill', '41'], FakeStdin('n\n'), ('declined', 'declined')),
            (KillScenario(), ['--kill', '41'], FakeStdin('y\n', tty=False), ('refused', 'no_terminal')),
            (KillScenario(), ['--kill', '41', '--query', '--yes'], None, ('refused', 'refused')),
            (KillScenario(grants=[['GRANT PROCESS ON *.* TO `viewer`@`%`']], who=[['viewer@localhost', 'viewer@%']]),
             ['--kill', '41'], FakeStdin('y\n'), ('refused', 'privilege')),
            (KillScenario(kill=ConnectorError('mysql client exited 1: ERROR 1095 (HY000) at line 1: You are not '
                                              'owner of thread 41')), ['--kill', '41', '--yes'], None,
             ('error', 'denied')),
        )
        for scenario, argv, stdin, expected in cases:
            with self.subTest(argv=argv, expected=expected):
                before = len(self.records())
                code, _, _ = self.cli(scenario, *argv, stdin=stdin)
                self.assertEqual(code, 1 if expected[0] == 'error' else 2)
                records = self.records()
                self.assertEqual(len(records), before + 1)
                self.assertEqual((records[0]['status'], records[0]['outcome']), expected)
                self.assertTrue(records[0]['finished_at'])
        self.assertEqual(self.records()[0]['error_code'], 1095)
        scenario = KillScenario(kill=ConnectorError('mysql client exited 1: ERROR 2013 (HY000): Lost ' + PASSWORD))
        code, _, _ = self.cli(scenario, '--kill', '41', '--yes', '-p' + PASSWORD)
        self.assertEqual(code, 2)
        record = self.records()[0]
        # A connection lost during the KILL leaves unknown whether it ran.
        self.assertEqual((record['status'], record['error_code'], record['outcome_unknown']), ('unknown', 2013, True))
        self.assertNotIn(PASSWORD, record['message'])
        self.assertNotIn(PASSWORD.encode(), self.store().path.read_bytes())

    def test_cli_read_only_profile_refuses_before_any_connection_or_prompt(self):
        self.profile(read_only=True)
        scenario = KillScenario()
        factory = FakeFactory(scenario.conn)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch('myflames.connector.MySQLConnection', factory), \
                mock.patch('getpass.getpass', side_effect=AssertionError('no prompt')), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                workspace_cli.cmd_locks(['--profile', 'prod', '--kill', '41', '--yes', '-p', '--json'])
        self.assertEqual(caught.exception.code, 2)
        self.assertIsNone(factory.options)
        self.assertEqual(scenario.conn.calls, [])
        self.assertEqual(out.getvalue(), '')
        self.assertIn('Connection prod is read-only in myflames', err.getvalue())
        self.assertIn('myflames profiles set prod --read-write', err.getvalue())
        [record] = self.records()
        self.assertEqual((record['status'], record['outcome'], record['connection']['profile_name'],
                          record['connection']['read_only']), ('refused', 'read_only', 'prod', True))
        self.assertTrue(record['id'])

    def test_cli_development_still_confirms_a_kill(self):
        self.profile(type='development')
        code, out, err = self.cli(KillScenario(), '--profile', 'prod', '--kill', '41',
                                  stdin=FakeStdin('y\n', tty=False))
        self.assertEqual(code, 2)
        self.assertIn('Not sent: stdin is not a terminal', err)
        self.assertIn('Connection prod is a Development connection. myflames asks only before risky writes, and '
                      'this statement is one.', err)
        scenario = KillScenario()
        code, _, err = self.cli(scenario, '--profile', 'prod', '--kill', '41', stdin=FakeStdin('y\n'))
        self.assertEqual((code, scenario.kills()), (0, [KILL_TAG + 'KILL CONNECTION 41']))
        self.assertEqual(self.records()[0]['policy']['connection_type'], 'development')

    def test_production_policy_line_is_shown_with_the_description(self):
        _, _, err = self.cli(KillScenario(), '--kill', '41', stdin=FakeStdin('n\n'))
        self.assertIn('  This is a Production connection, so myflames asks before every write.\n', err)

    def test_nothing_is_sent_when_the_audit_record_cannot_be_written(self):
        import sqlite3
        from myflames.workspace_store import WorkspaceStore
        scenario = KillScenario()
        with mock.patch.object(WorkspaceStore, 'audit_insert', side_effect=sqlite3.OperationalError('disk I/O error')):
            code, out, err = self.cli(scenario, '--kill', '41', '--yes')
        self.assertEqual((code, out, scenario.kills()), (2, '', []))
        self.assertIn('Could not write the audit record (disk I/O error), so nothing was sent.', err)

    def test_kill_is_sent_with_binary_mode(self):
        scenario = KillScenario()
        self.cli(scenario, '--kill', '41', '--yes')
        [flags] = [f for sql, f in scenario.conn.calls if sql.startswith(KILL_TAG)]
        self.assertEqual(flags, ['--comments', '--binary-mode'])

    def test_a_timeout_or_lost_connection_on_the_kill_is_outcome_unknown(self):
        for error in ('Query timed out after 12s: /*myflames:kill*/ KILL CONNECTION 41',
                      'mysql client exited 1: ERROR 2013 (HY000): Lost connection to MySQL server during query'):
            with self.subTest(error=error):
                code, _, err = self.cli(KillScenario(kill=ConnectorError(error)), '--kill', '41', '--yes')
                self.assertEqual(code, 2)
                self.assertIn('The KILL may have reached the server, so its outcome is unknown', err)
                record = self.records()[0]
                self.assertEqual((record['status'], record['outcome'], record['outcome_unknown']),
                                 ('unknown', 'timeout', True))

    def test_an_interrupt_after_the_kill_was_accepted_is_recorded_ok(self):
        scenario = KillScenario()
        with mock.patch.object(locks.time, 'sleep', side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                locks.kill_session(scenario.conn, 41, 'connection', bound_kill_expect(scenario.conn), audit=self.trail(), confirmed=True)
        self.assertEqual(scenario.kills(), [KILL_TAG + 'KILL CONNECTION 41'])
        record = self.records()[0]
        self.assertEqual((record['status'], record['outcome']), ('ok', 'killed'))
        self.assertIn('interrupted before checking', record['message'])

    def test_an_interrupt_while_the_kill_is_on_its_way_is_outcome_unknown(self):
        scenario = KillScenario(kill=KeyboardInterrupt())
        code, _, err = self.cli(scenario, '--kill', '41', '--yes')
        self.assertEqual(code, 2)
        self.assertIn('Cancelled. The KILL may already have reached the server', err)
        record = self.records()[0]
        self.assertEqual((record['status'], record['outcome']), ('unknown', 'interrupted'))

    def test_a_development_profile_pointed_elsewhere_is_production(self):
        self.profile(type='development')
        self.cli(KillScenario(), '--profile', 'prod', '-h', '127.0.0.2', '-ppw', '--kill', '41',
                 stdin=FakeStdin('n\n'))
        record = self.records()[0]
        self.assertEqual((record['status'], record['policy']['connection_type']), ('declined', 'production'))
        saved = self.store().find_profile('prod')
        scenario = KillScenario()
        self.api(scenario, {'profile_id': saved['id'], 'host': '127.0.0.9', 'user': 'app', 'password': 'pw'})
        self.assertEqual(self.records()[0]['policy']['connection_type'], 'production')

    def trail(self):
        from myflames import dbops
        return dbops.AuditTrail(self.store(), 'kill', 'cli', connection={'host': '127.0.0.1'})

    def api(self, scenario, connection, **payload):
        from myflames.workspace_store import WorkspaceStore
        factory = FakeFactory(scenario.conn)
        workspace = type('Workspace', (), {'store': WorkspaceStore()})()
        body = dict({'connection': connection, 'connection_id': 41, 'mode': 'connection', 'confirmed': True,
                     'expect': bound_kill_expect(scenario.conn, connection=connection)},
                    **payload)
        if isinstance(body.get('expect'), dict):
            body['review_token'] = ui._review_tokens(workspace).issue('kill',
                ui._review_identity(connection, workspace), ui._kill_review_details(body))
        with mock.patch('myflames.connector.MySQLConnection', factory), mock.patch.object(locks.time, 'sleep'):
            return ui.api_response('/api/live/kill', body, workspace=workspace), factory

    def test_api_kill_is_recorded(self):
        scenario = KillScenario()
        result, _ = self.api(scenario, {'host': '127.0.0.1', 'port': 3306, 'user': 'app', 'password': PASSWORD})
        self.assertEqual(result['status'], 'killed')
        [record] = self.records()
        self.assertEqual((record['id'], record['status'], record['surface'], record['connection']['user']),
                         (result['audit_id'], 'ok', 'ui', 'app'))
        self.assertNotIn(PASSWORD.encode(), self.store().path.read_bytes())
        scenario = KillScenario()
        result, _ = self.api(scenario, {'host': '127.0.0.1', 'port': 3306, 'user': 'app'}, mode='query')
        self.assertEqual((result['status'], self.records()[0]['status'], self.records()[0]['operation']),
                         ('refused', 'refused', 'kill_query'))

    def test_api_read_only_refuses_without_connecting(self):
        saved = self.profile(read_only=True)
        for connection in ({'profile_id': saved['id'], 'host': '127.0.0.1', 'user': 'app'},
                           {'host': '127.0.0.1', 'user': 'app', 'read_only': True}):
            with self.subTest(connection=connection):
                scenario = KillScenario()
                result, factory = self.api(scenario, connection)
                self.assertIsNone(factory.options)
                self.assertEqual((result['status'], result['statement'], scenario.kills()), ('refused', None, []))
                self.assertIn('is read-only in myflames', result['message'])
                self.assertEqual((self.records()[0]['status'], self.records()[0]['surface']), ('refused', 'ui'))
        # Bad input is still an error, before the policy and without a record.
        count = len(self.records())
        with self.assertRaises(ValueError):
            self.api(KillScenario(), {'profile_id': saved['id']}, expect=None)
        self.assertEqual(len(self.records()), count)

    def test_api_requires_literal_confirmation_before_opening_connection(self):
        connection = {'host': '127.0.0.1', 'port': 3306, 'user': 'app'}
        for fields in ({}, {'confirmed': False}, {'confirmed': None}, {'confirmed': 1}, {'confirmed': 'true'}):
            with self.subTest(fields=fields):
                scenario = KillScenario()
                body = dict(connection=connection, connection_id=41,
                            expect=bound_kill_expect(scenario.conn, connection=connection), **fields)
                workspace = type('Workspace', (), {'store': self.store()})()
                factory = FakeFactory(scenario.conn)
                with mock.patch('myflames.connector.MySQLConnection', factory):
                    result = ui._kill_response(body, workspace)
                self.assertEqual(result['status'], 'needs_confirmation')
                self.assertIsNone(factory.options)
                self.assertEqual(scenario.conn.calls, [])
                record = self.records()[0]
                self.assertEqual((record['status'], record['outcome']), ('refused', 'needs_confirmation'))
                self.assertTrue(record['finished_at'])

    def test_api_binds_confirmation_to_endpoint_and_policy(self):
        connection = {'host': '127.0.0.1', 'port': 3306, 'user': 'app'}
        for changed in ({'host': 'other'}, {'port': 3307}, {'user': 'other'}, {'ssl_mode': 'REQUIRED'},
                        {'ssl_ca': '/other.pem'}, {'profile_id': 'other'}, {'type': 'development'}):
            with self.subTest(changed=changed):
                scenario = KillScenario()
                expect = bound_kill_expect(scenario.conn, connection=dict(connection, **changed))
                result, factory = self.api(scenario, connection, expect=expect)
                self.assertEqual(result['status'], 'refused')
                self.assertIn('connection or its write policy changed', result['message'])
                self.assertIsNone(factory.options)
                self.assertEqual(self.records()[0]['outcome'], 'identity')

    def test_direct_kill_requires_confirmation_and_enforces_read_only(self):
        from myflames import dbops
        for read_only, confirmation in ((False, {}), (True, {}), (True, {'confirmed': True})):
            with self.subTest(read_only=read_only, confirmation=confirmation):
                scenario = KillScenario()
                audit = self.trail()
                audit.safety = dbops.connection_safety(connection={'read_only': read_only})
                # The low-level entry point must gate too; explicit confirmation
                # cannot override a read-only connection.
                result = locks.kill_session(scenario.conn, 41, expect=bound_kill_expect(scenario.conn),
                                            audit=audit, **confirmation)
                self.assertEqual(result['status'], 'refused' if read_only else 'needs_confirmation')
                self.assertEqual(scenario.conn.calls, [])
                self.assertEqual(self.records()[0]['status'], 'refused')

    def test_direct_kill_rejects_missing_or_changed_bindings(self):
        for fields in ({}, {'connection_identity': '[]'}, {'server_identity': {'server_uuid': 'new-server'}}):
            with self.subTest(fields=fields):
                scenario = KillScenario()
                expect = dict(K_EXPECT) if not fields else dict(bound_kill_expect(scenario.conn), **fields)
                result = locks.kill_session(scenario.conn, 41, expect=expect, confirmed=True, audit=self.trail())
                self.assertEqual((result['status'], scenario.kills()), ('refused', []))
                self.assertEqual(self.records()[0]['outcome'], 'identity')

    def test_mariadb_identity_checks_server_id_hostname_and_version(self):
        for key in ('server_id', 'hostname', 'version'):
            with self.subTest(key=key):
                scenario = KillScenario()
                scenario.conn.responses[0] = (VERSION_SQL, batch([['11.4.13-MariaDB', 1]]))
                expect = bound_kill_expect(scenario.conn)
                expect['server_identity'][key] = 'different'
                result = locks.kill_session(scenario.conn, 41, expect=expect, confirmed=True, audit=self.trail())
                self.assertEqual((result['status'], scenario.kills()), ('refused', []))
                self.assertIn('server identity', result['message'])
                self.assertEqual(self.records()[0]['outcome'], 'identity')

    def test_mariadb_kill_with_matching_identity(self):
        scenario = KillScenario()
        scenario.conn.responses[0] = (VERSION_SQL, batch([['11.4.13-MariaDB', 1]]))
        result = locks.kill_session(scenario.conn, 41, expect=bound_kill_expect(scenario.conn),
                                    confirmed=True, audit=self.trail(), settle_s=0)
        self.assertEqual((result['status'], scenario.kills()), ('killed', [KILL_TAG + 'KILL CONNECTION 41']))
        self.assertEqual(self.records()[0]['target']['server_identity'],
                         {'server_id': '1', 'hostname': 'server-one', 'version': '11.4.13-MariaDB'})

    def test_cli_missing_server_identity_is_refused_and_audited_before_prompt(self):
        scenario = KillScenario()
        scenario.conn.responses = [(p, '' if p == 'SELECT @@server_uuid' else value)
                                   for p, value in scenario.conn.responses]
        stdin = mock.Mock()
        stdin.readline.side_effect = AssertionError('must refuse before prompting')
        code, out, err = self.cli(scenario, '--kill', '41', stdin=stdin)
        self.assertEqual(code, 2)
        self.assertIn('Could not read the server identity', err)
        self.assertEqual(scenario.kills(), [])
        self.assertEqual((self.records()[0]['status'], self.records()[0]['outcome']), ('refused', 'identity'))

    def test_direct_cli_read_only_gate_precedes_all_reads(self):
        from myflames import dbops
        scenario = KillScenario()
        audit = self.trail()
        audit.safety = dbops.connection_safety(connection={'read_only': True})
        code, result = locks.kill_cli(scenario.conn, 41, assume_yes=True, audit=audit)
        self.assertEqual((code, result['status']), (1, 'refused'))
        self.assertEqual(scenario.conn.calls, [])
        self.assertEqual(self.records()[0]['outcome'], 'read_only')

    def test_direct_interrupt_and_timeout_complete_pending_audit_as_unknown(self):
        for error in (KeyboardInterrupt(), ConnectorError('Query timed out after 12s')):
            with self.subTest(error=type(error).__name__):
                scenario = KillScenario(kill=error)
                audit = self.trail()
                with self.assertRaises(KeyboardInterrupt if isinstance(error, KeyboardInterrupt) else ValueError):
                    locks.kill_session(scenario.conn, 41, expect=bound_kill_expect(scenario.conn),
                                       confirmed=True, audit=audit)
                self.assertFalse(audit.pending)
                record = self.records()[0]
                self.assertEqual(record['status'], 'unknown')
                self.assertTrue(record['finished_at'])
                self.assertEqual(record['outcome'], 'interrupted' if isinstance(error, KeyboardInterrupt) else 'timeout')

    def test_cli_pre_prompt_refusals_are_audited(self):
        targets = [('gone', []), ('own', K_TARGET[:9] + [41]),
                   ('system', K_TARGET[:2] + ['system user'] + K_TARGET[3:]),
                   ('idle', K_TARGET), ('missing_host', K_TARGET[:3] + [None] + K_TARGET[4:])]
        for label, target in targets:
            with self.subTest(label=label):
                scenario = KillScenario(target=target)
                args = ['--kill', '41'] + (['--query'] if label == 'idle' else [])
                stdin = mock.Mock()
                stdin.isatty.return_value = True
                stdin.readline.side_effect = AssertionError('refuse before prompting')
                before = len(self.records())
                code, _, _ = self.cli(scenario, *args, stdin=stdin)
                self.assertEqual(code, 2)
                self.assertEqual(scenario.kills(), [])
                stdin.readline.assert_not_called()
                self.assertEqual(len(self.records()), before + 1)
                record = self.records()[0]
                self.assertEqual(record['status'], 'refused')
                self.assertTrue(record['finished_at'])

    def test_cli_refuses_a_server_switch_during_confirmation(self):
        scenario = KillScenario()
        reads = []
        def identity(sql):
            reads.append(sql)
            return batch([['server-uuid-1' if len(reads) <= 2 else 'replacement-server']])
        scenario.conn.responses = [(p, identity if p == 'SELECT @@server_uuid' else value)
                                   for p, value in scenario.conn.responses]
        code, out, err = self.cli(scenario, '--kill', '41', stdin=FakeStdin('yes\n'))
        self.assertEqual(code, 2)
        self.assertIn('server identity', err)
        self.assertEqual(scenario.kills(), [])
        self.assertEqual(self.records()[0]['outcome'], 'identity')




class TestLockScaleBudgets(unittest.TestCase):
    def test_metadata_owner_snapshot_reused_across_waiters(self):
        """MySQL and MariaDB share owner enumeration, including rotation and self-exclusion."""
        class CountedOwners(dict):
            enumerations = 0

            def items(self):
                self.enumerations += 1
                return super(CountedOwners, self).items()

        owners = CountedOwners([(11, ['a', 'b']), (22, ['c']), (33, ['d'])])
        empty = CountedOwners()
        group = {'owners': owners}
        selected = [(group, 4), ({'owners': empty}, 0)]
        cases = ((11, 0, ['c', 'd']), (22, 1, ['d', 'a', 'b']),
                 (44, 7, ['c', 'd', 'a', 'b']), (33, 2, ['a', 'b', 'c']))
        for owner, start, expected in cases:
            result = list(locks._mdl_blockers(selected, owner, start))
            self.assertEqual([lock for _, lock in result], expected)
            self.assertTrue(all(found is group for found, _ in result))
        self.assertEqual(owners.enumerations, 1, 'a shared blocker group is materialized once')
        self.assertEqual(empty.enumerations, 1, 'an empty cached owner list is reusable too')

    def report_state(self, engine='mysql'):
        return locks._Report(engine, '8.4.11' if engine == 'mysql' else '11.4.13-MariaDB',
                             (8, 4, 11) if engine == 'mysql' else (11, 4, 13), True, 5000, 'all')

    def row_edge(self, waiter, blocker):
        return {'requesting': {'connection_id': waiter, 'trx_id': None, 'status': 'WAITING'},
                'blocking': {'connection_id': blocker, 'trx_id': None, 'status': 'GRANTED'}}

    def test_deep_chain_iterative_and_explicit_depth_budget(self):
        for engine in ('mysql', 'mariadb'):
            state = self.report_state(engine)
            rows = [self.row_edge(i + 1, i) for i in range(1, 1201)]
            tree, roots, nodes = locks._build_tree(state, rows, [], {})
            self.assertEqual((len(nodes), roots[0]['blocked_count']), (1201, 1200))
            json.dumps(tree)  # Default representation must remain serializable.
            last = tree[0]
            depth = 0
            while last['blocked']:
                last = last['blocked'][0]
                depth += 1
            self.assertEqual(depth, locks.MAX_TREE_DEPTH)
            self.assertEqual(last['omitted_branches'], 1)
            self.assertEqual(last['truncation_reason'], 'depth')
            # Prove the builder itself is independent of Python recursion depth.
            with mock.patch.object(locks, 'MAX_TREE_DEPTH', 2000):
                locks._build_tree(state, rows, [], {})
            self.assertEqual(state.tree_stats['nodes'], 1201)

    def test_node_edge_and_root_budgets(self):
        state = self.report_state()
        rows = [self.row_edge(i, 1) for i in range(2, 102)]
        with mock.patch.object(locks, 'MAX_TREE_NODES', 10):
            tree, _, _ = locks._build_tree(state, rows, [], {})
        self.assertEqual(state.tree_stats['nodes'], 10)
        self.assertEqual(tree[0]['omitted_branches'], 91)
        with mock.patch.object(locks, 'MAX_TREE_EDGES', 3):
            tree, _, _ = locks._build_tree(state, rows, [], {})
        self.assertEqual(state.tree_stats['edges'], 3)
        self.assertEqual(tree[0]['omitted_branches'], 97)
        rows = [self.row_edge(i + 100, i) for i in range(1, 51)]
        with mock.patch.object(locks, 'MAX_TREE_NODES', 10):
            tree, _, _ = locks._build_tree(state, rows, [], {})
        self.assertEqual(state.tree_stats['nodes'], 10)
        self.assertGreater(state.tree_stats['omitted_roots'], 0)

    def test_metadata_groups_cap_edges_and_exclude_own_owner(self):
        for engine in ('mysql', 'mariadb'):
            state = self.report_state(engine)
            accesses = [0]
            class Lock(dict):
                def __getitem__(self, key):
                    accesses[0] += 1
                    return super(Lock, self).__getitem__(key)
            def lock(owner, status, kind):
                return Lock(object_type='TABLE', schema='s', name='t', lock_type=kind,
                            status=status, ps_thread_id=owner, connection_id=owner,
                            duration='TRANSACTION', short=kind)
            state.mdl_locks = ([lock(i, 'GRANTED', 'SHARED_READ') for i in range(1, 3001)] +
                               [lock(i, 'PENDING', 'EXCLUSIVE') for i in range(3000, 4500)])
            locks._compute_mdl_waits(state)
            self.assertEqual(state.mdl_edge_total, 3000 * 1500 - 1)
            self.assertEqual(sum(len(w['blocked_by']) for w in state.mdl_waits), 5000)
            self.assertEqual(len(state.mdl_groups), 2)
            self.assertEqual(state.mdl_waits[0]['blockers_total'], 2999)
            self.assertTrue(state.mdl_waits[-1]['truncated'])
            # The budget is shared round-robin: 1,500 requests get 3 or 4 edges each.
            self.assertEqual(state.mdl_waits[-1]['blockers_omitted'], 2997)
            self.assertEqual(min(len(w['blocked_by']) for w in state.mdl_waits), 3)
            self.assertLess(accesses[0], 200000, 'must not scan every lock per pending lock')
            payload = locks._assemble(state, None, False)
            self.assertTrue(payload['metadata_locks']['waits_truncated'])
            self.assertEqual(payload['metadata_locks']['total_edges'], 4499999)
            self.assertEqual(payload['metadata_locks']['materialized_edges'], 5000)
            json.dumps(payload)

    def test_metadata_budget_never_makes_a_waiting_session_a_root(self):
        """Live on MySQL 8.4.11 and MariaDB 11.4.13: 100 sessions holding SHARED_READ in open
        transactions, 60 waiting LOCK TABLES ... WRITE (SHARED_NO_READ_WRITE) and 150 readers queued
        behind them make 15,000 edges. Spending the 5,000-edge budget in list order gave readers'
        queue edges first, so 43 writers had no edge to the holders and showed as root blockers."""
        for engine in ('mysql', 'mariadb'):
            state = self.report_state(engine)

            def lock(owner, status, kind):
                return dict(object_type='TABLE', schema='s', name='t', lock_type=kind, status=status,
                            ps_thread_id=owner, connection_id=owner, duration='TRANSACTION', short=kind,
                            source=None)
            # Readers first, as the ORDER BY LOCK_STATUS read returned them.
            state.mdl_locks = ([lock(i, 'PENDING', 'SHARED_READ') for i in range(1000, 1150)] +
                               [lock(i, 'PENDING', 'SHARED_NO_READ_WRITE') for i in range(500, 560)] +
                               [lock(i, 'GRANTED', 'SHARED_READ') for i in range(1, 101)])
            locks._compute_mdl_waits(state)
            self.assertEqual(state.mdl_edge_total, 60 * 100 + 150 * 60)
            self.assertEqual(sum(len(w['blocked_by']) for w in state.mdl_waits), locks.MAX_MDL_EDGES)
            self.assertTrue(all(w['blocked_by'] for w in state.mdl_waits), 'every request gets an edge')
            writers = [w for w in state.mdl_waits if w['waiting']['lock_type'] == 'SHARED_NO_READ_WRITE']
            self.assertTrue(all(b['reason'] == 'granted' for w in writers for b in w['blocked_by']))
            _tree, roots, _nodes = locks._build_tree(state, [], state.mdl_waits, {})
            # Only holders are roots, and the shared budget reaches every one of them.
            self.assertEqual({n['connection_id'] for n in roots}, set(range(1, 101)))

    def test_row_waits_read_granted_blockers_first(self):
        """InnoDB lists a waiter as waiting for every request queued ahead of it: 400 sessions on
        one row gave 80,200 rows (400 on the granted lock), live on MySQL 8.4.11 and MariaDB
        11.4.13. An unordered LIMIT showed the holder blocking 32 sessions (MySQL) or made 63
        waiting sessions root blockers (MariaDB)."""
        conn = FakeConn('8.4.11', [('data_lock_waits', '')])
        state = locks._Report('mysql', '8.4.11', (8, 4, 11), True, 500, 'all')
        locks._read_waits(conn, state)
        self.assertIn("ORDER BY b.LOCK_STATUS <> 'GRANTED' LIMIT 501", conn.calls[0][0])
        seen = []

        def waits(sql):
            seen.append(sql)
            if 'NOT IN' in sql:
                return 'w1\tw1:5\th\th:5\t`s`.`t`\tPRIMARY\tRECORD\tX\t1\t1\t`s`.`t`\tPRIMARY\tRECORD\tX\t1\t1\n'
            return 'w2\tw2:5\tw1\tw1:5\t`s`.`t`\tPRIMARY\tRECORD\tX\t1\t1\t`s`.`t`\tPRIMARY\tRECORD\tX\t1\t1\n'
        conn = FakeConn('11.4.13-MariaDB', [('trx_requested_lock_id FROM information_schema.INNODB_TRX',
                                             'w1:5\nw2:5\n'), ('INNODB_LOCK_WAITS', waits)])
        state = locks._Report('mariadb', '11.4.13-MariaDB', (11, 4, 13), True, 500, 'all')
        state.caps['process_privilege'] = True
        locks._read_waits(conn, state)
        self.assertIn("WHERE w.blocking_lock_id NOT IN (", seen[0])
        self.assertIn("WHERE w.blocking_lock_id IN (", seen[1])
        self.assertTrue(seen[0].endswith('LIMIT 501') and seen[1].endswith('LIMIT 500'))
        self.assertEqual([w['blocking']['status'] for w in state.waits], ['GRANTED', 'WAITING'])

    def test_lock_tsv_crlf_keeps_embedded_carriage_return(self):
        conn = FakeConn('8.4.11', [('SELECT', 'ON\tvalue\rinside\r\nNULL\tlast\r\n')])
        self.assertEqual(locks._q(conn, 'SELECT 1'), [['ON', 'value\rinside'], [None, 'last']])


if __name__ == '__main__':
    unittest.main()
