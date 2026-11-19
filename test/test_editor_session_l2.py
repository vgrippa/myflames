"""L2: real dump sections and conservative per-run SQL-mode provenance.

The planner, classification, confirmation policy and pre-send revalidation
are real. The process fixture supplies server answers without a database.
"""
import os
from pathlib import Path
import unittest

import test_editor_session as fixtures
from test_editor_session_j2 import SAFETY, bound_session
from myflames import dbops, editor_session as ed


DUMPS = Path(__file__).resolve().parent / "fixtures" / "dumps"
SERVERS = (("mysql", "5.7.44-log"), ("mysql", "8.4.11"),
           ("mariadb", "10.11.19-MariaDB"), ("mariadb", "11.8.9-MariaDB"))
MODE = "STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION"


def routine_section(name):
    """Copy the actual procedure preamble/body/restores, omitting DROP DDL."""
    text = (DUMPS / name).read_text(encoding="utf-8")
    start = text.index("/*!50003 SET @saved_sql_mode", text.index("-- Dumping routines"))
    end = text.index("/*!50003 SET collation_connection  = @saved_col_connection */ ;", start)
    end += len("/*!50003 SET collation_connection  = @saved_col_connection */ ;")
    section = text[start:end]
    return "\n".join(line for line in section.splitlines() if "DROP PROCEDURE" not in line)


