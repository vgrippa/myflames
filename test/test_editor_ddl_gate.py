"""The Query editor's one-DDL gate: _ddl_codes on planned units.

A CREATE PROCEDURE/FUNCTION/TRIGGER/EVENT/PACKAGE body (and an ALTER EVENT
body) is one DDL statement even with semicolons inside; anything after the
definition's end is a second statement and must be counted. Statement-level
IF/LOOP/WHILE/REPEAT/CASE blocks without an outer BEGIN, parenthesized or
post-DECLARE AS/IS in MariaDB packages, dotted name parts and quoted
identifiers must not change that counting.
"""
import ast
import os
import sys
import threading
import unittest
import types
from pathlib import Path
from unittest import mock

import test_editor_session as process_fixtures
import test_editor_session_e2 as setup_fixtures
import test_editor_session_identity as identity_fixtures

from myflames import editor_session as ed
from myflames.connector import ConnectorError

DELIMITER = "//"

# Event bodies without an outer BEGIN: one DDL in both servers' grammars
# (mysql-server/sql/sql_yacc.yy ev_sql_stmt / sp_proc_stmt; mariadb-server
# sql/sql_yacc.yy "ALTER ... EVENT ... DO" and create_event_body).
EVENT_BODIES = (
    "IF TRUE THEN SELECT 1; END IF",
    "IF (1) THEN SELECT 1; END IF",
    "IF IF(1, 2, 3) THEN SELECT 1; END IF",
    "WHILE 0 DO SELECT 1; END WHILE",
    "REPEAT SELECT 1; UNTIL TRUE END REPEAT",
    "LOOP SELECT 1; END LOOP",
    "CASE WHEN TRUE THEN SELECT 1; END CASE",
    "IF TRUE THEN SELECT (CASE WHEN 1 THEN 2 END); END IF",
    "IF TRUE THEN WHILE 0 DO SELECT 1; END WHILE; END IF",
)

# The round E8 definitions keep their one-part results.
E8_DEFINITIONS = (
    "CREATE PACKAGE BODY p AS PROCEDURE p1; BEGIN NULL; END p",
    "CREATE PACKAGE BODY p AS PROCEDURE p1 IS n INT; BEGIN NULL; END; BEGIN p1; END p",
    "CREATE PACKAGE BODY p FUNCTION f() RETURNS INT RETURN 1; END",
    "CREATE PACKAGE BODY p FUNCTION f() RETURNS INT RETURN 1; BEGIN SELECT f(); END; END",
    "CREATE PACKAGE BODY p FUNCTION f() RETURNS INT RETURN 1; SELECT f() AS n; BEGIN SELECT 2; END; END",
    "ALTER EVENT e DO BEGIN SELECT 1; SELECT CASE WHEN 1 THEN 2 END; END",
    "ALTER DEFINER = 'u'@'localhost' EVENT e ON SCHEDULE EVERY 1 DAY DO BEGIN SELECT 1; END",
    "CREATE PROCEDURE p() BEGIN SELECT 1; END",
)

# MariaDB packages: AS/IS inside parentheses (a DEFAULT expression such as
# (1 IS NULL)) or after DECLARE is not the package's IS/AS
# (mariadb-server/sql/sql_yacc.yy:20134-20137 sp_tail_is).
PACKAGE_DEFINITIONS = (
    "CREATE PACKAGE BODY p AS PROCEDURE p1(x INT DEFAULT (1 IS NULL)); BEGIN p1; END p",
    "CREATE PACKAGE BODY p AS PROCEDURE p1(x INT DEFAULT (1 IS NULL)) BEGIN NULL; END; END p",
    "CREATE PACKAGE BODY p AS PROCEDURE p1 IS n INT DEFAULT (1 IS NULL); BEGIN NULL; END; END p",
)

# MariaDB CREATE [OR REPLACE] [DEFINER = ...] AGGREGATE FUNCTION
# (mariadb-server/sql/sql_yacc.yy:20502-20529, opt_aggregate at 3344-3348).
AGGREGATE_DEFINITIONS = (
    "CREATE AGGREGATE FUNCTION f(x INT) RETURNS INT BEGIN RETURN 1; END",
    "CREATE OR REPLACE AGGREGATE FUNCTION f(x INT) RETURNS INT BEGIN RETURN 1; END",
    "CREATE DEFINER = 'u'@'localhost' AGGREGATE FUNCTION f(x INT) RETURNS INT BEGIN RETURN 1; END",
)


def parts_count(sql, engine):
    """The number of statements _ddl_codes counts in the planned units."""
    units = ed.plan_statements(sql, engine=engine, delimiter=DELIMITER)
    return sum(len(ed._ddl_codes(unit)) for unit in units)


