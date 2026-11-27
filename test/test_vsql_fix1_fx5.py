"""Regression coverage for the FX5 VillageSQL fixes and statement parity."""
import copy
import json
import os
import unittest
from unittest import mock

from myflames import extensions, health, locks, param_compare, readiness, schema_versions, watch
from myflames.perf_common import Server
from myflames.connector import MySQLConnection
from _perf_replay import ReplayConn, batch, expanded_preflight_sql
from test_health import FakeConn, FakeTime, IdentityHealthConn, fixture
from test_extensions import collect
from test_watch import _watch_conn
from test_readiness import Connection as ReadinessConnection
import _schema_replay as schema_replay
from test_vsql_fix2_fx8a import SchemaReplayConnection


class HealthCodebase(unittest.TestCase):
    def test_shared_replay_alias_accepts_only_the_exact_old_health_request(self):
        names = tuple(name for name in health.VARIABLES if name != "villagesql_server_version")
        original = health.HEALTH_TAG + ("SHOW GLOBAL VARIABLES WHERE Variable_name IN (%s)"
                                     % ", ".join("'%s'" % name for name in names))
        response = "version\t8.4.11\nversion_comment\tSource distribution\n"
        expanded = original[:-1] + ", 'villagesql_server_version')"
        self.assertEqual(expanded_preflight_sql(original, response), expanded)
        for sql, text in ((original.replace("health*/", "other*/"), response),
                          (original.replace("'version', ", ""), response),
                          (original + " LIMIT 1", response),
                          (original, response.replace("8.4.11", "8.4.11-villagesql-0.0.8"))):
            with self.subTest(sql=sql, response=text):
                self.assertEqual(expanded_preflight_sql(sql, text), sql)
        conn = ReplayConn([[original, response]])
        self.assertEqual(conn.run(expanded), response)
        self.assertEqual(conn.seen, {original: 1, expanded: 1})

    def test_codebase_comes_from_existing_variables_read(self):
        for base in ("mysql", "percona"):
            with self.subTest(base=base):
                data = copy.deepcopy(fixture("mysql-8.4"))
                data["variables"].update(version="8.4.11-villagesql-0.0.8",
                                         villagesql_server_version=base + "-8.4_0.0.8")
                clock = FakeTime()
                conn = FakeConn(data["variables"]["version"], data["variables"],
                                [sample["status"] for sample in data["samples"]], clock=clock)
                # The old code takes this path on real connectors. Fail if
                # Health attempts any separate identity probe.
                conn.query_rows = mock.Mock(side_effect=AssertionError("separate codebase probe"))
                report = health.health_report(conn, 1, sleep=clock.sleep, clock=clock.monotonic)
                self.assertEqual(report["server_base"], base)
                self.assertEqual(len(conn.calls), 3)
                self.assertIn("'villagesql_server_version'", conn.calls[0][0])
                self.assertTrue(all("SELECT @@villagesql_server_version" not in sql for sql, _ in conn.calls))
                conn.query_rows.assert_not_called()

    def test_identity_sampling_keeps_two_client_invocations(self):
        data = copy.deepcopy(fixture("mysql-8.4"))
        data["variables"].update(version="8.4.11-villagesql-0.0.8",
                                 villagesql_server_version="percona-8.4_0.0.8")
        clock = FakeTime()
        conn = IdentityHealthConn(data)
        conn.query_rows = mock.Mock(side_effect=AssertionError("separate codebase probe"))
        report = health.health_report(conn, 1, sleep=clock.sleep, clock=clock.monotonic, with_identity=True)
        self.assertEqual(report["server_base"], "percona")
        self.assertEqual(len(conn.calls), 2)
        self.assertIn("'villagesql_server_version'", conn.calls[0][0])
        conn.query_rows.assert_not_called()

    def test_old_health_captures_replay_with_only_the_optional_name_added(self):
        # Health fixtures contain captured variables/status, not SQL calls.
        # Keep the old request literal, and alias exactly the H1 addition
        # over those recorded answers. This is synthetic request evidence.
        original = (health.HEALTH_TAG + "SHOW GLOBAL VARIABLES WHERE Variable_name IN ("
                    "'version', 'version_comment', 'max_connections', 'table_open_cache', "
                    "'innodb_buffer_pool_size', 'innodb_page_size', 'innodb_max_dirty_pages_pct', "
                    "'innodb_max_dirty_pages_pct_lwm', 'slave_parallel_threads', "
                    "'innodb_redo_log_capacity', 'innodb_log_file_size', 'innodb_log_files_in_group', "
                    "'internal_tmp_mem_storage_engine', 'temptable_max_mmap', 'temptable_use_mmap', "
                    "'innodb_lru_scan_depth', 'innodb_buffer_pool_instances', 'table_open_cache_instances', "
                    "'table_definition_cache', 'thread_cache_size', 'thread_handling', 'log_bin', "
                    "'binlog_cache_size', 'binlog_stmt_cache_size', 'open_files_limit', 'sort_buffer_size', "
                    "'tmp_table_size', 'max_heap_table_size', 'temptable_max_ram', 'tmp_memory_table_size', "
                    "'tmp_disk_table_size')")
        expanded = original[:-1] + ", 'villagesql_server_version')"
        status_sql = health.HEALTH_TAG + "SHOW GLOBAL STATUS"
        for name in ("mysql-5.7", "mysql-8.4", "mysql-9.7", "mysql-26.7", "percona-8.0", "percona-8.4",
                     "mariadb-11.4", "mariadb-13.0", "readyset-synthetic"):
            with self.subTest(server=name):
                data = copy.deepcopy(fixture("mysql-8.4" if name == "readyset-synthetic" else name))
                if name == "readyset-synthetic":
                    data["variables"].update(version="8.0.0", version_comment="Readyset")
                self.assertNotIn("villagesql_server_version", data["variables"])
                calls = [[original, batch(data["variables"].items())]]
                expected = [expanded]
                if data["variables"].get("thread_handling") == "pool-of-threads":
                    pool = health.HEALTH_TAG + ("SHOW GLOBAL VARIABLES WHERE Variable_name IN "
                                               "('thread_pool_size', 'thread_pool_max_threads')")
                    calls.append([pool, batch((k, v) for k, v in data["variables"].items()
                                              if k in ("thread_pool_size", "thread_pool_max_threads"))])
                    expected.append(pool)
                calls.extend([status_sql, batch(sample["status"].items())] for sample in data["samples"])
                clock = FakeTime()
                conn = ReplayConn(calls, clock=clock)
                conn.aliases[expanded] = original
                conn.answers[expanded] = conn.answers[original]
                report = health.health_report(conn, 1, sleep=clock.sleep, clock=clock.monotonic)
                self.assertEqual([sql for sql, _ in conn.calls], expected + [status_sql, status_sql])
                self.assertNotIn("server_base", report)


