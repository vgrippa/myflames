"""F1 regressions: conditional SQL, helper modes and changing editor modes.

The planner, classifier, target guards, review tokens and transport are real.
Only the disposable client answers are synthetic; no server is started.
"""
import ast
import os
import unittest
from unittest import mock

import test_editor_session as fixtures
import test_editor_session_identity as identity_fixtures
from myflames import connector, dbops, editor_session as ed
from test_sql_lexer import (session as bound_session, ANSI_ATTACK, MSSQL_ATTACK, SAFETY,
                            CHARSET_COMMENT_ATTACK, CHARSET_MARIA_COMMENT_ATTACK,
                            CHARSET_ANSI_ATTACK, CHARSET_MSSQL_ATTACK, COMMENT_DELIMITER_ATTACK,
                            J1_SERVERS, J1_HINT_NESTED, J1_HINT_HASH, J1_COMMENT_DELIMITER, J1_ANSI_PROXY,
                            J3_COMMENT_SCRIPTS, K1_SERVERS, K1_ORDINARY_COMMENT_ATTACKS, K1_CONTROL)


VERSIONS = (("mysql", "8.4.11"), ("mariadb", "11.4.13-MariaDB"))
# The five shared attacks use future-version comments that both engines skip.
# Engine-specific syntax from the same repros is covered separately below.
SKIPPED_INPUTS = (
    "DELIMITER $$\nCREATE PROCEDURE p() /*!999999 BEGIN */ SELECT 1; "
    "DROP DATABASE prod; /*!999999 END */ $$",
    "DELIMITER $$\nALTER EVENT e DO /*!999999 BEGIN */ SELECT 1; "
    "DROP TABLE t; /*!999999 END */ $$",
    "DELIMITER $$\n/*!999999 CREATE PROCEDURE p() BEGIN */ "
    "DROP DATABASE prod /*!999999 ; END */ $$",
    "/*!999999 CREATE TABLE x ( */ ALTER TABLE big DROP INDEX ix /*!999999 ) */",
    "/*!999999 SELECT 1 FROM ( */ DELETE FROM t /*!999999 ) x */",
)


