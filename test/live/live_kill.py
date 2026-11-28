"""Live suite: Locks and Sessions Kill against real servers.

The KILL endpoint (/api/live/kill/prepare then /api/live/kill, as the
workspace sends them) is verified against running servers, not only fixtures:

- GA MariaDB 12.3 and 13.0 (wsrep_on OFF with a configured but disconnected
  provider): the Locks report and the Sessions report carry the same waiver
  identity (wsrep_disconnected_provider true, no node UUID). Kill from
  Sessions, and from Locks when a lock wait can be produced (an InnoDB
  duplicate-key insert waiting on a held insert), ends the victim; the audit
  record's first statement contains the three waiver clauses
  (@@global.wsrep_on <=> 0, wsrep_connected <=> 'OFF',
  wsrep_cluster_size <=> '0').
- mf-galera-2 after SET GLOBAL wsrep_on = OFF (still connected): the Sessions
  report gives no server identity and Kill is refused with nothing sent; the
  victim keeps running. Class setup puts a node left OFF by an interrupted
  earlier run back to ON and records the value; a registered cleanup restores
  it and reads it back, so a failed restore is a test error.
- mf-galera-1 and mf-pxc-1 (connected nodes): Kill from Sessions succeeds and
  its audit statement carries the node UUID clause; a request prepared on the
  peer's own connection (valid connection identity) but carrying this node's
  server identity and the victim's expect, as a rotating endpoint would send
  it, is refused on the server identity mismatch after the peer was reached
  and before anything is sent.

Every victim is a separate client login running a long SELECT SLEEP(60) (or a
lock-waiting INSERT), started in a thread; cleanup ends it directly also when
the workspace kill failed, so no session outlives the test.

Run: scripts/live-tests.sh kill   (or MYFLAMES_LIVE_LABS=1 with the labs up)
"""
import os
import re
import sys
import threading
import time
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lab import (Lab, LabError, check_lab, conn_for, ga_passwords, ga_servers,  # noqa: E402
                  lab_password, make_ga_home, make_home, redact, require_live,
                  wait_for)


def setUpModule():
    require_live()


def _unique(prefix):
    """A throwaway marker that cannot collide with an earlier or parallel run."""
    return "%s%s" % (prefix, uuid.uuid4().hex[:10])


