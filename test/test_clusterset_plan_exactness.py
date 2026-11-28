"""Exact ClusterSet reviews and route-attributed post-action evidence.

The endpoint cases retain the real review token, policy, classification and
send gate. Only SQL observations and the external Shell transport are fixtures.
"""
import copy
from contextlib import contextmanager
import types
import unittest
from unittest import mock

import test_cluster_actions as catalog_fixtures
import test_clusterset_execution as execution_fixtures
from myflames import cluster, cluster_shell, clusterset_execution, clusterset_verify


ROUTING_ACTIONS = (
    "remove_cluster:drcluster", "remove_cluster_keep:drcluster",
    "set_routing_option", "set_routing_option:router::rw",
)


class CatalogAndPlanTests(unittest.TestCase):
    def entry(self, report, checks, action):
        return next(item for item in cluster.overview_actions(report, checks) if item["id"] == action)

    def test_unreachable_cluster_cannot_be_removed_and_kept_running(self):
        for name in ("drcluster", "oldcluster"):
            with self.subTest(target=name):
                report = catalog_fixtures.clusterset_report()
                checks = catalog_fixtures.clusterset_checks(report)
                catalog_fixtures.clusterset_unreachable(report, checks, name)
                action = "remove_cluster_keep:" + name
                entry = self.entry(report, checks, action)
                self.assertFalse(entry["enabled"], entry)
                self.assertIn("unreachable", entry["why_not"].lower())
                self.assertTrue(entry["fix"])
                for parameters in ({}, {"force": True}, {"force": False}):
                    with self.subTest(parameters=parameters), self.assertRaises(ValueError) as caught:
                        cluster.plan_action(report, action, checks=checks, parameters=parameters)
                    self.assertIn(entry["why_not"], str(caught.exception))
                # The safe refusal is specific to keeping an unreachable
                # cluster running. Explicit forced removal still dissolves it.
                remove = cluster.plan_action(report, "remove_cluster:" + name, checks=checks,
                                             parameters={"force": True})
                self.assertTrue(remove["parameters"]["force"])
                self.assertIn("removeCluster", remove["statements"][0])

    def test_diagnostic_without_recorded_primary_is_disabled_before_review(self):
        for primary_state in ("role_recorded", "role_missing", "invalidated", "mismatched_name"):
            with self.subTest(primary_state=primary_state):
                report = catalog_fixtures.clusterset_report()
                checks = catalog_fixtures.clusterset_checks(report)
                primary = cluster._clusterset_primary(report)
                report["clusterset"]["primary_cluster"] = "drcluster" if primary_state == "mismatched_name" else None
                if primary_state == "role_missing":
                    primary["role"] = None
                elif primary_state == "invalidated":
                    primary["invalidated"] = True
                entry = self.entry(report, checks, "check_clusterset")
                self.assertFalse(entry["enabled"], entry)
                self.assertIn("primary", entry["why_not"].lower())
                self.assertTrue(entry["fix"])
                with self.assertRaises(ValueError) as caught:
                    cluster.plan_action(report, "check_clusterset", checks=checks)
                self.assertIn(entry["why_not"], str(caught.exception))

    def test_diagnostic_allows_a_recorded_but_unreachable_primary(self):
        report = catalog_fixtures.clusterset_report()
        checks = catalog_fixtures.clusterset_checks(report)
        primary = cluster._clusterset_primary(report)
        catalog_fixtures.clusterset_unreachable(report, checks, primary["name"])
        entry = self.entry(report, checks, "check_clusterset")
        self.assertTrue(entry["enabled"], entry)
        plan = cluster.plan_action(report, "check_clusterset", checks=checks)
        self.assertEqual(plan["expect"]["server_identity"]["adminapi"]["primary_cluster"], primary["name"])

    def test_removal_and_routing_reviews_sort_and_detach_all_router_configuration(self):
        for action in ROUTING_ACTIONS:
            with self.subTest(action=action):
                report = catalog_fixtures.clusterset_report()
                checks = catalog_fixtures.clusterset_checks(report)
                cs = report["clusterset"]
                cs["routing_options"]["options"] = {"tags": {"active": False}}
                cs["routers"] = [
                    {"id": 2, "name": "other::rw", "options": {"tags": {"b": 1, "a": False}}},
                    {"id": 1, "name": "router::rw", "options": {}},
                ]
                parameters = {"option": "target_cluster", "value": "primary"} if action.startswith(
                    "set_routing_option") else {}
                plan = cluster.plan_action(report, action, checks=checks, parameters=parameters)
                snapshot = copy.deepcopy(plan["expect"]["routing_configuration"])
                cs["routers"].reverse()
                cs["routers"][1]["options"]["tags"] = {"a": False, "b": 1}
                fresh = cluster.plan_action(report, action, checks=checks, parameters=parameters)
                self.assertEqual(snapshot, fresh["expect"]["routing_configuration"])
                self.assertEqual([router["id"] for router in snapshot["routers"]], [1, 2])
                cs["routing_options"]["options"]["tags"]["active"] = True
                cs["routers"][0]["options"]["target_cluster"] = "drcluster"
                self.assertEqual(plan["expect"]["routing_configuration"], snapshot)

    def test_unreadable_unselected_router_disables_each_configuration_bound_review(self):
        for action in ROUTING_ACTIONS:
            with self.subTest(action=action):
                report = catalog_fixtures.clusterset_report()
                checks = catalog_fixtures.clusterset_checks(report)
                report["clusterset"]["routers"][0]["name"] = "router::rw"
                report["clusterset"]["routers"].append({"id": 2, "name": "unselected::rw", "options": None})
                entry = self.entry(report, checks, action)
                self.assertFalse(entry["enabled"], entry)
                self.assertTrue(entry["why_not"])
                self.assertTrue(entry["fix"])
                parameters = {"option": "target_cluster", "value": "primary"} if action.startswith(
                    "set_routing_option") else {}
                with self.assertRaises(ValueError) as caught:
                    cluster.plan_action(report, action, checks=checks, parameters=parameters)
                self.assertIn(entry["why_not"], str(caught.exception))


