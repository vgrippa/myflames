"""Real client-child shutdown checks, without a database or MySQL Shell.

Only the executable is a Python stand-in. Process groups, pipe handling,
workspace lifecycle registration and the durable audit trail stay real.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from myflames import cluster_shell, clusterset_execution, connector, dbops, lifecycle
from myflames.workspace_store import WorkspaceStore
import test_clusterset_execution as execution_fixtures


@unittest.skipUnless(os.name == "posix", "desktop process-group supervision is POSIX")
class ShellShutdownSupervisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.environment = mock.patch.dict(os.environ, HOME=self.temp.name,
                                           USERPROFILE=self.temp.name,
                                           MYFLAMES_SECRET_BACKEND="vault")
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.registry = os.path.join(self.temp.name, "tool-groups.json")
        self.manager = lifecycle.LifecycleJobs(self.registry)
        self.addCleanup(self.manager.close)
        self.conn = types.SimpleNamespace(host="fixture", port=3306, user="admin",
                                          password="supervision-secret")
        self.shell = {"path": sys.executable, "realpath": sys.executable,
                      "version_tuple": [9, 7, 1]}

    def registry_groups(self):
        with open(self.registry) as handle:
            record = json.load(handle)
        self.assertEqual(record["schema_version"], "tool-groups-1.0")
        return record["groups"]

    def wait_for_file(self, path, worker):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            try:
                with open(path) as handle:
                    return json.load(handle)
            except (OSError, ValueError):
                if not worker.is_alive():
                    break
                time.sleep(0.01)
        self.fail("The stand-in client did not reach its running state.")

    def test_quiet_clone_child_is_listed_stopped_and_audited_unknown_on_shutdown(self):
        seen = os.path.join(self.temp.name, "client-started.json")
        child_code = "import time; time.sleep(30)"
        code = (
            "import json, os, subprocess, sys, time\n"
            "script = sys.stdin.read()\n"
            "child = subprocess.Popen([sys.executable, '-c', %r])\n"
            "with open(%r, 'w') as handle:\n"
            "    json.dump({'pid': os.getpid(), 'child': child.pid, 'script': script}, handle)\n"
            "time.sleep(30)\n" % (child_code, seen))
        store = WorkspaceStore()
        audit = dbops.AuditTrail(store, "cluster_action", "ui", connection={"host": "fixture"})
        script = 'myflamesClusterSet.createReplicaCluster("spare:3306", "replica", {recoveryMethod:"clone"});'
        audit_id = audit.attempt([script], target={"server_uuid": "reviewed-spare"})
        result = {}

        def request():
            try:
                with clusterset_execution.supervise_shell(self.conn, self.manager, audit):
                    writer = cluster_shell.ShellWriter(self.conn, self.shell)
                    result["writer"] = writer
                    # Exercise an already running clone transport, even when
                    # new clone reviews are disabled for target safety.
                    writer.run_write(script, timeout=8, idle_timeout=8)
            except BaseException as exc:
                result["error"] = exc

        worker = threading.Thread(target=request, name="test-shell-request", daemon=True)
        with mock.patch.object(connector, "checked_shell_program", return_value=sys.executable), \
                mock.patch.object(cluster_shell.ShellWriter, "argv", return_value=[sys.executable, "-c", code]):
            worker.start()
            try:
                running = self.wait_for_file(seen, worker)
                self.assertEqual(running["script"], script)
                self.assertEqual(self.registry_groups(), [running["pid"]])
                self.assertEqual(os.getpgid(running["child"]), running["pid"])
                with self.manager.lock:
                    runners = list(self.manager.runners)
                self.assertEqual(len(runners), 1)
                self.assertIs(runners[0].thread, worker)
                self.assertIsNone(result.get("error"))
                self.assertEqual(dbops.audit_report(store)["records"][0]["status"], "pending")
                remaining = self.manager.shutdown(term_wait=0.2, kill_wait=0.5, join_wait=2)
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(remaining, [])
                self.assertFalse(lifecycle._group_alive(running["pid"]))
                self.assertEqual(self.registry_groups(), [])
            finally:
                self.manager.groups.close_spawning()
                self.manager.groups.terminate(term_wait=0.1, kill_wait=0.5)
                worker.join(3)

        self.assertIsInstance(result.get("error"), connector.ConnectorError, result)
        self.assertIn("outcome is unknown", str(result["error"]))
        self.assertEqual(result["writer"].stderr.error_info["stage"], "shutdown")
        records = dbops.audit_report(store)["records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["id"], audit_id)
        self.assertEqual(records[0]["status"], "unknown")
        self.assertEqual(records[0]["outcome"], "unknown or partial")
        self.assertEqual(records[0]["after"]["observation"], "shutdown")
        for check in ("clone status", "server restart", "ClusterSet membership", "replication"):
            self.assertIn(check, records[0]["message"])
        self.assertNotIn(self.conn.password, json.dumps(records))
        # Independent SQL verification must not overwrite the already durable
        # interrupted-observation outcome or make a partial effect look final.
        deferred = clusterset_execution.DeferredAudit(audit)
        deferred.complete("ok", outcome="applied")
        out = clusterset_execution.finish(
            {"status": "partial", "states": ["failed"], "effect": [["member", "old", "new"]]},
            {"action": "create_replica_cluster:spare", "statements": [script]},
            {"shell_error": str(result["error"]), "shell_stderr": result["writer"].stderr,
             "verification": {"facts": [], "missing": [], "mismatches": []}}, audit=deferred)
        self.assertEqual(out["status"], "unknown")
        self.assertEqual(out["states"], ["unknown"])
        self.assertEqual(dbops.audit_report(store)["records"][0]["after"]["observation"], "shutdown")

    def test_closed_registry_refuses_before_popen(self):
        self.manager.groups.close_spawning()
        self.conn._tool_groups = self.manager.groups
        with mock.patch.object(connector, "checked_shell_program", return_value=sys.executable), \
                mock.patch.object(connector.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(ValueError, "Nothing was sent"):
                cluster_shell.ShellWriter(self.conn, self.shell).run_write("write();", timeout=2)
        spawn.assert_not_called()
        self.assertEqual(self.registry_groups(), [])

    def test_failed_registry_publication_sends_no_input_and_reaps_child(self):
        received = os.path.join(self.temp.name, "received-script")
        code = ("import sys\n"
                "data = sys.stdin.buffer.read()\n"
                "if data:\n"
                "    open(%r, 'wb').write(data)\n" % received)
        children = []
        real_popen = subprocess.Popen

        def spawn(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            children.append(process)
            return process

        try:
            with mock.patch.object(self.manager.groups, "_write", side_effect=OSError("registry unavailable")), \
                    mock.patch.object(connector.subprocess, "Popen", side_effect=spawn):
                with self.assertRaisesRegex(connector.ConnectorError, "Could not record.*nothing was sent"):
                    connector._run_bounded([sys.executable, "-c", code], 2, input=b"reviewed write",
                                           tool_groups=self.manager.groups)
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())
            self.assertFalse(lifecycle._group_alive(children[0].pid))
            self.assertEqual(self.manager.groups.snapshot(), [])
            self.assertFalse(os.path.exists(received))
        finally:
            for process in children:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait(timeout=2)

    def test_success_is_registered_before_stdin_and_unlisted_after_reaping(self):
        # The child reads the actual on-disk supervisor list after receiving
        # stdin. Its own pid must already be present in that list.
        code = ("import json, os, sys\n"
                "data = sys.stdin.buffer.read()\n"
                "with open(%r) as handle:\n"
                "    groups = json.load(handle)['groups']\n"
                "assert os.getpid() in groups, groups\n"
                "sys.stdout.buffer.write(data)\n" % self.registry)
        proc = connector._run_bounded([sys.executable, "-c", code], 3, input=b"reviewed script",
                                      tool_groups=self.manager.groups)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, b"reviewed script")
        self.assertEqual(self.registry_groups(), [])


class ShellActionEndpointSupervisionTests(execution_fixtures.EndpointHarness):
    def test_reviewed_workspace_action_attaches_its_lifecycle_supervisor(self):
        # Exercise the HTTP dispatcher with real review tokens and policy.
        # The process tests above exercise this same supervisor downstream.
        manager = lifecycle.LifecycleJobs()
        self.addCleanup(manager.close)
        self.workspace.lifecycle = manager
        plan = self.plan()

        def send(writer, script, **kwargs):
            self.assertIs(writer._conn._tool_groups, manager.groups)
            self.assertTrue(callable(writer._conn._shell_shutdown_audit))
            with manager.lock:
                runners = list(manager.runners)
            self.assertEqual(len(runners), 1)
            self.assertIs(runners[0].thread, threading.current_thread())
            self.assertEqual(script, "\n".join(plan["session_guard"] + plan["statements"]))
            self.store_primary_option()
            return ""

        with mock.patch.object(cluster_shell.ShellWriter, "run_write", autospec=True, side_effect=send) as sender:
            result = self.apply(plan)
        sender.assert_called_once()
        self.assertEqual(result["outcome_class"], "verified", result)
        self.assertEqual(manager.runners, set())
        self.assertIsNone(self.conn._tool_groups)
        self.assertIsNone(self.conn._shell_shutdown_audit)


if __name__ == "__main__":
    unittest.main()
