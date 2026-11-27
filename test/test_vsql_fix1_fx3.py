"""KNN regressions using recorded 0.0.8 plans and source-derived main plans."""
import copy
import json
import sys
import unittest
from pathlib import Path

from myflames.chart_data import build_chart_data
from myflames.complexity import compute_complexity
from myflames.parser import analyze_plan, is_knn_scan, knn_description, operator_family, parse_explain

TEST = Path(__file__).resolve().parent
sys.path.insert(0, str(TEST))
from test_villagesql_knn import knn_nodes, raw_nodes, released_fixtures  # noqa: E402


def released_limits():
    for path, _scenario, _format in released_fixtures(
            ("s1", "s5"), ("json-v2", "analyze-json-v2")):
        source = json.loads(path.read_text(encoding="utf-8"))
        limit = next(node for node in raw_nodes(source) if node.get("access_type") == "limit")
        yield path.name, source, limit


def parsed(source):
    return parse_explain(json.dumps(source))


def main_plan():
    return json.loads((TEST / "fixtures/villagesql/explain-knn-synthetic-analyze.json")
                      .read_text(encoding="utf-8"))


class TestK1Offset(unittest.TestCase):
    def test_released_k_excludes_parent_offset(self):
        for name, source, limit in released_limits():
            with self.subTest(fixture=name):
                limit.update(limit=15, limit_offset=5,
                             operation="Limit/Offset: 10/5 row(s)")
                before = copy.deepcopy(source)
                root = parsed(source)
                node = knn_nodes(root)[0]
                self.assertEqual(node["details"]["limit"], 10)
                self.assertEqual(node["short_label"], "KNN SCAN [idx_v] K=10")
                for compact in (False, True):
                    row = next(row for row in build_chart_data(root, compact=compact)["nodes"]
                               if row["id"] == node["node_id"])
                    self.assertEqual(row["limit"], 10)
                self.assertEqual(source, before)

    def test_main_explicit_limit_keeps_its_offset(self):
        source = main_plan()
        source.update(limit=15, limit_offset=5)
        source["inputs"][0]["limit"] = 15
        node = knn_nodes(parsed(source))[0]
        self.assertEqual(node["details"]["limit"], 15)
        self.assertIn("K=15", node["folded_label"])


class TestK2UnboundedLimit(unittest.TestCase):
    def test_negative_maximum_and_count_all_rows_parent_do_not_invent_k(self):
        for value in (-1, -2, 18446744073709551615, 18446744073709551616, 15):
            for name, source, limit in released_limits():
                with self.subTest(fixture=name, limit=value):
                    limit.update(limit=value, limit_offset=5)
                    if value == 15:
                        limit["count_all_rows"] = True
                    before = copy.deepcopy(source)
                    root = parsed(source)
                    node = knn_nodes(root)[0]
                    self.assertIsNone(node["details"].get("limit"))
                    self.assertNotIn("K=", node["folded_label"])
                    for compact in (False, True):
                        row = next(row for row in build_chart_data(root, compact=compact)["nodes"]
                                   if row["id"] == node["node_id"])
                        self.assertNotIn("limit", row)
                    self.assertEqual(source, before)

    def test_unbounded_parent_does_not_discard_main_explicit_limit(self):
        for value in (-1, 18446744073709551615, 15):
            with self.subTest(limit=value):
                source = main_plan()
                source.update(limit=value, limit_offset=5, count_all_rows=True)
                self.assertEqual(knn_nodes(parsed(source))[0]["details"]["limit"], 5)


class TestK3MainJsonV1(unittest.TestCase):
    def test_main_traditional_access_type_is_knn(self):
        # Main's opt_explain.cc writes knn_scan in v1. Project the identity
        # from its source-derived v2 fixture into the v1 table envelope.
        main = main_plan()["inputs"][0]
        for access in ("knn_scan", "KNN_SCAN", "Knn_Scan"):
            with self.subTest(access=access):
                source = {"query_block": {"select_id": 1, "table": {
                    "table_name": main["table_name"], "key": main["index_name"],
                    "access_type": access, "rows_examined_per_scan": 2005,
                    "filtered": "100.00"}}}
                root = parsed(source)
                nodes = knn_nodes(root)
                self.assertEqual(len(nodes), 1)
                node = nodes[0]
                self.assertEqual(node["details"]["access_type"], "knn_scan")
                self.assertEqual(node["short_label"], "KNN SCAN [vectors.idx_embedding]")
                self.assertIsNone(node["details"].get("limit"))
                self.assertEqual(operator_family(node["details"]), "index_scan")
                self.assertIsNone(compute_complexity(node))
                self.assertEqual(analyze_plan(root)["full_scans"], [])
                for compact in (False, True):
                    row = next(row for row in build_chart_data(root, compact=compact)["nodes"]
                               if row["id"] == node["node_id"])
                    self.assertEqual(row["family"], "index_scan")
                    self.assertEqual(row["wb_kind"], "index")
                    self.assertEqual(row["access_description"],
                                     "Nearest rows through custom index idx_embedding")
                    self.assertIsNone(row["complexity"])

    def test_main_access_type_is_recognized_without_normalization(self):
        for access in ("knn_scan", "KNN_SCAN", "Knn_Scan"):
            with self.subTest(access=access):
                details = {"access_type": access, "operation": "Index KNN scan on vectors"}
                self.assertTrue(is_knn_scan(details))
                self.assertIsNone(compute_complexity({"details": details, "children": []}))


