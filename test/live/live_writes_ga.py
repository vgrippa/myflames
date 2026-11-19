"""Live suite: reviewed writes on every GA server of the GA matrix.

The server-identity check that reviewed writes send with each statement was
proven live on MySQL 8.4 and MariaDB 11.8 by live_writes.py (the cluster
labs). This suite proves the same on every GA engine that is up in
scripts/ga-matrix.sh (one class per server, skipped when the server is not
running), plus the two error paths a security review asked for, all through
the app's own review then apply endpoints (myflames.ui.api_response), as
live_writes.py does:

- Users and roles: create a throwaway account (unique name), rename it to
  another unique name, change its password to a value with a backslash and
  a quote, drop it; each step must report applied, and the account is
  dropped also on failure. Create and rename both read SHOW GRANTS of an
  account that does not exist yet: the expected 1141 must reach the app's
  fallback (accounts.read_account), not fail the review. MySQL 5.7 may
  refuse an operation it lacks: the refusal must state its reason instead
  of failing the test.
- Expired password: a throwaway account created, granted SELECT on a
  throwaway schema, then expired (PASSWORD EXPIRE); a profile saved for it
  through the app. Reading its privileges must return the expired-password
  message (error 1820 on MySQL, 1862 on MariaDB, before any other statement
  runs), never a crash or a generic identity message alone, and a reviewed
  write planned on that connection must refuse with nothing sent (the
  server value is unchanged afterwards). The account is dropped at the end,
  also on failure.
- Variables: review and apply a harmless global (long_query_time) and
  restore it, as live_writes.py does (restored also on failure).
- Query editor: in a throwaway schema with a canary table, a DELIMITER
  procedure wrapper must refuse even when confirmed, and a versioned-comment
  DELETE (a DELETE on the server's own reading of the executable comment)
  must come back for confirmation as a write with nothing sent; the canary
  must still be there. A CREATE DATABASE with a versioned ENCRYPTION comment
  (mysqldump's clause) is accepted on every server, with confirmation, then
  dropped. A procedure dumped by that server's own dump tool is recreated
  through one editor confirmation, including the saved-mode preamble,
  DELIMITER lines and restores; the throwaway schema is always dropped.

Credentials come only from the matrix's 0600 credentials file (read by
_lab.ga_passwords for redaction, never printed). The saved profiles come
from ``ga-matrix.sh profiles`` into the lane's throwaway home
(_lab.make_ga_home), the way _lab.make_home runs the cluster matrix's
``profiles``.

Run: scripts/live-tests.sh ga   (or MYFLAMES_LIVE_LABS=1 with the matrix up)
"""
import os
import re
import subprocess
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lab import (Lab, LabError, conn_for, ga_passwords, ga_servers,
                  make_ga_home, redact, require_live)  # noqa: E402


def setUpModule():
    require_live()


def _unique(prefix):
    """A throwaway name that cannot collide with an earlier or parallel run."""
    return "%s%s" % (prefix, uuid.uuid4().hex[:10])


def _secret_password(tag):
    """A password with a backslash and a single quote: the quoting the unit
    tests' fake server never checked."""
    return "mf\\'%s-%s" % (tag, uuid.uuid4().hex[:8])


def _routine_section(dump):
    """Keep one dump routine's original preamble, definition and restores.

    The shape is recorded in test/fixtures/dumps. MySQL saves character
    settings before sql_mode; MariaDB saves sql_mode first. Copy both orders
    completely, excluding DROP so review contains exactly one DDL. The dump's
    global header, including MariaDB's sandbox command, is outside this slice.
    """
    marker = dump.index("-- Dumping routines for database")
    preamble = re.search(r"/\*!50003 SET @saved_(?:cs_client|sql_mode)\s*=", dump[marker:])
    if preamble is None:
        raise ValueError("The dump has no routine session-setting preamble.")
    start = marker + preamble.start()
    restore = re.search(r"/\*!50003 SET collation_connection\s*=\s*@saved_col_connection\s*\*/\s*;",
                        dump[start:])
    if restore is None:
        raise ValueError("The dump has no routine collation restore.")
    section = dump[start:start + restore.end()]
    return "\n".join(line for line in section.splitlines()
                     if not re.fullmatch(r"/\*!50003 DROP PROCEDURE IF EXISTS `(?:``|[^`])+` \*/;", line))


#: Editor run answers that mean nothing was sent.
_REFUSAL_STATUSES = ("invalid", "refused", "declined")