_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class _KillBase(object):
    """Shared helpers: saved connections, direct client logins, the Kill flow."""

    PASSWORD = None

    def _direct(self, conn):
        """A client login as the lab admin, for victims, holders and cleanup."""
        from myflames.connector import MySQLConnection
        return MySQLConnection(host=conn["host"], port=int(conn["port"]),
                               user=conn["user"], password=self.PASSWORD,
                               ssl_mode=conn.get("ssl_mode") or "PREFERRED",
                               connect_timeout=10, query_timeout=120)

    def _sessions(self, conn):
        return self.lab.api("/api/live/sessions", {"connection": dict(conn)})

    def _find_session(self, conn, marker):
        for session in self._sessions(conn).get("sessions") or ():
            if marker in (session.get("info") or ""):
                return session
        return None

    def _run_sql(self, handle, sql, outcome):
        started = time.monotonic()
        try:
            handle.run(sql, timeout=330)
            outcome.update(status="complete", elapsed=time.monotonic() - started)
        except Exception as exc:  # noqa: BLE001 - asserted below, redacted
            outcome.update(error=redact(str(exc)), elapsed=time.monotonic() - started)

    def _start_session(self, conn, sql, marker):
        """One client login running sql in a thread; returns the session row.

        Cleanup is registered before the wait for the session row, so a
        timeout there cannot leave the login running on the server."""
        handle = self._direct(conn)
        outcome = {}
        # Daemon: a session cleanup cannot end (asserted there) must not keep
        # the interpreter alive for the client subprocess's own timeout.
        thread = threading.Thread(target=self._run_sql, args=(handle, sql, outcome),
                                  daemon=True)
        started = {"thread": thread, "outcome": outcome, "session": None,
                   "handle": handle, "marker": marker, "conn": dict(conn)}
        thread.start()
        self.addCleanup(self._end_session, started)
        started["session"] = wait_for(lambda: self._find_session(conn, marker), until=bool,
                                      timeout=20.0, interval=0.5,
                                      what="the session running %s in the Sessions report" % marker)
        return started

    def _start_victim(self, conn, seconds=60):
        marker = _unique("mf_kill_")
        return self._start_session(conn, "SELECT SLEEP(%d) /* %s */" % (seconds, marker), marker)

    def _kill_by_marker(self, conn, marker):
        """KILL CONNECTION every session whose INFO carries the test's unique
        marker, through one separate direct admin login."""
        with self._direct(conn) as admin:
            out = admin.run("SELECT ID FROM information_schema.PROCESSLIST "
                            "WHERE ID <> CONNECTION_ID() AND INFO LIKE '%%%s%%'" % marker,
                            timeout=30)
            for line in out.splitlines():
                line = line.strip()
                if line.isdigit():
                    admin.run("KILL CONNECTION %d" % int(line), timeout=30)

    def _end_session(self, started):
        """Cleanup: end a still-running victim or holder directly, whatever it takes.

        MySQLConnection.close() cannot stop an active client subprocess, so a
        SLEEP or a lock-holding transaction must be ended on the server. On
        any failure of the Sessions lookup or the Kill, the session is found
        in information_schema.PROCESSLIST by the test's unique marker and
        ended through a separate direct admin login. The worker is joined on
        every path; a session still alive afterwards fails the cleanup with a
        redacted message."""
        failures = []
        thread = started["thread"]
        if thread.is_alive():
            try:
                session = started.get("session")
                if session is None:
                    # The start wait never found it (a timeout raised): look once
                    # more so the login is still ended by its marker.
                    session = self._find_session(started["conn"], started["marker"])
                if session and session.get("id"):
                    self._direct(started["conn"]).run(
                        "KILL CONNECTION %d" % int(session["id"]), timeout=30)
            except Exception as exc:  # noqa: BLE001 - retried by marker below
                failures.append(redact(str(exc)))
            thread.join(10)
        if thread.is_alive():
            try:
                self._kill_by_marker(started["conn"], started["marker"])
            except Exception as exc:  # noqa: BLE001 - reported redacted below
                failures.append(redact(str(exc)))
            thread.join(10)
        try:
            started["handle"].close()
        except Exception:
            pass
        if thread.is_alive():
            detail = "; ".join(failures) or "no session row matched the marker"
            raise AssertionError("cleanup could not end the session running %s: %s"
                                 % (started["marker"], detail))

    def _connection_identity(self, conn):
        from myflames import ui
        return ui._review_identity(conn, self.lab.ws)

    def _expect(self, conn, session, server_identity, trx_started=None, ps_thread_id=None):
        return {"user": session.get("user"), "host": session.get("host"),
                "trx_started": trx_started, "ps_thread_id": ps_thread_id,
                "connection_identity": self._connection_identity(conn),
                "server_identity": server_identity}

    def _review_kill(self, conn, connection_id, mode, expect):
        review = self.lab.api("/api/live/kill/prepare",
                              {"connection": dict(conn), "connection_id": connection_id,
                               "mode": mode, "expect": expect})
        self.assertTrue(review.get("review_token"), "no review token issued")
        return review

    def _send_kill(self, conn, connection_id, mode, expect, review):
        return self.lab.api("/api/live/kill",
                            {"connection": dict(conn), "connection_id": connection_id,
                             "mode": mode, "confirmed": True, "expect": expect,
                             "review_token": review["review_token"]})

    def _audit_record(self, audit_id):
        for record in self.lab.store.audit_records():
            if record.get("id") == audit_id:
                return record
        raise AssertionError("no audit record %s" % audit_id)

    def _guard_statement(self, audit_id):
        statements = self._audit_record(audit_id).get("statements") or []
        self.assertTrue(statements, "the audit record has no statements")
        return statements[0]

    def _assert_victim_ended(self, conn, victim, seconds=60):
        """The kill reached the victim: its login ended early with an error."""
        victim["thread"].join(30)
        self.assertFalse(victim["thread"].is_alive(), "the victim session never ended")
        outcome = victim["outcome"]
        self.assertIn("error", outcome, "the victim finished unharmed: %s" % outcome.get("status"))
        self.assertLess(outcome["elapsed"], seconds - 1.0,
                        "the victim ran %.1fs of %ds: the kill did not reach it"
                        % (outcome["elapsed"], seconds))
        wait_for(lambda: self._find_session(conn, victim["marker"]) is None, until=bool,
                 timeout=15.0, interval=0.5, what="the victim session to disappear")

    def _assert_victim_alive(self, conn, victim):
        """Nothing was sent: the victim still runs and is still listed."""
        self.assertTrue(victim["thread"].is_alive(), "the victim ended without a kill")
        self.assertNotIn("error", victim["outcome"],
                         "the victim errored: %s" % victim["outcome"].get("error"))
        self.assertIsNotNone(self._find_session(conn, victim["marker"]),
                             "the victim is gone from the Sessions report without a kill")