class ExtensionBounds(unittest.TestCase):
    def test_inventory_and_registration_are_bounded_before_transfer(self):
        self.assertTrue(extensions.EXTENSIONS_SQL.endswith("ORDER BY EXTENSION_NAME LIMIT 1001"))
        self.assertEqual(extensions.REGISTRATION_SQL,
                         "SELECT EXTENSION_NAME, NEGOTIATED_PROTOCOL, "
                         "IF(LENGTH(REGISTRATION_JSON) > 262144, "
                         "'[myflames: registration exceeds 256 KB]', REGISTRATION_JSON) "
                         "FROM INFORMATION_SCHEMA.EXTENSION_REGISTRATION ORDER BY EXTENSION_NAME LIMIT 1001")

    def test_registration_sentinel_is_excluded_and_reported_incomplete(self):
        rows = [["ext%04d" % n, 5, '{"funcs":[],"types":[]}'] for n in range(1001)]
        report, _ = collect({extensions.EXTENSIONS_SQL: [["ext1000", "1", None, None, None, None]],
                             extensions.REGISTRATION_SQL: rows})
        self.assertIsNone(report["extensions"][0]["protocol"])
        self.assertIsNone(report["summary"]["deprecated_protocol"])
        self.assertIsNone(report["summary"]["unsupported_protocol"])
        self.assertTrue(any("registration" in note.lower() and "1000" in note and "unknown" in note
                            for note in report["notes"]))

    def test_oversized_registration_remains_unknown(self):
        raw = '{"padding":"' + "x" * extensions.MAX_REGISTRATION_BYTES
        report, _ = collect({extensions.REGISTRATION_SQL: [["vsql_complex", 5, raw[:262145]]]})
        item = report["extensions"][0]
        self.assertEqual(item["protocol"], 5)
        self.assertIsNone(item["functions"])
        self.assertIsNone(item["functions_truncated"])
        self.assertTrue(any("256 KB" in note for note in report["notes"]))


