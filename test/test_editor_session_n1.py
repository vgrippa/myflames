"""Round N: client newline, comment boundaries and mode-save provenance.

These tests use the real lexer, planner and policy. Client expectations are
source-grounded in mysql-8.4.0 and mariadb-11.8.9 client/mysql.cc and checked
independently by client_addline_oracle; no server or transport is mocked.
"""
import unittest

from client_addline_oracle import client_packets
from myflames import dbops, editor_session as ed
from test_editor_session_j2 import SAFETY, bound_session
from test_editor_session_m1 import SERVERS


NEWLINE_TEXTS = (
    "DELETE FROM t WHERE flag =\ndelimiter -\n- 1 AND id = 5",
    "SELECT 1 AS\ndelimiter/\n* ' */ ; DELETE FROM t; -- '",
    "SELECT 1 AS\ndelimiter/\n* ' */ INTO OUTFILE '/tmp/out'; -- '",
    "SELECT a,\ndelimiter_x\nFROM t",
    # add_line tests the quote state at the END of the physical line.
    # A prefix inside a string is hazardous if that line closes the string.
    "SELECT 'first\ndelimiter_x' -\n- 1",
    "SELECT 'first\ndelimiter_x'/\n* ' */ ; DELETE FROM t; -- '",
)


class EditorPlanningN1Tests(unittest.TestCase):
    def plan(self, sql, engine="mysql", version="8.4.0", client="mysql", mode=""):
        session = bound_session(engine, version, mode)
        session.client_engine = client
        return ed._plan_run({"sql": sql}, session)

    def test_column_zero_delimiter_prefix_refuses_even_with_pending_sql(self):
        for engine, version in SERVERS:
            for client in ("mysql", "mariadb", None):
                for original in NEWLINE_TEXTS:
                    for spelling in ("delimiter", "DeLiMiTeR"):
                        sql = original.replace("delimiter", spelling)
                        with self.subTest(engine=engine, version=version, client=client, sql=sql):
                            with self.assertRaisesRegex(ed.EditorError, "newline"):
                                self.plan(sql, engine, version, client)
                            scan = dbops.lex_sql(sql, client=True, client_engine=client,
                                                client_delimiter=";", engine=engine, server_version=version)
                            self.assertTrue(any("newline" in reason for _, reason in scan["issues"]))

    def test_indented_and_quoted_delimiter_identifiers_keep_their_newlines(self):
        for client in ("mysql", "mariadb", None):
            for sql in ("SELECT a,\n delimiter_x\nFROM t", "SELECT a,\n`delimiter_x`\nFROM t",
                        "SELECT 'first\ndelimiter_x\nlast'", "SELECT 1 /* delimiter_x */"):
                with self.subTest(client=client, sql=sql):
                    unit = self.plan(sql, client=client)["units"][0]
                    self.assertEqual(unit["send"], sql)
                    if client:
                        self.assertEqual(client_packets(sql, client), ([sql], "", []))
            units = self.plan("SELECT 1;\nDELIMITER //\nSELECT 2//", client=client)["units"]
            self.assertEqual([unit["send"] for unit in units], ["SELECT 1", "SELECT 2"])

    def test_client_only_comment_closers_cannot_mutate_server_data(self):
        cases = (
            ("SELECT [a/*b] + [c*/d]", "mariadb", "11.8.9", "MSSQL", ("mysql", "mariadb", None)),
            ("SELECT /*M!100000 '*/' -- '\n*/", "mariadb", "11.8.9", "", ("mysql", None)),
            ("SELECT /*!999999 '/*' */ +1/*a*/+2 */ 1", "mysql", "8.4.0", "", ("mysql", "mariadb", None)),
            ("SELECT /*!999999 '/*' */ +1/*a*/+2 */ 1", "mariadb", "11.8.9", "", ("mysql", "mariadb", None)),
        )
        for sql, engine, version, mode, clients in cases:
            for client in clients:
                with self.subTest(sql=sql, client=client):
                    if client:
                        self.assertNotEqual(client_packets(sql, client)[0], [sql])
                    with self.assertRaisesRegex(ed.EditorError, "comment.*closer"):
                        self.plan(sql, engine, version, client, mode)

    def test_whitespace_edged_delimiters_refuse_before_normalization(self):
        for client in ("mysql", "mariadb", None):
            for argument in ("' +'", "'+ '", "'\t+'", "'+\t'", '" ;"', "`+ `"):
                with self.subTest(client=client, argument=argument):
                    with self.assertRaisesRegex(ed.EditorError, "whitespace"):
                        self.plan("DELIMITER " + argument + "\nSELECT 1 /* a */+ 2 +", client=client)
            for delimiter in (" +", "+ ", "\t+", "+\t"):
                with self.assertRaisesRegex(ValueError, "whitespace"):
                    dbops.split_statements("SELECT 1", delimiter=delimiter, client_engine=client)

    def test_delimiter_arguments_follow_the_selected_client_family(self):
        # get_arg: MySQL 8.4.0:4861-4900; MariaDB 11.8.9:5023-5080.
        # Text after a closing quote is ignored by both clients. MySQL
        # accepts a missing closer; MariaDB unescapes doubled quotes.
        cases = (("'//", "//", None), ("'a''b'ignored", "a", "a'b"),
                 ('"a""b" trailing', "a", 'a"b'), ("`a``b` tail", "a", "a`b"),
                 ("'//'ignored", "//", "//"), ("'//' extra", "//", "//"),
                 ("// ignored", "//", "//"), (r"\+", "+", "+"),
                 (r"'\+'", None, "+"), ("''", None, None))
        for argument, mysql, maria in cases:
            for client, expected in (("mysql", mysql), ("mariadb", maria),
                                     (None, mysql if mysql == maria else None)):
                sql = "DELIMITER " + argument + "\nSELECT 1" + (expected or "//")
                with self.subTest(client=client, argument=argument):
                    if expected is None:
                        with self.assertRaises(ed.EditorError):
                            self.plan(sql, client=client)
                    else:
                        unit = self.plan(sql, client=client)["units"][0]
                        self.assertEqual((unit["send"], unit["delimiter"]), ("SELECT 1", expected))

    def test_delimiter_command_name_uses_ascii_client_whitespace(self):
        for client in ("mysql", "mariadb", None):
            for separator in (" ", "\t", "\v", "\f"):
                with self.subTest(client=client, separator=separator):
                    units = self.plan("DELIMITER" + separator + "'//'\nSELECT 1//", client=client)["units"]
                    self.assertEqual([unit["send"] for unit in units], ["SELECT 1"])

    def test_normalization_cannot_create_a_delimiter_across_a_closing_slash(self):
        # Even without edge whitespace, an inserted space can complete a
        # delimiter beginning on an executable comment's final slash.
        sql = "DELIMITER '/ +'\nSELECT /*!40101 1 */+2/ +"
        for client in ("mysql", "mariadb", None):
            with self.subTest(client=client), self.assertRaisesRegex(ed.EditorError, "normaliz"):
                self.plan(sql, client=client)

    def test_only_call_free_set_statements_preserve_saved_modes_between_definitions(self):
        changes = ("SELECT @v", "SELECT f()", "DO f()", "INSERT INTO t VALUES (1)",
                   "UPDATE t SET n=1", "DELETE FROM t WHERE id=1", "SHOW TABLES",
                   "SET @other=f()", "SET @other=`f`()", "SET @other=db.f()",
                   "SET @other=(SELECT f())", "SET STATEMENT max_statement_time=1 FOR SELECT f()",
                   "SET STATEMENT max_statement_time=1 FOR UPDATE t SET n=1",
                   "SET STATEMENT max_statement_time=1 FOR SELECT @v")
        for engine, version in (("mysql", "8.4.0"), ("mariadb", "11.8.9")):
            for change in changes:
                sql = "SET @v=@@sql_mode; " + change + "; SET sql_mode=@v; SELECT 1"
                with self.subTest(engine=engine, change=change):
                    plan = self.plan(sql, engine, version, engine)
                    self.assertIsNone(plan["units"][-1]["sql_mode"])
                    self.assertTrue(plan["units"][-1]["mode_unknown"])
            safe = "SET @v=@@sql_mode; SET @other=@v; SET @x=(1+2); SET sql_mode=@v; SELECT 1"
            self.assertEqual(self.plan(safe, engine, version, engine)["units"][-1]["sql_mode"], "")

    def test_stored_definitions_preserve_saves_without_executing_the_body(self):
        definitions = ("CREATE PROCEDURE p() BEGIN SET @v='ORACLE'; CALL f(); END",
                       "CREATE FUNCTION f() RETURNS INT RETURN 1",
                       "CREATE TRIGGER tr BEFORE INSERT ON t FOR EACH ROW SET @v='ORACLE'",
                       "CREATE EVENT e ON SCHEDULE EVERY 1 DAY DO SET @v='ORACLE'",
                       "CREATE OR REPLACE DEFINER=`u`@`h` PROCEDURE p() SELECT f()")
        for engine, version in (("mysql", "8.4.0"), ("mariadb", "11.8.9")):
            for definition in definitions:
                sql = ("SET @v=@@sql_mode;\nDELIMITER //\n" + definition +
                       "//\nDELIMITER ;\nSET sql_mode=@v; SELECT 1")
                with self.subTest(engine=engine, definition=definition):
                    plan = self.plan(sql, engine, version, engine)
                    self.assertEqual(plan["units"][-1]["sql_mode"], "")
                    self.assertEqual(ed._decision(plan, SAFETY)["action"], "confirm")

    def test_package_mode_provenance_and_trailing_execution_are_separate(self):
        # Prediction only: the editor's ORACLE/package execution restrictions
        # remain independently enforced. Do not treat a trailing CALL as
        # part of a stored definition just because the packet starts CREATE.
        for code in ("CREATE PACKAGE p AS PROCEDURE p1(); END",
                     "CREATE PACKAGE BODY p AS PROCEDURE p1() SELECT 1; END"):
            saved = {"v": "ANSI_QUOTES"}
            unit = {"code": code, "sql_mode": "", "idents": []}
            self.assertEqual(ed._next_sql_mode(unit, saved, "mariadb"), "")
            self.assertEqual(saved, {"v": "ANSI_QUOTES"})
        saved = {"v": "ANSI_QUOTES"}
        unit = {"code": "CREATE PROCEDURE p() SELECT 1; SELECT f()", "sql_mode": "", "idents": []}
        ed._next_sql_mode(unit, saved, "mysql")
        self.assertEqual(saved, {})


if __name__ == "__main__":
    unittest.main()
