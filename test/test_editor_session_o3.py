"""O3: stale client status bits, independently of the bound server mode.

The process fixture has separate server-mode and client-status state. Its
status gate rejects the affected bytes unless the preceding helper's reply
has refreshed the bits. No classifier, policy or production guard is mocked.
"""
import json
import os
from pathlib import Path
import re
import unittest
from unittest import mock

from client_addline_oracle import client_packets
import test_editor_session as fixtures
import test_editor_session_identity as identity_fixtures
from test_editor_session_j2 import SAFETY, bound_session
from test_reviewed_writes_packets import RoutingTransport
from test_reviewed_writes_guard import MYSQL, MARIA
from myflames import accounts, dbops, editor_session as ed


SERVERS = (("mysql", "5.7.44"), ("mysql", "8.0.46"), ("mysql", "8.4.0"), ("mysql", "9.7.0"),
           ("mariadb", "10.11.16-MariaDB"), ("mariadb", "11.8.9-MariaDB"))
# Reviewer O-S1: comment-space insertion, a dropped DELIMITER newline and
# the enabled \C command/hidden SELECT. The lab case covers the reverse bit.
REVIEWER_TEXTS = (
    ("", "SELECT 'a\\' /*c*/x ' AS s -- '"),
    ("NO_BACKSLASH_ESCAPES", "SELECT 'a\\', '\ndelimiter_x\n' AS s -- '"),
    ("NO_BACKSLASH_ESCAPES", "SELECT 'x\\', ' \\C utf8mb4 ' AS a\n, '; SELECT 2; -- ' AS b"),
)
LAB_TEXT = "SELECT 'a\\'; SELECT 'SPLIT-AFTER' AS marker; -- ' AS probe"
GA_STATUS_PROBE = "SELECT 'x\\', \\C utf8mb4 ' AS a\n, 'y' AS b"
ANSI_TEXT = 'SELECT "a\\" /*c*/x " AS s -- "'
ANSI_ON_TEXT = 'SELECT 1 AS "a\\", 2 AS "\ndelimiter_x\n" -- "'
SYNC = "/*myflames:editor*/ SET SESSION sql_mode = @@SESSION.sql_mode"


