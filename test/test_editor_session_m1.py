"""Round M1 lexer/editor regressions using real planning and policy checks."""
import unittest
from unittest import mock

from myflames import dbops, editor_session as ed
from test_editor_session_j2 import SAFETY, bound_session


SERVERS = (("mysql", "5.7.44"), ("mysql", "8.0.46"), ("mysql", "8.1.0"),
           ("mysql", "8.4.0"), ("mysql", "9.7.0"), ("mariadb", "10.11.16"),
           ("mariadb", "11.8.9"), ("mariadb", "12.0.1"))


class EditorPlanningM1Tests(unittest.TestCase):
    def setUp(self):
        # The general cases use the Unix binary reader. The Windows-specific
        # case below overrides only the platform fact, never a safety check.
        platform = mock.patch.object(dbops.sys, "platform", "linux")
        platform.start()
        self.addCleanup(platform.stop)

    def plan(self, sql, engine="mysql", version="8.4.0", client="mysql", mode=""):
        session = bound_session(engine, version, mode)
        session.client_engine = client
        return ed._plan_run({"sql": sql}, session)

    def test_command_lines_inside_every_comment_kind_refuse_on_every_reading(self):
        for engine, version in SERVERS:
            for client in ("mysql", "mariadb", None):
                for opener in ("/* ", "/*+ ", "/*!999999 ", "/*M!999999 "):
                    for command in ("DELIMITER ?", "delimiter //", "charset utf8mb4", "\\C utf8mb4"):
                        sql = opener + "\n\t " + command + "\nDELETE FROM t?\n*/ SELECT 1"
                        with self.subTest(engine=engine, version=version, client=client,
                                          opener=opener, command=command):
                            # Unknown families already have a stricter
                            # cross-reading gate: MariaDB also exposes \\C
                            # in /*M!, unlike MySQL's ordinary comment.
                            reason = ("client family is unknown" if client is None and
                                      opener.startswith("/*M!") and command.startswith("\\") else
                                      "multi-line comment.*client command")
                            with self.assertRaisesRegex(ed.EditorError, reason):
                                self.plan(sql, engine, version, client)
                            scan = dbops.lex_sql(sql, engine=engine, server_version=version,
                                                client=True, client_engine=client, client_delimiter=";")
                            self.assertIn(dbops.REASON_COMMENT_COMMAND, [reason for at, reason in scan["issues"]])

    def test_comment_command_rule_is_conservative_even_after_pending_sql(self):
        for client in ("mysql", "mariadb", None):
            for name in ("clear", "connect", "edit", "ego", "exit", "go", "help", "nopager", "notee",
                         "pager", "print", "prompt", "quit", "rehash", "source", "status", "system",
                         "tee", "use", "warnings", "nowarning", "resetconnection", "query_attributes",
                         "ssl_session_data_print", "sandbox", "costs", "?", "\\g", "\\G", "\\N", "\\!"):
                sql = "SELECT 1 /* note\r\n  " + name + "\r\n*/"
                with self.subTest(client=client, name=name):
                    with self.assertRaisesRegex(ed.EditorError, "multi-line comment.*client command"):
                        self.plan(sql, client=client)

    def test_comments_without_command_lines_and_quoted_lookalikes_stay_usable(self):
        for client in ("mysql", "mariadb", None):
            for sql in ("/* note\nmore words */ SELECT 1", "/* note\r\nmore words */ SELECT 1",
                        "/* note\ncharset_name and status_code */ SELECT 1",
                        "/* note\nwords before DELIMITER // */ SELECT 1",
                        "SELECT '/* note\nDELIMITER ?\n*/'", "SELECT 'first\n\\C utf8mb4\nlast'",
                        "SELECT /*+ BKA(t)\n */ 1", "SELECT /*!40101\n1 */"):
                with self.subTest(client=client, sql=sql):
                    plan = self.plan(sql, client=client)
                    self.assertEqual(ed._decision(plan, SAFETY)["action"], "allow")
            # A genuine command after a completed comment is still supported.
            self.assertEqual(self.plan("/* note\nmore */\nDELIMITER //\nSELECT 1//", client=client)
                             ["units"][0]["send"], "SELECT 1")

    def test_executable_closing_slash_overlap_gets_explicit_refusal(self):
        for engine, version in SERVERS:
            for client in ("mysql", "mariadb", None):
                for opener in ("/*!40101", "/*!999999", "/*M!100000", "/*M!999999"):
                    sql = "SELECT " + opener + " 1 */*2"
                    with self.subTest(engine=engine, version=version, client=client, opener=opener):
                        scan = dbops.lex_sql(sql, client=True, engine=engine, server_version=version,
                                            client_engine=client, client_delimiter=";")
                        self.assertIn(dbops.REASON_COMMENT_OVERLAP, [reason for at, reason in scan["issues"]])
                        reason = "client family is unknown" if client is None and opener.startswith("/*M!") else "overlap"
                        with self.assertRaisesRegex(ed.EditorError, reason):
                            self.plan(sql, engine, version, client)
        for client in ("mysql", "mariadb", None):
            self.assertEqual(self.plan("SELECT /*!40101 1 */ *2", client=client)["units"][0]["code"],
                             "SELECT 1 *2")

    def test_overlap_cannot_expose_a_charset_command_in_mssql_mode(self):
        sql = "SELECT /*!40101 1 */* 1 AS [ ' ], ' \\C utf8mb4 ' -- */\n, '; DELETE FROM t; -- '"
        for client in ("mysql", "mariadb", None):
            with self.subTest(client=client), self.assertRaises(ed.EditorError):
                self.plan(sql, "mariadb", "12.0.1", client, "MSSQL")

    def test_crlf_delimiter_argument_is_not_silently_normalized(self):
        # Unix binary-mode clients keep CR in an unquoted argument, while
        # MySQL's Windows reader removes it. Refuse the nonportable delimiter.
        for suffix in ("//\r\n", "//\r", "'//\r'\n"):
            value, end = dbops._delimiter_argument("DELIMITER " + suffix, 9)
            self.assertIsNone(value)
        for engine, version in SERVERS:
            for client in ("mysql", "mariadb", None):
                for tail in ("SELECT 1// -- note\r\n", "SELECT 1//\r\nSELECT 2//",
                             "CREATE PROCEDURE p() BEGIN SELECT 1; END//"):
                    with self.subTest(engine=engine, version=version, client=client, tail=tail):
                        with self.assertRaisesRegex(ed.EditorError, "carriage return"):
                            self.plan("DELIMITER //\r\n" + tail, engine, version, client)

    @mock.patch.object(dbops.sys, "platform", "linux")
    def test_crlf_with_a_quoted_or_space_terminated_delimiter_preserves_offsets(self):
        for engine, version in SERVERS:
            for client in ("mysql", "mariadb", None):
                for argument in ("'//'", '"//"', "`//`", "// "):
                    sql = "DELIMITER " + argument + "\r\nSELECT 1//\r\n/* note\r\nmore */ SELECT 2//"
                    with self.subTest(engine=engine, version=version, client=client, argument=argument):
                        units = self.plan(sql, engine, version, client)["units"]
                        self.assertEqual([unit["send"] for unit in units],
                                         ["SELECT 1", "/* note\r\nmore */ SELECT 2"])
                        for unit in units:
                            self.assertEqual(sql[unit["start"]:unit["end"]], unit["send"])
                        routine = "DELIMITER " + argument + "\r\nCREATE PROCEDURE p() BEGIN\r\nSELECT 1;\r\nEND//"
                        plan = self.plan(routine, engine, version, client)
                        self.assertEqual(ed._decision(plan, SAFETY)["action"], "confirm")

    @mock.patch.object(dbops.sys, "platform", "win32")
    def test_windows_mysql_refuses_crlf_bytes_inside_sent_sql(self):
        for sql in ("SELECT 'first\r\nlast'", "SELECT 1 /* first\r\nlast */", "SELECT\r\n1"):
            for client in ("mysql", None):
                with self.subTest(sql=sql, client=client), self.assertRaisesRegex(ed.EditorError, "Windows"):
                    self.plan(sql, client=client)
            self.assertEqual(self.plan(sql, client="mariadb")["units"][0]["send"], sql)
        # Inter-statement line endings and consumed command lines never
        # enter a reviewed unit, so no client-dependent bytes remain.
        sql = "DELIMITER '//'\r\nSELECT 1//\r\nSELECT 2//\r\n"
        self.assertEqual([u["send"] for u in self.plan(sql)["units"]], ["SELECT 1", "SELECT 2"])
        for delimiter in ("//\r", "//\n", "//\r\n"):
            with self.assertRaisesRegex(ValueError, "line endings"):
                dbops.split_statements("SELECT 1", delimiter=delimiter)

    def test_set_statement_oracle_assignment_invalidates_following_mode(self):
        for version in ("10.11.16", "11.8.9", "12.0.1"):
            for mode in ("ORACLE", None):
                for target in ("sql_mode", "`sql_mode`", "session . `sql_mode`"):
                    sql = "SET STATEMENT max_statement_time = 10 FOR " + target + " := 'x'"
                    with self.subTest(version=version, mode=mode, target=target):
                        plan = self.plan(sql + "; SELECT 1", "mariadb", version, "mariadb", mode)
                        units = plan["units"]
                        self.assertTrue(ed._unit_changes_mode(units[0], "mariadb"))
                        self.assertIsNone(units[1]["sql_mode"])
                        self.assertTrue(units[1]["mode_unknown"])
                        self.assertFalse(ed._embedded_compound_ddl(units[0]))
                        decision = ed._decision(plan, SAFETY)
                        self.assertEqual(decision["action"], "confirm", decision)
                        with self.assertRaises(ed.EditorError):
                            self.plan(sql + r"; SELECT 'it\'s'", "mariadb", version, "mariadb", mode)

    def test_wrapped_assignment_inside_one_unit_invalidates_mode(self):
        sql = ("DELIMITER //\nSELECT 0; SET STATEMENT max_statement_time = 10 FOR "
               "sql_mode := 'x'//\nSELECT 1//")
        units = self.plan(sql, "mariadb", "11.8.9", "mariadb", "ORACLE")["units"]
        self.assertTrue(ed._unit_changes_mode(units[0], "mariadb"))
        self.assertIsNone(units[1]["sql_mode"])

    def test_assignment_operator_is_atomic_without_weakening_real_label_checks(self):
        self.assertEqual([m.group() for m in ed._MASKED_TOKEN.finditer("sql_mode := ''")],
                         ["sql_mode", ":=", "''"])
        for body in ("SELECT @x := 1", "SELECT @@sql_mode", "SET @sql_mode := ''"):
            unit = {"code": "SET STATEMENT max_statement_time = 10 FOR " + body, "sql_mode": "ORACLE"}
            self.assertFalse(ed._unit_changes_mode(unit, "mariadb"), body)
        for sql in ("label: BEGIN NOT ATOMIC SELECT 1; END label",
                    "SET STATEMENT max_statement_time = 10 FOR sql_mode := ''"):
            self.assertFalse(ed._embedded_compound_ddl({"code": sql, "idents": []}), sql)
        self.assertTrue(ed._embedded_compound_ddl({"code": "label: BEGIN NOT ATOMIC DROP TABLE t; END label",
                                                 "idents": []}))


if __name__ == "__main__":
    unittest.main()
