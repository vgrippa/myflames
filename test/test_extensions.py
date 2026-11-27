"""VillageSQL extension reads, replayed codebases and hostile projections.

MySQL, Percona, MariaDB and Readyset cannot expose these VillageSQL views;
their cases assert that the feature gate sends only generic identity reads.
"""
import copy
from email.message import Message
import io
import json
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from _perf_replay import FakeFactory, ReplayConn, assert_read_only, batch, fixture
from myflames import dbops, extensions as m, ui
from myflames.connector import ConnectorError, unescape_mysql
from myflames.workspace_store import WorkspaceStore
from test_chart_data import validate as _validate_schema

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(REPO, "docs", "schemas", "extensions-1.0.json"), encoding="utf-8") as handle:
    SCHEMA = json.load(handle)


def validate(value, schema):
    """Use the shared validator and enforce this report's string size limits."""
    _validate_schema(value, schema)

    def bounds(item, rule, path):
        if "$ref" in rule:
            target = schema
            for part in rule["$ref"].lstrip("#/").split("/"):
                target = target[part]
            bounds(item, target, path)
        if isinstance(item, str) and "maxLength" in rule:
            assert len(item) <= rule["maxLength"], "%s exceeds maxLength %d" % (path, rule["maxLength"])
        if isinstance(item, dict):
            for key, child in item.items():
                if key in rule.get("properties", {}):
                    bounds(child, rule["properties"][key], path + "." + key)
        if isinstance(item, list) and "items" in rule:
            for index, child in enumerate(item):
                bounds(child, rule["items"], "%s[%d]" % (path, index))

    bounds(value, schema, "$")


class Connection(object):
    """Fixed read responses without mocking SQL classification or policy."""

    password = "fixture-password-not-real"

    def __init__(self, rows=None, errors=None, version="8.4.11-villagesql-0.0.8", comment="Source distribution"):
        self.calls = []
        self.errors = errors or {}
        self.rows = {
            m.IDENTITY_SQL: [["version", version], ["version_comment", comment],
                             ["performance_schema", "ON"], ["userstat", "OFF"]],
            m.UPTIME_SQL: [["Uptime", "123"]],
            m.EXTENSIONS_SQL: [["vsql_complex", "0.0.3", None, None, None, None]],
            m.REGISTRATION_SQL: [["vsql_complex", "5", json.dumps({"sdk_version": "0.0.5", "requested_protocol": 5,
                                   "extension_name": "", "extension_version": "",
                                   "funcs": [{"name": "complex_add"}, {"name": "complex_abs"}],
                                   "types": [{"name": "COMPLEX"}]})]],
            m.VARIABLES_SQL: [["veb_dir", "/usr/lib/veb/"], ["vsql_allow_preview_extensions", "OFF"],
                              ["villagesql_vef_server_protocol", "6"], ["villagesql_schema_version", "2"],
                              ["villagesql_server_version", "mysql-8.4_0.0.8"],
                              ["villagesql_build_info", '{"git_sha":"abc123","is_dirty":false,"build_host":"not-exported"}'],
                              ["vsql_complex.mode", "cartesian"], ["unknown.mode", "unowned"]],
            m.STATUS_SQL: [["Villagesql_veb_cache_repaired", "0"], ["Com_install_extension", "1"],
                           ["Com_uninstall_extension", "0"], ["vsql_complex.ext_hits", "27"]],
            m.COLUMNS_SQL: [["testdb", "complexes", "v", "vsql_complex.COMPLEX"]],
            m.ROUTINES_SQL: [["testdb", "complex_return", "FUNCTION", None, "vsql_complex.COMPLEX"]],
        }
        self.rows.update(rows or {})

    def run(self, sql, extra_flags=None, timeout=None):
        self.calls.append((sql, list(extra_flags or [])))
        if not sql.startswith(m.EXTENSIONS_TAG):
            raise AssertionError("Missing observation tag")
        query = sql[len(m.EXTENSIONS_TAG):]
        if query in self.errors:
            raise ConnectorError(self.errors[query])
        if query not in self.rows:
            raise AssertionError("Unexpected statement: " + query)
        return batch(self.rows[query])

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def collect(rows=None, errors=None, **kwargs):
    conn = Connection(rows, errors, **kwargs)
    report = m.extensions_report(conn)
    validate(report, SCHEMA)
    json.dumps(report, allow_nan=False)
    return report, conn



