"""Live suite: reviewed writes against real servers (mf-ic and mf-galera labs).

Reviewed writes now send each statement with its server identity check in one
query packet, grouping the session setup with the statements that depend on
it. A unit-test fake server hid a real parse error in these paths, so they
get a real-server check on both engines, through the app's own review then
apply endpoints (myflames.ui.api_response, as the other suites do):

- Users and roles: create a throwaway account (unique name) whose password
  contains a backslash and a single quote, change its password to another
  such value, drop it; each step must report applied, the final password
  must log in with the client while the first no longer does. The account
  is dropped also on failure.
- Variables: review and apply a harmless dynamic global (long_query_time),
  assert the server reports the new value, set it back the same way; the
  original value is restored also on failure.
- Query editor: in a throwaway schema, a confirmed write sequence (CREATE
  TABLE, INSERT, DROP TABLE, each DDL its own confirmed run) through the
  editor's run with confirmation;
  then Stop during SELECT SLEEP(8) on the same direct connection: the sleep
  ends early (interrupted) and Stop reports the kill sent. The schema is
  dropped also on failure.

Run: scripts/live-tests.sh writes   (or MYFLAMES_LIVE_LABS=1 with the labs up)
"""
import os
import sys
import threading
import time
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lab import Lab, check_lab, conn_for, make_home, redact, require_live  # noqa: E402


def setUpModule():
    require_live()


def _unique(prefix):
    """A throwaway name that cannot collide with an earlier or parallel run."""
    return "%s%s" % (prefix, uuid.uuid4().hex[:10])


def _secret_password(tag):
    """A password with a backslash and a single quote: the quoting the unit
    tests' fake server never checked."""
    return "mf\\'%s-%s" % (tag, uuid.uuid4().hex[:8])


def _applied(result, what):
    if result.get("status") != "applied":
        raise AssertionError("%s did not report applied: %s"
                             % (what, redact(result.get("message") or result.get("status"))))


