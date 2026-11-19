"""G1 source-grounded SQL lexing and editor consent regressions.

Expected tokens follow mysql-server/sql/sql_lex.cc:1037-1066,1782-1812,
1931-2075 and mariadb-server/sql/sql_lex.cc:1824-1860,2401-2562. No real
connection is opened; the planner, classifier and target guards are real.
"""
import types
import unittest

from myflames import dbops, editor_session as ed, mcp_server


SERVERS = (("mysql", "8.4.11"), ("mariadb", "11.8.9-MariaDB"))
G1A = "/*! # */ '\n*/ /*!999999 SELECT 1 FROM ( */ DELETE FROM t /*!999999 ) x */ # '"
G1B = ("DELIMITER $$\nCREATE PROCEDURE p() /*!50000 COMMENT '*/' */ "
       "/*!999999 BEGIN */ SELECT '1'; DROP DATABASE prod; /*!999999 END */ -- '$$")
G2 = "/*!999999 /* */ SELECT 1 FROM ( */ DELETE FROM t /*!999999 ) x */"
G8 = "/*!50000 SELECT '*/', 'tail\\' */ ; DROP DATABASE prod; /* ' */"
OUTFILE = "SELECT 'a\\' INTO OUTFILE '/var/lib/mysql-files/out' -- '"
SAFETY = {"type": "production", "read_only": False, "profile_name": "prod"}


def session(engine="mysql", version="8.4.11", mode="", bound=True, wsrep=None):
    identity = {"connection_id": 1, "server_id": "1", "hostname": "lexer-test", "port": "3306",
                "version": version}
    if engine == "mysql":
        identity["server_uuid"] = "11111111-2222-3333-4444-555555555555"
    complete = dict(identity)
    if wsrep is not None:
        complete["wsrep_on"] = "ON" if wsrep else "OFF"
        if wsrep:
            complete["wsrep_gcomm_uuid"] = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    return types.SimpleNamespace(engine=engine, server_version=version, sql_mode=mode,
                                 backslash_escapes=mode is None or "NO_BACKSLASH_ESCAPES" not in mode,
                                 in_transaction=False, connection_id=1,
                                 server_identity=identity if bound else None,
                                 _kill_identity=complete if bound else None, _routed_startup=False)