class RoutingReviewExecutionTests(execution_fixtures.EndpointHarness):
    def test_type_only_routing_drift_refuses_each_bound_action_before_send(self):
        for action in ROUTING_ACTIONS:
            for location in ("defaults", "router"):
                for expected, observed in ((False, 0), (True, 1),
                                           ({"nested": [False, {"active": True}]},
                                            {"nested": [0, {"active": 1}]})):
                    with self.subTest(action=action, location=location, expected=expected):
                        self.report = copy.deepcopy(self.baseline)
                        cs = self.report["clusterset"]
                        options = (cs["routing_options"]["options"] if location == "defaults" else
                                   cs["routers"][0]["options"])
                        options["tags"] = {"flag": expected}
                        plan = self.plan(action)
                        view = cs["view_id"]
                        options["tags"]["flag"] = observed
                        result = self.apply(plan)
                        self.assertEqual(cs["view_id"], view)
                        self.assertEqual(result["status"], "refused", result)
                        self.assertIn("Router routing changed after review", result["message"])
                        self.assertEqual(result["sent"], [])
                        self.assertEqual(result["not_sent"], plan["statements"])
                        self.assertEqual(self.sent, [])

    def test_row_and_object_key_order_alone_do_not_expire_reviews(self):
        for action in ROUTING_ACTIONS:
            with self.subTest(action=action):
                self.report = copy.deepcopy(self.baseline)
                self.sent.clear()
                cs = self.report["clusterset"]
                cs["routers"] = [
                    {"id": 2, "name": "other::rw", "options": {"tags": {"b": 1, "a": False}}},
                    {"id": 1, "name": "router::rw", "options": {}},
                ]
                plan = self.plan(action)
                cs["routers"].reverse()
                cs["routers"][1]["options"]["tags"] = {"a": False, "b": 1}
                self.apply(plan)
                self.assertEqual(self.sent, ["\n".join(plan["session_guard"] + plan["statements"])])

    def test_unselected_router_becoming_unreadable_refuses_each_action_before_send(self):
        for action in ROUTING_ACTIONS:
            with self.subTest(action=action):
                self.report = copy.deepcopy(self.baseline)
                cs = self.report["clusterset"]
                cs["routers"].append({"id": 2, "name": "unselected::rw", "options": {}})
                plan = self.plan(action)
                cs["routers"][1]["options"] = None
                result = self.apply(plan)
                self.assertEqual(result["status"], "refused", result)
                self.assertIn("Router routing could not be read after review", result["message"])
                self.assertEqual(result["sent"], [])
                self.assertEqual(result["not_sent"], plan["statements"])
                self.assertEqual(self.sent, [])