class _GAWritesBase(object):
    """One GA server's reviewed writes against its saved matrix connection.

    Subclasses set SERVER (a ga-matrix.sh name; the saved profile is
    ga-SERVER, saved by ``ga-matrix.sh profiles``)."""

    SERVER = None

    @classmethod
    def setUpClass(cls):
        require_live()
        try:
            servers = ga_servers()
        except LabError as exc:
            raise unittest.SkipTest("the GA matrix is not available here: %s" % redact(exc))
        entry = servers.get(cls.SERVER)
        if not entry or not entry.get("up"):
            raise unittest.SkipTest("GA server %s is not running (scripts/ga-matrix.sh up %s)"
                                    % (cls.SERVER, cls.SERVER))
        cls.home = make_ga_home("writes-ga")
        cls.ga_server = entry
        cls.lab = Lab()
        cls.profile = cls.lab.profile("ga-" + cls.SERVER)
        cls.conn = conn_for(cls.profile)
        cls.TAB = "ga-writes-" + cls.SERVER

    @classmethod
    def tearDownClass(cls):
        try:
            cls.lab.api("/api/editor/close",
                        {"tab_id": cls.TAB, "connection": dict(cls.conn), "discard": True})
        except Exception:
            pass

    # -- shared helpers -------------------------------------------------------

    def _applied(self, result, what):
        """The change reports applied; a MySQL 5.7 refusal must state its reason."""
        if result.get("status") == "applied":
            return
        reason = redact(result.get("message") or result.get("error") or result.get("status") or "")
        if self.SERVER == "mysql-5.7":
            self.assertTrue(reason.strip(), "MySQL 5.7 refused %s without stating a reason" % what)
            raise unittest.SkipTest("MySQL 5.7 refused %s: %s" % (what, reason))
        raise AssertionError("%s did not report applied: %s" % (what, reason))

    def _account_apply(self, operation, user, host, password=None, **options):
        """Review then apply one account change, as the workspace sends it."""
        payload = {"connection": dict(self.conn), "operation": operation,
                   "user": user, "host": host}
        payload.update(options)
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

    def _drop_profile(self, name):
        """Cleanup: the throwaway saved connection goes away also on failure."""
        try:
            profile = self.lab.profile(name)
            self.lab.api("/api/profiles/delete", {"id": profile["id"]})
        except Exception:
            pass

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
        confirmation = first.get("confirmation") or {}
        token = confirmation.get("token")
        self.assertTrue(token, "no confirmation token issued")
        return self._editor("/api/editor/run", sql=sql, require_confirmation=True,
                            confirmed=True, confirm_token=token,
                            typed_target=confirmation.get("typed_target"), wait=wait)

    def _run_with_confirmation(self, sql, wait=30):
        """One editor run, confirmed when it comes back for review; the answer."""
        first = self._editor("/api/editor/run", sql=sql, require_confirmation=True, wait=wait)
        if first.get("status") != "needs_confirmation":
            return first
        token = (first.get("confirmation") or {}).get("token")
        self.assertTrue(token, "no confirmation token issued")
        return self._editor("/api/editor/run", sql=sql, require_confirmation=True,
                            confirmed=True, confirm_token=token, wait=wait)

    def _drop_schema(self, schema):
        """Cleanup: the throwaway schema goes away also when a test failed."""
        try:
            self._confirmed_run("DROP DATABASE IF EXISTS `%s`" % schema)
        except Exception:
            pass

    # -- (a) Users and roles: the 1141 fallback on create and rename ---------

    def test_01_users_and_roles(self):
        user, host = _unique("mf_ga_"), "%"
        renamed = _unique("mf_gb_")
        self.addCleanup(self._drop_account, renamed, host)
        self.addCleanup(self._drop_account, user, host)
        # Create reviews SHOW GRANTS of an account that does not exist yet:
        # the expected 1141 must reach the app's fallback, not fail the review.
        self._applied(self._account_apply("create", user, host, password=_secret_password("one")),
                      "create")
        # Renaming reads the destination the same way: another absent account.
        self._applied(self._account_apply("rename", user, host, new_name=renamed), "rename")
        self._applied(self._account_apply("password", renamed, host,
                                          password=_secret_password("two")), "password change")
        self._applied(self._account_apply("drop", renamed, host), "drop")

    # -- (b) Expired password: 1820/1862 before any other statement ----------

    def _expired_message(self, fn, what):
        """The redacted message of fn()'s refusal; fails when nothing refused."""
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - asserted below, redacted
            message = redact(str(exc))
        else:
            message = ""
        self.assertTrue(message, "%s on an expired-password connection was not refused" % what)
        self.assertTrue(re.search(r"expired|reset your password|set password", message, re.I),
                        "%s did not return the expired-password message: %s" % (what, message))
        return message

    def test_02_expired_password(self):
        user, host = _unique("mf_ge_"), "%"
        schema = _unique("mf_gexp_")
        profile_name = _unique("mf-gep-")
        password = _secret_password("exp")
        self.addCleanup(self._drop_account, user, host)
        self.addCleanup(self._drop_profile, profile_name)
        self.addCleanup(self._drop_schema, schema)
        created = self._confirmed_run("CREATE DATABASE `%s`" % schema)
        self.assertEqual(created.get("status"), "complete",
                         "CREATE DATABASE did not complete: %s"
                         % redact(created.get("message") or created.get("status")))
        self._applied(self._account_apply("create", user, host, password=password), "create")
        self._applied(self._account_apply("grant", user, host, level="database",
                                          database=schema, privileges=["SELECT"]),
                      "grant SELECT")
        self._applied(self._account_apply("expire", user, host), "expire")
        # A saved connection for the expired account, through the app.
        self.lab.api("/api/profiles/save", {
            "value": {"name": profile_name, "host": self.conn["host"],
                      "port": int(self.conn["port"]), "user": user,
                      "ssl_mode": "PREFERRED", "ssl_ca": "",
                      "type": "development", "read_only": False},
            "password": password, "remember_password": True})
        expired_conn = conn_for(self.lab.profile(profile_name))
        # Reading its privileges returns the expired-password message, never a
        # crash or a generic identity message alone.
        self._expired_message(
            lambda: self.lab.api("/api/live/privileges", {"connection": expired_conn}),
            "the privileges read")
        # A reviewed write planned on that connection refuses with nothing sent.
        before = self._global_value("long_query_time")
        self.assertIsNotNone(before, "no global_value for long_query_time")
        self._expired_message(
            lambda: self.lab.api("/api/live/variable/plan",
                                 {"connection": expired_conn, "name": "long_query_time",
                                  "mode": "global", "value": 7.0}),
            "the reviewed variable change")
        self.assertEqual(self._global_value("long_query_time"), before,
                         "the refused write reached the server: long_query_time changed")
        self._applied(self._account_apply("drop", user, host), "drop")

    # -- (c) Variables ---------------------------------------------------------

    def test_03_variable_global(self):
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

    # -- (d) Query editor: refused wrappers, canary intact --------------------

    def _assert_refused(self, result, what):
        status = result.get("status")
        self.assertIn(status, _REFUSAL_STATUSES,
                      "%s was not refused (status %s): %s"
                      % (what, status, redact(result.get("message") or result.get("status"))))
        self.assertTrue(result.get("message") or result.get("code"),
                        "%s was refused without a stated reason" % what)

    def _assert_canary(self, schema):
        result = self._editor("/api/editor/run",
                              sql="SELECT COUNT(*) FROM `%s`.`canary`" % schema, wait=30)
        self.assertEqual(result.get("status"), "complete",
                         "the canary table did not survive: %s"
                         % redact(result.get("message") or result.get("status")))
        run = result.get("run") or {}
        rows = [row for part in run.get("results", []) if part.get("type") == "rows"
                for row in part.get("rows", [])]
        self.assertEqual(rows, [["1"]], "the canary row changed: %s" % rows)

    def test_04_editor_refusals(self):
        schema = _unique("mf_gw_")
        self.addCleanup(self._drop_schema, schema)
        for sql in ("CREATE DATABASE `%s`" % schema,
                    "CREATE TABLE `%s`.`canary` (id INT PRIMARY KEY)" % schema,
                    "INSERT INTO `%s`.`canary` VALUES (1)" % schema):
            result = self._confirmed_run(sql)
            self.assertEqual(result.get("status"), "complete",
                             "the confirmed write did not complete: %s"
                             % redact(result.get("message") or result.get("status")))
        # A DELIMITER procedure wrapper must refuse, even when confirmed.
        result = self._run_with_confirmation(
            "DELIMITER $$\nCREATE PROCEDURE p() /*!999999 BEGIN */ SELECT 1; DROP TABLE canary;"
            " /*!999999 END */ $$")
        self._assert_refused(result, "DELIMITER procedure wrapper")
        self._assert_canary(schema)
        # The server reads the executable comment, so this is a DELETE: the
        # review asks for confirmation of a write and, unconfirmed, sends
        # nothing.
        first = self._editor("/api/editor/run",
                             sql="/*!999999 SELECT 1 FROM ( */ DELETE FROM canary /*!999999 ) x */",
                             require_confirmation=True, wait=30)
        self.assertEqual(first.get("status"), "needs_confirmation",
                         "the versioned-comment DELETE did not come back for review: %s"
                         % redact(first.get("message") or first.get("status")))
        self.assertIn((first.get("confirmation") or {}).get("kind"), ("write", "risky"),
                      "the versioned-comment DELETE was not reviewed as a write: %s"
                      % redact(str(first.get("confirmation"))))
        self.assertNotIn("run", first, "the unconfirmed run sent statements")
        self._assert_canary(schema)
        # A versioned comment every server reads as part of a plain CREATE
        # DATABASE (mysqldump's ENCRYPTION clause) is accepted everywhere,
        # with confirmation, then dropped.
        enc_schema = _unique("mf_genc_")
        self.addCleanup(self._drop_schema, enc_schema)
        created = self._confirmed_run(
            "CREATE DATABASE `%s` /*!80016 DEFAULT ENCRYPTION='N' */" % enc_schema)
        self.assertEqual(created.get("status"), "complete",
                         "CREATE DATABASE with a versioned comment did not complete: %s"
                         % redact(created.get("message") or created.get("status")))

    def test_05_editor_dump_routine(self):
        """A same-server dump section reviews once and restores a real routine."""
        schema, routine = _unique("mf_gdump_"), "dump_procedure"
        try:
            for sql in ("CREATE DATABASE `%s`" % schema, "USE `%s`" % schema,
                        "DELIMITER ;;\nCREATE PROCEDURE `%s`() "
                        "BEGIN SELECT 1 /* note */; END;;\nDELIMITER ;" % routine):
                result = self._confirmed_run(sql)
                self.assertEqual(result.get("status"), "complete", redact(str(result)))

            # Use the binary installed inside this existing GA container,
            # matching its server version. No service is started or stopped.
            engine = self.ga_server["engine"]
            tool = "mariadb-dump" if engine == "mariadb" else "mysqldump"
            args = ["docker", "exec", "-e", "MYSQL_PWD", self.ga_server["container"],
                    tool, "--no-defaults", "--user=mf_admin", "--no-data", "--no-create-info",
                    "--skip-triggers", "--routines", "--skip-add-drop-table"]
            if engine == "mysql":
                args.append("--set-gtid-purged=OFF")
            args.extend(("--databases", schema))
            env = dict(os.environ, MYSQL_PWD=ga_passwords()["MF_ADMIN_PASSWORD"])
            dumped = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    universal_newlines=True, env=env, timeout=25)
            self.assertEqual(dumped.returncode, 0, redact(dumped.stderr))
            section = _routine_section(dumped.stdout)
            for expected in ("SET @saved_sql_mode", "DELIMITER ;;", "PROCEDURE `%s`" % routine,
                             "DELIMITER ;", "= @saved_sql_mode", "= @saved_cs_client"):
                self.assertIn(expected, section)
            dropped = self._confirmed_run("DROP PROCEDURE `%s`.`%s`" % (schema, routine))
            self.assertEqual(dropped.get("status"), "complete", redact(str(dropped)))

            def routine_count():
                result = self._editor("/api/editor/run", sql=(
                    "SELECT COUNT(*) FROM information_schema.ROUTINES "
                    "WHERE ROUTINE_SCHEMA='%s' AND ROUTINE_NAME='%s' AND ROUTINE_TYPE='PROCEDURE'"
                    % (schema, routine)), wait=30)
                self.assertEqual(result.get("status"), "complete", redact(str(result)))
                return [row for part in (result.get("run") or {}).get("results", [])
                        if part.get("type") == "rows" for row in part.get("rows", [])]

            self.assertEqual(routine_count(), [["0"]])
            first = self._editor("/api/editor/run", sql=section, require_confirmation=True, wait=30)
            self.assertEqual(first.get("status"), "needs_confirmation", redact(str(first)))
            self.assertNotIn("run", first, "unconfirmed dump SQL was executed")
            confirmation = first.get("confirmation") or {}
            token = confirmation.get("token")
            self.assertTrue(token, "the complete dump section needs one confirmation token")
            statements = confirmation.get("statements") or []
            self.assertEqual(sum("CREATE" in sql and "PROCEDURE" in sql for sql in statements), 1)
            self.assertEqual(routine_count(), [["0"]], "the unconfirmed routine already exists")
            result = self._editor("/api/editor/run", sql=section, require_confirmation=True,
                                  confirmed=True, confirm_token=token, wait=30)
            self.assertEqual(result.get("status"), "complete", redact(str(result)))
            self.assertEqual(routine_count(), [["1"]], "the confirmed dump did not recreate the routine")
        finally:
            dropped = self._confirmed_run("DROP DATABASE IF EXISTS `%s`" % schema)
            self.assertEqual(dropped.get("status"), "complete", redact(str(dropped)))


    def test_06_editor_resynchronizes_status_after_stored_function(self):
        """A stored SET leaves NBE set in the reply after sql_mode is restored.

        Exercise the real editor and client on every running GA server. The
        vulnerable read contains an apparent second SELECT inside its string;
        the helper must restore client framing without changing these bytes.
        """
        from myflames import dbops, editor_session
        schema = _unique("mf_gbits_")
        sql = "SELECT 'a\\'; SELECT 'SPLIT-AFTER' AS marker; -- ' AS probe"
        try:
            for setup in ("CREATE DATABASE %s" % schema, "SET SESSION sql_mode = ''",
                          "DELIMITER //\nCREATE FUNCTION %s.f_nbe() RETURNS INT "
                          "DETERMINISTIC NO SQL BEGIN SET sql_mode='NO_BACKSLASH_ESCAPES'; "
                          "RETURN 1; END//\nDELIMITER ;" % schema):
                result = self._confirmed_run(setup)
                self.assertEqual(result.get("status"), "complete", redact(str(result)))
            called = self._confirmed_run("SELECT %s.f_nbe()" % schema)
            self.assertEqual(called.get("status"), "complete", redact(str(called)))
            self.assertEqual(called["session"]["sql_mode"], "", "stored-function mode must be restored")
            review = self._editor("/api/editor/run", sql=sql, require_confirmation=True, wait=30)
            self.assertEqual(review.get("status"), "needs_confirmation", redact(str(review)))
            self.assertEqual(review["confirmation"]["statements"],
                             [editor_session.TAG + dbops.CLIENT_STATUS_SYNC, sql])
            result = self._editor("/api/editor/run", sql=sql, require_confirmation=True, confirmed=True,
                                  confirm_token=review["confirmation"]["token"], wait=30)
            self.assertEqual(result.get("status"), "complete", redact(str(result)))
            run = result.get("run") or {}
            self.assertEqual(len(run.get("statements", [])), 1)
            rows = [part for part in run.get("results", []) if part.get("type") == "rows"]
            self.assertEqual(len(rows), 1, "the client must not expose a hidden second result")
            self.assertEqual([column["name"] for column in rows[0]["columns"]], ["probe"])
            self.assertEqual(rows[0]["rows"], [["a'; SELECT 'SPLIT-AFTER' AS marker; -- "]])
        finally:
            dropped = self._confirmed_run("DROP DATABASE IF EXISTS %s" % schema)
            self.assertEqual(dropped.get("status"), "complete", redact(str(dropped)))

