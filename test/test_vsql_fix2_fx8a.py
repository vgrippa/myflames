"""FX8a regressions: bounded extension output and exact identity evidence."""
import json
import os
import re
import unittest

from myflames import (extensions, lifecycle, plan_privacy, privileges, schema_versions,
                      server_info, upgrade_targets)
from _perf_replay import ReplayConn, batch, expanded_preflight_sql
import _schema_replay as schema_replay
from test_extensions import collect


class ExtensionReportBudgets(unittest.TestCase):
    def test_registration_sql_refuses_oversized_bytes_without_cutting_text(self):
        self.assertIn("IF(LENGTH(REGISTRATION_JSON) > 262144,", extensions.REGISTRATION_SQL)
        self.assertNotIn("LEFT(", extensions.REGISTRATION_SQL)
        self.assertTrue(extensions.REGISTRATION_SQL.endswith("ORDER BY EXTENSION_NAME LIMIT 1001"))

    def test_oversized_registration_sentinel_keeps_protocol_and_unknown_details(self):
        report, _ = collect({extensions.REGISTRATION_SQL: [["vsql_complex", 5,
                              "[myflames: registration exceeds 256 KB]"]]})
        item = report["extensions"][0]
        self.assertEqual(item["protocol"], 5)
        self.assertIsNone(item["functions"])
        self.assertIsNone(item["types"])
        self.assertTrue(any("exceeds the 256 KB" in note for note in report["notes"]))

    def test_multibyte_registration_has_a_byte_boundary(self):
        prefix, suffix = '{"sdk_version":"', '"}'
        raw = prefix + "😀" * ((extensions.MAX_REGISTRATION_BYTES - len(prefix + suffix)) // 4) + suffix
        notes = []
        self.assertIsInstance(extensions._json_object(raw, "Registration", notes), dict)
        self.assertIsNone(extensions._json_object(raw[:-2] + "😀" + suffix, "Registration", notes))
        self.assertTrue(any("256 KB" in note for note in notes))

    def test_duplicate_names_do_not_amplify_registration_and_dependencies(self):
        registration = json.dumps({key: [{"name": "x" * 256}] * 200
                                   for key in ("funcs", "types")})
        report, conn = collect({
            extensions.EXTENSIONS_SQL: [["vsql_complex", "1", None, None, None, None]] * 1000,
            extensions.REGISTRATION_SQL: [["vsql_complex", 5, registration]],
            extensions.COLUMNS_SQL: [["s" * 256, "t" * 256, "c" * 256,
                                      "vsql_complex." + "T" * 240]] * 500,
        })
        self.assertLess(len(json.dumps(report).encode("utf-8")), 2 * 1024 * 1024)
        self.assertIsNone(report["extensions"])
        self.assertIsNone(report["summary"]["installed"])
        self.assertTrue(any("duplicate" in note.lower() for note in report["notes"]))
        self.assertEqual(len(conn.calls), 8)

    def test_registration_names_have_one_report_wide_budget(self):
        registration = json.dumps({key: [{"name": "x" * 256}] * 200
                                   for key in ("funcs", "types")})
        report, _ = collect({
            extensions.EXTENSIONS_SQL: [["ext%d" % n, "1", None, None, None, None]
                                        for n in range(20)],
            extensions.REGISTRATION_SQL: [["ext%d" % n, 5, registration] for n in range(20)],
        })
        count = sum(len(item[key] or []) for item in report["extensions"]
                    for key in ("functions", "types"))
        self.assertLessEqual(count, 1000)
        self.assertTrue(report["extensions"][-1]["functions_truncated"])
        self.assertTrue(report["extensions"][-1]["types_truncated"])


class SharedPlanPrivacy(unittest.TestCase):
    def test_all_additional_expression_fields_hide_nested_literals(self):
        raw = {"query_block": {key: ["f('private-literal', 7654321)"] for key in
                              ("extra_condition", "functions", "filesort_key", "outer_ref_condition",
                               "group_items", "index_condition_bka", "pseudo_bits_condition")},
               "table_name": "table123", "rows": 42}
        for hide in (plan_privacy.hide_plan_literals, plan_privacy.hide_analysis_literals):
            with self.subTest(hide=hide.__name__):
                hidden = hide(raw)
                self.assertNotIn("private-literal", json.dumps(hidden))
                self.assertNotIn("7654321", json.dumps(hidden))
                self.assertEqual(hidden["table_name"], "table123")
                self.assertEqual(hidden["rows"], 42)
        self.assertIn("private-literal", json.dumps(raw), "The original plan must stay intact")

    def test_boolean_index_condition_bka_keeps_parser_evidence(self):
        from myflames.parser import parse_explain
        for enabled in (False, True):
            raw = {"query_block": {"select_id": 1, "table": {
                "table_name": "table123", "access_type": "range", "key": "idx123",
                "rows": 42, "index_condition_bka": enabled,
            }}}
            for hide in (plan_privacy.hide_plan_literals, plan_privacy.hide_analysis_literals):
                with self.subTest(enabled=enabled, hide=hide.__name__):
                    hidden = hide(raw)
                    self.assertIs(hidden["query_block"]["table"]["index_condition_bka"], enabled)
                    root = parse_explain(json.dumps(hidden))
                    self.assertEqual(root["details"]["pushed_index_condition"], enabled)


class VillageSQLReleaseLabels(unittest.TestCase):
    def test_hex_prerelease_labels_are_preserved_unless_a_separate_hash_follows(self):
        for label in ("deadbee", "abcdef1234", "A" * 40, "rc.1", "dev"):
            for build in ("", "-543f42e0601", "-" + "a" * 40):
                with self.subTest(label=label, build=build):
                    version = "8.4.11-villagesql-0.0.9-" + label + build
                    self.assertEqual(server_info.villagesql_version(version), "0.0.9-" + label)

    def test_unsafe_prerelease_suffix_is_never_copied_into_labels(self):
        for suffix in ("-dev\x1b[31m", "-<b>owned</b>", "-dev/owned", "-rç.1", "-dev+build",
                       "-K", "-İ", "-ı", "-ſ"):
            with self.subTest(suffix=suffix):
                version = "8.4.11-villagesql-0.0.9" + suffix
                self.assertIsNone(server_info.villagesql_version(version))
                label = server_info.server_label({"flavor": "villagesql", "server_version": version})
                self.assertEqual(label, "VillageSQL ? (8.4.11)")

    def test_safe_prerelease_labels_remain_visible(self):
        for suffix in ("", "-dev", "-rc.1", "-RC.2", "-dev-543f42e0601"):
            with self.subTest(suffix=suffix):
                expected = "0.0.9" + ("-dev" if suffix == "-dev-543f42e0601" else suffix)
                self.assertEqual(server_info.villagesql_version("8.4.11-villagesql-0.0.9" + suffix), expected)


class ExtensionPrivilegeDescriptions(unittest.TestCase):
    def test_server_registered_privilege_has_product_specific_description_only_on_villagesql(self):
        for flavor in ("mysql", "percona", "readyset", "villagesql"):
            with self.subTest(flavor=flavor):
                catalog, _ = privileges.privilege_catalog("mysql", "8.4.11",
                                                          [["EXTENSION_ADMIN"]], flavor=flavor)
                entry = next(item for item in catalog["global"] if item["name"] == "EXTENSION_ADMIN")
                self.assertTrue(entry["dynamic"])
                self.assertEqual(entry["description"], "Install, uninstall and change VillageSQL extensions."
                                 if flavor == "villagesql" else "Server-registered privilege.")
                for level, items in catalog.items():
                    if level != "global":
                        self.assertNotIn("EXTENSION_ADMIN", [item["name"] for item in items])


class RequestedVariablesConnection(object):
    """Emulate SHOW filtering: never return an identity name that was not asked for."""
    def __init__(self, base):
        self.values = {"version": "8.4.11-villagesql-0.0.8", "version_comment": "Source distribution",
                       "hostname": "fixture", "server_id": "1", "port": "3306",
                       "villagesql_server_version": base + "-8.4_0.0.8"}
        self.calls = []

    def run(self, sql, extra_flags=None, timeout=None):
        self.calls.append(sql)
        names = re.findall(r"'([a-z_]+)'", sql)
        names.extend(bytes.fromhex(value).decode("utf-8")
                     for value in re.findall(r"CONVERT\(X'([0-9a-f]+)' USING utf8mb4\)", sql))
        return batch((name, self.values[name]) for name in names if name in self.values)


class LifecycleIdentity(unittest.TestCase):
    def test_actual_requested_names_supply_the_villagesql_codebase(self):
        for base in ("mysql", "percona"):
            with self.subTest(base=base):
                conn = RequestedVariablesConnection(base)
                report = lifecycle.read_server(conn)
                self.assertEqual(report["server_base"], base)
                self.assertEqual(len(conn.calls), 1, "The existing SHOW must supply the codebase")

    def test_recorded_lifecycle_identity_changes_only_by_the_optional_name(self):
        root = os.path.join(os.path.dirname(__file__), "fixtures", "lifecycle")
        for name in ("native-8.0.46-to-8.4.0.json", "native-8.0.46-to-9.7.0.json"):
            with self.subTest(recording=name):
                with open(os.path.join(root, name), encoding="utf-8") as handle:
                    record = json.load(handle)
                conn = ReplayConn([[call["sql"], call["result"]] for call in record["calls"]])
                server = lifecycle.read_server(conn)
                original = record["calls"][0]["sql"]
                self.assertNotIn("server_base", server)
                self.assertEqual(conn.calls[0][0], expanded_preflight_sql(original, record["calls"][0]["result"]))
                self.assertEqual(conn.aliases[conn.calls[0][0]], original)
                self.assertEqual(len(conn.calls), 1)


class SchemaReplayConnection(schema_replay.ReplayConn):
    """Use the strict alias for the optional name, retaining all captured answers."""
    def _answer(self, method, sql, flags, record=True):
        for call in self.record["calls"]:
            if (method == call["method"] and sql != call["sql"] and
                    sql == expanded_preflight_sql(call["sql"], call.get("stdout", ""))):
                if record:
                    self.sent.append((method, sql, list(flags or [])))
                return super()._answer(method, call["sql"], flags, record=False)
        return super()._answer(method, sql, flags, record=record)


class FilteringSchemaConnection(schema_replay.ReplayConn):
    """Keep recorded object reads, but filter synthetic VillageSQL identity by SQL."""
    def __init__(self, base):
        record = schema_replay.recording("mysql-8.4", "before")
        super().__init__(record)
        self.variables = dict(line.split("\t", 1) for line in record["calls"][0]["stdout"].splitlines())
        self.variables.update(version="8.4.11-villagesql-0.0.8", version_comment="Source distribution",
                              villagesql_server_version=base + "-8.4_0.0.8")

    def _answer(self, method, sql, flags, record=True):
        if method == "run" and sql.startswith(schema_versions.TAG + "SHOW GLOBAL VARIABLES"):
            names = re.findall(r"'([a-z_]+)'", sql)
            return {"stdout": batch((name, self.variables[name]) for name in names if name in self.variables)}
        return super()._answer(method, sql, flags, record=record)


class SchemaIdentity(unittest.TestCase):
    def test_capture_requests_the_villagesql_codebase_in_its_existing_show(self):
        for base in ("mysql", "percona"):
            with self.subTest(base=base):
                conn = FilteringSchemaConnection(base)
                record = schema_replay.recording("mysql-8.4", "before")
                snapshot = schema_versions.capture(conn, record["database"], record["source"])
                self.assertEqual(snapshot["server"]["server_base"], base)
                shows = [sql for method, sql, _ in conn.sent
                         if sql.startswith(schema_versions.TAG + "SHOW GLOBAL VARIABLES")]
                self.assertEqual(len(shows), 1)
                self.assertIn("'villagesql_server_version'", shows[0])

    def test_recorded_schema_capture_keeps_checksums_and_all_other_statements(self):
        for server in schema_replay.SERVERS:
            for which in schema_replay.WHICH:
                with self.subTest(server=server, which=which):
                    record = schema_replay.recording(server, which)
                    conn = SchemaReplayConnection(record)
                    snapshot = schema_versions.capture(conn, record["database"], record["source"])
                    self.assertEqual(snapshot["checksum"], schema_replay.load(server + "-" + which)["checksum"])
                    self.assertNotIn("server_base", snapshot["server"])
                    # Every actual schema call is either byte-identical or
                    # exactly the one permitted optional-name expansion.
                    for method, sql, _ in conn.sent:
                        if sql.startswith(schema_versions.TAG):
                            self.assertTrue(any(method == call["method"] and sql in (
                                call["sql"], expanded_preflight_sql(call["sql"], call.get("stdout", "")))
                                for call in record["calls"]), sql)


class VillageSQLPackaging(unittest.TestCase):
    def test_mysql_layouts_do_not_establish_villagesql_packaging(self):
        for basedir, plugin, operating_system in (
                ("/usr", "/usr/lib64/mysql/plugin", "Linux"),
                ("/usr", "/usr/lib/mysql/plugin", "Linux"),
                ("/opt/mysql-8.4.11-linux-glibc2.28-x86_64",
                 "/opt/mysql-8.4.11-linux-glibc2.28-x86_64/lib/plugin", "Linux"),
                ("C:/Program Files/MySQL/MySQL Server 8.4", "C:/plugins", "Win64"),
                ("/opt/homebrew/Cellar/mysql/8.4.11", "/opt/homebrew/lib/plugin", "macos")):
            with self.subTest(basedir=basedir):
                layout = {"version": "8.4.11-villagesql-0.0.8", "version_comment": "Source distribution",
                          "basedir": basedir, "plugin_dir": plugin, "hostname": "012345abcdef",
                          "version_compile_os": operating_system, "version_compile_machine": "x86_64"}
                report = upgrade_targets.build_report(layout)
                install = report["install"]
                self.assertEqual(install["method"], "unknown")
                self.assertIn("VillageSQL", install["label"])
                self.assertIsNone(install["package"])
                self.assertFalse(install["container"])
                self.assertTrue(any(basedir in evidence for evidence in install["evidence"]))
                self.assertIn("own", " ".join(install["evidence"]))
                self.assertEqual(report["targets"], [])
                self.assertIsNone(report["guidance"]["command"])
                self.assertIsNone(report["guidance"]["url"])


if __name__ == "__main__":
    unittest.main()
