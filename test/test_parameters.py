"""Bulk parameter inventory, optional reads and unchanged CLI config reads."""
import json
import unittest
from unittest import mock

from _perf_replay import FakeFactory, ReplayConn, assert_read_only, batch, replay
from myflames import config_review, mysql_sysvar_help, mysql_sysvars, parameters as P, ui
from myflames.connector import ConnectorError
from myflames.perf_common import Reader, Server


TAG = config_review.CONFIG_TAG


def items(report):
    return {item["name"]: item for item in report["items"]}


def mysql(rows, **kwargs):
    defaults = {"info_rows": [], "persisted_rows": []}
    defaults.update(kwargs)
    return P.compute_parameters("mysql", defaults.pop("version", (8, 4, 10)), rows=rows, **defaults)


class TestComputeParameters(unittest.TestCase):
    def test_mysql_84_sources_types_checks_and_persistence(self):
        rows = [["sort_buffer_size", "262144"], ["max_connections", "151"],
                ["log_bin", "ON"], ["innodb_buffer_pool_size", "134217728"],
                ["innodb_buffer_pool_instances", "8"], ["sql_log_bin", "ON"]]
        info = [["max_connections", "COMPILED", None, "1", "100000", None, None, None],
                ["sort_buffer_size", "DYNAMIC", None, "32768", "18446744073709551615",
                 "2026-10-18 10:00:00", "admin", "localhost"],
                ["log_bin", "COMMAND_LINE", None, "0", "0", None, None, None]]
        report = mysql(rows, info_rows=info, persisted_rows=[["sort_buffer_size"]],
                       checks=[{"key": "buffer_pool", "settings": [{"name": "innodb_buffer_pool_size"}]}])
        self.assertTrue(report["complete"])
        self.assertEqual(report["schema_version"], "parameters-1.0")
        self.assertEqual(report["notes"], [])
        found = items(report)
        self.assertEqual(list(found), sorted(found))
        self.assertNotIn("sql_log_bin", found)
        self.assertEqual((found["max_connections"]["scope"], found["max_connections"]["apply_type"],
                          found["max_connections"]["modifiable"]), ("global", "dynamic", "now"))
        self.assertEqual((found["max_connections"]["min"], found["max_connections"]["max"],
                          found["max_connections"]["default"]), (1, 100000, "151"))
        self.assertEqual((found["max_connections"]["source_label"], found["max_connections"]["modified"]),
                         ("default", False))
        self.assertEqual(found["sort_buffer_size"]["type"], "bytes")
        self.assertEqual(found["sort_buffer_size"]["max"], 18446744073709551615)
        self.assertEqual(found["sort_buffer_size"]["scope"], "both")
        self.assertTrue(found["sort_buffer_size"]["persisted"])
        self.assertFalse(found["max_connections"]["persisted"])
        self.assertEqual(found["sort_buffer_size"]["set_user"], "admin")
        self.assertEqual(found["sort_buffer_size"]["source_label"], "SET GLOBAL")
        self.assertTrue(found["sort_buffer_size"]["modified"])
        self.assertEqual((found["log_bin"]["value"], found["log_bin"]["values"]), ("ON", ["OFF", "ON"]))
        self.assertEqual((found["log_bin"]["apply_type"], found["log_bin"]["modifiable"]), ("static", "option_file"))
        self.assertEqual(found["innodb_buffer_pool_instances"]["modifiable"], "restart")
        self.assertEqual(found["innodb_buffer_pool_size"]["modifiable"], "now")
        self.assertEqual(found["innodb_buffer_pool_size"]["checks"], ["buffer_pool"])
        self.assertEqual(json.loads(json.dumps(report, allow_nan=False)), report)

    def test_mysql_9_metadata_unknown_names_and_description_cap(self):
        report = mysql([["plugin_limit", "5"], ["binlog_row_image", "FULL"], ["only_session", "1"]],
                       version=(9, 7, 0), metadata_rows=[
                           ["plugin_limit", "GLOBAL", "Integer", "1", "10", "d" * 601],
                           ["binlog_row_image", "SESSION", "Enumeration", "0", "0", "Images"],
                           ["only_session", "SESSION_ONLY", "Integer", "0", "1", None]])
        found = items(report)
        self.assertTrue(report["complete"])
        self.assertNotIn("only_session", found)
        self.assertEqual((found["plugin_limit"]["scope"], found["plugin_limit"]["type"],
                          found["plugin_limit"]["min"], found["plugin_limit"]["max"]),
                         ("global", "integer", 1, 10))
        self.assertIsNone(found["plugin_limit"]["apply_type"])
        self.assertIsNone(found["plugin_limit"]["modifiable"])
        self.assertEqual(len(found["plugin_limit"]["description"]), 600)
        self.assertEqual(found["binlog_row_image"]["values"], ["MINIMAL", "NOBLOB", "FULL"])
        self.assertEqual(found["binlog_row_image"]["apply_type"], "dynamic")

    def test_mysql_before_9_describes_from_the_source_help(self):
        rows = [["max_connections", "151"], ["expire_logs_days", "0"], ["innodb_dedicated_server", "OFF"],
                ["percona_only_setting", "1"]]
        found = items(mysql(rows))
        self.assertEqual(found["max_connections"]["description"], "The number of simultaneous clients allowed")
        self.assertEqual(found["innodb_dedicated_server"]["description"], mysql_sysvar_help.HELP["innodb_dedicated_server"])
        self.assertIsNone(found["expire_logs_days"]["description"], "8.4 declares no such variable")
        self.assertIsNone(found["percona_only_setting"]["description"])
        # MySQL 8.0 takes its own text where it differs, then the 8.4 one.
        eighty = items(mysql(rows, version=(8, 0, 46)))
        self.assertEqual(eighty["expire_logs_days"]["description"], mysql_sysvar_help.HELP_80["expire_logs_days"])
        self.assertEqual(eighty["innodb_dedicated_server"]["description"], mysql_sysvar_help.HELP_80["innodb_dedicated_server"])
        self.assertNotEqual(eighty["innodb_dedicated_server"]["description"], found["innodb_dedicated_server"]["description"])
        self.assertEqual(eighty["max_connections"]["description"], found["max_connections"]["description"])
        # MySQL 9.0+ reports its own text; none is borrowed from the source when it reports none.
        nine = items(mysql(rows, version=(9, 7, 0), metadata_rows=[["max_connections", "GLOBAL", "Integer", "1", "100000", None]]))
        self.assertIsNone(nine["max_connections"]["description"])
        self.assertEqual(P.source_help((9, 0, 1), "max_connections"), None)
        self.assertEqual(P.source_help((8, 3, 0), "max_connections"), "The number of simultaneous clients allowed")

    def test_mysql_server_computed_values_have_no_change_path(self):
        names = ("version", "hostname", "server_uuid", "protocol_version", "license", "gtid_executed",
                 "gtid_owned", "version_compile_machine", "version_compile_os", "version_compile_zlib",
                 "lower_case_file_system", "system_time_zone", "character_set_system", "innodb_version",
                 "have_compress", "have_dynamic_loading", "have_geometry", "have_profiling",
                 "have_query_cache", "have_rtree_keys", "have_statement_timeout", "have_symlink")
        report = mysql([[name, "value"] for name in names])
        found = items(report)
        self.assertEqual(len(found), len(names))
        for name in names:
            with self.subTest(name=name):
                self.assertEqual((found[name]["apply_type"], found[name]["modifiable"]), ("static", "none"))
        self.assertEqual(json.loads(json.dumps(report, allow_nan=False)), report)

    def test_mysql_separate_options_and_hand_map_keep_change_paths(self):
        names = ("core_file", "log_bin", "log_bin_index", "relay_log_index", "skip_external_locking",
                 "locked_in_memory")
        found = items(mysql([[name, "value"] for name in names]))
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(found[name]["modifiable"], "option_file")
        # The exception still permits restart when its flags allow persistence.
        # Handwritten metadata keeps precedence over the generated nc flag.
        with mock.patch.dict(mysql_sysvars.SYSVARS, {
                "log_bin_index": ("global", "string", "ro nc"),
                "innodb_buffer_pool_instances": ("global", "integer", "ro nc"),
                "max_connections": ("global", "integer", "ro np nc")}):
            found = items(mysql([[name, "1"] for name in (
                "log_bin_index", "innodb_buffer_pool_instances", "max_connections")]))
        self.assertEqual(found["log_bin_index"]["modifiable"], "restart")
        self.assertEqual(found["innodb_buffer_pool_instances"]["modifiable"], "restart")
        self.assertEqual(found["max_connections"]["modifiable"], "now")

    def test_mysql_nc_applies_only_to_known_static_rows(self):
        with mock.patch.dict(mysql_sysvars.SYSVARS, {
                "computed": ("global", "integer", "ro nc"),
                "runtime": ("global", "integer", "nc"),
                "conflicting": ("global", "integer", None)}):
            found = items(mysql([[name, "1"] for name in ("computed", "runtime", "conflicting")]))
        self.assertEqual(found["computed"]["modifiable"], "none")
        self.assertEqual(found["runtime"]["modifiable"], "now")
        self.assertIsNone(found["conflicting"]["apply_type"])
        self.assertIsNone(found["conflicting"]["modifiable"])

    def test_mysql_platform_dependent_large_pages_has_unknown_change_path(self):
        for version in ((8, 4, 10), (9, 7, 0)):
            with self.subTest(version=version):
                row = items(mysql([["large_pages", "ON"]], version=version, metadata_rows=[]))["large_pages"]
                self.assertEqual(row["scope"], "global")
                self.assertEqual(row["type"], "boolean")
                self.assertIsNone(row["apply_type"])
                self.assertIsNone(row["modifiable"])

    def test_mysql_previously_missed_declarations_have_known_mutability(self):
        expected = {"sql_require_primary_key": "now", "restrict_fk_on_non_standard_key": "now",
                    "innodb_native_foreign_keys": "restart", "enable_cascade_triggers": "now"}
        found = items(mysql([[name, "ON"] for name in expected], version=(9, 7, 0), metadata_rows=[]))
        self.assertEqual({name: row["modifiable"] for name, row in found.items()}, expected)

    def test_mariadb_catalog_defaults_block_size_aliases_and_null(self):
        rows = [
            ["SORT_BUFFER_SIZE", "2097152", "COMPILE-TIME", "2097152", "SESSION", "BIGINT UNSIGNED",
             "1024", "18446744073709551615", "1024", None, "NO", None, "Sort memory", "REQUIRED"],
            ["LOG_BIN", "OFF", "CONFIG", "OFF", "GLOBAL", "BOOLEAN", None, None, None,
             None, "YES", "/etc/my.cnf", "Binary log", None],
            ["LOG_SLOW_QUERY", "ON", "SQL", "OFF", "SESSION", "BOOLEAN", None, None, None,
             None, "NO", None, "Slow log", "OPTIONAL"],
            ["NULL_TEXT", None, None, None, "GLOBAL", "VARCHAR", None, None, None, None, "YES", None, None, None],
            ["CHOICE", "A", "COMPILE-TIME", "A", "GLOBAL", "ENUM", None, None, None, "A,B", "NO", None, None,
             "REQUIRED"],
            ["ONLY_SESSION", "1", "SQL", "1", "SESSION ONLY", "INT", "0", "10", "1", None, "NO", None, None, None],
        ]
        report = P.compute_parameters("mariadb", (11, 4, 13), catalog_rows=rows, checks=[
            {"key": "slow_log", "settings": [{"name": "log_slow_query", "alias": "slow_query_log"}]},
            {"key": "other", "settings": [{"name": "slow_query_log", "alias": "log_slow_query"}]}])
        found = items(report)
        self.assertTrue(report["complete"])
        self.assertNotIn("only_session", found)
        self.assertEqual((found["sort_buffer_size"]["scope"], found["sort_buffer_size"]["type"],
                          found["sort_buffer_size"]["block_size"]), ("both", "bytes", 1024))
        self.assertFalse(found["sort_buffer_size"]["modified"])
        self.assertEqual(found["log_bin"]["modifiable"], "option_file")
        self.assertEqual(found["log_bin"]["source_path"], "/etc/my.cnf")
        self.assertEqual(found["log_bin"]["source_label"], "my.cnf")
        self.assertEqual(found["log_slow_query"]["checks"], ["other", "slow_log"])
        self.assertIsNone(found["log_slow_query"]["persisted"])
        self.assertEqual(found["log_slow_query"]["default"], "OFF")
        self.assertIsNone(found["null_text"]["value"])
        self.assertIsNone(found["null_text"]["modified"])
        self.assertEqual(found["choice"]["values"], ["A", "B"])

    def test_mariadb_server_computed_and_separate_startup_options(self):
        options = ("log_bin", "log_bin_index", "relay_log_index", "skip_external_locking", "locked_in_memory")
        rows = [[name.upper(), "value", "COMPILE-TIME", None, "GLOBAL", "VARCHAR", None, None, None,
                 None, "YES", None, "description", None] for name in options + ("hostname",)]
        rows += [["FLAG_OPTION", "ON", "COMPILE-TIME", "ON", "GLOBAL", "BOOLEAN", None, None, None,
                  None, "YES", None, None, "NONE"],
                 ["PORT", "3306", "COMPILE-TIME", "3306", "GLOBAL", "INT", "0", "65535", "1",
                  None, "YES", None, None, "REQUIRED"],
                 ["CORE_FILE", "OFF", "COMPILE-TIME", "OFF", "GLOBAL", "BOOLEAN", None, None, None,
                  None, "YES", None, "Write a core file on crash", "OPTIONAL"],
                 ["DYNAMIC", "ON", "SQL", "OFF", "GLOBAL", "BOOLEAN", None, None, None,
                  None, "NO", None, None, None]]
        found = items(P.compute_parameters("mariadb", (11, 4, 13), catalog_rows=rows))
        self.assertEqual(found["hostname"]["modifiable"], "none")
        self.assertEqual(found["hostname"]["apply_type"], "static")
        for name in options + ("flag_option", "port", "core_file"):
            with self.subTest(name=name):
                self.assertEqual(found[name]["modifiable"], "option_file")
        # The string NONE means a real option that takes no argument, not SQL NULL.
        self.assertEqual(found["dynamic"]["modifiable"], "now")

    def test_mariadb_auto_and_compile_time_are_not_modified(self):
        expected = {"COMPILE-TIME": False, "AUTO": False, "auto": False, "SQL": True,
                    "CONFIG": True, "COMMAND-LINE": True, "ENVIRONMENT": True, None: None}
        for origin, modified in expected.items():
            for read_only in ("YES", "NO"):
                with self.subTest(origin=origin, read_only=read_only):
                    rows = [["SIZED", "512", origin, "0", "GLOBAL", "INT", "0", "1024", "1",
                             None, read_only, None, None, "OPTIONAL"]]
                    row = items(P.compute_parameters("mariadb", (11, 4, 13), catalog_rows=rows))["sized"]
                    self.assertIs(row["modified"], modified)
                    self.assertEqual(row["source"], origin)

    def test_unknown_truncated_and_conflicting_facts(self):
        report = mysql([["unknown_variable", "z" * 4097], ["unknown_null", None],
                        ["unknown_empty", ""], ["exact_limit", "z" * 4096],
                        ["caching_sha2_password_digest_rounds", "5000"]])
        found = items(report)
        unknown = found["unknown_variable"]
        for key in ("scope", "apply_type", "modifiable", "type", "values", "min", "max", "block_size",
                    "default", "source", "modified", "description"):
            self.assertIsNone(unknown[key], key)
        self.assertTrue(unknown["truncated"])
        self.assertEqual(len(unknown["value"]), 4096)
        self.assertFalse(found["exact_limit"]["truncated"])
        self.assertFalse(found["unknown_null"]["truncated"])
        self.assertEqual(found["unknown_empty"]["value"], "")
        self.assertIsNone(found["caching_sha2_password_digest_rounds"]["apply_type"])
        self.assertIsNone(found["caching_sha2_password_digest_rounds"]["modifiable"])