class TestK4OwnLimitMisestimate(unittest.TestCase):
    def test_main_standalone_cap_is_not_statistics_evidence(self):
        for returned in (0.0, 3.0, 5.0):
            with self.subTest(returned=returned):
                source = main_plan()["inputs"][0]
                source.update(estimated_rows=2005.0, actual_rows=returned)
                root = parsed(source)
                self.assertEqual(analyze_plan(root)["misestimates"], [])
                self.assertNotIn("misestimate", root["details"])
                for compact in (False, True):
                    self.assertNotIn("misestimate", build_chart_data(root, compact=compact)["nodes"][0])

    def test_filter_on_capped_knn_does_not_recommend_statistics_refresh(self):
        child = main_plan()["inputs"][0]
        child["estimated_rows"] = 2005.0
        source = {"access_type": "filter", "operation": "Filter: vectors.id > 2",
                  "estimated_rows": 1000.0, "actual_rows": 3.0,
                  "actual_loops": 1, "actual_last_row_ms": 0.2, "inputs": [child]}
        self.assertEqual(analyze_plan(parsed(source))["misestimates"], [])

    def test_underestimates_and_unbounded_access_keep_statistics_findings(self):
        for cap, estimated, actual in ((5000, 1.0, 2005.0), (None, 2005.0, 5.0)):
            with self.subTest(cap=cap):
                source = main_plan()["inputs"][0]
                source.update(limit=cap, estimated_rows=estimated, actual_rows=actual)
                analysis = analyze_plan(parsed(source))
                self.assertEqual(len(analysis["misestimates"]), 1)
                self.assertEqual(analysis["misestimates"][0]["under"], actual > estimated)


class TestK5CapDescription(unittest.TestCase):
    def test_descriptions_state_cap_even_when_candidate_pool_is_short(self):
        # Extensions can supply extra candidates for offsets/filters/joins.
        # K describes the request, not a guaranteed count of scan output.
        for cap, returned in ((5, 3.0), (5, 5.0), (5, 12.0), (0, 0.0)):
            with self.subTest(cap=cap, returned=returned):
                source = main_plan()["inputs"][0]
                source.update(limit=cap, actual_rows=returned)
                root = parsed(source)
                meaning = "Up to %d nearest rows requested by embedding <-> '[1,0,0,0]' through custom index idx_embedding" % cap
                self.assertEqual(knn_description(root["details"]), meaning)
                for compact in (False, True):
                    row = build_chart_data(root, compact=compact)["nodes"][0]
                    self.assertEqual(row["access_description"], meaning)
                    self.assertIn(meaning, row["details_text"])
                    self.assertIn(meaning, row["flame_details"])


class TestK6PreviewContract(unittest.TestCase):
    def test_both_schemas_describe_the_actual_bounded_preview(self):
        source = main_plan()["inputs"][0]
        point = "'[" + ", ".join(["0.12", "0.98"] * 768) + "]'"
        source["lookup_condition"] = "embedding <-> " + point
        root = parsed(source)
        for version, compact in (("1.0", False), ("2.0", True)):
            with self.subTest(version=version):
                path = TEST.parent / "docs/schemas" / ("chart-data-%s.json" % version)
                schema = json.loads(path.read_text(encoding="utf-8"))
                fields = schema["$defs"]["node"]["properties"]
                description = fields["lookup_condition"]["description"].lower()
                self.assertIn("preview", description)
                self.assertIn("128", description)
                self.assertNotIn("verbatim", description)
                row = build_chart_data(root, compact=compact)["nodes"][0]
                preview = row["lookup_condition"].partition(" <-> ")[2]
                self.assertLessEqual(len(preview), 128)
                self.assertIn("1536 values", preview)
                self.assertNotEqual(row["lookup_condition"], source["lookup_condition"])


if __name__ == "__main__":
    unittest.main()