class SQLLexerTests(unittest.TestCase):
    def code(self, sql, engine, version, escaping=True):
        units = dbops.split_statements(sql, escaping, delimiter="//", engine=engine, server_version=version)
        self.assertEqual(len(units), 1)
        return units[0]["code"]

    def test_g1_line_comment_does_not_close_executed_comment(self):
        for engine, version in SERVERS:
            with self.subTest(engine=engine):
                found = dbops.skipped_executable_comments(G1A, engine, version)
                self.assertEqual([c["opener"] for c in found], ["/*!999999", "/*!999999"])
                self.assertEqual(dbops.classify(G1A, engine=engine, server_version=version)["kind"], "risky")
                self.assertEqual(self.code(G1A, engine, version), "DELETE FROM t")

    def test_executed_quote_and_line_comments_are_sql_tokens(self):
        for engine, version in SERVERS:
            for inner in ("SELECT '*/'", 'SELECT "*/"', "SELECT `*/`", "# */ '\nSELECT 1",
                          "-- */ '\nSELECT 1", "/* plain */ SELECT 1", "/* nested /* plain */ SELECT 1"):
                sql = "/*!50000 " + inner + " */"
                with self.subTest(engine=engine, inner=inner):
                    scan = dbops.lex_sql(sql, True, engine, version)
                    self.assertEqual(scan["unclosed"], [])
                    self.assertEqual(scan["comments"][0]["end"], len(sql))
                    self.assertEqual(dbops.classify(sql, engine=engine, server_version=version)["kind"], "read")
                    self.assertFalse(dbops.skipped_executable_comments(sql, engine, version))

    def test_skipped_comment_consumes_exactly_one_nested_level(self):
        for engine, version in SERVERS:
            scan = dbops.lex_sql(G2, True, engine, version)
            first = scan["comments"][0]
            self.assertEqual(G2[first["start"]:first["end"]], "/*!999999 /* */ SELECT 1 FROM ( */")
            self.assertEqual(self.code(G2, engine, version), "DELETE FROM t")
            # Not arbitrary nesting: consume_comment(0) stops at the first */.
            sql = "/*!999999 /* /* */ */ SELECT 1 */ DELETE FROM t"
            found = dbops.skipped_executable_comments(sql, engine, version)
            self.assertEqual(sql[:found[0]["end"]], "/*!999999 /* /* */ */")

    def test_version_digits_belong_to_the_engine(self):
        for opener in ("/*!", "/*M!"):
            for space in ("", " "):
                sql = opener + "100000" + space + "END*/"
                self.assertEqual(self.code(sql, "mariadb", "11.8.9-MariaDB"), "END")
        self.assertEqual(self.code("/*!100000END*/", "mysql", "9.7.0"), "0END")
        self.assertEqual(self.code("/*!100000 END*/", "mysql", "9.7.0"), "")
        self.assertEqual(self.code("/*M!100000END*/", "mysql", "9.7.0"), "")

    def test_mysql_only_and_maria_only_comments(self):
        for number in (50700, 80016, 99999):
            self.assertEqual(self.code("/*!%d SELECT 1 */" % number, "mariadb", "11.8.9-MariaDB"), "")
        self.assertEqual(self.code("/*M!50700 SELECT 1 */", "mariadb", "11.8.9-MariaDB"), "SELECT 1")
        self.assertEqual(self.code("/*!80016 SELECT 1 */", "mysql", "8.0.16"), "SELECT 1")
        self.assertEqual(self.code("/*!80016 SELECT 1 */", "mysql", "5.7.44"), "")

    def test_backticks_double_but_do_not_backslash_escape(self):
        sql = "SELECT `a\\`, `b``c`, 'it''s'"
        for engine, version in SERVERS:
            for escaping in (True, False):
                units = dbops.split_statements(sql, escaping, engine=engine, server_version=version)
                self.assertEqual(units[0]["idents"], ["a\\", "b`c"])
                self.assertEqual(units[0]["code"], "SELECT `0`, `1`, ''")
                self.assertFalse(units[0]["unclosed"])

    def test_skipped_quotes_do_not_change_server_reading(self):
        sql = "SELECT 1 /*!999999 '*/ DELETE FROM t"
        for engine, version in SERVERS:
            scan = dbops.lex_sql(sql, True, engine, version)
            self.assertIn(("code", sql.index(" DELETE"), len(sql)), scan["tokens"])
            # The client still opens a quote, so this cannot reach transport.
            units = dbops.split_statements(sql, True, engine=engine, server_version=version)
            self.assertTrue(units[0]["unclosed"])

    def test_client_delimiter_is_not_suppressed_by_server_version_gate(self):
        sql = "SELECT 1 /*!999999 ; DELETE FROM t */"
        for engine, version in SERVERS:
            units = dbops.split_statements(sql, engine=engine, server_version=version)
            self.assertEqual(len(units), 2)
            self.assertEqual([u["code"] for u in units], ["SELECT 1", ""])
            self.assertEqual([u["text"] for u in units], ["SELECT 1 /*!999999", "DELETE FROM t */"])

    def test_client_split_skipped_comment_cannot_hide_later_packet(self):
        # The client's --force path could run the DELETE fragment after
        # rejecting the earlier unterminated conditional-comment fragment.
        sql = "SELECT 1 /*!999999 ; DELETE FROM t # */\n;"
        for engine, version in SERVERS:
            with self.subTest(engine=engine):
                verdict = dbops.classify(sql, engine=engine, server_version=version)
                self.assertEqual(verdict["kind"], "risky")
                self.assertIn(dbops.REASON_COMMENT_FRAMING, verdict["reasons"])
                with self.assertRaisesRegex(ed.EditorError, "client delimiter"):
                    ed._plan_run({"sql": sql}, session(engine, version))

    def test_target_guard_unknown_mode_rejects_executed_comment_token_drift(self):
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            dbops.target_statement(G8, delimiter="//")

    def test_unknown_mode_takes_file_write_even_when_unit_offsets_match(self):
        for engine, version in SERVERS:
            result = dbops.classify(OUTFILE, engine=engine, server_version=version)
            self.assertEqual((result["kind"], result["category"]), ("write", "file"))
            true = dbops.split_statements(OUTFILE, True, engine=engine, server_version=version)
            false = dbops.split_statements(OUTFILE, False, engine=engine, server_version=version)
            self.assertEqual([(u["start"], u["end"]) for u in true], [(u["start"], u["end"]) for u in false])
            self.assertTrue(result["ambiguous"])
            self.assertEqual(result["statements"][0]["kind"], "write")
            self.assertEqual(dbops.classify(OUTFILE, True, engine=engine, server_version=version)["kind"], "read")
            self.assertEqual(dbops.classify(OUTFILE, False, engine=engine, server_version=version)["kind"], "write")

    def test_target_guard_compares_whole_reading_inside_executable_comments(self):
        for engine, version in SERVERS:
            with self.assertRaisesRegex(ValueError, "ambiguous|closed"):
                dbops.target_statement(G8, delimiter="//", engine=engine, server_version=version)
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                dbops.target_statement(OUTFILE, engine=engine, server_version=version)
            self.assertEqual(dbops.target_statement(G8, False, "//", engine, version), G8)

    def test_unknown_version_is_not_evidence_that_a_comment_executes(self):
        for engine, version in ((None, None), ("mysql", None), ("mariadb", "custom")):
            found = dbops.skipped_executable_comments("/*!40101 SELECT 1 */", engine, version)
            self.assertEqual(len(found), 1)
            self.assertFalse(found[0]["certain"])
        attack = "/*!999999 SELECT 1 FROM ( */ /*!50000 DELETE FROM t */ /*!999999 ) x */"
        self.assertEqual(dbops.classify(attack)["kind"], "risky")
        with self.assertRaises(ValueError):
            mcp_server._read_sql(attack)

    def test_mcp_cannot_mistake_g1_for_read(self):
        for sql in (G1A, G2, "/*! # */ '\n*/ DELETE FROM t"):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                mcp_server._read_sql(sql)