class ExtensionsInventory(unittest.TestCase):
    def test_inventory_projects_only_registered_names_and_known_build_fields(self):
        report, conn = collect()
        self.assertTrue(report["available"])
        self.assertIsNone(report["reason"])
        self.assertEqual(report["flavor"], "villagesql")
        self.assertEqual(report["server_product"], "VillageSQL")
        self.assertEqual(report["server_base"], "mysql")
        self.assertEqual(report["uptime_s"], 123)
        self.assertEqual(report["settings"], {"veb_dir": "/usr/lib/veb/", "preview_extensions": False,
                         "server_protocol": 6, "schema_version": "2", "codebase": "mysql-8.4_0.0.8",
                         "build": {"git_sha": "abc123", "is_dirty": False}})
        self.assertEqual(report["status"], {"veb_cache_repaired": 0, "install_count": 1, "uninstall_count": 0})
        self.assertEqual(report["summary"], {"installed": 1, "pending": 0, "failing": 0,
                                           "deprecated_protocol": 0, "unsupported_protocol": 0})
        item = report["extensions"][0]
        self.assertEqual((item["name"], item["version"]), ("vsql_complex", "0.0.3"))
        self.assertEqual(item["functions"], ["complex_add", "complex_abs"])
        self.assertEqual(item["types"], ["COMPLEX"])
        self.assertEqual(item["sdk_version"], "0.0.5")
        self.assertEqual(item["columns"], {"count": 1, "truncated": False,
                         "sample": [{"schema": "testdb", "table": "complexes", "column": "v", "data_type": "vsql_complex.COMPLEX"}]})
        self.assertEqual(item["routines"]["count"], 1)
        self.assertIsNone(item["routines"]["sample"][0]["parameter"])
        self.assertEqual(item["variables"], [{"name": "vsql_complex.mode", "value": "cartesian", "kind": "system"},
                                             {"name": "vsql_complex.ext_hits", "value": "27", "kind": "status"}])
        self.assertEqual(report["other_variables"], [{"name": "unknown.mode", "value": "unowned", "kind": "system"}])
        self.assertNotIn("REGISTRATION_JSON", json.dumps(report))
        self.assertNotIn("not-exported", json.dumps(report))
        self.assertEqual(len(conn.calls), 8, "No extra codebase probe or convenience query")
        assert_read_only(self, conn, m.EXTENSIONS_TAG)
        for sql, _ in conn.calls:
            self.assertEqual(dbops.classify(sql)["kind"], "read", sql)

    def test_non_villagesql_gate_sends_no_extension_statement(self):
        for version, comment, flavor in (("8.4.11", "Source distribution", "mysql"),
                                          ("8.4.11-11", "Percona Server (GPL)", "percona"),
                                          ("11.4.13-MariaDB", "MariaDB Server", "mariadb"),
                                          ("8.0.0", "Readyset", "readyset")):
            with self.subTest(flavor=flavor):
                report, conn = collect(version=version, comment=comment)
                self.assertFalse(report["available"])
                self.assertIn("only on VillageSQL", report["reason"])
                self.assertEqual(report["flavor"], flavor)
                self.assertEqual([sql[len(m.EXTENSIONS_TAG):] for sql, _ in conn.calls], [m.IDENTITY_SQL, m.UPTIME_SQL])
                # Filtered SHOW omits unknown variable names on other servers.
                # A direct system-variable read would instead fail with 1193.
                self.assertNotIn("@@villagesql_", " ".join(sql for sql, _ in conn.calls))
                self.assertIsNone(report["extensions"])
                self.assertTrue(all(value is None for value in report["summary"].values()))

    def test_source_distribution_comment_still_allows_percona_codebase(self):
        conn = Connection()
        for row in conn.rows[m.VARIABLES_SQL]:
            if row[0] == "villagesql_server_version":
                row[1] = "percona-8.4_0.0.8"
        report = m.extensions_report(conn)
        self.assertEqual(report["server_base"], "percona")

    def test_successful_empty_reads_are_zero_and_not_a_failure(self):
        report, _ = collect({m.EXTENSIONS_SQL: [], m.REGISTRATION_SQL: [], m.COLUMNS_SQL: [], m.ROUTINES_SQL: []})
        self.assertEqual(report["extensions"], [])
        self.assertEqual(report["summary"], {"installed": 0, "pending": 0, "failing": 0,
                                           "deprecated_protocol": 0, "unsupported_protocol": 0})
        self.assertEqual(len(report["other_variables"]), 3)

    def test_routine_dependencies_count_parameters_and_return_rows(self):
        # INFORMATION_SCHEMA.PARAMETERS has one row per argument and a NULL
        # PARAMETER_NAME row for a function return, not one row per routine.
        report, _ = collect({m.ROUTINES_SQL: [
            ["testdb", "complex_add", "FUNCTION", None, "vsql_complex.COMPLEX"],
            ["testdb", "complex_add", "FUNCTION", "left_value", "vsql_complex.COMPLEX"],
            ["testdb", "complex_add", "FUNCTION", "right_value", "vsql_complex.COMPLEX"],
        ]})
        usage = report["extensions"][0]["routines"]
        self.assertEqual(usage["count"], 3)
        self.assertEqual([row["parameter"] for row in usage["sample"]], [None, "left_value", "right_value"])
        self.assertTrue(all(row["routine"] == "complex_add" for row in usage["sample"]))
        self.assertFalse(usage["truncated"])

    def test_pending_and_failing_are_independent_counts(self):
        rows = [["a", "1", "2", "2026-11-27", None, None],
                ["b", "1", "3", "2026-11-26", "Could not load", "2026-11-27"],
                ["c", "1", None, None, None, None]]
        report, _ = collect({m.EXTENSIONS_SQL: rows})
        self.assertEqual(report["summary"]["installed"], 3)
        self.assertEqual(report["summary"]["pending"], 2)
        self.assertEqual(report["summary"]["failing"], 1)
        self.assertEqual(report["extensions"][1]["pending"]["last_error"], "Could not load")
        self.assertFalse(report["extensions"][1]["pending"]["error_truncated"])

    def test_protocol_state_uses_negotiated_protocol_not_requested_protocol(self):
        for value, expected in ((1, "deprecated"), (3, "deprecated"), (4, "unsupported"),
                                (5, "current"), (6, "current"), (7, "current"),
                                (0, "unknown"), (2, "unknown"), (None, "unknown"), ("invalid", "unknown")):
            with self.subTest(protocol=value):
                report, _ = collect({m.REGISTRATION_SQL: [["vsql_complex", value, '{"requested_protocol":4,"funcs":[],"types":[]}']]})
                item = report["extensions"][0]
                self.assertEqual(item["protocol_state"], expected)
                self.assertEqual(item["protocol"], value if isinstance(value, int) else None)
                if value == 4:
                    self.assertIn("0.0.8 refuses protocol 4", item["protocol_note"])
                if value in (None, "invalid"):
                    self.assertIsNone(report["summary"]["deprecated_protocol"])
                    self.assertIsNone(report["summary"]["unsupported_protocol"])
                else:
                    self.assertEqual(report["summary"]["deprecated_protocol"], int(expected == "deprecated"))
                    self.assertEqual(report["summary"]["unsupported_protocol"], int(expected == "unsupported"))

    def test_undefined_settings_counters_and_registration_fields_stay_null(self):
        report, _ = collect({m.VARIABLES_SQL: [], m.STATUS_SQL: [],
                             m.REGISTRATION_SQL: [["vsql_complex", None, "{}"]], m.UPTIME_SQL: []})
        self.assertTrue(all(value is None for value in report["settings"].values()))
        self.assertTrue(all(value is None for value in report["status"].values()))
        self.assertIsNone(report["uptime_s"])
        self.assertIsNone(report["server_base"])
        item = report["extensions"][0]
        for key in ("protocol", "sdk_version", "functions", "types", "functions_truncated", "types_truncated"):
            self.assertIsNone(item[key], key)
        self.assertEqual(item["variables"], [])

    def test_invalid_numbers_and_booleans_are_unknown(self):
        report, _ = collect({m.VARIABLES_SQL: [["villagesql_vef_server_protocol", "NaN"],
                                             ["vsql_allow_preview_extensions", "perhaps"]],
                             m.STATUS_SQL: [["Com_install_extension", "-1"], ["Com_uninstall_extension", "1.5"],
                                            ["Villagesql_veb_cache_repaired", "18446744073709551616"]]})
        self.assertIsNone(report["settings"]["server_protocol"])
        self.assertIsNone(report["settings"]["preview_extensions"])
        self.assertTrue(all(value is None for value in report["status"].values()))


