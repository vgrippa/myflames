"""Round O: client framing, bounded DELIMITER arguments and mode provenance.

Real lexer, planner, policy and MCP read adapter; no mocked safety decisions.
Client packets are independently checked against the tagged-source oracle.
"""
import unittest

from client_addline_oracle import client_packets
from myflames import dbops, editor_session as ed, mcp_server
from test_editor_session_j2 import SAFETY, bound_session
from test_editor_session_m1 import SERVERS


DOLLAR_NEWLINE = ("SELECT 1 /*!999999 '/*' $q$ */ ' /* $q$ */ $q$\n"
                  "delimiter$q$ */ + 1 /\n"
                  "* /*!999999 $q$ ' /* $q$ */ INTO OUTFILE '/tmp/out' -- */")
DOLLAR_CLOSER = "SELECT 1 /*!999999 '/*' $q$ */ ' /* $q$ */ /*a*/x $q$ ' $q$ */"
MSSQL_CLOSER = "SELECT [$q$ ' $q$] + [/*a*/x] + [$q$ ' $q$]"
QUOTE_DELIMITER = 'DELIMITER "\'a"\nSELECT \'1\'\'aDELETE FROM t\'aSELECT \'2\' \'a'
LONG_DELIMITER = "DELIMITER 123456789012345;\nSELECT 1 AS a123456789012345DELETE FROM t123456789012345;"