class EditorLexerTests(unittest.TestCase):
    def plan(self, sql, engine="mysql", version="8.4.11", mode="", **kwargs):
        return ed._plan_run({"sql": sql}, session(engine, version, mode, **kwargs))

    def test_g1_and_g2_delete_reaches_write_gate_with_bound_facts(self):
        for engine, version in SERVERS:
            for sql in (G1A, G2):
                with self.subTest(engine=engine, sql=sql):
                    plan = self.plan(sql, engine, version)
                    self.assertEqual(plan["verdict"]["kind"], "risky")
                    self.assertEqual(ed._decision(plan, SAFETY)["action"], "confirm")
                    self.assertEqual(plan["units"][0]["code"], "DELETE FROM t")
                    self.assertEqual(plan["units"][0]["send"], sql)
                    self.assertEqual(ed._decision(plan, dict(SAFETY, read_only=True))["action"], "refuse")

    def test_g1b_ddl_body_cannot_hide_drop(self):
        for engine, version in SERVERS:
            plan = self.plan(G1B, engine, version)
            self.assertEqual(len(ed._ddl_codes(plan["units"][0])), 2)
            self.assertEqual(ed._decision(plan, SAFETY)["action"], "refuse")

    def test_g3_mariadb_end_terminates_the_routine(self):
        for opener in ("/*!", "/*M!"):
            for space in ("", " "):
                sql = ("DELIMITER //\nCREATE PROCEDURE p() BEGIN SELECT 1; " + opener + "100000" +
                       space + "END*/; DROP DATABASE prod; END//")
                plan = self.plan(sql, "mariadb", "11.8.9-MariaDB")
                self.assertEqual(ed._decision(plan, SAFETY)["action"], "refuse")
                self.assertTrue(any("DROP DATABASE prod" in part for part in ed._ddl_codes(plan["units"][0])))

    def test_g3_skipped_mariadb_bodies_cannot_supply_blocks_or_file_writes(self):
        sql = ("DELIMITER $$\nCREATE PROCEDURE p() /*M!999999BEGIN*/ SELECT 1; "
               "DROP DATABASE prod; /*M!999999END*/$$")
        plan = self.plan(sql, "mariadb", "11.8.9-MariaDB")
        self.assertEqual(ed._decision(plan, SAFETY)["action"], "refuse")
        sql = "SELECT 1 /*M!999999INTO OUTFILE '/tmp/x'*/"
        plan = self.plan(sql, "mariadb", "11.8.9-MariaDB")
        self.assertEqual(plan["units"][0]["code"], "SELECT 1")
        self.assertEqual(plan["verdict"]["kind"], "read")
        self.assertTrue(ed._needs_session_guard(plan["units"][0]))

    def test_g8b_mode_change_refuses_before_any_unit_runs(self):
        sql = "DELIMITER //\nSET SESSION sql_mode = 'NO_BACKSLASH_ESCAPES'//\n" + G8 + "//"
        for engine, version in SERVERS:
            with self.assertRaisesRegex(ed.EditorError, "NO_BACKSLASH_ESCAPES"):
                self.plan(sql, engine, version)

    def test_unknown_initial_mode_marks_every_unit_unknown(self):
        for engine, version in SERVERS:
            with self.assertRaisesRegex(ed.EditorError, "quote mode is unknown"):
                self.plan(OUTFILE, engine, version, mode=None)
            plan = self.plan("SELECT 1; SELECT 'it''s'", engine, version, mode=None)
            self.assertTrue(all(u["mode_unknown"] for u in plan["units"]))
            self.assertEqual(plan["verdict"]["kind"], "read")

    def test_dump_headers_and_cross_version_options_keep_exact_sql(self):
        for engine, version in SERVERS + (("mysql", "5.7.44"), ("mysql", "8.0.16")):
            for header in (40101, 40103, 50503):
                sql = "/*!%d SET @dump = 1 */" % header
                plan = self.plan(sql, engine, version)
                self.assertEqual(plan["units"][0]["send"], sql)
                self.assertEqual(plan["verdict"]["kind"], "write")
            sql = "CREATE DATABASE d /*!80016 DEFAULT ENCRYPTION='N' */"
            plan = self.plan(sql, engine, version)
            self.assertEqual(plan["units"][0]["send"], sql)
            self.assertEqual(ed._decision(plan, SAFETY)["action"], "confirm")
            expected = "CREATE DATABASE d" if engine == "mariadb" or version.startswith("5.") else "CREATE DATABASE d DEFAULT ENCRYPTION=''"
            self.assertEqual(plan["units"][0]["code"], expected)

    def test_skip_is_a_noop_not_a_write_on_the_bound_server(self):
        plan = self.plan("/*!80016 SET @x = 1 */", "mariadb", "11.8.9-MariaDB")
        self.assertEqual(plan["verdict"]["kind"], "read")
        self.assertEqual(plan["units"][0]["send"], "/*!80016 SET @x = 1 */")

    def test_refusals_name_construct_without_sql_body(self):
        secret = "private_sql_body"
        cases = (("/*!40101 SELECT '%s' */", {"bound": False}),
                 ("/*!40101 SELECT '%s' */", {"mode": None}),
                 ("/*!!999999 SELECT '%s' */", {}),
                 ("/*!40101 SELECT '%s'", {}),
                 ("/*!99997 SELECT '%s' */", {"wsrep": True}),
                 ("/*!99997 SELECT '%s' */", {}))
        for template, kwargs in cases:
            with self.subTest(template=template, kwargs=kwargs):
                with self.assertRaises(ed.EditorError) as raised:
                    self.plan(template % secret, "mariadb", "11.8.9-MariaDB", **kwargs)
                self.assertIn("executable comment", str(raised.exception))
                self.assertNotIn(secret, str(raised.exception))
        for engine, version in (("unknown", "8.4.11"), ("mysql", "custom")):
            with self.assertRaises(ed.EditorError):
                self.plan("/*!40101 SELECT 1 */", engine, version)

    def test_wsrep_marker_is_accepted_only_with_known_off(self):
        plan = self.plan("SELECT 1 /*!99997 + 2 */", "mariadb", "11.8.9-MariaDB", wsrep=False)
        self.assertEqual(plan["units"][0]["code"], "SELECT 1")
        self.assertEqual(plan["verdict"]["kind"], "read")
        # MariaDB-specific comments do not take the skipped-MySQL branch
        # where the 99997 wsrep exception lives (sql_lex.cc:2477-2500).
        for wsrep in (None, False, True):
            plan = self.plan("SELECT 1 /*M!99997 + 2 */", "mariadb", "11.8.9-MariaDB", wsrep=wsrep)
            self.assertEqual(plan["units"][0]["code"], "SELECT 1 + 2")
        bound = session("mariadb", "11.8.9-MariaDB", wsrep=False)
        editor = ed.EditorSession("wsrep-lexer", "test", {})
        for key in ("engine", "server_version", "sql_mode", "connection_id", "server_identity", "_kill_identity"):
            setattr(editor, key, getattr(bound, key))
        sql = "SELECT 1 /*!99997 + 2 */"
        packet, unused_lines = editor._guarded_statement(editor._write_session_guard(), sql)
        self.assertIn("OR NOT (@@SESSION.wsrep_on <=> 0)", packet)
        self.assertIn(sql, packet)
        editor._kill_identity["wsrep_on"] = "unknown"
        with self.assertRaisesRegex(ed.EditorError, "wsrep executable comment"):
            ed._plan_run({"sql": sql}, editor)

    def test_routed_or_incomplete_identity_never_authorizes_skipped_code(self):
        for engine, version in SERVERS:
            for change in ("missing_complete", "missing_field", "version_drift", "session_drift", "routed"):
                s = session(engine, version)
                if change == "missing_complete":
                    s._kill_identity = None
                elif change == "missing_field":
                    del s.server_identity["hostname"]
                    del s._kill_identity["hostname"]
                elif change == "version_drift":
                    s.server_version = "5.7.44"
                elif change == "session_drift":
                    s.connection_id = 2
                else:
                    s._routed_startup = True
                with self.subTest(engine=engine, change=change), self.assertRaises(ed.EditorError):
                    ed._plan_run({"sql": "SELECT 1 /*!80016 + 2 */"}, s)

    def test_quoted_comment_text_does_not_require_identity(self):
        plan = self.plan("SELECT '/*!99997 private */'", bound=False, mode=None)
        self.assertEqual(plan["verdict"]["kind"], "read")


if __name__ == "__main__":
    unittest.main()
