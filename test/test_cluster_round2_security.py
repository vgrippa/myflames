"""ClusterSet round 2 regressions with real review and identity checks.

Only external SQL/Shell transports are synthetic. No live server behavior
is established by these tests.
"""
from contextlib import contextmanager
import json
import subprocess
import time
import types
import unittest
from unittest import mock

import test_cluster_shell as fixtures
from myflames import cluster, cluster_shell, reviewed_writes


class PrimaryInstanceIdentityTests(fixtures.Home):
    def setUp(self):
        super().setUp()
        self.report = fixtures.action_report("ic-8.4-clusterset-replica")
        self.member = next(item for item in self.report["members"] if item["role"] == "SECONDARY")
        self.plan = cluster.plan_action(self.report, "switchover", self.member["key"], fixtures.CHECKS)
        self.source = types.SimpleNamespace(host="reviewed-primary", port=3306, user="admin", password="secret",
                                            socket="/tmp/source.sock", ssl_mode="PREFERRED")
        self.target = mock.MagicMock()
        self.target.__enter__.return_value = self.target
        self.target.run.return_value = self.member["uuid"] + "\n"

    @contextmanager
    def transports(self):
        @contextmanager
        def credentials(options, tool):
            yield ["/fixture/mysqlsh", "--defaults-file=/fixture/private.cnf"], {}

        with mock.patch("myflames.connector.MySQLConnection", return_value=self.target) as opened, \
                mock.patch("myflames.lifecycle._credentials", side_effect=credentials) as credential_file, \
                mock.patch("myflames.connector.checked_shell_program", return_value="/fixture/mysqlsh"), \
                mock.patch("myflames.connector._run_bounded", return_value=
                           subprocess.CompletedProcess([], 0, b"ok\n", b"")) as shell:
            yield opened, credential_file, shell

    def apply(self):
        return reviewed_writes.execute(cluster_shell.ShellWriter(self.source, fixtures.SHELL), self.plan,
                                        lambda: self.plan, cluster.ACTION_SCHEMA_VERSION,
                                        timeout=cluster.SHELL_TIMEOUT)

    def test_review_binds_the_primary_candidate_address_and_uuid(self):
        expected = {"address": self.member["address"], "server_uuid": self.member["uuid"]}
        details = reviewed_writes.review_details(self.plan)
        self.assertEqual(details["expect"]["server_identity"]["target_instance"], expected)
        guard = details["session_guard"]
        self.assertEqual(len(guard), 1)
        self.assertTrue(guard[0].startswith("/*myflames:instance "))
        self.assertIn(self.member["address"], guard[0])
        self.assertIn(self.member["uuid"], guard[0])

    def test_matching_uuid_probes_the_exact_address_then_sends_the_reviewed_script(self):
        with self.transports() as (opened, credentials, shell):
            result = self.apply()
        self.assertEqual(result["status"], "done", result)
        host, port = cluster._split_address(self.member["address"])
        self.assertEqual((opened.call_args[1]["host"], opened.call_args[1]["port"]), (host, port))
        self.assertNotIn("socket", opened.call_args[1])
        self.target.run.assert_called_once_with(cluster.CLUSTER_TAG + "SELECT @@global.server_uuid",
                                                extra_flags=["--comments"], timeout=30)
        script = "\n".join(reviewed_writes.session_guard(self.plan) + self.plan["statements"])
        self.assertEqual(shell.call_args[1]["input"], script.encode("utf-8"))
        credentials.assert_called_once()

    def test_address_answering_as_another_member_refuses_before_credentials_or_shell(self):
        # The wrong UUID is itself a valid member of this cluster. Shell's
        # membership check alone would therefore permit promoting that member.
        for member in self.report["members"]:
            if member["uuid"] == self.member["uuid"]:
                continue
            with self.subTest(answering_member=member["name"]):
                self.target.run.return_value = member["uuid"] + "\n"
                with self.transports() as (opened, credentials, shell):
                    result = self.apply()
                self.assertEqual(result["status"], "refused", result)
                self.assertEqual(result["outcome"], "identity_mismatch")
                self.assertEqual(result["sent"], [])
                self.assertEqual(result["not_sent"], self.plan["statements"])
                opened.assert_called_once()
                credentials.assert_not_called()
                shell.assert_not_called()


