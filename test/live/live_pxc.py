"""Live suite: Percona XtraDB Cluster reads on the mf-pxc lab.

Mirrors live_galera.py for the PXC 8.4 cluster (cluster-pxc-1..3), read-only:
- the group is saved,
- each node's Cluster read reports kind pxc, size 3, this node local and the
  other two with their state from performance_schema.pxc_cluster_view,
- each node's wsrep gcomm UUID is recorded as its topology identity,
- the readiness endpoint reports galera_member for every node.

PXC has no Cluster actions (desync/resync/bootstrap are MariaDB Galera only),
so the suite asserts the action lists are empty, planning refuses forged
requests, and wsrep_desync stays OFF on every node.

Run: scripts/live-tests.sh pxc   (or MYFLAMES_LIVE_LABS=1 with the lab up)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lab import (Lab, LabError, check_lab, conn_for, make_home,  # noqa: E402
                  redact, require_live, save_group)

GROUP = "PXC-lab"


def setUpModule():
    require_live()


class PxcClusterReadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        check_lab("pxc")
        cls.home = make_home("pxc")
        cls.lab = Lab()
        cls.pxc = [cls.lab.profile("cluster-pxc-%d" % i) for i in (1, 2, 3)]

    def test_01_group_saved(self):
        save_group(self.lab, GROUP, self.pxc)
        self.__class__.pxc = [self.lab.refresh(p) for p in self.pxc]
        self.assertTrue(all(p.get("group") == GROUP for p in self.pxc),
                        "group not saved on every PXC node")

    def test_02_cluster_read_reports_pxc_members(self):
        for profile in self.pxc:
            with self.subTest(profile=profile["name"]):
                report = self.lab.api("/api/live/cluster", {"connection": conn_for(profile)})
                self.assertEqual(report.get("kind"), "pxc",
                                 "kind is %r: %s" % (report.get("kind"), redact(report.get("message"))))
                self.assertEqual(report.get("status"), "ok",
                                 "status is %r: %s" % (report.get("status"), redact(report.get("message"))))
                self.assertEqual((report.get("cluster") or {}).get("size"), 3)
                members = report.get("members") or ()
                self.assertEqual(len(members), 3)
                local = [m for m in members if m.get("local")]
                self.assertEqual(len(local), 1, "expected exactly one local member")
                local = local[0]
                others = [m for m in members if not m.get("local")]
                self.assertEqual(len(others), 2)
                for member in others:
                    self.assertEqual((member.get("state"), member.get("pxc_status"),
                                      member.get("state_source"), member.get("status")),
                                     ("Synced", "SYNCED", "pxc_cluster_view", "ok"),
                                     "peer %s state is not from pxc_cluster_view" % member.get("name"))
                recorded = self.lab.identity(profile, "wsrep_node_uuid")
                self.assertTrue(recorded, "no wsrep identity recorded for %s" % profile["name"])
                self.assertEqual(recorded, local.get("uuid"),
                                 "recorded identity is not the local node %s" % local.get("name"))

    def test_03_readiness_reports_galera_member(self):
        for profile in self.pxc:
            with self.subTest(profile=profile["name"]):
                readiness = self.lab.api("/api/live/readiness", {"connection": conn_for(profile)})
                self.assertIs(readiness.get("galera_member"), True,
                              "galera_member is %r on %s"
                              % (readiness.get("galera_member"), profile["name"]))

    def test_04_pxc_has_no_member_actions(self):
        connection = conn_for(self.pxc[0], GROUP)
        report = self.lab.api("/api/live/cluster", {"connection": connection})
        self.assertEqual(report.get("kind"), "pxc",
                         "kind is %r: %s" % (report.get("kind"), redact(report.get("message"))))
        members = report.get("members") or ()
        self.assertEqual(len(members), 3, "expected all three PXC members")
        for member in members:
            self.assertEqual(member.get("actions"), [],
                             "PXC member %s must have an empty action list" % member.get("name"))
        self.assertEqual(report.get("actions"), [], "PXC overview must have an empty action list")
        for member in members:
            for action in ("desync_node", "resync_node", "bootstrap_pc"):
                for target in (None, self.pxc[1]["id"]):
                    with self.subTest(member=member["name"], action=action, target=target):
                        payload = {"connection": connection, "action": action,
                                   "member_key": member["key"]}
                        if target is not None:
                            payload["target_profile_id"] = target
                        with self.assertRaisesRegex(
                                LabError, r"^That change is not offered for this member now\."):
                            self.lab.api("/api/live/cluster/plan", payload)
        # Each read uses that node's own connection, so a peer's state cannot
        # stand in for the wsrep_desync value on another node.
        for profile in self.pxc:
            with self.subTest(profile=profile["name"]):
                own = self.lab.api("/api/live/cluster", {"connection": conn_for(profile)})
                self.assertEqual((own.get("galera") or {}).get("desync"), "OFF",
                                 "wsrep_desync changed on %s" % profile["name"])


if __name__ == "__main__":
    unittest.main()
