"""The Query editor's one-DDL gate: _ddl_codes on planned units.

A CREATE PROCEDURE/FUNCTION/TRIGGER/EVENT/PACKAGE body (and an ALTER EVENT
body) is one DDL statement even with semicolons inside; anything after the
definition's end is a second statement and must be counted. Statement-level
IF/LOOP/WHILE/REPEAT/CASE blocks without an outer BEGIN, parenthesized or
post-DECLARE AS/IS in MariaDB packages, dotted name parts and quoted
identifiers must not change that counting.
"""
import unittest

from myflames import editor_session as ed

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


if __name__ == "__main__":
    unittest.main()
