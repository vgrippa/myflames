"""M2: normalize client-inserted comment spaces before SQL review.

The planner, policy, identity guard and subprocess framing are real. The
independent client oracle covers its documented ASCII domain; the disposable
process client supplies protocol replies without a database.
"""
import importlib.util
import json
import os
from pathlib import Path
import unittest

from client_addline_oracle import client_packets
import test_editor_session as fixtures
from test_editor_session_j2 import SAFETY, bound_session
from myflames import dbops, editor_session as ed


EXAMPLES = (
    ("SELECT a /*comment*/, b FROM t", "SELECT a /*comment*/ , b FROM t"),
    ("SELECT 1/*x*/+2", "SELECT 1/*x*/ +2"),
    ("INSERT INTO t /*cols*/(a,b) VALUES (1,2)", "INSERT INTO t /*cols*/ (a,b) VALUES (1,2)"),
    ("SELECT * FROM t /*idx*/WHERE a=1", "SELECT * FROM t /*idx*/ WHERE a=1"),
    ("DELIMITER ;;\nCREATE PROCEDURE p() BEGIN SELECT 1 /* note */; END;;",
     "CREATE PROCEDURE p() BEGIN SELECT 1 /* note */ ; END"),
)


class CommentSpaceM2Tests(unittest.TestCase):
    def plan(self, sql, engine="mysql", client="mysql", mode=""):
        session = bound_session(engine, "8.4.0" if engine == "mysql" else "11.8.9-MariaDB", mode)
        session.client_engine = client
        return ed._plan_run({"sql": sql}, session), session

    def test_five_reported_examples_review_normalized_client_packets(self):
        for engine in ("mysql", "mariadb"):
            for client in ("mysql", "mariadb"):
                for sql, expected in EXAMPLES:
                    with self.subTest(engine=engine, client=client, sql=sql):
                        plan, session = self.plan(sql, engine, client)
                        self.assertEqual(len(plan["units"]), 1)
                        unit = plan["units"][0]
                        self.assertEqual(unit["send"], expected)
                        self.assertEqual(client_packets(expected, client), ([expected], "", []))
                        self.assertEqual(sql[unit["start"]:unit["end"]], unit["text"])
                        decision = ed._decision(plan, SAFETY, session)
                        self.assertEqual(decision["action"], "confirm" if "CREATE" in expected or
                                         "INSERT" in expected else "allow")

    def test_closers_in_quoted_data_and_line_comments_stay_unchanged(self):
        for sql in ("SELECT '/*x*/+2', `/*x*/id`, 1", 'SELECT "/*x*/+2", 1',
                    "SELECT 1 # /*x*/+2\n, 2", "SELECT 1 -- /*x*/+2\n, 2",
                    "SELECT '*/', 1 /* a */ +2", "SELECT 1 /* a */\t+2"):
            for engine in ("mysql", "mariadb"):
                with self.subTest(sql=sql, engine=engine):
                    plan, _ = self.plan(sql, engine, engine)
                    self.assertEqual(plan["units"][0]["send"], sql)
                    self.assertFalse(plan["units"][0]["_comment_spaces"])

    def test_hint_and_executable_closers_normalize_across_client_families(self):
        for engine in ("mysql", "mariadb"):
            for client in ("mysql", "mariadb", None):
                for comment in ("/* x */", "/*+ x */", "/*! x */", "/*!999999 x */", "/*M! x */"):
                    sql = "SELECT 1 " + comment + "+2"
                    expected = "SELECT 1 " + comment + " +2"
                    with self.subTest(engine=engine, client=client, comment=comment):
                        plan, _ = self.plan(sql, engine, client)
                        self.assertEqual(plan["units"][0]["send"], expected)
                        if client:
                            self.assertEqual(client_packets(expected, client), ([expected], "", []))

    def test_literal_tokens_shift_after_each_inserted_space(self):
        sql = "SET /*first*/sql_mode=/*second*/'NO_BACKSLASH_ESCAPES'; SELECT 1"
        for engine in ("mysql", "mariadb"):
            with self.subTest(engine=engine):
                plan, _ = self.plan(sql, engine, engine)
                first, second = plan["units"]
                self.assertEqual(first["send"], "SET /*first*/ sql_mode=/*second*/ 'NO_BACKSLASH_ESCAPES'")
                values = [first["send"][start:end] for kind, start, end in first["_tokens"] if kind == "string"]
                self.assertEqual(values, ["'NO_BACKSLASH_ESCAPES'"])
                self.assertEqual(second["sql_mode"], "NO_BACKSLASH_ESCAPES")
                self.assertEqual(first["_tokens"], tuple(dbops.lex_sql(
                    first["send"], engine=engine, server_version=("8.4.0" if engine == "mysql"
                                                                else "11.8.9-MariaDB"))["tokens"]))

    def test_multiple_comments_are_idempotent_and_do_not_shift_source_ranges(self):
        sql = "  -- leading\nSELECT 1/*a*//*b*/+2; SELECT 3/*c*/+4"
        plan, _ = self.plan(sql)
        self.assertEqual([u["send"] for u in plan["units"]],
                         ["SELECT 1/*a*/ /*b*/ +2", "SELECT 3/*c*/ +4"])
        for unit in plan["units"]:
            self.assertEqual(sql[unit["start"]:unit["end"]], unit["text"])
            again, _ = self.plan(unit["send"])
            self.assertEqual(again["units"][0]["send"], unit["send"])
            self.assertFalse(again["units"][0]["_comment_spaces"])

    def test_custom_delimiters_leave_terminal_closers_unchanged(self):
        for delimiter in (";", ";;", "//"):
            sql = "DELIMITER %s\nSELECT 1/*x*/%s" % (delimiter, delimiter)
            plan, _ = self.plan(sql)
            self.assertEqual(plan["units"][0]["send"], "SELECT 1/*x*/")
        plan, _ = self.plan("DELIMITER ;;\nSELECT 1/*x*/; SELECT 2/*y*/;;")
        self.assertEqual(plan["units"][0]["send"], "SELECT 1/*x*/ ; SELECT 2/*y*/")

    def test_error_locations_map_to_original_source_after_insertions(self):
        sql = "-- leading\nSELECT '😀'/*a*/+1/*b*/+bad; SELECT 2/*c*/+wrong"
        plan, _ = self.plan(sql)
        for unit, near in zip(plan["units"], ("bad", "wrong")):
            for prefix in (0, 19):
                error_unit = dict(unit, send="x" * prefix + unit["send"], explain_prefix=prefix)
                result = ed.error_position("near '%s' at line 1" % near, error_unit, ed.Offsets(sql))
                original = sql.index(near)
                self.assertEqual(result["offset"], len(sql[:original].encode("utf-16-le")) // 2)
                self.assertEqual((result["line"], result["column"]),
                                 ed.Offsets(sql).line_col(original))
                self.assertTrue(result["exact"])

    def test_comment_overlap_and_client_commands_still_refuse(self):
        for sql in ("SELECT 1 /*a /*/+2", "SELECT /*+ a */*x*/1",
                    "SELECT 1 /*x*/\\C utf8mb4", "SELECT 1 /* missing"):
            with self.subTest(sql=sql), self.assertRaises(ed.EditorError):
                self.plan(sql)

    def test_normalized_write_keeps_its_identity_guard_and_private_tag(self):
        plan, _ = self.plan("INSERT INTO t /*cols*/(a) VALUES (1)")
        send = plan["units"][0]["send"]
        session = ed.EditorSession("m2", "identity", {})
        tagged = session._tag_statement(send)
        self.assertEqual(tagged, session.statement_tag + "INSERT INTO t /*cols*/ (a) VALUES (1)")
        guard = dbops.target_session_guard("mysql", {
            "connection_id": 1, "server_uuid": "11111111-2222-3333-4444-555555555555"})
        script, _ = dbops.target_guarded_script(guard, [tagged], backslash_escapes=True,
                                              engine="mysql", server_version="8.4.0", sql_mode="",
                                              client_engine="mysql")
        packets, residual, commands = client_packets(script.split("\n", 1)[1].rsplit("\n", 2)[0], "mysql")
        self.assertEqual((residual, commands), ("", []))
        self.assertEqual(len(packets), 1)
        self.assertIn(guard[0], packets[0])
        self.assertIn(tagged, packets[0])

    def test_live_dump_extraction_keeps_every_saved_setting_from_real_fixtures(self):
        path = Path(__file__).parent / "live" / "live_writes_ga.py"
        spec = importlib.util.spec_from_file_location("m2_live_dump", str(path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for name, engine in (("mysql57.sql", "mysql"), ("mysql84.sql", "mysql"),
                             ("mariadb1011.sql", "mariadb"), ("mariadb118.sql", "mariadb")):
            with self.subTest(name=name):
                section = module._routine_section((Path(__file__).parent / "fixtures" / "dumps" / name)
                                                  .read_text(encoding="utf-8"))
                self.assertNotIn("DROP PROCEDURE", section)
                self.assertNotIn("enable the sandbox mode", section)
                self.assertIn("DELIMITER ;;", section)
                self.assertIn("DELIMITER ;\n", section)
                for setting in ("sql_mode", "cs_client", "cs_results", "col_connection"):
                    self.assertRegex(section, r"SET @saved_" + setting + r"\s*=\s*@@")
                plan, session = self.plan(section, engine, engine)
                self.assertEqual(ed._decision(plan, SAFETY, session)["action"], "confirm")
                self.assertEqual(len([u for u in plan["units"] if "CREATE" in u["code"]]), 1)


@unittest.skipIf(os.name == "nt", "the fake client is a POSIX script")
class CommentSpaceM2ProcessTests(unittest.TestCase):
    engine = "mysql"
    setUp = fixtures.ProcessTests.setUp
    call = fixtures.ProcessTests.call
    session_log = fixtures.ProcessTests.session_log

    def test_review_and_execution_use_the_same_normalized_write(self):
        sql = "INSERT INTO t /*cols*/(a) VALUES (1)"
        expected = "INSERT INTO t /*cols*/ (a) VALUES (1)"
        self.assertEqual(self.call("/api/editor/open")["status"], "idle")
        first = self.call("/api/editor/run", sql=sql)
        self.assertEqual(first["status"], "needs_confirmation")
        self.assertEqual(first["confirmation"]["statements"], [expected])
        self.assertFalse(any(expected in line for line in self.session_log()))
        result = self.call("/api/editor/run", sql=sql, confirmed=True,
                           confirm_token=first["confirmation"]["token"])
        self.assertEqual(result["status"], "complete", result)
        session = self.registry.get("tab1")
        self.assertIn(expected, self.session_log())
        packets = [json.loads(line)[2] for line in (self.state / "packets.log").read_text().splitlines()]
        protected = [wire for wire in packets if session.statement_tag + expected in wire]
        self.assertEqual(len(protected), 1)
        self.assertTrue(protected[0].startswith(session.statement_tag + "SET @myflames_target = "))
        self.assertEqual(result["run"]["statements"][0]["text"], sql)
        self.assertEqual(result["run"]["statements"][0]["sent"], expected)


class MariaDBCommentSpaceM2ProcessTests(CommentSpaceM2ProcessTests):
    engine = "mariadb"


if __name__ == "__main__":
    unittest.main()
