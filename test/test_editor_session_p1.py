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
                    self.assertEqual(ed._review_statements(run["units"]), [SYNC, ANSI_SQL])
                    for fmt, prefix in (("table", "EXPLAIN "), ("json", "EXPLAIN FORMAT=JSON ")):
                        with self.subTest(version=version, mode=mode, scope=scope, fmt=fmt):
                            plan = ed._plan_run(dict(scope, sql=ANSI_SQL, explain_format=fmt), session)
                            self.assertEqual(len(plan["units"]), 1)
                            self.assertEqual(ed._review_statements(plan["units"]), [SYNC, prefix + ANSI_SQL])
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
                self.assertGreater(at, 0)
                self.assertTrue(packets[at - 1].endswith(SYNC), packets)
                self.assertNotIn(sql, packets[at - 1])

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