class ExtensionIdentity(unittest.TestCase):
    def test_failed_or_partial_settings_preserve_preflight_codebase(self):
        for variables, errors in (([], {}),
                                  ([], {extensions.VARIABLES_SQL: "ERROR 1142: SELECT denied"}),
                                  ([["veb_dir", "/extensions"]], {})):
            with self.subTest(variables=variables, errors=errors):
                identity = [["version", "8.4.11-villagesql-0.0.8"],
                            ["version_comment", "Source distribution"],
                            ["performance_schema", "ON"], ["villagesql_server_version", "percona-8.4_0.0.8"]]
                report, _ = collect({extensions.IDENTITY_SQL: identity, extensions.VARIABLES_SQL: variables}, errors)
                self.assertEqual(report["server_base"], "percona")
                self.assertEqual(report["settings"]["codebase"], "percona-8.4_0.0.8")

    def test_successful_settings_can_refresh_preflight_codebase(self):
        identity = [["version", "8.4.11-villagesql-0.0.8"], ["version_comment", "Source distribution"],
                    ["villagesql_server_version", "percona-8.4_0.0.8"]]
        report, _ = collect({extensions.IDENTITY_SQL: identity})
        self.assertEqual(report["server_base"], "mysql")
        self.assertEqual(report["settings"]["codebase"], "mysql-8.4_0.0.8")


class ExtensionCounters(unittest.TestCase):
    def test_install_counter_documents_update_requests_without_renaming(self):
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "docs", "schemas", "extensions-1.0.json")
        with open(path, encoding="utf-8") as handle:
            schema = json.load(handle)
        description = schema["properties"]["status"]["properties"]["install_count"].get("description", "")
        self.assertIn("INSTALL EXTENSION", description)
        self.assertIn("ALTER EXTENSION", description)
        self.assertIn("AT RESTART", description)
        report, _ = collect()
        self.assertEqual(set(report["status"]), {"veb_cache_repaired", "install_count", "uninstall_count"})


class WatchParity(unittest.TestCase):
    def test_mariadb_still_reads_version_comment_after_server_state(self):
        replay = _watch_conn(engine="mariadb", version="11.4.13-MariaDB", ps_on="0")

        class Connection(MySQLConnection):
            def __init__(self):
                super(Connection, self).__init__(host="replay.invalid", user="test")

            def run(self, sql, extra_flags=None, timeout=None):
                return replay.run(sql, extra_flags=extra_flags, timeout=timeout)

        comment_sql = "SELECT @@version_comment"
        replay.answers[comment_sql] = ["mariadb.org binary distribution\n"]
        report = watch.watch_report(Connection())
        self.assertEqual(report["engine"], "mariadb")
        self.assertEqual([sql for sql, _ in replay.calls[:2]],
                         [watch.WATCH_TAG + "SELECT @@version, @@performance_schema", comment_sql])
        self.assertEqual(len(replay.calls), 5)

    def test_other_products_keep_the_same_statement_sequence(self):
        for version, comment, flavor in (("8.4.11", "Source distribution", "mysql"),
                                          ("8.4.11-11", "Percona Server (GPL)", "percona"),
                                          ("8.0.0", "Readyset", "readyset")):
            with self.subTest(flavor=flavor):
                replay = _watch_conn(version=version, ps_on="0")
                original = list(replay.answers)
                comment_sql = "SELECT @@version_comment"
                replay.answers[comment_sql] = [comment + "\n"]

                class Connection(MySQLConnection):
                    def __init__(self):
                        super(Connection, self).__init__(host="replay.invalid", user="test")

                    def run(self, sql, extra_flags=None, timeout=None):
                        return replay.run(sql, extra_flags=extra_flags, timeout=timeout)

                report = watch.watch_report(Connection())
                self.assertEqual(report["flavor"], flavor)
                self.assertEqual([sql for sql, _ in replay.calls], original[:1] + [comment_sql] + original[1:])
                self.assertNotIn("server_base", report)