class _GAKillBase(_KillBase):
    """One GA MariaDB server's Locks and Sessions Kill (disconnected provider)."""

    SERVER = None

    @classmethod
    def setUpClass(cls):
        require_live()
        try:
            servers = ga_servers()
        except LabError as exc:
            raise unittest.SkipTest("the GA matrix is not available here: %s" % redact(exc))
        entry = servers.get(cls.SERVER)
        if not entry or not entry.get("up"):
            raise unittest.SkipTest("GA server %s is not running (scripts/ga-matrix.sh up %s)"
                                    % (cls.SERVER, cls.SERVER))
        cls.home = make_ga_home("kill")
        cls.lab = Lab()
        cls.profile = cls.lab.profile("ga-" + cls.SERVER)
        cls.conn = conn_for(cls.profile)
        cls.PASSWORD = ga_passwords()["MF_ADMIN_PASSWORD"]

    def test_01_reports_share_waiver_identity(self):
        locks = self.lab.api("/api/live/locks", {"connection": dict(self.conn)})
        sessions = self._sessions(self.conn)
        locks_identity = locks.get("server_identity") or {}
        sessions_identity = sessions.get("server_identity") or {}
        self.assertEqual(locks_identity, sessions_identity,
                         "Locks and Sessions read different server identities")
        self.assertIs(sessions_identity.get("wsrep_disconnected_provider"), True,
                      "no disconnected-provider waiver in the Sessions identity: %s"
                      % sessions_identity)
        self.assertNotIn("wsrep_gcomm_uuid", sessions_identity,
                         "the waiver identity carries a node UUID: %s" % sessions_identity)
        for key in ("server_id", "hostname", "port", "version"):
            self.assertTrue(sessions_identity.get(key), "the identity misses %s" % key)

    def _assert_waiver_audit(self, audit_id):
        first = self._guard_statement(audit_id)
        for clause in ("@@global.wsrep_on <=> 0",
                       "wsrep_connected') <=> 'OFF'",
                       "wsrep_cluster_size') <=> '0'"):
            self.assertIn(clause, first,
                          "the audit's guard statement misses the waiver clause %s" % clause)

    def test_02_kill_from_sessions_ends_victim(self):
        server_identity = self._sessions(self.conn).get("server_identity") or {}
        self.assertIs(server_identity.get("wsrep_disconnected_provider"), True,
                      "the Sessions report has no waiver identity")
        victim = self._start_victim(self.conn)
        expect = self._expect(self.conn, victim["session"], server_identity)
        review = self._review_kill(self.conn, victim["session"]["id"], "connection", expect)
        result = self._send_kill(self.conn, victim["session"]["id"], "connection", expect, review)
        self.assertEqual(result.get("status"), "killed",
                         "the Sessions kill did not kill: %s" % redact(result.get("message")))
        self._assert_waiver_audit(result.get("audit_id"))
        self._assert_victim_ended(self.conn, victim)

    def _drop_schema(self, schema):
        """Registered cleanup: drop the throwaway schema; a leftover fails."""
        try:
            self._direct(self.conn).run("DROP DATABASE IF EXISTS `%s`" % schema, timeout=30)
        except Exception as exc:  # noqa: BLE001 - redacted
            raise AssertionError("the throwaway schema %s survived cleanup: %s"
                                 % (schema, redact(str(exc))))

    def test_03_kill_from_locks_ends_waiting_victim(self):
        """An InnoDB lock wait is produced; the Locks kill ends the waiter."""
        schema = _unique("mf_kl_")
        holder_marker = _unique("mf_hold_")
        victim_marker = _unique("mf_wait_")
        direct = self._direct(self.conn)
        # Registered before the CREATE, whose reply can be lost after the
        # server created the schema; the drop of this exact generated,
        # test-owned name is harmless when the CREATE never happened. Cleanups
        # run LIFO: the sessions started below end first (the victim, then the
        # holder, releasing its lock), so the drop never waits on a session's
        # metadata lock. Registered before the table and sessions, so the
        # schema goes away also when the test fails partway.
        self.addCleanup(self._drop_schema, schema)
        direct.run("CREATE DATABASE `%s`" % schema, timeout=30)
        direct.run("CREATE TABLE `%s`.`t` (id INT PRIMARY KEY)" % schema, timeout=30)
        holder = self._start_session(
            self.conn,
            "START TRANSACTION; INSERT INTO `%s`.`t` VALUES (1); "
            "SELECT SLEEP(300) /* %s */" % (schema, holder_marker), holder_marker)
        victim = self._start_session(
            self.conn,
            "INSERT INTO `%s`.`t` VALUES (1) /* %s */" % (schema, victim_marker),
            victim_marker)

        def waiting_entry():
            report = self.lab.api("/api/live/locks", {"connection": dict(self.conn)})
            for trx in report.get("transactions") or ():
                if victim_marker in (trx.get("current_statement") or "") and \
                        trx.get("state") == "LOCK WAIT":
                    return report, trx
            return None

        found = wait_for(waiting_entry, until=bool, timeout=20.0, interval=0.5,
                         what="the victim's lock wait in the Locks report")
        report, trx = found
        server_identity = report.get("server_identity") or {}
        self.assertIs(server_identity.get("wsrep_disconnected_provider"), True,
                      "the Locks report has no waiver identity")
        expect = self._expect(self.conn, {"user": trx.get("user"), "host": trx.get("host")},
                              server_identity,
                              trx_started=trx.get("started"),
                              ps_thread_id=trx.get("ps_thread_id"))
        review = self._review_kill(self.conn, trx["connection_id"], "connection", expect)
        result = self._send_kill(self.conn, trx["connection_id"], "connection", expect, review)
        self.assertEqual(result.get("status"), "killed",
                         "the Locks kill did not kill: %s" % redact(result.get("message")))
        self.assertIn("transaction", result.get("verified") or [],
                      "the Kill did not verify the victim's transaction: %s"
                      % (result.get("verified"),))
        self._assert_waiver_audit(result.get("audit_id"))
        victim["thread"].join(30)
        self.assertFalse(victim["thread"].is_alive(), "the waiting victim never ended")
        self.assertIn("error", victim["outcome"],
                      "the waiting victim finished unharmed: %s"
                      % victim["outcome"].get("status"))