class _WritesBase(object):
    """One engine's reviewed writes against its saved lab connection.

    Subclasses set CLUSTERS (the check_lab precondition), PROFILE (the saved
    connection name, or None to resolve the InnoDB Cluster primary from the
    Cluster read) and TAB (the editor tab id)."""

    CLUSTERS = ()
    PROFILE = None
    TAB = "writes"

    @classmethod
    def setUpClass(cls):
        check_lab(*cls.CLUSTERS)
        cls.home = make_home("writes")
        cls.lab = Lab()
        cls.profile = cls.lab.profile(cls.PROFILE) if cls.PROFILE else cls._primary_profile()
        cls.conn = conn_for(cls.profile)

    @classmethod
    def _primary_profile(cls):
        """The saved connection of the InnoDB Cluster's current PRIMARY."""
        report = cls.lab.api("/api/live/cluster",
                             {"connection": conn_for(cls.lab.profile("cluster-ic-1"))})
        for member in report.get("members") or ():
            if member.get("role") == "PRIMARY" and member.get("state") == "ONLINE":
                node = (member.get("name") or "").split(":")[0]  # mf-ic-1:3306 -> mf-ic-1
                return cls.lab.profile("cluster-" + node.replace("mf-", ""))
        raise AssertionError("no ONLINE PRIMARY in the InnoDB Cluster report")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.lab.api("/api/editor/close",
                        {"tab_id": cls.TAB, "connection": dict(cls.conn), "discard": True})
        except Exception:
            pass

    # -- Users and roles ----------------------------------------------------

    def _account_apply(self, operation, user, host, password=None):
        """Review then apply one account change, as the workspace sends it."""
        payload = {"connection": dict(self.conn), "operation": operation,
                   "user": user, "host": host}
        if password is not None:
            payload["password"] = password
        plan = self.lab.api("/api/accounts/plan", payload)
        self.assertTrue(plan.get("plan_id"), "no plan_id in the %s plan" % operation)
        apply_payload = {"connection": dict(self.conn), "plan_id": plan["plan_id"],
                         "expect": plan.get("expect"), "confirmed": True}
        if password is not None:
            apply_payload["password"] = password
        result = self.lab.api("/api/accounts/apply", apply_payload)
        if result.get("status") == "needs_typed_name":
            apply_payload["typed_name"] = user
            result = self.lab.api("/api/accounts/apply", apply_payload)
        return result

    def _drop_account(self, user, host):
        """Cleanup: the throwaway account goes away also when a test failed."""
        try:
            self._account_apply("drop", user, host)
        except Exception:
            pass

    def _login_ok(self, user, password):
        """True when the mysql/mariadb client logs in with these credentials."""
        from myflames.connector import MySQLConnection
        try:
            with MySQLConnection(host=self.conn["host"], port=int(self.conn["port"]),
                                 user=user, password=password, ssl_mode="PREFERRED",
                                 connect_timeout=10, query_timeout=10) as conn:
                conn.query_rows("SELECT 1")
        except Exception:
            return False
        return True

    def test_01_users_and_roles(self):
        user, host = _unique("mf_w_"), "%"
        first, second = _secret_password("one"), _secret_password("two")
        self.addCleanup(self._drop_account, user, host)
        _applied(self._account_apply("create", user, host, password=first), "create")
        _applied(self._account_apply("password", user, host, password=second), "password change")
        self.assertTrue(self._login_ok(user, second),
                        "the final password does not log in with the client")
        self.assertFalse(self._login_ok(user, first),
                         "the first password still logs in after the change")
        _applied(self._account_apply("drop", user, host), "drop")

    # -- Variables ----------------------------------------------------------

    def _set_variable(self, name, value):
        """Review then apply one global variable change, as the workspace does."""
        plan = self.lab.api("/api/live/variable/plan",
                            {"connection": dict(self.conn), "name": name,
                             "mode": "global", "value": value})
        return self.lab.api("/api/live/variable/apply",
                            {"connection": dict(self.conn), "name": name, "mode": "global",
                             "value": value, "expect": plan.get("expect"), "confirmed": True})

    def _global_value(self, name):
        report = self.lab.api("/api/live/variable",
                              {"connection": dict(self.conn), "name": name})
        return report.get("global_value")

    def test_02_variable_global(self):
        name = "long_query_time"
        original = self._global_value(name)
        self.assertIsNotNone(original, "no global_value for %s" % name)
        target = 2.0 if float(original) != 2.0 else 3.0
        self.addCleanup(self._restore_variable, name, original)
        result = self._set_variable(name, target)
        self.assertEqual(result.get("status"), "changed",
                         "set did not report changed: %s"
                         % redact(result.get("message") or result.get("status")))
        self.assertEqual(float(self._global_value(name)), target,
                         "the server does not report the new %s" % name)
        self._restore_variable(name, original)
        self.assertEqual(float(self._global_value(name)), float(original),
                         "the server does not report the restored %s" % name)

    def _restore_variable(self, name, original):
        """The original value back, from the test body and again as cleanup."""
        result = self._set_variable(name, original)
        if result.get("status") not in ("changed", "unchanged"):
            raise AssertionError("restore of %s did not report done: %s"
                                 % (name, redact(result.get("message") or result.get("status"))))

    # -- Query editor ---------------------------------------------------------

    def _editor(self, path, **payload):
        payload.setdefault("tab_id", self.TAB)
        payload.setdefault("connection", dict(self.conn))
        return self.lab.api(path, payload)

    def _confirmed_run(self, sql, wait=30):
        """One editor run with the review step, as the workspace sends it."""
        first = self._editor("/api/editor/run", sql=sql, require_confirmation=True, wait=wait)
        self.assertEqual(first.get("status"), "needs_confirmation",
                         "the write did not come back for review: %s"
                         % redact(first.get("message") or first.get("status")))
        token = (first.get("confirmation") or {}).get("token")
        self.assertTrue(token, "no confirmation token issued")
        return self._editor("/api/editor/run", sql=sql, require_confirmation=True,
                            confirmed=True, confirm_token=token, wait=wait)

    def _drop_schema(self, schema):
        """Cleanup: the throwaway schema goes away also when a test failed."""
        try:
            first = self._editor("/api/editor/run", sql="DROP DATABASE IF EXISTS `%s`" % schema,
                                 require_confirmation=True, wait=30)
            if first.get("status") == "needs_confirmation":
                token = (first.get("confirmation") or {}).get("token")
                self._editor("/api/editor/run", sql="DROP DATABASE IF EXISTS `%s`" % schema,
                             require_confirmation=True, confirmed=True,
                             confirm_token=token, wait=30)
        except Exception:
            pass

    def test_03_editor_writes_and_stop(self):
        schema = _unique("mf_lw_")
        self.addCleanup(self._drop_schema, schema)
        created = self._confirmed_run("CREATE DATABASE `%s`" % schema)
        self.assertEqual(created.get("status"), "complete",
                         "CREATE DATABASE did not complete: %s"
                         % redact(created.get("message") or created.get("status")))
        # DDL goes one statement per confirmed run; the editor refuses a mix.
        for sql in ("CREATE TABLE `%s`.`t` (id INT PRIMARY KEY)" % schema,
                    "INSERT INTO `%s`.`t` VALUES (1), (2)" % schema,
                    "DROP TABLE `%s`.`t`" % schema):
            result = self._confirmed_run(sql)
            self.assertEqual(result.get("status"), "complete",
                             "the confirmed write did not complete: %s"
                             % redact(result.get("message") or result.get("status")))

        outcome = {}

        def sleeper():
            started = time.monotonic()
            try:
                out = self._editor("/api/editor/run", sql="SELECT SLEEP(8) AS slept", wait=25)
                outcome.update(result=out, elapsed=time.monotonic() - started)
            except Exception as exc:  # noqa: BLE001 - asserted below, redacted
                outcome.update(error=redact(str(exc)), elapsed=time.monotonic() - started)

        thread = threading.Thread(target=sleeper)
        thread.start()
        time.sleep(2)
        stop = self._editor("/api/editor/stop")
        thread.join(40)
        self.assertFalse(thread.is_alive(), "the sleep run never finished")
        # A direct connection is confirmable, so Stop sends KILL QUERY and the
        # sleep ends early (the opposite of the multiplexed ProxySQL refusal).
        self.assertTrue(stop.get("stopped"), "Stop did not send the kill: %s"
                        % redact(stop.get("message") or ""))
        self.assertIn("kill", (stop.get("message") or "").lower(),
                      "unexpected Stop answer: %s" % redact(stop.get("message") or ""))
        self.assertNotIn("error", outcome, "the sleep run errored: %s" % outcome.get("error"))
        self.assertLess(outcome["elapsed"], 7.0,
                        "the sleep ran %.1fs of 8s: the kill did not stop it" % outcome["elapsed"])
        run = outcome["result"].get("run") or {}
        self.assertEqual(outcome["result"].get("status"), "stopped",
                         "the interrupted run did not report stopped: %s"
                         % redact(outcome["result"].get("message") or outcome["result"].get("status")))
        self.assertTrue((run.get("stopped") or {}).get("interrupted"),
                        "the run does not report the statement interrupted")


class MySQLWritesTest(_WritesBase, unittest.TestCase):
    """MySQL 8.4: the InnoDB Cluster primary's saved connection."""

    CLUSTERS = ("ic",)
    PROFILE = None  # resolved to the current primary in setUpClass
    TAB = "writes-mysql"


class MariaDBWritesTest(_WritesBase, unittest.TestCase):
    """MariaDB 11.8: one Galera node's saved connection."""

    CLUSTERS = ("galera",)
    PROFILE = "cluster-galera-1"
    TAB = "writes-mariadb"


if __name__ == "__main__":
    unittest.main()
