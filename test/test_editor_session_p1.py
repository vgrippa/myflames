"""P1: Explain uses the planned client reading and handles pre-send Stop.

Planner, classifier, policy and guarded packet construction remain real.
The disposable client supplies protocol replies; helper wrappers place Stop
in the cancellation window without mocking _send or its None return.
"""
import json
import os
import threading
import unittest
from unittest import mock

import test_editor_session as fixtures
import test_editor_session_identity as identity_fixtures
from test_editor_session_j2 import bound_session
from myflames import dbops, editor_session as ed


ANSI_SQL = r"""SELECT "a\" AS b, '"; SELECT 2 -- '"""
SYNC = ed.TAG + dbops.CLIENT_STATUS_SYNC


class ExplainP1PlannerTests(unittest.TestCase):
    def test_explain_uses_the_same_ansi_reading_and_helper_as_run(self):
        for version in ("10.11.16-MariaDB", "11.4.10-MariaDB", "11.8.9-MariaDB"):
            for mode in ("ANSI_QUOTES", "ANSI", "ORACLE", "POSTGRESQL", "DB2", "MAXDB", "MSSQL"):
                session = bound_session("mariadb", version, mode)
                for scope in ({}, {"cursor": 0}, {"selection": {"start": 0, "end": len(ANSI_SQL)}}):
                    run = ed._plan_run(dict(scope, sql=ANSI_SQL), session)
                    probe = ed.TAG + dbops.client_status_probe("$$", True, ansi_quotes=True, client_engine="mariadb")[0]
                    self.assertEqual(ed._review_statements(run["units"]), [SYNC, probe, ANSI_SQL])
                    for fmt, prefix in (("table", "EXPLAIN "), ("json", "EXPLAIN FORMAT=JSON ")):
                        with self.subTest(version=version, mode=mode, scope=scope, fmt=fmt):
                            plan = ed._plan_run(dict(scope, sql=ANSI_SQL, explain_format=fmt), session)
                            self.assertEqual(len(plan["units"]), 1)
                            self.assertEqual(ed._review_statements(plan["units"]), [SYNC, probe, prefix + ANSI_SQL])
                            self.assertEqual(plan["verdict"]["kind"], "read")

    def test_explain_still_refuses_multiple_statements_under_a_custom_delimiter(self):
        session = bound_session("mariadb", "11.4.10-MariaDB", "ANSI_QUOTES")
        for sql in (ANSI_SQL + "\n; SELECT 3", "DELIMITER //\n" + ANSI_SQL + "\n; SELECT 3//"):
            for fmt in ("table", "json"):
                with self.subTest(sql=sql, fmt=fmt):
                    with self.assertRaisesRegex(ed.EditorError, "Explain one statement at a time"):
                        ed._plan_run({"sql": sql, "explain_format": fmt, "scope": "all"}, session)

    def test_explain_does_not_use_a_helper_to_override_an_unknown_client_or_binding(self):
        for missing in ("client", "binding", "mode"):
            session = bound_session("mariadb", "11.4.10-MariaDB", "ANSI_QUOTES")
            if missing == "client":
                session.client_engine = None
            elif missing == "binding":
                session.server_identity = {}
            else:
                session.sql_mode = None
            for fmt in ("table", "json"):
                with self.subTest(missing=missing, fmt=fmt), self.assertRaises(ed.EditorError):
                    ed._plan_run({"sql": ANSI_SQL, "explain_format": fmt}, session)

    def test_explain_parameters_use_bound_context_and_refuse_unknown_mode(self):
        cases = (
            ("mysql", "8.4.0", "", 'SELECT * FROM t WHERE name = "alice" AND id = :id',
             'SELECT * FROM t WHERE name = "alice" AND id = 1', False),
            ("mysql", "8.4.0", "ANSI_QUOTES", 'SELECT "col" FROM t WHERE id = :id',
             'SELECT "col" FROM t WHERE id = 1', False),
            ("mysql", "8.4.0", "", "SELECT 'it\\'s', :id", "SELECT 'it\\'s', 1", True),
            ("mysql", "8.4.0", "NO_BACKSLASH_ESCAPES", "SELECT 'a\\', ':id', :id",
             "SELECT 'a\\', ':id', 1", True),
            ("mysql", "8.4.0", "NO_BACKSLASH_ESCAPES", "SELECT 'a\\', :id, 'b'",
             "SELECT 'a\\', 1, 'b'", True),
            ("mariadb", "11.4.10-MariaDB", "", 'SELECT "col" FROM t WHERE id = :id',
             'SELECT "col" FROM t WHERE id = 1', False),
            ("mariadb", "11.4.10-MariaDB", "ANSI_QUOTES", 'SELECT "col" FROM t WHERE id = :id',
             'SELECT "col" FROM t WHERE id = 1', False),
            ("mariadb", "11.4.10-MariaDB", "ANSI_QUOTES", 'SELECT "a\\", ":id", :id FROM t',
             'SELECT "a\\", ":id", 1 FROM t', True),
            ("mariadb", "11.4.10-MariaDB", "MSSQL", "SELECT [col] FROM t WHERE id = :id",
             "SELECT [col] FROM t WHERE id = 1", False),
            ("mariadb", "11.4.10-MariaDB", "MSSQL", "SELECT [col :x] FROM t WHERE id = :id",
             "SELECT [col :x] FROM t WHERE id = 1", False),
        )
        for engine, version, mode, sql, expected_inner, expected_sync in cases:
            for fmt in ("table", "json"):
                session = bound_session(engine, version, mode)
                prefix = "EXPLAIN " if engine == "mariadb" and fmt == "table" else (
                    "EXPLAIN FORMAT=%s " % ("TRADITIONAL" if fmt == "table" else "JSON"))
                with self.subTest(engine=engine, mode=mode, sql=sql, fmt=fmt):
                    plan = ed._plan_run({"sql": sql, "explain_format": fmt, "parameters": {"id": 1}}, session)
                    self.assertEqual(len(plan["units"]), 1)
                    self.assertEqual(plan["units"][0]["send"], prefix + expected_inner)
                    self.assertEqual(bool(plan["units"][0]["client_status_sync"]), expected_sync)
                    self.assertEqual(plan["verdict"]["kind"], "read")
        # Unknown mode or unbound session still refuses ambiguous quotes and dialects.
        for sql in ("SELECT 'it\\'s', :id", 'SELECT "col" FROM t WHERE id = :id',
                    "SELECT [col] FROM t WHERE id = :id"):
            for unknown in (None, bound_session("mariadb", "11.4.10-MariaDB", None)):
                if sql == "SELECT [col] FROM t WHERE id = :id" and unknown is None:
                    # Unbound checks MariaDB too, so [col] is refused there as well.
                    pass
                with self.subTest(sql=sql, unknown=unknown):
                    with self.assertRaises(ed.EditorError):
                        ed._plan_run({"sql": sql, "explain_format": "table", "parameters": {"id": 1}}, unknown)
        unbound = bound_session("mariadb", "11.4.10-MariaDB", "ANSI_QUOTES")
        unbound._routed_startup = True
        for sql in ("SELECT 'it\\'s', :id", 'SELECT "col" FROM t WHERE id = :id',
                    "SELECT [col] FROM t WHERE id = :id"):
            with self.subTest(routed=sql):
                with self.assertRaises(ed.EditorError):
                    ed._plan_run({"sql": sql, "explain_format": "table", "parameters": {"id": 1}}, unbound)
        with self.assertRaises(ed.EditorError):
            ed._plan_run({"sql": "SELECT /*!80100 1 */ :id", "explain_format": "table",
                          "parameters": {"id": 1}}, None)

    def test_explain_preserves_custom_delimiter_with_semicolons_in_strings_or_comments(self):
        for engine, version, mode, sql, expected_inner in (
                ("mysql", "8.4.0", "", "DELIMITER //\nSELECT 'a;b'//", "SELECT 'a;b'"),
                ("mysql", "8.4.0", "", "DELIMITER //\nSELECT /*!999999 ; */ 1//", "SELECT /*!999999 ; */ 1"),
                ("mariadb", "11.4.10-MariaDB", "", "DELIMITER //\nSELECT 'a;b'//", "SELECT 'a;b'"),
                ("mariadb", "11.4.10-MariaDB", "", "DELIMITER //\nSELECT /*!999999 ; */ 1//",
                 "SELECT /*!999999 ; */ 1"),
                ("mariadb", "11.4.10-MariaDB", "MSSQL", "DELIMITER //\nSELECT 1 AS [a;b]//", "SELECT 1 AS [a;b]"),
        ):
            session = bound_session(engine, version, mode)
            for fmt in ("table", "json"):
                prefix = "EXPLAIN " if engine == "mariadb" and fmt == "table" else (
                    "EXPLAIN FORMAT=%s " % ("TRADITIONAL" if fmt == "table" else "JSON"))
                with self.subTest(engine=engine, mode=mode, sql=sql, fmt=fmt):
                    plan = ed._plan_run({"sql": sql, "explain_format": fmt}, session)
                    self.assertEqual(plan["units"][0]["send"], prefix + expected_inner)
                    self.assertEqual(plan["units"][0]["delimiter"], "//")
        # Parameterized Explain under DELIMITER // keeps the unit delimiter too.
        mssql = bound_session("mariadb", "11.4.10-MariaDB", "MSSQL")
        plan = ed._plan_run({"sql": "DELIMITER //\nSELECT [a;b] FROM t WHERE id = :id//",
                             "explain_format": "table", "parameters": {"id": 7}}, mssql)
        self.assertEqual(plan["units"][0]["send"], "EXPLAIN SELECT [a;b] FROM t WHERE id = 7")
        # Multiple server statements under DELIMITER // must still refuse.
        with self.assertRaisesRegex(ed.EditorError, "Explain one statement at a time"):
            ed._plan_run({"sql": "DELIMITER //\nSELECT 'a;b'; SELECT 2//", "explain_format": "table"}, mssql)

    def test_explain_parameters_cannot_complete_custom_delimiter(self):
        # Finding 3: filled values completing a custom delimiter must refuse.
        cases = (
            'DELIMITER "1 FOR UPDATE"\nSELECT * FROM t WHERE id = :x FOR UPDATE1 FOR UPDATE',
            'DELIMITER 1+SLEEP(5)\nSELECT :x+SLEEP(5)1+SLEEP(5)',
            'DELIMITER 1,@a:=2\nSELECT :x,@a:=21,@a:=2',
            'DELIMITER 1,NEXTVAL(s)\nSELECT :x,NEXTVAL(s)1,NEXTVAL(s)',
        )
        for engine, version in (("mysql", "8.4.0"), ("mariadb", "11.4.10-MariaDB")):
            session = bound_session(engine, version, "")
            for sql in cases:
                with self.subTest(engine=engine, sql=sql):
                    with self.assertRaisesRegex(ed.EditorError, "Explain one statement at a time"):
                        ed._plan_run({"sql": sql, "explain_format": "table", "parameters": {"x": 1}}, session)

    def test_explain_prefix_cannot_collide_with_custom_delimiter(self):
        # Finding 4: prepending EXPLAIN / ANALYZE must keep one unchanged unit.
        cases = (
            'DELIMITER "N SELECT @a:=1"\nSELECT @a:=1N SELECT @a:=1',
            'DELIMITER "N SELECT f()"\nSELECT f()N SELECT f()',
            'DELIMITER "N /* */"\n/* */ SELECT 1N /* */',
        )
        for engine, version in (("mysql", "8.4.0"), ("mariadb", "11.4.10-MariaDB")):
            session = bound_session(engine, version, "")
            for sql in cases:
                with self.subTest(engine=engine, sql=sql):
                    with self.assertRaisesRegex(ed.EditorError, "Explain one statement at a time"):
                        ed._plan_run({"sql": sql, "explain_format": "json"}, session)