class ExtensionsIsolation(unittest.TestCase):
    def test_known_extension_secrets_are_redacted_for_owned_and_unowned_variables(self):
        for installed in (True, False):
            with self.subTest(installed=installed):
                inventory = [["vsql_rest", "1", None, None, None, None]] if installed else []
                report, _ = collect({m.EXTENSIONS_SQL: inventory,
                                     m.VARIABLES_SQL: [["VSQL_REST.JWT_SECRET", "jwt-value-not-for-export"],
                                                       ["telemetry.otel_exporter_otlp_logs_headers", "Authorization=shared-secret"],
                                                       ["vsql_rest.secret_count", "42"],
                                                       ["vsql_rest.password_policy", "strict"]],
                                     m.STATUS_SQL: []})
                encoded = json.dumps(report)
                self.assertNotIn("jwt-value-not-for-export", encoded)
                self.assertNotIn("shared-secret", encoded)
                values = report["other_variables"] + (report["extensions"][0]["variables"] if installed else [])
                by_name = {value["name"]: value["value"] for value in values}
                self.assertEqual(by_name["VSQL_REST.JWT_SECRET"], "[redacted]")
                self.assertEqual(by_name["telemetry.otel_exporter_otlp_logs_headers"], "[redacted]")
                self.assertEqual(by_name["vsql_rest.secret_count"], "42", "No fragment-based secret heuristic")
                self.assertEqual(by_name["vsql_rest.password_policy"], "strict")
                self.assertEqual(report["notes"].count("Sensitive extension variable values are redacted."), 1)

    def test_null_sensitive_values_remain_unknown_and_empty_values_are_redacted(self):
        report, _ = collect({m.EXTENSIONS_SQL: [],
                             m.VARIABLES_SQL: [["vsql_rest.jwt_secret", None]],
                             m.STATUS_SQL: [["vsql_rest.jwt_secret", ""]]})
        self.assertEqual([row["value"] for row in report["other_variables"]], [None, "[redacted]"])

    def test_variable_values_redact_connection_passwords_and_sql_credentials_before_truncation(self):
        report, _ = collect({m.VARIABLES_SQL: [
            ["vsql_complex.message", "prefix " + Connection.password + " suffix"],
            ["vsql_complex.sql", "CREATE USER app IDENTIFIED BY 'sql-credential'"],
            ["unknown.tail", "x" * 1995 + Connection.password],
        ], m.STATUS_SQL: []})
        encoded = json.dumps(report)
        self.assertNotIn(Connection.password, encoded)
        self.assertNotIn("sql-credential", encoded)
        values = report["extensions"][0]["variables"]
        self.assertEqual(values[0]["value"], "prefix [redacted] suffix")
        self.assertIn("[redacted]", values[1]["value"])
        # Redact before bounding: cutting first would leak a password prefix.
        self.assertNotIn("fixtu", report["other_variables"][0]["value"])

    def test_pending_errors_redact_credentials_before_the_display_limit(self):
        error = "Connection " + Connection.password + ": CREATE USER app IDENTIFIED BY 'pending-credential'"
        report, _ = collect({m.EXTENSIONS_SQL: [["vsql_complex", "1", "2", None, error, None]]})
        encoded = json.dumps(report)
        self.assertNotIn(Connection.password, encoded)
        self.assertNotIn("pending-credential", encoded)
        pending = report["extensions"][0]["pending"]
        self.assertEqual(pending["last_error"].count("[redacted]"), 2)
        self.assertFalse(pending["error_truncated"])
        self.assertNotIn("Sensitive extension variable values are redacted.", report["notes"])

    def test_each_optional_read_failure_preserves_the_other_parts(self):
        cases = ((m.EXTENSIONS_SQL, "inventory"), (m.REGISTRATION_SQL, "registration"),
                 (m.VARIABLES_SQL, "variables"), (m.STATUS_SQL, "status"),
                 (m.COLUMNS_SQL, "columns"), (m.ROUTINES_SQL, "routines"), (m.UPTIME_SQL, "uptime"))
        for sql, part in cases:
            for message in ("ERROR 1142: SELECT command denied", "ERROR 2013: Lost connection", "query timed out"):
                with self.subTest(part=part, error=message):
                    report, conn = collect(errors={sql: message})
                    self.assertTrue(report["available"])
                    self.assertEqual(len(conn.calls), 8)
                    self.assertTrue(any(message in note for note in report["notes"]))
                    if part == "inventory":
                        self.assertIsNone(report["extensions"])
                        self.assertIsNone(report["summary"]["installed"])
                        self.assertEqual(report["settings"]["server_protocol"], 6)
                        continue
                    item = report["extensions"][0]
                    self.assertEqual(report["summary"]["installed"], 1)
                    if part == "registration":
                        self.assertIsNone(item["protocol"])
                        self.assertIsNone(item["functions"])
                        self.assertIsNone(report["summary"]["deprecated_protocol"])
                    elif part == "variables":
                        self.assertIsNone(report["settings"]["preview_extensions"])
                        self.assertIsNone(item["variables"])
                        self.assertEqual(report["status"]["install_count"], 1)
                    elif part == "status":
                        self.assertIsNone(report["status"]["install_count"])
                        self.assertIsNone(item["variables"])
                        self.assertEqual(report["settings"]["server_protocol"], 6)
                    elif part in ("columns", "routines"):
                        self.assertEqual(item[part], {"count": None, "sample": [], "truncated": None})
                    else:
                        self.assertIsNone(report["uptime_s"])

    def test_identity_failure_does_not_guess_villagesql(self):
        report, conn = collect(errors={m.IDENTITY_SQL: "ERROR 1045: Access denied"})
        self.assertFalse(report["available"])
        self.assertIn("Could not identify", report["reason"])
        self.assertEqual(len(conn.calls), 2)
        self.assertIsNone(report["server_version"])
        self.assertIsNone(report["performance_schema"])

    def test_caught_errors_cannot_leak_password_or_terminal_controls(self):
        report, _ = collect(errors={m.REGISTRATION_SQL: "ERROR 1142: denied " + Connection.password + "\x1b[31m" + "x" * 5000})
        encoded = json.dumps(report)
        self.assertNotIn(Connection.password, encoded)
        self.assertNotIn("\x1b", "".join(report["notes"]))
        self.assertLess(len(report["notes"][0]), 2100)


