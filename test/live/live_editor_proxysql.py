"""Live suite: the Query editor through the lab's ProxySQL (127.0.0.1:36033).

Ported from the ad-hoc live_editor_proxysql.py check. Covers: runs work
through the multiplexing ProxySQL frontend, and Stop during a SLEEP refuses
with "could not be confirmed" (the helper cannot prove it is killing this
tab's backend connection) and never reports a KILL that did not stop the
statement: the sleep runs to completion.

Run: scripts/live-tests.sh proxysql   (or MYFLAMES_LIVE_LABS=1 with the labs up)
"""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lab import Lab, check_lab, lab_password, make_home, redact, require_live  # noqa: E402


def setUpModule():
    require_live()


class EditorProxySQLTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        check_lab("ic", "proxysql")
        cls.home = make_home("proxysql")
        cls.lab = Lab()
        cls.profile = cls.lab.store.save(
            "profile", {"name": "proxysql-ic", "host": "127.0.0.1", "port": 36033,
                        "user": "mf_admin", "type": "development"},
            None, password=lab_password(), remember=True)
        cls.conn = {"profile_id": cls.profile["id"], "host": "127.0.0.1", "port": 36033,
                    "user": "mf_admin", "password": "", "type": "development"}

    @classmethod
    def tearDownClass(cls):
        try:
            cls.lab.api("/api/editor/close", {"tab_id": "proxy-tab", "connection": dict(cls.conn)})
        except Exception:
            pass

    def call(self, path, **payload):
        payload.setdefault("tab_id", "proxy-tab")
        payload.setdefault("connection", dict(self.conn))
        payload.setdefault("wait", 15)
        return self.lab.api(path, payload)

    def rows_of(self, result):
        rows = []
        sources = [result]
        if isinstance(result.get("run"), dict):
            sources.append(result["run"])
        for source in sources:
            for key in ("results", "result_sets", "sets"):
                for res in source.get(key) or ():
                    if isinstance(res, dict):
                        rows += res.get("rows") or res.get("data") or []
        return rows

    def test_01_runs_work(self):
        for _ in range(3):
            result = self.call("/api/editor/run",
                               sql="SELECT @@hostname AS host, @@port AS port, CONNECTION_ID() AS id")
            self.assertEqual(result.get("status"), "complete",
                             "run failed: %s" % redact(result.get("message") or result.get("error") or ""))
            self.assertTrue(self.rows_of(result), "run returned no rows: %s" % sorted(result)[:20])

    def test_02_stop_during_a_sleep_refuses_without_a_phantom_kill(self):
        outcome = {}

        def sleeper():
            started = time.monotonic()
            try:
                out = self.call("/api/editor/run", sql="SELECT SLEEP(6) AS slept", wait=20)
                out.pop("password", None)
                outcome.update(result=out, elapsed=time.monotonic() - started)
            except Exception as exc:  # noqa: BLE001 - asserted below, redacted
                outcome.update(error=redact(str(exc)), elapsed=time.monotonic() - started)

        thread = threading.Thread(target=sleeper)
        thread.start()
        time.sleep(2)
        stop = self.call("/api/editor/stop")
        thread.join(30)
        self.assertFalse(thread.is_alive(), "the sleep run never finished")
        # The refusal: ProxySQL multiplexing means the helper cannot confirm it
        # kills this tab's backend connection, so no KILL is sent.
        self.assertFalse(stop.get("stopped"), "Stop claims a KILL it cannot have confirmed: %s"
                         % redact(stop.get("message") or ""))
        self.assertIn("could not be confirmed", (stop.get("message") or "").lower(),
                      "unexpected Stop answer: %s" % redact(stop.get("message") or ""))
        # And the statement really was not stopped: the sleep ran its 6 seconds.
        self.assertNotIn("error", outcome, "the sleep run errored: %s" % outcome.get("error"))
        self.assertEqual(outcome["result"].get("status"), "complete",
                         "the sleep did not complete: %s"
                         % redact(outcome["result"].get("message") or ""))
        self.assertGreaterEqual(outcome["elapsed"], 5.0,
                                "the sleep ended after %.1fs: a KILL did stop the statement"
                                % outcome["elapsed"])


if __name__ == "__main__":
    unittest.main()