class SQLModePlanningTests(unittest.TestCase):
    """Bound-session attacks exercise the real planner, classifier and policy."""

    def test_unbound_oracle_compound_refusal_explains_missing_binding(self):
        sql = "DELIMITER //\nBEGIN NOT ATOMIC SELECT 1; END//"
        provisional = ed._plan_run({"sql": sql}, None)
        self.assertEqual(ed._decision(provisional, SAFETY)["action"], "confirm")
        sessions = []
        for engine, version in K1_SERVERS:
            for mode in ("", "ORACLE", None):
                sessions.append(bound_session(engine, version, mode, bound=False))
        for unbound in sessions:
            with self.subTest(session=unbound):
                with self.assertRaises(ed.EditorError) as raised:
                    ed._plan_run({"sql": sql}, unbound)
                message = str(raised.exception)
                self.assertIn("not bound to this session", message)
                self.assertIn("confirmed session", message)
                self.assertNotIn("known mode without ORACLE", message)
        # A bound MariaDB session with unknown mode still needs mode advice.
        with self.assertRaisesRegex(ed.EditorError, "known mode without ORACLE"):
            ed._plan_run({"sql": sql}, bound_session("mariadb", "11.8.9-MariaDB", None))

    def test_consumed_comment_prefixed_delimiter_never_appears_in_send(self):
        for engine, version in VERSIONS:
            for client in ("mysql", "mariadb"):
                bound = bound_session(engine, version, client_engine=client)
                for label, sql, expected in J3_COMMENT_SCRIPTS:
                    with self.subTest(engine=engine, client=client, label=label):
                        plan = ed._plan_run({"sql": sql}, bound)
                        self.assertEqual(tuple(unit["send"] for unit in plan["units"]), expected)
                        self.assertTrue(all("DELIMITER" not in unit["send"].upper() for unit in plan["units"]))
                        self.assertEqual(ed._decision(plan, SAFETY, bound)["action"], "confirm")

    def test_round_j_reproductions_refuse_for_every_client_and_server(self):
        cases = ((J1_HINT_NESTED, ""), (J1_HINT_HASH, ""),
                 (J1_HINT_HASH.replace("# */", "-- */"), ""),
                 (J1_COMMENT_DELIMITER, ""), (J1_ANSI_PROXY, "ANSI_QUOTES"))
        for engine, version in J1_SERVERS:
            for client in ("mysql", "mariadb"):
                for sql, known_mode in cases:
                    for mode in (known_mode, None):
                        with self.subTest(engine=engine, version=version, client=client, mode=mode, sql=sql):
                            bound = bound_session(engine, version, mode, client_engine=client)
                            with self.assertRaises(ed.EditorError):
                                ed._plan_run({"sql": sql}, bound)

    def test_round_j_ansi_proxy_count_rule_and_scoped_runs(self):
        for engine, version in VERSIONS:
            for client in ("mysql", "mariadb"):
                bound = bound_session(engine, version, "ANSI_QUOTES", client_engine=client)
                for scope in ({}, {"cursor": 1}, {"cursor": J1_ANSI_PROXY.index("SELECT \"second\"")},
                              {"selection": {"start": 0, "end": len(J1_ANSI_PROXY)}}):
                    with self.subTest(engine=engine, client=client, scope=scope):
                        with self.assertRaises(ed.EditorError):
                            ed._plan_run(dict(sql=J1_ANSI_PROXY, **scope), bound)
                # A custom delimiter intentionally groups server statements;
                # both client status readings agree on this complete packet.
                sql = "DELIMITER //\n" + J1_ANSI_PROXY + "\n//"
                plan = ed._plan_run({"sql": sql}, bound)
                self.assertEqual(plan["units"][0]["send"], J1_ANSI_PROXY)
                self.assertEqual(plan["verdict"]["kind"], "read")

    def test_client_command_reproductions_refuse_with_bound_and_unknown_modes(self):
        for engine, version in VERSIONS:
            cases = [(CHARSET_COMMENT_ATTACK, ""), (CHARSET_MARIA_COMMENT_ATTACK, ""),
                     (CHARSET_ANSI_ATTACK, "ANSI_QUOTES"), (COMMENT_DELIMITER_ATTACK, ""),
                     ("/*!40101 x */ DELIMITER //", "")]
            if engine == "mariadb":
                cases.append((CHARSET_MSSQL_ATTACK, "MSSQL"))
            else:
                cases.append((CHARSET_MSSQL_ATTACK, ""))
            for client in ("mysql", "mariadb"):
                for sql, bound_mode in cases:
                    for mode in (bound_mode, None):
                        with self.subTest(engine=engine, client=client, mode=mode, sql=sql):
                            if sql == CHARSET_MARIA_COMMENT_ATTACK and engine == client == "mariadb":
                                plan = ed._plan_run({"sql": sql}, bound_session(engine, version, mode,
                                                                              client_engine=client))
                                self.assertEqual(plan["units"][0]["send"], sql)
                                continue  # Both parsers keep C inside the same quoted token.
                            if sql == CHARSET_MSSQL_ATTACK and engine == "mysql":
                                plan = ed._plan_run({"sql": sql}, bound_session(engine, version, mode,
                                                                              client_engine=client))
                                self.assertEqual(plan["units"][0]["send"], sql)
                                continue  # MySQL does not recognize bracket identifiers.
                            bound = bound_session(engine, version, mode, client_engine=client)
                            with self.assertRaises(ed.EditorError):
                                ed._plan_run({"sql": sql}, bound)
                            editor = ed.EditorSession("i3-command", "test", {})
                            for key in ("engine", "server_version", "sql_mode", "connection_id",
                                        "server_identity", "_kill_identity", "client_engine"):
                                setattr(editor, key, getattr(bound, key))
                            with self.assertRaises((ed.EditorError, ValueError)):
                                editor._guarded_statement(editor._write_session_guard(), sql)

    def test_double_quoted_alter_requires_known_mode_and_preserves_wait_guard(self):
        for engine, version in VERSIONS:
            sql = 'ALTER TABLE "t" ADD COLUMN c INT'
            with self.subTest(engine=engine):
                for prefix in ("", "SET sql_mode=CONCAT('ANSI_QUOTES', ''); ", "CALL change_mode(); "):
                    with self.assertRaisesRegex(ed.EditorError, "quote mode is unknown"):
                        ed._plan_run({"sql": prefix + sql}, bound_session(engine, version, None if not prefix else ""))
                bound = bound_session(engine, version, "ANSI_QUOTES")
                plan = ed._plan_run({"sql": sql}, bound)
                alter = ed._alter_unit(plan)
                self.assertIsNotNone(alter)
                self.assertEqual(alter["idents"], ["t"])
                self.assertTrue(alter["mode_sensitive"])
                self.assertEqual(alter["packet_setup"]["send"], ed.ALTER_LOCK_WAIT)
                self.assertEqual(ed._decision(plan, SAFETY, bound)["action"], "confirm")
                for attack in ('ALTER TABLE "t" /*+ SET_VAR(lock_wait_timeout=100) */ ADD COLUMN c INT WAIT 100',
                               'ALTER TABLE "t" WAIT 100 ADD COLUMN c INT'):
                    decision = ed._decision(ed._plan_run({"sql": attack}, bound), SAFETY, bound)
                    self.assertEqual(decision["action"], "refuse")
                    self.assertIn("five-second metadata-lock wait", decision["text"])
                    with self.assertRaisesRegex(ed.EditorError, "quote mode is unknown"):
                        ed._plan_run({"sql": attack}, bound_session(engine, version, None))
                plan = ed._plan_run({"sql": 'DROP DATABASE "prod"'}, bound)
                self.assertEqual(ed._decision(plan, SAFETY, bound)["typed_target"], "prod")
                for sql in ('DROP DATABASE "prod"', 'CREATE PROCEDURE "p"() SELECT 1'):
                    with self.assertRaisesRegex(ed.EditorError, "quote mode is unknown"):
                        ed._plan_run({"sql": sql}, bound_session(engine, version, None))

    def test_legacy_mysql_comment_write_requires_consent(self):
        sql = "SELECT /*!100000 INTO OUTFILE '/tmp/i2' */"
        for version in ("5.7.44", "8.0.46"):
            with self.subTest(version=version):
                bound = bound_session("mysql", version)
                plan = ed._plan_run({"sql": sql}, bound)
                self.assertEqual(plan["units"][0]["send"], sql)
                self.assertEqual((plan["verdict"]["kind"], plan["verdict"]["category"]), ("write", "file"))
                self.assertEqual(ed._decision(plan, SAFETY, bound)["action"], "confirm")
                self.assertEqual(ed._decision(plan, dict(SAFETY, read_only=True), bound)["action"], "refuse")
        for version in ("8.1.0", "8.4.0", "9.7.2"):
            self.assertEqual(ed._plan_run({"sql": sql}, bound_session("mysql", version))["verdict"]["kind"], "read")

    def test_mysql_57_literal_mode_prediction_refuses_legacy_alias_count_mismatch(self):
        for mode in ("MSSQL", "ORACLE", "POSTGRESQL", "DB2", "MAXDB"):
            sql = "SET sql_mode='%s'; %s" % (mode, ANSI_ATTACK)
            with self.subTest(mode=mode):
                bound = bound_session("mysql", "5.7.44")
                with self.assertRaisesRegex(ed.EditorError, "several statements on the server"):
                    ed._plan_run({"sql": sql}, bound)

    def test_mariadb_client_keeps_ansi_identifier_in_guarded_packet(self):
        sql = 'SELECT "a\\b"'
        for engine, version in VERSIONS:
            with self.subTest(engine=engine):
                bound = bound_session(engine, version, "ANSI_QUOTES", client_engine="mariadb")
                plan = ed._plan_run({"sql": sql}, bound)
                self.assertEqual(plan["units"][0]["send"], sql)
                editor = ed.EditorSession("i2-client", "test", {})
                for key, value in vars(bound).items():
                    if key != "backslash_escapes":
                        setattr(editor, key, value)
                packet, unused = editor._guarded_statement(editor._write_session_guard(), sql)
                self.assertIn(sql, packet)
                self.assertIn("FIND_IN_SET('ANSI_QUOTES', @@SESSION.sql_mode) > 0) <=> 1", packet)
                # An escaped closing quote changes closure when the client
                # did not receive the ANSI bit, including through a proxy.
                for client in ("mysql", "mariadb"):
                    with self.assertRaises(ed.EditorError):
                        ed._plan_run({"sql": 'SELECT "a\\"'},
                                     bound_session(engine, version, "ANSI_QUOTES", client_engine=client))

    def test_cursor_selection_uses_selected_client_family(self):
        sql = 'SELECT "a\\"; SELECT 2 -- "'
        for engine, version in VERSIONS:
            bound = bound_session(engine, version, "ANSI_QUOTES", client_engine="mariadb")
            with self.assertRaisesRegex(ed.EditorError, "ANSI_QUOTES"):
                ed._plan_run({"sql": sql, "cursor": sql.index("SELECT 2") + 2}, bound)

    def test_custom_delimiter_cannot_match_an_invented_identifier_mask(self):
        # The server mask represents "a" as `0`. Those invented backticks
        # must not count as real delimiters in this valid single read.
        sql = 'DELIMITER "`"\nSELECT "a"`'
        for engine, version in VERSIONS:
            for client in ("mysql", "mariadb"):
                with self.subTest(engine=engine, client=client):
                    bound = bound_session(engine, version, "ANSI_QUOTES", client_engine=client)
                    plan = ed._plan_run({"sql": sql}, bound)
                    self.assertEqual([unit["send"] for unit in plan["units"]], ['SELECT "a"'])
                    self.assertEqual(plan["units"][0]["idents"], ["a"])
                    self.assertEqual(plan["verdict"]["kind"], "read")
                    self.assertEqual(ed._decision(plan, SAFETY, bound)["action"], "allow")

    def test_ansi_and_mssql_hidden_writes_require_review(self):
        for engine, version in (("mysql", "8.4.11"), ("mariadb", "11.8.9-MariaDB")):
            modes = ("ANSI_QUOTES", "ANSI")
            if engine == "mariadb":
                modes += ("ORACLE", "POSTGRESQL", "DB2", "MAXDB", "MSSQL")
            for mode in modes:
                for suffix in ("", ",NO_BACKSLASH_ESCAPES"):
                    with self.subTest(engine=engine, mode=mode + suffix):
                        session = bound_session(engine, version, mode + suffix, client_engine=engine)
                        if not suffix:
                            with self.assertRaisesRegex(ed.EditorError, "several statements on the server|ANSI_QUOTES"):
                                ed._plan_run({"sql": ANSI_ATTACK}, session)
                            continue
                        plan = ed._plan_run({"sql": ANSI_ATTACK}, session)
                        self.assertEqual(plan["verdict"]["kind"], "risky")
                        self.assertEqual(ed._decision(plan, SAFETY, session)["action"], "confirm")
                        self.assertEqual(ed._decision(plan, dict(SAFETY, read_only=True), session)["action"], "refuse")
                        self.assertIn("DELETE FROM t", " ".join(u["code"] for u in plan["units"]))
        session = bound_session("mariadb", "11.8.9-MariaDB", "MSSQL")
        with self.assertRaisesRegex(ed.EditorError, "several statements on the server"):
            ed._plan_run({"sql": MSSQL_ATTACK}, session)

    def test_unknown_modes_accept_agreeing_comments_and_refuse_hidden_writes(self):
        for engine, version in (("mysql", "8.4.11"), ("mariadb", "11.8.9-MariaDB")):
            session = bound_session(engine, version, None)
            for sql in ("/*!40101 SELECT 1 */", "CALL change_mode(); /*!40101 SET @x=1 */"):
                plan = ed._plan_run({"sql": sql}, session)
                self.assertTrue(all(u["mode_unknown"] for u in plan["units"]))
            for sql in (ANSI_ATTACK, "CALL change_mode(); " + ANSI_ATTACK):
                with self.assertRaisesRegex(ed.EditorError, "quote mode is unknown"):
                    ed._plan_run({"sql": sql}, session)
            with self.assertRaisesRegex(ed.EditorError, "quote mode is unknown"):
                ed._plan_run({"sql": ANSI_ATTACK, "cursor": 1}, session)
        with self.assertRaisesRegex(ed.EditorError, "quote mode is unknown"):
            ed._plan_run({"sql": MSSQL_ATTACK}, bound_session("mariadb", "11.8.9-MariaDB", None))

    def test_mysqldump_mode_headers_options_and_restore_plan_on_both_engines(self):
        for engine, version in (("mysql", "8.4.11"), ("mariadb", "11.8.9-MariaDB")):
            for header in ("SET SQL_MODE='NO_AUTO_VALUE_ON_ZERO'",
                           "/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */"):
                statements = [header, "/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */",
                              "/*!40101 SET NAMES utf8mb4 */",
                              "CREATE TABLE t (id int, CHECK (id > 0) /*!80016 NOT ENFORCED */)",
                              "/*!40101 SET SQL_MODE=@OLD_SQL_MODE */"]
                sql = ";\n".join(statements) + ";"
                with self.subTest(engine=engine, header=header):
                    plan = ed._plan_run({"sql": sql}, bound_session(engine, version))
                    self.assertEqual([u["send"] for u in plan["units"]], statements)
                    self.assertEqual(plan["verdict"]["kind"], "risky")
                    self.assertEqual("NOT ENFORCED" in plan["units"][3]["code"], engine == "mysql")

    def test_literal_mode_is_applied_to_later_units_but_expressions_are_unknown(self):
        for engine, version in (("mysql", "8.4.11"), ("mariadb", "11.8.9-MariaDB")):
            for assignment in ("SET sql_mode='ANSI_QUOTES'", "SET SESSION sql_mode='ANSI_QUOTES'",
                               "SET @@SESSION.`sql_mode`='ANSI_QUOTES'"):
                bound = bound_session(engine, version, client_engine=engine)
                with self.assertRaisesRegex(ed.EditorError, "several statements on the server|ANSI_QUOTES"):
                    ed._plan_run({"sql": assignment + "; " + ANSI_ATTACK}, bound)
                plan = ed._plan_run({"sql": assignment + '; SELECT "name"'}, bound)
                self.assertEqual(plan["units"][1]["sql_mode"], "ANSI_QUOTES")
                self.assertEqual([unit["kind"] for unit in plan["units"]], ["write", "read"])
            for assignment in ("SET sql_mode=@saved", "SET sql_mode=CONCAT(@@sql_mode, ',ANSI_QUOTES')"):
                with self.assertRaisesRegex(ed.EditorError, "quote mode is unknown"):
                    ed._plan_run({"sql": assignment + "; " + ANSI_ATTACK}, bound_session(engine, version))

    def test_mode_literal_cannot_come_from_comment_text(self):
        for engine, version in VERSIONS:
            # Only the double-quoted value assigns the mode; the comment's
            # single-quoted text must never supply the inferred value.
            sql = "/* 'ANSI_QUOTES' */ SET sql_mode=\"NO_BACKSLASH_ESCAPES\"; SELECT 1"
            plan = ed._plan_run({"sql": sql}, bound_session(engine, version))
            self.assertIsNone(plan["units"][1]["sql_mode"])
            self.assertTrue(plan["units"][1]["mode_unknown"])
            sql = "/* 'ANSI_QUOTES' */ SET sql_mode='NO_AUTO_VALUE_ON_ZERO'; SELECT 1"
            plan = ed._plan_run({"sql": sql}, bound_session(engine, version))
            self.assertEqual(plan["units"][1]["sql_mode"], "NO_AUTO_VALUE_ON_ZERO")

    def test_explain_cannot_expose_an_unprefixed_statement_under_ansi_quotes(self):
        for engine, version in VERSIONS:
            with self.assertRaisesRegex(ed.EditorError, "several statements on the server"):
                ed._plan_run({"sql": ANSI_ATTACK, "explain_format": "table"},
                             bound_session(engine, version, "ANSI_QUOTES"))

    def test_oracle_definitions_refuse_and_helper_requires_binding(self):
        session = bound_session("mariadb", "11.8.9-MariaDB", "ORACLE")
        self.assertTrue(ed.bound_oracle_mode(session))
        self.assertIsNone(ed.bound_oracle_mode(bound_session("mariadb", "11.8.9-MariaDB", "ORACLE", bound=False)))
        self.assertIsNone(ed.bound_oracle_mode(bound_session("mariadb", "11.8.9-MariaDB", None)))
        self.assertFalse(ed.bound_oracle_mode(bound_session(mode="ANSI")))
        for definition in ("CREATE PROCEDURE p AS BEGIN NULL; END", "CREATE OR REPLACE FUNCTION f RETURN INT AS BEGIN RETURN 1; END",
                           "CREATE PACKAGE BODY p AS END", "ALTER EVENT e DO BEGIN SELECT 1; END",
                           "ALTER PROCEDURE p COMMENT 'x'"):
            with self.subTest(definition=definition), self.assertRaisesRegex(ed.EditorError, "ORACLE mode"):
                ed._plan_run({"sql": "DELIMITER //\n" + definition + "//"}, session)
        self.assertEqual(ed._plan_run({"sql": "SELECT 1 AS \"x\""}, session)["verdict"]["kind"], "read")
        self.assertEqual(ed._plan_run({"sql": "CREATE TABLE t (event INT)"}, session)["verdict"]["kind"], "risky")
        with self.assertRaisesRegex(ed.EditorError, "known mode"):
            ed._plan_run({"sql": "CREATE PROCEDURE p() SELECT 1"},
                         bound_session("mariadb", "11.8.9-MariaDB", None))
        for initial, prefix in (("ORACLE", "SET sql_mode=''; "),
                                ("", "SET sql_mode='ORACLE'; SET sql_mode='INVALID_MODE'; "),
                                ("", "SET sql_mode=@saved; SET sql_mode=''; ")):
            with self.subTest(initial=initial, prefix=prefix), self.assertRaisesRegex(ed.EditorError, "ORACLE"):
                ed._plan_run({"sql": prefix + "CREATE PROCEDURE p() SELECT 1"},
                             bound_session("mariadb", "11.8.9-MariaDB", initial))

    def test_guard_binds_every_lexical_flag_and_oracle_grammar(self):
        for engine, version, mode in (("mysql", "8.4.11", "ANSI"), ("mariadb", "11.8.9-MariaDB", "MSSQL")):
            bound = bound_session(engine, version, mode)
            editor = ed.EditorSession("h1", "test", {})
            for key in ("engine", "server_version", "sql_mode", "connection_id", "server_identity", "_kill_identity", "client_engine"):
                setattr(editor, key, getattr(bound, key))
            packet, unused = editor._guarded_statement(editor._write_session_guard(), ANSI_ATTACK)
            self.assertIn("FIND_IN_SET('ANSI_QUOTES', @@SESSION.sql_mode) > 0) <=> 1", packet)
            if engine == "mariadb":
                self.assertIn("FIND_IN_SET('MSSQL', @@SESSION.sql_mode) > 0) <=> 1", packet)
                self.assertIn("FIND_IN_SET('ORACLE', @@SESSION.sql_mode) > 0) <=> 0", packet)

    def test_predicted_read_after_failed_mode_set_cannot_avoid_guard(self):
        for engine, version in VERSIONS:
            # A literal prediction is not an acknowledgement of SET. With
            # Continue on error an invalid mode leaves ANSI_QUOTES active.
            bound = bound_session(engine, version, "ANSI_QUOTES")
            sql = "SET sql_mode='INVALID_MODE'; " + ANSI_ATTACK
            plan = ed._plan_run({"sql": sql}, bound)
            read = plan["units"][1]
            self.assertEqual(read["kind"], "read")
            self.assertTrue(read["mode_sensitive"])
            self.assertTrue(ed._needs_session_guard(read))
            editor = ed.EditorSession("h1-mode-set", "test", {})
            for key in ("engine", "server_version", "sql_mode", "connection_id", "server_identity", "_kill_identity", "client_engine"):
                setattr(editor, key, getattr(bound, key))
            with self.assertRaises(ValueError):
                editor._guarded_statement(editor._write_session_guard(), read["send"], use_session_mode=False)