class EditorDdlGateTests(unittest.TestCase):
    def test_editor_backend_keeps_python_37_syntax(self):
        source = Path(ed.__file__).read_text(encoding="utf-8")
        ast.parse(source, **({"feature_version": 7} if sys.version_info >= (3, 8) else {}))

    def decision(self, sql, engine="mysql"):
        session = types.SimpleNamespace(engine=engine, server_version="8.4.11" if engine == "mysql" else "11.8.9-MariaDB",
                                        sql_mode="", backslash_escapes=True, in_transaction=False,
                                        connection_id=1, server_identity={})
        plan = ed._plan_run({"sql": "DELIMITER //\n" + sql + "//"}, session)
        return ed._decision(plan, {"type": "production", "read_only": False}, session)

    def test_identifier_keywords_cannot_hide_trailing_drop(self):
        for engine in ("mysql", "mariadb"):
            for prefix in ("$", "é", "☃", "@", "@@", "@@session."):
                sql = ("CREATE PROCEDURE p() BEGIN SET %sloop=1; END; DROP DATABASE prod; SELECT %send"
                       % (prefix, prefix))
                with self.subTest(engine=engine, sql=sql):
                    self.assertEqual(self.decision(sql, engine)["action"], "refuse")
            sql = "CREATE PROCEDURE p() SELECT 1 AS $BEGIN; DROP DATABASE prod; SELECT 1 AS $END"
            with self.subTest(engine=engine, sql=sql):
                self.assertEqual(self.decision(sql, engine)["action"], "refuse")

    def test_identifier_keywords_and_row_constructor_if_keep_one_ddl(self):
        for engine in ("mysql", "mariadb"):
            for body in ("BEGIN SELECT col$end FROM t; SELECT 2; END",
                         "BEGIN SELECT col☃end FROM t; SELECT 2; END",
                         "BEGIN SET @loop=1; SELECT @@session.sql_mode, @end; END",
                         "BEGIN IF (1, 2) = (1, 2) THEN SELECT 1; END IF; END"):
                with self.subTest(engine=engine, body=body):
                    self.assertEqual(parts_count("CREATE PROCEDURE p() " + body, engine), 1)

    def test_expression_if_not_exists_cannot_open_control_block(self):
        sql = ("CREATE PROCEDURE p() CREATE TABLE IF NOT EXISTS t AS SELECT CASE WHEN 1 THEN 2 END; "
               "DROP DATABASE prod; XA START 'x'; XA END 'x'")
        for engine in ("mysql", "mariadb"):
            with self.subTest(engine=engine):
                self.assertEqual(self.decision(sql, engine)["action"], "refuse")

    def test_package_body_alias_cannot_open_declarations(self):
        sql = ("CREATE PACKAGE BODY p AS PROCEDURE p1() SELECT 1 AS n; BEGIN SELECT 1; END p; "
               "DROP DATABASE prod; XA END 'x'")
        self.assertEqual(self.decision(sql, "mariadb")["action"], "refuse")

    def test_drop_inside_definition_requires_typed_target_or_refusal(self):
        for kind in ("DATABASE", "SCHEMA"):
            for name, decoded in (("prod", "prod"), ("`my``db`", "my`db")):
                sql = "CREATE PROCEDURE p() BEGIN DROP %s IF EXISTS %s; END" % (kind, name)
                with self.subTest(sql=sql):
                    self.assertEqual(self.decision(sql).get("typed_target"), decoded)
        sql = "CREATE PROCEDURE p() BEGIN DROP DATABASE one; DROP DATABASE two; END"
        self.assertEqual(self.decision(sql)["action"], "refuse")

    def test_possible_mode_changes_anywhere_in_unit(self):
        for sql in ('SET "sql_mode" = "ANSI_QUOTES"', 'SET @@session."sql_mode" = "ANSI_QUOTES"',
                    'SET `sql_mode` = "ANSI_QUOTES"',
                    'SET STATEMENT "sql_mode" = "ANSI_QUOTES" FOR SELECT 1',
                    'SET @x=IF(1, 2, 3), "sql_mode" = "ANSI_QUOTES"',
                    'SET @@ session . "sql_mode" = "ANSI_QUOTES"',
                    'SET @ @ session . `sql_mode` = "ANSI_QUOTES"',
                    "SET STATEMENT max_statement_time=1 FOR CALL p()",
                    "SET STATEMENT max_statement_time=1 FOR EXECUTE p",
                    "BEGIN NOT ATOMIC CALL p(); END",
                    "BEGIN NOT ATOMIC IF 1 THEN EXECUTE p; END IF; END"):
            with self.subTest(sql=sql):
                units = ed.plan_statements(sql, engine="mariadb", delimiter=DELIMITER)
                self.assertTrue(any(ed._unit_changes_mode(unit) for unit in units))

    def test_mode_changes_refuse_ambiguous_following_sql_before_sending(self):
        for change in ('SET "sql_mode" = "NO_BACKSLASH_ESCAPES"',
                       'SET @@SESSION."sql_mode" = "NO_BACKSLASH_ESCAPES"',
                       'SET @@ session . "sql_mode" = "NO_BACKSLASH_ESCAPES"',
                       "SET STATEMENT max_statement_time=1 FOR CALL change_mode()",
                       "SET STATEMENT max_statement_time=1 FOR EXECUTE mode_change",
                       "BEGIN NOT ATOMIC IF 1 THEN CALL change_mode(); END IF; END"):
            sql = "DELIMITER //\n" + change + "; SELECT 'a\\'; DELETE FROM t; -- '//"
            with self.subTest(change=change):
                with self.assertRaises(ed.EditorError):
                    ed.plan_statements(sql, engine="mariadb")

    def test_mode_keyword_substrings_and_variables_are_not_calls(self):
        for sql in ("SELECT $CALL, x☃EXECUTE, x$SQL_MODE, @call, @@session.execute, @sql_mode",
                    "SET @sql_mode = 'unchanged'", "SELECT t.call, `execute` FROM t",
                    "SELECT @ call, @@ session . execute, t . call"):
            with self.subTest(sql=sql):
                self.assertFalse(ed._unit_changes_mode(ed.plan_statements(sql)[0]))

    def test_nested_statement_positions_and_functions_keep_one_ddl(self):
        bodies = (
            "outer_label: BEGIN inner_label: LOOP LEAVE inner_label; END LOOP inner_label; END outer_label",
            "BEGIN IF (1,2)=(1,2) THEN IF IF(1,2,3) THEN SELECT REPEAT('x',2); END IF; "
            "ELSEIF (2,3)=(2,3) THEN SELECT 2; ELSE SELECT 3; END IF; END",
            "BEGIN CASE CASE WHEN 1 THEN 2 END WHEN 2 THEN SELECT CASE WHEN 1 THEN 2 END; "
            "ELSE SELECT 3; END CASE; END",
            "BEGIN DECLARE CONTINUE HANDLER FOR SQLSTATE '23000', NOT FOUND "
            "BEGIN SELECT 1; SELECT 2; END; SELECT 3; END",
            "REPEAT SELECT REPEAT('x', 2); UNTIL IF(1,2,3) END REPEAT",
            "BEGIN WHILE (1,2)=(3,4) DO SELECT IF(1,2,3); END WHILE; END",
        )
        for engine in ("mysql", "mariadb"):
            for body in bodies:
                sql = "CREATE PROCEDURE p() " + body
                with self.subTest(engine=engine, sql=sql):
                    self.assertEqual(parts_count(sql, engine), 1)
                    self.assertEqual(parts_count(sql + "; DROP DATABASE prod", engine), 2)

    def assert_definition_and_tail(self, definition, engine):
        """A complete definition reviews once; no trailing SQL shares it."""
        sql = "DELIMITER //\n" + definition + "//"
        self.assertEqual(parts_count(sql, engine), 1)
        self.assertEqual(self.decision(definition, engine)["action"], "confirm")
        for tail in ("DROP DATABASE prod", "CREATE TABLE extra (n INT)", "SELECT 2"):
            merged = definition + "; " + tail
            with self.subTest(tail=tail):
                self.assertEqual(parts_count("DELIMITER //\n" + merged + "//", engine), 2)
                self.assertEqual(self.decision(merged, engine)["action"], "refuse")

    def test_multiword_returns_types_and_quoted_charset_keep_one_definition(self):
        # mysql-server/sql/sql_yacc.yy:7157-7336,7371-7409,7698-7746;
        # mariadb-server/sql/sql_yacc.yy:6835-6879,19287-19322.
        common = ("DOUBLE PRECISION", "DOUBLE PRECISION(10, 2)",
                  "CHAR VARYING(10)", "CHARACTER VARYING(10)", "NCHAR VARYING(10)",
                  "NATIONAL CHAR(10)", "NATIONAL CHARACTER VARYING(10)",
                  "NATIONAL VARCHAR(10)", "NCHAR VARCHAR(10)",
                  "LONG VARCHAR", "LONG VARBINARY", "LONG CHARACTER VARYING",
                  "CHAR(10) ASCII", "CHAR(10) UNICODE", "CHAR(10) BYTE",
                  "VARCHAR(10) CHARACTER SET 'utf8mb4' COLLATE 'utf8mb4_bin'",
                  'VARCHAR(10) CHARSET "utf8mb4" COLLATE "utf8mb4_bin"',
                  "CHAR(10) CHAR SET `utf8mb4` COLLATE `utf8mb4_bin`")
        maria = ("TYPE OF t.c", "TYPE OF d.t.c", "ROW TYPE OF d.t",
                 "ROW(a INT, b CHAR(10))", "TEXT COMPRESSED", "TEXT COMPRESSED=zlib",
                 "GEOMETRY REF_SYSTEM_ID = 4326")
        for engine in ("mysql", "mariadb"):
            for returned in common + (maria if engine == "mariadb" else ()):
                definition = ("CREATE FUNCTION f() RETURNS %s DETERMINISTIC "
                              "BEGIN DECLARE x INT DEFAULT 1; RETURN x; END" % returned)
                with self.subTest(engine=engine, returned=returned):
                    self.assert_definition_and_tail(definition, engine)

    def test_quoted_end_labels_compare_names_instead_of_mask_indices(self):
        # MySQL sql/sql_yacc.yy:5570-5644 compares the two decoded labels;
        # MariaDB:4750-4753,4820-4854 uses the same optional end label.
        for engine in ("mysql", "mariadb"):
            for first, last in (("`lbl`", "`lbl`"), ("`Lbl`", "lbl"), ("lbl", "`LBL`"),
                                ("`a``b`", "`a``b`")):
                for body in ("BEGIN SELECT 1; END", "LOOP LEAVE %s; END LOOP" % first,
                             "WHILE 0 DO SELECT 1; END WHILE", "REPEAT SELECT 1; UNTIL 1 END REPEAT"):
                    definition = "CREATE PROCEDURE p() %s: %s %s" % (first, body, last)
                    with self.subTest(engine=engine, definition=definition):
                        self.assert_definition_and_tail(definition, engine)
            for body in ("`one`: BEGIN SELECT 1; END `two`",
                         "`one`: LOOP SELECT 1; END LOOP `two`"):
                with self.subTest(engine=engine, mismatch=body):
                    with self.assertRaises(ed.EditorError):
                        parts_count("CREATE PROCEDURE p() " + body, engine)

    def test_nonreserved_end_and_do_identifiers_keep_structural_boundaries(self):
        # MySQL sql/sql_yacc.yy:15894-15911 and MariaDB:16765,19364-19367
        # distinguish non-reserved names from compound-statement terminals.
        definitions = (
            "CREATE PROCEDURE p(start INT, end INT) BEGIN SELECT COALESCE(start, end); END",
            "CREATE PROCEDURE p() BEGIN INSERT INTO t (start, end) VALUES (1, 2); END",
            "CREATE PROCEDURE p() BEGIN DECLARE end INT; REPEAT SET end = 1; UNTIL end END REPEAT; END",
            "CREATE PROCEDURE p() BEGIN DECLARE end INT; REPEAT SELECT 1; UNTIL end > 0 END REPEAT; END",
            "CREATE PROCEDURE p() BEGIN DECLARE do INT; WHILE do > 0 DO SET do = do - 1; END WHILE; END",
            "CREATE PROCEDURE p() BEGIN DECLARE do INT; WHILE do DO SELECT 1; END WHILE; END",
            "CREATE PROCEDURE p() BEGIN WHILE 1 + do DO SELECT 1; END WHILE; END",
            "CREATE PROCEDURE p() BEGIN REPEAT SELECT 1; UNTIL 1 + end END REPEAT; END",
            "CREATE PROCEDURE p() BEGIN SELECT CASE WHEN end THEN end ELSE do END; END",
            "CREATE EVENT IF NOT EXISTS do ON SCHEDULE EVERY 1 DAY DO SELECT 1",
            "ALTER EVENT do DO SELECT 1",
        )
        for engine in ("mysql", "mariadb"):
            for definition in definitions:
                with self.subTest(engine=engine, definition=definition):
                    self.assert_definition_and_tail(definition, engine)

    def test_udf_without_body_is_one_ddl_and_never_absorbs_trailing_sql(self):
        # MySQL sql/sql_yacc.yy:7007-7011,18456-18464,18491-18497;
        # MariaDB:6196-6201,20530-20536.
        for engine in ("mysql", "mariadb"):
            for aggregate in ("", "AGGREGATE "):
                for returned in ("STRING", "INTEGER", "REAL", "DECIMAL"):
                    definition = "CREATE %sFUNCTION f RETURNS %s SONAME 'x.so'" % (aggregate, returned)
                    with self.subTest(engine=engine, definition=definition):
                        self.assert_definition_and_tail(definition, engine)
            for invalid in ("CREATE FUNCTION f RETURNS INTEGER SONAME 'x.so' SELECT 1",
                            "CREATE FUNCTION f RETURNS INTEGER SONAME",
                            "CREATE FUNCTION f RETURNS INTEGER BEGIN SELECT 1; END"):
                with self.subTest(engine=engine, invalid=invalid):
                    with self.assertRaises(ed.EditorError):
                        parts_count(invalid, engine)

    def test_top_level_mariadb_compounds_refuse_every_embedded_ddl_verb(self):
        # MariaDB sql/sql_yacc.yy:18696-18703,20812-20895 admits these as
        # SQLCOM_COMPOUND. DDL executed by a block needs standalone review.
        wrappers = ("BEGIN NOT ATOMIC %s; END", "IF 1 THEN %s; END IF",
                    "WHILE 1 DO %s; END WHILE", "LOOP %s; END LOOP",
                    "REPEAT %s; UNTIL 1 END REPEAT", "CASE WHEN 1 THEN %s; END CASE",
                    "BEGIN NOT ATOMIC IF 1 THEN %s; END IF; END",
                    "SET STATEMENT max_statement_time=1 FOR %s",
                    "SET STATEMENT max_statement_time=1 FOR BEGIN NOT ATOMIC %s; END")
        statements = ("CREATE DATABASE d", "CREATE OR REPLACE TABLE t (n INT)",
                      "CREATE VIEW v AS SELECT 1", "CREATE SEQUENCE s", "ALTER DATABASE d CHARACTER SET utf8mb4",
                      "ALTER TABLE t ADD COLUMN n INT", "DROP DATABASE prod", "DROP TABLES t",
                      "DROP VIEW v", "DROP PROCEDURE p", "DROP FUNCTION f", "DROP TRIGGER tr", "DROP EVENT e",
                      "RENAME TABLES t TO t2", "RENAME USER u TO u2", "TRUNCATE t")
        for wrapper in wrappers:
            for statement in statements:
                with self.subTest(wrapper=wrapper, statement=statement):
                    decision = self.decision(wrapper % statement, "mariadb")
                    self.assertEqual(decision["action"], "refuse", decision)
                    self.assertIn("standalone", decision["text"])

    def test_top_level_compound_writes_keep_write_policy_and_quoted_text_is_not_ddl(self):
        for body in ("UPDATE t SET n = 1 WHERE id = 1", "INSERT INTO t VALUES (1)",
                     "SELECT 'DROP DATABASE prod', `truncate`, t.drop FROM t", "SET @create = 1",
                     "SET truncate = 1", "DROP PREPARE stmt"):
            sql = "BEGIN NOT ATOMIC IF 1 THEN %s; END IF; END" % body
            with self.subTest(body=body):
                decision = self.decision(sql, "mariadb")
                self.assertEqual(decision["action"], "confirm", decision)
                self.assertNotEqual(decision["kind"], "read")

    def test_oracle_anonymous_blocks_refuse_including_after_possible_mode_change(self):
        session = types.SimpleNamespace(engine="mariadb", server_version="11.8.9-MariaDB",
                                        sql_mode="ORACLE", backslash_escapes=True, in_transaction=False,
                                        connection_id=1, server_identity={})
        for prefix in ("", "SET sql_mode='';//\n"):
            for block in ("BEGIN NULL; END", "DECLARE n INT; BEGIN n := 1; END",
                          "IF 1 THEN NULL; END IF"):
                with self.subTest(prefix=prefix, block=block):
                    with self.assertRaisesRegex(ed.EditorError, "ORACLE"):
                        ed._plan_run({"sql": "DELIMITER //\n" + prefix + block + "//"}, session)

    def test_long_charset_is_an_attribute_not_a_second_type_word(self):
        # mysql-8.4.0 sql/sql_yacc.yy:7081-7085: LONG is a complete type,
        # followed by opt_charset_with_opt_binary, including CHARACTER SET.
        for returned in ("LONG CHARACTER SET utf8mb4", "LONG CHAR SET utf8mb4",
                         "LONG CHARACTER SET 'utf8mb4'", "LONG CHARACTER SET `utf8mb4` COLLATE 'utf8mb4_bin'",
                         "LONG CHARSET utf8mb4", "LONG VARCHAR CHARACTER SET utf8mb4",
                         "LONG CHARACTER VARYING CHARACTER SET utf8mb4"):
            definition = "CREATE FUNCTION f() RETURNS %s BEGIN RETURN 'x'; END" % returned
            with self.subTest(returned=returned):
                self.assert_definition_and_tail(definition, "mysql")

    def test_trigger_order_names_keep_the_body_boundary(self):
        # Both grammars use ident_or_text for the ordering anchor. Qualified
        # identifiers also remain atomic in this boundary-only scanner; the
        # server, not this gate, validates the anchor's namespace.
        for engine in ("mysql", "mariadb"):
            for order in ("FOLLOWS", "PRECEDES"):
                for anchor in ("'other_trg'", '"other_trg"', "`other_trg`",
                               "d.other_trg", "`d` . `other_trg`"):
                    definition = ("CREATE TRIGGER tr BEFORE INSERT ON t FOR EACH ROW %s %s "
                                  "BEGIN SET NEW.n = 1; SET @seen = 1; END" % (order, anchor))
                    with self.subTest(engine=engine, order=order, anchor=anchor):
                        self.assert_definition_and_tail(definition, engine)

    def test_handlers_cursors_nested_declarations_and_loop_labels(self):
        bodies = (
            "BEGIN DECLARE x INT; DECLARE c CURSOR FOR SELECT d.t.end FROM d.t; "
            "DECLARE CONTINUE HANDLER FOR NOT FOUND SET x = 1; "
            "OPEN c; FETCH c INTO x; CLOSE c; END",
            "BEGIN DECLARE EXIT HANDLER FOR SQLSTATE VALUE '45000', 1062, SQLEXCEPTION "
            "BEGIN DECLARE x INT; SET x = 1; SET @seen = x; END; SELECT 2; END",
            "BEGIN DECLARE condition_name CONDITION FOR SQLSTATE '45000'; "
            "DECLARE CONTINUE HANDLER FOR condition_name, SQLWARNING BEGIN SELECT 1; END; SELECT 2; END",
            "outer_label: BEGIN DECLARE x INT; BEGIN DECLARE y INT; SET y = 2; END; "
            "lp: LOOP SET x = 1; LEAVE lp; END LOOP lp; "
            "wh: WHILE x < 2 DO SET x = x + 1; END WHILE wh; "
            "rp: REPEAT SET x = x + 1; UNTIL x > 3 END REPEAT rp; END outer_label",
        )
        for engine in ("mysql", "mariadb"):
            for body in bodies:
                with self.subTest(engine=engine, body=body):
                    self.assert_definition_and_tail("CREATE PROCEDURE p() " + body, engine)

    def test_mariadb_for_range_and_cursor_loops(self):
        # mariadb-server/sql/sql_yacc.yy:4769-4788,19426-19429.
        bodies = (
            "FOR i IN 1..10 DO SELECT i; SELECT 2; END FOR",
            "lp: FOR i IN REVERSE 1..10 DO SELECT i; END FOR lp",
            "FOR i IN (1 + 1)..(CASE WHEN 1 THEN 10 ELSE 2 END) DO SELECT i; END FOR",
            "FOR r IN (SELECT t.case, d.t.end FROM d.t) DO SELECT r.case; END FOR",
            "BEGIN DECLARE c CURSOR FOR SELECT 1 AS n; "
            "FOR r IN c DO BEGIN DECLARE n INT; SET n = r.n; END; END FOR; END",
            "BEGIN DECLARE c CURSOR (n INT) FOR SELECT n; "
            "FOR r IN c(2) DO SELECT r.n; END FOR; END",
        )
        for body in bodies:
            with self.subTest(body=body):
                self.assert_definition_and_tail("CREATE PROCEDURE p() " + body, "mariadb")

    def test_exception_sections_have_explicit_boundaries(self):
        # EXCEPTION is in MariaDB's Oracle grammar (20079-20102). This
        # exercises boundaries only, not permission to run ORACLE-mode SQL.
        for body in (
                "BEGIN SELECT 1; EXCEPTION WHEN SQLSTATE '45000' THEN SELECT 2; END",
                "BEGIN BEGIN SELECT 1; EXCEPTION WHEN NOT FOUND THEN SELECT 2; END; "
                "EXCEPTION WHEN 1062, SQLWARNING THEN BEGIN DECLARE x INT; SET x = 1; END; "
                "WHEN OTHERS THEN SELECT 3; END"):
            with self.subTest(body=body):
                self.assert_definition_and_tail("CREATE PROCEDURE p() " + body, "mariadb")

    def test_three_part_and_spaced_qualifiers_cannot_merge_statements(self):
        for engine in ("mysql", "mariadb"):
            for case, end in (("d.t.case", "d.t.end"), ("t .case", "t .end"),
                              ("d . t . case", "d . t . end"),
                              ("`d` . t.case", "`d` . t.end")):
                for head in ("CREATE PROCEDURE p() SELECT ",
                             "CREATE FUNCTION f() RETURNS INT RETURN ",
                             "CREATE TRIGGER tr BEFORE INSERT ON t FOR EACH ROW SET @v = ",
                             "CREATE EVENT e ON SCHEDULE EVERY 1 DAY DO SELECT "):
                    definition = head + case
                    with self.subTest(engine=engine, definition=definition):
                        self.assert_definition_and_tail(definition, engine)
                        merged = definition + "; DROP DATABASE prod; SELECT " + end
                        self.assertEqual(parts_count("DELIMITER //\n" + merged + "//", engine), 3)
                        self.assertEqual(self.decision(merged, engine)["action"], "refuse")

    def test_unbalanced_or_unknown_structures_fail_closed(self):
        for body in ("BEGIN SELECT 1; END IF", "BEGIN IF 1 THEN SELECT 1; END; END",
                     "BEGIN SELECT CASE WHEN 1 THEN 2; END", "BEGIN WHILE 1 SELECT 1; END WHILE; END",
                     "BEGIN FOR x IN 1..2 DO SELECT 1; END LOOP; END",
                     "BEGIN FOR x IN 1..2 SELECT 1; END FOR; END",
                     "BEGIN FOR x IN 1..2 DO SELECT 1; END; END",
                     "BEGIN EXCEPTION WHEN SQLWARNING SELECT 1; END",
                     "BEGIN EXCEPTION SELECT 1; END",
                     "BEGIN EXCEPTION WHEN SQLWARNING THEN SELECT 1; END IF",
                     "IS PROCEDURE nested() BEGIN SELECT 1; END; BEGIN SELECT 1; END"):
            with self.subTest(body=body):
                with self.assertRaisesRegex(ed.EditorError, "One DDL per confirmation"):
                    parts_count("CREATE PROCEDURE p() " + body, "mariadb")

    def test_drop_safety_net_covers_every_stored_definition(self):
        definitions = (
            "CREATE PROCEDURE p() BEGIN DROP SCHEMA prod; END",
            "CREATE FUNCTION f() RETURNS INT BEGIN DROP SCHEMA prod; RETURN 1; END",
            "CREATE TRIGGER tr BEFORE INSERT ON t FOR EACH ROW BEGIN DROP SCHEMA prod; END",
            "CREATE EVENT e ON SCHEDULE EVERY 1 DAY DO BEGIN DROP SCHEMA prod; END",
            "ALTER EVENT e DO BEGIN DROP SCHEMA prod; END",
            "CREATE PACKAGE BODY p AS PROCEDURE p1() DROP SCHEMA prod; END p",
        )
        for sql in definitions:
            with self.subTest(sql=sql):
                self.assertEqual(self.decision(sql, "mariadb").get("typed_target"), "prod")

    def test_drop_safety_net_ignores_strings_comments_and_identifier_parts(self):
        sql = ("CREATE PROCEDURE p() BEGIN SELECT 'DROP DATABASE secret', @drop, t.drop, "
               "col$drop, col☃drop; /* DROP SCHEMA other */ END")
        self.assertIsNone(self.decision(sql).get("typed_target"))

    def test_unknown_drop_target_refuses_and_duplicate_target_requires_typing(self):
        for target in ('"prod"', '@prod', 'd.prod'):
            with self.subTest(target=target):
                sql = "CREATE PROCEDURE p() BEGIN DROP DATABASE %s; END" % target
                self.assertEqual(self.decision(sql)["action"], "refuse")
        sql = "CREATE PROCEDURE p() BEGIN DROP DATABASE prod; DROP SCHEMA `prod`; END"
        self.assertEqual(self.decision(sql).get("typed_target"), "prod")

    def test_alter_event_without_replacing_body_is_one_ddl(self):
        for sql in ("ALTER EVENT e ENABLE", "ALTER EVENT e ON SCHEDULE EVERY 1 DAY DISABLE",
                    "ALTER EVENT e COMMENT 'leave the body unchanged'"):
            with self.subTest(sql=sql):
                self.assertEqual(parts_count(sql, "mysql"), 1)
                self.assertEqual(parts_count(sql + "; DROP DATABASE prod", "mysql"), 2)

    def test_wrapped_program_definition_refuses_unknown_boundary(self):
        for sql in ("SET STATEMENT max_statement_time=1 FOR CREATE PROCEDURE p() BEGIN SELECT 1; END",
                    "BEGIN NOT ATOMIC CREATE PROCEDURE p() SELECT 1; END"):
            with self.subTest(sql=sql):
                with self.assertRaisesRegex(ed.EditorError, "One DDL per confirmation"):
                    parts_count(sql, "mariadb")

    def test_event_bodies_without_begin_are_one_ddl(self):
        for engine in ("mysql", "mariadb"):
            for body in EVENT_BODIES:
                for head in ("ALTER EVENT e DO ", "CREATE EVENT e ON SCHEDULE EVERY 1 DAY DO "):
                    sql = head + body
                    with self.subTest(engine=engine, sql=sql):
                        self.assertEqual(parts_count(sql, engine), 1)

    def test_event_body_followed_by_a_second_statement_counts_two(self):
        for engine in ("mysql", "mariadb"):
            for body in EVENT_BODIES:
                sql = "ALTER EVENT e DO " + body + "; SELECT 1"
                with self.subTest(engine=engine, sql=sql):
                    self.assertEqual(parts_count(sql, engine), 2)

    def test_dotted_names_and_quoted_identifiers_are_not_block_keywords(self):
        for engine in ("mysql", "mariadb"):
            for body in ("BEGIN SELECT d.end, t.if FROM d.end; SELECT `while` FROM t.begin; END",
                         "BEGIN IF TRUE THEN SELECT d.end; END IF; SELECT t.case; END"):
                sql = "CREATE PROCEDURE p() " + body
                with self.subTest(engine=engine, sql=sql):
                    self.assertEqual(parts_count(sql, engine), 1)
                with self.subTest(engine=engine, sql=sql + "; DROP TABLE d.end"):
                    self.assertEqual(parts_count(sql + "; DROP TABLE d.end", engine), 2)

    def test_mariadb_packages_with_parenthesized_is_are_one_ddl(self):
        for sql in PACKAGE_DEFINITIONS:
            with self.subTest(sql=sql):
                self.assertEqual(parts_count(sql, "mariadb"), 1)

    def test_package_followed_by_a_second_statement_counts_two(self):
        for sql in PACKAGE_DEFINITIONS:
            for tail in ("; DROP TABLE d.end", "; SELECT 1"):
                with self.subTest(sql=sql + tail):
                    self.assertEqual(parts_count(sql + tail, "mariadb"), 2)

    def test_stray_end_after_a_package_cannot_merge_a_second_statement(self):
        # The safety direction of the parenthesized-IS bug: a leftover PACKAGE
        # block must not absorb a following statement into one confirmation.
        sql = ("CREATE PACKAGE BODY p AS PROCEDURE p1(x INT DEFAULT (1 IS NULL)); BEGIN p1; END p; "
               "DROP TABLE d.end; END")
        self.assertGreaterEqual(parts_count(sql, "mariadb"), 2)

    def test_aggregate_function_create_is_one_ddl(self):
        for sql in AGGREGATE_DEFINITIONS:
            with self.subTest(sql=sql):
                self.assertEqual(parts_count(sql, "mariadb"), 1)
            with self.subTest(sql=sql + "; SELECT 1"):
                self.assertEqual(parts_count(sql + "; SELECT 1", "mariadb"), 2)

    def test_e8_definitions_keep_one_part_with_terminal_comments(self):
        for definition in E8_DEFINITIONS:
            engine = "mariadb" if "PACKAGE" in definition else "mysql"
            for terminal in ("", ";", "; -- trailing comment\n", "; # trailing comment\n",
                             "; /* trailing comment */"):
                sql = definition + terminal
                with self.subTest(sql=sql):
                    self.assertEqual(parts_count(sql, engine), 1)

    def test_e8_definitions_with_a_second_statement_count_two(self):
        for definition in E8_DEFINITIONS:
            engine = "mariadb" if "PACKAGE" in definition else "mysql"
            sql = definition + "; DROP DATABASE d"
            with self.subTest(sql=sql):
                self.assertEqual(parts_count(sql, engine), 2)


