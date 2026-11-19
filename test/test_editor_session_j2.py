"""J2 decisions: compound DDL, unbound facts, dump modes and operands.

The planner, classifier and policy are real. The process cases use the
existing disposable client fixture, which supplies server answers only.
"""
import os
import types
import unittest

import test_editor_session as fixtures
import test_editor_session_identity as identity_fixtures
from myflames import editor_session as ed


SAFETY = {"type": "production", "read_only": False}
SERVERS = (("mysql", "8.4.11"), ("mariadb", "11.8.9-MariaDB"))
JG3 = ("DELIMITER //\nDROP PREPARE s; BEGIN NOT ATOMIC "
       "IF 1 THEN DROP DATABASE prod1; END IF; "
       "IF 1 THEN DROP DATABASE prod2; END IF; END//")


def bound_session(engine="mysql", version="8.4.11", mode=""):
    identity = {"connection_id": 1, "version": version, "hostname": "j2-db",
                "port": "3306", "server_id": "7"}
    if engine == "mysql":
        identity["server_uuid"] = "11111111-2222-3333-4444-555555555555"
    return types.SimpleNamespace(
        engine=engine, server_version=version, client_engine=engine, sql_mode=mode,
        backslash_escapes=mode is None or "NO_BACKSLASH_ESCAPES" not in mode.upper(),
        in_transaction=False, connection_id=1, server_identity=identity,
        _kill_identity=dict(identity, wsrep_on="OFF"), _routed_startup=False)