class ExecutableCommentTests(unittest.TestCase):
    def test_skip_rules_match_engine_lexers(self):
        # MySQL sql/sql_lex.cc:1991-2004; MariaDB sql/sql_lex.cc:2474-2486.
        for engine, version in VERSIONS:
            cases = (
                ("/*! SET @x = 1 */", False),
                ("/*!40101 SET @x = 1 */", False),
                ("/*!50000 SET @x = 1 */", False),
                ("/*!999999 SET @x = 1 */", True),
                ("/*!50700 SET @x = 1 */", engine == "mariadb"),
                ("/*!80016 SET @x = 1 */", engine == "mariadb"),
                ("/*!99999 SET @x = 1 */", True),
                ("/*M! SET @x = 1 */", engine == "mysql"),
                ("/*M!100000 SET @x = 1 */", engine == "mysql"),
                ("/*M!999999 SET @x = 1 */", True),
            )
            for sql, skipped in cases:
                with self.subTest(engine=engine, sql=sql):
                    found = dbops.skipped_executable_comments(sql, engine, version)
                    self.assertEqual(bool(found), skipped)
                    if found:
                        self.assertTrue(found[0]["certain"])
                        self.assertEqual((found[0]["start"], found[0]["end"]), (0, len(sql)))
                        self.assertNotIn("SET", found[0]["opener"])

    def test_six_digit_version_rules_and_boundaries(self):
        for engine, version in VERSIONS:
            for sql in ("/*!999999 SELECT 1 */", "/*!999999SELECT 1 */"):
                self.assertTrue(dbops.skipped_executable_comments(sql, engine, version))
        # MariaDB accepts a six-digit version without following whitespace;
        # MySQL then consumes five digits. 100000 > 9.7.0; 10000 <= 9.7.0.
        self.assertFalse(dbops.skipped_executable_comments("/*!100000SELECT 1 */", "mysql", "9.7.0"))
        self.assertTrue(dbops.skipped_executable_comments("/*!100000 SELECT 1 */", "mysql", "9.7.0"))
        self.assertFalse(dbops.skipped_executable_comments("/*!100000SELECT 1 */", "mariadb", "10.11.14"))
        self.assertFalse(dbops.skipped_executable_comments("/*!80411 SELECT 1 */", "mysql", "8.4.11"))
        self.assertTrue(dbops.skipped_executable_comments("/*!80412 SELECT 1 */", "mysql", "8.4.11"))
        self.assertFalse(dbops.skipped_executable_comments("/*M!110413 SELECT 1 */", "mariadb", "11.4.13"))
        self.assertTrue(dbops.skipped_executable_comments("/*M!110414 SELECT 1 */", "mariadb", "11.4.13"))

    def test_unknown_facts_are_conservative_and_quotes_are_not_comments(self):
        sql = "/*!999999 SELECT 1 */"
        for engine, version in ((None, None), ("mysql", None), ("mariadb", "custom build")):
            found = dbops.skipped_executable_comments(sql, engine, version)
            self.assertEqual(len(found), 1)
            self.assertFalse(found[0]["certain"])
        for engine, version in VERSIONS:
            quoted = "SELECT '/*!999999 SELECT 1 */', `/*!999999 name */` /* /*!999999 ignored */"
            self.assertEqual(dbops.skipped_executable_comments(quoted, engine, version), [])
            # Only NO_BACKSLASH_ESCAPES exposes this comment after the string.
            ambiguous = r"SELECT 'tail\' /*!999999 DELETE FROM t */ -- '"
            self.assertEqual(dbops.skipped_executable_comments(ambiguous, engine, version, True), [])
            self.assertEqual(len(dbops.skipped_executable_comments(ambiguous, engine, version, False)), 1)
            self.assertEqual(len(dbops.skipped_executable_comments(ambiguous, engine, version, None)), 1)

    def test_classifier_cannot_hide_delete_in_a_skipped_read_wrapper(self):
        sql = SKIPPED_INPUTS[-1]
        # Generic callers without server facts must consider the skipped body
        # too. Checking the per-statement verdict protects callers using it.
        for engine, version in ((None, None),) + VERSIONS:
            for backslash in (None, True, False):
                with self.subTest(engine=engine, backslash=backslash):
                    result = dbops.classify(sql, backslash, engine=engine, server_version=version)
                    self.assertEqual(result["kind"], "risky")
                    self.assertEqual(result["category"], "dml")
                    self.assertIn("DELETE without a WHERE", " ".join(result["reasons"]))
                    self.assertEqual(result["statements"][0]["kind"], "risky")
                    self.assertEqual(result["statements"][0]["text"], sql)

    def test_executed_block_comments_remain_one_ddl_and_dump_settings_are_accepted(self):
        sql = "DELIMITER //\nCREATE PROCEDURE p() /*!50000 BEGIN */ SELECT 1; SELECT 2; /*!50000 END */ //"
        for engine, version in VERSIONS:
            units = ed.plan_statements(sql, engine=engine, server_version=version, identity_bound=True)
            self.assertEqual(len(units), 1)
            self.assertEqual(len(ed._ddl_codes(units[0])), 1)
            session = bound_session(engine, version)
            plan = ed._plan_run({"sql": sql}, session)
            decision = ed._decision(plan, {"type": "production", "read_only": False, "profile_name": "prod"})
            self.assertEqual(decision["action"], "confirm", decision)
            for comment in ("/*!40101 SET @dump = 1 */", "/*! SET @dump = 1 */"):
                self.assertEqual(ed.plan_statements(comment, engine=engine, server_version=version, identity_bound=True)[0]["send"], comment)
            tagger = ed.EditorSession("tagger", "identity", {})
            for key in ("engine", "server_version", "sql_mode", "connection_id", "server_identity", "_kill_identity", "client_engine"):
                setattr(tagger, key, getattr(session, key))
            self.assertEqual(tagger._tag_statement(units[0]["send"]), tagger.statement_tag + units[0]["send"])

    def test_unknown_facts_cannot_hide_an_older_write_inside_a_future_read_wrapper(self):
        sql = "/*!999999 SELECT 1 FROM ( */ /*!50000 DELETE FROM t */ /*!999999 ) x */"
        for engine, version in ((None, None), ("mysql", None), ("mariadb", None)) + VERSIONS:
            with self.subTest(engine=engine, version=version):
                result = dbops.classify(sql, engine=engine, server_version=version)
                self.assertEqual(result["kind"], "risky")
                self.assertEqual(result["statements"][0]["kind"], "risky")
                if version is None:
                    self.assertTrue(result["ambiguous"])
        for engine, wrapper in (("mysql", "/*M!"), ("mariadb", "/*!50700")):
            # The wrapper is certainly skipped, even without a version.
            sql = "%s SELECT 1 FROM ( */ /*!50000 DELETE FROM t */ %s ) x */" % (wrapper, wrapper)
            result = dbops.classify(sql, engine=engine)
            self.assertEqual(result["kind"], "risky")
            self.assertIn("DELETE without a WHERE", " ".join(result["reasons"]))

    def test_skipped_fake_create_is_rejected_before_token_tagging(self):
        for engine, version in VERSIONS:
            session = ed.EditorSession("tagger", "identity", {})
            session.engine, session.server_version = engine, version
            for sql in SKIPPED_INPUTS[2:4]:
                with self.subTest(engine=engine, sql=sql):
                    with self.assertRaisesRegex(ed.EditorError, "not bound to this session"):
                        session._tag_statement(sql)