class EditorStatusO3Tests(unittest.TestCase):
    def plan(self, sql, engine="mysql", version="8.4.0", mode="", client="mysql", **selection):
        session = bound_session(engine, version, mode)
        session.client_engine = client
        plan = ed._plan_run(dict(selection, sql=sql), session)
        return plan, session

    def test_each_status_bit_is_independent_of_each_server_mode(self):
        for engine, version in SERVERS:
            for mode in ("", "NO_BACKSLASH_ESCAPES", "ANSI_QUOTES", "ANSI_QUOTES,NO_BACKSLASH_ESCAPES", None):
                for client in ("mysql", "mariadb", None):
                    with self.subTest(engine=engine, version=version, mode=mode, client=client):
                        readings = dbops.lexer_readings(None, engine, mode, version, client)
                        expected = {(family, escape, ansi)
                                    for family in ((client,) if client else ("mysql", "mariadb"))
                                    for escape in (False, True)
                                    for ansi in ((False, True) if family == "mariadb" else (False,))}
                        self.assertEqual({(r["client_engine"], r["client_backslash_escapes"],
                                           r["client_ansi_quotes"]) for r in readings}, expected)

    def test_reviewer_texts_and_lab_read_name_helper_without_changing_bytes(self):
        for engine, version in SERVERS:
            for client in ("mysql", "mariadb", None):
                for mode, sql in REVIEWER_TEXTS + (("", LAB_TEXT),):
                    for scope in ({}, {"cursor": 0}, {"selection": {"start": 0, "end": len(sql)}}):
                        with self.subTest(engine=engine, version=version, client=client, mode=mode,
                                          sql=sql, scope=scope):
                            plan, session = self.plan(sql, engine, version, mode, client, **scope)
                            self.assertEqual(len(plan["units"]), 1)
                            self.assertEqual(plan["units"][0]["send"], sql)
                            self.assertTrue(plan["units"][0]["client_status_sync"])
                            probe = ed.TAG + dbops.client_status_probe(
                                "$$", "NO_BACKSLASH_ESCAPES" not in mode,
                                ansi_quotes=False if ("NO_BACKSLASH_ESCAPES" not in mode and client != "mysql") else None,
                                client_engine=client)[0]
                            self.assertEqual(ed._review_statements(plan["units"]), [SYNC, probe, sql])
                            self.assertEqual(plan["verdict"]["kind"], "read")
                            self.assertEqual(ed._decision(plan, SAFETY, session)["action"], "allow")
                            self.assertEqual(ed._decision(plan, dict(SAFETY, read_only=True), session)["action"], "allow")

    def test_mariadb_ansi_bit_can_be_stale_when_server_ansi_mode_is_off(self):
        for client in ("mariadb", None):
            plan, unused = self.plan(ANSI_TEXT, "mariadb", "11.8.9-MariaDB", "", client)
            probe_off = ed.TAG + dbops.client_status_probe("$$", True, ansi_quotes=False, client_engine=client)[0]
            self.assertEqual(ed._review_statements(plan["units"]), [SYNC, probe_off, ANSI_TEXT])
            self.assertEqual(plan["verdict"]["kind"], "read")
        plan, unused = self.plan(ANSI_ON_TEXT, "mariadb", "11.8.9-MariaDB", "ANSI_QUOTES", "mariadb")
        probe_on = ed.TAG + dbops.client_status_probe("$$", True, ansi_quotes=True, client_engine="mariadb")[0]
        self.assertEqual(ed._review_statements(plan["units"]), [SYNC, probe_on, ANSI_ON_TEXT])
        # MariaDB's client on a MySQL server receives no ANSI bit, even when
        # MySQL's sql_mode uses ANSI_QUOTES. Synchronization must not invent it.
        readings = dbops.lexer_readings(None, "mysql", "ANSI_QUOTES", "8.4.0", "mariadb",
                                        client_status_synced=True)
        self.assertEqual({r["client_ansi_quotes"] for r in readings}, {False})

    def test_unbound_or_unknown_mode_refuses_including_cursor_and_selection(self):
        for mode, sql in REVIEWER_TEXTS + (("", ANSI_TEXT), ("", LAB_TEXT)):
            for failure in ("routed", "identity", "mode"):
                session = bound_session("mariadb", "11.8.9-MariaDB", mode)
                if failure == "routed":
                    session._routed_startup = True
                elif failure == "identity":
                    session.server_identity = {}
                else:
                    session.sql_mode = None
                for scope in ({}, {"cursor": 0}, {"selection": {"start": 0, "end": len(sql)}}):
                    with self.subTest(mode=mode, sql=sql, failure=failure, scope=scope):
                        with self.assertRaises(ed.EditorError):
                            ed._plan_run(dict(scope, sql=sql), session)

    def test_ordinary_diagnostic_units_never_get_a_helper(self):
        sql = ("SELECT 'C:\\logs'; SELECT ROW_COUNT(); SELECT FOUND_ROWS(); SHOW WARNINGS; "
               "GET DIAGNOSTICS @n = NUMBER; SELECT 'it''s', \"ordinary\"")
        for engine, version in SERVERS:
            for client in ("mysql", "mariadb", None):
                for mode in ("", "NO_BACKSLASH_ESCAPES"):
                    with self.subTest(engine=engine, client=client, mode=mode):
                        plan, unused = self.plan(sql, engine, version, mode, client)
                        self.assertFalse(any(u["client_status_sync"] for u in plan["units"]))
                        self.assertNotIn(SYNC, ed._review_statements(plan["units"]))

    def test_status_helper_is_per_affected_unit_and_cannot_fix_other_ambiguity(self):
        plan, unused = self.plan("SELECT 1; " + LAB_TEXT + "\n; SELECT ROW_COUNT()")
        self.assertEqual([u["client_status_sync"] for u in plan["units"]], [False, True, False])
        probe = ed.TAG + dbops.client_status_probe("$$", True)[0]
        self.assertEqual(ed._review_statements(plan["units"]),
                         ["SELECT 1", SYNC, probe, LAB_TEXT, "SELECT ROW_COUNT()"])
        # MySQL's client ignores ANSI_QUOTES: SET cannot teach it identifier
        # quoting. Unknown family must still check that client reading.
        sql = 'SELECT 1 AS "a\\", 2 AS "b"'
        for client in ("mysql", None):
            with self.subTest(client=client):
                with self.assertRaises(ed.EditorError):
                    self.plan(sql, "mariadb", "11.8.9-MariaDB", "ANSI_QUOTES", client)

    def test_guarded_builder_emits_separate_helper_and_preserves_original_unit(self):
        guard = dbops.target_session_guard("mysql", MYSQL)
        for mode, sql in REVIEWER_TEXTS + (("", LAB_TEXT),):
            with self.subTest(mode=mode, sql=sql):
                script, lines = dbops.target_guarded_script(
                    guard, [sql], sql_mode=mode, engine="mysql", server_version="8.4.0", client_engine="mysql")
                self.assertEqual(len(lines), 2)
                self.assertEqual(lines.sizes, (0, 1), "helper must not count as an applied user statement")
                self.assertEqual(lines.indices, (0, 0))
                delimiter, body = script.split("\n", 1)
                packets = body.split("\n" + delimiter[len("DELIMITER "):] + "\n")
                self.assertTrue(packets[0].endswith(dbops.CLIENT_STATUS_SYNC))
                self.assertTrue(packets[1].endswith(sql))
                self.assertIn("FIND_IN_SET('NO_BACKSLASH_ESCAPES'", packets[0])
                self.assertIn("FIND_IN_SET('NO_BACKSLASH_ESCAPES'", packets[1])
                for partial in ({}, {"backslash_escapes": True}, {"backslash_escapes": False}):
                    with self.assertRaisesRegex(ValueError, "known sql_mode"):
                        dbops.target_guarded_script(guard, [sql], **partial)
        script, lines = dbops.target_guarded_script(guard, ["SELECT 1", "SHOW WARNINGS"], sql_mode="")
        self.assertEqual(len(lines), 2)
        self.assertNotIn(dbops.CLIENT_STATUS_SYNC, script)

    def test_password_wrapper_reviews_status_helper_before_password_bytes(self):
        password = "slash\\'; DROP USER victim; -- "
        account = "ALTER USER 'app'@'%' IDENTIFIED BY " + accounts._password(password)
        for engine, identity in (("mysql", MYSQL), ("mariadb", MARIA)):
            with self.subTest(engine=engine):
                # Opposite status at login. Only the staged SET's reply is
                # allowed to fix it; the password remains byte-for-byte SQL.
                transport = RoutingTransport([identity, identity])
                for session in transport.sessions:
                    session["mode"] = "NO_BACKSLASH_ESCAPES"
                transport.client_escaping = True
                transport.run_stdin = transport.run_write
                guard = dbops.target_session_guard(engine, identity)
                reviewed = accounts._with_password_mode(account)
                probe_sql, _ = dbops.client_status_probe("$$", False)
                self.assertIn("SET SESSION sql_mode = @@SESSION.sql_mode;\n" + probe_sql + ";\n", reviewed)
                for tampered in (
                    reviewed.replace("SET SESSION sql_mode = @@SESSION.sql_mode;\n", ""),
                    reviewed.replace(probe_sql + ";\n", ""),
                    reviewed.replace(probe_sql, "SELECT 1"),
                ):
                    with self.assertRaisesRegex(accounts.ConnectorError, "quote-status helper changed"):
                        accounts._run_stdin(transport, guard[0] + "\n" + tampered, [password])
                    self.assertEqual(transport.packets, [], "a removed or changed reviewed helper must refuse before sending")
                accounts._run_stdin(transport, guard[0] + "\n" + reviewed, [password])
                self.assertEqual(len(transport.packets), 3)
                self.assertNotIn(account, transport.packets[0][1])
                self.assertIn("SET SESSION sql_mode = CONCAT_WS", transport.packets[0][1])
                self.assertTrue(transport.packets[0][1].endswith("SET SESSION sql_mode = @@SESSION.sql_mode"))
                self.assertTrue(transport.packets[1][1].endswith(probe_sql))
                self.assertNotIn("SET SESSION sql_mode = @@SESSION.sql_mode", transport.packets[2][1])
                self.assertIn(accounts.TAG + account, [sql for unused, sql in transport.executed])
                self.assertFalse(any(sql.startswith("DROP USER") for unused, sql in transport.executed))
        ordinary = accounts._with_password_mode("ALTER USER 'app'@'%' IDENTIFIED BY 'ordinary'")
        self.assertNotIn("SET SESSION sql_mode = @@SESSION.sql_mode", ordinary)
        self.assertNotIn("mf_probe", ordinary)

    def test_ga_probe_framing_differs_without_helper_under_stale_nbe_bit(self):
        # The previous GA probe hid its ';' inside a '-- ' line comment under
        # target_guarded_script's DELIMITER $$, so a stale NBE client sent the
        # same bytes either way. \C outside the string under stale NBE removes
        # the rest of the line in every client even inside DELIMITER $$.
        old_probe = "SELECT 'a\\'; SELECT 1 AS hidden; -- ' AS probe"
        versions = (("mysql", ("5.7.44", "8.0.46", "8.1.0", "8.4.0", "9.7.0")),
                    ("mariadb", ("10.11.16", "11.8.9", "12.0.1")))
        for engine, server_version, guard in (("mysql", "8.4.0", dbops.target_session_guard("mysql", MYSQL)),
                                              ("mariadb", "11.8.9-MariaDB", dbops.target_session_guard("mariadb", MARIA))):
            for family, client_versions in versions:
                plan, unused = self.plan(GA_STATUS_PROBE, engine, server_version, "", family)
                # Sync helper, then the quote-status probe, then the unit.
                review = ed._review_statements(plan["units"])
                self.assertEqual(len(review), 3, review)
                self.assertEqual((review[0], review[2]), (SYNC, GA_STATUS_PROBE))
                self.assertTrue(review[1].startswith(ed.TAG + "SELECT '\\'") and "AS mf_probe" in review[1], review)
                self.assertTrue(plan["units"][0]["client_status_sync"])
                probe_script, unused = dbops.target_guarded_script(
                    guard, [GA_STATUS_PROBE], sql_mode="", engine=engine, server_version=server_version,
                    client_engine=family, defer_status_sync=True)
                old_script, unused = dbops.target_guarded_script(
                    guard, [old_probe], sql_mode="", engine=engine, server_version=server_version,
                    client_engine=family, defer_status_sync=True)
                for client_version in client_versions:
                    with self.subTest(engine=engine, family=family, client_version=client_version):
                        restored = client_packets(probe_script, family, delimiter=";",
                                                  client_version=client_version, append_delimiter=False,
                                                  backslash_escapes=True)
                        stale = client_packets(probe_script, family, delimiter=";",
                                               client_version=client_version, append_delimiter=False,
                                               backslash_escapes=False)
                        self.assertEqual(restored[1:], ("", ["DELIMITER"]))
                        self.assertEqual(len(restored[0]), 1)
                        self.assertTrue(restored[0][0].endswith(GA_STATUS_PROBE))
                        self.assertEqual(stale[1:], ("", ["DELIMITER", "C"]))
                        self.assertEqual(len(stale[0]), 1)
                        self.assertNotEqual(stale[0], restored[0])
                        self.assertTrue(stale[0][0].endswith("SELECT 'x\\', \n, 'y' AS b"))
                        # Contrast with the old probe, which did not differ under DELIMITER $$.
                        self.assertEqual(
                            client_packets(old_script, family, delimiter=";", client_version=client_version,
                                           append_delimiter=False, backslash_escapes=False),
                            client_packets(old_script, family, delimiter=";", client_version=client_version,
                                           append_delimiter=False, backslash_escapes=True))


