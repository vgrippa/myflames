"""Run the replica-creation guard as JavaScript, without opening a database.

Only the external transport and Shell API are synthetic. The real guard,
private credential files, reviewed-write refusal and output adapters run.
"""
import copy
import json
import os
import shutil
import stat
import subprocess
import types
import unittest
from unittest import mock

import test_cluster_shell as fixtures
from myflames import cluster, cluster_shell, reviewed_writes


SHELL = dict(fixtures.SHELL, version="9.7.1", version_tuple=[9, 7, 1])
SPARE_UUID = "bbbbbbbb-cccc-dddd-eeee-000000000001"


class ReplicaTargetGuardTests(unittest.TestCase):
    def setUp(self):
        self.node = shutil.which("node")
        if self.node is None:
            self.skipTest("node is needed to execute the generated guard without a database")
        self.identity, self.status = fixtures.TestShellWriter._clusterset_guard_fixture()
        self.identity["target_instance"] = {"address": "spare:3310", "server_uuid": SPARE_UUID}
        self.password = "private-quote'\\\"-slash/\u00e9-secret"
        self.conn = types.SimpleNamespace(host="reviewed-primary", port=3306, user="admin",
                                          password=self.password, ssl_mode="VERIFY_CA", ssl_ca="/saved/ca.pem")

    def execute(self, method="clone", variant="match", address="spare:3310"):
        identity = copy.deepcopy(self.identity)
        identity["target_instance"]["address"] = address
        statement = cluster.CLUSTER_ACTION_TAG + cluster._shell_create_replica_cluster(
            address, "newcluster", {"recoveryMethod": method}, SHELL["version_tuple"])
        plan = {"action": "create_replica_cluster:spare", "via": "mysqlsh", "statements": [statement],
                "expect": {"engine": "mysql", "server_identity": identity}}
        config = {"identity": identity, "status": self.status, "variant": variant,
                  "target_uuid": SPARE_UUID,
                  "source_options": {"scheme": "mysql", "host": "reviewed-primary", "port": 3306,
                                     "user": "admin", "socket": "/source/mysql.sock", "schema": "source_schema",
                                     "ssl-mode": "VERIFY_CA", "ssl-ca": "/saved/ca.pem",
                                     "ssl-cert": "/saved/client.pem", "ssl-key": "/saved/client.key"}}
        harness = r'''
            const fs = require('fs'), vm = require('vm');
            const config = JSON.parse(CONFIG);
            const calls = [], events = [];
            const session = {uri: 'source-session-uri', runSql(sql) {
                events.push('source-sql');
                if (sql === 'SELECT @@global.server_uuid')
                    return {fetchOne() { return [config.identity.server_uuid]; }};
                if (sql === 'SELECT domain_name, CAST(view_id AS CHAR) FROM mysql_innodb_cluster_metadata.v2_cs_clustersets')
                    return {fetchAll() { return [[config.identity.adminapi.name, config.identity.adminapi.view_id]]; }};
                throw new Error('unexpected source SQL');
            }};
            const object = {
                status(options) { events.push('status'); return config.status; },
                createReplicaCluster(...args) { events.push('action'); calls.push(['action', args]); }
            };
            const dba = {getClusterSet() { events.push('object'); return object; }};
            const shell = {
                parseUri(uri) {
                    if (uri !== session.uri) throw new Error('wrong session URI');
                    return Object.assign({}, config.source_options);
                },
                openSession(options, password) {
                    events.push('open');
                    calls.push(['open', options]);
                    if (typeof password !== 'string' || !password) throw new Error('missing password');
                    if (config.variant === 'open-error') throw new Error('cannot connect');
                    return {
                        runSql(sql) {
                            events.push('target-sql');
                            calls.push(['target-sql', sql]);
                            if (sql !== 'SELECT @@global.server_uuid') throw new Error('unexpected target SQL');
                            if (config.variant === 'read-error') throw new Error('lost connection');
                            return {fetchOne() {
                                if (config.variant === 'missing') return null;
                                if (config.variant === 'empty') return [];
                                if (config.variant === 'wrong') return ['99999999-2222-3333-4444-555555555555'];
                                return [config.target_uuid];
                            }};
                        },
                        close() {
                            events.push('close');
                            if (config.variant === 'close-error') throw new Error('close failed');
                        }
                    };
                }
            };
            const os = {getenv: name => process.env[name], loadTextFile: path => fs.readFileSync(path, 'utf8')};
            let failure = null;
            try { vm.runInNewContext(fs.readFileSync(0, 'utf8'), {session, dba, shell, os}, {filename: 'STDIN'}); }
            catch (error) {
                failure = String(error); console.error(failure + '\n at STDIN:1'); process.exitCode = 1;
            }
            console.log(JSON.stringify({calls, events, failure}));
        '''.replace("CONFIG", json.dumps(json.dumps(config)))
        paths, traces = [], []

        def run(argv, timeout, **options):
            path = options["env"]["MYFLAMES_SHELL_PASSWORD_FILE"]
            paths.append(path)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), self.password)
            self.assertNotIn(self.password, json.dumps(argv) + json.dumps(options["env"]))
            self.assertNotIn(self.password, options["input"].decode("utf-8"))
            proc = subprocess.run([self.node, "-e", harness], input=options["input"], env=options["env"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
            traces.append(json.loads(proc.stdout.decode("utf-8")))
            return proc

        after = mock.Mock(return_value={})
        with mock.patch("myflames.connector.checked_shell_program", return_value=SHELL["path"]), \
                mock.patch("myflames.connector._run_bounded", side_effect=run):
            writer = cluster_shell.ShellWriter(self.conn, SHELL)
            result = reviewed_writes.execute(writer, plan, lambda: copy.deepcopy(plan), "test", read_after=after)
        self.assertEqual(len(traces), 1, result)
        self.assertEqual(len(paths), 1)
        for path in paths:
            self.assertFalse(os.path.exists(path), "private credentials removed after success or refusal")
            self.assertFalse(os.path.exists(os.path.dirname(path)))
        self.assertNotIn(self.password, str(writer.stdout) + str(writer.stderr) + json.dumps(result))
        script = "\n".join(reviewed_writes.session_guard(plan) + plan["statements"])
        self.assertEqual(cluster_shell._clusterset_identity(script, full=True), identity)
        return result, traces[0], after, plan

    def test_matching_target_closes_probe_immediately_before_both_recovery_methods(self):
        for method in ("clone", "incremental"):
            for address in ("spare:3310", "[2001:db8::42]:3311"):
                with self.subTest(method=method, address=address):
                    result, trace, _, plan = self.execute(method, address=address)
                    self.assertEqual(result["status"], "done", result)
                    self.assertIsNone(trace["failure"])
                    self.assertEqual(trace["events"], ["source-sql", "object", "status", "source-sql",
                                                        "open", "target-sql", "close", "action"])
                    options = trace["calls"][0][1]
                    host, port = cluster._split_address(address)
                    self.assertEqual(options, {"scheme": "mysql", "host": host, "port": port, "user": "admin",
                                               "ssl-mode": "VERIFY_CA", "ssl-ca": "/saved/ca.pem",
                                               "ssl-cert": "/saved/client.pem", "ssl-key": "/saved/client.key"})
                    self.assertEqual(trace["calls"][-1], ["action", [address, "newcluster",
                                                     {"recoveryMethod": method, "recoveryProgress": 1}]])
                    self.assertEqual(result["sent"], plan["statements"])

    def test_changed_or_unreadable_target_refuses_and_sends_no_action(self):
        for method in ("clone", "incremental"):
            for variant in ("wrong", "missing", "empty", "open-error", "read-error", "close-error"):
                with self.subTest(method=method, variant=variant):
                    result, trace, after, plan = self.execute(method, variant)
                    self.assertEqual((result["status"], result["outcome"]), ("refused", "identity_mismatch"), result)
                    self.assertEqual((result["sent"], result["not_sent"]), ([], plan["statements"]))
                    self.assertIn(cluster_shell.IDENTITY_MISMATCH_MARKER, trace["failure"])
                    self.assertNotIn("action", trace["events"])
                    self.assertEqual(trace["events"].count("close"), 0 if variant == "open-error" else 1)
                    after.assert_not_called()

    def test_tampering_with_bound_target_refuses_before_credentials_or_process(self):
        guard = cluster_shell.target_session_guard(self.identity)[0]
        for edited in (guard.replace(SPARE_UUID, "99999999-2222-3333-4444-555555555555", 1),
                       guard.replace('"spare:3310"', '"victim:3310"', 1),
                       guard.replace('myflamesTarget.close();', '')):
            with self.subTest(edited=edited), \
                    mock.patch("myflames.lifecycle._credentials") as credentials, \
                    mock.patch("myflames.connector._run_bounded") as run:
                with self.assertRaises(reviewed_writes.Refusal):
                    cluster_shell.ShellWriter(self.conn, SHELL).run_write(edited + "\nneverSend();")
                credentials.assert_not_called()
                run.assert_not_called()

    def test_target_validation_rejects_injected_options_and_malformed_endpoint(self):
        for target in ({"address": "spare:3310", "server_uuid": SPARE_UUID, "password": "injected"},
                       {"address": "spare:65536", "server_uuid": SPARE_UUID},
                       {"address": "spare:3310*/write();/*", "server_uuid": SPARE_UUID},
                       {"address": "spare:3310", "server_uuid": "not-a-uuid"}):
            with self.subTest(target=target):
                identity = dict(self.identity, target_instance=target)
                with self.assertRaises(ValueError):
                    cluster_shell.target_session_guard(identity)


class ConfigurationTransportTests(unittest.TestCase):
    def test_real_shell_writer_validates_raw_configuration_before_redacting_output_and_progress(self):
        for password in ("status", "ok", "MYFLAMES_CONFIG", "=", '"', "{", "escaped-quote'\\\"/\u00e9"):
            with self.subTest(password=password):
                conn = types.SimpleNamespace(host="spare", port=3306, user="admin", password=password)
                raw = ("MYFLAMES_CONFIG=" + json.dumps({"status": "ok", "detail": password}) + "\n").encode("utf-8")
                progress = reviewed_writes.Progress(1)

                def run(argv, timeout, **options):
                    for offset in range(0, len(raw), 3):
                        options["on_output"]("stdout", raw[offset:offset + 3])
                    return subprocess.CompletedProcess(argv, 0, raw, b"")

                with mock.patch("myflames.connector.checked_shell_program", return_value=SHELL["path"]), \
                        mock.patch("myflames.connector._run_bounded", side_effect=run):
                    writer = cluster_shell.ShellWriter(conn, SHELL, progress=progress)
                    output = writer.run_write('print("MYFLAMES_CONFIG=" + JSON.stringify(dba.checkInstanceConfiguration()));')
                self.assertIs(output.configuration_ok, True)
                self.assertIs(writer.stdout, output)
                self.assertNotIn(password, output)
                self.assertTrue(progress.snapshot()["output"])
                self.assertTrue(all(password not in item["text"] for item in progress.snapshot()["output"]))
                self.assertEqual(output.__dict__, {"diagnostic": None, "error_info": None, "configuration_ok": True})


if __name__ == "__main__":
    unittest.main()
