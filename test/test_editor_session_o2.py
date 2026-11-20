"""O2: independent client NBE status and bounded event-mode provenance.

The production splitter, classifier, planner and policy remain intact. Only
the process test's database replies come from the existing fake client.
"""
import os
import unittest

import test_editor_session as fixtures
import test_editor_session_identity as identity_fixtures
from test_editor_session_j2 import bound_session
from myflames import dbops, editor_session as ed


SERVERS = (("mysql", "8.4.0"), ("mariadb", "11.8.9-MariaDB"))
NBE_ATTACK = "SELECT 'x\\', ' \\C utf8mb4 ' AS a\n, '; SELECT 2; -- ' AS b"


class EditorPlanningO2Tests(unittest.TestCase):
    def plan(self, sql, engine, version, mode="", client=None):
        session = bound_session(engine, version, mode)
        session.client_engine = client
        return ed._plan_run({"sql": sql}, session)

    def test_client_nbe_status_is_independent_of_server_quote_mode(self):
        for engine, version in SERVERS:
            for client in ("mysql", "mariadb"):
                for mode, expected in (("NO_BACKSLASH_ESCAPES", {(False, False), (False, True)}),
                                       (None, {(False, False), (False, True), (True, False), (True, True)})):
                    with self.subTest(engine=engine, client=client, mode=mode):
                        readings = dbops.lexer_readings(None, engine, mode, version, client, NBE_ATTACK)
                        self.assertEqual({(r["backslash_escapes"], r.get("client_backslash_escapes"))
                                          for r in readings}, expected)

    def test_bound_nbe_attack_requires_status_helper_for_both_client_families(self):
        for engine, version in SERVERS:
            for client in ("mysql", "mariadb", None):
                with self.subTest(engine=engine, client=client):
                    plan = self.plan(NBE_ATTACK, engine, version, "NO_BACKSLASH_ESCAPES", client)
                    probe = dbops.client_status_probe("$$", False)[0]
                    self.assertEqual(ed._review_statements(plan["units"]),
                                     [ed.TAG + dbops.CLIENT_STATUS_SYNC, ed.TAG + probe, NBE_ATTACK])
                    args = dict(engine=engine, server_version=version,
                                sql_mode="NO_BACKSLASH_ESCAPES", client_engine=client)
                    self.assertNotEqual(dbops.classify(NBE_ATTACK, **args)["kind"], "read")
                    with self.assertRaises(ValueError):
                        dbops.target_statement(NBE_ATTACK, **args)

    def test_nbe_scripts_with_agreeing_client_boundaries_still_plan(self):
        for engine, version in SERVERS:
            for client in ("mysql", "mariadb", None):
                for sql in ("SELECT 'it''s', '2026-11-19'; SELECT 2", r"SELECT 'C:\logs'; SELECT 2"):
                    with self.subTest(engine=engine, client=client, sql=sql):
                        plan = self.plan(sql, engine, version, "NO_BACKSLASH_ESCAPES", client)
                        self.assertEqual(len(plan["units"]), 2)
                        self.assertEqual(plan["verdict"]["kind"], "read")
                        self.assertTrue(all(u["sql_mode"] == "NO_BACKSLASH_ESCAPES" for u in plan["units"]))

    def test_literal_event_schedules_preserve_saves_but_do_bodies_do_not_execute(self):
        schedules = ("AT '2026-11-19 12:52:56'", "AT CURRENT_TIMESTAMP",
                     "AT NOW() + INTERVAL 1 DAY", "AT (CURRENT_TIMESTAMP(6) + INTERVAL (1+2) HOUR)",
                     "AT TIMESTAMP '2026-11-19 12:52:56' - INTERVAL '1:02' HOUR_MINUTE",
                     "EVERY 1 DAY", "EVERY (1+2) HOUR STARTS NOW() ENDS NOW() + INTERVAL 1 DAY",
                     "EVERY '1-2' YEAR_MONTH STARTS '2026-11-19 12:52:56'",
                     "EVERY 1e+1 SECOND STARTS CURRENT_TIMESTAMP + INTERVAL -1 DAY")
        for engine, version in SERVERS:
            for schedule in schedules:
                for options in ("", " ON COMPLETION NOT PRESERVE ENABLE COMMENT 'f(@v:=1)'"):
                    sql = ("SET @v=@@sql_mode; CREATE DEFINER=`u`@`h` EVENT IF NOT EXISTS `do` ON SCHEDULE " +
                           schedule + options + " DO SET @v='ORACLE'; SET sql_mode=@v; SELECT 1")
                    with self.subTest(engine=engine, schedule=schedule, options=options):
                        unit = self.plan(sql, engine, version, client=engine)["units"][-1]
                        self.assertEqual(unit["sql_mode"], "")
                        self.assertFalse(unit["mode_unknown"])

    def test_event_schedule_expressions_outside_the_literal_subset_end_saves(self):
        schedules = ("AT CURRENT_TIMESTAMP + INTERVAL (@v := 1) HOUR",
                     "AT @v", "AT @@timestamp", "AT f()", "AT db.f()", "AT `NOW`()",
                     "AT NOW(@v:=1)", "AT CURRENT_TIMESTAMP + INTERVAL f() HOUR",
                     "AT (SELECT c FROM v)", "AT t.c", "AT `CURRENT_TIMESTAMP`",
                     "EVERY @v DAY", "EVERY 1 DAY STARTS f()", "EVERY 1 DAY ENDS (@v:=1)")
        for engine, version in SERVERS:
            for schedule in schedules:
                sql = ("SET @v=@@sql_mode; CREATE EVENT e ON SCHEDULE " + schedule +
                       " DO SELECT 1; SET sql_mode=@v; SELECT 1")
                with self.subTest(engine=engine, schedule=schedule):
                    unit = self.plan(sql, engine, version, client=engine)["units"][-1]
                    self.assertIsNone(unit["sql_mode"])
                    self.assertTrue(unit["mode_unknown"])

    def test_event_exemption_requires_one_standalone_definition(self):
        for engine, version in SERVERS:
            sql = ("SET @v=@@sql_mode;\nDELIMITER //\n"
                   "CREATE EVENT e ON SCHEDULE EVERY 1 DAY DO SELECT 1; SELECT f()//\n"
                   "DELIMITER ;\nSET sql_mode=@v; SELECT 1")
            unit = self.plan(sql, engine, version, client=engine)["units"][-1]
            self.assertIsNone(unit["sql_mode"])


