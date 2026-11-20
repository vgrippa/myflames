"""Live suite: InnoDB Cluster group connection routing on the mf-ic lab.

Ported from the ad-hoc live_group_routing.py and live_group_router.py checks.
Covers: the group is saved, each member's Cluster read records its own
identity, Stop and Start Group Replication on mf-ic-2 route through that
member's own saved connection, and two saved connections claiming one member
fail closed. Server state is restored (Start after Stop) also on failure.

Run: scripts/live-tests.sh ic   (or MYFLAMES_LIVE_LABS=1 with the lab up)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lab import (Lab, check_lab, cluster_action, conn_for,  # noqa: E402
                  make_home, member_named, redact, require_live,
                  result_summary, save_group, wait_for)

GROUP = "IC-lab"


def setUpModule():
    require_live()


class GroupRoutingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        check_lab("ic")
        cls.home = make_home("ic")
        cls.lab = Lab()
        cls.ic = [cls.lab.profile("cluster-ic-%d" % i) for i in (1, 2, 3)]

    def group_conn(self):
        return conn_for(self.ic[0], GROUP)

    def read_group(self):
        return self.lab.api("/api/live/cluster", {"connection": self.group_conn()})

    def state_of(self, needle):
        return member_named(self.read_group(), needle).get("state")

    def test_01_group_saved(self):
        save_group(self.lab, GROUP, self.ic)
        self.ic = [self.lab.refresh(p) for p in self.ic]
        self.assertTrue(all(p.get("group") == GROUP for p in self.ic),
                        "group not saved on every member")

    def test_02_identities_recorded(self):
        for profile in self.ic:
            with self.subTest(profile=profile["name"]):
                report = self.lab.api("/api/live/cluster", {"connection": conn_for(profile)})
                local = next((m for m in report.get("members") or () if m.get("local")), {})
                recorded = self.lab.identity(profile)
                self.assertTrue(recorded, "no identity recorded for %s" % profile["name"])
                self.assertEqual(recorded, local.get("uuid"),
                                 "recorded identity is not the local member %s" % local.get("name"))

    def test_03_stop_and_start_member_routes_through_its_own_connection(self):
        report = self.read_group()
        m2 = member_named(report, "mf-ic-2")
        stop = {a["id"]: a for a in m2.get("actions") or ()}.get("stop_member") or {}
        self.assertTrue(stop.get("enabled"), redact(stop.get("why_not") or "stop_member not offered"))
        self.assertEqual((stop.get("target") or {}).get("profile_id"), self.ic[1]["id"],
                         "stop on mf-ic-2 does not route to cluster-ic-2")
        # Restore even when a later assertion fails: mf-ic-2 rejoins the group.
        self.addCleanup(self._ensure_member_2_online)
        _reviewed, result = cluster_action(self.lab, self.group_conn(), "stop_member", m2, self.ic[1])
        self.assertTrue(result.get("status") in ("done", "sent", "ok") or result.get("ok") is True,
                        "stop not sent: %s" % result_summary(result))
        wait_for(lambda: self.state_of("mf-ic-2"), until=lambda s: s != "ONLINE",
                 timeout=90, what="mf-ic-2 to leave the group")
        m2 = member_named(self.read_group(), "mf-ic-2")
        _reviewed, result = cluster_action(self.lab, self.group_conn(), "start_member", m2, self.ic[1])
        self.assertTrue(result.get("status") in ("done", "sent", "ok") or result.get("ok") is True,
                        "start not sent: %s" % result_summary(result))
        self._ensure_member_2_online()

    def _ensure_member_2_online(self):
        state = self.state_of("mf-ic-2")
        if state == "ONLINE":
            return
        m2 = member_named(self.read_group(), "mf-ic-2")
        start = {a["id"]: a for a in m2.get("actions") or ()}.get("start_member") or {}
        if start.get("enabled"):
            cluster_action(self.lab, self.group_conn(), "start_member", m2, self.ic[1])
        wait_for(lambda: self.state_of("mf-ic-2"), until=lambda s: s == "ONLINE",
                 timeout=150, interval=4, what="mf-ic-2 back ONLINE")

    def test_04_two_connections_claiming_one_member_fail_closed(self):
        # The router always lands on the primary, so after it joins the group
        # both cluster-ic-1 and cluster-ic-router-rw claim mf-ic-1.
        router = self.lab.profile("cluster-ic-router-rw")
        save_group(self.lab, GROUP, self.ic + [router], previous_name=GROUP)
        self.addCleanup(save_group, self.lab, GROUP, self.ic, GROUP)
        report = self.read_group()
        m1 = member_named(report, "mf-ic-1")
        stop = {a["id"]: a for a in m1.get("actions") or ()}.get("stop_member") or {}
        self.assertFalse(stop.get("enabled"),
                         "primary's stop stayed enabled with two connections claiming it")
        self.assertIn("both reach this member", stop.get("why_not") or "",
                      "unexpected why_not: %s" % redact(stop.get("why_not") or ""))
        m3 = member_named(report, "mf-ic-3")
        stop3 = {a["id"]: a for a in m3.get("actions") or ()}.get("stop_member") or {}
        self.assertTrue(stop3.get("enabled"), redact(stop3.get("why_not") or "stop_member not offered"))
        self.assertEqual((stop3.get("target") or {}).get("profile_id"), self.ic[2]["id"],
                         "a secondary's action no longer routes to its own connection")
        with self.assertRaises(Exception, msg="a plan was issued for the ambiguous member"):
            self.lab.api("/api/live/cluster/plan", {
                "connection": self.group_conn(), "action": "stop_member",
                "member_key": m1["key"], "target_profile_id": router["id"]})


if __name__ == "__main__":
    unittest.main()