class EditorDollarDelimiterTests(unittest.TestCase):
    def test_identifier_delimiter_cannot_split_a_dollar_tag_before_detection(self):
        for body in ("$tag$ SELECT 1; DELETE FROM t $tag$", "$tag$ SELECT 1", "$tag$ SELECT 1 $TAG$"):
            sql = "DELIMITER tag\nSELECT " + body + "tag"
            for engine, version in VERSIONS:
                with self.subTest(body=body, engine=engine):
                    with self.assertRaisesRegex(ed.EditorError, "[Dd]ollar-quoted"):
                        ed.plan_statements(sql, engine=engine, server_version=version, identity_bound=True)
                    # Cursor selection must not discard the offending tag
                    # before the planner sees the selected fragment.
                    with self.assertRaisesRegex(ed.EditorError, "[Dd]ollar-quoted"):
                        ed._plan_run({"sql": sql, "cursor": sql.index("SELECT") + 2}, None)

    def test_quoted_dollar_tags_and_identifier_dollars_keep_their_meaning(self):
        self.assertEqual(ed.plan_statements("DELIMITER tag\nSELECT '$tag$'tag")[0]["send"], "SELECT '$tag$'")
        for sql in ("SELECT `dollar$tag$name`", "SELECT prefix$tag$name", "SELECT '/* $tag$ */'"):
            self.assertEqual(ed.plan_statements(sql)[0]["send"], sql)