@unittest.skipIf(os.name == "nt", "the fake client is a POSIX script")
class EditorNbeO2ProcessTests(unittest.TestCase):
    engine = "mysql"
    setUp = fixtures.ProcessTests.setUp
    call = fixtures.ProcessTests.call
    run_sql = fixtures.ProcessTests.run_sql
    session_log = fixtures.ProcessTests.session_log
    packets = identity_fixtures.EditorIdentityTests.packets

    def test_nbe_status_attack_follows_separate_helper_packet(self):
        self.assertEqual(self.run_sql("SET SESSION sql_mode='NO_BACKSLASH_ESCAPES'")["status"], "complete")
        session = self.registry.get("tab1")
        session.sql_mode = "NO_BACKSLASH_ESCAPES"
        for client in ("mysql", "mariadb"):
            with self.subTest(client=client):
                session.client_engine = client
                before = len(self.packets())
                result = self.call("/api/editor/run", sql=NBE_ATTACK)
                self.assertEqual(result["status"], "complete", result)
                packets = [p[2] for p in self.packets()[before:]]
                at = next(i for i, p in enumerate(packets) if p.endswith(session.statement_tag + NBE_ATTACK))
                self.assertTrue(packets[at - 2].endswith(ed.TAG + dbops.CLIENT_STATUS_SYNC))
                self.assertTrue(packets[at - 1].endswith(ed.TAG + dbops.client_status_probe("$$", False)[0]))
