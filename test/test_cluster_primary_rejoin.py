"""Primary ClusterSet member rejoin and conservative dissolution outcomes.

SQL answers are synthetic, but the reader, catalog, planner and reviewed
execution gates are the production implementations. Live evidence is kept
separately by scripts/clusterset-live.py.
"""
import copy
import importlib.util
from pathlib import Path
import unittest

import _cluster_fixtures as F
import test_cluster_shell as shell_fixtures
import test_clusterset_execution as execution_fixtures
from myflames import cluster


def primary_with_missing_member():
    """Read real projection shapes instead of fabricating an OK status field."""
    conn = F.conn("ic-8.4-clusterset-primary")
    tables = conn.scenario["tables"]
    tables["performance_schema.replication_group_members"].pop()
    tables["performance_schema.replication_group_member_actions"] = [[1]]
    tables["performance_schema.replication_connection_status"] = []
    return cluster.read_status(conn)


class PrimaryRejoinTests(unittest.TestCase):
    def member(self, report):
        return next(item for item in report["members"] if item["state"] == "MISSING")

    def action(self, report):
        return next(item for item in cluster.available_actions(
            report, self.member(report)["key"], shell_fixtures.CHECKS)
                    if item["id"] == "rejoin_instance")

    def assert_refused(self, report):
        self.assertFalse(cluster._primary_clusterset_rejoin_ok(report))
        action = self.action(report)
        self.assertFalse(action["enabled"], action)
        with self.assertRaises(cluster.ActionUnavailable):
            cluster.plan_action(report, "rejoin_instance", self.member(report)["key"],
                                shell_fixtures.CHECKS)

    def test_primary_member_rejoin_is_catalogued_and_planned_without_global_status(self):
        report = primary_with_missing_member()
        self.assertNotIn("global_status", report["clusterset"])
        self.assertTrue(cluster._primary_clusterset_rejoin_ok(report))
        self.assertTrue(self.action(report)["enabled"])
        member = self.member(report)
        plan = cluster.plan_action(report, "rejoin_instance", member["key"], shell_fixtures.CHECKS)
        self.assertEqual(plan["member_key"], member["key"])
        self.assertIn("rejoinInstance", plan["statements"][0])
        self.assertIn(member["address"], plan["statements"][0])
        self.assertIn('recoveryMethod: "incremental"', plan["statements"][0])

    def test_primary_proof_requires_complete_current_local_identity_and_health(self):
        changes = (
            ("server", "server_uuid", None),
            ("set", "invalidated", None),
            ("set", "invalidated", True),
            ("set", "from_local_copy", None),
            ("set", "from_local_copy", True),
            ("set", "local_role", "REPLICA"),
            ("set", "local_cluster", None),
            ("set", "primary_cluster", "another-cluster"),
            ("cluster", "local", False),
            ("cluster", "role", "REPLICA"),
            ("cluster", "invalidated", True),
            ("cluster", "read", "route"),
            ("cluster", "read_error", "The route could not be verified"),
            ("cluster", "quorum", None),
            ("cluster", "quorum", False),
            ("route", "self", False),
            ("route", "server_uuid", "a-different-server"),
            ("primary", "server_uuid", "a-different-server"),
            ("primary", "state", "RECOVERING"),
            ("cluster", "super_read_only", None),
            ("cluster", "super_read_only", True),
            ("cluster", "fenced_writes", None),
            ("cluster", "fenced_writes", True),
        )
        baseline = primary_with_missing_member()
        for scope, key, value in changes:
            with self.subTest(scope=scope, key=key, value=value):
                report = copy.deepcopy(baseline)
                local = next(item for item in report["clusterset"]["clusters"] if item["local"])
                target = {"server": report["server"], "set": report["clusterset"],
                          "cluster": local, "route": local["route"], "primary": local["primary"]}[scope]
                target[key] = value
                self.assert_refused(report)

    def test_primary_requires_a_positively_absent_channel(self):
        baseline = primary_with_missing_member()
        for channel in (None, {}, {"present": None},
                        {"present": False, "read_error": "Access denied"},
                        {"present": True, "receiver_state": "ON", "applier_state": "ON"},
                        {"present": True, "receiver_state": "OFF", "applier_state": "OFF"}):
            with self.subTest(channel=channel):
                report = copy.deepcopy(baseline)
                local = next(item for item in report["clusterset"]["clusters"] if item["local"])
                local["channel"] = channel
                self.assert_refused(report)

    def test_primary_cluster_identity_must_be_unique(self):
        report = primary_with_missing_member()
        local = next(item for item in report["clusterset"]["clusters"] if item["local"])
        report["clusterset"]["clusters"].append(copy.deepcopy(local))
        self.assert_refused(report)

    def test_explicit_non_ok_status_is_never_overridden_by_primary_observations(self):
        for status in ("OK_NOT_REPLICATING", "OK_NOT_CONSISTENT", "OK_MISCONFIGURED", "NOT_OK",
                       "UNKNOWN", "INVALIDATED", "OK_FENCED_WRITES"):
            with self.subTest(status=status):
                report = primary_with_missing_member()
                self.assertTrue(cluster._primary_clusterset_rejoin_ok(report))
                report["clusterset"]["global_status"] = status
                action = self.action(report)
                self.assertFalse(action["enabled"], action)
                self.assertIn(status, action["why_not"])
                with self.assertRaisesRegex(cluster.ActionUnavailable, "ClusterSet status"):
                    cluster.plan_action(report, "rejoin_instance", self.member(report)["key"],
                                        shell_fixtures.CHECKS)

    def test_replica_sql_status_does_not_prove_gtid_consistency(self):
        conn = F.conn("ic-8.4-clusterset-replica")
        conn.scenario["tables"]["performance_schema.replication_group_members"].pop()
        conn.scenario["tables"]["performance_schema.replication_group_member_actions"] = [[0]]
        report = cluster.read_status(conn)
        self.assertNotIn("global_status", report["clusterset"])
        self.assert_refused(report)


class RejoinLiveHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parent.parent / "scripts" / "clusterset-live.py"
        spec = importlib.util.spec_from_file_location("clusterset_primary_rejoin_live", str(path))
        cls.live = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.live)

    def report(self):
        action = {"id": "rejoin_instance", "enabled": True, "member_key": "member:a2"}
        return {"actions": [dict(action, member_key="wrong-overview")],
                "members": [{"address": "mf-cs-a1:3306", "actions": [dict(action, member_key="member:a1")]},
                            {"address": "mf-cs-a2:3306", "actions": [action]}]}

    def test_rejoin_selects_the_exact_member_catalog_and_preserves_member_key(self):
        action, parameters, entry = self.live.select_action(self.report(), "rejoin-a2")
        self.assertEqual(action, "rejoin_instance")
        self.assertEqual(parameters, {})
        self.assertEqual(entry["member_key"], "member:a2")

    def test_rejoin_fails_closed_when_target_missing_or_ambiguous(self):
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate):
                report = self.report()
                if duplicate:
                    report["members"].append(copy.deepcopy(report["members"][1]))
                else:
                    report["members"].pop()
                with self.assertRaisesRegex(RuntimeError, "exactly one"):
                    self.live.select_action(report, "rejoin-a2")

    def test_rejoin_cannot_fall_back_to_an_overview_action(self):
        report = self.report()
        report["members"][1]["actions"] = []
        with self.assertRaisesRegex(RuntimeError, "absent from the live catalog"):
            self.live.select_action(report, "rejoin-a2")

    def test_rejoin_retains_the_disabled_member_action_reason(self):
        report = self.report()
        report["members"][1]["actions"][0].update(enabled=False, why_not="Missing quorum", fix="Restore quorum")
        with self.assertRaisesRegex(RuntimeError, "Missing quorum.*Restore quorum"):
            self.live.select_action(report, "rejoin-a2")


class DissolvePreconditionTests(execution_fixtures.EndpointHarness):
    def test_51004_with_unchanged_topology_does_not_prove_no_persistent_change(self):
        # Shell takes locks before the offline-member check. Those locks can
        # install service UDFs with binlogging suppressed. The topology and
        # cleanup projections do not observe that persistent side effect.
        message = "Failed to dissolve the ClusterSet: Detected instances with (MISSING) state."
        for source in ("mysqlsh", "mysql", "unknown"):
            with self.subTest(source=source):
                before = copy.deepcopy(self.report)
                self.warning = message + " (MYSQLSH 51004)\n at STDIN:2\n"
                self.error = "mysqlsh exited 1: ERROR 51004 at line 2: " + message
                self.error_info = {"source": source, "code": 51004, "line": 2, "stage": "adminapi"}
                result = self.apply(self.plan("dissolve_clusterset"))
                self.assertEqual(self.report, before)
                self.assertEqual(result["outcome_class"], "unknown or partial", result)
                self.assertEqual(result["shell_error_info"], self.error_info)
                self.assertNotEqual(result["status"], "refused")


if __name__ == "__main__":
    unittest.main()
