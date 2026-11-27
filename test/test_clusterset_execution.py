"""ClusterSet execution evidence, using real review, policy and audit gates.

Only external SQL/Shell transport is synthetic. These tests do not claim a
live MySQL outcome; scripts/clusterset-live.py records those separately.
"""
import copy
from contextlib import contextmanager
import json
import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock

import _cluster_fixtures as F
import test_cluster_shell as fixtures
from myflames import cluster, cluster_shell, clusterset_execution as execution
from myflames import clusterset_verify as verify, dbops, reviewed_writes, ui
from myflames.connector import ConnectorError
from myflames.workspace_store import WorkspaceStore


SHELL = dict(fixtures.SHELL, version="9.7.1", version_tuple=[9, 7, 1])
SPARE_UUID = "bbbbbbbb-cccc-dddd-eeee-000000000001"


def topology():
    # Give the primary an explicit member-action result. With the fencing
    # statements sharing a mysql login, an absent action table fails the
    # whole batch and cannot supply a super_read_only observation.
    conn = F.conn("ic-8.4-clusterset-primary")
    conn.scenario["tables"]["performance_schema.replication_group_member_actions"] = [[0]]
    report = fixtures.clusterset_reads(cluster.read_status(conn))
    cs = report["clusterset"]
    primary = next(item for item in cs["clusters"] if item["role"] == "PRIMARY")
    address = primary["primary"]["address"]
    primary_uuid = primary["primary"]["server_uuid"]
    for item in cs["clusters"]:
        if item.get("role") == "REPLICA" and item.get("channel"):
            item["channel"]["source"] = address
            item["channel"]["source_uuid"] = primary_uuid
    cs["routers"] = [{"id": 1, "name": "router::rw", "options": {}, "version": "9.7.1"}]
    cs["routing_options"] = {"options": {}}
    return report


