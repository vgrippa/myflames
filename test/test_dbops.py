"""The shared write safety layer (myflames/dbops.py): classification, the
confirmation policy, privilege hints, redaction, and the audit trail.

Every test that reaches the workspace database uses a temporary HOME and
MYFLAMES_SECRET_BACKEND=vault, never ~/.myflames or the OS keychain.
"""
import contextlib
import csv
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

from myflames import dbops, ui, workspace_cli
from myflames.workspace_store import WorkspaceStore


class TempHome(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.dict(os.environ, {'HOME': self.temp.name, 'USERPROFILE': self.temp.name,
                                               'MYFLAMES_SECRET_BACKEND': 'vault'})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        self.store = WorkspaceStore()


# ---- classification -----------------------------------------------------------------

# (sql, overall kind, category of the worst statement, verbs in order)
CASES = (
    # Reads.
    ('SELECT 1', 'read', 'read', ['SELECT']),
    ('select * from t where id = 1;', 'read', 'read', ['SELECT']),
    ('(SELECT 1) UNION (SELECT 2)', 'read', 'read', ['SELECT']),
    ('WITH d AS (SELECT id FROM t) SELECT * FROM d', 'read', 'read', ['WITH']),
    ('WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 5) SELECT * FROM c',
     'read', 'read', ['WITH']),
    ('TABLE t', 'read', 'read', ['TABLE']),
    ('VALUES ROW(1, 2)', 'read', 'read', ['VALUES']),
    ('SHOW FULL PROCESSLIST', 'read', 'read', ['SHOW']),
    ('DESC t', 'read', 'read', ['DESC']),
    ('DESCRIBE SELECT 1', 'read', 'read', ['DESCRIBE']),
    ('EXPLAIN FORMAT=JSON SELECT * FROM t', 'read', 'read', ['EXPLAIN']),
    ('EXPLAIN DELETE FROM t', 'read', 'read', ['EXPLAIN']),
    ('EXPLAIN FOR CONNECTION 42', 'read', 'read', ['EXPLAIN']),
    ("HELP 'select'", 'read', 'read', ['HELP']),
    ('USE shop', 'read', 'session', ['USE']),
    ('SELECT REPLACE(a, "x", "y"), INSERT(a, 1, 1, "z") FROM t', 'read', 'read', ['SELECT']),
    ('SELECT `where`, `delete` FROM `update`', 'read', 'read', ['SELECT']),
    ("SELECT 'DROP TABLE t; DELETE FROM u'", 'read', 'read', ['SELECT']),
    ('SELECT 1--1', 'read', 'read', ['SELECT']),
    ('SET NAMES utf8mb4', 'read', 'session', ['SET NAMES']),
    ("SET SESSION time_zone = '+00:00', max_execution_time = 1000", 'read', 'session', ['SET SESSION']),
    ('SET TRANSACTION ISOLATION LEVEL READ COMMITTED', 'read', 'session', ['SET TRANSACTION']),
    ('START TRANSACTION READ ONLY', 'read', 'transaction', ['START TRANSACTION']),
    ('BEGIN', 'read', 'transaction', ['BEGIN']),
    ('ROLLBACK', 'read', 'transaction', ['ROLLBACK']),
    ('UNLOCK TABLES', 'read', 'lock', ['UNLOCK TABLES']),
    ('XA RECOVER', 'read', 'transaction', ['XA RECOVER']),
    ("PREPARE s FROM 'SELECT 1'", 'read', 'session', ['PREPARE']),
    ('CHECKSUM TABLE t', 'read', 'read', ['CHECKSUM']),
    # Comments hide nothing and add nothing.
    ('/* DELETE FROM t */ SELECT 1', 'read', 'read', ['SELECT']),
    ('SELECT 1 -- ; DROP TABLE t', 'read', 'read', ['SELECT']),
    ('SELECT 1 # ; DROP TABLE t', 'read', 'read', ['SELECT']),
    ('SELECT /*+ MAX_EXECUTION_TIME(10) */ 1', 'read', 'read', ['SELECT']),
    # Writes.
    ('INSERT INTO t VALUES (1)', 'write', 'dml', ['INSERT']),
    ('REPLACE INTO t VALUES (1)', 'write', 'dml', ['REPLACE']),
    # UPDATE and DELETE with a WHERE are risky unless the caller passes key_columns (TestClassify).
    ('UPDATE t SET a = 1 WHERE id = 3', 'risky', 'dml', ['UPDATE']),
    ('DELETE FROM t WHERE id IN (SELECT id FROM u WHERE x = 1) LIMIT 10', 'risky', 'dml', ['DELETE']),
    ('WITH d AS (SELECT id FROM t) DELETE FROM t WHERE id IN (SELECT id FROM d)', 'risky', 'dml', ['WITH']),
    ('SELECT * FROM t WHERE id = 1 FOR UPDATE', 'write', 'locking_read', ['SELECT']),
    ('SELECT * FROM t FOR SHARE SKIP LOCKED', 'write', 'locking_read', ['SELECT']),
    ('SELECT * FROM t LOCK IN SHARE MODE', 'write', 'locking_read', ['SELECT']),
    ("SELECT * INTO OUTFILE '/tmp/t.csv' FROM t", 'write', 'file', ['SELECT']),
    ("SELECT a FROM t INTO DUMPFILE '/tmp/a'", 'write', 'file', ['SELECT']),
    ('SELECT a INTO @x FROM t LIMIT 1', 'write', 'session', ['SELECT']),
    ('SELECT @n := @n + 1 FROM t', 'write', 'session', ['SELECT']),
    ("SELECT GET_LOCK('job', 10)", 'write', 'lock', ['SELECT']),
    ('SELECT NEXTVAL(s)', 'write', 'sequence', ['SELECT']),
    ('SET @x = 1', 'write', 'session', ['SET']),
    ('SET autocommit = 0', 'write', 'session', ['SET']),
    ('SET SESSION sql_mode = ""', 'write', 'session', ['SET SESSION']),
    ('SET ROLE ALL', 'write', 'session', ['SET ROLE']),
    ('COMMIT', 'write', 'transaction', ['COMMIT']),
    ("XA START 'x'", 'write', 'transaction', ['XA START']),
    ('CREATE TEMPORARY TABLE x (a INT)', 'write', 'ddl_temporary', ['CREATE TABLE']),
    ('ANALYZE TABLE t', 'write', 'maintenance', ['ANALYZE TABLE']),
    ('CHECK TABLE t', 'write', 'maintenance', ['CHECK']),
    ('FLUSH STATUS', 'write', 'server', ['FLUSH STATUS']),
    ('ANALYZE FORMAT=JSON INSERT INTO t VALUES (1)', 'write', 'dml', ['ANALYZE']),
    ('SET SESSION transaction_read_only = OFF', 'write', 'session', ['SET SESSION']),
    ('SET TRANSACTION READ WRITE', 'write', 'session', ['SET TRANSACTION']),
    ('START TRANSACTION READ WRITE', 'write', 'transaction', ['START TRANSACTION']),
    # Risky writes.
    ('DELETE FROM t', 'risky', 'dml', ['DELETE']),
    ('DELETE FROM t LIMIT 10', 'risky', 'dml', ['DELETE']),
    ('UPDATE t SET a = (SELECT b FROM u WHERE u.id = 1)', 'risky', 'dml', ['UPDATE']),
    ('UPDATE t SET a = 1 WHERE 1 = 1', 'risky', 'dml', ['UPDATE']),
    ('DELETE FROM t WHERE TRUE', 'risky', 'dml', ['DELETE']),
    ('UPDATE `t` SET `where` = 1', 'risky', 'dml', ['UPDATE']),
    ('EXPLAIN ANALYZE DELETE FROM t', 'risky', 'dml', ['EXPLAIN ANALYZE']),
    ('TRUNCATE TABLE t', 'risky', 'ddl', ['TRUNCATE']),
    ('CREATE TABLE x (a INT)', 'risky', 'ddl', ['CREATE TABLE']),
    ('CREATE TABLE x AS SELECT * FROM t', 'risky', 'ddl', ['CREATE TABLE']),
    ('ALTER TABLE t ADD INDEX (a), ALGORITHM=INPLACE, LOCK=NONE', 'risky', 'ddl', ['ALTER TABLE']),
    ('DROP DATABASE shop', 'risky', 'ddl', ['DROP DATABASE']),
    ('RENAME TABLE a TO b', 'risky', 'ddl', ['RENAME TABLE']),
    ('OPTIMIZE TABLE t', 'risky', 'maintenance', ['OPTIMIZE']),
    ('KILL 41', 'risky', 'kill', ['KILL']),
    ('KILL CONNECTION 41', 'risky', 'kill', ['KILL']),
    ('KILL QUERY 41', 'risky', 'kill', ['KILL QUERY']),
    ('SET GLOBAL max_connections = 500', 'risky', 'global', ['SET GLOBAL']),
    ('SET @@GLOBAL.max_connections = 500', 'risky', 'global', ['SET GLOBAL']),
    ('SET PERSIST innodb_io_capacity = 2000', 'risky', 'persist', ['SET PERSIST']),
    ('SET @@persist_only.innodb_log_file_size = 1', 'risky', 'persist', ['SET PERSIST_ONLY']),
    ('RESET PERSIST innodb_io_capacity', 'risky', 'persist', ['RESET PERSIST']),
    ('SET GLOBAL TRANSACTION ISOLATION LEVEL READ COMMITTED', 'risky', 'global', ['SET GLOBAL']),
    ('SET sql_log_bin = 0', 'risky', 'replication', ['SET']),
    ('SET SESSION gtid_next = "AUTOMATIC"', 'risky', 'replication', ['SET SESSION']),
    ('SET @x = 1, GLOBAL read_only = ON', 'risky', 'global', ['SET']),
    ('START REPLICA', 'risky', 'replication', ['START REPLICA']),
    ('STOP SLAVE', 'risky', 'replication', ['STOP SLAVE']),
    ('START ALL SLAVES', 'risky', 'replication', ['START ALL']),
    ("CHANGE REPLICATION SOURCE TO SOURCE_HOST = 'h'", 'risky', 'replication', ['CHANGE REPLICATION']),
    ("CHANGE MASTER TO MASTER_HOST = 'h'", 'risky', 'replication', ['CHANGE MASTER']),
    ('RESET MASTER', 'risky', 'replication', ['RESET MASTER']),
    ("PURGE BINARY LOGS TO 'binlog.000010'", 'risky', 'replication', ['PURGE BINARY']),
    ("CLONE INSTANCE FROM 'u'@'h':3306 IDENTIFIED BY 'x'", 'risky', 'clone', ['CLONE']),
    ('GRANT SELECT ON shop.* TO app', 'risky', 'account', ['GRANT']),
    ('REVOKE ALL ON *.* FROM app', 'risky', 'account', ['REVOKE']),
    ("CREATE USER u IDENTIFIED BY 'x'", 'risky', 'account', ['CREATE USER']),
    ('DROP USER u', 'risky', 'account', ['DROP USER']),
    ("SET PASSWORD FOR u = 'x'", 'risky', 'account', ['SET PASSWORD']),
    ("LOAD DATA INFILE '/tmp/t.csv' INTO TABLE t", 'risky', 'bulk', ['LOAD DATA']),
    ('LOCK TABLES t WRITE', 'risky', 'lock', ['LOCK TABLES']),
    ('FLUSH TABLES WITH READ LOCK', 'risky', 'server', ['FLUSH TABLES']),
    ("XA COMMIT 'x'", 'risky', 'transaction', ['XA COMMIT']),
    ('CALL refresh_totals()', 'risky', 'unknown', ['CALL']),
    ('EXECUTE s', 'risky', 'unknown', ['EXECUTE']),
    ("EXECUTE IMMEDIATE 'DELETE FROM t'", 'risky', 'unknown', ['EXECUTE']),
    ('SHUTDOWN', 'risky', 'server', ['SHUTDOWN']),
    ('DO SLEEP(1)', 'risky', 'unknown', ['DO']),
    ('DO my_db.cleanup()', 'risky', 'unknown', ['DO']),
    ('ANALYZE FORMAT=JSON DELETE FROM t WHERE id = 1', 'risky', 'dml', ['ANALYZE']),
    ('SET skip_replication = 1', 'risky', 'replication', ['SET']),
    ('SET SESSION wsrep_on = OFF', 'risky', 'replication', ['SET SESSION']),
    ('FROBNICATE t', 'risky', 'unknown', ['FROBNICATE']),
    ("SELECT 'unclosed", 'risky', 'unknown', ['SELECT']),
    ('SET STATEMENT max_statement_time = 1 FOR DELETE FROM t', 'risky', 'dml', ['SET STATEMENT']),
    # Executable comments are code.
    ('/*!50000 DROP TABLE t */', 'risky', 'ddl', ['DROP TABLE']),
    ('/*!DROP TABLE t*/', 'risky', 'ddl', ['DROP TABLE']),
    ('/*!100100 DELETE FROM t */', 'risky', 'dml', ['DELETE']),
    ('/*M!100100 DELETE FROM t */', 'risky', 'dml', ['DELETE']),
    ('SELECT 1 /*!50000 , GET_LOCK("x", 1) */', 'write', 'lock', ['SELECT']),
    # Several statements: the worst counts.
    ('SELECT 1; DROP TABLE t', 'risky', 'ddl', ['SELECT', 'DROP TABLE']),
    ('SELECT 1 # x\n; DELETE FROM t', 'risky', 'dml', ['SELECT', 'DELETE']),
    ('SELECT 1 /*!50000 ; DROP TABLE t */', 'risky', 'ddl', ['SELECT', 'DROP TABLE']),
    ("PREPARE s FROM 'DROP TABLE t'; EXECUTE s", 'risky', 'unknown', ['PREPARE', 'EXECUTE']),
    ('INSERT INTO t VALUES (1); SELECT 1', 'write', 'dml', ['INSERT', 'SELECT']),
    ('DELIMITER //\nCREATE PROCEDURE p() BEGIN UPDATE t SET a = 1; END//\nDELIMITER ;\nCALL p();',
     'risky', 'ddl', ['CREATE PROCEDURE', 'CALL']),
    ('', 'read', 'read', []),
    ('-- only a comment\n/* and another */', 'read', 'read', []),
)


class TestSelectFunctionSafety(unittest.TestCase):
    def test_administrative_and_unknown_calls_require_review_in_both_types(self):
        # MySQL administrative UDFs and either engine's stored functions can
        # perform work from SELECT. No connection needs opening to gate them.
        queries = (
            'SELECT group_replication_switch_to_multi_primary_mode()',
            'SELECT asynchronous_connection_failover_reset()',
            "SELECT keyring_key_generate('key', 'AES', 32)",
            "SELECT service_get_write_locks('namespace', 0, 'lock')",
            'SELECT mutate_rows()', 'SELECT mutar_ação()', 'SELECT app.mutate_rows()',
            'SELECT app.COUNT(1)', 'SELECT `app`.`mutate_rows`()',
            'SELECT `COUNT`(1)', 'SELECT "app"."mutate_rows"()',
            'SELECT "mutate_rows"()', 'SELECT mutate_rows /* comment */ ()',
            'SELECT COALESCE(mutate_rows(), 0)',
            'WITH c AS (SELECT mutate_rows()) SELECT * FROM c',
            'SELECT 1 /*!50000 , mutate_rows() */',
            'SELECT 1 /*M! , mutate_rows() */',
            'SELECT JSON_TABLE(doc, "$[*]" COLUMNS (n INT PATH "$")) FROM t',
        )
        for sql in queries:
            with self.subTest(sql=sql):
                verdict = dbops.classify(sql)
                self.assertEqual(verdict['kind'], 'risky')
                for connection_type in ('production', 'development'):
                    decision = dbops.decide(verdict, connection_type)
                    self.assertEqual(decision['action'], 'confirm')
                    self.assertEqual(decision['tone'], 'danger')
                    self.assertEqual(dbops.decide(verdict, connection_type, True)['action'], 'refuse')

    def test_waits_and_uuid_generators_are_reads(self):
        # These native functions exist on both supported engines. Duration and
        # nondeterminism do not make a SELECT a write; nested writes still gate.
        for sql in ('SELECT SLEEP(30)', 'SELECT SLEEP(0)', 'SELECT SLEEP(NULL)',
                    'SELECT UUID(), UUID_SHORT()', 'SELECT sleep /* delay */ (0)',
                    'WITH c AS (SELECT UUID()) SELECT * FROM c'):
            with self.subTest(sql=sql):
                verdict = dbops.classify(sql)
                self.assertEqual(verdict['kind'], 'read')
                for connection_type in ('production', 'development'):
                    for read_only in (False, True):
                        self.assertEqual(dbops.decide(verdict, connection_type, read_only)['action'], 'allow')
        for sql in ("SELECT BENCHMARK(100, ABS(1))", 'SELECT SLEEP(mutate_rows())',
                    'SELECT app.UUID()', 'SELECT `SLEEP`(1)',
                    "SELECT UUID(), GET_LOCK('n',0)", "SELECT UUID_SHORT(), RELEASE_LOCK('n')"):
            with self.subTest(sql=sql):
                verdict = dbops.classify(sql)
                self.assertNotEqual(verdict['kind'], 'read')
                self.assertEqual(dbops.decide(verdict, 'production')['action'], 'confirm')

    def test_builtin_confirmation_reasons_describe_the_actual_effect(self):
        cases = (
            ('SELECT BENCHMARK(100, ABS(1))', 'risky', 'unknown',
             'BENCHMARK runs an expression many times and can load the server.'),
            ("SELECT GET_LOCK('n', 0)", 'write', 'lock',
             'This built-in takes or releases a named user lock.'),
            ("SELECT RELEASE_LOCK('n')", 'write', 'lock',
             'This built-in takes or releases a named user lock.'),
            ('SELECT RELEASE_ALL_LOCKS()', 'write', 'lock',
             'This built-in takes or releases a named user lock.'),
        )
        for sql, kind, category, reason in cases:
            with self.subTest(sql=sql):
                verdict = dbops.classify(sql)
                self.assertEqual((verdict['kind'], verdict['category']), (kind, category))
                self.assertEqual(verdict['reasons'], [reason])
        for sql in ('SELECT app.BENCHMARK(1, 2)', 'SELECT `BENCHMARK`(1, 2)',
                    'SELECT mutate_rows()', 'SELECT BENCHMARK(1, mutate_rows())'):
            with self.subTest(sql=sql):
                self.assertTrue(any('not a verified native reader' in reason
                                    for reason in dbops.classify(sql)['reasons']))

    def test_common_native_readers_and_sql_parentheses_remain_reads(self):
        queries = (
            'SELECT COUNT(*), COALESCE(SUM(x),0), AVG(x), MIN(x), MAX(x) FROM t',
            'SELECT CONCAT(LOWER(name), UPPER(name)), LENGTH(name), ABS(n), ROUND(n,2) FROM t',
            'SELECT ABS /* a native builder accepts spaces */ (1)',
            "SELECT JSON_EXTRACT(doc, '$.name'), JSON_OBJECT('n', 1), JSON_ARRAY(1) FROM t",
            "SELECT DATE_FORMAT(NOW(), '%Y'), YEAR(CURDATE()), DATE_ADD(NOW(), INTERVAL 1 DAY)",
            'SELECT CAST(id AS DECIMAL(10,2)), CONVERT(id, DECIMAL(10,2)) FROM t',
            'SELECT ROW_NUMBER() OVER (PARTITION BY x ORDER BY id), LAG(x) OVER (ORDER BY id) FROM t',
            'SELECT * FROM t WHERE id IN (1,2) AND (x=1 OR (y=2)) AND EXISTS (SELECT 1 FROM u)',
            'WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c WHERE n<5) SELECT * FROM c',
            'WITH c(n) AS (SELECT 1), d(m) AS (SELECT 2) SELECT * FROM c JOIN d USING (n)',
            'SELECT * FROM (SELECT 1) AS t(n)',
            'SELECT * FROM (SELECT 1) `t`(n)',
            'WITH `c`(`n`) AS (SELECT 1) SELECT * FROM `c`',
            'SELECT * FROM t USE INDEX (idx)',
            "SELECT 'mutate_rows()', `mutate_rows`, 'COUNT (1)' /* mutate_rows() */",
            'SELECT CONNECTION_ID(), USER(), CURRENT_USER(), VERSION(), DATABASE()',
            'SELECT LAST_INSERT_ID()',
        )
        for sql in queries:
            with self.subTest(sql=sql):
                self.assertEqual(dbops.classify(sql)['kind'], 'read')

    def test_space_sensitive_native_names_are_not_assumed_native(self):
        # Without IGNORE_SPACE these names can resolve differently. Comments
        # also break adjacency. The generic native ABS builder is tested above.
        for sql in ('SELECT COUNT (1)', 'SELECT SUM /* comment */ (x) FROM t',
                    'SELECT NOW\n()', 'SELECT `ABS`(1)'):
            with self.subTest(sql=sql):
                self.assertEqual(dbops.classify(sql)['kind'], 'risky')

    def test_sequences_and_session_functions_are_writes(self):
        # MariaDB sequences have both function and expression syntax.
        for sql in ('SELECT NEXTVAL(s)', 'SELECT SETVAL(s,42)', 'SELECT NEXT VALUE FOR s',
                    "SELECT GET_LOCK('n',0)", "SELECT RELEASE_LOCK('n')", 'SELECT RELEASE_ALL_LOCKS()',
                    'SELECT LAST_INSERT_ID(42)'):
            with self.subTest(sql=sql):
                result = dbops.classify(sql)
                self.assertEqual(result['kind'], 'write')
                self.assertEqual(dbops.decide(result, 'production', True)['action'], 'refuse')
        for sql in ('SELECT s.NEXTVAL', 'SELECT db.s.nextval', 'SELECT `s`.`NEXTVAL`',
                    'SELECT "s"."NEXTVAL"'):
            with self.subTest(sql=sql):
                self.assertEqual(dbops.classify(sql)['kind'], 'risky')


class TestClassify(unittest.TestCase):
    maxDiff = None

    def test_table(self):
        for sql, kind, category, verbs in CASES:
            with self.subTest(sql=sql):
                result = dbops.classify(sql)
                self.assertEqual((result['kind'], result['category'], [s['verb'] for s in result['statements']]),
                                 (kind, category, verbs))
                self.assertEqual(result['count'], len(verbs))
                if kind != 'read':
                    self.assertTrue(result['reasons'], 'a write always says why')
                json.dumps(result, allow_nan=False)

    def test_statement_text_and_offsets(self):
        sql = '  SELECT 1 ;\n/* c */ DELETE FROM t WHERE id = 2;  '
        statements = dbops.classify(sql)['statements']
        self.assertEqual([s['text'] for s in statements], ['SELECT 1', '/* c */ DELETE FROM t WHERE id = 2'])
        for s in statements:
            self.assertEqual(sql[s['start']:s['end']], s['text'])
        self.assertEqual([s['kind'] for s in statements], ['read', 'risky'])

    def test_delimiter_lines_are_not_statements(self):
        sql = 'DELIMITER $$\nCREATE TRIGGER tr BEFORE INSERT ON t FOR EACH ROW BEGIN SET NEW.a = 1; END$$\n' \
              'DELIMITER ;\nSELECT 1;'
        parts = dbops.split_statements(sql)
        self.assertEqual([p['text'] for p in parts],
                         ['CREATE TRIGGER tr BEFORE INSERT ON t FOR EACH ROW BEGIN SET NEW.a = 1; END', 'SELECT 1'])
        self.assertEqual([p['delimiter'] for p in parts], ['$$', ';'])
        # DELIMITER inside a statement or a string is text, not a command.
        self.assertEqual(len(dbops.split_statements("SELECT 'x'\nDELIMITER //")), 1)
        self.assertEqual([p['text'] for p in dbops.split_statements("SELECT 'DELIMITER //'; SELECT 2")],
                         ["SELECT 'DELIMITER //'", 'SELECT 2'])

    def test_quoted_identifiers_are_names_not_keywords(self):
        part = dbops.split_statements('UPDATE `t` SET `where` = 1 WHERE `my id` = 2')[0]
        self.assertEqual(part['idents'], ['t', 'where', 'my id'])
        result = dbops.classify('UPDATE `t` SET `where` = 1 WHERE `my id` = 2', key_columns=['my id'])
        self.assertEqual(result['kind'], 'write')

    def test_both_backslash_readings(self):
        # With backslash escapes this is one SELECT; under NO_BACKSLASH_ESCAPES
        # the string ends at the backslash and DROP TABLE is a statement.
        sql = "SELECT 'a\\'; DROP TABLE t; SELECT \\''"
        result = dbops.classify(sql)
        self.assertTrue(result['ambiguous'])
        self.assertEqual(result['kind'], 'risky')
        self.assertIn(dbops.AMBIGUOUS_REASON, result['reasons'])
        self.assertEqual(dbops.classify(sql, backslash_escapes=True)['kind'], 'read')
        self.assertEqual(dbops.classify(sql, backslash_escapes=False)['kind'], 'risky')
        self.assertFalse(dbops.classify("SELECT 'it''s'; SELECT 2")['ambiguous'])

    def test_key_columns_decide_whether_where_is_risky(self):
        self.assertEqual(dbops.classify('DELETE FROM t WHERE id = 5', key_columns=['id'])['kind'], 'write')
        self.assertEqual(dbops.classify('DELETE FROM t WHERE t.ID IN (1, 2)', key_columns=['id'])['kind'], 'write')
        self.assertEqual(dbops.classify('UPDATE t SET a = 1 WHERE 5 = id', key_columns=['id'])['kind'], 'write')
        risky = dbops.classify('UPDATE t SET a = 1 WHERE status = "x"', key_columns=['id'])
        self.assertEqual(risky['kind'], 'risky')
        self.assertIn('pins no key column (id) to a value', risky['reasons'][0])
        # Without key information Development must confirm too (it may change many rows).
        unknown = dbops.classify('UPDATE t SET a = 1 WHERE id = 5')
        self.assertEqual(unknown['kind'], 'risky')
        self.assertIn('could not check whether WHERE pins a key column', unknown['reasons'][0])
        self.assertEqual(dbops.decide(unknown, 'development')['action'], 'confirm')

    def test_bad_input(self):
        for value in (None, 5, b'SELECT 1'):
            with self.assertRaises(ValueError):
                dbops.classify(value)
        with self.assertRaises(ValueError):
            dbops.classify('x' * 1000001)


# ---- review gate regressions (2026-10-14) -------------------------------------------------

class TestReviewGate(unittest.TestCase):
    """Probes from the review of the safety foundation, one per finding."""

    def kind(self, sql, **kw):
        return dbops.classify(sql, **kw)['kind']

    def test_1_set_names_is_one_item_of_the_list(self):
        for sql in ('SET NAMES utf8mb4, GLOBAL read_only = 0', 'SET NAMES utf8mb4, sql_log_bin = 0',
                    'SET CHARACTER SET utf8mb4, PERSIST max_connections = 1',
                    'SET NAMES latin1, @@global.super_read_only = OFF', 'SET CHARSET utf8mb4, @@persist_only.x = 1'):
            with self.subTest(sql=sql):
                result = dbops.classify(sql)
                self.assertEqual(result['kind'], 'risky')
                self.assertEqual(dbops.decide(result, 'production', True)['action'], 'refuse')
        self.assertEqual(self.kind("SET NAMES 'utf8mb4' COLLATE 'utf8mb4_bin'"), 'read')
        self.assertEqual(self.kind('SET NAMES utf8mb4, @x = 1'), 'write')

    def test_2_quoted_delimiter_argument(self):
        for sql in ("DELIMITER '//'\nSELECT 1//\nDROP TABLE t//", 'DELIMITER "$$"\nSELECT 1$$\nDROP TABLE t$$',
                    'DELIMITER `;;`\nSELECT 1;;\nDROP TABLE t;;', 'DELIMITER //\tx\nSELECT 1//\tx DROP TABLE t//\tx',
                    'delimiterX //\nSELECT 1//DROP TABLE t//'):
            with self.subTest(sql=sql):
                parts = dbops.split_statements(sql)
                self.assertEqual([p['text'] for p in parts], ['SELECT 1', 'DROP TABLE t'])
                self.assertEqual(self.kind(sql), 'risky')
        # An unquoted argument ends at a space; the client keeps the old
        # delimiter when it has a backslash or is missing.
        self.assertEqual(dbops.split_statements('DELIMITER // ignored\nSELECT 1//')[0]['delimiter'], '//')
        for sql in ('DELIMITER /\\/\nSELECT 1/\\/', 'DELIMITER\nSELECT 1', "DELIMITER ''\nSELECT 1"):
            with self.subTest(sql=sql):
                result = dbops.classify(sql)
                self.assertEqual(result['kind'], 'risky')
                self.assertIn(dbops.REASON_DELIMITER, result['reasons'])

    def test_3_semicolons_inside_a_custom_delimiter_statement(self):
        result = dbops.classify('DELIMITER //\nSELECT 1; DROP TABLE t//')
        self.assertEqual((result['kind'], result['count']), ('risky', 1))
        self.assertIn(dbops.REASON_MULTI, result['reasons'])
        self.assertEqual(dbops.decide(result, 'production', True)['action'], 'refuse')
        self.assertEqual(self.kind('DELIMITER //\nSELECT 1; SELECT 2//'), 'read')
        self.assertEqual(self.kind("DELIMITER //\nSELECT ';' ; SHOW TABLES//"), 'read')

    def test_4_dash_comments_need_client_white_space(self):
        for dash in ('\x1c', '\x1d', '\x1e', '\x1f', '\x01', '\x7f'):
            with self.subTest(dash=repr(dash)):
                result = dbops.classify('SELECT 1 --%s; DROP TABLE t' % dash)
                self.assertEqual(result['kind'], 'risky')
                self.assertIn(dbops.REASON_DASH_CONTROL, result['reasons'])
        # NBSP is not white space to the client: '--\xa0' is code and ';' splits.
        result = dbops.classify('SELECT 1 --\xa0; DROP TABLE t')
        self.assertEqual((result['kind'], result['count']), ('risky', 2))
        self.assertEqual(self.kind('SELECT 1 --\t; DROP TABLE t'), 'read')
        self.assertEqual(self.kind('SELECT 1 --\v; DROP TABLE t'), 'read')

    def test_5_client_backslash_commands(self):
        parts = dbops.split_statements('SELECT 1\\gDROP TABLE t\\G')
        self.assertEqual([p['text'] for p in parts], ['SELECT 1', 'DROP TABLE t'])
        self.assertEqual(self.kind('SELECT 1\\gDROP TABLE t'), 'risky')
        for command in ('!', '.', 'r', 'u', 'd', 'C', 'q', 'T', 'P'):
            sql = 'SELECT 1\\%s x' % command
            with self.subTest(sql=sql):
                result = dbops.classify(sql)
                self.assertEqual(result['kind'], 'risky')
                self.assertIn(dbops.REASON_CLIENT_COMMAND % command, result['reasons'])
        # Inside strings and comments a backslash is no command; \N is NULL.
        for sql in ("SELECT 'a\\\\gb'", 'SELECT 1 /* \\! x */', 'SELECT 1 -- \\! x', 'SELECT \\N'):
            with self.subTest(sql=sql):
                self.assertEqual(self.kind(sql), 'read')
        self.assertEqual(dbops.WRITE_CLIENT_FLAGS, ('--comments', '--binary-mode'))

    def test_6_requests_cannot_lower_the_protection(self):
        self.assertEqual(dbops.connection_safety(None, {'type': 'development'})['type'], 'production')
        dev = {'id': 'p1', 'name': 'dev', 'type': 'development', 'read_only': False, 'host': 'dev.db', 'port': 3306,
               'user': 'app'}
        same = {'profile_id': 'p1', 'host': 'dev.db', 'port': '3306', 'user': 'app', 'password': 'x'}
        self.assertEqual(dbops.connection_safety(dev, same)['type'], 'development')
        for change in ({'host': 'prod.db'}, {'port': 3307}, {'user': 'root'}, {'ssl_mode': 'DISABLED'}, {'port': 'x'}):
            with self.subTest(change=change):
                safety = dbops.connection_safety(dev, dict(same, **change))
                self.assertEqual((safety['type'], safety['endpoint_matches']), ('production', False))
        locked = dbops.connection_safety(dict(dev, read_only=True), dict(same, host='prod.db'))
        self.assertTrue(locked['read_only'])

    def test_7_set_statement_variables_count(self):
        for variable in ('sql_log_bin=0', 'gtid_domain_id=9', 'server_id=7', 'pseudo_thread_id=3'):
            sql = 'SET STATEMENT %s FOR INSERT INTO t VALUES (1)' % variable
            with self.subTest(sql=sql):
                self.assertEqual(dbops.classify(sql)['category'], 'replication')
        self.assertEqual(self.kind('SET STATEMENT max_statement_time = 1, sql_log_bin = 0 FOR SELECT 1'), 'risky')
        self.assertEqual(self.kind('SET STATEMENT max_statement_time = 1 FOR SELECT 1'), 'read')

    def test_8_dml_needs_a_key_pinned_to_a_value(self):
        for where in ('id > 0', '2 > 1', "'1'", 'TRUE OR a = 2', 'a = a', 'id = id', 'NOT id = 5', 'id <> 5',
                      'id = 5 OR id = 6', 'id IN (SELECT id FROM u)', 'id BETWEEN 1 AND 9'):
            for keys in (None, ['id']):
                sql = 'DELETE FROM t WHERE ' + where
                with self.subTest(sql=sql, keys=keys):
                    result = dbops.classify(sql, key_columns=keys)
                    self.assertEqual(result['kind'], 'risky')
                    self.assertEqual(dbops.decide(result, 'development')['action'], 'confirm')
        for where in ('id = 5', 'id = ?', 'id = :id', "id = 'a'", 'id IN (1, 2)', 'id <=> 5', 't.id = 5 AND a > 2',
                      '5 = id', '`id` = 1'):
            sql = 'UPDATE t SET a = 1 WHERE ' + where
            with self.subTest(sql=sql):
                self.assertEqual(self.kind(sql, key_columns=['id']), 'write')

    def test_9_read_only_is_enforced_by_the_server_too(self):
        self.assertEqual(dbops.decide(dbops.classify('SELECT f()'), 'production', True)['session_guard'],
                         ['SET SESSION TRANSACTION READ ONLY'])
        self.assertEqual(dbops.decide(dbops.classify('SELECT 1'), 'production')['session_guard'], [])
        for sql in ('SET SESSION transaction_read_only = 0', 'SET tx_read_only = OFF', 'SET TRANSACTION READ WRITE',
                    'SET SESSION TRANSACTION READ WRITE', 'START TRANSACTION READ WRITE'):
            with self.subTest(sql=sql):
                self.assertEqual(dbops.decide(dbops.classify(sql), 'development', True)['action'], 'refuse')
        self.assertEqual(self.kind('SET SESSION TRANSACTION READ ONLY'), 'read')
        self.assertEqual(self.kind('DO my_function()'), 'risky')

    def test_11_category_and_tone_do_not_depend_on_order(self):
        for sql in ('SET @a = 1; DELETE FROM t WHERE id = 1', 'DELETE FROM t WHERE id = 1; SET @a = 1'):
            with self.subTest(sql=sql):
                result = dbops.classify(sql, key_columns=['id'])
                self.assertEqual((result['kind'], result['category']), ('write', 'dml'))
                self.assertEqual(dbops.decide(result, 'production')['tone'], 'danger')
        self.assertEqual(dbops.decide(dbops.classify('SET @a = 1; COMMIT'), 'production')['tone'], 'primary')

    def test_12_key_check_is_linear(self):
        import time
        started = time.monotonic()
        for size in (20000, 60000):
            dbops.classify('DELETE FROM t WHERE ' + 'a .' * size, key_columns=['id'])
        self.assertLess(time.monotonic() - started, 5)
        result = dbops.classify('DELETE FROM t WHERE id = 1 AND ' + 'a = 1 AND ' * 3000 + 'b = 2', key_columns=['id'])
        self.assertEqual(result['kind'], 'risky')
        self.assertIn('too long', result['reasons'][0])

    def test_13_mariadb_replication_session_variables(self):
        for variable in ('skip_replication', 'wsrep_on'):
            self.assertIn(variable, dbops.SESSION_RISKY_VARIABLES)
        hint = dbops.privilege_hint('set_session_restricted', 'mariadb', (11, 4, 2), ['GRANT USAGE ON *.* TO u'],
                                    variable='skip_replication')
        self.assertEqual((hint['allowed'], hint['need']), (True, 'no privilege'))
        hint = dbops.privilege_hint('set_session_restricted', 'mariadb', (11, 4, 2), ['GRANT USAGE ON *.* TO u'],
                                    variable='wsrep_on')
        self.assertEqual((hint['allowed'], hint['need']), (False, 'SUPER'))

    def test_14_connection_identity_matches_binding_ts(self):
        base = {'profile_id': 'p1', 'host': 'db', 'port': '3306', 'user': 'app', 'ssl_mode': 'REQUIRED',
                'password': 'never'}
        self.assertEqual(dbops.connection_identity(base), '["p1","db",3306,"app","REQUIRED",""]')
        self.assertEqual(dbops.connection_identity(dict(base, type='production', read_only=False)),
                         dbops.connection_identity(base))
        self.assertEqual(dbops.connection_identity(dict(base, type='development')),
                         '["p1","db",3306,"app","REQUIRED","","development",false]')
        self.assertEqual(dbops.connection_identity(base, {'type': 'production', 'read_only': True}),
                         '["p1","db",3306,"app","REQUIRED","","production",true]')
        self.assertNotIn('never', dbops.connection_identity(base))
        source = (Path(__file__).resolve().parent.parent / 'apps' / 'workspace' / 'src' / 'binding.ts').read_text()
        self.assertIn('key.push(type, c.read_only === true)', source)
        from myflames import report
        options = {'host': 'db', 'port': 3306}
        self.assertNotEqual(report.flight_key(options, 'p1', {'type': 'development'}),
                            report.flight_key(options, 'p1', {'type': 'production'}))
        self.assertEqual(report.flight_key(options, 'p1'), report.flight_key(options, 'p1'))

    def test_15_set_statement_cannot_lift_session_guards(self):
        """Editor review gate item 3: MariaDB SET STATEMENT accepts
        transaction_read_only and tx_read_only (no NO_SET_STMT), so a
        read-only session could write for one statement. Quoted names count."""
        for sql in ('SET STATEMENT transaction_read_only = 0 FOR SELECT f()',
                    'SET STATEMENT `transaction_read_only` = 0 FOR SELECT f()',
                    'SET STATEMENT `tx_read_only`=0 FOR SELECT f()',
                    'SET STATEMENT max_statement_time = 1, sql_safe_updates = 0 FOR SELECT 1'):
            with self.subTest(sql=sql):
                self.assertNotEqual(self.kind(sql), 'read')
                self.assertEqual(dbops.decide(dbops.classify(sql), 'development', True)['action'], 'refuse')
        for sql in ('SET SESSION sql_safe_updates = 0', 'SET `sql_safe_updates` = 0', 'SET tx_read_only = 0'):
            with self.subTest(sql=sql):
                self.assertEqual(self.kind(sql), 'write')
        self.assertEqual(self.kind('SET STATEMENT max_statement_time = 1 FOR SELECT 1'), 'read')

    def test_15b_set_value_with_a_call_is_a_write(self):
        """A read variable assigned from a function call, user variable or
        subquery is a write: the value can run a stored function or take a lock."""
        for sql in ("SET SESSION group_concat_max_len = my_writer()",
                    "SET SESSION group_concat_max_len = GET_LOCK('a', 1)",
                    "SET max_execution_time = (SELECT 1)",
                    "SET SESSION group_concat_max_len = @a := 5",
                    "SET SESSION time_zone = '+00:00', max_execution_time = db.f(1)"):
            with self.subTest(sql=sql):
                self.assertEqual(self.kind(sql), 'write')
        for sql in ('SET SESSION group_concat_max_len = 1000000', "SET SESSION time_zone = 'a(b'"):
            with self.subTest(sql=sql):
                self.assertEqual(self.kind(sql), 'read')

    def test_15c_set_values_that_can_write_are_writes(self):
        """Security review: calls named in non-ASCII or with $, quoted names
        (ANSI_QUOTES "f" is masked as a string), MariaDB NEXT VALUE FOR and
        ORACLE-mode seq.NEXTVAL, and every value inside MariaDB SET STATEMENT
        (set_stmt_option takes set_expr_or_default) can change state."""
        for sql in ('SET time_zone = "f"()',
                    "SET time_zone = 'f' ()",
                    'SET sql_select_limit = é()',
                    'SET sql_select_limit = €()',
                    'SET sql_select_limit = ©()',
                    'SET sql_select_limit = 。()',
                    'SET sql_select_limit = 1$()',
                    'SET sql_select_limit = \u00a0()',
                    'SET sql_select_limit = (TABLE v)',
                    'SET sql_select_limit = NEXT VALUE FOR db.s',
                    'SET sql_select_limit = next  value  for s',
                    'SET sql_select_limit = s.NEXTVAL',
                    'SET sql_select_limit = db.s.`nextval`',
                    "SET STATEMENT max_statement_time = GET_LOCK('x',1) FOR SELECT 1",
                    'SET STATEMENT max_statement_time = (@x := 5) FOR SELECT 1',
                    'SET STATEMENT max_statement_time = 1, sql_select_limit = f() FOR SELECT 1',
                    'SET time_zone = CONVERT(f() USING utf8mb4)',
                    "SET time_zone = CONCAT(CONVERT(X'41' USING utf8mb4))",
                    "SET time_zone = CONVERT(X'41' USING utf8mb4), sql_select_limit = f()"):
            with self.subTest(sql=sql):
                self.assertEqual(self.kind(sql), 'write')
                self.assertEqual(dbops.decide(dbops.classify(sql), 'development', True)['action'], 'refuse')

    def test_15d_reads_myflames_sends_stay_reads(self):
        """Plain values, and myflames' own hex text literal (the exact
        CONVERT(X'..' USING utf8mb4) form), stay reads, so Parameters session
        changes keep working on read-only connections."""
        from myflames import perf_common, variables
        reads = ["SET time_zone = '+00:00'", 'SET NAMES utf8mb4', 'SET SESSION sql_select_limit = 10',
                 "SET @@session.optimizer_switch = 'index_merge=off'",
                 "SET SESSION time_zone = 'SYSTEM'", 'SET SESSION explain_json_format_version=2',
                 'SET explain_json_format_version=2', 'SET SESSION lock_wait_timeout = 5',
                 'SET SESSION innodb_lock_wait_timeout = 5', 'SET SESSION information_schema_stats_expiry = 0',
                 "SET SESSION optimizer_trace='enabled=on'", "SET SESSION optimizer_switch='mrr=on'",
                 'SET SESSION TRANSACTION READ ONLY', 'SET time_zone = @@global.time_zone',
                 'SET max_execution_time = 1.5', 'SET sql_select_limit = s.CURRVAL',
                 'SET STATEMENT max_statement_time = 5 FOR SELECT 1',
                 'SET SESSION time_zone = ' + perf_common.literal('Europe/Zürich'),
                 "SET time_zone = CONVERT( X'41'  USING  UTF8MB4 )",
                 variables.statement_for('time_zone', 'session', variables._text_literal('Europe/Zürich')),
                 variables.statement_for('lc_time_names', 'session', variables._text_literal("it's"))]
        for sql in reads:
            with self.subTest(sql=sql):
                self.assertEqual(self.kind(sql), 'read')
                self.assertEqual(dbops.decide(dbops.classify(sql), 'production', True)['action'], 'allow')

    def test_15e_calls_named_with_any_non_ascii_character_are_not_reads(self):
        """Both lexers take any byte that starts a multibyte character as an
        identifier character (MySQL my_ismb1st, MariaDB is_multi_byte_ident),
        so with utf8mb4 €() calls a stored function: capture and the MCP
        explain tool must refuse it. Non-ASCII text in strings, plain column
        names and ASCII white space stay reads."""
        from myflames import live
        # Non-ASCII spaces are identifier characters too: CONCAT\u00a0( calls
        # a function named "CONCAT\u00a0", not CONCAT.
        for sql in ('SELECT €()', 'SELECT ©(1) FROM t', 'SELECT 。()', 'SELECT a FROM t WHERE €x(a) > 0',
                    'SELECT db€.f()', 'SELECT \u3000() FROM t', "SELECT CONCAT\u00a0('a')",
                    "SELECT COUNT\u2007(*) FROM t"):
            with self.subTest(sql=sql):
                self.assertNotEqual(self.kind(sql), 'read')
                with self.assertRaises(ValueError):
                    live.bind_query(sql)
        for sql in ("SELECT '€()' FROM t", 'SELECT €col FROM t', 'SELECT `€`(1) FROM t',
                    "SELECT CONCAT('a', '\u00a0')", 'SELECT 1\tFROM\nt'):
            with self.subTest(sql=sql):
                expected = 'read' if not sql.startswith('SELECT `') else 'risky'
                self.assertEqual(self.kind(sql), expected)

    def test_16_backslash_commands_inside_optimizer_hints(self):
        """Editor review gate item 10: the client runs backslash commands inside
        /*+ */ hints (SSC_HINT is not a comment to add_line), e.g. \\C latin1."""
        for sql in ('SELECT /*+ \\C latin1 */ 1', 'SELECT /*+ \\d */ 1', "SELECT /*+ SET_VAR(a='\\x') */ 1"):
            with self.subTest(sql=sql):
                statement = dbops.split_statements(sql)[0]
                self.assertTrue(statement['issues'])
                self.assertEqual(self.kind(sql), 'risky')
        self.assertEqual(dbops.split_statements('SELECT /*+ MAX_EXECUTION_TIME(10) */ 1')[0]['issues'], [])
        self.assertEqual(dbops.split_statements('SELECT /* \\C latin1 */ 1')[0]['issues'], [])


# ---- the confirmation policy -----------------------------------------------------------

class TestPolicy(unittest.TestCase):
    MATRIX = {
        # kind: (production, development, read-only production, read-only development)
        'read': ('allow', 'allow', 'allow', 'allow'),
        'write': ('confirm', 'allow', 'refuse', 'refuse'),
        'risky': ('confirm', 'confirm', 'refuse', 'refuse'),
    }

    def test_matrix(self):
        sql = {'read': 'SELECT 1', 'write': 'INSERT INTO t VALUES (1)', 'risky': 'KILL 41'}
        for kind, expected in self.MATRIX.items():
            verdict = dbops.classify(sql[kind])
            got = tuple(dbops.decide(verdict, ctype, read_only)['action']
                        for read_only in (False, True) for ctype in ('production', 'development'))
            self.assertEqual(got, expected, kind)

    def test_texts_and_tone(self):
        refuse = dbops.decide(dbops.classify('KILL 41'), 'production', True, name='prod db')
        self.assertEqual(refuse['title'], 'Read-only connection')
        self.assertEqual(refuse['text'], "Connection prod db is read-only in myflames, so this statement was not "
                                         "sent. To allow writes, turn off Read-only in its settings, or run: "
                                         "myflames profiles set 'prod db' --read-write.")
        confirm = dbops.decide(dbops.classify('DELETE FROM t; DELETE FROM u'), 'production')
        self.assertEqual(confirm['text'], 'This is a Production connection, so myflames asks before every write.')
        self.assertEqual((confirm['tone'], confirm['statements']), ('danger', ['DELETE FROM t', 'DELETE FROM u']))
        risky = dbops.decide(dbops.classify('DROP TABLE t'), 'development', name='dev')
        self.assertEqual(risky['text'], 'Connection dev is a Development connection. myflames asks only before '
                                        'risky writes, and this statement is one.')
        light = dbops.decide(dbops.classify('SET @x = 1'), 'production')
        self.assertEqual((light['action'], light['tone']), ('confirm', 'primary'))
        for decision in (refuse, confirm, risky, light):
            self.assertNotIn('—', decision['text'])

    def test_gate(self):
        confirm = dbops.decide('write', 'production')
        self.assertEqual(dbops.gate(confirm)[0], 'needs_confirmation')
        self.assertIsNone(dbops.gate(confirm, confirmed=True))
        self.assertEqual(dbops.gate(dbops.decide('write', 'production', True), confirmed=True)[0], 'refused')
        self.assertIsNone(dbops.gate(dbops.decide('write', 'development')))

    def test_types_and_safety(self):
        self.assertEqual(dbops.normalize_type(None), 'production')
        self.assertEqual(dbops.normalize_type('Development'), 'development')
        for bad in ('staging', 5, True):
            with self.assertRaises(ValueError):
                dbops.normalize_type(bad)
        with self.assertRaises(ValueError):
            dbops.decide({'kind': 'maybe'})
        profile = {'id': 'p1', 'name': 'dev', 'type': 'development', 'read_only': False}
        self.assertEqual(dbops.connection_safety(profile),
                         {'profile_id': 'p1', 'profile_name': 'dev', 'type': 'development', 'read_only': False,
                          'endpoint_matches': True})
        # A request may ask for more protection, never less.
        self.assertTrue(dbops.connection_safety(profile, {'read_only': True})['read_only'])
        self.assertTrue(dbops.connection_safety(dict(profile, read_only=True), {'read_only': False})['read_only'])
        self.assertEqual(dbops.connection_safety(None, {}),
                         {'profile_id': None, 'profile_name': None, 'type': 'production', 'read_only': False,
                          'endpoint_matches': False})
        self.assertEqual(dbops.connection_safety(None, {'type': 'other', 'read_only': 'yes'})['type'], 'production')
        self.assertFalse(dbops.connection_safety(None, {'read_only': 'yes'})['read_only'])


# ---- privileges ----------------------------------------------------------------------

class TestPrivileges(unittest.TestCase):
    def test_parse_grants(self):
        parsed = dbops.parse_grants([
            'GRANT SELECT, PROCESS ON *.* TO `app`@`%`',
            'GRANT CONNECTION_ADMIN,SYSTEM_USER ON *.* TO `app`@`%`',
            'GRANT INSERT, UPDATE ON `shop`.* TO `app`@`%`',
            'GRANT DELETE ON `my\\_db`.* TO `app`@`%`',
            'GRANT ALTER ON `shop`.`t` TO `app`@`%`',
            'GRANT `role1`@`%` TO `app`@`%`',
        ])
        self.assertEqual(parsed['global'], {'SELECT', 'PROCESS', 'CONNECTION_ADMIN', 'SYSTEM_USER'})
        self.assertEqual(parsed['schemas'], {'shop': {'INSERT', 'UPDATE'}, 'my_db': {'DELETE'}})
        self.assertEqual(parsed['unparsed'], [])

    def test_parse_grants_keeps_escaped_and_wildcard_names_apart(self):
        # db\_x is the database db_x; db_x is a pattern that also matches dbAx.
        parsed = dbops.parse_grants(['GRANT SELECT ON `db\\_x`.* TO `u`@`%`', 'GRANT INSERT ON `db_x`.* TO `u`@`%`',
                                     'GRANT DELETE ON `shop%`.* TO `u`@`%`'])
        self.assertEqual(parsed['schemas'], {'db_x': {'SELECT'}})
        self.assertEqual(parsed['patterns'], {'db_x': {'INSERT'}, 'shop%': {'DELETE'}})
        lines = ['GRANT SELECT ON `db\\_x`.* TO u', 'GRANT INSERT ON `db_x`.* TO u']
        self.assertIsNone(self.hint('insert', 'mysql', (8, 4, 6), *lines, schema='db_x')['allowed'])
        self.assertTrue(self.hint('insert', 'mysql', (8, 4, 6), *lines, schema='dbAx')['allowed'])

    def test_parse_grants_keeps_package_library_and_unknown_lines(self):
        parsed = dbops.parse_grants(['GRANT EXECUTE ON PACKAGE BODY `shop`.`pkg` TO `app`@`%`',
                                     'GRANT EXECUTE ON LIBRARY `shop`.`lib` TO `app`@`%`',
                                     'GRANT SOMETHING NEW ON EVERYTHING'], 'mariadb')
        self.assertEqual({r['kind'] for r in parsed['model']['routines']}, {'PACKAGE BODY', 'LIBRARY'})
        self.assertEqual(parsed['unparsed'], ['GRANT SOMETHING NEW ON EVERYTHING'])
        # An unparsed line may grant it: unknown, never denied.
        hint = dbops.privilege_hint('kill', 'mysql', (8, 4, 6), ['GRANT USAGE ON *.* TO u', 'GRANT ??? ON x'])
        self.assertIsNone(hint['allowed'])

    def test_strip_credentials(self):
        cases = {
            "GRANT USAGE ON *.* TO `a`@`%` IDENTIFIED BY PASSWORD '*13C17A9ABF411E6300B51748CB04F59182323686'":
                'GRANT USAGE ON *.* TO `a`@`%`',
            "GRANT USAGE ON *.* TO `e`@`%` IDENTIFIED VIA ed25519 USING 'bikAd6IJ/jBlkOkm' OR unix_socket":
                'GRANT USAGE ON *.* TO `e`@`%`',
            "GRANT SELECT ON *.* TO `a`@`%` IDENTIFIED VIA x USING 'WITH GRANT OPTION' WITH GRANT OPTION":
                'GRANT SELECT ON *.* TO `a`@`%` WITH GRANT OPTION',
            "CREATE USER `a`@`%` IDENTIFIED WITH 'caching_sha2_password' AS 0x2441243030 DEFAULT ROLE `r`@`%` "
            "REQUIRE NONE": 'CREATE USER `a`@`%` DEFAULT ROLE `r`@`%` REQUIRE NONE',
            "CREATE USER `a`@`%` IDENTIFIED WITH 'p1' AS 'x' AND IDENTIFIED WITH 'p2' AS 'y' ACCOUNT LOCK":
                'CREATE USER `a`@`%` ACCOUNT LOCK',
            "CREATE USER `p`@`%` IDENTIFIED BY PASSWORD '*27E2' WITH MAX_USER_CONNECTIONS 3":
                'CREATE USER `p`@`%` WITH MAX_USER_CONNECTIONS 3',
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(dbops.strip_credentials(text), expected)

    def test_read_grants_returns_no_credentials(self):
        class Conn(object):
            def run(self, sql, extra_flags=None):
                if 'USER()' in sql:
                    return 'app@localhost\tapp@%\n'
                return ("GRANT USAGE ON *.* TO `app`@`%` IDENTIFIED BY PASSWORD '*13C17A9ABF411E6300B51748CB04F59182323686'\n"
                        "GRANT SELECT ON `shop`.* TO `app`@`%`\n")
        result = dbops.read_grants(Conn())
        self.assertEqual(result['grants'], ['GRANT USAGE ON *.* TO `app`@`%`', 'GRANT SELECT ON `shop`.* TO `app`@`%`'])
        self.assertNotIn('13C17A9', json.dumps(result))

    def test_mariadb_super_split_ends_at_11_0_1(self):
        from myflames import locks
        line = ['GRANT SUPER ON *.* TO u']
        self.assertTrue(self.hint('kill', 'mariadb', (11, 0, 0), *line)['allowed'])
        self.assertIsNone(self.hint('kill', 'mariadb', (11, 0, 1), *line)['allowed'])
        self.assertTrue(locks.parse_kill_privilege('mariadb', (11, 0, 0), line)['can_kill_others'])
        self.assertFalse(locks.parse_kill_privilege('mariadb', (11, 0, 1), line)['can_kill_others'])
        self.assertTrue(self.hint('write_when_read_only', 'mariadb', (10, 11, 0), *line)['allowed'])
        hint = self.hint('write_when_read_only', 'mariadb', (10, 11, 1), *line)
        self.assertIsNone(hint['allowed'])
        self.assertIn('10.11.1', hint['note'])

    def hint(self, operation, engine, version, *lines, **kw):
        return dbops.privilege_hint(operation, engine, version, list(lines), **kw)

    def test_mysql(self):
        self.assertEqual(self.hint('kill', 'mysql', (8, 4, 6), 'GRANT SUPER ON *.* TO u')['via'], 'SUPER')
        h = self.hint('kill', 'mysql', (8, 4, 6), 'GRANT PROCESS ON *.* TO u')
        self.assertEqual((h['allowed'], h['need']), (False, 'CONNECTION_ADMIN or SUPER'))
        self.assertIn('SYSTEM_USER', h['note'])
        h = self.hint('set_persist_only', 'mysql', (8, 4, 6), 'GRANT SUPER ON *.* TO u')
        self.assertEqual((h['allowed'], h['need']), (False, 'SYSTEM_VARIABLES_ADMIN and PERSIST_RO_VARIABLES_ADMIN'))
        h = self.hint('set_global', 'mysql', (8, 4, 6), 'GRANT SYSTEM_VARIABLES_ADMIN ON *.* TO u',
                      variable='default_table_encryption')
        self.assertFalse(h['allowed'])
        h = self.hint('set_global', 'mysql', (8, 4, 6),
                      'GRANT SYSTEM_VARIABLES_ADMIN,TABLE_ENCRYPTION_ADMIN ON *.* TO u',
                      variable='default_table_encryption')
        self.assertEqual(h['via'], 'SYSTEM_VARIABLES_ADMIN and TABLE_ENCRYPTION_ADMIN')
        h = self.hint('set_global', 'mysql', (8, 4, 6), 'GRANT SUPER ON *.* TO u', variable='mandatory_roles')
        self.assertEqual((h['allowed'], h['need']), (False, 'SYSTEM_VARIABLES_ADMIN and ROLE_ADMIN or SUPER and ROLE_ADMIN'))
        self.assertTrue(self.hint('replication', 'mysql', (8, 4, 6), 'GRANT ALL PRIVILEGES ON *.* TO u')['allowed'])
        self.assertTrue(self.hint('clone_donor', 'mysql', (8, 4, 6), 'GRANT BACKUP_ADMIN ON *.* TO u')['allowed'])

    def test_mariadb(self):
        h = self.hint('kill', 'mariadb', (10, 11, 9), 'GRANT SUPER ON *.* TO u')
        self.assertEqual((h['allowed'], h['via']), (True, 'SUPER'))
        # From 11.0, SUPER counts only for grants stored by an earlier version.
        h = self.hint('kill', 'mariadb', (11, 4, 2), 'GRANT SUPER ON *.* TO u')
        self.assertIsNone(h['allowed'])
        self.assertIn('stored by an earlier version', h['note'])
        self.assertTrue(self.hint('kill', 'mariadb', (11, 4, 2), 'GRANT CONNECTION ADMIN ON *.* TO u')['allowed'])
        self.assertEqual(self.hint('set_session_restricted', 'mariadb', (11, 4, 2), 'GRANT BINLOG ADMIN ON *.* TO u',
                                   variable='sql_log_bin')['via'], 'BINLOG ADMIN')
        self.assertFalse(self.hint('set_persist', 'mariadb', (11, 4, 2), 'GRANT ALL ON *.* TO u')['available'])
        self.assertIsNone(self.hint('write_when_read_only', 'mariadb', (11, 4, 2), 'GRANT SUPER ON *.* TO u')['allowed'])
        self.assertTrue(self.hint('write_when_read_only', 'mariadb', (10, 6, 0), 'GRANT SUPER ON *.* TO u')['allowed'])

    def test_object_privileges_check_global_and_schema_grants(self):
        line = 'GRANT INSERT ON `shop`.* TO u'
        self.assertTrue(self.hint('insert', 'mysql', (8, 4, 6), line, schema='shop')['allowed'])
        self.assertIsNone(self.hint('insert', 'mysql', (8, 4, 6), line, schema='other')['allowed'])
        with self.assertRaises(ValueError):
            dbops.required_privileges('fly', 'mysql')

    def test_kill_hint_shares_the_parser(self):
        from myflames import locks
        lines = ['GRANT PROCESS ON *.* TO `u`@`%`', 'GRANT CONNECTION_ADMIN ON *.* TO `u`@`%`']
        self.assertEqual(locks.parse_kill_privilege('mysql', (8, 4, 6), lines)['via'], 'CONNECTION_ADMIN')
        self.assertEqual(dbops.privilege_hint('kill', 'mysql', (8, 4, 6), lines)['via'], 'CONNECTION_ADMIN')

    def test_read_grants(self):
        class Conn(object):
            calls = []

            def run(self, sql, extra_flags=None):
                self.calls.append((sql, extra_flags))
                return 'app@localhost\tapp@%\n' if 'USER()' in sql else 'GRANT USAGE ON *.* TO `app`@`%`\n'
        conn = Conn()
        result = dbops.read_grants(conn)
        self.assertEqual((result['user'], result['account'], result['grants']),
                         ('app', 'app@%', ['GRANT USAGE ON *.* TO `app`@`%`']))
        self.assertTrue(all(sql.startswith('/*myflames:dbops*/ ') and flags == ['--comments']
                            for sql, flags in conn.calls))


# ---- redaction -----------------------------------------------------------------------------

class TestRedaction(unittest.TestCase):
    def test_hex_hashes(self):
        text = ("CREATE USER `a`@`%` IDENTIFIED WITH 'caching_sha2_password' AS "
                "0x244124303041242A493F2F2725193549337A REQUIRE NONE")
        self.assertEqual(dbops.redact(text), "CREATE USER `a`@`%` IDENTIFIED WITH 'caching_sha2_password' AS "
                                             "'[redacted]' REQUIRE NONE")
        self.assertNotIn('0x2441', dbops.redact('ERROR near AS 0x2441ABCD'))
        # A quoted plugin name ending in "password" is not a PASSWORD option (it used to swallow the rest).
        text = "CREATE USER `r`@`%` IDENTIFIED WITH 'caching_sha2_password' REQUIRE NONE ACCOUNT LOCK"
        self.assertEqual(dbops.redact(text), text)

    def test_secrets(self):
        cases = {
            "CREATE USER 'u'@'%' IDENTIFIED BY 's3cret'": "CREATE USER 'u'@'%' IDENTIFIED BY '[redacted]'",
            "ALTER USER u IDENTIFIED WITH caching_sha2_password BY 'new' REPLACE 'old'":
                "ALTER USER u IDENTIFIED WITH caching_sha2_password BY '[redacted]' REPLACE '[redacted]'",
            "CREATE USER u IDENTIFIED WITH mysql_native_password AS '*ABCD'":
                "CREATE USER u IDENTIFIED WITH mysql_native_password AS '[redacted]'",
            "CREATE USER u IDENTIFIED BY PASSWORD '*94BD'": "CREATE USER u IDENTIFIED BY PASSWORD '[redacted]'",
            "CREATE USER u IDENTIFIED VIA ed25519 USING PASSWORD('pw')":
                "CREATE USER u IDENTIFIED VIA ed25519 USING PASSWORD('[redacted]')",
            "SET PASSWORD FOR 'u'@'h' = PASSWORD('x1')": "SET PASSWORD FOR 'u'@'h' = PASSWORD('[redacted]')",
            "SET PASSWORD = 'plain'": "SET PASSWORD = '[redacted]'",
            "CHANGE REPLICATION SOURCE TO SOURCE_USER='r', SOURCE_PASSWORD='p4', SOURCE_HOST='h'":
                "CHANGE REPLICATION SOURCE TO SOURCE_USER='r', SOURCE_PASSWORD='[redacted]', SOURCE_HOST='h'",
            'CHANGE MASTER TO MASTER_PASSWORD = "p4"': 'CHANGE MASTER TO MASTER_PASSWORD = "[redacted]"',
            "START REPLICA USER='r' PASSWORD='rp'": "START REPLICA USER='r' PASSWORD='[redacted]'",
            "CLONE INSTANCE FROM 'u'@'h':3306 IDENTIFIED BY 'cl'":
                "CLONE INSTANCE FROM 'u'@'h':3306 IDENTIFIED BY '[redacted]'",
            "CREATE SERVER s FOREIGN DATA WRAPPER mysql OPTIONS (USER 'r', PASSWORD 'f', HOST 'h')":
                "CREATE SERVER s FOREIGN DATA WRAPPER mysql OPTIONS (USER 'r', PASSWORD '[redacted]', HOST 'h')",
            "ALTER USER u IDENTIFIED BY 'a\\'b' ": "ALTER USER u IDENTIFIED BY '[redacted]' ",
            "IDENTIFIED BY 'never closed": "IDENTIFIED BY '[redacted]'",
            'SELECT 1': 'SELECT 1',
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(dbops.redact(text), expected)

    def test_random_password_replace_redacted_without_explicit_secret(self):
        prefix = 'ALTER USER u IDENTIFIED BY RANDOM PASSWORD REPLACE '
        for literal in ("'old_secret'", "'old''secret'", '"old_secret"', "'unfinished"):
            with self.subTest(literal=literal):
                self.assertEqual(dbops.redact(prefix + literal), prefix + literal[0] + '[redacted]' + literal[0])
        statement = "SELECT REPLACE(value, 'old', 'new') FROM t"
        self.assertEqual(dbops.redact(statement), statement)

    def test_connection_password_anywhere(self):
        self.assertEqual(dbops.redact("Access denied for hunter2 ('hunter2')", 'hunter2'),
                         "Access denied for [redacted] ('[redacted]')")
        self.assertEqual(dbops.redact("x 'it''s' y it\\'s", "it's"), "x '[redacted]' y [redacted]")
        self.assertIsNone(dbops.redact(None))
        self.assertEqual(dbops.redact_value({'a': ['pw is pw1'], 'password': 'pw1', 'n': 3}, 'pw1'),
                         {'a': ['pw is [redacted]'], 'n': 3})

    def test_error_code(self):
        self.assertEqual(dbops.error_code('mysql client exited 1: ERROR 1095 (HY000) at line 1: x'), 1095)
        self.assertIsNone(dbops.error_code('Lost connection'))


# ---- the audit trail ----------------------------------------------------------------------

class TestAudit(TempHome):
    CONNECTION = {'host': 'db.internal', 'port': 3307, 'user': 'app', 'password': 'pw-not-stored'}

    def trail(self, **kw):
        safety = {'profile_id': 'p1', 'profile_name': 'prod', 'type': 'production', 'read_only': False}
        return dbops.AuditTrail(self.store, kw.pop('operation', 'kill'), kw.pop('surface', 'cli'),
                                connection=self.CONNECTION, safety=safety, password='pw-not-stored', **kw)

    def test_lifecycle_pending_then_ok(self):
        trail = self.trail(policy={'action': 'confirm', 'kind': 'risky'})
        trail.server('mysql', '8.4.6')
        audit_id = trail.attempt(["CREATE USER u IDENTIFIED BY 'secret' -- pw-not-stored"],
                                 target={'user': 'app', 'host': '10.0.0.5'}, verified=['user', 'host'])
        record = self.store.audit_records()[0]
        self.assertEqual((record['id'], record['status'], record['outcome_unknown'], record['finished_at']),
                         (audit_id, 'pending', True, None))
        self.assertEqual(record['statements'], ["CREATE USER u IDENTIFIED BY '[redacted]' -- [redacted]"])
        self.assertEqual(record['connection'], {'profile_id': 'p1', 'profile_name': 'prod', 'type': 'production',
                                                'read_only': False, 'host': 'db.internal', 'port': 3307,
                                                'user': 'app', 'engine': 'mysql', 'server_version': '8.4.6'})
        self.assertEqual((record['target'], record['verified'], record['policy']),
                         ({'user': 'app', 'host': '10.0.0.5'}, ['user', 'host'], {'action': 'confirm', 'kind': 'risky'}))
        trail.complete('ok', outcome='killed', message='done', after={'present': False})
        record = self.store.audit_records()[0]
        self.assertEqual((record['status'], record['outcome'], record['outcome_unknown'], record['after']),
                         ('ok', 'killed', False, {'present': False}))
        self.assertIsNotNone(record['finished_at'])
        self.assertGreaterEqual(record['duration_ms'], 0)
        self.assertEqual((record['surface'], record['operation']), ('cli', 'kill'))
        # A second complete never rewrites a finished record.
        self.assertFalse(dbops.complete(self.store, audit_id, 'error', message='late'))
        self.assertEqual(self.store.audit_records()[0]['status'], 'ok')
        self.assertNotIn(b'pw-not-stored', self.store.path.read_bytes())
        self.assertNotIn(b'secret', self.store.path.read_bytes())
        if os.name == 'posix':
            self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.store.path.parent.stat().st_mode & 0o777, 0o700)

    def test_a_crash_leaves_outcome_unknown(self):
        trail = self.trail()
        trail.attempt(['/*myflames:kill*/ KILL CONNECTION 41'])
        # The process dies here: no complete(). A new store sees the pending record.
        record = WorkspaceStore(self.store.path).audit_records()[0]
        self.assertEqual((record['status'], record['outcome_unknown']), ('pending', True))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            workspace_cli.cmd_audit([])
        self.assertIn('unknown', out.getvalue())

    def test_error_codes_and_unsent_outcomes(self):
        trail = self.trail()
        trail.attempt(['/*myflames:kill*/ KILL CONNECTION 41'])
        trail.complete('error', outcome='denied', error='ERROR 1095 (HY000): You are not owner of thread 41',
                       message='refused')
        self.assertEqual(self.store.audit_records()[0]['error_code'], 1095)
        for status in ('declined', 'refused'):
            self.trail().outcome(['KILL 7'], status, outcome=status, message='Nothing was ended.')
        failed = self.trail()
        failed.fail(ValueError('connection refused for pw-not-stored'), ['KILL 8'])
        records = self.store.audit_records()
        self.assertEqual([r['status'] for r in records], ['error', 'refused', 'declined', 'error'])
        self.assertEqual(records[0]['message'], 'connection refused for [redacted]')
        self.assertTrue(all(r['finished_at'] for r in records))

    def test_10_interrupt_or_timeout_after_sending_is_unknown(self):
        for exc, outcome in ((KeyboardInterrupt(), 'interrupted'),
                             (ValueError('Query timed out after 12s: KILL 4'), 'timeout'),
                             (ValueError('ERROR 2013 (HY000): Lost connection to server during query'), 'timeout')):
            with self.subTest(exc=repr(exc)):
                trail = self.trail()
                trail.attempt(['KILL 4'])
                self.assertTrue(trail.pending)
                trail.fail(exc)
                record = self.store.audit_records()[0]
                self.assertEqual((record['status'], record['outcome'], record['outcome_unknown']),
                                 ('unknown', outcome, True))
                self.assertIn('its outcome is unknown', record['message'])
        trail = self.trail()
        trail.attempt(['KILL 5'])
        trail.fail(ValueError('ERROR 1045: Access denied'))
        self.assertEqual(self.store.audit_records()[0]['status'], 'error')

    def test_15_unsent_outcomes_are_written_in_one_transaction(self):
        with mock.patch.object(WorkspaceStore, 'audit_complete', side_effect=AssertionError('second write')):
            dbops.record_outcome(self.store, 'kill', ['KILL 1'], 'cli', 'declined', message='no')
            self.trail().outcome(['KILL 2'], 'refused', message='read-only')
        records = self.store.audit_records()
        self.assertEqual([(r['status'], r['outcome_unknown']) for r in records],
                         [('refused', False), ('declined', False)])
        self.assertTrue(all(r['finished_at'] for r in records))

    def test_validation(self):
        with self.assertRaises(ValueError):
            dbops.record_attempt(self.store, 'kill', ['KILL 1'], 'web')
        with self.assertRaises(ValueError):
            dbops.complete(self.store, 'x', 'pending')

    def test_filters_limit_and_purge(self):
        for name, host in (('prod', 'db.internal'), (None, 'other.host')):
            safety = {'profile_id': None, 'profile_name': name, 'type': 'production', 'read_only': False}
            dbops.record_outcome(self.store, 'kill', ['KILL 1'], 'ui', 'refused', connection={'host': host, 'port': 3306},
                                 safety=safety)
        self.assertEqual(len(self.store.audit_records(server='PROD')), 1)
        self.assertEqual(len(self.store.audit_records(server='other.host:3306')), 1)
        self.assertEqual(len(self.store.audit_records(limit=1)), 1)
        self.assertEqual(len(self.store.audit_records(since_days=1)), 2)
        # Age one record by 200 days: --since and purge leave it out.
        with contextlib.closing(sqlite3.connect(str(self.store.path))) as conn, conn:
            conn.execute("UPDATE audit SET attempted_at = '2020-01-01T00:00:00.000000+00:00' WHERE host = 'other.host'")
        self.assertEqual(len(self.store.audit_records(since_days=30)), 1)
        self.assertEqual(self.store.audit_purge(30), 1)
        self.assertEqual(self.store.audit_purge(0), 1)
        self.assertEqual(self.store.audit_records(), [])
        for bad in ({'since_days': 0}, {'since_days': True}, {'limit': 0}, {'server': 5}):
            with self.assertRaises(ValueError):
                self.store.audit_records(**bad)
        with self.assertRaises(ValueError):
            self.store.audit_purge(-1)

    def test_status_filter_applies_before_the_limit_and_pages_continue(self):
        """An older record beyond the newest page is found by its status, and
        `before` pages through the rest in the list's order (review gate)."""
        from myflames.workspace_store import _cutoff
        dbops.record_outcome(self.store, 'kill', ['KILL 1'], 'ui', 'refused', message='old')
        with contextlib.closing(sqlite3.connect(str(self.store.path))) as conn, conn:
            conn.execute("UPDATE audit SET attempted_at = ?", (_cutoff(10),))
        for n in range(5):
            dbops.record_outcome(self.store, 'kill', ['KILL %d' % (n + 10)], 'ui', 'declined')
        report = dbops.audit_report(self.store, limit=3)
        self.assertEqual((len(report['records']), report['has_more']), (3, True))
        self.assertNotIn('refused', [r['status'] for r in report['records']])
        found = dbops.audit_report(self.store, limit=3, status='refused')
        self.assertEqual(([r['statements'] for r in found['records']], found['has_more']), ([['KILL 1']], False))
        # Paging: every record exactly once, newest first.
        seen, before = [], None
        while True:
            page = dbops.audit_report(self.store, limit=2, before=before)
            seen.extend(r['id'] for r in page['records'])
            if not page['has_more']:
                break
            before = page['records'][-1]['id']
        self.assertEqual(seen, [r['id'] for r in self.store.audit_records()])
        self.assertEqual(len(seen), 6)
        self.assertEqual(dbops.audit_report(self.store, limit=100000)['has_more'], False)
        for bad in ({'status': 'done'}, {'before': ''}, {'before': 5}, {'before': 'missing'}):
            with self.assertRaises(ValueError):
                self.store.audit_records(**bad)
        for limit in (0, 100001, True):
            with self.assertRaises(ValueError):
                dbops.audit_report(self.store, limit=limit)
        exported = json.loads(dbops.audit_export(self.store, 'json', status='refused')['content'])
        self.assertEqual([r['statements'] for r in exported['records']], [['KILL 1']])
        self.assertNotIn('has_more', exported)
        rows = list(csv.reader(io.StringIO(dbops.audit_export(self.store, 'csv', status='declined')['content'])))
        self.assertEqual(len(rows), 6)

    def test_retention_purges_opportunistically(self):
        from myflames.workspace_store import _cutoff
        self.assertEqual(self.store.audit_retention_days(), 90)
        dbops.record_outcome(self.store, 'kill', ['KILL 1'], 'cli', 'refused')
        with contextlib.closing(sqlite3.connect(str(self.store.path))) as conn, conn:
            conn.execute("UPDATE audit SET attempted_at = '2020-01-01T00:00:00.000000+00:00'")
        dbops.record_outcome(self.store, 'kill', ['KILL 2'], 'cli', 'refused')
        self.assertEqual([r['statements'] for r in self.store.audit_records()], [['KILL 2']])
        dbops.record_outcome(self.store, 'kill', ['KILL 3'], 'cli', 'refused')
        with contextlib.closing(sqlite3.connect(str(self.store.path))) as conn, conn:
            conn.execute("UPDATE audit SET attempted_at = ? WHERE statements LIKE '%KILL 3%'", (_cutoff(10),))
        self.assertEqual(self.store.set_audit_retention_days(5), 5)
        self.assertEqual(self.store.audit_retention_days(), 5)
        self.assertEqual([r['statements'] for r in self.store.audit_records()], [['KILL 2']])
        for bad in (0, 3651, '5', True):
            with self.assertRaises(ValueError):
                self.store.set_audit_retention_days(bad)

    def test_reading_never_creates_the_database(self):
        self.assertEqual(self.store.audit_records(), [])
        self.assertEqual(self.store.audit_purge(0), 0)
        self.assertEqual(self.store.audit_retention_days(), 90)
        self.assertFalse(self.store.path.exists())

    def test_export_json_and_csv(self):
        dbops.record_outcome(self.store, 'sql', ['=HYPERLINK("http://x")', "SET PASSWORD = 'p'"], 'ui', 'declined',
                             message='-cmd')
        exported = dbops.audit_export(self.store, 'json')
        self.assertEqual((exported['format'], exported['mime']), ('json', 'application/json'))
        self.assertRegex(exported['filename'], r'^myflames-audit-\d{8}-\d{6}\.json$')
        data = json.loads(exported['content'])
        self.assertEqual((data['schema_version'], data['retention_days']), ('audit-1.0', 90))
        self.assertEqual(data['records'][0]['statements'][1], "SET PASSWORD = '[redacted]'")
        exported = dbops.audit_export(self.store, 'csv')
        rows = list(csv.reader(io.StringIO(exported['content'])))
        self.assertEqual(rows[0], list(dbops.AUDIT_CSV_COLUMNS))
        record = dict(zip(rows[0], rows[1]))
        # Cells a spreadsheet would run as a formula start with a quote.
        self.assertEqual(record['statements'], '\'=HYPERLINK("http://x")\nSET PASSWORD = \'[redacted]\'')
        self.assertEqual((record['message'], record['status']), ("'-cmd", 'declined'))
        with self.assertRaises(ValueError):
            dbops.audit_export(self.store, 'xml')


# ---- profiles: type, read-only, migration ---------------------------------------------------

class TestProfileSafety(TempHome):
    def test_new_profiles_are_production_and_keep_settings_on_save(self):
        saved = self.store.save('profile', {'name': 'prod', 'host': 'db', 'user': 'app'})
        self.assertEqual((saved['type'], saved['read_only']), ('production', False))
        dev = self.store.save('profile', {'name': 'dev', 'host': 'dev', 'type': 'development', 'read_only': True})
        self.assertEqual((dev['type'], dev['read_only']), ('development', True))
        # A save that does not mention them (an older UI form) keeps them on the same endpoint.
        again = self.store.save('profile', {'name': 'dev again', 'host': 'dev'}, dev['id'])
        self.assertEqual((again['name'], again['type'], again['read_only']), ('dev again', 'development', True))
        changed = self.store.save('profile', {'name': 'dev', 'host': 'dev', 'read_only': False}, dev['id'])
        self.assertEqual((changed['type'], changed['read_only']), ('development', False))

    def test_6_a_new_endpoint_without_a_type_is_production_again(self):
        dev = self.store.save('profile', {'name': 'dev', 'host': 'dev', 'user': 'app', 'type': 'development'})
        same = self.store.save('profile', {'name': 'dev renamed', 'host': 'dev', 'user': 'app', 'database': 'x'},
                               dev['id'])
        self.assertEqual(same['type'], 'development')
        moved = self.store.save('profile', {'name': 'dev', 'host': 'prod', 'user': 'app'}, dev['id'])
        self.assertEqual(moved['type'], 'production')
        chosen = self.store.save('profile', {'name': 'dev', 'host': 'other', 'user': 'app', 'type': 'development'},
                                 dev['id'])
        self.assertEqual(chosen['type'], 'development')

    def test_bad_values_do_not_create_the_database(self):
        for value in ({'type': 'staging'}, {'type': 1}, {'read_only': 'yes'}, {'read_only': 1}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.save('profile', dict(value, host='db'))
        self.assertFalse(self.store.path.exists())

    def test_set_profile_safety_keeps_the_saved_password(self):
        saved = self.store.save('profile', {'name': 'prod', 'host': 'db', 'user': 'app'}, password='pw',
                                remember=True)
        changed = self.store.set_profile_safety(saved['id'], 'development', True)
        self.assertEqual((changed['type'], changed['read_only'], changed['has_saved_password']),
                         ('development', True, True))
        filled = self.store.connection_with_saved_password({'profile_id': saved['id'], 'host': 'db', 'user': 'app'})
        self.assertEqual(filled['password'], 'pw')
        self.assertEqual(self.store.set_profile_safety(saved['id'], read_only=False)['type'], 'development')
        forgotten = self.store.forget_password(saved['id'])
        self.assertEqual((forgotten['type'], forgotten['read_only']), ('development', False))
        for args in ((saved['id'],), (saved['id'], 'other'), (saved['id'], None, 'no'), ('missing', 'production')):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.store.set_profile_safety(*args)

    def test_existing_profiles_migrate_to_production(self):
        path = self.store.path
        path.parent.mkdir(mode=0o700, parents=True)
        with contextlib.closing(sqlite3.connect(str(path))) as conn, conn:
            # The schema and a profile written by myflames before connection types.
            conn.execute('CREATE TABLE records (id TEXT PRIMARY KEY, kind TEXT NOT NULL, updated TEXT NOT NULL, '
                         'payload TEXT NOT NULL)')
            conn.execute("INSERT INTO records VALUES ('p1', 'profile', '2026-01-01', ?)",
                         (json.dumps({'name': 'old', 'host': 'db', 'port': 3306, 'remember_password': False}),))
            conn.execute("INSERT INTO records VALUES ('p2', 'profile', '2026-01-01', 'not json')")
        os.chmod(str(path), 0o600)
        profile = WorkspaceStore(path).get('profile', 'p1')
        self.assertEqual((profile['type'], profile['read_only']), ('production', False))
        with contextlib.closing(sqlite3.connect(str(path))) as conn, conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 1)
            stored = json.loads(conn.execute("SELECT payload FROM records WHERE id = 'p1'").fetchone()[0])
            self.assertEqual((stored['type'], stored['read_only']), ('production', False))
            self.assertEqual(conn.execute("SELECT payload FROM records WHERE id = 'p2'").fetchone()[0], 'not json')
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({'audit', 'settings'} <= tables)


# ---- CLI ----------------------------------------------------------------------------------------

def run_cli(command, argv, stdin=None):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            mock.patch.object(sys, 'stdin', stdin or io.StringIO('')):
        try:
            command(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class TtyInput(io.StringIO):
    def isatty(self):
        return True


class TestAuditCLI(TempHome):
    def add(self, status='refused', host='db.internal', name='prod'):
        safety = {'profile_id': None, 'profile_name': name, 'type': 'production', 'read_only': True}
        return dbops.record_outcome(self.store, 'kill', ['/*myflames:kill*/ KILL CONNECTION 41'], 'cli', status,
                                    outcome='read_only', connection={'host': host, 'port': 3306, 'user': 'app'},
                                    safety=safety, message='Connection prod is read-only')

    def test_list_text_json_csv(self):
        code, out, err = run_cli(workspace_cli.cmd_audit, [])
        self.assertEqual((code, out), (0, 'WHEN (UTC)  STATUS  FROM  OPERATION  SERVER  STATEMENT  ID\n'))
        self.assertIn('No audit records yet', err)
        audit_id = self.add()
        code, out, err = run_cli(workspace_cli.cmd_audit, ['list'])
        self.assertEqual(code, 0, err)
        self.assertIn('WHEN (UTC)', out)
        self.assertIn('refused (read_only)', out)
        self.assertIn(audit_id[:8], out)
        self.assertIn('Kept for 90 days', err)
        code, out, _ = run_cli(workspace_cli.cmd_audit, ['--since', '7', '--server', 'prod', '--json'])
        data = json.loads(out)
        self.assertEqual((data['schema_version'], len(data['records'])), ('audit-1.0', 1))
        code, out, _ = run_cli(workspace_cli.cmd_audit, ['list', '--csv', '-q'])
        self.assertTrue(out.startswith('id,attempted_at,'))
        code, out, err = run_cli(workspace_cli.cmd_audit, ['list', '--server', 'nope'])
        self.assertIn('No audit records match', err)

    def test_export_to_a_file(self):
        self.add()
        path = Path(self.temp.name) / 'audit.csv'
        code, out, err = run_cli(workspace_cli.cmd_audit, ['export', '--csv', '-o', str(path)])
        self.assertEqual((code, out), (0, ''))
        self.assertIn('KILL CONNECTION 41', path.read_text())
        code, out, _ = run_cli(workspace_cli.cmd_audit, ['export'])
        self.assertEqual(json.loads(out)['records'][0]['status'], 'refused')

    def test_purge_needs_confirmation(self):
        self.add()
        code, _, err = run_cli(workspace_cli.cmd_audit, ['purge', '--older-than', '0'])
        self.assertEqual(code, 1)
        self.assertIn('Re-run with --yes', err)
        code, _, err = run_cli(workspace_cli.cmd_audit, ['purge', '--older-than', '0'], stdin=TtyInput('n\n'))
        self.assertEqual(code, 1)
        self.assertIn('Nothing was deleted', err)
        self.assertEqual(len(self.store.audit_records()), 1)
        code, _, err = run_cli(workspace_cli.cmd_audit, ['purge', '--older-than', '0'], stdin=TtyInput('y\n'))
        self.assertEqual(code, 0)
        self.assertIn('Deleted 1 audit record.', err)
        self.add()
        code, _, err = run_cli(workspace_cli.cmd_audit, ['purge', '--older-than', '30', '--yes'])
        self.assertEqual((code, len(self.store.audit_records())), (0, 1))

    def test_usage_errors_exit_2(self):
        for argv in (['purge'], ['purge', '--older-than', '-1'], ['list', '--since', '0'], ['list', '--json', '--csv'],
                     ['bogus'], ['list', '--limit', '0']):
            with self.subTest(argv=argv):
                self.assertEqual(run_cli(workspace_cli.cmd_audit, argv)[0], 2)

    def test_root_dispatch_and_help(self):
        from myflames import cli
        with mock.patch.object(sys, 'argv', ['myflames', 'audit', '--json']):
            code, out, _ = run_cli(lambda argv: cli.main(), [])
        self.assertEqual((code, json.loads(out)['schema_version']), (0, 'audit-1.0'))
        with mock.patch.object(sys, 'argv', ['myflames', '--help']):
            code, out, _ = run_cli(lambda argv: cli.main(), [])
        self.assertIn('audit     Local audit trail of server writes', out)


class TestProfilesSetCLI(TempHome):
    def test_set_and_list(self):
        saved = self.store.save('profile', {'name': 'prod', 'host': 'db', 'user': 'app'})
        code, out, err = run_cli(workspace_cli.cmd_profiles, ['list'])
        self.assertIn('TYPE', out)
        self.assertIn('production  read-write', out)
        code, out, err = run_cli(workspace_cli.cmd_profiles, ['set', 'prod', '--type', 'development', '--read-only'])
        self.assertEqual((code, out), (0, ''))
        self.assertIn("Profile 'prod' is now Development and read-only.", err)
        code, out, _ = run_cli(workspace_cli.cmd_profiles, ['list', '--json'])
        profile = json.loads(out)['profiles'][0]
        self.assertEqual((profile['id'], profile['type'], profile['read_only']), (saved['id'], 'development', True))
        code, _, err = run_cli(workspace_cli.cmd_profiles, ['set', saved['id'], '--read-write', '-q'])
        self.assertEqual((code, err), (0, ''))
        self.assertFalse(self.store.get('profile', saved['id'])['read_only'])
        for argv in (['set', 'prod'], ['set', 'prod', '--type', 'staging'], ['set', 'nope', '--read-only'],
                     ['set', 'prod', '--read-only', '--read-write']):
            with self.subTest(argv=argv):
                self.assertEqual(run_cli(workspace_cli.cmd_profiles, argv)[0], 2)


# ---- endpoints ---------------------------------------------------------------------------------

class TestEndpoints(TempHome):
    def workspace(self):
        return type('Workspace', (), {'store': self.store})()

    def test_audit_list_export_settings(self):
        dbops.record_outcome(self.store, 'kill', ['KILL 1'], 'ui', 'refused', message='read-only')
        listed = ui.api_response('/api/audit/list', {'since_days': 7}, self.workspace())
        self.assertEqual((listed['schema_version'], len(listed['records'])), ('audit-1.0', 1))
        exported = ui.api_response('/api/audit/export', {'format': 'csv'}, self.workspace())
        self.assertEqual(exported['mime'], 'text/csv')
        self.assertIn('KILL 1', exported['content'])
        self.assertEqual(ui.api_response('/api/audit/settings', {}, self.workspace()), {'retention_days': 90})
        self.assertEqual(ui.api_response('/api/audit/settings', {'retention_days': 30}, self.workspace()),
                         {'retention_days': 30})
        with self.assertRaises(ValueError):
            ui.api_response('/api/audit/settings', {'retention_days': 0}, self.workspace())
        # Status and paging go through the endpoints too.
        dbops.record_outcome(self.store, 'kill', ['KILL 2'], 'ui', 'declined')
        listed = ui.api_response('/api/audit/list', {'status': 'refused', 'limit': 1}, self.workspace())
        self.assertEqual(([r['status'] for r in listed['records']], listed['has_more']), (['refused'], False))
        first = ui.api_response('/api/audit/list', {'limit': 1}, self.workspace())
        self.assertTrue(first['has_more'])
        older = ui.api_response('/api/audit/list', {'limit': 1, 'before': first['records'][0]['id']}, self.workspace())
        self.assertEqual(([r['statements'] for r in older['records']], older['has_more']), ([['KILL 1']], False))
        exported = ui.api_response('/api/audit/export', {'format': 'json', 'status': 'declined'}, self.workspace())
        self.assertEqual([r['status'] for r in json.loads(exported['content'])['records']], ['declined'])
        with self.assertRaisesRegex(ValueError, 'requires a running workspace'):
            ui.api_response('/api/audit/list', {})
        with self.assertRaises(ValueError):
            ui.api_response('/api/audit/other', {}, self.workspace())

    def test_14_ui_declines_are_audited(self):
        saved = self.store.save('profile', {'name': 'dev', 'host': 'db', 'user': 'app', 'type': 'development'})
        result = ui.api_response('/api/audit/declined', {
            'operation': 'kill', 'statements': ["/*myflames:kill*/ KILL CONNECTION 41 -- IDENTIFIED BY 'pw'"],
            'target': {'connection_id': 41}, 'connection': {'profile_id': saved['id'], 'host': 'db', 'user': 'app',
                                                            'password': 'typed-secret'}}, self.workspace())
        record = self.store.audit_records()[0]
        self.assertEqual((record['id'], record['status'], record['surface'], result['status']),
                         (result['audit_id'], 'declined', 'ui', 'declined'))
        self.assertEqual((record['connection']['type'], record['connection']['profile_name']), ('development', 'dev'))
        self.assertNotIn(b'typed-secret', self.store.path.read_bytes())
        self.assertIn("IDENTIFIED BY '[redacted]'", record['statements'][0])
        for bad in ({}, {'operation': 'kill'}, {'operation': 'kill', 'statements': 'KILL 1'},
                    {'operation': 'kill', 'statements': ['KILL 1'], 'target': 'x'}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ui.api_response('/api/audit/declined', bad, self.workspace())

    def test_routes_are_allowlisted_and_not_connection_paths(self):
        import inspect
        source = inspect.getsource(ui.WorkspaceHandler.do_POST)
        for route in ('/api/audit/list', '/api/audit/export', '/api/audit/settings', '/api/audit/declined'):
            self.assertIn('"%s"' % route, source)
            self.assertNotIn(route, ui._CONNECTION_PATHS)
        self.assertIn('/api/live/kill', ui._WRITE_PATHS)

    def test_profiles_accept_and_return_type_and_read_only(self):
        saved = ui.api_response('/api/profiles/save', {'value': {'name': 'dev', 'host': 'db', 'type': 'development',
                                                                 'read_only': True}}, self.workspace())
        self.assertEqual((saved['type'], saved['read_only']), ('development', True))
        items = ui.api_response('/api/profiles/list', {}, self.workspace())['items']
        self.assertEqual([(p['type'], p['read_only']) for p in items], [('development', True)])
        with self.assertRaises(ValueError):
            ui.api_response('/api/profiles/save', {'value': {'host': 'db', 'type': 'qa'}}, self.workspace())


class TestAuditSafetyRegressions(TempHome):
    def test_declined_refused_and_unknown_are_once_only_and_redacted(self):
        secret = 'audit-regression-secret'
        statement = "ALTER USER 'app'@'%' IDENTIFIED BY 'new-account-secret' /* " + secret + ' */'
        ids = []
        for status in ('declined', 'refused', 'unknown'):
            with self.subTest(status=status):
                trail = dbops.AuditTrail(self.store, 'accounts.password', 'ui',
                                         connection={'host': 'db', 'password': secret}, password=secret,
                                         policy={'note': secret})
                target = {'password': secret, 'nested': {'detail': secret}}
                if status == 'unknown':
                    audit_id = trail.attempt([statement], target=target, verified=[secret])
                    pending = next(r for r in self.store.audit_records() if r['id'] == audit_id)
                    self.assertEqual((pending['status'], pending['outcome_unknown']), ('pending', True))
                    trail.fail(ValueError('ERROR 2013: Lost connection to server: ' + secret))
                else:
                    audit_id = trail.outcome([statement], status, outcome=status,
                                             message='Nothing sent: ' + secret, target=target, verified=[secret])
                ids.append(audit_id)
                original = next(r for r in self.store.audit_records() if r['id'] == audit_id)
                self.assertEqual((original['status'], original['outcome_unknown']), (status, status == 'unknown'))
                self.assertIsNotNone(original['finished_at'])
                # Handlers can report the same exception at multiple layers.
                # None may duplicate an outcome or overwrite the first result.
                trail.fail(ValueError('late failure: ' + secret), [statement])
                trail.complete('ok', message='late success')
                self.assertEqual(trail.outcome([statement], 'refused'), audit_id)
                self.assertFalse(dbops.complete(self.store, audit_id, 'ok'))
                self.assertEqual(next(r for r in self.store.audit_records() if r['id'] == audit_id), original)
        records = self.store.audit_records()
        self.assertEqual(len(records), 3)
        self.assertEqual({r['id'] for r in records}, set(ids))
        reopened = WorkspaceStore(self.store.path).audit_records()
        self.assertEqual(reopened, records)
        for value in (secret, 'new-account-secret'):
            self.assertNotIn(value, json.dumps(records))
            self.assertNotIn(value.encode('utf-8'), self.store.path.read_bytes())

    def test_failed_audit_insert_prevents_a_real_write_path_sending(self):
        from myflames import locks
        from test_locks import KillScenario, bound_kill_expect
        scenario = KillScenario()
        trail = dbops.AuditTrail(self.store, 'kill', 'ui')
        with mock.patch.object(self.store, 'audit_insert', side_effect=sqlite3.OperationalError('disk full')) as insert:
            with self.assertRaisesRegex(ValueError, 'Could not write the audit record.*nothing was sent'):
                locks.kill_session(scenario.conn, 41, expect=bound_kill_expect(scenario.conn),
                                   settle_s=0, audit=trail, confirmed=True)
        insert.assert_called_once()
        self.assertFalse(trail.pending)
        self.assertIsNone(trail.id)
        # Reads really ran: an empty statement list alone could hide an early
        # validation refusal, which would not test the audit-before-send rule.
        self.assertTrue(scenario.conn.calls)
        self.assertEqual(scenario.kills(), [])
        self.assertEqual(self.store.audit_records(), [])

    def test_retention_preserves_pending_rows_at_and_inside_cutoff(self):
        from myflames import workspace_store
        cutoff = '2026-01-01T00:00:00.000000+00:00'
        # Each deletion entry point must preserve both recent and boundary
        # pending rows. Older pending rows follow the documented retention.
        for purge in ('insert', 'explicit', 'settings'):
            with self.subTest(purge=purge):
                store = WorkspaceStore(Path(self.temp.name) / (purge + '.sqlite3'))
                trails = [dbops.AuditTrail(store, 'kill', 'ui') for _ in range(3)]
                ids = [trail.attempt(['KILL %d' % i]) for i, trail in enumerate(trails, 1)]
                dates = ('2025-12-31T23:59:59.999999+00:00', cutoff,
                         '2026-01-01T00:00:00.000001+00:00')
                with contextlib.closing(sqlite3.connect(str(store.path))) as conn, conn:
                    conn.executemany('UPDATE audit SET attempted_at = ? WHERE id = ?', zip(dates, ids))
                with mock.patch.object(workspace_store, '_cutoff', return_value=cutoff):
                    if purge == 'insert':
                        dbops.record_outcome(store, 'kill', ['KILL 4'], 'ui', 'declined')
                    elif purge == 'explicit':
                        self.assertEqual(store.audit_purge(30), 1)
                    else:
                        self.assertEqual(store.set_audit_retention_days(30), 30)
                pending = store.audit_records(status='pending')
                self.assertEqual({r['id'] for r in pending}, set(ids[1:]))
                self.assertTrue(all(r['outcome_unknown'] and r['finished_at'] is None for r in pending))
                # Retention must not prevent a surviving write from completing.
                trails[2].complete('ok', outcome='killed')
                self.assertEqual([r['id'] for r in store.audit_records(status='pending')], [ids[1]])
                self.assertEqual([r['id'] for r in store.audit_records(status='ok')], [ids[2]])


if __name__ == '__main__':
    unittest.main()


class TestCLIConfirmation(unittest.TestCase):
    def confirm(self, answer='', tty=True, **kwargs):
        stream = mock.Mock()
        stream.isatty.return_value = tty
        if isinstance(answer, BaseException):
            stream.readline.side_effect = answer
        else:
            stream.readline.return_value = answer
        err = io.StringIO()
        result = dbops.cli_confirm('Target: db:3306\nSET GLOBAL x = 1;', 'Apply? [y/N] ',
                                    stdin=stream, stderr=err, **kwargs)
        return result, err.getvalue(), stream

    def test_default_no_interrupt_eof_and_affirmative_answers(self):
        for answer in ('', '\n', 'n\n', 'yes please\n', EOFError(), KeyboardInterrupt()):
            with self.subTest(answer=answer):
                result, err, stream = self.confirm(answer)
                self.assertEqual(result, ('declined', 'declined', 'Nothing was sent.'))
                self.assertLess(err.index('SET GLOBAL'), err.index('Apply?'))
                stream.readline.assert_called_once_with()
        for answer in ('y\n', 'YES\r\n', ' yes \n'):
            self.assertIsNone(self.confirm(answer)[0])

    def test_non_terminal_yes_quiet_and_policy_allowed(self):
        for ask in (False, True):
            result, err, stream = self.confirm(tty=False, ask=ask)
            self.assertEqual(result[:2], ('refused', 'no_terminal'))
            self.assertIn('--yes', result[2])
            stream.readline.assert_not_called()
        for quiet in (False, True):
            result, err, stream = self.confirm(tty=False, assume_yes=True, quiet=quiet)
            self.assertIsNone(result)
            self.assertEqual(bool(err), not quiet)
            stream.readline.assert_not_called()
        result, err, stream = self.confirm(ask=False, quiet=True)
        self.assertIsNone(result)
        self.assertIn('SET GLOBAL', err)
        stream.readline.assert_not_called()

    def test_typed_guard_is_exact_and_yes_does_not_bypass_it(self):
        for answer, accepted in [('db:3306\n', True), ('db:3306\r\n', True),
                                 (' db:3306\n', False), ('db:3306 \n', False), ('DB:3306\n', False)]:
            with self.subTest(answer=answer):
                result, _, _ = self.confirm(answer, expected='db:3306')
                self.assertEqual(result is None, accepted)
        for supplied, outcome in [(None, 'no_target'), (' db:3306', 'target_mismatch'), ('db:3306', None)]:
            result, _, stream = self.confirm(tty=False, assume_yes=True, expected='db:3306', supplied=supplied)
            self.assertEqual(result[1] if result else None, outcome)
            stream.readline.assert_not_called()
        for answer in ('', EOFError(), KeyboardInterrupt()):
            self.assertEqual(self.confirm(answer, expected='db:3306', mismatch_outcome='refused')[0][:2],
                             ('declined', 'declined'))

    def test_terminal_controls_and_credentials_are_scrubbed(self):
        err = io.StringIO()
        stream = mock.Mock()
        stream.isatty.return_value = False
        problem = dbops.cli_confirm("ALTER USER x IDENTIFIED BY 'secret';\nTarget: db\x1b[2J",
            'secret\x07', stdin=stream, stderr=err, passwords=('secret',),
            no_terminal_message='secret\x1b[2J')
        text = err.getvalue() + problem[2]
        self.assertNotIn('secret', text)
        self.assertNotIn('\x1b', text)
        self.assertIn('[redacted]', text)
