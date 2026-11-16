"""Consent canary: network-level proof that reading one connection group never
makes a login attempt to a saved connection in another group.

test_connection_groups.py proves the consent rules with every connection faked
(its header: no test there opens a network socket). This file is the complement:
a TCP listener on 127.0.0.1 plays the "production" saved connection, only the
active connection is faked, and every other connection goes through the real
connector, which spawns the real mysql/mariadb client against the listener. A
consent bug in any layer (ui profile scoping, connection_groups.members,
GroupScope.opener, replication.profile_opener, connector._check_login_consent)
shows up as an accepted connection on the listener.

Readers are driven the way the workspace drives them: ui.api_response for
/api/live/cluster, /api/live/replication, /api/live/proxysql and
/api/live/readyset against a throwaway HOME with the vault secret backend, the
same pattern as GroupCase. The MCP topology tools are not covered here on
purpose: mcp_server.replication_status_tool/cluster_status_tool/
readyset_status_tool call the module readers without profiles or open_profile,
so they share no saved-peer login path (test_connection_groups.
test_mcp_topology_tools_open_only_selected_connection pins that).

The positive controls are the non-vacuity proof: the identical setup with the
peer saved connection in the active group (consent given) must make the
listener record at least one connection. The listener accepts and closes at
once, so the client fails fast and each test stays far under 10 seconds.

Skips only when the connector has no client binary to spawn; with
MYFLAMES_REQUIRE_LIVE=1 a missing client fails instead.
"""
import os
import socket
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import _cluster_fixtures as CF
from test_proxysql import FakeAdmin
from test_readyset import FakeReadyset
from test_topology_links import FakeProbeConn, xml_rows

from myflames import client_tools, connector, replication as rp, topology_links as tl, ui
from myflames.workspace_store import ENDPOINT_FIELDS, WorkspaceStore

SKIP_REASON = "mysql client needed for the consent canary"
GROUP_A = "lab"
GROUP_B = "production"
#: The active connection's fake endpoint. Below every ephemeral port range, so
#: it can never collide with the listener's port.
ACTIVE_PORT = 3399
ACTIVE_SOURCE_UUID = "aaaaaaaa-1111-2222-3333-444444444444"

#: test id -> (seconds, accepted connections), printed by tearDownModule.
REPORT = {}


def _client_available():
    """The connector's own candidate scan (stat-only), as _resolve_binary sees it."""
    try:
        return any(c["name"] in ("mysql", "mariadb") and not c.get("trust_error")
                   for c in client_tools.snapshot())
    except Exception:
        return False