class IdentityEnvelopes(unittest.TestCase):
    def test_readiness_uses_existing_batch_for_base(self):
        conn = ReadinessConnection()
        conn.data["variables"].update(version="8.4.11-villagesql-0.0.8", version_comment="Source distribution",
                                      villagesql_server_version="percona-8.4_0.0.8")
        report = readiness.read_readiness(conn)
        self.assertEqual(report["flavor"], "villagesql")
        self.assertEqual(report.get("server_base"), "percona")
        self.assertEqual(len(conn.calls), 2)
        self.assertIn("'villagesql_server_version'", conn.calls[0][0])

    def test_readiness_changes_only_the_optional_name_in_its_existing_batch(self):
        original = ("/*myflames:readiness*/ SHOW GLOBAL VARIABLES WHERE Variable_name IN "
                    "('version','version_comment','read_only','super_read_only','performance_schema',"
                    "'partial_revokes','wsrep_on','group_replication_group_name');\n"
                    "/*myflames:readiness*/ SHOW GLOBAL STATUS WHERE Variable_name IN "
                    "('wsrep_cluster_status','wsrep_cluster_size');\n/*myflames:readiness*/ SHOW GRANTS;\n")
        expected = original.replace("'group_replication_group_name')",
                                    "'group_replication_group_name','villagesql_server_version')")
        for source, comment, flavor in (("mysql", "Source distribution", "mysql"),
                                        ("mysql", "Percona Server (GPL)", "percona"),
                                        ("mariadb", "MariaDB Server", "mariadb"),
                                        ("mysql", "Readyset", "readyset")):
            with self.subTest(flavor=flavor):
                conn = ReadinessConnection(source)
                conn.data["variables"]["version_comment"] = comment
                report = readiness.read_readiness(conn)
                self.assertEqual(report["flavor"], flavor)
                self.assertEqual(conn.calls[0][0], expected)
                self.assertEqual(conn.calls[1][0], readiness.TAG + (
                    "SHOW ALL SLAVES STATUS" if flavor == "mariadb" else
                    "SELECT 1 FROM performance_schema.replication_connection_configuration "
                    "WHERE CHANNEL_NAME NOT IN ('group_replication_applier','group_replication_recovery') LIMIT 1"))
                self.assertEqual(len(conn.calls), 2)
                self.assertNotIn("server_base", report)

    def test_locks_preserves_identity_base(self):
        state = locks._Report("mysql", "8.4.11-villagesql-0.0.8", (8, 4, 11), True, 500, "deadlock")
        state.identity = {"flavor": "villagesql", "server_product": "VillageSQL", "server_base": "percona"}
        state.server_identity = {}
        report = locks._assemble(state, None, False)
        self.assertEqual(report.get("server_base"), "percona")

    def test_parameter_capture_and_validation_preserve_base(self):
        config = {"engine": "mysql", "flavor": "villagesql", "server_product": "VillageSQL",
                  "server_base": "percona", "server_version": "8.4.11-villagesql-0.0.8",
                  "parameters": {"complete": True, "items": [], "notes": []}}
        with mock.patch("myflames.config_review.config_report", return_value=config):
            report = param_compare.capture(mock.Mock(host="db1", port=3306))
        self.assertEqual(report["server"].get("server_base"), "percona")
        self.assertEqual(param_compare.validate_snapshot(report)["server"].get("server_base"), "percona")

    def test_parameter_snapshot_rejects_invalid_base_metadata(self):
        from test_param_compare import snapshot
        report = snapshot()
        report["server"]["server_base"] = "unknown-server-controlled-text"
        with self.assertRaisesRegex(ValueError, "server base"):
            param_compare.validate_snapshot(report)

    def test_schema_capture_and_validation_preserve_base(self):
        record = schema_replay.recording("mysql-8.4", "before")
        server = Server({"version": "8.4.11-villagesql-0.0.8", "version_comment": "Source distribution",
                         "villagesql_server_version": "percona-8.4_0.0.8"}, {})
        # Reuse recorded schema reads and inject only their typed identity.
        # No policy, SQL classification or review-token checks are replaced.
        with mock.patch.object(schema_versions, "Server", return_value=server):
            report = schema_versions.capture(SchemaReplayConnection(record), record["database"], record["source"])
        self.assertEqual(report["server"].get("server_base"), "percona")
        self.assertEqual(schema_versions.validate_snapshot(report)["server"].get("server_base"), "percona")

    def test_schema_snapshot_rejects_invalid_base_metadata(self):
        report = schema_replay.snapshot("mysql-8.4", "before")
        report["server"]["server_base"] = "unknown-server-controlled-text"
        with self.assertRaisesRegex(ValueError, "server base"):
            schema_versions.validate_snapshot(report)


if __name__ == "__main__":
    unittest.main()
