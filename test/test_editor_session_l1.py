"""L1 regressions use the real planner, classifier, and policy; no server."""
import unittest

from myflames import dbops, editor_session as ed
from test_editor_session_j2 import SAFETY, bound_session


class EditorPlanningL1Tests(unittest.TestCase):
    def plan(self, sql, engine="mysql", mode="", client="mysql"):
        session = bound_session(engine, "8.4.0" if engine == "mysql" else "11.8.9-MariaDB", mode)
        session.client_engine = client
        return ed._plan_run({"sql": sql}, session)

    def test_overlapping_closer_refuses_every_comment_kind_and_client(self):
        tail = " , ' /* z */ \\C utf8mb4 '\n, '; DELETE FROM t; -- '"
        for engine in ("mysql", "mariadb"):
            for client in ("mysql", "mariadb", None):
                for opener in ("/*", "/*+", "/*+ a\n", "/*!", "/*!40101", "/*!999999",
                               "/*M!", "/*M!100000", "/*M!999999"):
                    sql = "SELECT 1 " + opener + " a /*/" + tail
                    with self.subTest(engine=engine, client=client, opener=opener):
                        with self.assertRaises(ed.EditorError):
                            self.plan(sql, engine, client=client)
                        verdict = dbops.classify(sql, engine=engine,
                                                server_version="8.4.0" if engine == "mysql" else "11.8.9",
                                                sql_mode="", client_engine=client)
                        self.assertEqual(verdict["kind"], "risky")

    def test_plain_closers_and_overlap_inside_quoted_data_remain_reads(self):
        for engine in ("mysql", "mariadb"):
            for client in ("mysql", "mariadb", None):
                for sql in ("SELECT 1 /* a */ , ' /* z */ \\C utf8mb4 '\n, '; DELETE FROM t; -- '",
                            "SELECT 1 /* outer /* ordinary */", "SELECT 1 /* / */",
                            "SELECT '/*/', `/*/`", "SELECT 1 # /*/\n", "SELECT 1 -- /*/\n",
                            "SELECT /*+ BKA(t) */ 1", "SELECT /*!40101 1 */"):
                    with self.subTest(engine=engine, client=client, sql=sql):
                        plan = self.plan(sql, engine, client=client)
                        self.assertEqual(ed._decision(plan, SAFETY)["action"], "allow")

    def test_closing_slash_cannot_reopen_a_hint_or_change_sent_spacing(self):
        for engine in ("mysql", "mariadb"):
            for client in ("mysql", "mariadb"):
                for sql in ("SELECT /*+ */*/ 1 , ' /* z */ \\C utf8mb4 '\n, '; DELETE FROM t; -- '",
                            "SELECT 1 /* note */+2", "SELECT 1 /* note */'x'"):
                    with self.subTest(engine=engine, client=client, sql=sql):
                        with self.assertRaises(ed.EditorError):
                            self.plan(sql, engine, client=client)
                for sql in ("SELECT 1 /* note */ +2", "SELECT 1 /* note */;", "SELECT 1 /* note */\n+2"):
                    self.assertEqual(ed._decision(self.plan(sql, engine, client=client), SAFETY)["action"], "allow")

    def test_unknown_client_cannot_discard_another_readings_delimiter_prefix(self):
        for engine, prefix in (("mysql", "/*M!100000 SELECT 2 */"),
                               ("mariadb", "/*M!999999 SELECT 2 */")):
            for before in ("", "SELECT 0;\n", "SET sql_mode='';\n"):
                sql = before + prefix + "\nDELIMITER //\nSELECT 1//"
                with self.subTest(engine=engine, before=before):
                    with self.assertRaisesRegex(ed.EditorError, "DELIMITER"):
                        self.plan(sql, engine, client=None)
                    with self.assertRaises(ed.EditorError):
                        self.plan(sql, engine, client="mariadb")
                    # A known MySQL client and a server that ignores this
                    # prefix agree: the standalone command stays supported.
                    plan = self.plan(sql, engine, client="mysql")
                    self.assertEqual(plan["units"][-1]["send"], "SELECT 1")
            # A second consumed line must also be checked, not just the first.
            with self.assertRaisesRegex(ed.EditorError, "DELIMITER"):
                self.plan("DELIMITER //\nSELECT 0//\n" + prefix + "\nDELIMITER ;;\nSELECT 1;;",
                          engine, client=None)
            for before in ("", "SELECT 0;\n", "SET sql_mode='';\n", "SET sql_mode=@saved;\n"):
                with self.subTest(engine=engine, trailing_command=True, before=before):
                    with self.assertRaisesRegex(ed.EditorError, "DELIMITER"):
                        self.plan(before + prefix + "\nDELIMITER //", engine, client=None)

    def test_unknown_client_agrees_on_ordinary_comment_prefixes(self):
        for engine in ("mysql", "mariadb"):
            for prefix in ("", "/* note */\n", "-- note\n# note\n", "/* note\nmore */\n"):
                plan = self.plan(prefix + "DELIMITER //\nSELECT 1//", engine, client=None)
                self.assertEqual([unit["send"] for unit in plan["units"]], ["SELECT 1"])

    def test_stored_program_refusal_names_missing_binding(self):
        sql = "DELIMITER //\nCREATE PROCEDURE p() BEGIN SELECT 1; END//"
        for engine in ("mysql", "mariadb"):
            session = bound_session(engine, "8.4.0" if engine == "mysql" else "11.8.9-MariaDB", "")
            session._routed_startup = True
            with self.subTest(engine=engine), self.assertRaisesRegex(ed.EditorError, "not bound to this session"):
                ed._plan_run({"sql": sql}, session)
        with self.assertRaisesRegex(ed.EditorError, "known mode without ORACLE"):
            self.plan(sql, "mariadb", "ORACLE", "mariadb")
        self.assertEqual(len(ed._plan_run({"sql": sql}, None)["units"]), 1)

    def test_oracle_assignment_invalidates_later_mode(self):
        # mariadb-11.8.9:sql/sql_yacc.yy:19225-19230,19513-19547;
        # sql/sql_lex.cc:9042-9050 falls back to set_system_variable.
        for mode in ("ORACLE", None):
            for target in ("sql_mode", "`sql_mode`", "session.sql_mode", "session . `sql_mode`"):
                sql = target + " := 'NO_BACKSLASH_ESCAPES'"
                with self.subTest(mode=mode, target=target):
                    plan = self.plan(sql + "; SELECT 1", "mariadb", mode, "mariadb")
                    self.assertTrue(ed._unit_changes_mode(plan["units"][0]))
                    self.assertIsNone(plan["units"][1]["sql_mode"])
                    self.assertTrue(plan["units"][1]["mode_unknown"])
                    with self.assertRaises(ed.EditorError):
                        self.plan(sql + r"; SELECT 'it\'s'", "mariadb", mode, "mariadb")

    def test_oracle_assignment_in_custom_delimiter_group_checks_later_quoting(self):
        sql = "DELIMITER //\nSELECT 1; sql_mode := 'NO_BACKSLASH_ESCAPES'; SELECT 'it\\'s'//"
        with self.assertRaisesRegex(ed.EditorError, "may change sql_mode"):
            self.plan(sql, "mariadb", "ORACLE", "mariadb")

    def test_assignment_like_reads_and_non_oracle_sql_preserve_mode(self):
        for engine, mode in (("mysql", ""), ("mysql", "ORACLE"), ("mariadb", "")):
            plan = self.plan("sql_mode := @saved; SELECT 1", engine, mode, engine)
            self.assertEqual(plan["units"][1]["sql_mode"], mode)
        for sql in ("SELECT sql_mode = ''", "SELECT @sql_mode := ''", "SET @sql_mode := ''",
                    "SELECT 'sql_mode := x'", "SELECT `sql_mode := x`", "SET @saved = @@sql_mode"):
            with self.subTest(sql=sql):
                plan = self.plan(sql + "; SELECT 1", "mariadb", "ORACLE", "mariadb")
                self.assertFalse(ed._unit_changes_mode(plan["units"][0]))
                self.assertEqual(plan["units"][1]["sql_mode"], "ORACLE")


if __name__ == "__main__":
    unittest.main()