@unittest.skipIf(os.name == "nt", "the fake client is a POSIX script")
class ExplainP1ProcessTests(unittest.TestCase):
    engine = "mysql"
    setUp = fixtures.ProcessTests.setUp
    call = fixtures.ProcessTests.call
    run_sql = fixtures.ProcessTests.run_sql
    session_log = fixtures.ProcessTests.session_log
    packets = identity_fixtures.EditorIdentityTests.packets

    def test_explain_sends_the_status_helper_before_the_exact_statement(self):
        mode = "ANSI_QUOTES" if self.engine == "mariadb" else ""
        self.assertEqual(self.run_sql("SET SESSION sql_mode = '%s'" % mode)["status"], "complete")
        session = self.registry.get("tab1")
        session.client_engine = self.engine
        sql = ANSI_SQL if self.engine == "mariadb" else "SELECT 'it\\'s'"
        for fmt in ("table", "json"):
            with self.subTest(fmt=fmt):
                before = len(self.packets())
                result = self.call("/api/editor/run", sql=sql, explain_format=fmt)
                self.assertEqual(result["status"], "complete", result)
                prefix = ("EXPLAIN " if self.engine == "mariadb" and fmt == "table" else
                          "EXPLAIN FORMAT=%s " % ("TRADITIONAL" if fmt == "table" else "JSON"))
                self.assertEqual(result["run"]["statements"][0]["sent"], prefix + sql)
                packets = [packet[2] for packet in self.packets()[before:]]
                at = next(i for i, packet in enumerate(packets)
                          if packet.endswith(session.statement_tag + prefix + sql))
                self.assertGreater(at, 1)
                probe = ed.TAG + dbops.client_status_probe(
                    "$$", True, ansi_quotes=True if self.engine == "mariadb" else None,
                    client_engine=self.engine)[0]
                self.assertTrue(packets[at - 2].endswith(SYNC), packets)
                self.assertTrue(packets[at - 1].endswith(probe), packets)
                self.assertNotIn(sql, packets[at - 2])
                self.assertNotIn(sql, packets[at - 1])

    def test_explain_parameters_with_double_quotes_and_status_helper(self):
        self.run_sql("CREATE TEMPORARY TABLE tt (id INT, col TEXT)")
        prefix = "EXPLAIN " if self.engine == "mariadb" else "EXPLAIN FORMAT=TRADITIONAL "
        for mode, sql, expected, needs_sync in (
                ("", 'SELECT * FROM tt WHERE col = "alice" AND id = :id',
                 'SELECT * FROM tt WHERE col = "alice" AND id = 3', False),
                ("ANSI_QUOTES", 'SELECT "col" FROM tt WHERE id = :id',
                 'SELECT "col" FROM tt WHERE id = 3', False),
                ("", "SELECT 'it\\'s', :id FROM tt", "SELECT 'it\\'s', 3 FROM tt", True),
                ("NO_BACKSLASH_ESCAPES", "SELECT 'a\\', ':id', :id FROM tt",
                 "SELECT 'a\\', ':id', 3 FROM tt", True),
        ):
            with self.subTest(mode=mode, sql=sql):
                self.assertEqual(self.run_sql("SET SESSION sql_mode = '%s'" % mode)["status"], "complete")
                session = self.registry.get("tab1")
                session.client_engine = self.engine
                before = len(self.packets())
                result = self.call("/api/editor/run", sql=sql, explain_format="table", parameters={"id": 3})
                self.assertEqual(result["status"], "complete", result)
                self.assertEqual(result["run"]["statements"][0]["sent"], prefix + expected)
                packets = [packet[2] for packet in self.packets()[before:]]
                at = next(i for i, packet in enumerate(packets)
                          if packet.endswith(session.statement_tag + prefix + expected))
                if needs_sync:
                    # Sync helper, then the quote-status probe, then the plan.
                    self.assertGreater(at, 1)
                    self.assertTrue(packets[at - 2].endswith(SYNC), packets)
                    self.assertIn("AS mf_probe", packets[at - 1], packets)

    def test_explain_and_run_plan_accept_semicolon_inside_custom_delimiter_unit(self):
        cases = [("", "DELIMITER //\nSELECT 'a;b'//", "SELECT 'a;b'"),
                 ("", "DELIMITER //\nSELECT /*!999999 ; */ 1//", "SELECT /*!999999 ; */ 1")]
        if self.engine == "mariadb":
            cases.append(("MSSQL", "DELIMITER //\nSELECT 1 AS [a;b]//", "SELECT 1 AS [a;b]"))
        for mode, sql, expected in cases:
            with self.subTest(mode=mode, sql=sql):
                self.assertEqual(self.run_sql("SET SESSION sql_mode = '%s'" % mode)["status"], "complete")
                session = self.registry.get("tab1")
                session.client_engine = self.engine
                result = self.call("/api/editor/run", sql=sql, explain_format="table")
                self.assertEqual(result["status"], "complete", result)
                prefix = "EXPLAIN " if self.engine == "mariadb" else "EXPLAIN FORMAT=TRADITIONAL "
                self.assertEqual(result["run"]["statements"][0]["sent"], prefix + expected)
                for plan_mode in ("explain", "analyze"):
                    self.assertIsInstance(json.loads(session.run_plan(sql, plan_mode)), dict)
        session = self.registry.get("tab1")
        with self.assertRaisesRegex(ed.EditorError, "exactly one SQL statement"):
            session.run_plan("DELIMITER //\nSELECT 'a;b'; SELECT 2//", "explain")
        for bad_sql in (
            'DELIMITER "N SELECT @a:=1"\nSELECT @a:=1N SELECT @a:=1',
            'DELIMITER "N SELECT f()"\nSELECT f()N SELECT f()',
            'DELIMITER "N /* */"\n/* */ SELECT 1N /* */',
        ):
            for plan_mode in ("explain", "analyze"):
                with self.subTest(bad_sql=bad_sql, plan_mode=plan_mode):
                    before = len(self.packets())
                    with self.assertRaisesRegex(ed.EditorError, "exactly one SQL statement"):
                        session.run_plan(bad_sql, plan_mode)
                    self.assertEqual(len(self.packets()), before)

    def check_cancel_during_sync(self, by):
        self.call("/api/editor/open")
        session = self.registry.get("tab1")
        session.client_engine = self.engine
        sql = "SELECT 'it\\'s'"
        for mode in ("explain", "analyze"):
            with self.subTest(mode=mode, by=by):
                before = len(self.packets())
                helper, stop = session._helper, session.stop
                cancelled = threading.Event()
                runs = []

                def record_stop(*args, **kwargs):
                    result = stop(*args, **kwargs)
                    cancelled.set()
                    return result

                def cancel_after_reply(send, *args, **kwargs):
                    step = helper(send, *args, **kwargs)
                    if send == SYNC:
                        runs.append(session.run)
                        if by == "user":
                            stopped = session.stop()
                            self.assertTrue(stopped["stop_requested"])
                            self.assertFalse(stopped["stopped"], "Stop must not KILL a helper")
                        else:
                            # The real plan Timer calls Stop while this helper
                            # has not returned to _send. No user packet exists.
                            self.assertTrue(cancelled.wait(5), "the plan timeout did not request Stop")
                    return step

                with mock.patch.object(session, "_helper", side_effect=cancel_after_reply), \
                        mock.patch.object(session, "stop", side_effect=record_stop):
                    with self.assertRaises(ed.EditorError) as caught:
                        session.run_plan(sql, mode, timeout=0.1 if by == "timeout" else None)
                self.assertEqual(caught.exception.code, "stopped")
                self.assertEqual(str(caught.exception), "Plan cancelled before it started.")
                self.assertEqual(len(runs), 1)
                self.assertEqual(runs[0].stopped_by, by)
                self.assertTrue(runs[0].done.is_set())
                self.assertIsNone(runs[0].options)
                self.assertFalse(session.busy())
                self.assertFalse(session.run_lock.locked())
                self.assertFalse(session.ended)
                packets = [packet[2] for packet in self.packets()[before:]]
                self.assertTrue(any(packet.endswith(SYNC) for packet in packets))
                self.assertFalse(any(sql in packet for packet in packets), packets)
                self.assertFalse((self.state / "kills.log").exists())
                if self.engine == "mysql":
                    self.assertEqual(self.session_log()[-2:], [ed.TAG + "ROLLBACK",
                                                              ed.TAG + "SET SESSION explain_json_format_version=1"])
                # Cancellation releases the tab and restores its plan settings.
                self.assertIsInstance(json.loads(session.run_plan(sql, mode)), dict)

    def test_stop_during_the_status_helper_cancels_the_plan_and_releases_the_tab(self):
        self.check_cancel_during_sync("user")

    def test_timeout_during_the_status_helper_cancels_the_plan_and_releases_the_tab(self):
        self.check_cancel_during_sync("timeout")


class MariaDBExplainP1ProcessTests(ExplainP1ProcessTests):
    engine = "mariadb"
