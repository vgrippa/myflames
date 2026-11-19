"""Live suite: Galera group connection routing on the mf-galera lab.

Ported from the ad-hoc live_group_routing_galera.py check. Covers: the group
is saved, each node's Cluster read records its own wsrep node identity, and
Desync and Resync of galera-2 through the group route to that peer's own
saved connection. The peer's action flags in the group report are not
state-gated (planning reads the peer's own wsrep_desync), so Desync and
Resync are verified on the peer's own wsrep_desync and
wsrep_local_state_comment. Resync runs after Desync also on failure.

Run: scripts/live-tests.sh galera   (or MYFLAMES_LIVE_LABS=1 with the lab up)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lab import (Lab, check_lab, cluster_action, conn_for, make_home,  # noqa: E402
                  member_named, redact, require_live, result_summary,
                  save_group, wait_for)

GROUP = "GAL-lab"


def setUpModule():
    require_live()


class GaleraGroupRoutingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        check_lab("galera")
        cls.home = make_home("galera")
        cls.lab = Lab()
        cls.gl = [cls.lab.profile("cluster-galera-%d" % i) for i in (1, 2, 3)]

    @classmethod
    def tearDownClass(cls):
        try:
            cls.lab.api("/api/editor/close", {"tab_id": "galera-peer",
                                              "connection": conn_for(cls.gl[1])})
        except Exception:
            pass

    def group_conn(self):
        return conn_for(self.gl[0], GROUP)

    def read_group(self):
        return self.lab.api("/api/live/cluster", {"connection": self.group_conn()})

    def test_01_group_saved(self):
        save_group(self.lab, GROUP, self.gl)
        self.gl = [self.lab.refresh(p) for p in self.gl]
        self.assertTrue(all(p.get("group") == GROUP for p in self.gl),
                        "group not saved on every node")

    def test_02_node_identities_recorded(self):
        for profile in self.gl:
            with self.subTest(profile=profile["name"]):
                report = self.lab.api("/api/live/cluster", {"connection": conn_for(profile)})
                local = next((m for m in report.get("members") or () if m.get("local")), {})
                recorded = self.lab.identity(profile, "wsrep_node_uuid")
                self.assertTrue(recorded, "no wsrep identity recorded for %s" % profile["name"])
                self.assertEqual(recorded, local.get("uuid"),
                                 "recorded identity is not the local node %s" % local.get("name"))

    def peer_state(self):
        """galera-2's own wsrep_desync and state comment, read on that node.

        The group report does not gate a peer's actions on state (planning
        reads the peer's own wsrep_desync), so the test asserts on the
        server's real state through the peer's own saved connection.
        """
        values = {}
        for show in ("SHOW GLOBAL VARIABLES LIKE 'wsrep_desync'",
                     "SHOW GLOBAL STATUS LIKE 'wsrep_local_state_comment'"):
            result = self.lab.api("/api/editor/run", {
                "tab_id": "galera-peer", "connection": conn_for(self.gl[1]),
                "sql": show, "wait": 15})
            if result.get("status") != "complete":
                return {"error": redact(result.get("message") or result.get("error") or
                                        repr(sorted(result))[:200])}
            for entry in (result.get("run") or {}).get("results") or ():
                if entry.get("type") != "rows":
                    continue
                names = [c.get("name") for c in entry.get("columns") or ()]
                for row in entry.get("rows") or ():
                    row = dict(zip(names, row))
                    if row.get("Variable_name"):
                        values[str(row["Variable_name"]).lower()] = row.get("Value")
        return {"desync": values.get("wsrep_desync"),
                "state": values.get("wsrep_local_state_comment")}

    def test_03_desync_and_resync_route_through_the_peers_own_connection(self):
        report = self.read_group()
        node = member_named(report, "galera-2")
        actions = {a["id"]: a for a in node.get("actions") or ()}
        desync = actions.get("desync_node") or {}
        self.assertTrue(desync.get("enabled"), redact(desync.get("why_not") or "desync_node not offered"))
        self.assertEqual((desync.get("target") or {}).get("profile_id"), self.gl[1]["id"],
                         "desync on galera-2 does not route to cluster-galera-2")
        self.addCleanup(self._ensure_resynced)
        result = self._run("desync_node", node)
        self.assertEqual(result.get("status"), "done", "desync not sent: %s" % result_summary(result))
        state = wait_for(self.peer_state,
                         until=lambda s: s.get("desync") == "ON",
                         timeout=60, what="galera-2 wsrep_desync=ON")
        self.assertEqual(state.get("state"), "Donor/Desynced",
                         "galera-2 desynced but its own state comment is %r" % state.get("state"))
        result = self._run("resync_node", node)
        self.assertEqual(result.get("status"), "done", "resync not sent: %s" % result_summary(result))
        wait_for(self.peer_state,
                 until=lambda s: s.get("desync") == "OFF" and s.get("state") == "Synced",
                 timeout=90, what="galera-2 back to Synced with wsrep_desync OFF")

    def _run(self, action, node):
        _reviewed, result = cluster_action(self.lab, self.group_conn(), action, node, self.gl[1])
        return result

    def _ensure_resynced(self):
        try:
            state = self.peer_state()
        except Exception:  # noqa: BLE001 - unreadable: try the resync anyway
            state = {}
        if state.get("desync") == "OFF":
            return
        node = member_named(self.read_group(), "galera-2")
        try:
            self._run("resync_node", node)
        except Exception as exc:  # noqa: BLE001 - already resynced is the goal
            if "wsrep_desync is not ON" in str(exc):
                return
            raise
        wait_for(self.peer_state,
                 until=lambda s: s.get("desync") == "OFF",
                 timeout=90, what="galera-2 resync to take effect")


if __name__ == "__main__":
    unittest.main()
