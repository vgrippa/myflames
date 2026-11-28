"""Rejoin checks the UUID at the exact endpoint that AdminAPI will contact.

Only SQL and Shell transports are synthetic; the reviewed guard and refusal
handling run unchanged. These checks do not establish live server behavior.
"""
from contextlib import contextmanager
import json
import subprocess
import types
import unittest
from unittest import mock

import test_cluster_shell as fixtures
from myflames import cluster, cluster_shell, reviewed_writes
from myflames.connector import ConnectorError


class RejoinIdentityTests(unittest.TestCase):
    def setUp(self):
        self.report = fixtures.action_report("ic-8.4-member-offline")
        self.member = fixtures.missing(self.report)
        self.plan = cluster.plan_action(self.report, "rejoin_instance", self.member["key"], fixtures.CHECKS)
        self.source = types.SimpleNamespace(host="reviewed-primary", port=3310, user="admin", password="secret",
                                            socket="/tmp/source.sock", ssl_mode="VERIFY_CA", ssl_ca="/fixture/ca.pem",
                                            ssl_cert="/fixture/client.pem", ssl_key="/fixture/client.key",
                                            _binary_override="/fixture/mysql")
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
        writer = cluster_shell.ShellWriter(self.source, fixtures.SHELL)
        return reviewed_writes.execute(writer, self.plan, lambda: self.plan, cluster.ACTION_SCHEMA_VERSION,
                                        typed=self.plan["typed_guard"], timeout=cluster.SHELL_TIMEOUT)

    def test_rejoin_review_binds_target_endpoint_and_uuid(self):
        report = fixtures.action_report("ic-8.4-member-offline")
        member = fixtures.missing(report)
        plan = cluster.plan_action(report, "rejoin_instance", member["key"], fixtures.CHECKS)
        expected = {"address": member["address"], "server_uuid": member["uuid"]}
        self.assertEqual(plan["expect"]["server_identity"].get("target_instance"), expected)
        guard = reviewed_writes.session_guard(plan)
        self.assertEqual(len(guard), 1)
        self.assertTrue(guard[0].startswith("/*myflames:instance "))
        self.assertIn(member["uuid"], guard[0])
        self.assertIn(member["address"], guard[0])

    def test_correct_target_is_probed_with_credentials_tls_and_exact_script_preserved(self):
        calls = []
        self.source._login_guard = lambda: calls.append("consent")
        with self.transports() as (opened, credentials, shell):
            result = self.apply()
        self.assertEqual(result["status"], "done", result)
        host, port = cluster._split_address(self.member["address"])
        options = opened.call_args[1]
        self.assertEqual((options["host"], options["port"]), (host, port))
        for key in ("user", "password", "ssl_mode", "ssl_ca", "ssl_cert", "ssl_key"):
            self.assertEqual(options[key], getattr(self.source, key))
        self.assertEqual(options["binary"], self.source._binary_override)
        self.assertNotIn("socket", options)
        self.assertIs(self.target._login_guard, self.source._login_guard)
        self.target.run.assert_called_once_with(cluster.CLUSTER_TAG + "SELECT @@global.server_uuid",
                                                extra_flags=["--comments"], timeout=30)
        self.assertGreaterEqual(len(calls), 3)
        expected = "\n".join(reviewed_writes.session_guard(self.plan) + self.plan["statements"]).encode("utf-8")
        self.assertEqual(shell.call_args[1]["input"], expected)
        credentials.assert_called_once()

    def test_changed_missing_or_malformed_target_uuid_refuses_without_shell_spawn(self):
        for output in ("11111111-2222-3333-4444-555555555555\n", "", "NULL\n", "not-a-uuid\n",
                       self.member["uuid"] + "\n" + self.member["uuid"] + "\n",
                       self.member["uuid"] + "\textra\n"):
            with self.subTest(output=output):
                self.target.run.return_value = output
                with self.transports() as (opened, credentials, shell):
                    result = self.apply()
                self.assertEqual(result["status"], "refused", result)
                self.assertEqual(result["outcome"], "identity_mismatch")
                self.assertEqual(result["not_sent"], self.plan["statements"])
                self.assertEqual(result["sent"], [])
                credentials.assert_not_called()
                shell.assert_not_called()

    def test_unreadable_target_refuses_without_shell_spawn(self):
        self.target.run.side_effect = ConnectorError("cannot connect")
        with self.transports() as (opened, credentials, shell):
            result = self.apply()
        self.assertEqual(result["status"], "refused", result)
        self.assertEqual(result["not_sent"], self.plan["statements"])
        credentials.assert_not_called()
        shell.assert_not_called()

    def test_consent_changed_during_probe_refuses_before_shell_credentials(self):
        allowed = [True]

        def consent():
            if not allowed[0]:
                raise reviewed_writes.Refusal("Group consent changed", "changed")

        def probe(*args, **kwargs):
            allowed[0] = False
            return self.member["uuid"] + "\n"

        self.source._login_guard = consent
        self.target.run.side_effect = probe
        with self.transports() as (opened, credentials, shell):
            result = self.apply()
        self.assertEqual(result["status"], "refused", result)
        self.assertEqual(result["not_sent"], self.plan["statements"])
        credentials.assert_not_called()
        shell.assert_not_called()

    def test_invalid_transport_marker_refuses_before_any_connection(self):
        for target in ({"address": "victim:3306", "server_uuid": "invalid"},
                       {"address": "victim:65536", "server_uuid": self.member["uuid"]},
                       {"address": "victim:3306*/doWrite();/*", "server_uuid": self.member["uuid"]},
                       {"address": "victim:3306", "server_uuid": self.member["uuid"], "password": "injected"}):
            with self.subTest(target=target):
                script = "/*myflames:instance %s*/ " % json.dumps(target) + "print('never');"
                with self.transports() as (opened, credentials, shell), \
                        self.assertRaises(reviewed_writes.Refusal):
                    cluster_shell.ShellWriter(self.source, fixtures.SHELL).run_write(script)
                opened.assert_not_called()
                credentials.assert_not_called()
                shell.assert_not_called()

    def test_malformed_transport_marker_refuses_before_any_connection(self):
        target = json.dumps({"address": self.member["address"], "server_uuid": self.member["uuid"]})
        for script in ("/*myflames:instance" + target + "*/ print('never');",
                       "/*myflames:instance " + target,
                       "/*myflames:instance " + target + "\n*/ print('never');",
                       "/*myflames:instance \n" + target + "*/ print('never');"):
            with self.subTest(script=script):
                with self.transports() as (opened, credentials, shell), \
                        self.assertRaises(reviewed_writes.Refusal):
                    cluster_shell.ShellWriter(self.source, fixtures.SHELL).run_write(script)
                opened.assert_not_called()
                credentials.assert_not_called()
                shell.assert_not_called()

    def test_ipv6_target_probes_the_reviewed_host_and_port(self):
        address = "[2001:db8::42]:3311"
        self.plan["expect"]["server_identity"]["target_instance"]["address"] = address
        self.plan["statements"] = [statement.replace(self.member["address"], address)
                                   for statement in self.plan["statements"]]
        with self.transports() as (opened, credentials, shell):
            result = self.apply()
        self.assertEqual(result["status"], "done", result)
        self.assertEqual(opened.call_args[1]["host"], "2001:db8::42")
        self.assertEqual(opened.call_args[1]["port"], 3311)
        self.assertIn(address.encode("utf-8"), shell.call_args[1]["input"])


if __name__ == "__main__":
    unittest.main()