class ClientFamilyTests(unittest.TestCase):
    def test_selected_client_family_is_not_inferred_from_filename(self):
        conn = connector.MySQLConnection(host="localhost", binary="/example/mysql")
        conn._client_use = {"path": "/example/mysql", "engine": "mariadb"}
        self.assertEqual(conn.client_engine("/example/mysql"), "mariadb")
        with mock.patch("myflames.connector.os.path.isfile", return_value=True), \
                mock.patch("myflames.client_tools.probe", return_value={"available": True, "engine": "mysql"}) as probe:
            self.assertEqual(conn.client_engine("/example/mariadb"), "mysql")
            probe.assert_called_once_with("mariadb", "/example/mariadb")
        with mock.patch("myflames.connector.os.path.isfile", return_value=True), \
                mock.patch("myflames.client_tools.probe", return_value={"available": False, "engine": "mysql"}):
            self.assertIsNone(conn.client_engine("/unprobed/mysql"))
            self.assertEqual(conn._defaults_engine("/unprobed/mysql"), "mysql")
        with mock.patch("myflames.connector.os.path.isfile", return_value=True), \
                mock.patch("myflames.client_tools.probe", return_value={"available": False, "engine": "mariadb"}):
            self.assertIsNone(conn.client_engine("/unprobed/mariadb"))
            self.assertEqual(conn._defaults_engine("/unprobed/mariadb"), "mariadb")

    def test_invocation_defaults_carries_the_chosen_client_family(self):
        path = connector._InvocationDefaults("/private/defaults.cnf", "/example/mysql", "mariadb")
        self.assertEqual(str(path), "/private/defaults.cnf")
        self.assertEqual((path.binary, path.client_engine), ("/example/mysql", "mariadb"))


