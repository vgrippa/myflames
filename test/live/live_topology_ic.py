"""Live suite: Topology place group reads and rolling PERSIST change on mf-ic.

Covers the Topology place against the InnoDB Cluster 8.4 lab (cluster-ic-1..3,
cluster-ic-router-rw, plus proxysql-admin when the ProxySQL lab is running):
- map: every member reached, one Group Replication / InnoDB Cluster with the
  three direct members verified by identity, router marked as an alias
  (duplicate_of) of a direct member so the group keeps one primary, and
  ProxySQL proxy_route links to the IC members when ProxySQL is present.
- members: one primary and two secondary roles, all ONLINE, one server version
  across all three, secondaries read_only and super_read_only, and transactions
  behind reported as a number (0 when idle).
- parameters, accounts, plugins: no failed member, identity settings folded as
  per_server, the router alias compared with the same values as its canonical
  member, and ProxySQL listed in not_compared with a reason.
- rolling change: read max_connect_errors on all three direct members, plan a
  PERSIST change to current + 1, verify secondaries precede the primary and a
  review token (plan_id) is issued, apply, re-read through parameters, verify
  replaying the same token is refused, then plan and apply back to the original
  value. Original state is restored in tearDownClass if a test fails midway.

Run: scripts/live-tests.sh topology   (or scripts/live-tests.sh ic)
"""
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lab import (Lab, LabError, check_lab, conn_for, make_home,  # noqa: E402
                  redact, require_live, save_group)

GROUP = "IC-topology-lab"
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
    return "live-topo-ic-%s-%s" % (tag, uuid.uuid4().hex[:12])


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


class TopologyICTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        check_lab("ic")
        cls.home = make_home("ic")
        cls.lab = Lab()
        cls.ic = [cls.lab.profile("cluster-ic-%d" % i) for i in (1, 2, 3)]
        cls.router = cls.lab.profile("cluster-ic-router-rw")
        try:
            cls.proxysql = cls.lab.profile("proxysql-admin")
        except LabError:
            cls.proxysql = None
        cls.members = list(cls.ic) + [cls.router] + ([cls.proxysql] if cls.proxysql is not None else [])
        cls._original_value = None
        cls._needs_restore = False
        save_group(cls.lab, GROUP, cls.members)
        cls._refresh_profiles()

    @classmethod
    def _refresh_profiles(cls):
        cls.ic = [cls.lab.refresh(p) for p in cls.ic]
        cls.router = cls.lab.refresh(cls.router)
        if cls.proxysql is not None:
            cls.proxysql = cls.lab.refresh(cls.proxysql)
        cls.members = list(cls.ic) + [cls.router] + ([cls.proxysql] if cls.proxysql is not None else [])

    @classmethod
    def tearDownClass(cls):
        if not cls._needs_restore or cls._original_value is None:
            return
        try:
            direct_ids = [p["id"] for p in cls.ic]
            conn = _change_conn(cls.ic[0], GROUP)
            plan = cls.lab.api("/api/live/topology/variable/plan", {
                "connection": conn, "name": PARAM, "value": cls._original_value,
                "mode": "persist", "member_ids": direct_ids,
            })
            payload = {
                "connection": conn, "plan_id": plan["plan_id"],
                "expect": plan["expect"], "confirmed": True,
                "progress_id": _progress_id("restore"),
            }
            if (plan.get("confirmation") or {}).get("typed_name"):
                payload["typed_name"] = GROUP
            cls.lab.api("/api/live/topology/variable/apply", payload)
            cls._needs_restore = False
        except Exception:
            pass

    def group_conn(self):
        return conn_for(self.ic[0], GROUP)

    def change_conn(self):
        return _change_conn(self.ic[0], GROUP)

    def direct_ids(self):
        return [p["id"] for p in self.ic]

    def read_section(self, section):
        return self.lab.api("/api/live/topology", {
            "connection": self.group_conn(), "section": section,
        })

    def _plan_change(self, value, member_ids=None):
        return self.lab.api("/api/live/topology/variable/plan", {
            "connection": self.change_conn(),
            "name": PARAM,
            "value": value,
            "mode": "persist",
            "member_ids": list(member_ids) if member_ids is not None else self.direct_ids(),
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
        save_group(self.lab, GROUP, self.members)
        self.__class__._refresh_profiles()
        self.assertTrue(all(p.get("group") == GROUP for p in self.members),
                        "group not saved on every member")
        generations = {p.get("group_generation") for p in self.members}
        self.assertEqual(len(generations), 1, "group_generation differs across members")
        self.assertTrue(next(iter(generations)), "empty group_generation on saved group")

    def test_02_map_cluster_and_router_alias(self):
        report = self.read_section("map")
        self.assertEqual(report.get("schema_version"), "topology-group-1.0")
        self.assertEqual(report.get("section"), "map")
        self.assertEqual(report.get("group_name"), GROUP)
        summary = report.get("summary") or {}
        self.assertEqual(summary.get("failed"), 0, redact(report.get("members")))
        self.assertEqual(summary.get("not_reached"), 0, redact(report.get("members")))
        self.assertEqual(summary.get("read"), len(self.members), redact(summary))

        direct = set(self.direct_ids())
        members = {m["profile_id"]: m for m in report.get("members") or ()}
        self.assertEqual(set(members), {p["id"] for p in self.members})
        for member in members.values():
            self.assertEqual(member.get("outcome"), "read",
                             "member %s not read: %s" % (member.get("profile_name"), redact(member.get("message"))))

        topo_map = report.get("map") or {}
        clusters = topo_map.get("clusters") or []
        self.assertEqual(len(clusters), 1, "expected one cluster box, got %d" % len(clusters))
        cluster = clusters[0]
        self.assertIn(cluster.get("kind"), ("innodb_cluster", "group_replication"),
                      "unexpected cluster kind: %r" % cluster.get("kind"))
        self.assertEqual(set(cluster.get("members") or ()), direct,
                         "cluster members do not match the three direct IC servers")
        self.assertEqual(set(cluster.get("seen_from") or ()), direct,
                         "not every direct IC server attested cluster membership")

        nodes = {n["id"]: n for n in topo_map.get("nodes") or ()}
        direct_uuids = set()
        for pid in direct:
            node = nodes.get(pid) or {}
            self.assertEqual(node.get("kind"), "member")
            self.assertEqual(node.get("cluster_id"), cluster["id"],
                             "direct member %s missing cluster_id" % node.get("label"))
            self.assertIsNone(node.get("duplicate_of"),
                              "direct member %s unexpectedly marked as alias" % node.get("label"))
            self.assertTrue(node.get("server_uuid"),
                            "direct member %s missing server_uuid" % node.get("label"))
            direct_uuids.add(node["server_uuid"])
        self.assertEqual(len(direct_uuids), 3, "direct IC members did not report three distinct server_uuids")

        # No peer node should represent any of the three direct IC members.
        peer_uuids = {n.get("server_uuid") for n in nodes.values() if n.get("kind") == "peer"}
        self.assertFalse(direct_uuids & peer_uuids,
                         "direct IC member appeared as an external peer node")

        # Router member is marked as an alias of a direct member, keeping one primary.
        router_id = self.router["id"]
        router_member = members[router_id]
        alias_target = router_member.get("duplicate_of")
        self.assertIn(alias_target, direct,
                      "router duplicate_of is %r, expected one of the direct IC members" % (alias_target,))
        router_node = nodes.get(router_id) or {}
        self.assertEqual(router_node.get("duplicate_of"), alias_target)
        self.assertIsNone(router_node.get("cluster_id"),
                          "router alias should not join the cluster box separately")
        alias_edges = [e for e in topo_map.get("edges") or ()
                       if e.get("from") == router_id and e.get("to") == alias_target and e.get("kind") == "alias"]
        self.assertEqual(len(alias_edges), 1, "missing alias edge from router to its canonical direct member")
        self.assertEqual(alias_edges[0].get("evidence"), "server_uuid")

        primary_id = report.get("primary_profile_id")
        self.assertIn(primary_id, direct, "primary_profile_id %r is not a direct IC member" % (primary_id,))
        primaries = [m["profile_id"] for m in members.values()
                     if m["profile_id"] in direct and m.get("role") == "primary"]
        self.assertEqual(primaries, [primary_id], "expected exactly one canonical primary")

    def test_03_map_proxysql_routes(self):
        if self.proxysql is None:
            self.skipTest("proxysql-admin profile is not present in the lab home")
        report = self.read_section("map")
        proxy_id = self.proxysql["id"]
        members = {m["profile_id"]: m for m in report.get("members") or ()}
        proxy_member = members.get(proxy_id) or {}
        self.assertEqual(proxy_member.get("outcome"), "read", redact(proxy_member.get("message")))
        self.assertEqual(proxy_member.get("role"), "proxy")
        self.assertEqual(proxy_member.get("product"), "ProxySQL Admin")

        edges = [e for e in (report.get("map") or {}).get("edges") or ()
                 if e.get("from") == proxy_id and e.get("kind") == "proxy_route"]
        targets = {e.get("to") for e in edges}
        direct = set(self.direct_ids())
        missing = direct - targets
        self.assertFalse(missing,
                         "ProxySQL map has no proxy_route edges to direct IC member(s) %s (observed targets: %s)"
                         % (sorted(missing), sorted(targets)))

    def test_04_members(self):
        report = self.read_section("members")
        summary = report.get("summary") or {}
        self.assertEqual(summary.get("failed"), 0, redact(summary))
        self.assertEqual(summary.get("not_reached"), 0, redact(summary))

        members = {m["profile_id"]: m for m in report.get("members") or ()}
        direct = [members[pid] for pid in self.direct_ids()]
        for member in direct:
            self.assertEqual(member.get("outcome"), "read", redact(member.get("message")))
            self.assertEqual(member.get("state"), "ONLINE",
                             "%s state is %r" % (member.get("profile_name"), member.get("state")))

        primaries = [m for m in direct if m.get("role") == "primary"]
        secondaries = [m for m in direct if m.get("role") == "secondary"]
        self.assertEqual(len(primaries), 1, "expected 1 primary, got %d" % len(primaries))
        self.assertEqual(len(secondaries), 2, "expected 2 secondaries, got %d" % len(secondaries))
        self.assertEqual(report.get("primary_profile_id"), primaries[0]["profile_id"])

        versions = {m.get("version") for m in direct}
        self.assertEqual(len(versions), 1, "direct IC members report multiple versions: %r" % (versions,))
        self.assertTrue(next(iter(versions)), "missing server version")

        self.assertIs(primaries[0].get("read_only"), False, "primary should not be read_only")
        self.assertIs(primaries[0].get("super_read_only"), False, "primary should not be super_read_only")
        for sec in secondaries:
            self.assertIs(sec.get("read_only"), True,
                          "secondary %s should be read_only" % sec.get("profile_name"))
            self.assertIs(sec.get("super_read_only"), True,
                          "secondary %s should be super_read_only" % sec.get("profile_name"))

        for member in direct:
            behind = member.get("behind")
            self.assertIsInstance(behind, dict,
                                  "%s missing transactions behind (reason: %s)"
                                  % (member.get("profile_name"), redact(member.get("behind_reason"))))
            self.assertEqual(behind.get("kind"), "mysql_gtid_transactions")
            value = behind.get("value")
            self.assertTrue(isinstance(value, int) and not isinstance(value, bool),
                            "%s behind value is not an int: %r" % (member.get("profile_name"), value))
            self.assertEqual(value, 0,
                             "%s expected 0 transactions behind when idle, got %r"
                             % (member.get("profile_name"), value))

    def test_05_parameters_accounts_plugins(self):
        direct = set(self.direct_ids())
        router_id = self.router["id"]
        proxy_id = self.proxysql["id"] if self.proxysql is not None else None
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
                compared = set(block.get("compared") or ())
                not_compared = {item["profile_id"]: item.get("reason") or ""
                                for item in block.get("not_compared") or ()}
                self.assertTrue(direct.issubset(compared),
                                "%s: direct IC members missing from compared: %s"
                                % (section, sorted(direct - compared)))
                self.assertIn(router_id, compared,
                              "%s: router alias missing from compared" % section)
                members = {member["profile_id"]: member for member in report.get("members") or ()}
                canonical_id = members[router_id].get("duplicate_of")
                self.assertIn(canonical_id, direct,
                              "%s: router alias does not duplicate a direct IC member" % section)
                if proxy_id is not None:
                    self.assertNotIn(proxy_id, compared,
                                     "%s: ProxySQL should not be in compared" % section)
                    self.assertIn(proxy_id, not_compared,
                                  "%s: ProxySQL missing from not_compared" % section)
                    self.assertTrue(not_compared[proxy_id].strip(),
                                    "%s: ProxySQL not_compared entry lacks a reason" % section)

                rows = block.get("rows") or []
                self.assertTrue(rows, "%s returned no rows" % section)
                for row in rows:
                    if section == "accounts":
                        present = set(row.get("present_on") or ())
                        self.assertEqual(router_id in present, canonical_id in present,
                                         "accounts: alias and canonical member disagree on account presence")
                        differences = row.get("differences") or ()
                        self.assertEqual(
                            [(item.get("kind"), item.get("detail")) for item in differences
                             if item.get("profile_id") == router_id],
                            [(item.get("kind"), item.get("detail")) for item in differences
                             if item.get("profile_id") == canonical_id],
                            "accounts: alias and canonical member have different account values")
                    else:
                        values = row.get("values") or {}
                        self.assertIn(router_id, values)
                        self.assertIn(canonical_id, values)
                        self.assertEqual(values[router_id], values[canonical_id],
                                         "%s: alias and canonical member differ for %s"
                                         % (section, row.get("name")))
                if section == "parameters":
                    by_name = {row["name"]: row for row in rows}
                    for name in ("server_id", "server_uuid", "hostname"):
                        self.assertIn(name, by_name, "identity setting %s missing from parameters" % name)
                        self.assertEqual(by_name[name].get("status"), "per_server",
                                         "%s status is %r, expected per_server" % (name, by_name[name].get("status")))

    def test_06_rolling_change_persist_and_restore(self):
        members_report = self.read_section("members")
        direct = set(self.direct_ids())
        primary_id = members_report.get("primary_profile_id")
        self.assertIn(primary_id, direct, "no direct primary identified")
        secondary_ids = direct - {primary_id}
        self.assertEqual(len(secondary_ids), 2)

        before_report = self.read_section("parameters")
        before_values = _param_values(before_report, PARAM, self.direct_ids())
        self.assertEqual(len(set(before_values.values())), 1,
                         "expected uniform initial %s across direct members, got %r" % (PARAM, before_values))
        original = next(iter(before_values.values()))
        target = original + 1
        self.__class__._original_value = original
        self.__class__._needs_restore = True

        # Plan PERSIST change to current + 1 on all three direct members.
        plan = self._plan_change(target)
        self.assertEqual(plan.get("schema_version"), "topology-change-plan-1.0")
        self.assertTrue(plan.get("plan_id"), "rolling change plan did not issue a review token (plan_id)")
        planned_members = plan.get("members") or []
        self.assertEqual(len(planned_members), 3)
        ordered_ids = [m["profile_id"] for m in planned_members]
        self.assertEqual(set(ordered_ids[:2]), secondary_ids,
                         "plan order did not put both secondaries first: %r" % (ordered_ids,))
        self.assertEqual(ordered_ids[2], primary_id,
                         "plan order did not put primary last: %r" % (ordered_ids,))
        for member in planned_members:
            self.assertEqual(member.get("status"), "planned",
                             "%s not planned: %s" % (member.get("profile_name"), redact(member.get("reason"))))
            self.assertEqual(member.get("effective_mode"), "persist")
            self.assertIn("SET PERSIST", member.get("statement") or "")

        # Apply and verify every member applied and parameters reflects target.
        result = self._apply_change(plan, tag="up")
        self.assertEqual(result.get("schema_version"), "topology-change-1.0")
        self.assertEqual(result.get("status"), "applied",
                         "rolling apply did not succeed: %s" % redact(result.get("message")))
        self.assertEqual([m["profile_id"] for m in result.get("members") or ()], ordered_ids)
        for member in result.get("members") or ():
            self.assertEqual(member.get("status"), "applied",
                             "%s status was %r: %s"
                             % (member.get("profile_name"), member.get("status"), redact(member.get("message"))))

        after_up = _param_values(self.read_section("parameters"), PARAM, self.direct_ids())
        self.assertEqual(after_up, {pid: target for pid in self.direct_ids()},
                         "re-read %s does not match target %d: %r" % (PARAM, target, after_up))

        # Replaying the same review token must be refused.
        replay = self._apply_change(plan, tag="replay")
        self.assertIn(replay.get("status"), ("stale", "refused"),
                      "replaying a consumed review token was not refused: %s" % redact(replay.get("status")))
        self.assertTrue(all(m.get("status") != "applied" for m in replay.get("members") or ()),
                        "replayed token applied a member")

        # Plan and apply the change back to the original value.
        restore_plan = self._plan_change(original)
        self.assertTrue(restore_plan.get("plan_id"), "restore plan did not issue a review token")
        restore_ids = [m["profile_id"] for m in restore_plan.get("members") or ()]
        self.assertEqual(set(restore_ids[:2]), secondary_ids)
        self.assertEqual(restore_ids[2], primary_id)
        restore_result = self._apply_change(restore_plan, tag="down")
        self.assertEqual(restore_result.get("status"), "applied",
                         "rolling restore did not succeed: %s" % redact(restore_result.get("message")))
        for member in restore_result.get("members") or ():
            self.assertEqual(member.get("status"), "applied",
                             "%s restore status was %r: %s"
                             % (member.get("profile_name"), member.get("status"), redact(member.get("message"))))

        after_down = _param_values(self.read_section("parameters"), PARAM, self.direct_ids())
        self.assertEqual(after_down, {pid: original for pid in self.direct_ids()},
                         "re-read %s was not restored to %d: %r" % (PARAM, original, after_down))
        self.__class__._needs_restore = False


if __name__ == "__main__":
    unittest.main()