class CanaryListener(object):
    """A TCP listener on 127.0.0.1 that records every accepted connection."""

    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sock.settimeout(0.1)
        self.port = self._sock.getsockname()[1]
        self._hits = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="consent-canary")
        self._thread.daemon = True
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, peer = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            # Record before closing: the client only fails after this, so a
            # finished reader can never hide a dial from the count.
            with self._lock:
                self._hits.append((time.monotonic(), peer))
            try:
                conn.close()
            except OSError:
                pass

    def count(self):
        with self._lock:
            return len(self._hits)

    def close(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(5)


class CanaryReplica(FakeProbeConn):
    """The active connection for the replication reader: a MySQL replica whose
    only channel names the canary endpoint as its source, with a relay log that
    holds no source format event, so the source version can only come from a
    login to the matching saved connection."""

    def __init__(self, canary_port):
        super(CanaryReplica, self).__init__("database", hostname=socket.gethostname(),
                                            uuid="uuid-canary-replica")
        self.cfg["port"] = ACTIVE_PORT
        self.cfg["channels"] = [{
            "Channel_Name": "", "Source_Host": "127.0.0.1", "Source_Port": str(canary_port),
            "Source_Server_Id": "301", "Source_UUID": ACTIVE_SOURCE_UUID,
            "Replica_IO_Running": "Yes", "Replica_SQL_Running": "Yes", "Seconds_Behind_Source": "0",
            "Relay_Log_File": "relay.000001", "Relay_Log_Pos": "4",
            "Retrieved_Gtid_Set": "", "Executed_Gtid_Set": "", "Auto_Position": "1",
        }]
        for key in ENDPOINT_FIELDS:
            setattr(self, key, None)
        self.host = "127.0.0.1"
        self.port = ACTIVE_PORT
        self.user = "dba"

    def run(self, sql, extra_flags=None, timeout=None):
        sql = sql.replace(rp.REPLICATION_TAG, "")
        if "RELAYLOG" in sql.upper():
            return xml_rows([])
        return super(CanaryReplica, self).run(sql, extra_flags=extra_flags, timeout=timeout)


class CanaryReadyset(FakeReadyset):
    """The active connection for the Readyset reader: one upstream, the canary."""

    def __init__(self, canary_port):
        super(CanaryReadyset, self).__init__()
        self._canary_port = canary_port

    def run(self, sql, extra_flags=None, timeout=None):
        if "UPSTREAM_SERVERS" in sql.upper():
            return xml_rows([{
                "host": "127.0.0.1", "port": str(self._canary_port), "database": "shop",
                "user": "readyset", "weight": "100", "purpose": "cdc, query", "active_cdc": "true",
                "health": "up", "down_since": None, "last_ok": "2026-10-20T12:00:00Z",
                "connections": "3",
            }])
        return super(CanaryReadyset, self).run(sql, extra_flags=extra_flags, timeout=timeout)


class ConsentCanaryCase(unittest.TestCase):

    def setUp(self):
        self._started = time.monotonic()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = dict(os.environ, HOME=self.temp.name, USERPROFILE=self.temp.name,
                   MYFLAMES_SECRET_BACKEND="vault", PYTHONDONTWRITEBYTECODE="1")
        env.pop("MYFLAMES_VAULT_PASSPHRASE", None)
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.listener = CanaryListener()
        self.addCleanup(self.listener.close)
        self.store = WorkspaceStore()
        tl._reset_cache_for_tests()
        rp.reset_source_cache()
        self.save("current", GROUP_A, host="127.0.0.1", port=ACTIVE_PORT)
        if not _client_available():
            if os.environ.get("MYFLAMES_REQUIRE_LIVE") == "1":
                self.fail(SKIP_REASON)
            self.skipTest(SKIP_REASON)

    def tearDown(self):
        listener = getattr(self, "listener", None)
        if listener is not None:
            REPORT[self.id().split(".")[-1]] = (time.monotonic() - self._started, listener.count())

    def save(self, pid, group, **fields):
        value = dict(name=pid, host=pid + ".example", port=3306, user="dba", group=group)
        value.update(fields)
        return self.store.save("profile", value, pid, password="test-password", remember=True)

    def save_peer(self, group):
        """The "production" connection: host and port are the listener, so only
        consent stands between a reader and a login attempt."""
        return self.save("peer", group, host="127.0.0.1", port=self.listener.port)

    def _request(self):
        current = self.store.get("profile", "current")
        return dict({k: current.get(k) for k in ENDPOINT_FIELDS}, profile_id="current")

    def _drive(self, path, fake):
        """Run one workspace read. The active connection is the fake; every other
        connection is the real connector, which spawns the real client."""
        workspace = types.SimpleNamespace(store=self.store)
        real_connection = connector.MySQLConnection
        dialed = []

        def factory(**options):
            if (options.get("host"), options.get("port")) == ("127.0.0.1", ACTIVE_PORT):
                return fake
            dialed.append(options)
            return real_connection(**options)

        with mock.patch("myflames.connector.MySQLConnection", side_effect=factory):
            report = ui.api_response(path, {"connection": self._request()}, workspace)
        return report, dialed

    # -- fakes per reader ------------------------------------------------------

    def cluster_conn(self):
        # A standalone server: tier discovery probes every consenting group
        # member, so the peer is dialed the moment consent lets it through.
        return CF.conn("mysql-8.4-standalone")

    def replication_conn(self):
        return CanaryReplica(self.listener.port)

    def proxysql_conn(self):
        conn = FakeAdmin()
        self.addCleanup(conn.db.close)
        conn.db.execute("INSERT INTO main.mysql_servers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (40, "127.0.0.1", self.listener.port, 0, "ONLINE", 100, 0, 1000, 0, 0, 0,
                         "canary backend"))
        conn.load("servers")
        return conn

    def readyset_conn(self):
        return CanaryReadyset(self.listener.port)

    # -- the two directions ------------------------------------------------------

    def _assert_never_dialed(self, path, fake, suggested):
        self.save_peer(GROUP_B)
        report, dialed = self._drive(path, fake)
        self.assertEqual(dialed, [], "the real connector was built for a peer: %r" % (dialed,))
        self.assertEqual(self.listener.count(), 0,
                         "CONSENT LEAK: %s with only group %s active accepted a connection for the "
                         "group %s saved connection" % (path, GROUP_A, GROUP_B))
        group = report.get("connection_group") or {}
        self.assertEqual(group.get("read"), [])
        if suggested:
            # The active server named the peer's endpoint; consent, not
            # ignorance, is what kept the login from happening.
            self.assertIn("peer", [s.get("id") for s in group.get("suggestions") or []])

    def _assert_dialed(self, path, fake):
        self.save_peer(GROUP_A)
        report, dialed = self._drive(path, fake)
        self.assertTrue(dialed, "no login attempt reached the real connector; the canary is vacuous")
        for options in dialed:
            self.assertEqual((options.get("host"), options.get("port")), ("127.0.0.1", self.listener.port))
        self.assertGreaterEqual(self.listener.count(), 1,
                                "the canary listener saw no connection; the canary is vacuous")
        reads = {r["id"]: r for r in (report.get("connection_group") or {}).get("read") or []}
        self.assertIn("peer", reads)
        # The listener closes without answering, so the read fails after the
        # login attempt; both outcomes prove the wire was touched.
        self.assertIn(reads["peer"]["outcome"], ("read", "failed"))