class Home(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.dict(os.environ, HOME=self.temp.name, USERPROFILE=self.temp.name,
                                  MYFLAMES_SECRET_BACKEND="vault")
        patcher.start()
        self.addCleanup(patcher.stop)


class CandidateConnection(F.ScenarioConn):
    """The spare's exact SQL answers, including refusal edge cases."""
    def __init__(self):
        super().__init__({"variables": {
            "server_uuid": SPARE_UUID, "version": "8.4.11", "version_comment": "MySQL Community Server",
            "report_host": "spare", "hostname": "spare", "port": "3306", "super_read_only": "ON",
            "gtid_mode": "ON", "enforce_gtid_consistency": "ON", "log_bin": "ON",
            "log_replica_updates": "ON", "binlog_format": "ROW", "server_id": "101"}})
        self.members = []
        self.metadata = []
        self.subset = "1"

    def _answer(self, body):
        # Preserve the UUID result when replaying a guarded candidate login.
        # The generic batch replay does not dispatch back to these spare-only
        # projections, so split here before answering even an empty table.
        prefix = "SELECT @@GLOBAL.server_uuid; "
        if body.startswith(prefix):
            return F.batch([[self.scenario["variables"].get("server_uuid")]]) + self._answer(body[len(prefix):])
        if body == "SELECT MEMBER_ID, MEMBER_STATE FROM performance_schema.replication_group_members":
            return F.batch(self.members)
        if "FROM information_schema.SCHEMATA" in body:
            return F.batch(self.metadata)
        if body.startswith("SELECT GTID_SUBSET("):
            return F.batch([[self.subset]])
        return super()._answer(body)


class CandidateTests(Home):
    def setUp(self):
        super().setUp()
        self.report = topology()
        self.candidate = CandidateConnection()
        self.profile = {"id": "spare", "name": "Spare", "host": "spare", "port": 3306,
                        "group": "G", "remember_password": True,
                        "topology_identity": {"server_uuid": SPARE_UUID}}
        self.opens = []

        @contextmanager
        def opener(profile):
            self.opens.append(profile["id"])
            yield self.candidate

        primary_uuid = self.report["server"]["server_uuid"]
        self.source = types.SimpleNamespace(run=lambda sql, **kwargs: F.batch([[primary_uuid, ""]]))
        self.checks = {"shell": SHELL, "group_name": "G", "group_profiles": [self.profile],
                       "read_options": {"profiles": [self.profile], "open_profile": opener}}

    def candidate_read(self, output='MYFLAMES_CONFIG={"status":"ok"}\n', password=None):
        private_output = cluster_shell._status_output(output, password)
        with mock.patch.object(cluster_shell.ShellWriter, "run_write", return_value=private_output) as shell:
            result = execution.read_candidate(self.source, self.report, "create_replica_cluster:spare", self.checks)
        return result, shell

    def test_known_spare_is_offered_without_opening_or_running_configuration_check(self):
        with mock.patch.object(execution, "read_candidate", side_effect=AssertionError("catalog preflight")):
            catalog = cluster.overview_actions(self.report, self.checks)
        selected = next(a for a in catalog if a["id"] == "create_replica_cluster:spare")
        self.assertTrue(selected["enabled"], selected)
        self.assertEqual(self.opens, [])
        with self.assertRaisesRegex(ValueError, "preflight|standalone"):
            cluster.plan_action(self.report, selected["id"], checks=self.checks,
                                parameters={"name": "newcluster", "recoveryMethod": "incremental"})

    def test_unknown_uuid_and_member_of_an_existing_cluster_are_not_offered_as_spares(self):
        for identity in ({}, {"server_uuid": self.report["server"]["server_uuid"]}):
            with self.subTest(identity=identity):
                self.profile["topology_identity"] = identity
                entries = cluster.overview_actions(self.report, self.checks)
                self.assertFalse(any(a["enabled"] and a["id"] == "create_replica_cluster:spare" for a in entries))
        self.assertEqual(self.opens, [])

    def test_preflight_reads_standalone_facts_and_uses_read_only_shell_configuration_check(self):
        result, shell = self.candidate_read()
        self.assertEqual(self.opens, ["spare"])
        self.assertEqual(result["configuration"]["status"], "ok")
        self.assertTrue(result["super_read_only"])
        self.assertTrue(result["gtid_subset"])
        script = shell.call_args[0][0]
        self.assertIn("dba.checkInstanceConfiguration()", script)
        self.assertIn(SPARE_UUID, script)
        self.assertNotIn("dryRun", script)
        self.assertFalse(any("SET " in sql or "CREATE " in sql for sql, _ in self.candidate.calls))
        self.assertTrue(any("GTID_SUBSET" in sql for sql, _ in self.candidate.calls))
        self.assertEqual(len(self.candidate.calls), 4)
        for sql, flags in self.candidate.calls:
            self.assertTrue(sql.startswith(cluster.CLUSTER_TAG + "SELECT @@GLOBAL.server_uuid; "), sql)
            self.assertIn("--skip-reconnect", flags)

    def test_each_spare_fact_login_refuses_an_endpoint_switched_to_another_server(self):
        # Empty GR and metadata results also need an identity row. Without
        # it, an endpoint swap could supply a different server's empty state.
        for projection in ("SHOW GLOBAL VARIABLES", "replication_group_members",
                           "information_schema.SCHEMATA", "GTID_SUBSET"):
            with self.subTest(projection=projection):
                self.candidate = CandidateConnection()
                original_run = self.candidate.run

                def swapped(sql, **kwargs):
                    if projection in sql:
                        self.candidate.scenario["variables"]["server_uuid"] = "11111111-2222-3333-4444-555555555555"
                    return original_run(sql, **kwargs)

                with mock.patch.object(self.candidate, "run", side_effect=swapped), \
                        mock.patch.object(cluster_shell.ShellWriter, "run_write") as shell:
                    with self.assertRaisesRegex(ValueError, "UUID changed|different server"):
                        execution.read_candidate(self.source, self.report, "create_replica_cluster:spare", self.checks)
                shell.assert_not_called()

    def test_missing_identity_row_cannot_establish_empty_standalone_facts(self):
        for projection in ("replication_group_members", "information_schema.SCHEMATA", "GTID_SUBSET"):
            with self.subTest(projection=projection):
                self.candidate = CandidateConnection()
                original_run = self.candidate.run

                def missing_guard(sql, **kwargs):
                    result = original_run(sql, **kwargs)
                    return result.partition("\n")[2] if projection in sql else result

                with mock.patch.object(self.candidate, "run", side_effect=missing_guard), \
                        mock.patch.object(cluster_shell.ShellWriter, "run_write") as shell:
                    with self.assertRaisesRegex(ValueError, "UUID changed|different server"):
                        execution.read_candidate(self.source, self.report, "create_replica_cluster:spare", self.checks)
                shell.assert_not_called()

    def test_candidate_suffix_versions_pass_preflight_and_plan_version_checks(self):
        for version in ("8.0.27-commercial", "8.4.6-commercial", "8.4.6-6"):
            with self.subTest(version=version):
                self.candidate.scenario["variables"]["version"] = version
                candidate, _ = self.candidate_read()
                self.assertEqual(candidate["server_version"], version)
                self.checks["replica_candidates"] = [candidate]
                plan = cluster.plan_action(self.report, "create_replica_cluster:spare", checks=self.checks,
                                           parameters={"name": "newcluster", "recoveryMethod": "incremental"})
                self.assertEqual(plan["expect"]["replica_candidate"]["server_version"], version)

    def test_candidate_suffix_versions_do_not_bypass_mysql_version_floor(self):
        candidate, _ = self.candidate_read()
        for version in ("8.0.26-commercial", "5.7.44-log", "", "unknown"):
            with self.subTest(version=version):
                candidate["server_version"] = version
                issue = cluster._clusterset_candidate_issue(self.report, self.checks, candidate, self.profile)
                self.assertIn("8.0.27", issue[0])
                self.candidate.scenario["variables"]["version"] = version
                with self.assertRaisesRegex(ValueError, "8.0.27 or later"):
                    self.candidate_read()
        self.candidate.scenario["variables"]["version"] = "10.11.6-MariaDB"
        with self.assertRaisesRegex(ValueError, "8.0.27 or later"):
            self.candidate_read()

    def test_instance_in_metadata_outside_this_set_is_not_a_spare(self):
        self.report._clusterset_instances["another-cluster"] = [{"server_uuid": SPARE_UUID}]
        entries = cluster.overview_actions(self.report, self.checks)
        self.assertFalse(any(a["enabled"] and a["id"] == "create_replica_cluster:spare" for a in entries))
        with self.assertRaisesRegex(ValueError, "outside.*metadata"):
            self.candidate_read()
        self.assertEqual(self.opens, [])

    def test_preflight_refuses_configuration_not_confirmed_ok(self):
        for output in ("", 'MYFLAMES_CONFIG={"status":"error"}\n', 'MYFLAMES_CONFIG={}\n',
                       'MYFLAMES_CONFIG={"status":"ok"',
                       'MYFLAMES_CONFIG={"status":"error","status":"ok"}\n',
                       'MYFLAMES_CONFIG={"status":"ok","other":NaN}\n',
                       'MYFLAMES_CONFIG={"status":"ok"}\nMYFLAMES_CONFIG={"status":"ok"}\n'):
            with self.subTest(output=output):
                with self.assertRaisesRegex(ValueError, "configured for InnoDB Cluster"):
                    self.candidate_read(output)

    def test_configuration_uses_private_evidence_when_password_overlaps_machine_output(self):
        for password in ("status", "ok", "MYFLAMES_CONFIG", "{", '"', "="):
            with self.subTest(password=password):
                result, shell = self.candidate_read(password=password)
                self.assertEqual(result["configuration"]["status"], "ok")
                self.assertIs(shell.return_value.configuration_ok, True)
                self.assertNotIn(password, shell.return_value)
                self.assertEqual(shell.return_value.__dict__, {
                    "diagnostic": None, "error_info": None, "configuration_ok": True})

    def test_redacted_display_alone_cannot_become_configuration_evidence(self):
        with mock.patch.object(cluster_shell.ShellWriter, "run_write",
                               return_value='MYFLAMES_CONFIG={"status":"ok"}\n'):
            with self.assertRaisesRegex(ValueError, "configured for InnoDB Cluster"):
                execution.read_candidate(self.source, self.report, "create_replica_cluster:spare", self.checks)

    def test_create_refuses_reported_address_redirect_before_shell_configuration_check(self):
        for changed in ({"report_host": "victim.example"}, {"hostname": "victim.example", "report_host": ""},
                        {"port": "3310"}, {"report_port": "3310"}):
            with self.subTest(changed=changed):
                self.candidate = CandidateConnection()
                self.candidate.scenario["variables"].update(changed)
                with mock.patch.object(cluster_shell.ShellWriter, "run_write",
                                       return_value='MYFLAMES_CONFIG={"status":"ok"}\n') as shell:
                    with self.assertRaisesRegex(ValueError, "reported.*saved|saved.*reported"):
                        execution.read_candidate(self.source, self.report, "create_replica_cluster:spare", self.checks)
                shell.assert_not_called()

    def test_read_only_spare_is_disabled_and_cannot_be_read_or_planned_for_creation(self):
        candidate, _ = self.candidate_read()
        self.checks["replica_candidates"] = [candidate]
        self.profile["read_only"] = True
        catalog_checks = dict(self.checks, replica_candidates=[])
        listed = next(a for a in cluster.overview_actions(self.report, catalog_checks)
                      if a["id"] == "create_replica_cluster:spare")
        self.assertFalse(listed["enabled"])
        self.assertIn("Read-only", listed["why_not"])
        self.assertTrue(listed["fix"])
        entry = next(a for a in cluster.overview_actions(self.report, self.checks)
                     if a["id"] == "create_replica_cluster:spare")
        self.assertFalse(entry["enabled"], entry)
        self.assertIn("Read-only", entry["why_not"])
        self.assertTrue(entry["fix"])
        with self.assertRaisesRegex(ValueError, "Read-only"):
            cluster.plan_action(self.report, entry["id"], checks=self.checks,
                                parameters={"name": "newcluster", "recoveryMethod": "incremental"})
        self.opens.clear()
        with mock.patch.object(cluster_shell.ShellWriter, "run_write") as shell:
            with self.assertRaisesRegex(ValueError, "Read-only"):
                execution.read_candidate(self.source, self.report, entry["id"], self.checks)
        self.assertEqual(self.opens, [])
        shell.assert_not_called()

    def test_creation_binds_target_for_clone_and_incremental_and_restores_clone_limits(self):
        candidate, _ = self.candidate_read()
        self.checks["replica_candidates"] = [candidate]
        self.assertEqual(candidate["address"], candidate["reported_address"])
        for method in ("clone", "incremental"):
            with self.subTest(method=method):
                plan = cluster.plan_action(self.report, "create_replica_cluster:spare", checks=self.checks,
                                           parameters={"name": "newcluster", "recoveryMethod": method})
                self.assertEqual(plan["expect"]["server_identity"]["target_instance"], {
                    "address": "spare:3306", "server_uuid": SPARE_UUID})
                self.assertEqual(plan["parameters"]["recoveryMethod"], method)
                guard = reviewed_writes.session_guard(plan)[0]
                self.assertIn("shell.openSession", guard)
                self.assertIn(SPARE_UUID, guard)
                self.assertIn("Use a direct server address", " ".join(plan["warnings"]))
                if method == "clone":
                    self.assertEqual(plan["timeout_s"], cluster_shell.CLONE_TIMEOUT)
                    self.assertEqual(plan["idle_timeout_s"], cluster_shell.CLONE_IDLE_TIMEOUT)
                    self.assertIn("24 hours", " ".join(plan["warnings"]))

    def test_incremental_creation_keeps_the_existing_shell_timeout(self):
        candidate, _ = self.candidate_read()
        self.checks["replica_candidates"] = [candidate]
        plan = cluster.plan_action(self.report, "create_replica_cluster:spare", checks=self.checks,
                                   parameters={"name": "newcluster", "recoveryMethod": "incremental"})
        self.assertEqual(plan["timeout_s"], cluster.SHELL_TIMEOUT)
        self.assertNotIn("idle_timeout_s", plan)
        self.assertNotIn("24 hours", " ".join(plan["warnings"]))
        expected = plan["expect"]["replica_candidate"]
        self.assertIs(expected["read_only"], False)
        self.assertEqual(expected["endpoint"]["host"], "spare")
        self.assertEqual(expected["endpoint"]["port"], 3306)
        self.assertEqual(expected["reported_address"], expected["address"])

    def test_fresh_standalone_identity_group_gtids_metadata_and_fencing_are_required(self):
        cases = (
            (lambda c: c.scenario["variables"].update(server_uuid="11111111-2222-3333-4444-555555555555"),
             "UUID changed|different server"),
            (lambda c: setattr(c, "members", [[SPARE_UUID, "ONLINE"]]), "Group Replication is running"),
            (lambda c: setattr(c, "metadata", [["mysql_innodb_cluster_metadata"]]), "still has InnoDB Cluster metadata"),
            (lambda c: setattr(c, "subset", "0"), "not a subset"),
            (lambda c: c.scenario["variables"].pop("super_read_only"), "super_read_only could not be read"),
            (lambda c: c.scenario["variables"].update(version="8.0.26"), "8.0.27 or later"),
        )
        for mutate, expected in cases:
            with self.subTest(expected=expected):
                self.candidate = CandidateConnection()
                mutate(self.candidate)
                with self.assertRaisesRegex(ValueError, expected), \
                        mock.patch.object(cluster_shell.ShellWriter, "run_write") as shell:
                    execution.read_candidate(self.source, self.report, "create_replica_cluster:spare", self.checks)
                shell.assert_not_called()


class EndpointHarness(Home):
    """Only transport observations change; token/typed/policy/audit stay real."""
    def setUp(self):
        super().setUp()
        self.report = topology()
        self.baseline = copy.deepcopy(self.report)
        self.store = WorkspaceStore()
        self.workspace = types.SimpleNamespace(store=self.store)
        self.connection = {"host": "fixture", "port": 3306, "user": "admin", "password": "secret-split"}
        self.conn = F.conn("ic-8.4-clusterset-primary")
        self.conn.host, self.conn.port, self.conn.user, self.conn.password = "fixture", 3306, "admin", "secret-split"
        self.sent = []
        self.output = json.dumps({"domainName": self.report["clusterset"]["name"], "status": "HEALTHY",
                                  "clusters": {item["name"]: {"globalStatus": "OK", "clusterRole": item["role"]}
                                               for item in self.report["clusterset"]["clusters"]}}) + "\n"
        self.warning = ""
        self.error = None
        self.error_info = None
        self.mutate_after = None

        def send(writer, script, **kwargs):
            self.sent.append(script)
            writer.stderr = cluster_shell._ShellText(self.warning, error_info=self.error_info)
            if self.mutate_after:
                self.mutate_after()
            if self.error:
                raise ConnectorError(self.error)
            return cluster_shell._status_output(self.output, writer.password, cluster_shell._clusterset_identity(script))

        patches = (
            mock.patch("myflames.connector.MySQLConnection", return_value=self.conn),
            mock.patch.object(cluster_shell, "find_shell", return_value=SHELL),
            mock.patch.object(cluster, "read_status", side_effect=lambda *a, **kw: copy.deepcopy(self.report)),
            mock.patch.object(cluster_shell.ShellWriter, "run_write", new=send),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def plan(self, action="set_routing_option:router::rw", parameters=None):
        if parameters is None:
            parameters = {"option": "target_cluster", "value": "primary"} if action.startswith("set_routing_option") else {}
        return ui.api_response("/api/live/cluster/plan", {"connection": self.connection, "action": action,
                               "parameters": parameters}, self.workspace)

    def apply(self, plan, **changes):
        reviewed = dict(plan)
        token = reviewed.pop("review_token")
        payload = {"connection": self.connection, "plan": reviewed, "review_token": token,
                   "typed": plan.get("typed_guard"), "confirmed": True, "progress_id": "p" * 24}
        payload.update(changes)
        return ui.api_response("/api/live/cluster/action", payload, self.workspace)

    def store_primary_option(self):
        self.report["clusterset"]["routers"][0]["options"]["target_cluster"] = "primary"


class EndpointExecutionTests(EndpointHarness):
    def test_parameters_are_forwarded_and_exact_bound_js_is_executed(self):
        plan = self.plan()
        self.assertEqual(plan["parameters"], {"option": "target_cluster", "value": "primary"})
        self.mutate_after = self.store_primary_option
        result = self.apply(plan)
        self.assertEqual(result["outcome_class"], "verified", result)
        expected = "\n".join(plan["session_guard"] + plan["statements"])
        self.assertEqual(self.sent, [expected])

    def test_audit_is_one_record_containing_exact_js_target_outcome_and_sql_facts(self):
        plan = self.plan()
        self.mutate_after = self.store_primary_option
        result = self.apply(plan)
        records = dbops.audit_report(self.store)["records"]
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["id"], result["audit_id"])
        self.assertEqual(record["statements"], plan["session_guard"] + plan["statements"])
        self.assertEqual(record["target"], plan["expect"])
        self.assertEqual(record["outcome"], "verified")
        self.assertEqual(record["after"]["verification"]["facts"], result["verification"]["facts"])
        self.assertNotIn("secret-split", json.dumps(record))

    def test_preread_changes_to_view_role_group_identity_and_route_uuid_send_nothing(self):
        for change in ("view", "domain", "role", "invalidation", "primary_cluster", "metadata_address",
                       "group", "cluster_id", "route", "server"):
            with self.subTest(change=change):
                self.report = copy.deepcopy(self.baseline)
                plan = self.plan("set_primary_cluster:drcluster")
                target = next(c for c in self.report["clusterset"]["clusters"] if c["name"] == "drcluster")
                if change == "view":
                    self.report["clusterset"]["view_id"] = str(int(self.report["clusterset"]["view_id"]) + 1)
                elif change == "domain":
                    self.report["clusterset"]["name"] = "replacement-set"
                elif change == "role":
                    target["role"] = "PRIMARY"
                elif change == "invalidation":
                    target["invalidated"] = True
                elif change == "primary_cluster":
                    self.report["clusterset"]["primary_cluster"] = target["name"]
                elif change == "metadata_address":
                    cluster_id = self.report._clusterset_ids[target["name"]]
                    self.report._clusterset_instances[cluster_id][0]["address"] = "replacement:3306"
                elif change == "group":
                    self.report._clusterset_groups["drcluster"] = SPARE_UUID
                elif change == "cluster_id":
                    self.report._clusterset_ids["drcluster"] = "replacement-cluster"
                elif change == "route":
                    target["route"]["server_uuid"] = SPARE_UUID
                else:
                    self.report["server"]["server_uuid"] = SPARE_UUID
                result = self.apply(plan)
                self.assertEqual(result["status"], "refused", result)
                self.assertIn("nothing was sent", result["message"].lower())
                self.assertEqual(result["sent"], [])
                self.assertEqual(self.sent, [])

    def test_switchover_refuses_routing_changes_before_sending(self):
        for change in ("defaults", "router", "default_tag_type", "router_tag_type", "added", "removed"):
            with self.subTest(change=change):
                self.report = copy.deepcopy(self.baseline)
                cs = self.report["clusterset"]
                cs["routing_options"]["options"] = {"target_cluster": "primary", "tags": {"active": False}}
                cs["routers"][0]["options"] = {"target_cluster": "primary", "tags": {"active": False}}
                plan = self.plan("set_primary_cluster:drcluster")
                view_id = cs["view_id"]
                if change == "defaults":
                    cs["routing_options"]["options"]["target_cluster"] = "drcluster"
                elif change == "router":
                    cs["routers"][0]["options"]["target_cluster"] = "drcluster"
                elif change == "default_tag_type":
                    cs["routing_options"]["options"]["tags"]["active"] = 0
                elif change == "router_tag_type":
                    cs["routers"][0]["options"]["tags"]["active"] = 0
                elif change == "added":
                    cs["routers"].append({"id": 2, "name": "another::rw", "options": {}})
                else:
                    cs["routers"].clear()
                self.assertEqual(cs["view_id"], view_id)
                result = self.apply(plan)
                self.assertEqual(result["status"], "refused", result)
                self.assertIn("Router routing changed after review", result["message"])
                self.assertIn("Nothing was sent", result["message"])
                self.assertEqual(result["sent"], [])
                self.assertEqual(result["not_sent"], plan["statements"])
                self.assertEqual(result["states"], ["not_sent"])
                self.assertEqual(self.sent, [])

    def test_switchover_refuses_if_the_fresh_routing_read_is_unavailable(self):
        for failure in ("defaults", "routers", "override"):
            with self.subTest(failure=failure):
                self.report = copy.deepcopy(self.baseline)
                plan = self.plan("set_primary_cluster:drcluster")
                cs = self.report["clusterset"]
                if failure == "defaults":
                    cs["routing_options"]["options"] = None
                elif failure == "routers":
                    cs["routers"] = None
                else:
                    cs["routers"][0]["options"] = None
                result = self.apply(plan)
                self.assertEqual(result["status"], "refused", result)
                self.assertIn("Router routing could not be read after review", result["message"])
                self.assertEqual(result["sent"], [])
                self.assertEqual(result["not_sent"], plan["statements"])
                self.assertEqual(self.sent, [])

    def test_switchover_accepts_unchanged_routing_in_a_different_row_order(self):
        cs = self.report["clusterset"]
        cs["routers"] = [{"id": 1, "name": "router::r01", "options": {"tags": {"a": False, "b": 1}}},
                         {"id": 2, "name": "router::r1", "options": {}}]
        plan = self.plan("set_primary_cluster:drcluster")
        cs["routers"].reverse()
        cs["routers"][1]["options"]["tags"] = {"b": 1, "a": False}
        self.apply(plan)
        self.assertEqual(self.sent, ["\n".join(plan["session_guard"] + plan["statements"])])

    def test_switchover_routing_snapshot_is_bound_by_the_review_token(self):
        plan = self.plan("set_primary_cluster:drcluster")
        changed = copy.deepcopy(plan)
        changed["expect"]["routing_configuration"]["defaults"]["options"]["target_cluster"] = "drcluster"
        result = self.apply(changed)
        self.assertEqual(result["status"], "refused", result)
        self.assertEqual(self.sent, [])

    def test_plan_binds_only_member_addresses_from_this_sets_reviewed_metadata(self):
        self.report._clusterset_instances["unrelated-cluster"] = [
            {"server_uuid": SPARE_UUID, "address": "unrelated:3306"}]
        plan = self.plan()
        identity = plan["expect"]["server_identity"]
        self.assertEqual(identity["server_uuid"], self.report["server"]["server_uuid"])
        self.assertEqual(cluster_shell._clusterset_identity("\n".join(
            plan["session_guard"] + plan["statements"])), identity["adminapi"])
        addresses = identity["adminapi"]["metadata_addresses"]
        expected = sorted({instance["address"] for cluster_id in self.report._clusterset_ids.values()
                           for instance in self.report._clusterset_instances[cluster_id]})
        self.assertEqual(addresses, expected)
        self.assertNotIn("unrelated:3306", addresses)
        self.assertNotIn("fixture:3306", addresses, "the saved connection alias is not a metadata member")

    def test_guard_identity_fields_cannot_be_changed_under_a_review_token(self):
        for field, replacement in (("name", "replacement-set"), ("roles", {}), ("invalidated", {}),
                                   ("primary_cluster", "drcluster"), ("view_id", "99"),
                                   ("metadata_addresses", ["unreviewed:3306"])):
            with self.subTest(field=field):
                plan = self.plan()
                plan["expect"]["server_identity"]["adminapi"][field] = replacement
                result = self.apply(plan)
                self.assertEqual(result["status"], "refused", result)
                self.assertEqual(self.sent, [])

    def test_diagnostic_review_cannot_authorize_an_appended_mutation(self):
        plan = self.plan("check_clusterset")
        plan["statements"].append(cluster.CLUSTER_ACTION_TAG + "myflamesClusterSet.dissolve();")
        result = self.apply(plan)
        self.assertEqual(result["status"], "refused", result)
        self.assertEqual(self.sent, [])

    def test_altered_js_and_replayed_token_are_refused_by_real_registry(self):
        plan = self.plan()
        tampered = copy.deepcopy(plan)
        tampered["statements"][0] += " dba.dropMetadataSchema();"
        result = self.apply(tampered)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(self.sent, [])
        self.mutate_after = self.store_primary_option
        result = self.apply(plan)
        self.assertEqual(result["outcome_class"], "verified")
        replay = self.apply(plan)
        self.assertEqual(replay["status"], "refused")
        self.assertEqual(len(self.sent), 1)

    def test_wrong_typed_confirmation_refuses_before_shell(self):
        plan = self.plan("remove_cluster:drcluster")
        result = self.apply(plan, typed="another-cluster")
        self.assertEqual(result["status"], "refused", result)
        self.assertEqual(self.sent, [])

    def test_zero_exit_with_warning_and_unchanged_sql_is_unknown_or_partial(self):
        plan = self.plan()
        self.warning = "WARNING: Failed to store option on a server.\n"
        result = self.apply(plan)
        self.assertEqual(result["outcome_class"], "unknown or partial", result)
        self.assertEqual(result["shell_stderr"], self.warning)
        self.assertTrue(result["verification"]["mismatches"])
        self.assertIn("router::rw", result["message"])

    def test_routing_success_does_not_require_a_readable_replica_route(self):
        plan = self.plan()

        def changed():
            self.store_primary_option()
            target = next(c for c in self.report["clusterset"]["clusters"] if c["name"] == "drcluster")
            target.update(read="failed", read_error="Route did not answer", route=None)

        self.mutate_after = changed
        result = self.apply(plan)
        self.assertEqual(result["outcome_class"], "verified", result)
        self.assertEqual(result["status"], "done", result)
        self.assertFalse(result["verification"]["missing"])
        self.assertFalse(result["verification"]["mismatches"])

    def test_read_only_status_returns_shell_output_and_is_audited_once(self):
        self.connection["read_only"] = True
        plan = self.plan("check_clusterset")
        self.assertTrue(plan["read_only"])
        self.assertIsNone(plan["typed_guard"])
        result = self.apply(plan, confirmed=False)
        self.assertEqual(result["outcome_class"], "verified", result)
        self.assertEqual(result["shell_output"], self.output)
        self.assertEqual(len(dbops.audit_report(self.store)["records"]), 1)
        self.assertIn(".status(", self.sent[0])

    def test_valid_degraded_status_is_done_with_observations_not_health_expectations(self):
        replica = next(c for c in self.report["clusterset"]["clusters"] if c["role"] == "REPLICA")
        replica.update(quorum=False, primary=None, status="NOT_OK")
        replica["channel"].update(receiver_state="OFF", applier_state="OFF")
        self.report["clusterset"]["from_local_copy"] = True
        raw = json.loads(self.output)
        raw["status"] = "AVAILABLE"
        raw["clusters"][replica["name"]].update(globalStatus="NOT_OK", clusterSetReplicationStatus="STOPPED")
        self.output = json.dumps(raw)
        result = self.apply(self.plan("check_clusterset"))
        self.assertEqual((result["status"], result["outcome_class"]), ("done", "verified"), result)
        self.assertEqual(result["verification"]["missing"], [])
        self.assertEqual(result["verification"]["mismatches"], [])
        facts = result["verification"]["facts"]
        self.assertTrue(any(f["actual"] == "NOT_OK" for f in facts), facts)
        self.assertTrue(any(f["actual"] == "STOPPED" for f in facts), facts)

    def test_stored_routing_effect_is_verified_while_replica_channels_are_stopped(self):
        replica = next(c for c in self.report["clusterset"]["clusters"] if c["role"] == "REPLICA")
        replica.update(quorum=False, primary=None)
        replica["channel"].update(receiver_state="OFF", applier_state="OFF")
        self.mutate_after = self.store_primary_option
        result = self.apply(self.plan())
        self.assertEqual((result["status"], result["outcome_class"]), ("done", "verified"), result)
        self.assertTrue(any("stored target_cluster" in f["fact"] for f in result["verification"]["facts"]))

    def test_valid_raw_status_survives_password_collisions_with_domain_and_json_keys(self):
        for password in (self.report["clusterset"]["name"], "domainName", "clusters", "status", "OK"):
            with self.subTest(password=password):
                self.connection["password"] = self.conn.password = password
                result = self.apply(self.plan("check_clusterset"))
                self.assertEqual((result["status"], result["outcome_class"]), ("done", "verified"), result)
                self.assertNotIn(password, result["shell_output"])
                self.assertEqual(result["verification"]["missing"], [])
                self.assertNotIn(password, json.dumps([f["actual"] for f in result["verification"]["facts"]]))

    def test_raw_wrong_domain_or_invalid_shape_cannot_be_hidden_by_redaction(self):
        for change in ("domain", "clusters", "cluster entry", "status", "duplicate key", "nonfinite value"):
            with self.subTest(change=change):
                raw = json.loads(self.output)
                if change == "domain":
                    raw["domainName"] = "another-set"
                elif change == "clusters":
                    raw["clusters"] = {}
                elif change == "cluster entry":
                    raw["clusters"]["drcluster"] = "unread"
                elif change == "status":
                    raw["status"] = 1
                output = json.dumps(raw)
                if change == "duplicate key":
                    output = '{"domainName":"another-set",' + output[1:]
                elif change == "nonfinite value":
                    output = '{"notJSON":NaN,' + output[1:]
                original = self.output
                self.output = output
                result = self.apply(self.plan("check_clusterset"))
                self.output = original
                self.assertEqual(result["outcome_class"], "done, not fully verified", result)
                self.assertTrue(result["verification"]["missing"])

    def test_zero_exit_progress_text_does_not_prove_a_status_report(self):
        self.output = "Shell finished with a warning\n"
        result = self.apply(self.plan("check_clusterset"))
        self.assertEqual(result["outcome_class"], "done, not fully verified")
        self.assertIn("extended status JSON", result["message"])

    def test_large_shell_results_are_byte_bounded_tails_with_truncation_flags(self):
        limit = 1024 * 1024
        self.output = "discarded prefix\n" + "\N{SNOWMAN}" * (limit // 3 + 20) + "output tail"
        self.warning = "discarded warning\n" + "w" * limit + "stderr tail"
        result = self.apply(self.plan())
        for key, original in (("shell_output", self.output), ("shell_stderr", self.warning)):
            self.assertLessEqual(len(result[key].encode("utf-8")), limit)
            self.assertEqual(result[key], original.encode("utf-8")[-limit:].decode("utf-8", "ignore"))
            self.assertTrue(result[key + "_truncated"])
        record = dbops.audit_report(self.store)["records"][0]
        self.assertNotIn("discarded prefix", json.dumps(record))
        self.assertNotIn("discarded warning", json.dumps(record))

    def test_small_shell_results_explicitly_report_no_truncation(self):
        result = self.apply(self.plan())
        self.assertEqual(result["shell_output"], self.output)
        self.assertFalse(result["shell_output_truncated"])
        self.assertFalse(result["shell_stderr_truncated"])

    def test_truncated_output_cannot_turn_an_invalid_report_into_verified_json(self):
        # Truncation drops the invalid prefix and leaves a parseable JSON
        # suffix. A partial output must not become complete status evidence.
        self.output = "unparseable server output\n" + " " * (1024 * 1024) + self.output
        result = self.apply(self.plan("check_clusterset"))
        self.assertTrue(result["shell_output_truncated"])
        self.assertEqual(result["outcome_class"], "done, not fully verified")

    def test_valid_complete_report_is_verified_before_display_retention(self):
        raw = json.loads(self.output)
        raw["padding"] = "x" * (execution.SHELL_OUTPUT_LIMIT + 1)
        self.output = json.dumps(raw)
        result = self.apply(self.plan("check_clusterset"))
        self.assertTrue(result["shell_output_truncated"])
        self.assertEqual(result["outcome_class"], "verified", result)

    def test_numeric_errors_alone_cannot_prove_a_premutation_shell_refusal(self):
        for code, action, location, expected in (
            (51317, "set_primary_cluster:drcluster", 2, "unknown or partial"),
            (51317, "set_primary_cluster:drcluster", 3, "unknown or partial"),
            (51317, "set_routing_option:router::rw", 2, "unknown or partial"),
            (51157, "set_primary_cluster:drcluster", 2, "unknown or partial"),
            (2013, "set_primary_cluster:drcluster", 2, "unknown or partial"),
        ):
            with self.subTest(code=code, action=action, location=location):
                plan = self.plan(action)
                self.error = "mysqlsh exited 1: ERROR %d at line %d: synthetic precondition" % (code, location)
                result = self.apply(plan)
                self.assertEqual(result["outcome_class"], expected, result)
                self.assertEqual(result["shell_error_info"]["source"], "unknown")

    def test_provenance_is_retained_and_only_guard_failures_prove_no_send(self):
        for source, stage, location, expected in (
            ("mysql", "adminapi", 2, "unknown or partial"),
            ("mysqlsh", "adminapi", 2, "unknown or partial"),
            ("unknown", "identity_guard", 1, "failed, nothing changed"),
            ("mysqlsh", "unknown", 2, "unknown or partial"),
            ("mysqlsh", "identity_guard", 1, "failed, nothing changed"),
        ):
            with self.subTest(source=source, stage=stage):
                self.error = "mysqlsh exited 1: ERROR 51317 at line %d: synthetic failure" % location
                self.error_info = {"source": source, "code": 51317, "line": location, "stage": stage}
                result = self.apply(self.plan("set_primary_cluster:drcluster"))
                self.assertEqual(result["outcome_class"], expected, result)
                self.assertEqual(result["shell_error_info"], self.error_info)
                record = next(r for r in dbops.audit_report(self.store)["records"] if r["id"] == result["audit_id"])
                self.assertEqual(record["after"]["shell_error_info"], self.error_info)


class CandidateEndpointTests(EndpointHarness):
    """A second saved target must retain its own write consent until spawn."""
    def setUp(self):
        super().setUp()
        active = self.store.save("profile", dict(name="Primary", host="fixture", port=3306,
                                 user="admin", group="G", type="development"), "primary",
                                 password="secret-split", remember=True)
        self.store.record_topology_identity(active["id"], cluster.target_server_identity(self.report))
        self.spare = self.store.save("profile", dict(name="Spare", host="spare", port=3306,
                                     user="admin", group="G", type="development"), "spare",
                                     password="secret-spare", remember=True)
        self.store.record_topology_identity("spare", {"server_uuid": SPARE_UUID})
        self.connection.update(profile_id="primary", group_name="G")
        self.candidate = CandidateConnection()
        self.config_reads = 0
        self.on_config = None
        self.on_send = None
        self.send_timeouts = []
        self.send_idle_timeouts = []
        original_run = self.conn.run

        def source_run(sql, *args, **kwargs):
            if "SELECT @@global.server_uuid, @@global.gtid_executed" in sql:
                return F.batch([[self.report["server"]["server_uuid"], ""]])
            return original_run(sql, *args, **kwargs)

        @contextmanager
        def opener(profile):
            yield self.candidate

        def send(writer, script, **kwargs):
            if "dba.checkInstanceConfiguration()" in script:
                self.config_reads += 1
                if self.on_config:
                    self.on_config()
                return cluster_shell._status_output('MYFLAMES_CONFIG={"status":"ok"}\n', getattr(writer, "password", None))
            if self.on_send:
                self.on_send()
            guard = getattr(writer, "_login_guard", None)
            if guard:
                guard()
            self.sent.append(script)
            self.send_timeouts.append(kwargs.get("timeout"))
            self.send_idle_timeouts.append(kwargs.get("idle_timeout"))
            if self.error:
                raise ConnectorError(self.error)
            return "Created replica cluster.\n"

        for patch in (mock.patch.object(self.conn, "run", side_effect=source_run),
                      mock.patch("myflames.replication.profile_opener", return_value=opener),
                      mock.patch.object(cluster_shell.ShellWriter, "run_write", new=send)):
            patch.start()
            self.addCleanup(patch.stop)

    # This class reuses endpoint helpers, not the unrelated routing test cases.
    def plan(self, action="create_replica_cluster:spare", parameters=None):
        return super().plan(action, parameters or {"name": "newcluster", "recoveryMethod": "incremental"})

    def test_clone_review_issues_token_and_sends_bound_action_with_clone_limits(self):
        tokens = ui._review_tokens(self.workspace)
        with mock.patch.object(tokens, "issue", wraps=tokens.issue) as issue:
            plan = self.plan(parameters={"name": "newcluster", "recoveryMethod": "clone"})
        issue.assert_called_once()
        self.assertTrue(plan["review_token"])
        self.assertEqual(self.sent, [])
        self.assertEqual(dbops.audit_report(self.store)["records"], [])
        snapshots = []
        self.on_send = lambda: snapshots.append(ui.api_response(
            "/api/live/action/progress", {"progress_id": "p" * 24}, self.workspace))
        self.apply(plan)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0], "\n".join(reviewed_writes.session_guard(plan) + plan["statements"]))
        self.assertEqual(self.send_timeouts, [cluster_shell.CLONE_TIMEOUT])
        self.assertEqual(self.send_idle_timeouts, [cluster_shell.CLONE_IDLE_TIMEOUT])
        self.assertEqual(snapshots[0]["timeout_s"], cluster_shell.CLONE_TIMEOUT)
        self.assertEqual(snapshots[0]["idle_timeout_s"], cluster_shell.CLONE_IDLE_TIMEOUT)

    def test_previously_reviewed_clone_without_target_binding_is_refused_before_send(self):
        # Even a genuine review token cannot let an old unbound plan pass the
        # fresh plan comparison. No classification or policy gate is mocked.
        tokens = ui._review_tokens(self.workspace)
        with mock.patch.object(tokens, "issue", wraps=tokens.issue) as issue:
            plan = self.plan(parameters={"name": "newcluster", "recoveryMethod": "clone"})
        operation, identity = issue.call_args[0][:2]
        plan["expect"]["server_identity"].pop("target_instance")
        plan["session_guard"] = reviewed_writes.session_guard(plan)
        plan["review_token"] = tokens.issue(operation, identity, reviewed_writes.review_details(plan))
        result = self.apply(plan)
        self.assertEqual(result["status"], "refused", result)
        self.assertEqual(result["sent"], [])
        self.assertEqual(self.sent, [])
        records = dbops.audit_report(self.store)["records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "refused")

    def test_incremental_progress_keeps_the_shell_limit_without_clone_idle_limit(self):
        plan = self.plan(parameters={"name": "newcluster", "recoveryMethod": "incremental"})
        snapshots = []
        self.on_send = lambda: snapshots.append(ui.api_response(
            "/api/live/action/progress", {"progress_id": "p" * 24}, self.workspace))
        self.apply(plan)
        snapshots.append(ui.api_response("/api/live/action/progress", {"progress_id": "p" * 24},
                                         self.workspace))
        self.assertEqual([item["phase"] for item in snapshots], ["sending", "finished"])
        self.assertEqual(plan["timeout_s"], cluster.SHELL_TIMEOUT)
        self.assertNotIn("idle_timeout_s", plan)
        for snapshot in snapshots:
            self.assertEqual(snapshot["timeout_s"], cluster.SHELL_TIMEOUT, snapshot)
            self.assertIsNone(snapshot.get("idle_timeout_s"), snapshot)
        self.assertEqual(self.send_timeouts, [cluster.SHELL_TIMEOUT])
        self.assertEqual(self.send_idle_timeouts, [None])

    def test_spare_read_only_enabled_after_review_refuses_before_credentials(self):
        from myflames import secret_store
        plan = self.plan()
        self.store.set_profile_safety("spare", read_only=True)
        with mock.patch.object(secret_store, "get_secret") as lookup, \
                mock.patch("myflames.connector.MySQLConnection") as connection:
            result = self.apply(plan)
        self.assertEqual(result["status"], "refused", result)
        self.assertEqual(result["sent"], [])
        self.assertEqual(self.sent, [])
        lookup.assert_not_called()
        connection.assert_not_called()

    def test_spare_policy_changed_during_preflight_or_at_login_refuses(self):
        for phase in ("preflight", "login"):
            with self.subTest(phase=phase):
                self.store.set_profile_safety("spare", read_only=False)
                self.on_config = self.on_send = None
                plan = self.plan()
                flip = lambda: self.store.set_profile_safety("spare", read_only=True)
                if phase == "preflight":
                    self.on_config = flip
                else:
                    self.on_send = flip
                result = self.apply(plan)
                self.assertEqual(result["status"], "refused", result)
                self.assertEqual(result["sent"], [])
                self.assertEqual(self.sent, [])

    def test_creation_timeout_is_unknown_and_audited_without_retry(self):
        self._assert_creation_error_audited("MySQL Shell timed out after 600s; the outcome is unknown.")

    def test_stopped_creation_is_unknown_and_audited_without_retry(self):
        self._assert_creation_error_audited("MySQL Shell was stopped (signal 15); the outcome is unknown.")

    def _assert_creation_error_audited(self, message):
        plan = self.plan()
        self.error = message
        result = self.apply(plan)
        self.assertEqual(result["status"], "unknown", result)
        self.assertEqual(result["outcome_class"], "unknown or partial")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.send_timeouts, [plan["timeout_s"]])
        self.assertEqual(self.send_idle_timeouts, [None])
        progress = ui.api_response("/api/live/action/progress", {"progress_id": "p" * 24}, self.workspace)
        self.assertEqual((progress["phase"], progress["status"]), ("finished", "unknown"), progress)
        self.assertEqual(progress["timeout_s"], plan["timeout_s"])
        self.assertIsNone(progress.get("idle_timeout_s"))
        records = dbops.audit_report(self.store)["records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "unknown")
        self.assertEqual(records[0]["outcome"], "unknown or partial")
        self.assertIn("Unknown or partial", records[0]["message"])
        self.assertIn(message, result["error"])


class VerificationFactsTests(unittest.TestCase):
    def test_reroute_never_opens_unclassified_profiles_when_member_uuids_are_missing(self):
        before = topology()
        primary = next(item for item in before['clusterset']['clusters'] if item['role'] == 'PRIMARY')
        for member in primary['members']:
            member['server_uuid'] = None
        profiles = [{'id': 'no-identity', 'name': 'Unknown', 'group': 'G'},
                    {'id': 'blank', 'name': 'Blank UUID', 'group': 'G',
                     'topology_identity': {'server_uuid': ''}},
                    {'id': 'invalid', 'name': 'Invalid UUID', 'group': 'G',
                     'topology_identity': {'server_uuid': 'not-a-server-uuid'}}]
        opened = []

        def opener(profile):
            opened.append(profile['id'])
            raise AssertionError('An unclassified profile must never be opened.')

        initial = {'server': {}, 'clusterset': None}
        with mock.patch.object(cluster, 'read_status', return_value=initial):
            after = verify._read_after(object(), before, {'profiles': profiles, 'open_profile': opener},
                                       verify._Evidence())
        self.assertIs(after, initial)
        self.assertEqual(opened, [])

    def test_rerouted_post_action_report_must_match_the_route_uuid(self):
        before = topology()
        primary = next(item for item in before['clusterset']['clusters'] if item['role'] == 'PRIMARY')
        uuid = primary['primary']['server_uuid']
        profile = {'id': 'primary', 'name': 'Primary', 'group': 'G',
                   'topology_identity': {'server_uuid': uuid}}
        initial = {'server': {}, 'clusterset': None}
        for reported, accepted in ((uuid, True), (uuid.upper(), True),
                                   ('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', False), (None, False)):
            with self.subTest(reported=reported):
                peer = F.conn('ic-8.4-clusterset-primary')
                peer.scenario['variables']['server_uuid'] = uuid
                candidate = {'server': {'server_uuid': reported}, 'clusterset': {'name': 'mfcs'}}
                opened = []

                def opener(saved):
                    opened.append(saved['id'])
                    return peer

                evidence = verify._Evidence()
                options = {'profiles': [profile], 'open_profile': opener}
                with mock.patch.object(cluster, 'read_status', side_effect=[initial, candidate]):
                    after = verify._read_after(object(), before, options, evidence)
                self.assertEqual(opened, ['primary'])
                self.assertTrue(peer.closed)
                self.assertIs(after, candidate if accepted else initial)
                self.assertEqual(bool(after.get('clusterset')), accepted)

    def test_switchover_requires_higher_view_and_new_primary_roles(self):
        before, after = topology(), topology()
        plan = {"action": "set_primary_cluster:drcluster", "expect": {"clusterset_view_id": before["clusterset"]["view_id"]}}
        evidence = verify._Evidence()
        verify._verify_membership(evidence, plan, before, after)
        self.assertIn("mfcs: membership view advanced", evidence.mismatches)
        self.assertIn("mfcs: primary cluster", evidence.mismatches)
        after["clusterset"]["view_id"] = str(int(before["clusterset"]["view_id"]) + 1)
        after["clusterset"]["primary_cluster"] = "drcluster"
        for item in after["clusterset"]["clusters"]:
            item["role"] = "PRIMARY" if item["name"] == "drcluster" else "REPLICA"
        evidence = verify._Evidence()
        verify._verify_membership(evidence, plan, before, after)
        self.assertEqual(evidence.mismatches, [])
        self.assertEqual(evidence.missing, [])

    def test_failover_requires_view_id_high_counter_to_advance(self):
        before, after = topology(), topology()
        before["clusterset"]["view_id"] = "3"
        plan = {"action": "force_primary_cluster:drcluster", "expect": {"clusterset_view_id": "3"}}
        after["clusterset"]["primary_cluster"] = "drcluster"
        for item in after["clusterset"]["clusters"]:
            if item["name"] == "drcluster":
                item["role"], item["invalidated"] = "PRIMARY", False
            else:
                item["role"], item["invalidated"] = None, True
        # Advancing only the low 32-bit counter does not prove a failover view.
        after["clusterset"]["view_id"] = "4"
        evidence = verify._Evidence()
        verify._verify_membership(evidence, plan, before, after)
        self.assertIn("mfcs: membership view high counter advanced", evidence.mismatches)
        # Advancing the high 32-bit counter (1 << 32 | 3) satisfies both checks.
        after["clusterset"]["view_id"] = str((1 << 32) | 3)
        evidence = verify._Evidence()
        verify._verify_membership(evidence, plan, before, after)
        self.assertEqual(evidence.mismatches, [])
        self.assertEqual(evidence.missing, [])

    def test_primary_actions_target_primary_route_while_failover_targets_surviving_replica(self):
        report = topology()
        cs = report["clusterset"]
        invalidated = {
            "name": "oldcluster", "role": None, "invalidated": True, "local": False,
            "read": "route", "read_error": None, "status": "INVALIDATED", "quorum": True,
            "fenced_writes": True, "channel": None,
            "primary": {"label": "old-1:3306", "address": "old-1:3306",
                        "server_uuid": "aaaaaaaa-bbbb-cccc-dddd-000000000099", "state": "ONLINE", "role": "PRIMARY"},
            "members": [{"label": "old-1:3306", "address": "old-1:3306",
                         "server_uuid": "aaaaaaaa-bbbb-cccc-dddd-000000000099", "state": "ONLINE", "role": "PRIMARY"}],
            "route": {"profile_id": "route:oldcluster", "profile_name": "oldcluster",
                      "self": False, "server_uuid": "aaaaaaaa-bbbb-cccc-dddd-000000000099"},
        }
        cs["clusters"].append(invalidated)
        primary_pid = cs["clusters"][0]["route"]["profile_id"]
        spare_profile = {"id": "spare", "name": "Spare", "host": "spare", "port": 3306,
                         "group": "G", "topology_identity": {"server_uuid": SPARE_UUID}}
        checks = {"shell": SHELL, "group_name": "G", "group_profiles": [spare_profile],
                  "replica_candidates": [{"profile_id": "spare", "read": "route", "server_uuid": SPARE_UUID,
                                          **execution.candidate_binding(spare_profile),
                                          "route": {"profile_id": "spare", "profile_name": "Spare",
                                                    "self": False, "server_uuid": SPARE_UUID},
                                          "address": "spare:3306", "reported_address": "spare:3306",
                                          "server_version": "8.4.13", "engine": "mysql",
                                          "read_error": None, "kind": "none", "group_name": "",
                                          "gtid_subset": True, "super_read_only": True}]}
        actions = {a["id"]: a for a in cluster.overview_actions(report, checks)}
        for aid in ("rejoin_cluster:oldcluster", "set_primary_cluster:drcluster",
                    "remove_cluster:drcluster", "create_replica_cluster:spare", "dissolve_clusterset"):
            self.assertEqual(actions[aid]["target"]["profile_id"], primary_pid, aid)
        # When oldcluster has a verified route that is OFFLINE, the fix should not ask to add a saved connection.
        invalidated.update(quorum=None, primary=None, read_error="The route is not an online group member.")
        offline_actions = {a["id"]: a for a in cluster.overview_actions(report, checks)}
        self.assertFalse(offline_actions["rejoin_cluster:oldcluster"]["enabled"])
        self.assertIn("Restore quorum", offline_actions["rejoin_cluster:oldcluster"]["fix"])
        # When the primary cluster is unreachable, failover targets the surviving replica cluster route.
        primary = cs["clusters"][0]
        primary.update(route=None, read="failed", primary=None, quorum=None, status="NOT_READ")
        report._clusterset_reachability = {primary["name"]: [
            {"server_uuid": m["server_uuid"], "reachable": False} for m in primary["members"]]}
        failover_actions = {a["id"]: a for a in cluster.overview_actions(report, checks)}
        self.assertEqual(failover_actions["force_primary_cluster:drcluster"]["target"]["profile_id"],
                         "route:drcluster")

    def test_channels_and_sro_must_match_on_every_read_replica_primary(self):
        report = topology()
        target = next(c for c in report["clusterset"]["clusters"] if c["role"] == "REPLICA")
        target["channel"]["receiver_state"] = "OFF"
        target["channel"]["applier_state"] = "OFF"
        target["super_read_only"] = False
        evidence = verify._Evidence()
        verify._verify_live(evidence, report, report)
        self.assertTrue(any("receiver ON" in item for item in evidence.mismatches))
        self.assertTrue(any("applier ON" in item for item in evidence.mismatches))
        self.assertTrue(any("super_read_only" in item for item in evidence.mismatches))

    def test_primary_sro_facts_are_independent_of_write_fencing(self):
        cases = (("PRIMARY", False, None, "verified"),
                 ("PRIMARY", True, False, "mismatch"),
                 ("PRIMARY", None, False, "missing"),
                 ("REPLICA", True, False, "verified"),
                 ("REPLICA", True, None, "verified"),
                 ("REPLICA", False, True, "mismatch"),
                 ("REPLICA", None, True, "missing"))
        for role, sro, fenced, status in cases:
            with self.subTest(role=role, super_read_only=sro, fenced_writes=fenced):
                report = topology()
                target = next(c for c in report["clusterset"]["clusters"] if c["role"] == role)
                target.update(super_read_only=sro, fenced_writes=fenced)
                evidence = verify._Evidence()
                verify._verify_live(evidence, report, report)
                fact = next(f for f in evidence.facts
                            if f["server"] == target["name"] and f["fact"] == "primary super_read_only")
                self.assertIs(fact["expected"], role == "REPLICA")
                self.assertIs(fact["actual"], sro)
                self.assertEqual(fact["status"], status)

    def test_missing_raw_sro_does_not_fall_back_to_write_fencing(self):
        report = topology()
        for item in report["clusterset"]["clusters"]:
            item.pop("super_read_only")
            item["fenced_writes"] = item["role"] == "REPLICA"
        evidence = verify._Evidence()
        verify._verify_live(evidence, report, report)
        facts = [f for f in evidence.facts if f["fact"] == "primary super_read_only"]
        self.assertEqual(len(facts), 2)
        self.assertTrue(all(f["status"] == "missing" and f["actual"] is None for f in facts))

    def test_verify_requires_replica_member_action_disabled_and_marks_unread_unverified(self):
        cases = (
            (False, True, "verified"),
            (True, False, "mismatch"),
            (None, True, "missing"),
            ("absent", True, "missing"),
        )
        for action_enabled, fenced, expected in cases:
            with self.subTest(action_enabled=action_enabled, fenced_writes=fenced):
                report = topology()
                replica = next(c for c in report["clusterset"]["clusters"] if c["role"] == "REPLICA")
                replica["super_read_only"] = True
                replica["fenced_writes"] = fenced
                if action_enabled == "absent":
                    replica.pop("disable_super_read_only_if_primary", None)
                else:
                    replica["disable_super_read_only_if_primary"] = action_enabled
                evidence = verify._Evidence()
                verify._verify_live(evidence, report, report)
                fact = next(f for f in evidence.facts
                            if f["server"] == replica["name"] and
                            f["fact"] == "mysql_disable_super_read_only_if_primary enabled")
                self.assertIs(fact["expected"], False)
                self.assertEqual(fact["status"], expected)
                if expected == "verified":
                    self.assertEqual(evidence.mismatches, [])
                    self.assertEqual(evidence.missing, [])
                elif expected == "mismatch":
                    self.assertIn("drcluster: mysql_disable_super_read_only_if_primary enabled", evidence.mismatches)
                else:
                    self.assertIn("drcluster: mysql_disable_super_read_only_if_primary enabled", evidence.missing)

    def test_verify_checks_replica_source_uuid_independently_of_host_text(self):
        primary_uuid = topology()["clusterset"]["clusters"][0]["primary"]["server_uuid"]
        cases = (
            (primary_uuid, "verified"),
            (primary_uuid.upper(), "verified"),
            ("99999999-2222-11f0-8a00-0242ac130002", "mismatch"),
            ("", "mismatch"),
            (None, "missing"),
        )
        for source_uuid, expected in cases:
            with self.subTest(source_uuid=source_uuid):
                report = topology()
                replica = next(c for c in report["clusterset"]["clusters"] if c["role"] == "REPLICA")
                # Configured source host:port still matches the primary address.
                replica["channel"]["source_uuid"] = source_uuid
                evidence = verify._Evidence()
                verify._verify_live(evidence, report, report)
                fact = next(f for f in evidence.facts
                            if f["server"] == replica["name"] and f["fact"] == "clusterset_replication source UUID")
                self.assertEqual(fact["status"], expected)
                if expected == "verified":
                    self.assertEqual(evidence.mismatches, [])
                    self.assertEqual(evidence.missing, [])
                elif expected == "mismatch":
                    self.assertIn("drcluster: clusterset_replication source UUID", evidence.mismatches)
                else:
                    self.assertIn("drcluster: clusterset_replication source UUID", evidence.missing)

    def test_verify_requires_primary_channel_absent_or_stopped(self):
        cases = (
            ("absent", {"present": False, "workers": [], "read_error": None}, "verified"),
            ("stopped", {"present": True, "receiver_state": "OFF", "applier_state": "OFF",
                         "last_error_number": 0, "workers": [{"worker_id": 1, "state": "OFF", "last_error_number": 0}],
                         "read_error": None}, "verified"),
            ("running", {"present": True, "receiver_state": "ON", "applier_state": "ON",
                         "last_error_number": 0, "workers": [{"worker_id": 1, "state": "ON", "last_error_number": 0}],
                         "read_error": None}, "mismatch"),
            ("connecting", {"present": True, "receiver_state": "CONNECTING", "applier_state": "ON",
                            "last_error_number": 0, "workers": [], "read_error": None}, "mismatch"),
            ("receiver_error", {"present": True, "receiver_state": "OFF", "applier_state": "OFF",
                                "last_error_number": 2003, "workers": [], "read_error": None}, "mismatch"),
            ("worker_error", {"present": True, "receiver_state": "OFF", "applier_state": "OFF",
                              "last_error_number": 0,
                              "workers": [{"worker_id": 1, "state": "OFF", "last_error_number": 1062}],
                              "read_error": None}, "mismatch"),
            ("unread_none", None, "missing"),
            ("unread_present_none", {"present": None, "workers": [], "read_error": "denied"}, "missing"),
            ("stopped_with_read_error", {"present": True, "receiver_state": "OFF", "applier_state": "OFF",
                                         "last_error_number": 0, "workers": [], "read_error": "workers denied"},
             "missing"),
        )
        for name, channel, expected in cases:
            with self.subTest(case=name):
                report = topology()
                primary = next(c for c in report["clusterset"]["clusters"] if c["role"] == "PRIMARY")
                primary["channel"] = channel
                evidence = verify._Evidence()
                verify._verify_live(evidence, report, report)
                fact = next(f for f in evidence.facts
                            if f["server"] == primary["name"] and
                            f["fact"] == "primary clusterset_replication channel absent or stopped")
                self.assertEqual(fact["status"], expected)
                if expected == "verified":
                    self.assertEqual(evidence.mismatches, [])
                    self.assertEqual(evidence.missing, [])
                elif expected == "mismatch":
                    self.assertIn("mfcluster: primary clusterset_replication channel absent or stopped",
                                  evidence.mismatches)
                else:
                    self.assertIn("mfcluster: primary clusterset_replication channel absent or stopped",
                                  evidence.missing)

    def test_router_cluster_name_is_verified_against_stored_group_uuid(self):
        report = topology()
        plan = {"action": "set_routing_option:router::rw",
                "parameters": {"option": "target_cluster", "value": "drcluster"}}
        report["clusterset"]["routers"][0]["options"]["target_cluster"] = report._clusterset_groups["drcluster"]
        evidence = verify._Evidence()
        verify._verify_routing(evidence, plan, report, report)
        self.assertEqual(evidence.mismatches, [])
        self.assertEqual(evidence.missing, [])
        self.assertIn("traffic are not checked", evidence.facts[0]["fact"])

    def test_global_frequency_reset_verifies_versioned_key_presence_and_value(self):
        plan = {"action": "set_routing_option", "parameters": {"option": "stats_updates_frequency", "value": None}}
        for version, options, matches in (
            ("2.1.0", {"stats_updates_frequency": 0}, True),
            ("2.1.0", {}, False),
            ("2.1.0", {"stats_updates_frequency": None}, False),
            ("2.1.0", {"stats_updates_frequency": False}, False),
            ("2.1.0", {"stats_updates_frequency": "0"}, False),
            ("2.2.0", {}, True),
            ("2.2.0", {"stats_updates_frequency": 0}, False),
            ("2.2.0", {"stats_updates_frequency": None}, False),
            ("2.3.0", {}, True),
            ("2.4.0", {"stats_updates_frequency": 60}, False),
        ):
            with self.subTest(version=version, options=options):
                report = topology()
                report["metadata"]["schema_version"] = version
                report["clusterset"]["routing_options"]["options"] = options
                evidence = verify._Evidence()
                verify._verify_routing(evidence, plan, report, report)
                self.assertEqual(evidence.missing, [])
                self.assertEqual(bool(evidence.mismatches), not matches, evidence.facts)
                self.assertEqual(evidence.facts[0]["actual"]["present"], "stats_updates_frequency" in options)

    def test_global_frequency_reset_with_unread_version_is_unverified(self):
        plan = {"action": "set_routing_option", "parameters": {"option": "stats_updates_frequency", "value": None}}
        for version in (None, "", "unknown", "0.0.0", "2.2"):
            with self.subTest(version=version):
                report = topology()
                report["metadata"]["schema_version"] = version
                evidence = verify._Evidence()
                verify._verify_routing(evidence, plan, report, report)
                self.assertTrue(evidence.missing)
                self.assertFalse(evidence.mismatches)

    def test_router_frequency_reset_requires_key_removal_at_every_metadata_version(self):
        plan = {"action": "set_routing_option:router::rw",
                "parameters": {"option": "stats_updates_frequency", "value": None}}
        for version in ("2.1.0", "2.2.0", "2.4.0", None):
            for options in ({}, {"stats_updates_frequency": None}, {"stats_updates_frequency": 0}):
                with self.subTest(version=version, options=options):
                    report = topology()
                    report["metadata"]["schema_version"] = version
                    report["clusterset"]["routers"][0]["options"] = options
                    evidence = verify._Evidence()
                    verify._verify_routing(evidence, plan, report, report)
                    self.assertFalse(evidence.missing)
                    self.assertEqual(bool(evidence.mismatches), bool(options), evidence.facts)

    def test_setting_explicit_frequency_zero_requires_stored_integer_zero(self):
        for action in ("set_routing_option", "set_routing_option:router::rw"):
            for options in ({}, {"stats_updates_frequency": 0}, {"stats_updates_frequency": False},
                            {"stats_updates_frequency": "0"}, {"stats_updates_frequency": 0.0}):
                with self.subTest(action=action, options=options):
                    report = topology()
                    report["clusterset"]["routing_options"]["options"] = options
                    report["clusterset"]["routers"][0]["options"] = options
                    plan = {"action": action, "parameters": {"option": "stats_updates_frequency", "value": 0}}
                    evidence = verify._Evidence()
                    verify._verify_routing(evidence, plan, report, report)
                    self.assertFalse(evidence.missing)
                    self.assertEqual(not evidence.mismatches, type(options.get("stats_updates_frequency")) is int)

    def test_new_cluster_metadata_without_online_expected_member_is_not_verified(self):
        before, after = topology(), topology()
        plan = {"action": "create_replica_cluster:spare", "parameters": {"name": "newcluster"},
                "expect": {"replica_candidate": {"server_uuid": SPARE_UUID}}}
        after["clusterset"]["view_id"] = str(int(before["clusterset"]["view_id"]) + 1)
        after["clusterset"]["clusters"].append({"name": "newcluster", "role": "REPLICA",
                                                   "members": [{"server_uuid": SPARE_UUID, "state": "RECOVERING"}]})
        evidence = verify._Evidence()
        verify._verify_membership(evidence, plan, before, after)
        self.assertIn("newcluster: new member ONLINE", evidence.mismatches)

    def cleanup(self, keep=False, dissolve=True, schema=False, channels=None, performance_schema=True):
        before = topology()
        item = before["clusterset"]["clusters"][0]
        uuid = item["primary"]["server_uuid"]
        queries = []

        def rows(sql):
            queries.append(sql)
            if sql == "SELECT @@performance_schema":
                return [["ON" if performance_schema else "OFF"]]
            if sql == cluster._MD_SCHEMA_SQL:
                return [["mysql_innodb_cluster_metadata"]] if schema else []
            if sql == cluster._CLUSTERSET_SNAPSHOT_QUERIES[3][1]:
                return [["cid-1", "=" + item["name"], "", before._clusterset_groups[item["name"]]]]
            if sql == cluster._CLUSTERSET_SNAPSHOT_QUERIES[1][1]:
                return []
            if "FROM performance_schema.replication_group_members" in sql:
                return [[uuid, "ONLINE", "PRIMARY"]] if keep else []
            if sql == "SELECT @@global.super_read_only":
                return [["OFF" if keep else "ON"]]
            if "FROM performance_schema.persisted_variables" in sql:
                return [["group_replication_start_on_boot", "OFF"], ["skip_replica_start", "OFF"]]
            if sql.startswith("SELECT CHANNEL_NAME FROM performance_schema."):
                return channels or []
            raise AssertionError("Unexpected cleanup SQL: " + sql)

        evidence = verify._Evidence()
        verify._inspect_cleanup(types.SimpleNamespace(rows=rows), evidence, "fixture-primary", uuid,
                                item, before, keep, dissolve)
        return evidence, queries

    def test_dissolve_requires_schema_and_both_managed_channels_gone(self):
        evidence, queries = self.cleanup()
        self.assertEqual(evidence.missing, [])
        self.assertEqual(evidence.mismatches, [])
        self.assertTrue(any("read_replica_replication" in sql for sql in queries))
        evidence, _ = self.cleanup(schema=True, channels=[["read_replica_replication"]])
        self.assertIn("fixture-primary: metadata schema present", evidence.mismatches)
        self.assertTrue(any("managed channels removed" in fact for fact in evidence.mismatches))

    def test_remove_keep_requires_online_unfenced_standalone_cluster_and_preserved_identity(self):
        evidence, queries = self.cleanup(keep=True, dissolve=False, schema=True)
        self.assertEqual(evidence.missing, [])
        self.assertEqual(evidence.mismatches, [])
        self.assertTrue(any(fact["fact"] == "kept member ONLINE" for fact in evidence.facts))
        self.assertTrue(any(fact["fact"] == "kept member super_read_only" and fact["actual"] is False
                            for fact in evidence.facts))
        self.assertFalse(any("read_replica_replication" in sql for sql in queries))

    def test_disabled_performance_schema_empty_tables_cannot_prove_cleanup(self):
        evidence, queries = self.cleanup(performance_schema=False)
        self.assertIn("fixture-primary: replication cleanup", evidence.missing)
        self.assertFalse(any("CHANNEL_NAME" in sql for sql in queries))


class ShellProgressTests(Home):
    def test_status_json_escaped_secrets_are_redacted_in_output_facts_and_progress(self):
        report = topology()
        plan = cluster.plan_action(report, "check_clusterset", checks={"shell": SHELL})
        script = "\n".join(reviewed_writes.session_guard(plan) + plan["statements"])
        for password in ('quoted"secret', "uniçøde-秘密", "path\\with/slashes"):
            for ensure_ascii in (True, False):
                with self.subTest(password=password, ensure_ascii=ensure_ascii):
                    raw = json.dumps({"domainName": report["clusterset"]["name"], "status": "UNAVAILABLE",
                                      "clusters": {c["name"]: {"globalStatus": "NOT_OK", "clusterErrors": [password]}
                                                   for c in report["clusterset"]["clusters"]},
                                      "key:" + password: password}, ensure_ascii=ensure_ascii, indent=2).encode("utf-8")
                    if ensure_ascii:
                        # json.dumps does not emit the valid JSON \/ escape.
                        raw = raw.replace(b"/", b"\\/")
                    conn = types.SimpleNamespace(host="fixture", port=3306, user="admin", password=password)
                    progress = reviewed_writes.Progress(1)

                    def run(argv, timeout, **kwargs):
                        # Split escaped passwords across transport chunks.
                        for offset in range(0, len(raw), 7):
                            kwargs["on_output"]("stdout", raw[offset:offset + 7])
                        return subprocess.CompletedProcess(argv, 0, raw, b"")

                    with mock.patch("myflames.connector.checked_shell_program", return_value=SHELL["path"]), \
                            mock.patch("myflames.connector._run_bounded", side_effect=run):
                        writer = cluster_shell.ShellWriter(conn, SHELL, progress=progress)
                        output = writer.run_write(script)
                    result = execution.finish({"status": "done", "states": ["done"]}, plan,
                                              {"shell_output": output, "shell_stderr": writer.stderr})
                    self.assertEqual(result["outcome_class"], "verified", result)
                    self.assertNotIn(password, json.dumps(json.loads(result["shell_output"]), ensure_ascii=False))
                    for ascii_variant in (True, False):
                        escaped = json.dumps(password, ensure_ascii=ascii_variant)[1:-1]
                        self.assertNotIn(escaped, result["shell_output"])
                        self.assertNotIn(escaped, "\n".join(item["text"] for item in progress.snapshot()["output"]))

    def test_raw_status_validation_evidence_survives_the_real_sender_adapter(self):
        # The state adapter retains a redacted string, not a raw JSON object.
        report = topology()
        plan = cluster.plan_action(report, "check_clusterset", checks={"shell": SHELL})
        raw = json.dumps({"domainName": report["clusterset"]["name"], "status": "UNAVAILABLE",
                          "clusters": {c["name"]: {"globalStatus": "NOT_OK"}
                                       for c in report["clusterset"]["clusters"]}}).encode("utf-8")
        conn = types.SimpleNamespace(host="fixture", port=3306, user="admin", password="domainName")
        state = {"plan": plan, "shell": SHELL}
        with mock.patch("myflames.connector.checked_shell_program", return_value=SHELL["path"]), \
                mock.patch("myflames.connector._run_bounded", return_value=subprocess.CompletedProcess([], 0, raw, b"")):
            cluster._Sender(conn, state).run_write("\n".join(reviewed_writes.session_guard(plan) + plan["statements"]))
        self.assertIsInstance(state["shell_output"], str)
        self.assertNotIn("domainName", state["shell_output"])
        self.assertNotIn("domainName", json.dumps(state["shell_output"]))
        result = execution.finish({"status": "done", "states": ["done"]}, plan, state)
        self.assertEqual((result["status"], result["outcome_class"]), ("done", "verified"), result)

    def test_real_sender_preserves_server_error_provenance_and_unknown_mutation_stage(self):
        # An action call's line is not proof of its internal mutation stage.
        conn = types.SimpleNamespace(host="fixture", port=3306, user="admin", password="secret")
        state = {"plan": {"via": "mysqlsh"}, "shell": SHELL}
        for suffix, source in (("(MySQL Error 51317)", "mysql"), ("(MYSQLSH 51317)", "mysqlsh"),
                               ("(MYSQLSH 51317) (RuntimeError)", "unknown")):
            with self.subTest(source=source):
                stderr = ("ClusterSet.setPrimaryCluster: failed " + suffix + "\n at STDIN:2\n").encode("utf-8")
                with mock.patch("myflames.connector.checked_shell_program", return_value=SHELL["path"]), \
                        mock.patch("myflames.connector._run_bounded", return_value=
                                   subprocess.CompletedProcess([], 1, b"Refreshing replication account\n", stderr)):
                    with self.assertRaises(ConnectorError):
                        cluster._Sender(conn, state).run_write("guard;\ncall();")
                result = execution.finish({"status": "failed", "states": ["failed"], "error": "failed", "not_sent": []},
                                          {"action": "set_primary_cluster:drcluster", "statements": ["call();"]}, state)
                self.assertEqual(result["outcome_class"], "unknown or partial", result)
                self.assertEqual(result["shell_error_info"]["source"], source)
                self.assertEqual(result["shell_error_info"]["stage"], "adminapi")

    def test_result_cap_redacts_secrets_before_the_retention_boundary(self):
        limit = execution.SHELL_OUTPUT_LIMIT
        password = "retention-boundary-secret"
        conn = types.SimpleNamespace(host="fixture", port=3306, user="admin", password=password)
        # The retained raw tail would expose the password's final five bytes.
        raw = ("discarded-prefix" + password + "x" * (limit - 5)).encode("utf-8")
        with mock.patch("myflames.connector.checked_shell_program", return_value=SHELL["path"]), \
                mock.patch("myflames.connector._run_bounded", return_value=
                           subprocess.CompletedProcess([], 0, raw, raw)):
            writer = cluster_shell.ShellWriter(conn, SHELL)
            writer.run_write("print('output');", timeout=600)
        result = execution.finish({"status": "done", "states": ["done"]},
                                  {"action": "check_clusterset", "statements": []},
                                  {"shell_output": writer.stdout, "shell_stderr": writer.stderr})
        for key in ("shell_output", "shell_stderr"):
            self.assertLessEqual(len(result[key].encode("utf-8")), limit)
            self.assertNotIn(password[-5:], result[key])
            self.assertTrue(result[key + "_truncated"])

    def test_actual_shell_timeout_maps_to_unknown_and_requests_process_tree_cleanup(self):
        conn = types.SimpleNamespace(host="fixture", port=3306, user="admin", password="secret")
        with mock.patch("myflames.connector.checked_shell_program", return_value=SHELL["path"]), \
                mock.patch("myflames.connector._run_bounded", side_effect=
                           subprocess.TimeoutExpired("mysqlsh", cluster.SHELL_TIMEOUT)) as run:
            writer = cluster_shell.ShellWriter(conn, SHELL)
            with self.assertRaisesRegex(ConnectorError, "timed out after 600s; the outcome is unknown"):
                writer.run_write("myflamesClusterSet.createReplicaCluster();", timeout=cluster.SHELL_TIMEOUT)
        self.assertEqual(run.call_args[0][1], 600)
        self.assertTrue(run.call_args[1]["kill_tree"])
        self.assertNotIn("idle_timeout", run.call_args[1])

    def test_clone_limits_reach_transport_and_timeout_output_is_retained_redacted(self):
        conn = types.SimpleNamespace(host="fixture", port=3306, user="admin", password="clone-secret")
        plan = {"via": "mysqlsh", "timeout_s": cluster_shell.CLONE_TIMEOUT,
                "idle_timeout_s": cluster_shell.CLONE_IDLE_TIMEOUT}
        for idle in (False, True):
            with self.subTest(idle=idle):
                state = {"plan": plan, "shell": SHELL}

                def timeout(argv, seconds, **kwargs):
                    self.assertEqual(seconds, cluster_shell.CLONE_TIMEOUT)
                    self.assertEqual(kwargs["idle_timeout"], cluster_shell.CLONE_IDLE_TIMEOUT)
                    self.assertTrue(kwargs["kill_tree"])
                    kwargs["on_spawn"]()
                    error = subprocess.TimeoutExpired(argv, kwargs["idle_timeout"] if idle else seconds,
                                                      output=b"Cloning clone-secret\n",
                                                      stderr=b"Restart clone-secret\n")
                    if idle:
                        error.idle_timeout = kwargs["idle_timeout"]
                    raise error

                with mock.patch("myflames.connector.checked_shell_program", return_value=SHELL["path"]), \
                        mock.patch("myflames.connector._run_bounded", side_effect=timeout) as run:
                    with self.assertRaises(ConnectorError) as caught:
                        # This adapter receives the old generic action budget;
                        # only the fresh clone plan may select longer limits.
                        cluster._Sender(conn, state).run_write("fake clone;", timeout=cluster.SHELL_TIMEOUT)
                self.assertIn("no output for 3600s" if idle else "timed out after 86400s", str(caught.exception))
                self.assertIn("outcome is unknown", str(caught.exception))
                self.assertIn("Cloning", state["shell_output"])
                self.assertIn("Restart", state["shell_stderr"])
                self.assertNotIn("clone-secret", json.dumps(state))
                run.assert_called_once()

    def test_progress_preserves_chunks_until_password_redacted_and_keeps_zero_exit_stderr(self):
        conn = types.SimpleNamespace(host="fixture", port=3306, user="admin", password="secret-split")
        progress = reviewed_writes.Progress(1)
        stdout = b"Cloning secret-split\nfinished\n"
        stderr = b"WARNING: partial cleanup secret-split\n"

        def run(argv, timeout, **kwargs):
            emit = kwargs["on_output"]
            emit("stdout", b"Cloning secret-")
            self.assertEqual(progress.snapshot()["output"], [])
            emit("stdout", b"split\nfinished\n")
            emit("stderr", stderr)
            return subprocess.CompletedProcess(argv, 0, stdout, stderr)

        with mock.patch("myflames.connector.checked_shell_program", return_value=SHELL["path"]), \
                mock.patch("myflames.connector._run_bounded", side_effect=run):
            writer = cluster_shell.ShellWriter(conn, SHELL, progress=progress)
            output = writer.run_write("myflamesClusterSet.createReplicaCluster();", timeout=3)
        serialized = json.dumps(progress.snapshot())
        self.assertNotIn("secret-split", serialized + output + writer.stderr)
        self.assertIn("Cloning", serialized)
        self.assertIn("WARNING", writer.stderr)
        self.assertEqual(len(progress.snapshot()["output"]), 3)

    def test_progress_is_bounded(self):
        progress = reviewed_writes.Progress(1)
        for index in range(150):
            progress.emit(str(index) + "x" * 4000)
        output = progress.snapshot()["output"]
        self.assertEqual(len(output), 100)
        self.assertTrue(all(len(item["text"]) <= 2048 for item in output))


if __name__ == "__main__":
    unittest.main()
