"""Final ClusterSet preflight, shutdown and mysql-batch name regressions.

The external transport is synthetic. Review tokens, write classification,
audit completion, UUID binding and mysql batch decoding use production code.
"""
import copy
from contextlib import contextmanager
import re
import types
import unittest
from unittest import mock

import _cluster_fixtures as F
import test_clusterset_execution as fixtures
from myflames import cluster, cluster_shell, clusterset_execution as execution
from myflames import clusterset_verify as verify, connector, dbops
from myflames.connector import ConnectorError
from myflames.lifecycle import ToolGroups


class StandalonePreflightBudgetTests(fixtures.Home):
    def read_candidate(self, seconds_per_login):
        report = fixtures.topology()
        target = fixtures.CandidateConnection()
        profile = {"id": "spare", "name": "Spare", "host": "spare", "port": 3306,
                   "group": "G", "remember_password": True,
                   "topology_identity": {"server_uuid": fixtures.SPARE_UUID}}
        source_uuid = report["server"]["server_uuid"]
        source = types.SimpleNamespace(run=lambda sql, **kwargs: F.batch([[source_uuid, ""]]))
        clock = [100.0]
        original_run = target.run

        def remote_login(sql, **kwargs):
            output = original_run(sql, **kwargs)
            clock[0] += seconds_per_login
            return output

        @contextmanager
        def opener(saved):
            self.assertEqual(saved["id"], profile["id"])
            yield target

        checks = {"shell": fixtures.SHELL, "group_name": "G", "group_profiles": [profile],
                  "read_options": {"profiles": [profile], "open_profile": opener}}
        shell_output = cluster_shell._status_output('MYFLAMES_CONFIG={"status":"ok"}\n', None)
        with mock.patch.object(target, "run", side_effect=remote_login), \
                mock.patch.object(cluster.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(cluster_shell.ShellWriter, "run_write", return_value=shell_output) as shell:
            try:
                result = execution.read_candidate(source, report, "create_replica_cluster:spare", checks)
            except ConnectorError:
                self.assertLessEqual(len(target.calls), 2)
                shell.assert_not_called()
                raise
        return result, target, shell

    def test_several_healthy_remote_logins_can_exceed_topology_route_budget(self):
        candidate, target, shell = self.read_candidate(2.0)
        self.assertEqual(candidate["server_uuid"], fixtures.SPARE_UUID)
        self.assertTrue(candidate["gtid_subset"])
        self.assertEqual(len(target.calls), 4)
        self.assertTrue(all(0 < timeout <= 120 for timeout in target.timeouts), target.timeouts)
        for sql, flags in target.calls:
            self.assertIn("SELECT @@GLOBAL.server_uuid; ", sql)
            self.assertIn("--skip-reconnect", flags)
        shell.assert_called_once()

    def test_exhausted_spare_budget_stops_before_configuration_shell(self):
        with self.assertRaisesRegex(ConnectorError, "timed out|budget"):
            self.read_candidate(200.0)


class PostActionShutdownTests(fixtures.EndpointHarness):
    def test_shutdown_skips_sql_verification_and_records_unknown_with_recovery_checks(self):
        for transport in ("zero_exit", "shutdown_error", "shutdown_stage_only"):
            with self.subTest(transport=transport):
                self.report = copy.deepcopy(self.baseline)
                self.error = None
                self.error_info = None
                groups = ToolGroups()
                self.conn._tool_groups = groups if transport != "shutdown_stage_only" else None
                plan = self.plan()
                self.mutate_after = groups.close_spawning
                if transport != "zero_exit":
                    self.error = execution.SHELL_SHUTDOWN_MESSAGE
                    self.error_info = {"source": "workspace", "code": None, "line": None, "stage": "shutdown"}
                with mock.patch.object(verify, "verify", side_effect=AssertionError("post-shutdown SQL")) as sql:
                    result = self.apply(plan)
                sql.assert_not_called()
                self.assertEqual(result["status"], "unknown", result)
                self.assertEqual(result["outcome_class"], "unknown or partial")
                self.assertEqual(result["states"], ["unknown"] * len(plan["statements"]))
                self.assertIn("dba.getClusterSet().status()", result["message"])
                self.assertIn("direct SQL", result["message"])
                records = dbops.audit_report(self.store)["records"]
                record = next(r for r in records if r["id"] == result["audit_id"])
                self.assertEqual(record["status"], "unknown")
                self.assertEqual(record["outcome"], "unknown or partial")
                self.assertTrue(result["verification"]["missing"])

    def test_shutdown_during_verification_stops_the_next_client_login(self):
        groups = ToolGroups()
        self.conn._tool_groups = groups
        plan = self.plan()
        logins = []

        def login(sql, **kwargs):
            logins.append(sql)
            groups.close_spawning()
            return F.batch([[self.report["server"]["server_uuid"]]])

        def observe(source, *args):
            source.run(cluster.CLUSTER_TAG + "SELECT @@server_uuid")
            source.run(cluster.CLUSTER_TAG + "SELECT @@server_uuid")
            raise AssertionError("verification continued after shutdown")

        with mock.patch.object(self.conn, "run", side_effect=login), \
                mock.patch.object(verify, "verify", side_effect=observe):
            result = self.apply(plan)
        self.assertEqual(len(logins), 1, logins)
        self.assertEqual(result["status"], "unknown", result)
        self.assertEqual(result["outcome_class"], "unknown or partial")
        self.assertIn("dba.getClusterSet().status()", result["message"])


class VerificationTransportTests(unittest.TestCase):
    def test_real_client_adapter_forwards_shutdown_registry_and_restores_methods(self):
        # Exercise the actual MySQLConnection login adapter, replacing only
        # its external process transport. No binary lookup or login occurs.
        conn = connector.MySQLConnection(host="synthetic-local")
        groups = ToolGroups()
        conn._tool_groups = groups
        consent_checks = []
        conn._login_guard = lambda: consent_checks.append("checked")
        original_run, original_client = conn.run, conn._run_client
        state = {}
        with mock.patch.object(connector, "_run_bounded", return_value="client reply") as transport:
            with execution.verification_reads(conn, {}, state) as (source, options):
                self.assertIs(source, conn)
                self.assertEqual(options, {})
                self.assertEqual(source._run_client(["synthetic-mysql"], timeout=7), "client reply")
            self.assertEqual(transport.call_args[0], (["synthetic-mysql"],))
            self.assertIs(transport.call_args[1]["tool_groups"], groups)
            self.assertEqual(transport.call_args[1]["timeout"], 7)
        self.assertEqual(consent_checks, ["checked"])
        self.assertEqual(conn.run, original_run)
        self.assertEqual(conn._run_client, original_client)
        self.assertNotIn("run", conn.__dict__)
        self.assertNotIn("_run_client", conn.__dict__)

    def test_remote_opener_keeps_consent_scope_and_blocks_new_logins_after_shutdown(self):
        local = connector.MySQLConnection(host="synthetic-local")
        peer = connector.MySQLConnection(host="synthetic-peer")
        groups = ToolGroups()
        local._tool_groups = groups
        profile = {"id": "peer", "group": "G"}
        opens, logins = [], []

        @contextmanager
        def opener(saved):
            self.assertIs(saved, profile)
            opens.append(saved["id"])
            yield peer

        opener.group_scope = object()
        opener.check = lambda: None
        opener.consent = lambda: None
        opener.current_profile = {"id": "local", "group": "G"}
        opener.prepare = mock.Mock(side_effect=AssertionError("unguarded prepare shortcut"))
        state = {}
        original_run, original_client = peer.run, peer._run_client

        def transport(argv, **kwargs):
            logins.append(argv)
            self.assertIs(kwargs["tool_groups"], groups)
            return "peer reply"

        with mock.patch.object(connector, "_run_bounded", side_effect=transport), \
                self.assertRaisesRegex(ConnectorError, "workspace closed|workspace is closing"):
            with execution.verification_reads(local, {"profiles": [profile], "open_profile": opener}, state) as (_, options):
                wrapped = options["open_profile"]
                for key in ("group_scope", "check", "consent", "current_profile"):
                    self.assertIs(getattr(wrapped, key), getattr(opener, key))
                self.assertFalse(hasattr(wrapped, "prepare"))
                with wrapped(profile) as source:
                    self.assertEqual(source._run_client(["first-login"], timeout=4), "peer reply")
                    groups.close_spawning()
                    with self.assertRaises(ConnectorError):
                        source.run("SELECT @@server_uuid")
                    with self.assertRaises(ConnectorError):
                        source._run_client(["second-login"], timeout=4)
                with self.assertRaises(ConnectorError):
                    with wrapped(profile):
                        self.fail("opened a peer after shutdown")
        self.assertEqual(opens, ["peer"])
        self.assertEqual(logins, [["first-login"]])
        opener.prepare.assert_not_called()
        self.assertEqual(peer.run, original_run)
        self.assertEqual(peer._run_client, original_client)
        self.assertTrue(state["workspace_shutdown"])
        self.assertTrue(state["verification"]["missing"])


class MetadataBatchConnection(object):
    """Return actual mysql batch strings, including its ambiguous NULL text."""
    def __init__(self, name, domain, group, uuid, keep=False, stale=False):
        self.name, self.domain, self.group, self.uuid = name, domain, group, uuid
        self.keep, self.stale = keep, stale
        self.calls = []

    def run(self, sql, **kwargs):
        self.calls.append(sql)
        if "v2_gr_clusters" in sql:
            name = self.name
            if re.search(r"CONCAT\('=', (?:\w+\.)?cluster_name\)", sql) and name is not None:
                name = "=" + name
            return F.batch([["cid-kept", name, "pm", self.group]])
        if "v2_cs_members" in sql:
            if not self.stale:
                return ""
            name, domain = self.name, self.domain
            if re.search(r"CONCAT\('=', (?:\w+\.)?cluster_name\)", sql) and name is not None:
                name = "=" + name
            if re.search(r"CONCAT\('=', (?:\w+\.)?domain_name\)", sql) and domain is not None:
                domain = "=" + domain
            return F.batch([["set-id", "cid-kept", name, None, "1", domain, "3", "{}"]])
        if "schema_version" in sql:
            return F.batch([[2, 1, 0]])
        if "information_schema.SCHEMATA" in sql:
            return F.batch([[cluster.METADATA_SCHEMA]])
        if "@@performance_schema" in sql:
            return F.batch([["ON"]])
        if "FROM performance_schema.replication_group_members" in sql:
            return F.batch([[self.uuid, "ONLINE", "PRIMARY"]]) if self.keep else ""
        if "@@global.super_read_only" in sql:
            return F.batch([["OFF" if self.keep else "ON"]])
        if "FROM performance_schema.persisted_variables" in sql:
            return F.batch([["group_replication_start_on_boot", "OFF"]])
        if "SELECT CHANNEL_NAME FROM performance_schema." in sql:
            return ""
        raise AssertionError("Unexpected SQL: " + sql)


class LiteralNullCleanupTests(unittest.TestCase):
    def cleanup(self, cluster_name, set_name, keep=False, stale=False):
        before = fixtures.topology()
        item = copy.deepcopy(before["clusterset"]["clusters"][0])
        group = before._clusterset_groups[item["name"]]
        item["name"] = cluster_name
        before["clusterset"]["name"] = set_name
        before._clusterset_groups[cluster_name] = group
        uuid = item["primary"]["server_uuid"]
        conn = MetadataBatchConnection(cluster_name, set_name, group, uuid, keep, stale)
        evidence = verify._Evidence()
        reader = cluster.Reader(conn, cluster.CLUSTER_TAG)
        verify._inspect_cleanup(reader, evidence, "removed member", uuid, item, before, keep, False)
        return evidence, conn

    def test_kept_standalone_cluster_literally_named_null_keeps_group_identity(self):
        evidence, _ = self.cleanup("NULL", "oldset", keep=True)
        fact = next(f for f in evidence.facts if f["fact"] == "standalone cluster group identity")
        self.assertEqual(fact["status"], "verified", fact)
        self.assertEqual(evidence.missing, [])
        self.assertEqual(evidence.mismatches, [])

    def test_removed_cluster_or_set_named_null_cannot_hide_stale_membership(self):
        for name, domain in (("NULL", "oldset"), ("kept", "NULL"), ("NULL", "NULL")):
            for keep in (False, True):
                with self.subTest(name=name, domain=domain, keep=keep):
                    evidence, _ = self.cleanup(name, domain, keep=keep, stale=True)
                    fact = next(f for f in evidence.facts if "ClusterSet membership absent" in f["fact"])
                    self.assertEqual(fact["status"], "mismatch", fact)
                    self.assertEqual(fact["actual"], [name])

    def test_standalone_metadata_reader_preserves_null_name_without_clusterset_snapshot(self):
        for name in ("NULL", "=NULL", "=", None):
            with self.subTest(name=name):
                conn = MetadataBatchConnection(name, "oldset", "reviewed-group", fixtures.SPARE_UUID)
                metadata = cluster._metadata_base()
                rows = cluster._read_metadata_tables(cluster.Reader(conn, cluster.CLUSTER_TAG), metadata, [])
                self.assertEqual(rows[0][1], name)
                self.assertEqual(metadata["schema_version"], "2.1.0")

    def test_cleanup_keeps_sql_null_distinct_from_literal_null_name(self):
        before = fixtures.topology()
        before["clusterset"]["name"] = "NULL"
        item = copy.deepcopy(before["clusterset"]["clusters"][0])
        item["name"] = "NULL"
        uuid = item["primary"]["server_uuid"]
        conn = MetadataBatchConnection(None, "NULL", "old-group", uuid, stale=True)
        evidence = verify._Evidence()
        verify._inspect_cleanup(cluster.Reader(conn, cluster.CLUSTER_TAG), evidence, "removed member",
                                uuid, item, before, False, False)
        fact = next(f for f in evidence.facts if "ClusterSet membership absent" in f["fact"])
        self.assertEqual((fact["status"], fact["actual"]), ("verified", []))


if __name__ == "__main__":
    unittest.main()
