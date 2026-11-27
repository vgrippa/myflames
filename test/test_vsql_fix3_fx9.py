"""Final VillageSQL regressions using captured plans and bounded edge cases.

Captures prove display behavior only. Synthetic mutations exercise malformed
metadata without connecting to a server or reading a saved connection.
"""
import copy
import json
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from myflames.chart_data import build_chart_data
from myflames import extensions, server_info
from myflames.parser import flatten_nodes, is_knn_scan, parse_explain
from myflames.plan_privacy import hide_analysis_literals
from test_extensions import collect
from test_villagesql_knn import long_knn_cases, raw_with_point

TEST = Path(__file__).resolve().parent


class SharedChartMeasurements(unittest.TestCase):
    def test_captured_village_and_mysql_measurement_text_survives_sharing(self):
        paths = [TEST / "fixtures" / "villagesql" /
                 ("explain-cidx-%s-s1-analyze-json-v2.json" % version)
                 for version in ("8.4", "9.7")]
        paths.append(TEST / "fixtures" / "explain-001-table-scan-users-no-filter.json")
        for path in paths:
            for compact in (False, True):
                with self.subTest(fixture=path.name, compact=compact):
                    data = build_chart_data(parse_explain(path.read_text(encoding="utf-8")),
                                            compact=compact)
                    original = copy.deepcopy(data)
                    hidden = hide_analysis_literals(data)
                    for before, after in zip(data["nodes"], hidden["nodes"]):
                        for field in ("details_text", "flame_details", "access_description"):
                            if field in before:
                                self.assertEqual(after[field], before[field])
                    self.assertEqual(data, original, "Sharing never mutates the collected plan")

    def test_knn_points_and_previews_are_hidden_without_erasing_measurements(self):
        cases = [("short vector", "'[918273,654321,345678]'"),
                 ("SQL expression", "make_point(918273, 654321, 345678)")]
        cases.extend((name, point) for name, point, _counts in long_knn_cases())
        for name, point in cases:
            for compact in (False, True):
                with self.subTest(point=name, compact=compact):
                    data = build_chart_data(parse_explain(json.dumps(raw_with_point(point))),
                                            compact=compact)
                    hidden = hide_analysis_literals(data)
                    before, after = data["nodes"][0], hidden["nodes"][0]
                    for field in ("details_text", "flame_details", "access_description"):
                        self.assertNotIn(point[:12], after[field])
                        self.assertNotIn("characters)", after[field])
                        self.assertNotIn("values)", after[field])
                    for field in ("details_text", "flame_details"):
                        # These lines are generated measurements, not SQL literals.
                        metric_lines = [line for line in before[field].splitlines()
                                        if line.startswith(("Actual ", "Estimated ", "Total ",
                                                            "Self time", "Rows:", "Time:", "Cost:"))]
                        self.assertTrue(metric_lines)
                        for line in metric_lines:
                            self.assertIn(line, after[field])
                    self.assertIn("Up to 5 nearest rows requested", after["access_description"])

    def test_a_scalar_point_equal_to_k_does_not_erase_k_or_row_measurements(self):
        data = build_chart_data(parse_explain(json.dumps(raw_with_point("5"))))
        row = hide_analysis_literals(data)["nodes"][0]
        self.assertEqual(row["lookup_condition"], "embedding <-> ?")
        self.assertIn("Up to 5 nearest rows requested by embedding <-> ?", row["access_description"])
        self.assertIn("Actual rows / loop: 5.0", row["details_text"])