@unittest.skipIf(os.name == "nt", "the fake client is a POSIX script")
class EditorOpenSafetyTests(unittest.TestCase):
    """Exercise real policy/identity/session code against the fake client."""

    engine = "mysql"
    call = process_fixtures.ProcessTests.call
    run_sql = process_fixtures.ProcessTests.run_sql
    session_log = process_fixtures.ProcessTests.session_log
    rewrite_client = identity_fixtures.EditorIdentityTests.rewrite_client
    inject_answers = identity_fixtures.EditorIdentityTests.inject_answers
    packets = identity_fixtures.EditorIdentityTests.packets
    route_matching = setup_fixtures.EditorSetupPacketTests.route_matching

    def setUp(self):
        process_fixtures.ProcessTests.setUp(self)
        spawn = self.processes.side_effect
        self.children = []

        def tracked_spawn(*args, **kwargs):
            child = spawn(*args, **kwargs)
            self.children.append(child)
            return child

        self.processes.side_effect = tracked_spawn
        self.addCleanup(self.reap_test_clients)

    def reap_test_clients(self):
        self.registry.close()
        # Also reap a regression's unowned child after a failed assertion.
        for child in self.children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)

    def unavailable_identity(self):
        self.inject_answers('''        if upper.startswith("SHOW GLOBAL VARIABLES WHERE "):
            self.error(line, 1142, "42000", "Identity denied")
            return
''')

    def assert_new_session_preference_refused(self, wanted):
        payload = {"safe_updates": wanted}
        if not wanted:
            review = self.call("/api/editor/open", **payload)
            self.assertEqual(review["status"], "needs_confirmation")
            payload.update(confirmed=True, confirm_token=review["confirmation"]["token"])
        with self.assertRaises(ed.EditorError) as caught:
            self.call("/api/editor/open", **payload)
        self.assertEqual(caught.exception.code, "identity_unavailable")
        self.assertIsNone(self.registry.get("tab1"))
        self.assertEqual(self.call("/api/editor/status")["status"], "none")
        record = self.store.audit_records()[0]
        self.assertEqual((record["status"], record["outcome"]), ("refused", "identity_unavailable"))
        self.assertTrue(record["finished_at"])
        self.assertTrue(all(proc.poll() is not None for proc in self.children))

    def test_new_session_with_unavailable_identity_is_removed_when_hardening_refuses(self):
        self.unavailable_identity()
        self.assert_new_session_preference_refused(True)

    def test_new_session_with_unavailable_identity_is_removed_when_lowering_refuses(self):
        self.unavailable_identity()
        self.assert_new_session_preference_refused(False)

    def test_new_routed_session_is_removed_when_hardening_refuses(self):
        self.route_matching("AS database_name", "connection_id")
        self.assert_new_session_preference_refused(True)
        self.assertEqual(sum("--xml" in call[0][0] for call in self.processes.call_args_list), 2)

    def test_existing_session_survives_refused_preference(self):
        self.unavailable_identity()
        self.assertEqual(self.run_sql("SELECT 7")["status"], "complete")
        session = self.registry.get("tab1")
        self.assertTrue(session.safe_updates)
        with self.assertRaises(ed.EditorError):
            self.call("/api/editor/open", safe_updates=True)
        self.assertIs(self.registry.get("tab1"), session)
        self.assertFalse(session.ended)
        self.assertTrue(session.safe_updates)

    def test_definition_drop_typed_name_is_required_before_any_client_opens(self):
        sql = "DELIMITER //\nCREATE PROCEDURE p() BEGIN DROP DATABASE `prod`; END//"
        review = self.call("/api/editor/run", sql=sql)
        self.assertEqual(review["status"], "needs_confirmation")
        self.assertEqual(review["confirmation"]["typed_target"], "prod")
        for typed in (None, "wrong", "PROD"):
            with self.subTest(typed=typed):
                refused = self.call("/api/editor/run", sql=sql, confirmed=True, typed_target=typed,
                                    confirm_token=review["confirmation"]["token"])
                self.assertEqual(refused["status"], "needs_confirmation")
                self.assertFalse(self.children)
        result = self.call("/api/editor/run", sql=sql, confirmed=True, typed_target="prod",
                           confirm_token=review["confirmation"]["token"])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["run"]["statements"][0]["status"], "ok")
        record = next(r for r in self.store.audit_records() if r["id"] == result["run"]["audit_id"])
        self.assertEqual(record["status"], "ok")

    def assert_close_during_retry(self, shutdown):
        self.route_matching("SET SESSION sql_safe_updates = 1")
        join = threading.Thread.join
        observed = []

        def drain(reader, timeout=None):
            session = self.registry.get("tab1")
            if session is not None and reader in session._readers and not observed:
                observed.append((session.ended, session.state, session.ready.is_set()))
                if shutdown:
                    self.registry.close()
                else:
                    self.assertTrue(self.call("/api/editor/close", discard=True)["closed"])
            return join(reader, timeout)

        with mock.patch.object(threading.Thread, "join", drain):
            with self.assertRaises(ConnectorError):
                self.call("/api/editor/open")
        self.assertEqual(observed, [(False, "starting", False)])
        self.assertIsNone(self.registry.get("tab1"))
        self.assertEqual(sum("--xml" in call[0][0] for call in self.processes.call_args_list), 1)
        self.assertTrue(all(proc.poll() is not None for proc in self.children))

    def test_close_while_startup_readers_drain_prevents_retry(self):
        self.assert_close_during_retry(shutdown=False)

    def test_shutdown_while_startup_readers_drain_prevents_retry(self):
        self.assert_close_during_retry(shutdown=True)

    def test_registry_removal_after_retry_reset_prevents_replacement_spawn(self):
        self.route_matching("SET SESSION sql_safe_updates = 1")
        features = ed._client_features
        removed = []

        def remove_before_spawn(binary, env):
            session = self.registry.get("tab1")
            if session is not None and session._routed_startup:
                # Shutdown removes ownership before calling session.end().
                self.registry.remove("tab1", session)
                removed.append(session)
            return features(binary, env)

        with mock.patch.object(ed, "_client_features", remove_before_spawn):
            with self.assertRaises(ConnectorError):
                self.call("/api/editor/open")
        self.assertEqual(len(removed), 1)
        self.assertTrue(removed[0].ended)
        self.assertEqual(sum("--xml" in call[0][0] for call in self.processes.call_args_list), 1)
        self.assertTrue(all(proc.poll() is not None for proc in self.children))


if __name__ == "__main__":
    unittest.main()