class ShellRedactionCostTests(unittest.TestCase):
    def test_unterminated_escaped_quote_lines_at_8_64_and_256_kib(self):
        # An absent closing quote must not retry the long suffix at every
        # escaped quote. A fixed generous budget catches the quadratic path;
        # assert inside the loop so a regression stops before larger inputs.
        for size in (8 * 1024, 64 * 1024, 256 * 1024):
            line = 'x"' + '\\"' * ((size - 2) // 2)
            started = time.monotonic()
            redacted = cluster_shell._redact_shell_text(line + '\n"s3cret"', "s3cret")
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 1.0, "%d KiB unterminated line took %.3f s" % (size // 1024, elapsed))
            self.assertEqual(redacted, line + '\n"[redacted]"')

    def test_unterminated_strings_and_dangling_escapes_keep_line_boundaries(self):
        password = 'p"ass\\/\u00e9'
        token = json.dumps(password).replace("/", "\\/")
        for newline in ("\n", "\r", "\r\n"):
            for ending in ("", "\\"):
                with self.subTest(newline=newline, ending=ending):
                    prefix = 'unfinished "' + '\\"' * 20 + ending + newline
                    output = cluster_shell._redact_shell_text(prefix + "{%s: %s}" % (token, token), password)
                    self.assertEqual(output, prefix + '{"[redacted]": "[redacted]"}')
        for text in ('unfinished "', 'unfinished "\\', 'unfinished "\\"\\"'):
            with self.subTest(text=text):
                self.assertEqual(cluster_shell._redact_shell_text(text, password), text)

    def test_large_closed_json_string_still_redacts_the_decoded_password(self):
        value = '"' * (128 * 1024) + "s3cret"
        started = time.monotonic()
        output = cluster_shell._redact_shell_text(json.dumps(value), "s3cret")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0, "256 KiB closed string took %.3f s" % elapsed)
        self.assertEqual(json.loads(output), value.replace("s3cret", "[redacted]"))


class AdminAPITimeoutTests(unittest.TestCase):
    def builders(self):
        version = (9, 7, 1)
        return (
            ("switchover", lambda value: cluster._shell_set_primary_cluster("replica", {"timeout": value}, version)),
            ("failover", lambda value: cluster._shell_force_primary_cluster("replica", {"timeout": value}, version)),
            ("remove", lambda value: cluster._shell_remove_cluster("replica", {"timeout": value}, version)),
            ("dissolve", lambda value: cluster._shell_dissolve_clusterset({"timeout": value}, version)),
            ("create_incremental", lambda value: cluster._shell_create_replica_cluster(
                "spare:3306", "replica", {"recoveryMethod": "incremental", "timeout": value}, version)),
            ("create_clone", lambda value: cluster._shell_create_replica_cluster(
                "spare:3306", "replica", {"recoveryMethod": "clone", "timeout": value}, version)),
        )

    def test_all_adminapi_timeout_options_stay_below_the_shell_transport_limit(self):
        for name, builder in self.builders():
            for timeout in (cluster.SHELL_TIMEOUT, cluster.SHELL_TIMEOUT + 1, 2147483647):
                with self.subTest(action=name, timeout=timeout):
                    with self.assertRaisesRegex(ValueError, "timeout must be less than the MySQL Shell time limit"):
                        builder(timeout)
            for timeout in (0, 1, cluster.SHELL_TIMEOUT - 1):
                with self.subTest(action=name, timeout=timeout):
                    # The final options argument is a JSON.parse string,
                    # so decode both the JavaScript string and its JSON.
                    encoded = builder(timeout).split("JSON.parse(", 1)[1][:-3]
                    self.assertEqual(json.loads(json.loads(encoded))["timeout"], timeout)

    def test_timeout_keeps_integer_type_and_nonnegative_validation(self):
        for name, builder in self.builders():
            for timeout in (-1, True, False, 1.5, "1", None, 2147483648):
                with self.subTest(action=name, timeout=timeout):
                    with self.assertRaises(ValueError):
                        builder(timeout)

    def test_planning_enforces_the_same_timeout_bound_and_retains_accepted_values(self):
        report = fixtures.action_report("ic-8.4-clusterset-primary")
        action = "set_primary_cluster:drcluster"
        with self.assertRaisesRegex(ValueError, "timeout must be less than"):
            cluster.plan_action(report, action, checks=fixtures.CHECKS, parameters={"timeout": cluster.SHELL_TIMEOUT})
        timeout = cluster.SHELL_TIMEOUT - 1
        plan = cluster.plan_action(report, action, checks=fixtures.CHECKS, parameters={"timeout": timeout})
        self.assertEqual(plan["parameters"], {"timeout": timeout})
        self.assertEqual(plan["expect"]["clusterset_parameters"], {"timeout": timeout})
        self.assertLess(timeout, plan["timeout_s"])


if __name__ == "__main__":
    unittest.main()