class RegistrationBudget(unittest.TestCase):
    def collect_after_budget(self, registrations):
        # Five valid function lists consume the entire projection budget.
        full = {"funcs": [{"name": "function%d" % n} for n in range(200)], "types": []}
        payloads = [full] * 5 + registrations
        return collect({
            extensions.EXTENSIONS_SQL: [["ext%02d" % n, "1", None, None, None, None]
                                        for n in range(len(payloads))],
            extensions.REGISTRATION_SQL: [["ext%02d" % n, 5, json.dumps(value)]
                                          for n, value in enumerate(payloads)],
        })[0]

    def test_exhausted_lists_are_unknown_with_one_report_note(self):
        report = self.collect_after_budget([
            {"funcs": [{"name": "unseen"}], "types": [{"name": "UNSEEN"}]},
            {"funcs": [{"name": "also_unseen"}], "types": [{"name": "ALSO_UNSEEN"}]},
            {"funcs": [], "types": []}, {},
        ])
        for item in report["extensions"][5:7]:
            for key in ("functions", "types"):
                self.assertIsNone(item[key], "An omitted nonempty list cannot establish zero names")
                self.assertTrue(item[key + "_truncated"])
        for key in ("functions", "types"):
            self.assertEqual(report["extensions"][7][key], [])
            self.assertFalse(report["extensions"][7][key + "_truncated"])
            self.assertIsNone(report["extensions"][8][key])
            self.assertIsNone(report["extensions"][8][key + "_truncated"])
        self.assertEqual(len(report["notes"]), 1)
        self.assertIn("1000", report["notes"][0])
        self.assertNotIn("first 0", json.dumps(report["notes"]))

    def test_malformed_entries_remain_unknown_after_budget_or_prefix_limit(self):
        for malformed in (None, "not an object", {}, {"name": 123}):
            for prefix in ([], [{"name": "valid"}] * 200):
                with self.subTest(malformed=malformed, prefix=len(prefix)):
                    report = self.collect_after_budget([
                        {"funcs": prefix + [malformed], "types": []},
                    ])
                    item = report["extensions"][-1]
                    self.assertIsNone(item["functions"])
                    self.assertIsNone(item["functions_truncated"])
                    self.assertEqual(len(report["notes"]), 1)
                    self.assertIn("malformed funcs entries", report["notes"][0])
            notes = []
            names, truncated = extensions._registered_names(
                {"funcs": [{"name": "valid"}] * 200 + [malformed]}, "funcs", "test", notes)
            self.assertIsNone(names, "Malformed entries beyond the per-list prefix still invalidate it")
            self.assertIsNone(truncated)

    def test_partial_budget_keeps_a_bounded_prefix_and_one_report_note(self):
        full = {"funcs": [{"name": "f"}] * 200, "types": []}
        payloads = [full] * 4 + [{"funcs": [{"name": "f"}] * 150, "types": []},
                               {"funcs": [{"name": "f"}] * 75, "types": [{"name": "T"}]}]
        report, _ = collect({
            extensions.EXTENSIONS_SQL: [["ext%d" % n, "1", None, None, None, None]
                                        for n in range(len(payloads))],
            extensions.REGISTRATION_SQL: [["ext%d" % n, 5, json.dumps(value)]
                                          for n, value in enumerate(payloads)],
        })
        item = report["extensions"][-1]
        self.assertEqual(len(item["functions"]), 50)
        self.assertTrue(item["functions_truncated"])
        self.assertIsNone(item["types"])
        self.assertTrue(item["types_truncated"])
        self.assertEqual(len(report["notes"]), 1)
        self.assertIn("1000", report["notes"][0])


class ParentLimitValidation(unittest.TestCase):
    def parse_with_limit(self, count, offset, own_limit=None):
        path = TEST / "fixtures" / "villagesql" / "explain-cidx-8.4-s1-analyze-json-v2.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["limit"], raw["limit_offset"] = count, offset
        if own_limit is not None:
            raw["inputs"][0]["limit"] = own_limit
        root = parse_explain(json.dumps(raw))
        knn = next(node for node in flatten_nodes(root) if is_knn_scan(node["details"]))
        return root, knn

    def test_invalid_offsets_do_not_fail_the_plan_or_infer_k(self):
        for offset in (-1, 11, True, False, None, 1.5, "1", [], {}):
            with self.subTest(offset=offset):
                root, knn = self.parse_with_limit(10, offset)
                self.assertIsNone(knn["details"].get("limit"))
                self.assertNotIn(" K=", knn["short_label"])
                row = next(row for row in build_chart_data(root)["nodes"] if row["id"] == knn["node_id"])
                self.assertIsNone(row.get("limit"))

    def test_valid_offsets_including_zero_count_keep_exact_k(self):
        for count, offset, expected in ((10, 0, 10), (10, 3, 7), (10, 10, 0), (0, 0, 0)):
            with self.subTest(count=count, offset=offset):
                _root, knn = self.parse_with_limit(count, offset)
                self.assertEqual(knn["details"]["limit"], expected)
                self.assertIn(" K=%d" % expected, knn["short_label"])

    def test_unbounded_parent_limits_are_unknown_and_cannot_override_an_own_limit(self):
        for count in (-1, 18446744073709551615, None):
            with self.subTest(count=count):
                _root, knn = self.parse_with_limit(count, 0)
                self.assertIsNone(knn["details"].get("limit"))
                _root, knn = self.parse_with_limit(count, 11, own_limit=5)
                self.assertEqual(knn["details"]["limit"], 5)

    def test_malformed_parent_count_retains_existing_rejection(self):
        for count in (True, 1.5, "10", [], {}):
            with self.subTest(count=count):
                with self.assertRaisesRegex(ValueError, "KNN limit"):
                    self.parse_with_limit(count, 0)


class SharedIdentityASCII(unittest.TestCase):
    def test_shared_unicode_and_ascii_label_vectors(self):
        path = TEST / "fixtures" / "villagesql" / "version-labels.json"
        vectors = json.loads(path.read_text(encoding="utf-8"))
        for vector in vectors:
            with self.subTest(name=vector["name"]):
                self.assertEqual(server_info.villagesql_version(vector["version"]),
                                 vector["expected_release"])
                if "expected_upstream" in vector:
                    self.assertEqual(server_info.base_version(vector["version"]),
                                     vector["expected_upstream"])
                expected = vector.get("expected_label") or vector.get("expected_explicit_label")
                if expected is not None:
                    self.assertEqual(server_info.server_label({
                        "server_version": vector["version"], "server_product": "VillageSQL",
                        "server_base": vector["base"],
                    }), expected)


if __name__ == "__main__":
    unittest.main()