@unittest.skipIf(os.name == "nt", "the fake client is a POSIX script")
class EditorF1ProcessTests(unittest.TestCase):
    engine = "mysql"
    setUp = fixtures.ProcessTests.setUp
    call = fixtures.ProcessTests.call
    run_sql = fixtures.ProcessTests.run_sql
    rows = fixtures.ProcessTests.rows
    session_log = fixtures.ProcessTests.session_log
    until = fixtures.ProcessTests.until
    logged = fixtures.ProcessTests.logged
    packets = identity_fixtures.EditorIdentityTests.packets
    rewrite_client = identity_fixtures.EditorIdentityTests.rewrite_client
    inject_answers = identity_fixtures.EditorIdentityTests.inject_answers

    def test_round_k1_nested_ordinary_comments_send_nothing_but_control_runs(self):
        self.assertEqual(self.run_sql("SELECT 7")["status"], "complete")
        session = self.registry.get("tab1")
        for engine, version in K1_SERVERS:
            if engine != self.engine:
                continue
            for client in ("mysql", "mariadb"):
                # Supply consistent bound server facts at the real API entry;
                # only the disposable client's answers are synthetic.
                session.server_version = version
                session.server_identity["version"] = version
                session._kill_identity["version"] = version
                session.client_engine = client
                self.assertTrue(ed._lexer_session_context(session)["identity_bound"])
                for label, sql in K1_ORDINARY_COMMENT_ATTACKS:
                    with self.subTest(version=version, client=client, label=label):
                        before = self.packets()
                        result = self.call("/api/editor/run", sql=sql)
                        self.assertEqual(result["status"], "invalid", result)
                        self.assertIn("ordinary comment contains a nested", result["message"])
                        self.assertEqual(self.packets(), before, "no helper or user SQL may be sent")
                with self.subTest(version=version, client=client, label="KD-control"):
                    before = len(self.packets())
                    result = self.call("/api/editor/run", sql=K1_CONTROL)
                    self.assertEqual(result["status"], "complete", result)
                    self.assertTrue(any(K1_CONTROL in packet[2] for packet in self.packets()[before:]))
                    self.assertEqual(result["run"]["statements"][0]["kind"], "read")

    def test_session_pins_client_family_from_banner_not_server_identity(self):
        family = "mariadb" if self.engine == "mysql" else "mysql"
        banner = "mariadb Ver 11.4.13-MariaDB" if family == "mariadb" else "mysql Ver 8.4.11"
        self.rewrite_client(lambda source: source.replace(
            'print("mariadb Ver 11.4.13-MariaDB" if ENGINE == "mariadb" else "mysql Ver 8.4.11")',
            'print(%r)' % banner))
        self.assertEqual(self.run_sql("SELECT 7")["status"], "complete")
        session = self.registry.get("tab1")
        self.assertEqual(session.engine, self.engine)
        self.assertEqual(session.client_engine, family)
        self.assertEqual(ed._lexer_session_context(session)["client_engine"], family)

    def test_client_framing_refusals_send_no_packets(self):
        self.assertEqual(self.run_sql("SELECT 7")["status"], "complete")
        session = self.registry.get("tab1")
        self.assertEqual(session.client_engine, self.engine)
        cases = [("SELECT 'a\\\nb'", "line ends with a backslash"),
                 ("SELECT /*+\n ' */ 1", "optimizer hint"), (J1_HINT_NESTED, "optimizer hint"),
                 ("SELECT /*+ SET_VAR(x='unclosed) */ 1", "optimizer hint"),
                 ("SELECT /*+\n BKA(`t1`) */ 1", "multi-line optimizer hint"),
                 (J1_HINT_HASH, "optimizer hint"), (J1_COMMENT_DELIMITER, "client command DELIMITER")]
        if self.engine == "mysql":
            cases.append(("SELECT /*+\n $tag$ */ 1", "multi-line optimizer hint"))
        for sql, message in cases:
            with self.subTest(sql=sql):
                before = len(self.packets())
                result = self.call("/api/editor/run", sql=sql)
                self.assertEqual(result["status"], "invalid", result)
                self.assertIn(message, result["message"])
                self.assertEqual(len(self.packets()), before)

    def test_ansi_proxy_status_disagreement_sends_no_packets(self):
        self.assertEqual(self.run_sql("SELECT 7")["status"], "complete")
        session = self.registry.get("tab1")
        session.sql_mode = "ANSI_QUOTES"
        for client in ("mysql", "mariadb"):
            session.client_engine = client
            before = len(self.packets())
            result = self.call("/api/editor/run", sql=J1_ANSI_PROXY)
            self.assertEqual(result["status"], "invalid", result)
            self.assertEqual(len(self.packets()), before)

    def test_client_commands_and_unknown_alter_targets_send_no_packets(self):
        self.assertEqual(self.run_sql("SELECT 7")["status"], "complete")
        session = self.registry.get("tab1")
        cases = [(CHARSET_COMMENT_ATTACK, ""), (CHARSET_MARIA_COMMENT_ATTACK, ""),
                 (CHARSET_ANSI_ATTACK, "ANSI_QUOTES"), (COMMENT_DELIMITER_ATTACK, ""),
                 ("/*!40101 x */ DELIMITER //", ""), ("/* ordinary */ charset utf8mb4", ""),
                 ("charset;", ""), ("DELIMITER //\n/* ordinary */ charset//", ""),
                 ("SELECT 1 \\c", ""), ("SELECT 1 \\d //", ""), ("SELECT 1 \\-", ""),
                 ('ALTER TABLE "t" /*+ SET_VAR(lock_wait_timeout=100) */ ADD COLUMN c INT WAIT 100', None)]
        if self.engine == "mariadb":
            cases.append((CHARSET_MSSQL_ATTACK, "MSSQL"))
        for sql, bound_mode in cases:
            for mode in (bound_mode, None):
                with self.subTest(sql=sql, mode=mode):
                    if sql == CHARSET_MARIA_COMMENT_ATTACK and self.engine == "mariadb":
                        continue  # This client/server pair treats the bytes as one string.
                    session.sql_mode = mode
                    before = len(self.packets())
                    result = self.call("/api/editor/run", sql=sql)
                    self.assertEqual(result["status"], "invalid", result)
                    self.assertEqual(len(self.packets()), before, "refusal must precede every helper or SQL packet")

    def test_known_ansi_alter_review_uses_the_restored_table_name(self):
        self.assertEqual(self.run_sql("SELECT 7")["status"], "complete")
        session = self.registry.get("tab1")
        session.sql_mode = "ANSI_QUOTES"
        sql = 'ALTER TABLE "tt" ADD COLUMN c INT'
        before = len(self.packets())
        result = self.call("/api/editor/run", sql=sql)
        self.assertEqual(result["status"], "needs_confirmation", result)
        self.assertEqual(result["confirmation"]["alter_preflight"]["table"], "tt")
        self.assertIn(ed.ALTER_LOCK_WAIT, result["confirmation"]["statements"])
        self.assertFalse(any(sql in packet[2] for packet in self.packets()[before:]), "review does not execute ALTER")

    def test_skipped_wrappers_cannot_bypass_the_server_reading_write_gate(self):
        self.assertEqual(self.run_sql("SELECT 7")["status"], "complete")
        expected = ("refused", "refused", "needs_confirmation", "needs_confirmation", "needs_confirmation")
        for sql, status in zip(SKIPPED_INPUTS, expected):
            with self.subTest(sql=sql):
                before = len(self.packets())
                # Review only. Known skipped text is accepted under G1;
                # the actual DDL/DML must still refuse or require consent.
                result = self.call("/api/editor/run", sql=sql)
                self.assertEqual(result["status"], status, result)
                if status == "refused":
                    self.assertEqual(result["decision"]["action"], "refuse")
                    self.assertEqual(len(self.packets()), before)
                else:
                    self.assertEqual(result["confirmation"]["kind"], "risky")
                    original = ed._plan_run({"sql": sql}, self.registry.get("tab1"))["units"][-1]["send"]
                    self.assertIn(original, result["confirmation"]["statements"])
                    self.assertFalse(any(original in p[2] for p in self.packets()[before:]))
                    if sql == SKIPPED_INPUTS[2]:
                        self.assertEqual(result["confirmation"]["typed_target"], "prod")

    def test_engine_specific_skipped_blocks_still_refuse_ddl_batches(self):
        self.run_sql("SELECT 7")
        sql = ("DELIMITER $$\nCREATE PROCEDURE p() /*M! BEGIN */ SELECT 1; "
               "ALTER TABLE big DROP INDEX ix; /*M! END */ $$" if self.engine == "mysql" else
               "DELIMITER $$\nCREATE PROCEDURE p() /*!50700 BEGIN */ SELECT 1; "
               "DROP DATABASE prod; /*!50700 END */ $$")
        before = len(self.packets())
        result = self.call("/api/editor/run", sql=sql)
        self.assertEqual(result["status"], "refused", result)
        self.assertEqual(result["decision"]["title"], "One DDL per confirmation")
        self.assertEqual(len(self.packets()), before)
        if self.engine == "mariadb":
            skipped = "/*!80016 SET @must_not_run = 1 */"
            result = self.run_sql(skipped)
            self.assertEqual(result["status"], "complete", result)
            self.assertEqual(result["run"]["statements"][0]["kind"], "read")
            packet = next(p[2] for p in self.packets() if skipped in p[2])
            self.assertIn("SET @myflames_target", packet)

    def test_executed_comments_reach_one_guarded_ddl_packet_and_dump_settings_stay_accepted(self):
        self.assertEqual(self.run_sql("SELECT 7")["status"], "complete")
        sql = "DELIMITER //\nCREATE PROCEDURE p() /*!50000 BEGIN */ SELECT 1; SELECT 2; /*!50000 END */ //"
        result = self.run_sql(sql)
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(len(result["run"]["statements"]), 1)
        definition = ed.plan_statements(sql)[0]["send"]
        packets = [p[2] for p in self.packets() if definition in p[2]]
        self.assertEqual(len(packets), 1)
        self.assertIn("SET @myflames_target = ", packets[0])
        self.assertTrue(packets[0].endswith(definition), "no private token may enter the definition")
        dump = "/*!40101 SET @dump = 1 */"
        self.assertEqual(self.run_sql(dump)["status"], "complete")
        self.assertTrue(any(dump in p[2] for p in self.packets()))

    def test_first_alter_run_uses_bound_skipped_option_before_review(self):
        sql = "ALTER TABLE tt ADD COLUMN c INT /*!999999 INVISIBLE */"
        self.assertIsNone(self.registry.get("tab1"))
        result = self.call("/api/editor/run", sql=sql)
        self.assertEqual(result["status"], "needs_confirmation", result)
        self.assertIn(sql, result["confirmation"]["statements"])
        self.assertIsNotNone(self.registry.get("tab1"))
        self.assertFalse(any(sql in p[2] for p in self.packets()), "review does not execute ALTER")
        plan = ed._plan_run({"sql": sql}, self.registry.get("tab1"))
        self.assertEqual(plan["units"][-1]["code"], "ALTER TABLE tt ADD COLUMN c INT")

    def test_first_read_run_replans_skipped_comment_and_guards_the_noop(self):
        sql = "/*!999999 SELECT 1 */"
        self.assertEqual(ed._plan_run({"sql": sql}, None)["units"][0]["code"], "SELECT 1")
        self.assertIsNone(self.registry.get("tab1"))
        result = self.run_sql(sql)
        self.assertEqual(result["status"], "complete", result)
        session = self.registry.get("tab1")
        self.assertEqual(ed._plan_run({"sql": sql}, session)["units"][0]["code"], "")
        packet = next(p[2] for p in self.packets() if sql in p[2])
        self.assertIn("SET @myflames_target", packet)
        self.assertIn("CONNECTION_ID() <>", packet)
        self.assertIn("@@SESSION.sql_mode", packet)
        self.assertEqual(self.rows(self.run_sql("SELECT 8")), [["8"]])

    def test_start_run_rechecks_units_planned_before_server_facts(self):
        self.run_sql("SELECT 7")
        session = self.registry.get("tab1")
        sql = "/*!999999 SELECT 1 */"
        units = ed.plan_statements(sql)
        ed.classify_units(units, True, sql)
        run = ed.Run(units, 1000, self.connection, sql, ed.Offsets(sql), "all")
        before = len(self.packets())
        with self.assertRaisesRegex(ed.EditorError, "interpretation changed after planning"):
            session.start_run(run)
        self.assertEqual(len(self.packets()), before)
        self.assertFalse(session.run_lock.locked())

    def test_g1_delete_requires_consent_and_g8_mode_change_sends_nothing(self):
        from test_sql_lexer import G1A, G2, G8
        self.run_sql("SELECT 7")
        for sql in (G1A,):
            before = len(self.packets())
            result = self.call("/api/editor/run", sql=sql)
            self.assertEqual(result["status"], "needs_confirmation", result)
            self.assertEqual(result["confirmation"]["kind"], "risky")
            self.assertEqual(result["confirmation"]["statements"], [sql])
            self.assertEqual(len(self.packets()), before)
        before = len(self.packets())
        nested = self.call("/api/editor/run", sql=G2)
        self.assertEqual(nested["status"], "invalid", nested)
        self.assertIn("nested", nested["message"])
        sql = "DELIMITER //\nSET SESSION sql_mode = @next_mode//\n" + G8 + "//"
        result = self.call("/api/editor/run", sql=sql)
        self.assertEqual(result["status"], "invalid", result)
        self.assertEqual(result["code"], "invalid_statement")
        self.assertEqual(len(self.packets()), before)

    def test_unknown_initial_mode_and_unbound_routed_facts_send_no_user_sql(self):
        from test_sql_lexer import OUTFILE
        self.run_sql("SELECT 7")
        session = self.registry.get("tab1")
        session.sql_mode = None
        before = len(self.packets())
        result = self.call("/api/editor/run", sql=OUTFILE)
        self.assertEqual(result["status"], "invalid", result)
        self.assertIn("quote mode is unknown", result["message"])
        self.assertEqual(len(self.packets()), before)
        session.sql_mode = ""
        session.server_identity = None
        session._kill_identity = None
        sql = "SELECT 1 /*!80016 + 2 */"
        result = self.call("/api/editor/run", sql=sql)
        self.assertEqual(result["status"], "invalid", result)
        self.assertIn("not bound to this session", result["message"])
        self.assertEqual(len(self.packets()), before)

    def test_conditional_read_cannot_cross_the_reviewed_backend(self):
        self.run_sql("SELECT 7")
        sql = "SELECT 7 /*!999999 + 1 */"
        result = self.run_sql(sql)
        self.assertEqual(result["status"], "complete", result)
        packet = next(p[2] for p in self.packets() if sql in p[2])
        self.assertIn("SET @myflames_target", packet)
        self.assertIn("CONNECTION_ID() <>", packet)
        self.assertIn("@@SESSION.sql_mode", packet)
        self.assertTrue(packet.endswith(sql), packet)

    def test_stop_uses_helper_mode_in_both_directions_and_tab_writes_assert_their_mode(self):
        # Model the fresh helper's global mode separately from the tab. This
        # server-side check would reject the old cached-mode KILL packet.
        self.rewrite_client(lambda source: source.replace(
            'self.sql_mode = "STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION"',
            'self.sql_mode = os.environ.get("FAKE_GLOBAL_SQL_MODE", "")').replace(
            "def one_shot(sql):\n", r'''def one_shot(sql):
    expected = re.search(r"FIND_IN_SET\('NO_BACKSLASH_ESCAPES', @@SESSION.sql_mode\) > 0\) <=> ([01])", sql)
    actual = "NO_BACKSLASH_ESCAPES" in os.environ.get("FAKE_GLOBAL_SQL_MODE", "").split(",")
    if expected and int(expected.group(1)) != int(actual):
        sys.stderr.write("ERROR 1242 (21000) at line 2: Subquery returns more than 1 row\n")
        return 1
'''))
        for index, tab_mode in enumerate(("NO_BACKSLASH_ESCAPES", "")):
            os.environ["FAKE_GLOBAL_SQL_MODE"] = "" if tab_mode else "NO_BACKSLASH_ESCAPES"
            tab = "opposite-mode-%d" % index
            self.assertEqual(self.run_sql("SET SESSION sql_mode = '%s'" % tab_mode, tab=tab)["status"], "complete")
            session = self.registry.get(tab)
            self.assertEqual(session.sql_mode, tab_mode)
            write = "SET @mode_test = %d" % index
            self.assertEqual(self.run_sql(write, tab=tab)["status"], "complete")
            packet = next(p[2] for p in reversed(self.packets()) if write in p[2])
            self.assertIn("@@SESSION.sql_mode) > 0) <=> %d" % int(bool(tab_mode)), packet)
            sql = "SELECT 7 /*gate*/"
            self.run_sql(sql, tab=tab, wait=0)
            self.until(lambda: self.logged(tab, sql), "the owned read to reach its gate")
            self.assertTrue(self.call("/api/editor/stop", tab=tab)["stopped"])
            self.assertTrue(session.run.done.wait(5))
            self.assertEqual(session.run.state, "stopped", session.snapshot())
            self.assertFalse(session.ended)
            batches = [ast.literal_eval(line) for line in (self.state / "helper-batches.log").read_text().splitlines()]
            kill = next(b for b in reversed(batches) if "KILL QUERY" in b)
            self.assertNotIn("@@SESSION.sql_mode", kill)
            self.assertIn("information_schema.PROCESSLIST", kill)
            self.assertIn("WHERE ID = %d" % session.connection_id, kill)
            self.assertIn("AND ID <> CONNECTION_ID()", kill)
            self.assertIn(session.statement_tag.rstrip(), kill)
            self.assertIn("@@global.server_uuid" if self.engine == "mysql" else "@@global.server_id", kill)
            self.assertEqual(self.rows(self.run_sql("SELECT 8", tab=tab)), [["8"]])

    def test_sql_mode_change_followed_by_guarded_writes_completes_and_refreshes_mode(self):
        self.assertEqual(self.run_sql("CREATE TEMPORARY TABLE tt (id INT, v TEXT)")["status"], "complete")
        sql = "SET SESSION sql_mode = 'NO_BACKSLASH_ESCAPES'; INSERT INTO tt VALUES (1, 'mode'); SET @x = 42"
        result = self.run_sql(sql)
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual([u["status"] for u in result["run"]["statements"]], ["ok"] * 3)
        session = self.registry.get("tab1")
        self.assertFalse(session.ended)
        self.assertEqual(session.sql_mode, "NO_BACKSLASH_ESCAPES")
        sends = [u["send"] for u in ed.plan_statements(sql)]
        packets = [next(p[2] for p in self.packets() if s in p[2]) for s in sends]
        self.assertIn("@@SESSION.sql_mode", packets[0])
        for packet in packets[1:]:
            # A literal assignment supplies the later mode prediction. It
            # is checked in each packet, even though the cached mode stays
            # unknown until refresh_state finishes the run.
            self.assertIn("FIND_IN_SET('NO_BACKSLASH_ESCAPES', @@SESSION.sql_mode) > 0) <=> 1", packet)
            self.assertIn("FIND_IN_SET('ANSI_QUOTES', @@SESSION.sql_mode) > 0) <=> 0", packet)
            self.assertIn("SET @myflames_target = ", packet)
            self.assertIn("CONNECTION_ID() <> %d" % session.connection_id, packet)
        self.assertEqual(self.rows(self.run_sql("SELECT @x")), [["42"]])
        self.assertEqual(self.rows(self.run_sql("SELECT * FROM tt")), [["1", "mode"]])
        self.assertEqual(self.run_sql("SET @after_refresh = 1")["status"], "complete")
        packet = next(p[2] for p in reversed(self.packets()) if "SET @after_refresh = 1" in p[2])
        self.assertIn("@@SESSION.sql_mode) > 0) <=> 1", packet)

    def test_ambiguous_later_unit_after_mode_change_refuses_before_any_send(self):
        self.run_sql("SELECT 7")
        for later in (r"INSERT INTO tt VALUES (1, 'tail\')", r"SELECT 'tail\'", r"/*! SET @x = 'tail\' */"):
            sql = "SET SESSION sql_mode = @next_mode; " + later
            before = len(self.packets())
            result = self.run_sql(sql)
            self.assertEqual(result["status"], "invalid", result)
            self.assertEqual(len(self.packets()), before)

    def test_proxysql_9006_has_recovery_message_and_original_detail(self):
        original = "ProxySQL Error: connection is locked to hostgroup 30 but trying to reach hostgroup 32"
        self.inject_answers('''        if upper == "SELECT 9006":
            self.error(line, 9006, "HY000", %r)
            return
''' % original)
        result = self.run_sql("SELECT 9006")
        self.assertEqual(result["status"], "error", result)
        error = result["run"]["error"]
        self.assertEqual((error["code"], error["sqlstate"]), (9006, "HY000"))
        self.assertIn("proxy has tied this tab to one server group after a refused change", error["message"])
        self.assertIn("Close and reopen the tab to run reads again", error["message"])
        self.assertEqual(error["detail"], original)
        self.assertIn(original, error["message"], "the current result UI displays message text")
        self.assertFalse(result["session"]["ended"])


class MariaDBEditorF1ProcessTests(EditorF1ProcessTests):
    engine = "mariadb"


if __name__ == "__main__":
    unittest.main()