class EditorDecisionJ2Tests(unittest.TestCase):
    def plan(self, sql, engine="mysql", version="8.4.11", mode=""):
        return ed._plan_run({"sql": sql}, bound_session(engine, version, mode))

    def decision(self, sql, engine="mariadb", version="11.8.9-MariaDB"):
        session = bound_session(engine, version)
        return ed._decision(ed._plan_run({"sql": sql}, session), SAFETY, session)

    def test_jg3_multiple_database_targets_refuse(self):
        decision = self.decision(JG3)
        self.assertEqual(decision["action"], "refuse", decision)
        self.assertIn("more than one database", decision["text"])
        self.assertNotIn("typed_target", decision)

    def test_ddl_prefix_does_not_exempt_later_compound_ddl(self):
        # DROP PREPARE is deallocation, not object DDL: MySQL
        # sql_yacc.yy:2627-2645; MariaDB sql_yacc.yy:2230-2240.
        prefixes = ("DROP PREPARE s", "CREATE TABLE spare (n INT)",
                    "ALTER TABLE spare ADD COLUMN n INT", "DROP TABLE spare",
                    "RENAME TABLE spare TO spare2", "TRUNCATE TABLE spare")
        changes = ("DROP DATABASE prod", "DROP SCHEMA `my``db`",
                   "ALTER TABLE t WAIT 100 ADD COLUMN c INT",
                   "CREATE VIEW v AS SELECT 1", "RENAME USER a TO b", "TRUNCATE t")
        for prefix in prefixes:
            for change in changes:
                sql = "DELIMITER //\n%s; BEGIN NOT ATOMIC IF 1 THEN %s; END IF; END//" % (prefix, change)
                with self.subTest(prefix=prefix, change=change):
                    decision = self.decision(sql)
                    self.assertEqual(decision["action"], "refuse", decision)
                    self.assertIn("standalone", decision["text"])
                    if change.startswith("DROP"):
                        self.assertEqual(decision["typed_target"], "prod" if "DATABASE" in change else "my`db")

    def test_prefix_before_oracle_block_cannot_bypass_mode_refusal(self):
        for prefix in ("DROP PREPARE s; ", "CREATE TABLE t(n INT); ", "ALTER TABLE t ADD n INT; "):
            for block in ("BEGIN NULL; END", "DECLARE n INT; BEGIN n := 1; END",
                          "IF 1 THEN NULL; END IF"):
                with self.subTest(prefix=prefix, block=block):
                    with self.assertRaisesRegex(ed.EditorError, "ORACLE"):
                        self.plan("DELIMITER //\n" + prefix + block + "//", "mariadb", "11.8.9-MariaDB", "ORACLE")

    def test_ordinary_ddl_and_deallocate_compounds_keep_their_policy(self):
        for sql in ("CREATE TABLE t(n INT)", "ALTER TABLE t ADD n INT", "DROP TABLE t",
                    "RENAME TABLE t TO t2", "TRUNCATE t",
                    "DROP PREPARE s; BEGIN NOT ATOMIC IF 1 THEN SELECT 1; END IF; END"):
            with self.subTest(sql=sql):
                self.assertEqual(self.decision("DELIMITER //\n" + sql + "//")["action"], "confirm")
        sql = "DELIMITER //\nCREATE PROCEDURE p() BEGIN DROP DATABASE prod; END//"
        decision = self.decision(sql)
        self.assertEqual((decision["action"], decision["typed_target"]), ("confirm", "prod"))
        self.assertEqual(self.decision(sql[:-2] + "; DROP DATABASE second//")["action"], "refuse")

    def test_database_target_scan_covers_all_units_without_matching_quoted_text(self):
        for sql in ("DROP DATABASE prod", "DROP SCHEMA IF EXISTS `prod`",
                    "DROP PREPARE s; DROP DATABASE prod",
                    "DROP PREPARE s; IF 1 THEN DROP SCHEMA prod; END IF"):
            with self.subTest(sql=sql):
                self.assertEqual(self.decision("DELIMITER //\n" + sql + "//")["typed_target"], "prod")
        sql = ("DELIMITER //\nDROP PREPARE s; BEGIN NOT ATOMIC "
               "SELECT 'DROP DATABASE secret', @drop, t.drop, col$drop, `DROP SCHEMA other` FROM t; "
               "/* DROP DATABASE hidden */ END//")
        decision = self.decision(sql)
        self.assertEqual(decision["action"], "confirm", decision)
        self.assertNotIn("typed_target", decision)
        for target in ("@prod", "d.prod", '"prod"'):
            with self.subTest(target=target):
                self.assertEqual(self.decision("DELIMITER //\nDROP PREPARE s; "
                                               "IF 1 THEN DROP DATABASE %s; END IF//" % target)["action"], "refuse")

    def test_unbound_display_facts_cannot_narrow_any_server_context(self):
        for engine, version in SERVERS:
            for failure in ("routed", "missing_identity", "missing_kill", "version_drift", "session_drift"):
                session = bound_session(engine, version, "MSSQL,NO_BACKSLASH_ESCAPES")
                if failure == "routed":
                    session._routed_startup = True
                elif failure == "missing_identity":
                    session.server_identity = {}
                elif failure == "missing_kill":
                    session._kill_identity = None
                elif failure == "version_drift":
                    session.server_version = "5.7.44"
                else:
                    session.connection_id = 2
                with self.subTest(engine=engine, failure=failure):
                    context = ed._lexer_session_context(session)
                    self.assertIs(context["identity_bound"], False)
                    for field in ("engine", "server_version", "sql_mode", "wsrep_on"):
                        self.assertIsNone(context[field], field)
                    self.assertEqual(context["client_engine"], engine)
                    plan = ed._plan_run({"sql": "SELECT 7; SELECT 'it''s'"}, session)
                    self.assertEqual(plan["verdict"]["kind"], "read")
                    self.assertTrue(all(unit["mode_unknown"] for unit in plan["units"]))
                    self.assertTrue(all(unit["sql_mode"] is None for unit in plan["units"]))

    def test_unbound_quoting_refuses_before_all_cursor_and_selection_plans(self):
        attacks = ("SELECT 1 AS [a'] ; DROP DATABASE prod; -- '",
                   r"SELECT 'it\'s'", r"SELECT 'tail\'")
        for mode in ("", "MSSQL", "NO_BACKSLASH_ESCAPES"):
            session = bound_session(mode=mode)
            session._routed_startup = True
            for sql in attacks:
                for scope in ({}, {"cursor": 0}, {"selection": {"start": 0, "end": len(sql)}}):
                    with self.subTest(mode=mode, sql=sql, scope=scope):
                        with self.assertRaises(ed.EditorError):
                            ed._plan_run(dict(scope, sql=sql), session)
        with self.assertRaisesRegex(ed.EditorError, "ORACLE"):
            ed._plan_run({"sql": "DELIMITER //\nDROP PREPARE s; BEGIN NULL; END//"}, session)

    def test_bound_facts_still_allow_their_known_quote_mode(self):
        for engine, version in SERVERS:
            session = bound_session(engine, version, "NO_BACKSLASH_ESCAPES")
            self.assertEqual(ed._lexer_session_context(session), {
                "engine": engine, "server_version": version, "sql_mode": "NO_BACKSLASH_ESCAPES",
                "identity_bound": True, "wsrep_on": False, "client_engine": engine})
            plan = ed._plan_run({"sql": r"SELECT 'tail\'"}, session)
            self.assertFalse(plan["units"][0]["mode_unknown"])
            self.assertTrue(plan["units"][0]["mode_sensitive"])

    def test_comments_and_executable_markers_before_standalone_ddl_are_not_wrappers(self):
        for engine, version in SERVERS:
            for sql in ("/* note */ ALTER TABLE t ADD n INT", "/*!40101 ALTER TABLE t ADD n INT */",
                        "/*!999999 CREATE */ ALTER TABLE t ADD n INT"):
                with self.subTest(engine=engine, sql=sql):
                    plan = self.plan(sql, engine, version)
                    self.assertFalse(ed._embedded_compound_ddl(ed._alter_unit(plan)))
                    self.assertEqual(ed._decision(plan, SAFETY)["action"], "confirm")
                    self.assertEqual(plan["units"][0]["send"], ed.ALTER_LOCK_WAIT)

    def test_unbound_explain_format_uses_display_facts_without_trusting_sql_modes(self):
        for engine, version in SERVERS:
            session = bound_session(engine, version, "MSSQL")
            session._routed_startup = True
            for fmt, prefix in (("table", "EXPLAIN " if engine == "mariadb" else "EXPLAIN FORMAT=TRADITIONAL "),
                                ("json", "EXPLAIN FORMAT=JSON ")):
                with self.subTest(engine=engine, fmt=fmt):
                    plan = ed._plan_run({"sql": "SELECT 1", "explain_format": fmt}, session)
                    self.assertEqual(plan["units"][0]["send"], prefix + "SELECT 1")
                    self.assertIsNone(plan["units"][0]["sql_mode"])
                    self.assertTrue(plan["units"][0]["mode_unknown"])
                    with self.assertRaises(ed.EditorError):
                        ed._plan_run({"sql": "SELECT 1 AS [a'] ; DROP DATABASE prod; -- '",
                                      "explain_format": fmt}, session)
            if engine == "mysql":
                plan = ed._plan_run({"sql": "SELECT 1", "explain_format": "tree"}, session)
                self.assertEqual(plan["units"][0]["send"], "EXPLAIN FORMAT=TREE SELECT 1")
                session.server_version = "5.7.44"
            with self.assertRaisesRegex(ed.EditorError, "Tree format needs MySQL"):
                ed._plan_run({"sql": "SELECT 1", "explain_format": "tree"}, session)

    def test_explained_ddl_is_not_executed_but_later_compounds_still_refuse(self):
        for engine, version in SERVERS:
            session = bound_session(engine, version)
            with self.subTest(engine=engine):
                plan = ed._plan_run({"sql": "CREATE TABLE t (n INT)", "explain_format": "table"}, session)
                self.assertEqual(ed._decision(plan, SAFETY, session)["action"], "allow")
                for prefix in ("EXPLAIN CREATE TABLE t (n INT)", "EXPLAIN FORMAT=JSON DROP DATABASE prod"):
                    plan = self.plan("DELIMITER //\n" + prefix +
                                     "; BEGIN NOT ATOMIC DROP TABLE t; END//", engine, version)
                    self.assertEqual(ed._decision(plan, SAFETY, session)["action"], "refuse")
                plan = self.plan("EXPLAIN ANALYZE CREATE TABLE t (n INT)", engine, version)
                self.assertEqual(ed._decision(plan, SAFETY, session)["action"], "refuse")

    def test_reading_sql_mode_never_invalidates_the_mode(self):
        reads = ("SELECT @@sql_mode", "SELECT @@ SESSION . `sql_mode`", "SELECT sql_mode FROM t",
                 "SET @saved_sql_mode = @@sql_mode", "SET @sql_mode = @@SESSION.sql_mode",
                 "SET @first = @@sql_mode, @second = @@local.sql_mode")
        for engine, version in SERVERS:
            for read in reads:
                with self.subTest(engine=engine, read=read):
                    plan = self.plan(read + r"; SELECT 'it\'s'", engine, version)
                    self.assertFalse(ed._unit_changes_mode(plan["units"][0]))
                    self.assertEqual(plan["units"][1]["sql_mode"], "")
                    self.assertFalse(plan["units"][1]["mode_unknown"])

    def test_only_literal_session_assignment_with_optional_dump_save_is_predicted(self):
        assignments = (
            "SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO'",
            "SET @saved=@@SESSION.sql_mode, SESSION sql_mode='NO_AUTO_VALUE_ON_ZERO'",
            "SET @`saved mode` = @ @ LOCAL . `sql_mode`, @@LOCAL.`sql_mode`='NO_AUTO_VALUE_ON_ZERO'",
            "SET LOCAL `sql_mode`='NO_AUTO_VALUE_ON_ZERO'",
        )
        for engine, version in SERVERS:
            for assignment in assignments:
                with self.subTest(engine=engine, assignment=assignment):
                    unit = self.plan(assignment, engine, version)["units"][0]
                    self.assertTrue(ed._unit_changes_mode(unit))
                    self.assertEqual(ed._literal_sql_mode(unit), "NO_AUTO_VALUE_ON_ZERO")
                    plan = self.plan(assignment + r"; SELECT 'it\'s'", engine, version)
                    self.assertEqual(plan["units"][1]["sql_mode"], "NO_AUTO_VALUE_ON_ZERO")
        for assignment in (
                "SET sql_mode=@OLD_SQL_MODE", "SET sql_mode=CONCAT(@@sql_mode, ',ANSI_QUOTES')",
                "SET @x=1, sql_mode='NO_AUTO_VALUE_ON_ZERO'",
                "SET @old=@@GLOBAL.sql_mode, sql_mode='NO_AUTO_VALUE_ON_ZERO'",
                "SET @old=@@sql_mode, GLOBAL sql_mode='NO_AUTO_VALUE_ON_ZERO'",
                "SET sql_mode='', sql_mode=@OLD_SQL_MODE",
                "SET `@saved=@@sql_mode, sql_mode`='NO_AUTO_VALUE_ON_ZERO'",
                "SET @`saved=@@sql_mode, sql_mode`='NO_AUTO_VALUE_ON_ZERO'"):
            with self.subTest(assignment=assignment):
                self.assertIsNone(ed._literal_sql_mode(self.plan(assignment)["units"][0]))

    def test_nonliteral_quoted_and_spaced_assignments_still_invalidate_mode(self):
        for assignment in ("SET sql_mode=@saved", "SET @@SESSION.`sql_mode`=@saved",
                           "SET @ @ session . `sql_mode`=@saved", 'SET "sql_mode"=@saved',
                           'SET @@ session . "sql_mode"=@saved', "SET @save=@@sql_mode, sql_mode=@saved"):
            for engine, version in SERVERS:
                with self.subTest(engine=engine, assignment=assignment):
                    unit = self.plan(assignment, engine, version)["units"][0]
                    self.assertTrue(ed._unit_changes_mode(unit))
                    self.assertIsNone(ed._literal_sql_mode(unit))
                    with self.assertRaises(ed.EditorError):
                        self.plan(assignment + r"; SELECT 'it\'s'", engine, version)

    def test_mysql_and_mariadb_dump_header_tables_routine_and_trailer_plan(self):
        # Emitted forms: mysql-server/client/mysqldump.cc:995,1588-1597;
        # mariadb-server/client/mysqldump.cc:835,1812-1825. The whole dump
        # plans, but its multiple DDL statements still need separate reviews.
        dump = r"""-- SQL dump, table structure and stored routine
/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!50503 SET NAMES utf8mb4 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
DROP TABLE IF EXISTS `notes`;
CREATE TABLE `notes` (`id` int NOT NULL, `note` varchar(100), PRIMARY KEY (`id`)) ENGINE=InnoDB;
LOCK TABLES `notes` WRITE;
INSERT INTO `notes` VALUES (1,'it\'s'),(2,"x");
UNLOCK TABLES;
DELIMITER ;;
/*!50003 SET @saved_cs_client = @@character_set_client */;;
/*!50003 SET @saved_sql_mode = @@sql_mode */;;
/*!50003 SET sql_mode = 'STRICT_TRANS_TABLES' */;;
/*!50003 CREATE*/ /*!50020 DEFINER=`root`@`localhost`*/ /*!50003 PROCEDURE `p`()
BEGIN
  SELECT 'it\'s', "x";
END */;;
/*!50003 SET sql_mode = @saved_sql_mode */;;
/*!50003 SET character_set_client = @saved_cs_client */;;
DELIMITER ;
/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
"""
        for engine, version in SERVERS:
            with self.subTest(engine=engine):
                plan = self.plan(dump, engine, version)
                self.assertEqual(len(plan["units"]), 18)
                self.assertEqual(plan["units"][7]["send"], r"""INSERT INTO `notes` VALUES (1,'it\'s'),(2,"x")""")
                self.assertEqual(plan["units"][7]["sql_mode"], "NO_AUTO_VALUE_ON_ZERO")
                self.assertFalse(ed._unit_changes_mode(plan["units"][10]))
                routine = plan["units"][12]
                self.assertEqual(routine["sql_mode"], "STRICT_TRANS_TABLES")
                self.assertEqual(len(ed._ddl_codes(routine)), 1)
                self.assertIn(r"""SELECT 'it\'s', "x";""", routine["send"])
                self.assertEqual(ed._decision(plan, SAFETY)["action"], "refuse")
                # The routine's own preamble/restore remains predictable,
                # but earlier DDL and DML invalidate @OLD_SQL_MODE: a
                # trigger could have assigned that user variable.
                self.assertTrue(plan["units"][-1]["mode_unknown"])
                self.assertIsNone(plan["units"][-1]["sql_mode"])

    def test_end_and_do_operands_keep_each_definition_and_expose_trailing_sql(self):
        # MySQL sql_yacc.yy:3657-3727,10592-10600,10738-10744;
        # MariaDB:3213-3268,10198-10216,10624-10628.
        definitions = (
            "CREATE PROCEDURE p() BEGIN DECLARE do INT DEFAULT 1; WHILE a < b + INTERVAL do DAY DO SELECT 1; END WHILE; END",
            "CREATE PROCEDURE p() BEGIN WHILE BINARY do DO SELECT 1; END WHILE; END",
            "CREATE PROCEDURE p() BEGIN REPEAT SELECT 1; UNTIL a < b + INTERVAL end DAY END REPEAT; END",
            "CREATE PROCEDURE p() BEGIN REPEAT SELECT 1; UNTIL BINARY end END REPEAT; END",
            "CREATE EVENT e ON SCHEDULE AT do DO SELECT 1",
            "CREATE EVENT e ON SCHEDULE EVERY do HOUR DO SELECT 1",
            "CREATE EVENT e ON SCHEDULE EVERY 1 HOUR STARTS do DO SELECT 1",
            "CREATE EVENT e ON SCHEDULE EVERY 1 HOUR ENDS do DO SELECT 1",
            "CREATE EVENT e ON SCHEDULE EVERY 1 HOUR STARTS do ENDS do DO SELECT 1",
            "CREATE EVENT e ON SCHEDULE AT CASE WHEN 1 THEN BINARY end ELSE end END DO SELECT 1",
        )
        for engine, version in SERVERS:
            for definition in definitions:
                with self.subTest(engine=engine, definition=definition):
                    plan = self.plan("DELIMITER //\n" + definition + "//", engine, version)
                    self.assertEqual(len(ed._ddl_codes(plan["units"][0])), 1)
                    self.assertEqual(ed._decision(plan, SAFETY)["action"], "confirm")
                    for tail in ("DROP DATABASE prod", "SELECT 2"):
                        plan = self.plan("DELIMITER //\n" + definition + "; " + tail + "//", engine, version)
                        self.assertEqual(len(ed._ddl_codes(plan["units"][0])), 2)
                        self.assertEqual(ed._decision(plan, SAFETY)["action"], "refuse")


