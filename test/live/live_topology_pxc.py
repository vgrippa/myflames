"""Live suite: Topology place group reads and rolling SET GLOBAL on mf-pxc.

Mirrors live_topology_galera.py for the Percona XtraDB Cluster 8.4 lab
(cluster-pxc-1..3):
- map: every node reached, one PXC cluster box with all three nodes
  self-attested and linked by wsrep node identity (no unmatched peers).
- members: all three nodes Synced, role node, one version.
- parameters, accounts, plugins: no failed member, all three nodes compared,
  identity settings folded as per_server, auto_increment_offset folded per
  server and auto_increment_increment the same on every node
  (wsrep_auto_increment_control sets both), never reported as drift.
- rolling change: read max_connect_errors on all three nodes, plan a SET GLOBAL
  change to current + 1, verify node-name order and each plan member's [mysqld]
  option-file line (config_line), apply, re-read through parameters, verify
  token replay is refused, then plan and apply back to the original value.
  Original state is restored in tearDownClass if a test fails midway.

Run: scripts/live-tests.sh topology   (or scripts/live-tests.sh pxc)
"""
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lab import (Lab, check_lab, conn_for, make_home,  # noqa: E402
                  redact, require_live, save_group)

GROUP = "PXC-topology-lab"
PARAM = "max_connect_errors"


def setUpModule():
    require_live()


def _change_conn(profile, group):
    """Group connection payload matching /api/live/topology/variable/* keys."""
    conn = conn_for(profile, group)
    conn.pop("password", None)
    if "port" in conn and conn["port"] is not None:
        conn["port"] = int(conn["port"])
    if "ssh_port" in conn and conn["ssh_port"] is not None:
        conn["ssh_port"] = int(conn["ssh_port"])
    return conn


def _progress_id(tag):
    return "live-topo-pxc-%s-%s" % (tag, uuid.uuid4().hex[:12])


def _param_values(report, name, profile_ids):
    rows = {row["name"]: row for row in (report.get("parameters") or {}).get("rows") or ()}
    if name not in rows:
        raise AssertionError("parameter %s missing from topology parameters report" % name)
    values = rows[name].get("values") or {}
    out = {}
    for pid in profile_ids:
        raw = values.get(pid)
        if raw is None:
            raise AssertionError("parameter %s has no value for profile %s" % (name, pid))
        out[pid] = int(str(raw).strip())
    return out


class TopologyPxcTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        check_lab("pxc")
        cls.home = make_home("pxc")
        cls.lab = Lab()
        cls.pxc = [cls.lab.profile("cluster-pxc-%d" % i) for i in (1, 2, 3)]
        cls._original_value = None
        cls._needs_restore = False
        save_group(cls.lab, GROUP, cls.pxc)
        cls.pxc = [cls.lab.refresh(p) for p in cls.pxc]

    @classmethod
    def tearDownClass(cls):
        if not cls._needs_restore or cls._original_value is None:
            return
        node_ids = [p["id"] for p in cls.pxc]
        names = dict((p["id"], p.get("name") or p["id"]) for p in cls.pxc)
        conn = _change_conn(cls.pxc[0], GROUP)
        try:
            plan = cls.lab.api("/api/live/topology/variable/plan", {
                "connection": conn, "name": PARAM, "value": cls._original_value,
                "mode": "global", "member_ids": node_ids,
            })
            payload = {
                "connection": conn, "plan_id": plan["plan_id"],
                "expect": plan["expect"], "confirmed": True,
                "progress_id": _progress_id("restore"),
            }
            if (plan.get("confirmation") or {}).get("typed_name"):
                payload["typed_name"] = GROUP
            result = cls.lab.api("/api/live/topology/variable/apply", payload)
        except Exception as exc:
            raise RuntimeError("cleanup failed: could not restore %s=%s on %s: %s"
                               % (PARAM, cls._original_value, GROUP, redact(exc)))
        problems = []
        if result.get("status") != "applied":
            problems.append("restore apply status %r (%s)"
                            % (result.get("status"), redact(result.get("message"))))
        for member in result.get("members") or ():
            if member.get("status") != "applied":
                problems.append("%s: apply status %r (%s)"
                                % (member.get("profile_name"), member.get("status"),
                                   redact(member.get("message"))))
        try:
            report = cls.lab.api("/api/live/topology", {
                "connection": conn_for(cls.pxc[0], GROUP), "section": "parameters",
            })
            values = _param_values(report, PARAM, node_ids)
        except Exception as exc:
            raise RuntimeError("cleanup failed: could not re-read %s on %s after restore: %s"
                               % (PARAM, GROUP, redact(exc)))
        for pid, value in sorted(values.items()):
            if value != cls._original_value:
                problems.append("%s: %s=%s, want %s"
                                % (names.get(pid, pid), PARAM, value, cls._original_value))
        if problems:
            raise RuntimeError("cleanup failed: %s not restored on %s: %s"
                               % (PARAM, GROUP, "; ".join(problems)))
        cls._needs_restore = False

    def group_conn(self):
        return conn_for(self.pxc[0], GROUP)

    def change_conn(self):
        return _change_conn(self.pxc[0], GROUP)

    def node_ids(self):
        return [p["id"] for p in self.pxc]

    def read_section(self, section):
        return self.lab.api("/api/live/topology", {
            "connection": self.group_conn(), "section": section,
        })

    def _plan_change(self, value, member_ids=None):
        return self.lab.api("/api/live/topology/variable/plan", {
            "connection": self.change_conn(),
            "name": PARAM,
            "value": value,
            "mode": "global",
            "member_ids": list(member_ids) if member_ids is not None else self.node_ids(),
        })

    def _apply_change(self, plan, tag="apply"):
        payload = {
            "connection": self.change_conn(),
            "plan_id": plan["plan_id"],
            "expect": plan["expect"],
            "confirmed": True,
            "progress_id": _progress_id(tag),
        }
        if (plan.get("confirmation") or {}).get("typed_name"):
            payload["typed_name"] = GROUP
        return self.lab.api("/api/live/topology/variable/apply", payload)

    def test_01_group_saved(self):
        save_group(self.lab, GROUP, self.pxc)
        self.__class__.pxc = [self.lab.refresh(p) for p in self.pxc]
        self.assertTrue(all(p.get("group") == GROUP for p in self.pxc),
                        "group not saved on every PXC node")
        generations = {p.get("group_generation") for p in self.pxc}
        self.assertEqual(len(generations), 1, "group_generation differs across PXC nodes")
        self.assertTrue(next(iter(generations)), "empty group_generation on saved group")

    def test_02_map_pxc_cluster(self):
        report = self.read_section("map")
        self.assertEqual(report.get("schema_version"), "topology-group-1.0")
        self.assertEqual(report.get("section"), "map")
        self.assertEqual(report.get("group_name"), GROUP)
        summary = report.get("summary") or {}
        self.assertEqual(summary.get("failed"), 0, redact(summary))
        self.assertEqual(summary.get("not_reached"), 0, redact(summary))
        self.assertEqual(summary.get("read"), 3, redact(summary))

        ids = set(self.node_ids())
        members = {m["profile_id"]: m for m in report.get("members") or ()}
        self.assertEqual(set(members), ids)
        for member in members.values():
            self.assertEqual(member.get("outcome"), "read",
                             "node %s not read: %s" % (member.get("profile_name"), redact(member.get("message"))))

        topo_map = report.get("map") or {}
        clusters = topo_map.get("clusters") or []
        self.assertEqual(len(clusters), 1, "expected one PXC cluster box, got %d" % len(clusters))
        cluster = clusters[0]
        self.assertEqual(cluster.get("kind"), "pxc")
        self.assertTrue((cluster.get("id") or "").startswith("wsrep:"),
                        "unexpected PXC cluster id: %r" % cluster.get("id"))
        # Each saved node only enters cluster.members and seen_from after
        # proving its own wsrep_gcomm_uuid in the membership view.
        self.assertEqual(set(cluster.get("members") or ()), ids,
                         "PXC cluster members do not match the three saved nodes")
        self.assertEqual(set(cluster.get("seen_from") or ()), ids,
                         "not every PXC node attested cluster membership")

        nodes = topo_map.get("nodes") or []
        peers = [n for n in nodes if n.get("kind") == "peer"]
        self.assertEqual(peers, [], "unexpected external peer nodes in PXC map: %r" % (peers,))
        by_id = {n["id"]: n for n in nodes}
        self.assertEqual(set(by_id), ids)
        for pid in ids:
            node = by_id[pid]
            self.assertEqual(node.get("cluster_id"), cluster["id"])
            self.assertIsNone(node.get("duplicate_of"))
            self.assertEqual(node.get("engine"), "mysql")

    def test_03_members_all_synced(self):
        report = self.read_section("members")
        summary = report.get("summary") or {}
        self.assertEqual(summary.get("failed"), 0, redact(summary))
        self.assertEqual(summary.get("not_reached"), 0, redact(summary))

        members = report.get("members") or []
        self.assertEqual(len(members), 3)
        versions = set()
        for member in members:
            self.assertEqual(member.get("outcome"), "read", redact(member.get("message")))
            self.assertEqual(member.get("engine"), "mysql")
            self.assertEqual(member.get("role"), "node")
            self.assertEqual(member.get("state"), "Synced",
                             "node %s state is %r" % (member.get("profile_name"), member.get("state")))
            lag = member.get("lag") or {}
            self.assertEqual(lag.get("kind"), "galera_recv_queue")
            self.assertIsNone(member.get("behind"))
            self.assertTrue(member.get("behind_reason"), "PXC node missing behind_reason")
            versions.add(member.get("version"))
        self.assertEqual(len(versions), 1, "PXC nodes report multiple versions: %r" % (versions,))
        self.assertTrue(next(iter(versions)), "missing PXC version")

    def test_04_parameters_accounts_plugins(self):
        ids = set(self.node_ids())
        for section in ("parameters", "accounts", "plugins"):
            with self.subTest(section=section):
                report = self.read_section(section)
                summary = report.get("summary") or {}
                self.assertEqual(summary.get("failed"), 0, redact(summary))
                self.assertEqual(summary.get("not_reached"), 0, redact(summary))
                for member in report.get("members") or ():
                    self.assertEqual(member.get("outcome"), "read",
                                     "%s: %s not read: %s"
                                     % (section, member.get("profile_name"), redact(member.get("message"))))
                block = report.get(section) or {}
                self.assertEqual(set(block.get("compared") or ()), ids,
                                 "%s: not all PXC nodes compared" % section)
                self.assertEqual(block.get("not_compared") or [], [],
                                 "%s: unexpected not_compared entries: %s"
                                 % (section, redact(block.get("not_compared"))))
                rows = block.get("rows") or []
                self.assertTrue(rows, "%s returned no rows" % section)
                if section == "parameters":
                    by_name = {row["name"]: row for row in rows}
                    for name in ("server_id", "hostname", "wsrep_node_name"):
                        self.assertIn(name, by_name, "identity setting %s missing from parameters" % name)
                        self.assertEqual(by_name[name].get("status"), "per_server",
                                         "%s status is %r, expected per_server" % (name, by_name[name].get("status")))
                    # wsrep_auto_increment_control sets the increment to the
                    # cluster size (the same on every node) and the offset from
                    # the node index (MariaDB sql/wsrep_server_service.cc:231-236).
                    # param_compare folds only the generated offset per server
                    # (param_compare.py _group_auto_increment); an increment
                    # difference stays real drift.
                    expected = {"auto_increment_increment": "same", "auto_increment_offset": "per_server"}
                    for name, status in sorted(expected.items()):
                        with self.subTest(parameter=name):
                            self.assertIn(name, by_name, "%s missing from parameters" % name)
                            self.assertEqual(by_name[name].get("status"), status,
                                             "%s status is %r, expected %s (wsrep_auto_increment_control)"
                                             % (name, by_name[name].get("status"), status))

    def test_05_rolling_change_global_and_restore(self):
        before_values = _param_values(self.read_section("parameters"), PARAM, self.node_ids())
        self.assertEqual(len(set(before_values.values())), 1,
                         "expected uniform initial %s across PXC nodes, got %r" % (PARAM, before_values))
        original = next(iter(before_values.values()))
        target = original + 1
        self.__class__._original_value = original
        self.__class__._needs_restore = True

        # Send member_ids in reverse order to verify the planner sorts by node name.
        reversed_ids = list(reversed(self.node_ids()))
        plan = self._plan_change(target, member_ids=reversed_ids)
        self.assertEqual(plan.get("schema_version"), "topology-change-plan-1.0")
        self.assertTrue(plan.get("plan_id"), "rolling change plan did not issue a review token (plan_id)")
        self.assertIn("node name", (plan.get("order_rule") or "").lower(),
                      "unexpected PXC order_rule: %r" % plan.get("order_rule"))

        planned_members = plan.get("members") or []
        self.assertEqual(len(planned_members), 3)
        planned_names = [m.get("profile_name") for m in planned_members]
        self.assertEqual(planned_names, sorted(planned_names, key=str.lower),
                         "PXC plan members are not in node-name order: %r" % (planned_names,))

        expected_line = "%s = %d" % (PARAM, target)
        expected_stmt = "SET GLOBAL %s = %d" % (PARAM, target)
        ordered_ids = [m["profile_id"] for m in planned_members]
        for member in planned_members:
            self.assertEqual(member.get("status"), "planned",
                             "%s not planned: %s" % (member.get("profile_name"), redact(member.get("reason"))))
            self.assertEqual(member.get("effective_mode"), "global")
            self.assertEqual(member.get("statement"), expected_stmt)
            self.assertEqual(member.get("config_line"), expected_line,
                             "%s missing [mysqld] option-file line %r (got %r)"
                             % (member.get("profile_name"), expected_line, member.get("config_line")))

        result = self._apply_change(plan, tag="up")
        self.assertEqual(result.get("schema_version"), "topology-change-1.0")
        self.assertEqual(result.get("status"), "applied",
                         "rolling apply did not succeed: %s" % redact(result.get("message")))
        self.assertEqual([m["profile_id"] for m in result.get("members") or ()], ordered_ids)
        for member in result.get("members") or ():
            self.assertEqual(member.get("status"), "applied",
                             "%s status was %r: %s"
                             % (member.get("profile_name"), member.get("status"), redact(member.get("message"))))

        after_up = _param_values(self.read_section("parameters"), PARAM, self.node_ids())
        self.assertEqual(after_up, {pid: target for pid in self.node_ids()},
                         "re-read %s does not match target %d: %r" % (PARAM, target, after_up))

        replay = self._apply_change(plan, tag="replay")
        self.assertIn(replay.get("status"), ("stale", "refused"),
                      "replaying a consumed review token was not refused: %s" % redact(replay.get("status")))
        self.assertTrue(all(m.get("status") != "applied" for m in replay.get("members") or ()),
                        "replayed token applied a node")

        # Plan and apply back to the original value.
        restore_plan = self._plan_change(original, member_ids=reversed_ids)
        self.assertTrue(restore_plan.get("plan_id"))
        self.assertEqual([m["profile_id"] for m in restore_plan.get("members") or ()], ordered_ids)
        restore_line = "%s = %d" % (PARAM, original)
        for member in restore_plan.get("members") or ():
            self.assertEqual(member.get("config_line"), restore_line)
        restore_result = self._apply_change(restore_plan, tag="down")
        self.assertEqual(restore_result.get("status"), "applied",
                         "rolling restore did not succeed: %s" % redact(restore_result.get("message")))
        for member in restore_result.get("members") or ():
            self.assertEqual(member.get("status"), "applied",
                             "%s restore status was %r: %s"
                             % (member.get("profile_name"), member.get("status"), redact(member.get("message"))))

        after_down = _param_values(self.read_section("parameters"), PARAM, self.node_ids())
        self.assertEqual(after_down, {pid: original for pid in self.node_ids()},
                         "re-read %s was not restored to %d: %r" % (PARAM, original, after_down))
        self.__class__._needs_restore = False


if __name__ == "__main__":
    unittest.main()