class ExtensionsBounds(unittest.TestCase):
    def test_hostile_server_version_obeys_the_report_text_bound(self):
        report, _ = collect(version="8.4.11-villagesql-0.0.8\x1b" + "x" * 5000)
        self.assertEqual(report["flavor"], "villagesql")
        self.assertLessEqual(len(report["server_version"]), 2000)
        self.assertNotIn("\x1b", report["server_version"])

    def test_global_dependency_sentinels_never_become_false_exact_counts(self):
        columns = [["testdb", "t%03d" % index, "v", "vsql_complex.COMPLEX"] for index in range(501)]
        routines = [["testdb", "f%03d" % index, "FUNCTION", None, "vsql_complex.COMPLEX"] for index in range(201)]
        report, _ = collect({m.COLUMNS_SQL: columns, m.ROUTINES_SQL: routines,
                             m.EXTENSIONS_SQL: [["vsql_complex", "1", None, None, None, None],
                                                ["another", "1", None, None, None, None]]})
        for item in report["extensions"]:
            for key in ("columns", "routines"):
                self.assertIsNone(item[key]["count"])
                self.assertTrue(item[key]["truncated"])
                self.assertLessEqual(len(item[key]["sample"]), 50)
        self.assertEqual(len(report["extensions"][0]["columns"]["sample"]), 50)
        self.assertTrue(any("500" in note for note in report["notes"]))
        self.assertTrue(any("200" in note for note in report["notes"]))

    def test_sample_limit_keeps_exact_total_when_complete(self):
        for size in (0, 50, 51, 500):
            with self.subTest(size=size):
                report, _ = collect({m.COLUMNS_SQL: [["s", "t", str(n), "vsql_complex.COMPLEX"] for n in range(size)]})
                group = report["extensions"][0]["columns"]
                self.assertEqual(group["count"], size)
                self.assertEqual(len(group["sample"]), min(size, 50))
                self.assertEqual(group["truncated"], size > 50)

    def test_dependency_reads_exclude_protected_schema_and_have_server_limits(self):
        _, conn = collect()
        dependency_sql = [sql for sql, _ in conn.calls if "FROM INFORMATION_SCHEMA.COLUMNS" in sql or "FROM INFORMATION_SCHEMA.PARAMETERS" in sql]
        self.assertEqual(len(dependency_sql), 2)
        self.assertIn("LIMIT 501", dependency_sql[0])
        self.assertIn("LIMIT 201", dependency_sql[1])
        for sql in dependency_sql:
            for schema in ("mysql", "information_schema", "performance_schema", "sys", "villagesql"):
                self.assertIn("'%s'" % schema, sql)
        self.assertFalse(any("FROM villagesql." in sql for sql, _ in conn.calls))

    def test_hostile_pending_error_and_names_are_bounded_plain_text(self):
        hostile = "<img src=x onerror=alert(1)>\x1b\x00\x07"
        name = "<script>alert(1)</script>\x01"
        report, _ = collect({m.EXTENSIONS_SQL: [[name, "0.0.3", "0.0.4", "now", hostile + "x" * 100000, "later"]],
                             m.REGISTRATION_SQL: [[name, 5, json.dumps({"funcs": [{"name": hostile * 50}], "types": []})]]})
        item = report["extensions"][0]
        self.assertIn("<script>alert(1)</script>", item["name"])
        self.assertIn("<img src=x onerror=alert(1)>", item["pending"]["last_error"])
        self.assertLessEqual(len(item["pending"]["last_error"]), 2000)
        self.assertTrue(item["pending"]["error_truncated"])
        self.assertLessEqual(len(item["functions"][0]), 256)
        for value in (item["name"], item["pending"]["last_error"], item["functions"][0]):
            self.assertNotIn("\x1b", value)
            self.assertNotIn("\x00", value)
            self.assertNotIn("\x01", value)

    def test_error_cap_boundary_and_control_expansion_are_reported(self):
        for text, truncated in (("x" * 2000, False), ("x" * 2001, True), ("\x1b" * 1000, True)):
            with self.subTest(length=len(text)):
                report, _ = collect({m.EXTENSIONS_SQL: [["vsql_complex", "1", "2", None, text, None]]})
                pending = report["extensions"][0]["pending"]
                self.assertLessEqual(len(pending["last_error"]), 2000)
                self.assertEqual(pending["error_truncated"], truncated)

    def test_malformed_oversized_nested_and_non_object_json_are_isolated(self):
        for raw in ("{", "[]", "null", '"hello"', '{"funcs":NaN}', "[" * 2000 + "]" * 2000,
                    '{"padding":"' + "x" * (256 * 1024) + '"}', '{"padding":"' + "é" * 140000 + '"}'):
            with self.subTest(length=len(raw)):
                report, _ = collect({m.REGISTRATION_SQL: [["vsql_complex", 5, raw]]})
                item = report["extensions"][0]
                self.assertEqual(item["protocol"], 5)
                self.assertIsNone(item["functions"])
                self.assertIsNone(item["types"])
                self.assertTrue(report["notes"])
                self.assertEqual(item["columns"]["count"], 1)

    def test_registered_lists_are_bounded_without_exposing_raw_json(self):
        raw = json.dumps({"funcs": [{"name": "f%d" % n} for n in range(201)],
                          "types": [{"name": "T%d" % n} for n in range(201)],
                          "unexpected_payload": "this-field-must-not-escape"})
        report, _ = collect({m.REGISTRATION_SQL: [["vsql_complex", 5, raw]]})
        item = report["extensions"][0]
        self.assertEqual(len(item["functions"]), 200)
        self.assertEqual(len(item["types"]), 200)
        self.assertTrue(item["functions_truncated"])
        self.assertTrue(item["types_truncated"])
        self.assertEqual(item["functions"][-1], "f199")
        self.assertNotIn("this-field-must-not-escape", json.dumps(report))

    def test_bad_registration_lists_are_unknown_not_empty(self):
        for values in ({"funcs": {}}, {"funcs": [None]}, {"funcs": [{"name": 123}]}, {"funcs": ["not-an-object"]}):
            with self.subTest(values=values):
                report, _ = collect({m.REGISTRATION_SQL: [["vsql_complex", 5, json.dumps(values)]]})
                self.assertIsNone(report["extensions"][0]["functions"])
                self.assertTrue(report["notes"])

    def test_ten_thousand_dotted_variables_have_a_global_response_bound(self):
        rows = [[("vsql_complex" if n % 2 else "unknown") + ".value%d" % n, "x" * 3000] for n in range(10000)]
        report, _ = collect({m.VARIABLES_SQL: rows, m.STATUS_SQL: []})
        item = report["extensions"][0]
        self.assertEqual(len(item["variables"]) + len(report["other_variables"]), 1000)
        self.assertTrue(item["variables_truncated"])
        self.assertTrue(report["other_variables_truncated"])
        self.assertTrue(any("1000" in note for note in report["notes"]))
        self.assertTrue(all(len(row["value"]) == 2000 for row in item["variables"]))
        self.assertLess(len(json.dumps(report)), 2200000)

    def test_missing_extension_inventory_does_not_assign_unowned_variables(self):
        report, _ = collect(errors={m.EXTENSIONS_SQL: "ERROR 1142: denied"})
        self.assertIsNone(report["other_variables"])
        self.assertIsNone(report["other_variables_truncated"])

    def test_build_json_errors_leave_build_unknown(self):
        for raw in ("{", "[]", "x" * 300000):
            with self.subTest(length=len(raw)):
                report, _ = collect({m.VARIABLES_SQL: [["villagesql_build_info", raw]]})
                self.assertIsNone(report["settings"]["build"])
                self.assertTrue(report["notes"])

    def test_schema_rejects_raw_registration_and_wrong_known_types(self):
        report, _ = collect()
        cases = []
        bad = copy.deepcopy(report)
        bad["extensions"][0]["registration_json"] = "{}"
        cases.append(bad)
        bad = copy.deepcopy(report)
        bad["summary"]["installed"] = "1"
        cases.append(bad)
        bad = copy.deepcopy(report)
        bad["extensions"][0]["protocol_state"] = "safe"
        cases.append(bad)
        bad = copy.deepcopy(report)
        bad["extensions"][0]["functions"] = ["f"] * 201
        cases.append(bad)
        bad = copy.deepcopy(report)
        bad["extensions"][0]["name"] = "n" * 257
        cases.append(bad)
        bad = copy.deepcopy(report)
        bad["extensions"][0]["functions"] = ["f" * 257]
        cases.append(bad)
        bad = copy.deepcopy(report)
        bad["server_version"] = "v" * 2001
        cases.append(bad)
        for bad in cases:
            with self.assertRaises(AssertionError):
                validate(bad, SCHEMA)