class EditorPlanningO1Tests(unittest.TestCase):
    def plan(self, sql, engine="mysql", version="8.4.0", client="mysql", mode=""):
        session = bound_session(engine, version, mode)
        session.client_engine = client
        return ed._plan_run({"sql": sql}, session)

    def test_dollar_closing_line_has_the_same_newline_guard_as_other_quotes(self):
        for quote in ("'", '"', "`", "$$", "$q$", "$Q_1$"):
            for prefix in ("delimiter", "DeLiMiTeR_x"):
                sql = "SELECT " + quote + "first\n" + prefix + quote + " -\n- 1"
                with self.subTest(quote=quote, prefix=prefix):
                    scan = dbops.lex_sql(sql, client=True, dollar_quotes=True,
                                        client_engine="mysql", client_delimiter=";")
                    self.assertIn((sql.index(prefix), dbops.REASON_DELIMITER_NEWLINE), scan["issues"])
        for sql in ("SELECT $q$first\ndelimiter\nlast$q$", "SELECT $q$first\n delimiter$q$"):
            scan = dbops.lex_sql(sql, client=True, dollar_quotes=True, client_engine="mysql", client_delimiter=";")
            self.assertNotIn(dbops.REASON_DELIMITER_NEWLINE, [reason for unused, reason in scan["issues"]])
            self.assertEqual(client_packets(sql, "mysql"), ([sql], "", []))

    def test_skipped_server_comment_cannot_hide_different_client_token_spans(self):
        # No dangerous line here: the server masks the whole skipped comment
        # identically, but the client's quotes and closers differ.
        for sql, engine, version, mode in ((DOLLAR_CLOSER, "mysql", "8.4.0", ""),
                                           (MSSQL_CLOSER, "mariadb", "11.8.9", "MSSQL")):
            args = dict(engine=engine, server_version=version, sql_mode=mode, client_engine="mysql")
            plain = dbops.split_statements(sql, **args)
            dollars = dbops.split_statements(sql, dollar_quotes=True, **args)
            with self.subTest(sql=sql):
                self.assertEqual(plain[0]["token_spans"], dollars[0]["token_spans"])
                self.assertNotEqual(dbops._target_reading(plain), dbops._target_reading(dollars))
                with self.assertRaises(ValueError):
                    dbops.target_statement(sql, **args)

    def test_reported_dollar_newline_payloads_refuse_before_policy_or_transport(self):
        texts = (DOLLAR_NEWLINE, DOLLAR_NEWLINE.replace("INTO OUTFILE '/tmp/out'", "; DELETE FROM t;"),
                 "DELETE FROM t WHERE flag = 1 /*!999999 '/*' $q$ */ ' /* $q$ */ $q$\n"
                 "delimiter$q$ */ -\n- 1 /*!999999 $q$ ' $q$ */ AND id = 5")
        for engine, version in SERVERS:
            for client in ("mysql", "mariadb", None):
                for sql in texts:
                    with self.subTest(engine=engine, version=version, client=client, sql=sql):
                        with self.assertRaises(ed.EditorError):
                            self.plan(sql, engine, version, client)
                        verdict = dbops.classify(sql, engine=engine, server_version=version,
                                                sql_mode="", client_engine=client)
                        self.assertNotEqual(verdict["kind"], "read")
        with self.assertRaises(ValueError):
            mcp_server._read_sql(DOLLAR_NEWLINE, engine="mysql", server_version="8.4.0")

    def test_quote_starting_delimiter_splits_at_the_second_doubled_quote(self):
        expected = ["SELECT '1'", "DELETE FROM t", "SELECT '2'"]
        for engine, version in SERVERS:
            for client in ("mysql", "mariadb", None):
                with self.subTest(engine=engine, version=version, client=client):
                    units = dbops.split_statements(QUOTE_DELIMITER, engine=engine, server_version=version,
                                                  client_engine=client, sql_mode="")
                    self.assertEqual([unit["text"] for unit in units], expected)
                    self.assertFalse(any(unit["issues"] or unit["unclosed"] for unit in units))
                    plan = self.plan(QUOTE_DELIMITER, engine, version, client)
                    self.assertEqual([unit["send"] for unit in plan["units"]], expected)
                    self.assertEqual(ed._decision(plan, SAFETY)["action"], "confirm")
                    self.assertNotEqual(dbops.classify(QUOTE_DELIMITER, engine=engine, server_version=version,
                                                      client_engine=client, sql_mode="")["kind"], "read")
                    if client:
                        self.assertEqual(client_packets(QUOTE_DELIMITER, client, delimiter=";", append_delimiter=False),
                                         (expected, "", ["DELIMITER"]))
        with self.assertRaises(ValueError):
            mcp_server._read_sql(QUOTE_DELIMITER)

    def test_all_quote_characters_and_valid_quote_delimiters_keep_real_boundaries(self):
        for client in ("mysql", "mariadb", None):
            for quote in ("'", '"', "`"):
                outer = '"' if quote != '"' else "'"
                delimiter = quote + "a"
                text = "SELECT " + quote + "hello" + quote
                script = "DELIMITER " + outer + delimiter + outer + "\n" + text + delimiter
                with self.subTest(client=client, quote=quote):
                    self.assertEqual([unit["send"] for unit in self.plan(script, client=client)["units"]], [text])
                    if client:
                        self.assertEqual(client_packets(script, client, delimiter=";", append_delimiter=False)[0], [text])
            # Doubled quotes without a delimiter at the second quote retain
            # their server value, including a delimiter elsewhere in the body.
            self.assertEqual(self.plan("DELIMITER //\nSELECT '1''a'//", client=client)["units"][0]["send"],
                             "SELECT '1''a'")

    def test_long_delimiters_refuse_and_limits_count_utf8_bytes(self):
        for client in ("mysql", "mariadb", None):
            for delimiter in ("x" * 16, "x" * 50, "é" * 8, "界" * 6, "x" * 14 + "é"):
                with self.subTest(client=client, delimiter=delimiter):
                    sql = "DELIMITER '" + delimiter + "'\nSELECT 1" + delimiter
                    with self.assertRaisesRegex(ed.EditorError, "15.*bytes"):
                        self.plan(sql, client=client)
                    with self.assertRaisesRegex(ValueError, "15.*bytes"):
                        dbops.split_statements("SELECT 1", delimiter=delimiter, client_engine=client)
            for delimiter in ("x" * 15, "x" + "é" * 7, "abc" + "界" * 4):
                self.assertEqual(self.plan("DELIMITER '" + delimiter + "'\nSELECT 1" + delimiter,
                                           client=client)["units"][0]["send"], "SELECT 1")
            # NUL terminates a client C string; a surrogate cannot be sent
            # as UTF-8. Neither may become a usable delimiter even below
            # the byte limit, through a command or a direct caller.
            for delimiter in ("+\x00x", "\ud800"):
                with self.assertRaises(ed.EditorError):
                    self.plan("DELIMITER '" + delimiter + "'\nSELECT 1", client=client)
                with self.assertRaises(ValueError):
                    dbops.split_statements("SELECT 1", delimiter=delimiter, client_engine=client)
            with self.assertRaises(ed.EditorError):
                self.plan(LONG_DELIMITER, client=client)
            self.assertNotEqual(dbops.classify(LONG_DELIMITER, client_engine=client)["kind"], "read")
        with self.assertRaises(ValueError):
            mcp_server._read_sql(LONG_DELIMITER)

    def test_multibyte_first_delimiter_character_refuses_before_client_framing(self):
        # add_line copies a multibyte character and continues before its
        # delimiter check: mysql-8.4.0:client/mysql.cc:2759-2770;
        # mariadb-11.8.9:client/mysql.cc:2643-2657. A Unicode first character
        # therefore never matches the delimiter in the UTF-8 client.
        for client in ("mysql", "mariadb", None):
            for delimiter in ("é", "界" * 5, "é" + "x" * 13):
                with self.subTest(client=client, delimiter=delimiter):
                    with self.assertRaises(ed.EditorError):
                        self.plan("DELIMITER '" + delimiter + "'\nSELECT 1" + delimiter,
                                  client=client)
                    with self.assertRaises(ValueError):
                        dbops.split_statements("SELECT 1", delimiter=delimiter, client_engine=client)

    def test_delimiter_command_line_limit_includes_ignored_tail_and_utf8(self):
        for client in ("mysql", "mariadb", None):
            for length in (254, 255, 256, 257, 300):
                for command in ("DELIMITER " + " " * (length - 14) + "'//'",
                                "DELIMITER '//'" + "x" * (length - 14)):
                    self.assertEqual(len(command.encode("utf-8")), length)
                    with self.subTest(client=client, length=length, command=command):
                        if length > 255:
                            with self.assertRaisesRegex(ed.EditorError, "255.*bytes"):
                                self.plan(command + "\nSELECT 1//", client=client)
                        else:
                            self.assertEqual(self.plan(command + "\nSELECT 1//", client=client)["units"][0]["send"],
                                             "SELECT 1")
            for command in ("DELIMITER " + " " * 244 + "'/;'", "DELIMITER '//'" + "é" * 121,
                            " " * 256 + "DELIMITER //"):
                with self.assertRaises(ed.EditorError):
                    self.plan(command + "\nSELECT 1//", client=client)

    def test_dollar_exposed_client_comment_closers_refuse(self):
        for sql, engine, version, mode in ((DOLLAR_CLOSER, "mysql", "8.4.0", ""),
                                           (MSSQL_CLOSER, "mariadb", "11.8.9", "MSSQL")):
            for client in ("mysql", None):
                with self.subTest(sql=sql, client=client):
                    self.assertNotEqual(client_packets(sql, "mysql")[0], [sql])
                    with self.assertRaises(ed.EditorError):
                        self.plan(sql, engine, version, client, mode)

    def test_create_event_schedule_expressions_invalidate_saved_modes(self):
        schedules = ("EVERY IF((@v := 'NO_BACKSLASH_ESCAPES') = '', 1, 1) DAY",
                     "AT f()", "EVERY 1 DAY STARTS f()", "EVERY 1 DAY ENDS f()")
        for engine, version in (("mysql", "8.4.0"), ("mariadb", "11.8.9")):
            for schedule in schedules:
                for prefix in ("CREATE", "CREATE DEFINER=`u`@`h`"):
                    sql = ("SET @v=@@sql_mode; " + prefix + " EVENT e ON SCHEDULE " + schedule +
                           " DO SELECT 1; SET sql_mode=@v; SELECT 1")
                    with self.subTest(engine=engine, schedule=schedule, prefix=prefix):
                        unit = self.plan(sql, engine, version, engine)["units"][-1]
                        self.assertIsNone(unit["sql_mode"])
                        self.assertTrue(unit["mode_unknown"])

    def test_subqueries_and_table_expressions_in_set_invalidate_saved_modes(self):
        changes = ("SET @other=(SELECT c FROM v)", "SET @other=(TABLE v LIMIT 1)",
                   "SET @other=(WITH q AS (SELECT c FROM v) SELECT c FROM q)",
                   "SET @other=(SELECT 1)", "SET @other=(SELECT c FROM `v`)",
                   "SET STATEMENT max_statement_time=1 FOR SET @other=(SELECT c FROM v)")
        for engine, version in (("mysql", "8.4.0"), ("mariadb", "11.8.9")):
            for change in changes:
                sql = "SET @v=@@sql_mode; " + change + "; SET sql_mode=@v; SELECT 1"
                with self.subTest(engine=engine, change=change):
                    unit = self.plan(sql, engine, version, engine)["units"][-1]
                    self.assertIsNone(unit["sql_mode"])
                    self.assertTrue(unit["mode_unknown"])
            safe = ("SET @v=@@sql_mode; SET @other='SELECT c FROM v'; SET @table=1; "
                    "SET `select`=1; SET @x=(1+2); SET sql_mode=@v; SELECT 1")
            self.assertEqual(self.plan(safe, engine, version, engine)["units"][-1]["sql_mode"], "")


if __name__ == "__main__":
    unittest.main()