class MariaDB123KillTest(_GAKillBase, unittest.TestCase):
    """MariaDB 12.3: wsrep_on OFF with a configured but disconnected provider."""

    SERVER = "mariadb-12.3"


class MariaDB130KillTest(_GAKillBase, unittest.TestCase):
    """MariaDB 13.0: wsrep_on OFF with a configured but disconnected provider."""

    SERVER = "mariadb-13.0"


class _ClusterKillBase(_KillBase):
    """One cluster node's Sessions Kill plus the peer-refusal check."""

    CLUSTERS = ()
    PROFILE = None
    PEER = None

    @classmethod
    def setUpClass(cls):
        check_lab(*cls.CLUSTERS)
        cls.home = make_home("kill")
        cls.lab = Lab()
        cls.profile = cls.lab.profile(cls.PROFILE)
        cls.conn = conn_for(cls.profile)
        cls.peer = cls.lab.profile(cls.PEER)
        cls.peer_conn = conn_for(cls.peer)
        cls.PASSWORD = lab_password()

    def test_01_kill_binds_node_uuid_and_peer_refuses(self):
        server_identity = self._sessions(self.conn).get("server_identity") or {}
        uuid = server_identity.get("wsrep_gcomm_uuid")
        self.assertTrue(uuid and _UUID.match(uuid),
                        "the Sessions report has no node UUID: %s" % server_identity)
        victim = self._start_victim(self.conn)
        expect = self._expect(self.conn, victim["session"], server_identity)
        review = self._review_kill(self.conn, victim["session"]["id"], "connection", expect)
        # A rotating endpoint produces a request reviewed on the peer's own
        # connection, so its connection identity is valid, but carrying this
        # node's server identity and the victim's expect. The peer is reached
        # and reads its own identity, then refuses on the server identity
        # mismatch before anything is sent.
        peer_expect = self._expect(self.peer_conn, victim["session"], server_identity)
        peer_review = self._review_kill(self.peer_conn, victim["session"]["id"],
                                        "connection", peer_expect)
        refused = self._send_kill(self.peer_conn, victim["session"]["id"],
                                  "connection", peer_expect, peer_review)
        self.assertEqual(refused.get("status"), "refused",
                         "the peer request was not refused: %s" % redact(refused.get("message")))
        self.assertIn("server identity", (refused.get("message") or "").lower(),
                      "the peer refusal does not name the server identity mismatch: %s"
                      % redact(refused.get("message")))
        self.assertIn("nothing was sent", (refused.get("message") or "").lower(),
                      "the peer refusal does not state that nothing was sent: %s"
                      % redact(refused.get("message")))
        self.assertFalse(refused.get("sent"), "the refused peer kill sent statements")
        record = self._audit_record(refused.get("audit_id"))
        # The audit's connection block carries the engine and version read
        # from the server that answered: present only when the peer was read.
        audited = record.get("connection") or {}
        self.assertEqual(audited.get("profile_name"), self.peer["name"],
                         "the refusal audit names another connection: %s" % redact(audited))
        self.assertTrue(audited.get("engine") and audited.get("server_version"),
                        "the peer was never reached: the refusal audit has no server: %s"
                        % redact(record))
        # The connection-identity gate on its own: a review bound to this
        # connection sent to the peer is refused before the peer is opened.
        stale = self._send_kill(self.peer_conn, victim["session"]["id"],
                                "connection", expect, review)
        self.assertEqual(stale.get("status"), "refused",
                         "the connection-identity gate did not refuse: %s"
                         % redact(stale.get("message")))
        self.assertIn("connection or its write policy changed",
                      (stale.get("message") or "").lower(),
                      "the connection-identity refusal has the wrong message: %s"
                      % redact(stale.get("message")))
        self._assert_victim_alive(self.conn, victim)
        # On the reviewed connection the same review kills the victim, and the
        # audit's guard statement binds this node UUID.
        result = self._send_kill(self.conn, victim["session"]["id"], "connection", expect, review)
        self.assertEqual(result.get("status"), "killed",
                         "the Sessions kill did not kill: %s" % redact(result.get("message")))
        first = self._guard_statement(result.get("audit_id"))
        self.assertIn("VARIABLE_NAME = 'wsrep_gcomm_uuid'", first,
                      "the audit's guard statement does not bind the node UUID")
        self.assertIn(uuid, first, "the audit's guard statement binds another node UUID")
        self._assert_victim_ended(self.conn, victim)


