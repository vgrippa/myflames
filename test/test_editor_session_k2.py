"""K2 planner regressions using the real splitter, classifier and write policy.

No client process or database is opened. Bound facts use the existing J2
fixture; routed startup explicitly removes that binding for ORACLE checks.
"""
import unittest

from myflames import dbops, editor_session as ed
from test_editor_session_j2 import SAFETY, bound_session


SERVERS = tuple(("mysql", version) for version in ("5.7.44", "8.0.46", "8.4.0", "9.7.0")) + (
    ("mariadb", "10.11.16-MariaDB"), ("mariadb", "11.8.9-MariaDB"))


class EditorPlanningK2Tests(unittest.TestCase):
    def plan(self, sql, engine="mysql", version="8.4.11", client=None, mode=""):
        session = bound_session(engine, version, mode)
        session.client_engine = client or engine
        return ed._plan_run({"sql": sql}, session)

    def test_show_create_is_a_read_on_both_engines(self):
        # mysql-9.7.0:sql/sql_yacc.yy:14343-14444;
        # mariadb-11.8.9:sql/sql_yacc.yy:14438-14629.
        for engine, version in SERVERS:
            for object_type in ("TABLE", "VIEW", "PROCEDURE", "FUNCTION", "TRIGGER",
                                "EVENT", "DATABASE", "SCHEMA"):
                sql = "SHOW CREATE %s `object`" % object_type
                with self.subTest(engine=engine, sql=sql):
                    plan = self.plan(sql, engine, version)
                    self.assertEqual(plan["verdict"]["kind"], "read")
                    self.assertEqual([u["send"] for u in plan["units"]], [sql])
                    self.assertFalse(ed._embedded_compound_ddl(plan["units"][0]))
                    for safety in (SAFETY, dict(SAFETY, type="development"), dict(SAFETY, read_only=True)):
                        self.assertEqual(ed._decision(plan, safety)["action"], "allow")

    def test_custom_delimiter_keeps_show_create_and_other_reads(self):
        for engine, version in SERVERS:
            for body in ("SHOW CREATE TABLE t; SELECT 1", "SELECT 1; SHOW CREATE TABLE t",
                         "SHOW CREATE TABLE t; SHOW CREATE VIEW v"):
                with self.subTest(engine=engine, body=body):
                    plan = self.plan("DELIMITER //\n" + body + "//", engine, version)
                    self.assertEqual(plan["verdict"]["kind"], "read")
                    self.assertEqual([u["send"] for u in plan["units"]], [body])
                    self.assertEqual(ed._decision(plan, SAFETY)["action"], "allow")

    def test_read_exemption_does_not_hide_later_compound_ddl(self):
        for engine, version in SERVERS:
            for prefix in ("SHOW CREATE TABLE t", "SELECT 1", "DROP PREPARE s"):
                sql = "DELIMITER //\n%s; BEGIN NOT ATOMIC DROP TABLE t; END//" % prefix
                with self.subTest(engine=engine, prefix=prefix):
                    plan = self.plan(sql, engine, version)
                    self.assertNotEqual(plan["verdict"]["kind"], "read")
                    decision = ed._decision(plan, SAFETY)
                    self.assertEqual(decision["action"], "refuse")
                    self.assertIn("standalone", decision["text"])

    def test_mode_assignment_after_any_set_expression_refuses_ambiguous_packet(self):
        # CAST CHARACTER SET must not replace the enclosing SET's assignment
        # context. Commas in nested expressions are not assignment boundaries.
        prefixes = ("SET @x = 1", "SET @x = CAST('a' AS CHAR CHARACTER SET utf8mb4)",
                    "SET @x = CONVERT('a' USING utf8mb4)",
                    "SET @x = CONCAT(CAST('a' AS CHAR CHARACTER SET utf8mb4), 'b')",
                    "SET @x = (((CAST('a' AS CHAR CHARACTER SET utf8mb4))))",
                    "SET NAMES utf8mb4 COLLATE utf8mb4_bin", "SET CHARACTER SET utf8mb4")
        suffix = r"; SELECT 'a\' ; DROP DATABASE prod; -- '//"
        for engine, version in SERVERS:
            for prefix in prefixes:
                with self.subTest(engine=engine, prefix=prefix):
                    sql = "DELIMITER //\n" + prefix + ", sql_mode = 'NO_BACKSLASH_ESCAPES'" + suffix
                    with self.assertRaisesRegex(ed.EditorError, "may change sql_mode"):
                        self.plan(sql, engine, version)

    def test_mode_assignment_after_expression_invalidates_later_units(self):
        for engine, version in SERVERS:
            sql = "SET @x = CAST('a' AS CHAR CHARACTER SET utf8mb4), sql_mode = @saved"
            with self.subTest(engine=engine):
                plan = self.plan(sql + "; SELECT 1", engine, version)
                self.assertTrue(ed._unit_changes_mode(plan["units"][0]))
                self.assertIsNone(plan["units"][1]["sql_mode"])
                self.assertTrue(plan["units"][1]["mode_unknown"])
                with self.assertRaises(ed.EditorError):
                    self.plan(sql + r"; SELECT 'it\'s'", engine, version)

    def test_all_mode_assignment_targets_and_operators_are_detected(self):
        # mysql-9.7.0:sql/sql_yacc.yy:16633-16731,13861-13864;
        # mariadb-11.8.9:sql/sql_yacc.yy:17065-17277,13694-13697.
        # Persist scopes are MySQL-only; MariaDB rejects them as SQL syntax.
        for engine, version in SERVERS:
            scopes = ("", "SESSION ", "LOCAL ", "GLOBAL ", "@@", "@@SESSION.",
                      "@@LOCAL.", "@@GLOBAL.", "@ @ SESSION . ")
            if engine == "mysql" and version != "5.7.44":
                scopes += ("PERSIST ", "PERSIST_ONLY ", "@@PERSIST.", "@@PERSIST_ONLY.")
            for scope in scopes:
                for name in ("sql_mode", "`sQl_mOdE`", '"sql_mode"'):
                    for operator in ("=", ":="):
                        sql = "SET @x=CAST('a' AS CHAR CHARACTER SET utf8mb4), %s%s %s @saved" % (
                            scope, name, operator)
                        with self.subTest(engine=engine, sql=sql):
                            unit = self.plan(sql, engine, version)["units"][0]
                            self.assertTrue(ed._unit_changes_mode(unit))

    def test_mode_assignments_in_wrappers_and_stored_bodies(self):
        assignment = "SET @x=CAST('a' AS CHAR CHARACTER SET utf8mb4), sql_mode=@saved"
        for engine, version in SERVERS:
            sql = "DELIMITER //\nCREATE PROCEDURE p() BEGIN " + assignment + "; END//"
            with self.subTest(engine=engine):
                self.assertTrue(ed._unit_changes_mode(self.plan(sql, engine, version)["units"][0]))
        for sql in ("SET STATEMENT sql_mode='ANSI_QUOTES' FOR SELECT 1",
                    "SET STATEMENT max_statement_time=1, `sql_mode`='ANSI_QUOTES' FOR SELECT 1",
                    "SET STATEMENT max_statement_time=1 FOR " + assignment):
            with self.subTest(sql=sql):
                self.assertTrue(ed._unit_changes_mode(self.plan(sql, "mariadb", "11.8.9-MariaDB")["units"][0]))

    def test_rhs_mode_reads_and_user_variables_preserve_known_mode(self):
        reads = ("SELECT @@sql_mode", "SELECT @@SESSION.`sql_mode` = ''", "SELECT 1, @@sql_mode = ''",
                 "SELECT sql_mode = '' FROM t", "SET @sql_mode := @@sql_mode",
                 "SET @saved=@@sql_mode, @x=CAST('a' AS CHAR CHARACTER SET utf8mb4)",
                 "SET @saved = IF(1, @@sql_mode = '', 0)",
                 "SET @saved = CASE WHEN @@sql_mode = '' THEN 1 ELSE 0 END",
                 "SET @`sql_mode`=1", "SET @'sql_mode'=1",
                 "SET `sql_mode = x`=1", "SELECT 'SET sql_mode = x'")
        for engine, version in SERVERS:
            for sql in reads:
                with self.subTest(engine=engine, sql=sql):
                    plan = self.plan(sql + r"; SELECT 'it\'s'", engine, version)
                    self.assertFalse(ed._unit_changes_mode(plan["units"][0]))
                    self.assertEqual(plan["units"][1]["sql_mode"], "")
                    self.assertFalse(plan["units"][1]["mode_unknown"])

    def test_active_comment_before_delimiter_never_disappears(self):
        for engine, version in SERVERS:
            for client in ("mysql", "mariadb"):
                for marker in ("/*!40101", "/*M!100000"):
                    if engine == "mysql" and client == "mysql" and marker.startswith("/*M!"):
                        continue  # Both readings ignore this comment; covered below.
                    for body in ("DELETE FROM t", "SELECT 2"):
                        prefix = marker + " " + body + " */"
                        sql = prefix + "\nDELIMITER //\nSELECT 1//"
                        with self.subTest(engine=engine, client=client, prefix=prefix):
                            units = dbops.split_statements(sql, engine=engine, server_version=version,
                                                           client_engine=client, sql_mode="")
                            self.assertTrue(any(prefix in unit["text"] for unit in units), units)
                            verdict = dbops.classify(sql, engine=engine, server_version=version,
                                                     client_engine=client, sql_mode="")
                            self.assertNotEqual(verdict["kind"], "read")
                            with self.assertRaisesRegex(ed.EditorError, "DELIMITER"):
                                self.plan(sql, engine, version, client)

    def test_consumed_delimiter_requires_a_server_ignored_prefix(self):
        for engine, version in SERVERS:
            for client in ("mysql", "mariadb"):
                prefixes = ("", "-- note\n", "# note\n", "/* note */\n/* another */\n")
                if client == "mysql":
                    prefixes += ("/*M!999999 DELETE FROM t */\n",)
                    if engine == "mysql":
                        prefixes += ("/*M!100000 DELETE FROM t */\n",)
                for prefix in prefixes:
                    sql = "SELECT 0;\n" + prefix + "DELIMITER //\nSELECT 1//"
                    with self.subTest(engine=engine, client=client, prefix=prefix):
                        plan = self.plan(sql, engine, version, client)
                        self.assertEqual([u["send"] for u in plan["units"]], ["SELECT 0", "SELECT 1"])
                        self.assertEqual(plan["verdict"]["kind"], "read")

    def test_consumed_delimiter_refusal_keeps_offsets_and_prefix_without_a_suffix(self):
        for suffix in ("", "\nSELECT 1//", "\n/* note */\nDELIMITER $$\nSELECT 1$$"):
            prefix = "/*M!100000 DELETE FROM t */"
            sql = "SELECT 0;\n" + prefix + "\nDELIMITER //" + suffix
            units = dbops.split_statements(sql, engine="mariadb", server_version="11.8.9-MariaDB",
                                           client_engine="mysql", sql_mode="")
            with self.subTest(suffix=suffix):
                self.assertEqual(units[0]["text"], "SELECT 0")
                self.assertGreaterEqual(len(units), 2, "The executable prefix must remain in a refused unit")
                refused = units[1]
                self.assertIn(prefix, refused["text"])
                self.assertIn("SQL before DELIMITER", " ".join(refused["issues"]))
                self.assertEqual(sql[refused["start"]:refused["end"]], refused["text"])
                with self.assertRaisesRegex(ed.EditorError, "DELIMITER"):
                    self.plan(sql, "mariadb", "11.8.9-MariaDB", "mysql")

    def test_provisional_definitions_plan_but_existing_unbound_session_refuses(self):
        definitions = ("CREATE PROCEDURE p() AS BEGIN NULL; END",
                       "CREATE FUNCTION f() RETURN INT AS BEGIN RETURN 1; END",
                       "CREATE TRIGGER tr BEFORE INSERT ON t FOR EACH ROW BEGIN NULL; END",
                       "CREATE EVENT e ON SCHEDULE EVERY 1 DAY DO BEGIN NULL; END",
                       "CREATE PACKAGE p AS PROCEDURE p1(); END p",
                       "CREATE OR REPLACE DEFINER=`root`@`localhost` PROCEDURE p() AS BEGIN NULL; END",
                       "ALTER EVENT e DO BEGIN NULL; END")
        for definition in definitions:
            with self.subTest(before_startup=definition):
                plan = ed._plan_run({"sql": "DELIMITER //\n" + definition + "//"}, None)
                self.assertEqual([unit["send"] for unit in plan["units"]], [definition])
        for engine, version in SERVERS:
            for client in ("mysql", "mariadb"):
                session = bound_session(engine, version, "ORACLE")
                session.client_engine = client
                session._routed_startup = True
                self.assertFalse(ed._lexer_session_context(session)["identity_bound"])
                for definition in definitions:
                    sql = "DELIMITER //\n" + definition + "//"
                    with self.subTest(engine=engine, client=client, definition=definition):
                        with self.assertRaisesRegex(ed.EditorError, "ORACLE"):
                            ed._plan_run({"sql": sql}, session)

    def test_possible_oracle_mode_after_assignment_is_kept_for_unknown_engine(self):
        for engine in (None, "mariadb"):
            for prefix in ("SET sql_mode=@saved;", "SET sql_mode='ORACLE';",
                           "SET sql_mode='ORACLE'; SET sql_mode='';"):
                with self.subTest(engine=engine, prefix=prefix):
                    with self.assertRaisesRegex(ed.EditorError, "ORACLE"):
                        ed.plan_statements(prefix + "\nDELIMITER //\nCREATE PROCEDURE p() BEGIN SELECT 1; END//",
                                           engine=engine, sql_mode="", client_engine="mysql", identity_bound=True)

    def test_bound_nonoracle_definitions_still_plan(self):
        sql = "DELIMITER //\nCREATE PROCEDURE p() BEGIN SELECT 1; END//"
        for engine, version in SERVERS:
            with self.subTest(engine=engine):
                plan = self.plan(sql, engine, version)
                self.assertEqual(ed._decision(plan, SAFETY)["action"], "confirm")


if __name__ == "__main__":
    unittest.main()