@unittest.skipIf(os.name == "nt", "the fake client is a POSIX script")
class EditorDecisionJ2ProcessTests(unittest.TestCase):
    engine = "mysql"
    setUp = fixtures.ProcessTests.setUp
    call = fixtures.ProcessTests.call
    session_log = fixtures.ProcessTests.session_log
    packets = identity_fixtures.EditorIdentityTests.packets

    def test_compound_ddl_refuses_without_any_packet_or_confirmation(self):
        self.call("/api/editor/open")
        before = len(self.packets())
        log = self.session_log()
        for sql in (JG3, "DELIMITER //\nDROP PREPARE s; BEGIN NOT ATOMIC IF 1 THEN "
                    "ALTER TABLE t WAIT 100 ADD COLUMN c INT; END IF; END//"):
            with self.subTest(sql=sql):
                result = self.call("/api/editor/run", sql=sql)
                self.assertEqual(result["status"], "refused", result)
                self.assertNotIn("confirmation", result)
                self.assertEqual(len(self.packets()), before)
                self.assertEqual(self.session_log(), log)
                self.assertFalse(self.registry.tokens)

    def test_unbound_display_mode_never_sends_ambiguous_user_sql(self):
        self.call("/api/editor/open")
        session = self.registry.get("tab1")
        session._routed_startup = True
        session.engine = "mysql"
        session.sql_mode = "MSSQL"
        before = len(self.packets())
        log = self.session_log()
        result = self.call("/api/editor/run", sql="SELECT 1 AS [a'] ; DROP DATABASE prod; -- '")
        self.assertEqual((result["status"], result.get("code")), ("invalid", "invalid_statement"), result)
        self.assertEqual(len(self.packets()), before)
        self.assertEqual(self.session_log(), log)
        self.assertFalse(self.registry.tokens)


class MariaDBEditorDecisionJ2ProcessTests(EditorDecisionJ2ProcessTests):
    engine = "mariadb"


if __name__ == "__main__":
    unittest.main()