# Synthetic replies only. sql_mode and the reply bits are deliberately separate,
# as they are after sp_head restores its mode. An affected unit with stale bits
# produces an extra result, so missing the helper cannot accidentally pass.
STATUS_FAKE = r'''
        if upper.startswith("SELECT O3_STALE_"):
            mode = ("NO_BACKSLASH_ESCAPES" if "NBE" in upper else "ANSI_QUOTES" if "ANSI_ON" in upper else "")
            self.sql_mode = mode
            self.o3_client_nbe = False if "ANSI" in upper else "NO_BACKSLASH_ESCAPES" not in mode
            self.o3_client_ansi = ENGINE == "mariadb" and "ANSI_ON" not in upper
            self.o3_syncs = 0
            self.result(statement, ["stale"], [["1"]])
            return
        if upper == "SET SESSION SQL_MODE = @@SESSION.SQL_MODE":
            if os.path.exists(os.path.join(STATE, "sync-fails")):
                self.error(line, 1231, "42000", "synthetic status sync failure")
                return
            if not os.path.exists(os.path.join(STATE, "stale-after-sync")):
                self.o3_client_nbe = "NO_BACKSLASH_ESCAPES" in self.sql_mode
                self.o3_client_ansi = ENGINE == "mariadb" and "ANSI_QUOTES" in self.sql_mode
            self.o3_syncs = getattr(self, "o3_syncs", 0) + 1
            self.ok()
            return
        if "AS mf_probe" in text and os.path.exists(os.path.join(STATE, "wrong-probe")):
            self.result(statement, ["mf_probe"], [["wrong"]])
            return
        if getattr(self, "o3_client_nbe", None) is not None and text.startswith("SELECT ") and "\\" in text and "AS mf_probe" not in text:
            matched = (self.o3_client_nbe == ("NO_BACKSLASH_ESCAPES" in self.sql_mode) and
                       self.o3_client_ansi == (ENGINE == "mariadb" and "ANSI_QUOTES" in self.sql_mode))
            with open(os.path.join(STATE, "status-wire.log"), "a") as handle:
                handle.write(json.dumps([self.sql_mode, self.o3_client_nbe, self.o3_client_ansi,
                                        self.o3_syncs, text]) + "\n")
            self.result(statement, ["reviewed"], [["single"]])
            if not matched:
                self.result("SELECT 'hidden'", ["hidden"], [["not reviewed"]])
            return
'''