def bounded_inventory_replay(recording):
    """Alias only the two newly bounded requests over complete old captures.

    These are synthetic request aliases, not re-recorded live evidence. Prove
    the captured rows already fit both SQL bounds and their new ordering, so
    the unchanged recorded responses remain valid. Every other SQL change
    still fails ReplayConn's exact lookup.
    """
    inventory = ("SELECT EXTENSION_NAME, EXTENSION_VERSION, PENDING_VERSION, PENDING_REQUESTED_AT, "
                 "PENDING_LAST_ERROR, PENDING_LAST_ERROR_AT FROM INFORMATION_SCHEMA.EXTENSIONS "
                 "ORDER BY EXTENSION_NAME")
    registration = ("SELECT EXTENSION_NAME, NEGOTIATED_PROTOCOL, REGISTRATION_JSON "
                    "FROM INFORMATION_SCHEMA.EXTENSION_REGISTRATION")
    bounded_registration = (
        "SELECT EXTENSION_NAME, NEGOTIATED_PROTOCOL, IF(LENGTH(REGISTRATION_JSON) > 262144, "
        "'[myflames: registration exceeds 256 KB]', REGISTRATION_JSON) "
        "FROM INFORMATION_SCHEMA.EXTENSION_REGISTRATION ORDER BY EXTENSION_NAME LIMIT 1001")
    conn = ReplayConn(recording["calls"])
    for original, bounded in ((inventory, inventory + " LIMIT 1001"),
                               (registration, bounded_registration)):
        original, bounded = m.EXTENSIONS_TAG + original, m.EXTENSIONS_TAG + bounded
        if original not in conn.answers:
            continue
        for answer in conn.answers[original]:
            rows = [[unescape_mysql(cell) for cell in line.split("\t")]
                    for line in answer.split("\n") if line]
            assert len(rows) <= 1000, "old capture exceeds the new inventory bound"
            names = [row[0] for row in rows]
            assert names == sorted(names), "old capture needs the new deterministic ordering"
            if original == m.EXTENSIONS_TAG + registration:
                assert all(len(row[2].encode("utf-8")) <= 262144 for row in rows), \
                    "old capture exceeds the new registration bound"
        conn.aliases[bounded] = original
        conn.answers[bounded] = conn.answers[original]
    return conn