class TestConsentCanary(ConsentCanaryCase):

    def test_cluster_read_never_dials_another_group(self):
        self._assert_never_dialed("/api/live/cluster", self.cluster_conn(), suggested=False)

    def test_replication_read_never_dials_another_group(self):
        # Covers both replication readers: the source login
        # (attach_source_versions, the channel names the canary as source) and
        # tier discovery (discover_database_tiers).
        self._assert_never_dialed("/api/live/replication", self.replication_conn(), suggested=True)

    def test_proxysql_read_never_dials_another_group(self):
        self._assert_never_dialed("/api/live/proxysql", self.proxysql_conn(), suggested=True)

    def test_readyset_read_never_dials_another_group(self):
        self._assert_never_dialed("/api/live/readyset", self.readyset_conn(), suggested=True)

    def test_canary_fires_for_consented_cluster_probe(self):
        self._assert_dialed("/api/live/cluster", self.cluster_conn())

    def test_canary_fires_for_consented_replication_source_login(self):
        self._assert_dialed("/api/live/replication", self.replication_conn())

    def test_canary_fires_for_consented_proxysql_probe(self):
        self._assert_dialed("/api/live/proxysql", self.proxysql_conn())

    def test_canary_fires_for_consented_readyset_probe(self):
        self._assert_dialed("/api/live/readyset", self.readyset_conn())


def tearDownModule():
    for name in sorted(REPORT):
        seconds, accepted = REPORT[name]
        print("consent canary %-58s %6.3f s, listener accepted %d connection(s)"
              % (name, seconds, accepted))


if __name__ == "__main__":
    unittest.main()
