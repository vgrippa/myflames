"""Real POSIX child-process lifecycle tests for the Query editor engine.

The shared fake speaks the client protocol; its gate/nokill hooks model a
running statement that either acknowledges or ignores KILL QUERY. No database
server, user workspace, or changes to the shared fake are needed.
"""
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

from myflames import editor_session as ed
from myflames.workspace_store import WorkspaceStore


FAKE = Path(__file__).resolve().parent / "_fake_mysql_client.py"
WAIT = 5.0


@unittest.skipIf(os.name == "nt", "the fake client is a POSIX script")
class EditorProcessTests(unittest.TestCase):
    engine = "mysql"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        bindir = root / "bin"
        bindir.mkdir()
        self.state = root / "state"
        self.state.mkdir()
        # Match the identity-bearing fake installed by test_editor_session.py.
        client = FAKE.read_text(encoding="utf-8").replace(
            '"connection_id": str(self.conn_id),',
            '"connection_id": str(self.conn_id), "hostname": "fake-db", "port": "3306", "server_id": "1",')
        script = bindir / "mysql"
        script.write_text("#!%s\n%s" % (sys.executable, client), encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        patcher = mock.patch.dict(os.environ, {
            "HOME": self.temp.name, "USERPROFILE": self.temp.name,
            "MYFLAMES_SECRET_BACKEND": "vault",
            "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("MYFLAMES_VAULT_PASSPHRASE", None)
        self.children = []
        self.sessions = []
        # Inject only these controls at the process boundary. Production keeps
        # its private HOME and its environment allowlist unchanged.
        popen = ed.subprocess.Popen

        def spawn(*args, **kwargs):
            env = dict(kwargs.get("env") or os.environ)
            for key in list(env):
                if key.startswith("FAKE_"):
                    del env[key]
            env.update(FAKE_STATE=str(self.state), FAKE_ENGINE=self.engine)
            proc = popen(*args, **dict(kwargs, env=env))
            self.children.append(proc)
            return proc

        process_patch = mock.patch.object(ed.subprocess, "Popen", side_effect=spawn)
        process_patch.start()
        self.addCleanup(process_patch.stop)
        feature_patch = mock.patch.dict(ed._FEATURES, {}, clear=True)
        feature_patch.start()
        self.addCleanup(feature_patch.stop)
        grace_patch = mock.patch.object(ed, "STOP_GRACE", 0.05)
        grace_patch.start()
        self.addCleanup(grace_patch.stop)
        self.registry = ed.EditorSessions(reap_every=0)
        self.workspace = types.SimpleNamespace(store=WorkspaceStore(), editor=self.registry)
        self.options = {"host": "127.0.0.1", "port": 3306, "user": "app"}
        # The connection the API resolves for these sessions: requests that
        # name another connection must not reach them.
        self.connection = dict(self.options, type="development")
        self.identity = ed._connection_facts({"connection": self.connection}, self.workspace.store)[1]
        # Registered last so even a failed assertion cannot leave a child or a
        # reader running after its files/environment have been removed.
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        try:
            self.registry.close()
            for session in self.sessions:
                if session.run is not None:
                    self.assertTrue(session.run.done.wait(WAIT), "run did not finish during cleanup")
                for reader in session._readers:
                    reader.join(WAIT)
                    self.assertFalse(reader.is_alive(), "client pipe reader did not exit")
            for proc in self.children:
                self.assert_reaped(proc)
        finally:
            # Emergency cleanup follows assertions, never masks a reaping bug.
            for proc in self.children:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=WAIT)

    def until(self, predicate, message, timeout=WAIT):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        self.fail(message)

    def assert_reaped(self, proc):
        # poll()/wait() themselves reap zombies: inspect cached returncode and
        # waitpid first so a broken close cannot pass by virtue of this helper.
        self.assertIsNotNone(proc.returncode, "client was not waited for")
        with self.assertRaises(ChildProcessError):
            os.waitpid(proc.pid, os.WNOHANG)
        self.assertIsNotNone(proc.poll())
        self.assertNotIn(proc, ed._REGISTRY_PROCS)

    def open_session(self, tab="tab"):
        def make():
            session = ed.EditorSession(tab, self.identity, self.options, connection_type="development")
            self.sessions.append(session)
            session.start(self.options)
            return session
        return self.registry.open(tab, make, identity=self.identity)

    def run_sql(self, session, sql, timeout=None):
        units = ed.plan_statements(sql, engine=session.engine)
        ed.classify_units(units, True, sql)
        run = ed.Run(units, 1000, dict(self.options), sql, ed.Offsets(sql), "all", timeout=timeout)
        return session.start_run(run)

    def running_at_gate(self, session, sql):
        path = self.state / ("session-%d.log" % session.connection_id)
        self.until(lambda: path.exists() and sql in path.read_text(encoding="utf-8"),
                   "fake client never reached the statement gate")
        self.assertFalse(session.run.done.is_set())
        self.assertIsNone(session.proc.returncode)

    def assert_control_kill(self, session, since):
        expected = ed.TAG + "KILL QUERY %d" % session.connection_id
        kills = (self.state / "kills.log").read_text(encoding="utf-8").splitlines()
        self.assertIn(expected, kills)
        controls = [p for p in self.children[since:] if "-e" in p.args]
        self.assertTrue(controls, "KILL QUERY did not use a second client process")
        for control in controls:
            self.assertNotEqual(control.pid, session.proc.pid)
            self.assertEqual(control.args[control.args.index("-e") + 1], expected)
            self.assert_reaped(control)

    def close_api(self, tab="tab", **payload):
        return ed.api_response("/api/editor/close", dict({"connection": self.connection}, **dict(payload, tab_id=tab)),
                               self.workspace)

    def test_normal_close_terminates_and_reaps(self):
        session = self.open_session()
        self.assertFalse(session.in_transaction)
        self.assertTrue(self.close_api()["closed"])
        self.assertEqual(session.ended_reason, "closed")
        self.assertIsNone(self.registry.get("tab"))
        self.assert_reaped(session.proc)

    def test_discard_open_transaction_terminates_and_reaps(self):
        session = self.open_session()
        run = self.run_sql(session, "START TRANSACTION")
        self.assertTrue(run.done.wait(WAIT))
        self.assertEqual(run.state, "complete")
        self.assertTrue(session.in_transaction)
        refused = self.close_api()
        self.assertFalse(refused["closed"])
        self.assertEqual(refused["reason"], "transaction_open")
        self.assertIsNone(session.proc.returncode)
        self.assertTrue(self.close_api(discard=True)["closed"])
        self.assert_reaped(session.proc)

    def test_close_already_dead_client_reaps_and_is_idempotent(self):
        session = self.open_session()
        session.proc.kill()  # Deliberately do not wait: the engine owns reaping.
        self.until(lambda: session.ended, "client EOF did not end the session")
        self.assertEqual(session.ended_reason, "client_exited")
        self.assertTrue(self.close_api()["closed"])
        self.assert_reaped(session.proc)
        self.assertTrue(self.close_api()["closed"])

    def test_stop_uses_second_connection_and_keeps_session(self):
        session = self.open_session()
        sql = "SELECT 7 /*gate*/"
        run = self.run_sql(session, sql)
        self.running_at_gate(session, sql)
        since = len(self.children)
        self.assertTrue(session.stop(self.options)["stopped"])
        self.assertTrue(run.done.wait(WAIT))
        self.assert_control_kill(session, since)
        self.assertEqual(run.state, "stopped")
        self.assertEqual(run.units[0]["status"], "stopped")
        self.assertFalse(session.ended)
        self.assertIsNone(session.proc.poll())
        proc, connection_id = session.proc, session.connection_id
        followup = self.run_sql(session, "SELECT 9")
        self.assertTrue(followup.done.wait(WAIT))
        self.assertEqual(followup.state, "complete")
        rows = [r for r in session.snapshot()["results"] if r["type"] == "rows"]
        self.assertEqual(rows[0]["rows"], [["9"]])
        self.assertIs(session.proc, proc)
        self.assertEqual(session.connection_id, connection_id)

    def check_unresponsive(self, automatic):
        session = self.open_session()
        sql = "SELECT 7 /*gate:nokill*/"
        since = len(self.children)
        started = time.monotonic()
        run = self.run_sql(session, sql, timeout=0.1 if automatic else None)
        self.running_at_gate(session, sql)
        if not automatic:
            self.assertTrue(session.stop(self.options)["stopped"])
        self.assertTrue(run.done.wait(WAIT), "unresponsive statement never ended")
        # _wait_step checks grace once per second; include that scheduling
        # interval, while still detecting the production 15-second fallback.
        self.assertLess(time.monotonic() - started, 3.0)
        self.assert_control_kill(session, since)
        self.assertEqual(run.stopped_by, "timeout" if automatic else "user")
        self.assertEqual(run.state, "session_ended")
        self.assertEqual(run.units[0]["status"], "session_ended")
        self.assertEqual(session.ended_reason, "unresponsive")
        self.assertIn("did not end after KILL QUERY", session.end_message)
        self.assert_reaped(session.proc)

    def test_timeout_unresponsive_client_is_terminated_and_reaped(self):
        self.check_unresponsive(automatic=True)

    def test_stop_unresponsive_client_is_terminated_and_reaped(self):
        self.check_unresponsive(automatic=False)

    def test_default_workspace_cap_frees_slot_after_close(self):
        self.assertEqual(self.registry.max_sessions, 8)
        sessions = [self.open_session("tab%d" % i) for i in range(8)]
        before = len(self.children)
        with self.assertRaises(ed.EditorError) as caught:
            self.open_session("overflow")
        self.assertEqual(caught.exception.code, "too_many_sessions")
        self.assertEqual(str(caught.exception), "8 editor sessions are open. Close a tab's session first.")
        self.assertEqual(len(self.children), before, "refused open spawned a client")
        self.assertIsNone(self.registry.get("overflow"))
        self.assertTrue(self.close_api(tab="tab0")["closed"])
        self.assert_reaped(sessions[0].proc)
        replacement = self.open_session("overflow")
        self.assertFalse(replacement.ended)
        self.assertNotEqual(replacement.connection_id, sessions[0].connection_id)
        self.assertEqual(sum(not s.ended for s in self.registry.sessions.values()), 8)

    def test_workspace_shutdown_reaps_idle_and_running_clients(self):
        sessions = [self.open_session("tab%d" % i) for i in range(3)]
        sql = "SELECT 7 /*gate:nokill*/"
        run = self.run_sql(sessions[1], sql)
        self.running_at_gate(sessions[1], sql)
        self.registry.close()  # The server's close-all hook is named close().
        self.assertEqual(self.registry.sessions, {})
        self.assertTrue(run.done.is_set())
        self.assertEqual(run.state, "session_ended")
        for session in sessions:
            self.assertEqual(session.ended_reason, "server_exit")
            self.assert_reaped(session.proc)
        self.registry.close()
        with self.assertRaises(ed.EditorError) as caught:
            self.open_session("after-shutdown")
        self.assertEqual(caught.exception.code, "closed")


class MariaDBEditorProcessTests(EditorProcessTests):
    engine = "mariadb"


if __name__ == "__main__":
    unittest.main()