class ExtensionsReplay(unittest.TestCase):
    def test_live_recordings_for_all_three_codebases_match_their_reports(self):
        for name, base in (("villagesql-8.4", "mysql"), ("villagesql-percona-8.4", "percona"),
                           ("villagesql-9.7", "mysql")):
            with self.subTest(server=name):
                recording = fixture("extensions", name)
                conn = bounded_inventory_replay(recording)
                report = m.extensions_report(conn)
                report["taken_at"] = recording["report"]["taken_at"]
                self.assertEqual(report, recording["report"])
                self.assertEqual(report["server_base"], base)
                self.assertTrue(report["available"])
                self.assertEqual(report["settings"]["server_protocol"], 6)
                self.assertEqual(report["settings"]["preview_extensions"], True)
                validate(report, SCHEMA)
                assert_read_only(self, conn, m.EXTENSIONS_TAG)

    def test_synthetic_empty_fixture_is_explicitly_not_a_live_empty_server(self):
        recording = fixture("extensions", "villagesql-empty")
        conn = bounded_inventory_replay(recording)
        report = m.extensions_report(conn)
        report["taken_at"] = recording["report"]["taken_at"]
        self.assertEqual(report, recording["report"])
        self.assertEqual(report["extensions"], [])
        self.assertEqual(report["summary"]["installed"], 0)
        self.assertIn("synthetic", json.dumps(recording.get("source", "")).lower())
        validate(report, SCHEMA)