class RoutingValueVerificationTests(unittest.TestCase):
    def test_tags_require_recursive_json_type_equality(self):
        cases = (
            (False, 0, False), (True, 1, False), (0, False, False),
            ([False, {"enabled": True}], [0, {"enabled": 1}], False),
            ({"a": [False], "b": {"c": True}}, {"b": {"c": 1}, "a": [0]}, False),
            ({"a": [False, 1], "b": {"c": True}}, {"b": {"c": True}, "a": [False, 1]}, True),
            (False, False, True), (0, 0, True), (None, None, True),
        )
        for action in ("set_routing_option", "set_routing_option:router::rw"):
            for option in ("tag:flag", "tags"):
                for expected, observed, matches in cases:
                    with self.subTest(action=action, option=option, expected=expected, observed=observed):
                        tags = {"flag": observed}
                        report = {"clusterset": {"routing_options": {"options": {"tags": tags}},
                                                  "routers": [{"name": "router::rw", "options": {"tags": tags}}]}}
                        plan = {"action": action, "parameters": {"option": option,
                                "value": {"flag": expected} if option == "tags" else expected}}
                        evidence = clusterset_verify._Evidence()
                        clusterset_verify._verify_routing(evidence, plan, report, report)
                        self.assertEqual(evidence.missing, [])
                        self.assertEqual(bool(evidence.mismatches), not matches, evidence.facts)

    def test_non_object_stored_tags_cannot_prove_a_tag_was_cleared(self):
        for action in ("set_routing_option", "set_routing_option:router::rw"):
            for tags in (None, False, 0, "", []):
                with self.subTest(action=action, tags=tags):
                    options = {"tags": tags}
                    report = {"clusterset": {"routing_options": {"options": options},
                                              "routers": [{"name": "router::rw", "options": options}]}}
                    plan = {"action": action, "parameters": {"option": "tag:flag", "value": None}}
                    evidence = clusterset_verify._Evidence()
                    clusterset_verify._verify_routing(evidence, plan, report, report)
                    self.assertTrue(evidence.missing, evidence.facts)
                    self.assertEqual(evidence.mismatches, [])
                    self.assertEqual(evidence.facts[0]["actual"], "Stored tags are not an object")


class CleanupRouteVerificationTests(unittest.TestCase):
    A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    def cleanup(self, wrong_route):
        members = [{"server_uuid": uuid, "label": "Member " + name, "address": name.lower() + ":3306",
                    "state": "ONLINE", "role": "PRIMARY" if name == "A" else "SECONDARY"}
                   for uuid, name in ((self.A, "A"), (self.B, "B"))]
        before = {"server": {"server_uuid": "outside-the-removal"},
                  "clusterset": {"name": "set", "clusters": [{"name": "replica", "members": members}]}}
        profiles = [{"id": name, "name": "Saved " + name, "host": name.lower(), "port": 3306,
                     "topology_identity": {"server_uuid": uuid}} for uuid, name in ((self.A, "A"), (self.B, "B"))]
        reads = []

        @contextmanager
        def opener(profile):
            uuid = self.B if wrong_route and profile["id"] == "A" else profile["topology_identity"]["server_uuid"]
            yield types.SimpleNamespace(uuid=uuid, label=profile["name"])

        class Reader(object):
            def __init__(self, peer):
                self.peer = peer

            def rows(self, sql):
                reads.append((self.peer.label, sql))
                if sql == "SELECT @@server_uuid":
                    return [[self.peer.uuid]]
                if sql == "SELECT @@performance_schema":
                    return [["ON"]]
                if sql == "SELECT @@global.super_read_only":
                    return [["ON"]]
                if "FROM performance_schema.persisted_variables" in sql:
                    return [["group_replication_start_on_boot", "OFF"]]
                return []

        evidence = clusterset_verify._Evidence()
        with mock.patch.object(cluster, "_ClusterSetReader", Reader):
            clusterset_verify._verify_cleanup(object(), evidence, {"action": "remove_cluster:replica"}, before,
                                              {"profiles": profiles, "open_profile": opener})
        return evidence, reads

    def test_a_route_for_a_cannot_claim_b_cleanup_even_when_b_is_in_inventory(self):
        evidence, reads = self.cleanup(wrong_route=True)
        self.assertEqual([sql for label, sql in reads if label == "Saved A"], ["SELECT @@server_uuid"])
        self.assertTrue(any(fact["status"] == "mismatch" and fact["expected"] == self.A and
                            fact["actual"] == self.B for fact in evidence.facts), evidence.facts)
        self.assertTrue(any("Member A" in label for label in evidence.missing), evidence.missing)
        self.assertFalse(any("Member B" in label for label in evidence.missing), evidence.missing)
        self.assertTrue(any(fact["server"] == "Saved B" and fact["fact"] == "route server UUID" and
                            fact["status"] == "verified" for fact in evidence.facts), evidence.facts)

    def test_matching_saved_routes_verify_each_member_under_the_correct_label(self):
        evidence, reads = self.cleanup(wrong_route=False)
        self.assertEqual(evidence.missing, [])
        self.assertEqual(evidence.mismatches, [])
        routes = [(fact["server"], fact["actual"]) for fact in evidence.facts if fact["fact"] == "route server UUID"]
        self.assertEqual(routes, [("Saved A", self.A), ("Saved B", self.B)])
        self.assertTrue(any(label == "Saved A" and sql == "SELECT @@performance_schema" for label, sql in reads))
        self.assertTrue(any(label == "Saved B" and sql == "SELECT @@performance_schema" for label, sql in reads))