@unittest.skipIf(os.name == "nt", "the fake client is a POSIX script")
class EditorStatusO3ProcessTests(unittest.TestCase):
    engine = "mysql"
    call = fixtures.ProcessTests.call
    run_sql = fixtures.ProcessTests.run_sql
    rows = fixtures.ProcessTests.rows
    packets = identity_fixtures.EditorIdentityTests.packets
    session_log = fixtures.ProcessTests.session_log

    def setUp(self):
        fixtures.ProcessTests.setUp(self)
        script = Path(os.environ["PATH"].split(os.pathsep)[0]) / "mysql"
        source = script.read_text(encoding="utf-8")
        probe_split = (
            '        if "AS mf_probe" in wire and getattr(self, "o3_client_nbe", None) is not None:\n'
            '            nbe_srv = "NO_BACKSLASH_ESCAPES" in self.route_sql_mode.upper().split(",")\n'
            '            ansi_srv = ENGINE == "mariadb" and "ANSI_QUOTES" in self.route_sql_mode.upper().split(",")\n'
            '            if self.o3_client_nbe != nbe_srv or (\'1 AS "\' in wire and self.o3_client_ansi != ansi_srv):\n'
            '                self.error(line, 1064, "42000", "You have an error in your SQL syntax; stale quote probe split")\n'
            '                return\n'
        )
        source = source.replace(
            '        statement = re.sub(r"/\\*myflames:editor:[0-9a-f]{32}\\*/ ", "", wire)\n',
            '        statement = re.sub(r"/\\*myflames:editor:[0-9a-f]{32}\\*/ ", "", wire)\n' + probe_split)
        source = source.replace('        upper = text.upper()\n', '        upper = text.upper()\n' + STATUS_FAKE)
        script.write_text(source, encoding="utf-8")

    def stale(self, mode="", ansi=False):
        label = ("ANSI_ON" if mode == "ANSI_QUOTES" else "ANSI_OFF" if ansi else
                 "NBE" if mode else "EMPTY")
        result = self.run_sql("SELECT O3_STALE_%s()" % label)
        self.assertEqual(result["status"], "complete", result)
        session = self.registry.get("tab1")
        self.assertEqual(session.sql_mode, mode)
        session.client_engine = self.engine
        return session

    def test_opposite_bits_are_refreshed_before_exact_reviewer_bytes(self):
        for client in (self.engine, None):
            cases = REVIEWER_TEXTS + (("", LAB_TEXT),)
            if self.engine == "mariadb":
                cases += (("", ANSI_TEXT),)
                if client == "mariadb":
                    cases += (("ANSI_QUOTES", ANSI_ON_TEXT),)
            for mode, sql in cases:
                with self.subTest(client=client, mode=mode, sql=sql):
                    session = self.stale(mode, ansi=sql in (ANSI_TEXT, ANSI_ON_TEXT))
                    session.client_engine = client
                    before = len(self.packets())
                    probe_ansi = (None if ("NO_BACKSLASH_ESCAPES" in mode or client == "mysql") else
                                  ("ANSI_QUOTES" in mode if (self.engine == "mariadb" and client == "mariadb")
                                   else (False if "ANSI_QUOTES" not in mode else None)))
                    probe = ed.TAG + dbops.client_status_probe(
                        "$$", "NO_BACKSLASH_ESCAPES" not in mode, ansi_quotes=probe_ansi, client_engine=client)[0]
                    review = self.call("/api/editor/run", sql=sql, require_confirmation=True)
                    self.assertEqual(review["status"], "needs_confirmation", review)
                    self.assertEqual(review["confirmation"]["statements"], [SYNC, probe, sql])
                    self.assertEqual(len(self.packets()), before, "review must not send its helper")
                    result = self.call("/api/editor/run", sql=sql, require_confirmation=True, confirmed=True,
                                       confirm_token=review["confirmation"]["token"])
                    self.assertEqual(result["status"], "complete", result)
                    results = [part for part in result["run"]["results"] if part["type"] == "rows"]
                    self.assertEqual(len(results), 1, "stale bits must not expose a hidden second result")
                    self.assertEqual(results[0]["rows"], [["single"]])
                    packets = [p[2] for p in self.packets()[before:]]
                    at = next(i for i, packet in enumerate(packets) if packet.endswith(session.statement_tag + sql))
                    self.assertTrue(packets[at - 2].endswith(SYNC), packets)
                    self.assertTrue(packets[at - 1].endswith(probe), packets)
                    self.assertNotIn(sql, packets[at - 2])
                    self.assertNotIn(sql, packets[at - 1])
                    records = [json.loads(line) for line in (self.state / "status-wire.log").read_text().splitlines()]
                    self.assertEqual(records[-1], [mode, "NO_BACKSLASH_ESCAPES" in mode,
                                                  "ANSI_QUOTES" in mode, 1, sql])
                    audit = self.store.audit_records()[0]
                    self.assertEqual(audit["statements"], [SYNC, probe, sql])

    def test_unbound_and_unknown_mode_send_nothing(self):
        for failure in ("routed", "mode"):
            for mode, sql in REVIEWER_TEXTS + (("", LAB_TEXT),):
                with self.subTest(failure=failure, sql=sql):
                    session = self.stale(mode)
                    original = session.sql_mode
                    if failure == "routed":
                        session._routed_startup = True
                    else:
                        session.sql_mode = None
                    before = len(self.packets())
                    result = self.call("/api/editor/run", sql=sql)
                    self.assertEqual(result["status"], "invalid", result)
                    self.assertEqual(len(self.packets()), before)
                    session._routed_startup = False
                    session.sql_mode = original

    def test_ordinary_units_leave_stale_bits_alone_and_have_no_interleaved_helper(self):
        self.stale()
        before = len(self.packets())
        sql = "SELECT 1; SELECT ROW_COUNT(); SELECT FOUND_ROWS(); SHOW WARNINGS; GET DIAGNOSTICS @n = NUMBER"
        result = self.run_sql(sql)
        self.assertEqual(result["status"], "complete", result)
        packets = [p[2] for p in self.packets()[before:]]
        self.assertFalse(any(dbops.CLIENT_STATUS_SYNC in p for p in packets))
        logical = self.session_log()
        at = logical.index("SELECT 1")
        self.assertEqual(logical[at:at + 5], sql.split("; "))

    def test_failed_helper_never_sends_user_unit_even_with_continue(self):
        self.stale()
        (self.state / "sync-fails").write_text("yes", encoding="utf-8")
        before = len(self.packets())
        result = self.run_sql(LAB_TEXT + "\n; SELECT 987", on_error="continue")
        self.assertNotEqual(result["status"], "complete")
        packets = [p[2] for p in self.packets()[before:]]
        self.assertTrue(any(dbops.CLIENT_STATUS_SYNC in p for p in packets))
        self.assertFalse(any(LAB_TEXT in p or "SELECT 987" in p for p in packets))
        self.assertTrue(self.registry.get("tab1").ended)
        self.assertEqual([u["status"] for u in result["run"]["statements"]], ["not_run", "not_run"])
        self.assertEqual(result["run"]["counts"]["run"], 0)

    def test_stop_after_helper_reply_never_starts_the_affected_unit(self):
        session = self.stale()
        before = len(self.packets())
        helper = session._helper

        def stop_after_reply(send, *args, **kwargs):
            step = helper(send, *args, **kwargs)
            if send == SYNC:
                stopped = session.stop()
                self.assertTrue(stopped["stop_requested"])
                self.assertFalse(stopped["stopped"], "Stop must not KILL a helper")
            return step

        with mock.patch.object(session, "_helper", side_effect=stop_after_reply):
            result = self.run_sql(LAB_TEXT + "\n; SELECT 987")
        self.assertEqual(result["status"], "stopped", result)
        self.assertEqual([u["status"] for u in result["run"]["statements"]], ["not_run", "not_run"])
        self.assertEqual(result["run"]["counts"]["run"], 0)
        packets = [p[2] for p in self.packets()[before:]]
        self.assertTrue(any(dbops.CLIENT_STATUS_SYNC in p for p in packets))
        self.assertFalse(any(LAB_TEXT in p or "SELECT 987" in p for p in packets))
        self.assertFalse((self.state / "kills.log").exists())

    def test_mode_change_after_helper_is_refused_by_the_user_packet_guard(self):
        session = self.stale()
        before = len(self.packets())
        helper = session._helper

        def route_after_reply(send, *args, **kwargs):
            step = helper(send, *args, **kwargs)
            if "AS mf_probe" in send:
                (self.state / "packet-routes.json").write_text(
                    json.dumps([{"sql_mode": "NO_BACKSLASH_ESCAPES"}]), encoding="utf-8")
            return step

        with mock.patch.object(session, "_helper", side_effect=route_after_reply):
            result = self.run_sql(LAB_TEXT + "\n; SELECT 987", on_error="continue")
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["run"]["counts"]["run"], 0)
        self.assertEqual([u["status"] for u in result["run"]["statements"]], ["not_run", "not_run"])
        self.assertFalse((self.state / "status-wire.log").exists(), "the server guard must stop the user SQL")
        self.assertFalse(any("SELECT 987" in p[2] for p in self.packets()[before:]))
        self.assertTrue(session.ended)

    def test_stale_client_bits_after_sync_ok_split_probe_and_never_send_unit(self):
        cases = [("", LAB_TEXT, False), ("NO_BACKSLASH_ESCAPES", REVIEWER_TEXTS[1][1], False)]
        if self.engine == "mariadb":
            cases.extend((("", ANSI_TEXT, True), ("ANSI_QUOTES", ANSI_ON_TEXT, True)))
        for mode, sql, ansi in cases:
            with self.subTest(mode=mode, ansi=ansi):
                self.setUp()
                session = self.stale(mode, ansi=ansi)
                (self.state / "stale-after-sync").write_text("yes", encoding="utf-8")
                before = len(self.packets())
                result = self.run_sql(sql + "\n; SELECT 987", on_error="continue")
                self.assertEqual(result["status"], "session_ended", result)
                self.assertEqual(session.ended_reason, "client_status_sync_failed")
                self.assertIn("The client's quote status could not be confirmed. Nothing was sent.",
                              session.end_message or "")
                self.assertEqual([u["status"] for u in result["run"]["statements"]], ["not_run", "not_run"])
                self.assertEqual(result["run"]["counts"]["run"], 0)
                packets = [p[2] for p in self.packets()[before:]]
                self.assertTrue(any(dbops.CLIENT_STATUS_SYNC in p for p in packets))
                self.assertTrue(any("AS mf_probe" in p for p in packets))
                self.assertFalse(any(sql in p or "SELECT 987" in p for p in packets), packets)
                self.assertFalse((self.state / "status-wire.log").exists())

    def test_wrong_probe_value_ends_session_and_never_sends_unit(self):
        session = self.stale()
        (self.state / "wrong-probe").write_text("yes", encoding="utf-8")
        before = len(self.packets())
        result = self.run_sql(LAB_TEXT + "\n; SELECT 987", on_error="continue")
        self.assertEqual(result["status"], "session_ended", result)
        self.assertEqual(session.ended_reason, "client_status_sync_failed")
        self.assertIn("The client's quote status could not be confirmed. Nothing was sent.",
                      session.end_message or "")
        self.assertEqual([u["status"] for u in result["run"]["statements"]], ["not_run", "not_run"])
        self.assertEqual(result["run"]["counts"]["run"], 0)
        packets = [p[2] for p in self.packets()[before:]]
        self.assertFalse(any(LAB_TEXT in p or "SELECT 987" in p for p in packets), packets)
        self.assertFalse((self.state / "status-wire.log").exists())


class MariaDBStatusO3ProcessTests(EditorStatusO3ProcessTests):
    engine = "mariadb"