class ExtensionsAPI(unittest.TestCase):
    """Exercise dispatch and the real HTTP handler without a listening service."""

    path = "/api/live/extensions"

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = mock.patch.dict(os.environ, {"HOME": temp.name, "USERPROFILE": temp.name,
                                          "MYFLAMES_SECRET_BACKEND": "vault"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MYFLAMES_VAULT_PASSPHRASE", None)
        self.server = SimpleNamespace(origin="http://127.0.0.1:8765", token="test-session-token",
                                      cookie_name="myflames_test", session_secret="test-cookie-secret",
                                      store=WorkspaceStore())
        self.connection = {"host": "127.0.0.1", "port": 3306, "user": "app",
                           "password": Connection.password}

    def post(self, payload=None, headers=None, raw=None):
        # Same stream-backed handler setup as test_ui_http.TestWriteHTTP.post.
        # A negative fd ends the disconnect watcher without creating a socket.
        body = raw if raw is not None else json.dumps(payload).encode("utf-8")
        values = {"Host": "127.0.0.1:8765", "Origin": self.server.origin,
                  "Cookie": "myflames_test=test-cookie-secret", "X-Myflames-Token": self.server.token,
                  "Content-Type": "application/json", "Content-Length": str(len(body))}
        values.update(headers or {})
        handler = object.__new__(ui.WorkspaceHandler)
        handler.server, handler.connection = self.server, mock.Mock()
        handler.connection.fileno.return_value = -1
        handler.path, handler.command, handler.request_version = self.path, "POST", "HTTP/1.1"
        handler.requestline = "POST %s HTTP/1.1" % self.path
        handler.headers = Message()
        for key, value in values.items():
            if value is not None:
                handler.headers[key] = value
        handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
        handler.do_POST()
        head, content = handler.wfile.getvalue().split(b"\r\n\r\n", 1)
        lines = head.decode("ascii").split("\r\n")
        response_headers = dict(line.split(": ", 1) for line in lines[1:])
        self.assertEqual(response_headers["Cache-Control"], "no-store")
        self.assertEqual(response_headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(int(response_headers["Content-Length"]), len(content))
        return int(lines[0].split()[1]), json.loads(content)

    def test_route_is_bound_cancellable_and_single_flight(self):
        for paths in (ui._SERVER_VIEW_PATHS, ui._CONNECTION_PATHS,
                      ui._SINGLE_FLIGHT_READ_PATHS, ui._CANCELLABLE_READ_PATHS):
            self.assertIn(self.path, paths)
        self.assertNotIn(self.path, ui._WRITE_PATHS)
        conn = Connection()
        factory = FakeFactory(conn)
        with mock.patch("myflames.connector.MySQLConnection", factory):
            status, report = self.post({"connection": self.connection})
        self.assertEqual(status, 200)
        self.assertEqual(report["summary"]["installed"], 1)
        self.assertEqual(factory.options["query_timeout"], 60)
        self.assertEqual(factory.options["password"], Connection.password)
        self.assertNotIn(Connection.password, json.dumps(report))
        validate(report, SCHEMA)
        assert_read_only(self, conn, m.EXTENSIONS_TAG)

    def test_saved_password_is_resolved_for_matching_endpoint(self):
        settings = {key: value for key, value in self.connection.items() if key != "password"}
        profile = self.server.store.save("profile", dict(settings, name="VillageSQL lab"),
                                         password=Connection.password, remember=True)
        factory = FakeFactory(Connection())
        with mock.patch("myflames.connector.MySQLConnection", factory):
            report = ui.api_response(self.path, {"connection": dict(settings, profile_id=profile["id"])}, self.server)
        self.assertEqual(factory.options["password"], Connection.password)
        self.assertEqual(report["schema_version"], "extensions-1.0")
        self.assertNotIn(Connection.password, json.dumps(report))

    def test_auth_rejects_host_origin_cookie_and_token_before_database_dispatch(self):
        invalid = ({"Host": None}, {"Host": "attacker.example:8765"}, {"Origin": None},
                   {"Origin": "https://attacker.example"}, {"Origin": "null"}, {"Cookie": None},
                   {"Cookie": "myflames_test=wrong"}, {"X-Myflames-Token": None},
                   {"X-Myflames-Token": "wrong"})
        with mock.patch("myflames.connector.MySQLConnection") as factory:
            for headers in invalid:
                with self.subTest(headers=headers):
                    status, body = self.post({"connection": self.connection}, headers)
                    self.assertEqual(status, 403)
                    self.assertIn("error", body)
            factory.assert_not_called()

    def test_http_request_limits_run_before_database_dispatch(self):
        cases = (({"Content-Type": "text/plain"}, b"{}", 415),
                 ({"Content-Length": str(ui.MAX_BODY + 1)}, b"{}", 413),
                 ({"Transfer-Encoding": "chunked"}, b"{}", 413),
                 ({}, b"{", 400))
        with mock.patch("myflames.connector.MySQLConnection") as factory:
            for headers, body, expected in cases:
                with self.subTest(headers=headers):
                    status, response = self.post(headers=headers, raw=body)
                    self.assertEqual(status, expected)
                    self.assertIn("error", response)
            factory.assert_not_called()

    def test_connection_errors_keep_standard_envelope_without_credentials(self):
        with mock.patch("myflames.connector.MySQLConnection", side_effect=ConnectorError(
                "ERROR 1045: Access denied " + Connection.password)):
            status, body = self.post({"connection": self.connection})
        self.assertEqual(status, 400)
        self.assertIn("error", body)
        self.assertNotIn(Connection.password, json.dumps(body))


if __name__ == "__main__":
    unittest.main()