class TestParameterReads(unittest.TestCase):
    def test_mariadb_reads_command_line_argument_and_auto_origin(self):
        self.assertIn("VARIABLE_COMMENT, COMMAND_LINE_ARGUMENT FROM", P.MARIADB_SQL)
        conn = ReplayConn([[TAG + P.MARIADB_SQL, batch([
            ["HOSTNAME", "db-host", "COMPILE-TIME", None, "GLOBAL", "VARCHAR", None, None, None,
             None, "YES", None, "Server host", None],
            ["SIZED", "512", "AUTO", "0", "GLOBAL", "INT", "0", "1024", "1",
             None, "NO", None, "Sized by server", "OPTIONAL"],
        ])]])
        report = P.read_parameters(Reader(conn, TAG), Server({"version": "11.4.13-MariaDB"}, {}), [], [])
        self.assertTrue(report["complete"])
        self.assertEqual(items(report)["hostname"]["modifiable"], "none")
        self.assertEqual(items(report)["hostname"]["description"], "Server host")
        self.assertFalse(items(report)["sized"]["modified"])
        self.assertEqual(items(report)["sized"]["modifiable"], "now")
        assert_read_only(self, conn, TAG)

    def test_denied_info_retains_items_and_reports_incomplete(self):
        conn = ReplayConn([
            [TAG + "SHOW GLOBAL VARIABLES", batch([["max_connections", "151"], ["text", "a\n\t\\n"]])],
            [TAG + P.MYSQL_INFO_SQL, None, "ERROR 1142: SELECT denied for variables_info"],
            [TAG + P.MYSQL_PERSISTED_SQL, ""],
        ])
        notes = ["existing config note"]
        report = P.read_parameters(Reader(conn, TAG), Server({"version": "8.4.10"}, {}), notes, [])
        self.assertFalse(report["complete"])
        self.assertEqual(len(report["items"]), 2)
        self.assertEqual(items(report)["text"]["value"], "a\n\t\\n")
        self.assertEqual(len(report["notes"]), 1)
        self.assertIn("variables_info", report["notes"][0])
        self.assertEqual(notes[1:], report["notes"])
        self.assertIsNone(items(report)["max_connections"]["source"])
        assert_read_only(self, conn, TAG)

    def test_null_looking_values_are_read_again_exactly(self):
        # Batch output prints SQL NULL and the text NULL alike.
        exact = P.MYSQL_EXACT_SQL % "'character_set_results', 'init_connect'"
        conn = ReplayConn([
            [TAG + "SHOW GLOBAL VARIABLES", batch([["init_connect", None], ["character_set_results", None],
                                                     ["max_connections", "151"]])],
            [TAG + P.MYSQL_INFO_SQL, ""],
            [TAG + P.MYSQL_PERSISTED_SQL, ""],
            [TAG + exact, batch([["character_set_results", "1", None], ["init_connect", "0", "4E554C4C"]])],
        ])
        report = P.read_parameters(Reader(conn, TAG), Server({"version": "8.4.10"}, {}), [], [])
        self.assertEqual(items(report)["init_connect"]["value"], "NULL")
        self.assertIsNone(items(report)["character_set_results"]["value"])
        self.assertTrue(report["complete"])
        assert_read_only(self, conn, TAG)

    def test_failed_exact_read_keeps_unknown_null_with_a_note(self):
        exact = P.MYSQL_EXACT_SQL % "'init_connect'"
        conn = ReplayConn([
            [TAG + "SHOW GLOBAL VARIABLES", batch([["init_connect", None]])],
            [TAG + P.MYSQL_INFO_SQL, ""],
            [TAG + P.MYSQL_PERSISTED_SQL, ""],
            [TAG + exact, None, "ERROR 1142: SELECT command denied for global_variables"],
        ])
        report = P.read_parameters(Reader(conn, TAG), Server({"version": "8.4.10"}, {}), [], [])
        self.assertIsNone(items(report)["init_connect"]["value"])
        self.assertTrue(any("values shown as NULL" in n for n in report["notes"]))
        # SQL NULL and the text NULL are still indistinguishable: the inventory says so.
        self.assertFalse(report["complete"])

    def test_denied_persisted_is_unknown_not_absent(self):
        report = mysql([["max_connections", "151"]], persisted_rows=None)
        self.assertFalse(report["complete"])
        self.assertIsNone(items(report)["max_connections"]["persisted"])

    def test_denied_global_or_mariadb_catalog_has_no_items(self):
        for engine, sql, version in (("mysql", "SHOW GLOBAL VARIABLES", "8.4.10"),
                                     ("mariadb", P.MARIADB_SQL, "11.4.13-MariaDB")):
            with self.subTest(engine=engine):
                conn = ReplayConn([], error=ConnectorError("ERROR 1142: SELECT denied"))
                report = P.read_parameters(Reader(conn, TAG), Server({"version": version}, {}), [], [])
                self.assertFalse(report["complete"])
                self.assertEqual(report["items"], [])
                self.assertTrue(report["notes"])
                self.assertEqual(conn.calls[0][0], TAG + sql)

    def test_metadata_version_gate_and_read_failure(self):
        calls = [[TAG + "SHOW GLOBAL VARIABLES", "max_connections\t151\n"],
                 [TAG + P.MYSQL_INFO_SQL, ""], [TAG + P.MYSQL_PERSISTED_SQL, ""],
                 [TAG + P.MYSQL_METADATA_SQL, None, "ERROR 1146: Table variables_metadata does not exist"]]
        for version, count, complete in (("8.4.10", 3, True), ("9.0.0", 4, False)):
            with self.subTest(version=version):
                conn = ReplayConn(calls)
                report = P.read_parameters(Reader(conn, TAG), Server({"version": version}, {}), [], [])
                self.assertEqual(report["complete"], complete)
                self.assertEqual(len(conn.calls), count)
                self.assertEqual(len(report["items"]), 1)

    def test_connection_failure_is_not_swallowed(self):
        conn = ReplayConn([], error=ConnectorError("ERROR 2013: Lost connection"))
        with self.assertRaises(ConnectorError):
            P.read_parameters(Reader(conn, TAG), Server({"version": "8.4.10"}, {}), [], [])

    def test_config_default_preserves_existing_reads_and_report(self):
        for name in ("mysql-8.4", "mysql-9.7", "mariadb-11.4"):
            with self.subTest(name=name):
                conn, recording = replay("config", name)
                report = config_review.config_report(conn)
                self.assertNotIn("parameters", report)
                self.assertEqual([sql for sql, _ in conn.calls], [call[0] for call in recording["calls"]])

    def test_config_opt_in_reads_after_existing_checks(self):
        _, recording = replay("config", "mysql-8.4")
        extra = [[TAG + "SHOW GLOBAL VARIABLES", "innodb_buffer_pool_size\t134217728\n"],
                 [TAG + P.MYSQL_INFO_SQL, ""], [TAG + P.MYSQL_PERSISTED_SQL, ""]]
        conn = ReplayConn(recording["calls"] + extra)
        report = config_review.config_report(conn, parameters=True)
        self.assertTrue(report["parameters"]["complete"])
        self.assertEqual(items(report["parameters"])["innodb_buffer_pool_size"]["checks"], ["buffer_pool"])
        self.assertEqual([sql for sql, _ in conn.calls[-3:]], [call[0] for call in extra])

    def test_workspace_requests_parameters(self):
        conn = ReplayConn([])
        with mock.patch("myflames.connector.MySQLConnection", FakeFactory(conn)), \
                mock.patch("myflames.config_review.config_report", return_value={}) as report:
            self.assertEqual(ui.api_response("/api/live/config", {"connection": {"host": "localhost"}},
                                             workspace=object()), {})
        report.assert_called_once_with(conn, parameters=True)