class MySQL57WritesTest(_GAWritesBase, unittest.TestCase):
    """MySQL 5.7 (the last 5.7 release): the GA matrix's mysql-5.7."""

    SERVER = "mysql-5.7"


class MySQL80WritesTest(_GAWritesBase, unittest.TestCase):
    """MySQL 8.0: the GA matrix's mysql-8.0."""

    SERVER = "mysql-8.0"


class MySQL84WritesTest(_GAWritesBase, unittest.TestCase):
    """MySQL 8.4 LTS: the GA matrix's mysql-8.4."""

    SERVER = "mysql-8.4"


class MySQL97WritesTest(_GAWritesBase, unittest.TestCase):
    """MySQL 9.7: the GA matrix's mysql-9.7."""

    SERVER = "mysql-9.7"


class Percona80WritesTest(_GAWritesBase, unittest.TestCase):
    """Percona Server 8.0 (a MySQL flavor): the GA matrix's percona-8.0."""

    SERVER = "percona-8.0"


class Percona84WritesTest(_GAWritesBase, unittest.TestCase):
    """Percona Server 8.4 (a MySQL flavor): the GA matrix's percona-8.4."""

    SERVER = "percona-8.4"


class MariaDB1011WritesTest(_GAWritesBase, unittest.TestCase):
    """MariaDB 10.11: the GA matrix's mariadb-10.11."""

    SERVER = "mariadb-10.11"


class MariaDB114WritesTest(_GAWritesBase, unittest.TestCase):
    """MariaDB 11.4: the GA matrix's mariadb-11.4."""

    SERVER = "mariadb-11.4"


class MariaDB118WritesTest(_GAWritesBase, unittest.TestCase):
    """MariaDB 11.8: the GA matrix's mariadb-11.8."""

    SERVER = "mariadb-11.8"


if __name__ == "__main__":
    unittest.main()