class CandidatePreflightGateTests(unittest.TestCase):
    def inputs(self):
        report = catalog_fixtures.clusterset_report()
        group = catalog_fixtures.clusterset_checks(report)
        candidate = group.pop("replica_candidates")[0]
        group.pop("shell")
        group["read_options"] = {"profiles": group["group_profiles"], "open_profile": mock.Mock()}
        source = types.SimpleNamespace(ssh_host=None, ssl_mode="PREFERRED")
        return report, group, candidate, source

    def prepare(self, report, group, candidate, source, shell):
        with mock.patch.object(cluster, "read_status", return_value=report), \
                mock.patch.object(cluster_shell, "find_shell", return_value=shell), \
                mock.patch.object(clusterset_execution, "read_candidate", return_value=candidate) as preflight:
            try:
                result = cluster.prepare_action(source, "create_replica_cluster:spare",
                                                group_checks=lambda: group, check_grants=False)
            except ValueError as error:
                return None, preflight, error
            return result, preflight, None

    def test_shell_version_and_route_transport_gates_run_before_spare_preflight(self):
        cases = (("missing_shell", "MySQL Shell"), ("old_shell", "8.0.32"),
                 ("primary_ssh", "SSH"), ("primary_tls", "REQUIRED"),
                 ("primary_password", "password"), ("spare_ssh", "SSH"),
                 ("spare_tls", "VERIFY_IDENTITY"), ("spare_password", "password"))
        for failure, phrase in cases:
            with self.subTest(failure=failure):
                report, group, candidate, source = self.inputs()
                shell = execution_fixtures.SHELL
                primary = cluster._clusterset_primary(report)
                route_profile = next(profile for profile in group["group_profiles"]
                                     if profile["id"] == primary["route"]["profile_id"])
                spare = next(profile for profile in group["group_profiles"] if profile["id"] == "spare")
                if failure == "missing_shell":
                    shell = None
                elif failure == "old_shell":
                    shell = dict(shell, version="8.0.31", version_tuple=[8, 0, 31])
                elif failure == "primary_ssh":
                    route_profile["ssh_host"] = "primary-bastion"
                elif failure == "primary_tls":
                    route_profile["ssl_mode"] = "REQUIRED"
                elif failure == "primary_password":
                    route_profile["remember_password"] = False
                elif failure == "spare_ssh":
                    spare["ssh_host"] = "spare-bastion"
                elif failure == "spare_tls":
                    spare["ssl_mode"] = "VERIFY_IDENTITY"
                else:
                    spare["remember_password"] = False
                result, preflight, error = self.prepare(report, group, candidate, source, shell)
                self.assertIsNone(result)
                self.assertIsNotNone(error)
                self.assertIn(phrase.lower(), str(error).lower())
                preflight.assert_not_called()

    def test_eligible_direct_routes_run_exactly_one_candidate_preflight(self):
        for version in ((8, 0, 32), (9, 7, 1)):
            with self.subTest(version=version):
                report, group, candidate, source = self.inputs()
                # SSH on an active replica does not invalidate the selected
                # direct primary and spare routes used by this action.
                source.ssh_host = "active-replica-bastion"
                shell = dict(execution_fixtures.SHELL, version=".".join(str(n) for n in version),
                             version_tuple=list(version))
                result, preflight, error = self.prepare(report, group, candidate, source, shell)
                self.assertIsNone(error)
                self.assertIs(result[0], report)
                self.assertEqual(result[1]["replica_candidates"], [candidate])
                preflight.assert_called_once()
                self.assertEqual(preflight.call_args[0][:3], (source, report, "create_replica_cluster:spare"))