class EditorDumpL2Tests(unittest.TestCase):
    def plan(self, sql, engine="mysql", version="8.4.11", mode=MODE, **payload):
        session = bound_session(engine, version, mode)
        return ed._plan_run(dict(payload, sql=sql), session), session

    def test_real_dumps_plan_but_keep_one_ddl_per_confirmation(self):
        for name, (engine, version) in zip(
                ("mysql57.sql", "mysql84.sql", "mariadb1011-nosb.sql", "mariadb118-nosb.sql"), SERVERS):
            with self.subTest(name=name):
                sql = (DUMPS / name).read_text(encoding="utf-8")
                plan, session = self.plan(sql, engine, version)
                self.assertGreater(len(plan["units"]), 80)
                self.assertTrue(any("PROCEDURE" in u["code"] for u in plan["units"]))
                decision = ed._decision(plan, SAFETY, session)
                self.assertEqual((decision["action"], decision["title"]),
                                 ("refuse", "One DDL per confirmation"))

    def test_mariadb_originals_refuse_with_actionable_sandbox_message(self):
        message = ("the mariadb-dump sandbox line, a mariadb client command; delete that line "
                   "to run the rest here, or use Maintenance > Restore for a whole dump")
        for name, version in (("mariadb1011.sql", "10.11.19-MariaDB"),
                              ("mariadb118.sql", "11.8.9-MariaDB")):
            text = (DUMPS / name).read_text(encoding="utf-8")
            self.assertEqual(text.splitlines()[0].strip(), r"/*M!999999\- enable the sandbox mode */")
            self.assertEqual(text.split("\n", 1)[1],
                             (DUMPS / name.replace(".sql", "-nosb.sql")).read_text(encoding="utf-8"))
            with self.subTest(name=name), self.assertRaisesRegex(ed.EditorError, message):
                self.plan(text, "mariadb", version)
            issues = dbops.lex_sql(text, client=True, client_engine="mariadb")["issues"]
            self.assertIn(message, " ".join(reason for _at, reason in issues))

    def test_real_procedure_sections_confirm_exactly_one_definition(self):
        # mariadb-11.8.9:client/mysqldump.cc:1830-1839;
        # mariadb-10.11.16:client/mysqldump.c:1733-1746.
        for name in ("mariadb1011.sql", "mariadb118.sql"):
            sql = routine_section(name)
            for version in ("10.11.19-MariaDB", "11.8.9-MariaDB"):
                with self.subTest(name=name, version=version):
                    plan, session = self.plan(sql + "\nSELECT 1;", "mariadb", version)
                    definitions = [u for u in plan["units"] if "CREATE" in u["code"]]
                    self.assertEqual(len(definitions), 1)
                    self.assertEqual(len(ed._ddl_codes(definitions[0])), 1)
                    self.assertIn("BEGIN DECLARE n INT DEFAULT 0;", definitions[0]["send"])
                    self.assertEqual(plan["units"][-1]["sql_mode"], MODE)
                    self.assertFalse(plan["units"][-1]["mode_unknown"])
                    self.assertEqual(ed._decision(plan, SAFETY, session)["action"], "confirm")

    def test_save_restore_spellings_preserve_the_saved_mode(self):
        cases = (
            ("SET @v=@@sql_mode", "SET sql_mode=@V"),
            ("SET @`saved mode` = @@ SESSION . `sql_mode`", "SET LOCAL `sql_mode`=@`saved mode`"),
            ("SET @sql_mode := @@LOCAL.sql_mode", "SET @@SESSION . sql_mode := @sql_mode"),
            ("SET @old=@@sql_mode, sql_mode='ANSI_QUOTES'", "SET SQL_MODE=@OLD"),
            ("SET @`saved=@@sql_mode, sql_mode`=@@sql_mode", "SET sql_mode=@`saved=@@sql_mode, sql_mode`"),
        )
        for engine, version in SERVERS:
            for save, restore in cases:
                for wrap in (False, True):
                    if wrap:
                        save, restore = "/*!50003 " + save + " */", "/*!50003 " + restore + " */"
                    sql = save + "; SET sql_mode='NO_BACKSLASH_ESCAPES'; " + restore + r"; SELECT 'it\'s'"
                    with self.subTest(engine=engine, save=save):
                        plan, _ = self.plan(sql, engine, version)
                        self.assertEqual(plan["units"][-1]["sql_mode"], MODE)

    def test_unrelated_assignments_and_reads_do_not_erase_the_save(self):
        sql = ("SET @v=@@sql_mode; SET @other=1; SELECT @v; "
               "SET sql_mode='NO_BACKSLASH_ESCAPES'; SET sql_mode=@v; SELECT 1")
        for engine, version in SERVERS:
            plan, _ = self.plan(sql, engine, version)
            self.assertEqual(plan["units"][-1]["sql_mode"], MODE)

    def test_reassigned_variable_is_unknown_even_if_reassigned_to_a_literal(self):
        mutations = ("SET @v='ORACLE'", "SET @V:=1", "SET @`v`=@@GLOBAL.sql_mode",
                     "SET @'v'='ORACLE'", "SELECT @v := 'ORACLE'", "SELECT 'ORACLE' INTO @v",
                     "SET @other=1, @v=CONCAT('OR', 'ACLE')",
                     "SET @other=(SELECT @v := 'ORACLE')")
        for engine, version in SERVERS:
            for change in mutations:
                sql = "SET @v=@@sql_mode; " + change + "; SET sql_mode=@v; SELECT 1"
                with self.subTest(engine=engine, change=change):
                    plan, _ = self.plan(sql, engine, version)
                    self.assertIsNone(plan["units"][-1]["sql_mode"])
                    self.assertTrue(plan["units"][-1]["mode_unknown"])
                    with self.assertRaises(ed.EditorError):
                        self.plan(sql[:-8] + r"SELECT 'it\'s'", engine, version)

    def test_unknown_expressions_and_indirect_execution_cannot_restore_a_mode(self):
        changes = ("SET @v=@@GLOBAL.sql_mode", "SET @v=CONCAT(@@sql_mode, '')",
                   "CALL change_mode()", "EXECUTE stmt", "SOURCE other.sql")
        for engine, version in SERVERS:
            for change in changes:
                with self.subTest(engine=engine, change=change):
                    plan, _ = self.plan("SET @v=@@sql_mode; " + change +
                                        "; SET sql_mode=@v; SELECT 1", engine, version)
                    self.assertIsNone(plan["units"][-1]["sql_mode"])
            for restore in ("SET sql_mode=CONCAT(@v, '')", "SET sql_mode=@missing",
                            "SET @other=1, sql_mode=@v"):
                plan, _ = self.plan("SET @v=@@sql_mode; " + restore + "; SELECT 1", engine, version)
                self.assertIsNone(plan["units"][-1]["sql_mode"])

    def test_saved_values_never_survive_between_runs_or_unbound_sessions(self):
        self.plan("SET @v=@@sql_mode")
        plan, _ = self.plan("SET sql_mode=@v; SELECT 1")
        self.assertIsNone(plan["units"][-1]["sql_mode"])
        for mode in (None, ""):
            session = bound_session("mysql", "8.4.11", mode)
            if mode == "":
                session._routed_startup = True
            plan = ed._plan_run({"sql": "SET @v=@@sql_mode; SET sql_mode=@v; SELECT 1"}, session)
            self.assertIsNone(plan["units"][-1]["sql_mode"])

    def test_continue_cannot_remove_a_previously_possible_oracle_mode(self):
        definition = "DELIMITER ;;\nCREATE PROCEDURE p() BEGIN SELECT 1; END;;\nDELIMITER ;"
        block = "DELIMITER ;;\nBEGIN NOT ATOMIC SELECT 1; END;;\nDELIMITER ;"
        for version in ("10.11.19-MariaDB", "11.8.9-MariaDB"):
            for change in ("SET sql_mode='ORACLE'", "SET sql_mode=@unknown", "CALL change_mode()"):
                for tail in (definition, block):
                    with self.subTest(version=version, change=change, tail=tail):
                        sql = "SET @v=@@sql_mode; " + change + "; SET sql_mode=@v;\n" + tail
                        with self.assertRaisesRegex(ed.EditorError, "ORACLE"):
                            self.plan(sql, "mariadb", version, on_error="continue")


@unittest.skipIf(os.name == "nt", "the fake client is a POSIX script")
class EditorDumpL2ProcessTests(unittest.TestCase):
    engine = "mariadb"
    setUp = fixtures.ProcessTests.setUp
    call = fixtures.ProcessTests.call
    session_log = fixtures.ProcessTests.session_log

    def test_confirmation_executes_real_section_and_revalidates_the_restore(self):
        for name in ("mariadb1011.sql", "mariadb118.sql"):
            sql = routine_section(name) + "\nSELECT 1;"
            tab = name[:-4]
            self.assertEqual(self.call("/api/editor/open", tab=tab)["status"], "idle")
            review = self.call("/api/editor/run", tab=tab, sql=sql)
            self.assertEqual(review["status"], "needs_confirmation", review)
            result = self.call("/api/editor/run", tab=tab, sql=sql, confirmed=True,
                               confirm_token=review["confirmation"]["token"])
            self.assertEqual(result["status"], "complete", result)
            units = self.registry.get(tab).run.units
            self.assertEqual(units[-1]["sql_mode"], MODE)
            self.assertFalse(units[-1]["mode_unknown"])


if __name__ == "__main__":
    unittest.main()