class GaleraKillTest(_ClusterKillBase, unittest.TestCase):
    """MariaDB 11.8 Galera: kill on galera-1, the peer refusal through
    galera-2, and the wsrep_on OFF refusal on galera-2 itself."""

    CLUSTERS = ("galera",)
    PROFILE = "cluster-galera-1"
    PEER = "cluster-galera-2"

    @classmethod
    def setUpClass(cls):
        super(GaleraKillTest, cls).setUpClass()
        # The lab runs galera-2 with wsrep_on ON, and only this suite ever
        # sets it OFF. A hard stop of an earlier run between test_02's SET
        # GLOBAL wsrep_on = OFF and its cleanup leaves it OFF for every later
        # lane, so put it back to ON here (and read it back) before recording
        # the value the cleanup restores. _direct only needs class attributes,
        # so the class itself stands in for an instance here.
        try:
            if cls._read_wsrep_on() == "OFF":
                cls._direct(cls, cls.peer_conn).run("SET GLOBAL wsrep_on = ON", timeout=30)
            cls.peer_wsrep_on = cls._read_wsrep_on()
        except Exception as exc:  # noqa: BLE001 - redacted
            raise LabError("could not read or restore galera-2's wsrep_on: %s"
                           % redact(str(exc)))
        if cls.peer_wsrep_on != "ON":
            raise LabError("galera-2 has wsrep_on=%s after the restore attempt (expected ON)"
                           % cls.peer_wsrep_on)

    @classmethod
    def _read_wsrep_on(cls):
        """galera-2's current wsrep_on value ('ON'/'OFF')."""
        out = cls._direct(cls, cls.peer_conn).run("SHOW GLOBAL VARIABLES LIKE 'wsrep_on'",
                                                  timeout=30)
        parts = out.split()
        return parts[1] if len(parts) > 1 else None

    def _restore_wsrep_on(self):
        """Registered cleanup: put galera-2's wsrep_on back to the value
        recorded at class setup and read it back, so a failed restore is a
        test error and the node is never left fail-closed for later suites."""
        self._direct(self.peer_conn).run("SET GLOBAL wsrep_on = %s" % self.peer_wsrep_on,
                                         timeout=30)
        current = self._read_wsrep_on()
        self.assertEqual(current, self.peer_wsrep_on,
                         "galera-2 wsrep_on is %s after the restore (expected %s)"
                         % (current, self.peer_wsrep_on))

    def test_02_wsrep_off_sessions_kill_refused(self):
        """wsrep_on OFF but still connected: no identity, Kill refused, victim alive."""
        conn2 = self.peer_conn
        # Registered before the SET, whose reply can be lost after the server
        # applied it; the restore sets the value recorded at class setup and
        # reads it back, so running it when the SET never happened is harmless.
        self.addCleanup(self._restore_wsrep_on)
        self._direct(conn2).run("SET GLOBAL wsrep_on = OFF", timeout=30)
        # The Sessions report keeps working but gives no server identity: a
        # connected node with wsrep_on OFF hides its node UUID, and the
        # disconnected-provider waiver does not apply.
        def no_identity():
            report = self._sessions(conn2)
            return not (report.get("server_identity") or {})

        wait_for(no_identity, until=bool, timeout=20.0, interval=0.5,
                 what="the Sessions report to give no server identity")
        victim = self._start_victim(conn2)
        expect = self._expect(conn2, victim["session"], {})
        review = self._review_kill(conn2, victim["session"]["id"], "connection", expect)
        result = self._send_kill(conn2, victim["session"]["id"], "connection", expect, review)
        self.assertEqual(result.get("status"), "refused",
                         "the kill was not refused without a server identity: %s"
                         % redact(result.get("message")))
        self.assertIn("identity", (result.get("message") or "").lower(),
                      "the refusal does not name the missing identity: %s"
                      % redact(result.get("message")))
        self.assertIn("nothing was sent", (result.get("message") or "").lower(),
                      "the refusal does not state that nothing was sent: %s"
                      % redact(result.get("message")))
        self.assertFalse(result.get("sent"), "the refused kill sent statements")
        self._assert_victim_alive(conn2, victim)
        # Restore now (the registered cleanup restores again and reads the
        # value back): the identity comes back, so the node is not left
        # fail-closed even when a later step failed.
        self._direct(conn2).run("SET GLOBAL wsrep_on = ON", timeout=30)

        def has_identity():
            report = self._sessions(conn2)
            return bool((report.get("server_identity") or {}).get("wsrep_gcomm_uuid"))

        wait_for(has_identity, until=bool, timeout=20.0, interval=0.5,
                 what="the Sessions report to give the node identity again")


class PxcKillTest(_ClusterKillBase, unittest.TestCase):
    """Percona XtraDB Cluster 8.4: kill on pxc-1, refusal through pxc-2."""

    CLUSTERS = ("pxc",)
    PROFILE = "cluster-pxc-1"
    PEER = "cluster-pxc-2"


if __name__ == "__main__":
    unittest.main()
