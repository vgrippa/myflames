"""VillageSQL custom-index KNN access keeps its own meaning in every view.

The cidx fixtures were recorded from released VillageSQL 0.0.8 servers.
The synthetic fixtures cover the source-derived main-branch format; their
provenance is in fixtures/villagesql/SYNTHETIC.md. MariaDB cannot emit this
VillageSQL-only access path, so the MariaDB case guards ordinary full-scan
findings instead. None of these tests connects to a server or reads profiles.
"""
import copy
import json
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from myflames.advisor import advise
from myflames.chart_data import build_chart_data
from myflames.complexity import compute_complexity
from myflames.output_html_report import _render_svg
from myflames.output_workbench import _details, _kind, render_workbench
from myflames.parser import (
    _detect_nonsargable_joins, _detect_optimizer_switches,
    analyze_plan, build_folded_label, enhance_tooltip_flame, flatten_nodes,
    hide_parsed_plan_literals, hide_plan_json_literals, is_knn_scan, knn_description,
    knn_lookup_preview, operator_family, parse_explain, parse_node,
)

TEST = Path(__file__).resolve().parent
REPO = TEST.parent
sys.path.insert(0, str(TEST))
from test_chart_data import COMPACT_SCHEMA, SCHEMA, validate  # noqa: E402

FIXTURES = (
    TEST / "fixtures" / "villagesql" / "explain-knn-synthetic-analyze.json",
    TEST / "fixtures" / "villagesql" / "explain-knn-synthetic-join.json",
)
RELEASED_FORMATS = ("json-v1", "json-v2", "analyze-json-v2")
RELEASED_SCENARIOS = ("s1", "s2", "s3", "s4", "s5")
SVG = "{http://www.w3.org/2000/svg}"
LOOKUP = "embedding <-> '[1,0,0]'"
MEANING = "Up to 5 nearest rows requested by embedding <-> '[1,0,0]' through custom index idx_embedding"
FOLDED = "KNN SCAN [vectors.idx_embedding] K=5 starts=1 rows=5"


def knn_raw(**overrides):
    """Minimal JSON-v2 access fields emitted by explain_access_path.cc."""
    value = {
        "operation": "Index KNN scan on vectors using idx_embedding "
                     "(embedding <-> '[1,0,0]'), limit 5",
        "access_type": "index",
        "index_access_type": "index_knn_scan",
        "table_name": "vectors",
        "index_name": "idx_embedding",
        "lookup_condition": LOOKUP,
        "covering": False,
        "limit": 5,
        "estimated_rows": 5.0,
        "estimated_total_cost": 2.0,
        "actual_rows": 5.0,
        "actual_loops": 1,
        "actual_first_row_ms": 0.4,
        "actual_last_row_ms": 1.0,
    }
    value.update(overrides)
    return value


def parsed(value):
    return parse_explain(json.dumps(value))


def released_fixtures(scenarios=RELEASED_SCENARIOS, formats=RELEASED_FORMATS):
    """Each live capture is intentional, including the non-KNN fallback."""
    for server in ("8.4", "9.7"):
        for scenario in scenarios:
            for plan_format in formats:
                path = TEST / "fixtures" / "villagesql" / (
                    "explain-cidx-%s-%s-%s.json" % (server, scenario, plan_format))
                yield path, scenario, plan_format


def released_knn_raw(**overrides):
    """The released v2 node reports an index in prose and no table identity."""
    value = {
        "operation": "Custom index distance scan on idx_v",
        "access_type": "index_distance_scan",
        "actual_rows": 10.0,
        "actual_loops": 1,
        "estimated_rows": 2005.0,
        "actual_first_row_ms": 0.9,
        "actual_last_row_ms": 1.0,
    }
    value.update(overrides)
    return value


def limit_raw(child, count=10):
    return {"operation": "Limit: %s row(s)" % count,
            "access_type": "limit", "limit": count, "inputs": [child]}


def long_knn_cases():
    """Large point expressions with an exact size and a recognizable prefix."""
    vector = "'[" + ", ".join(["0.12", "0.98"] * 768) + "]'"
    literal = "'private-vector-" + "x" * (50 * 1024 - 17) + "'"
    return (("1536-value vector", vector, ("1536 values", "%d characters" % len(vector))),
            ("50 KB literal", literal, ("51200 characters",)))


def raw_with_point(point):
    lookup = "embedding <-> " + point
    return knn_raw(lookup_condition=lookup, operation=(
        "Index KNN scan on vectors using idx_embedding (%s), limit 5" % lookup))


def raw_nodes(value):
    """Read source field values without reproducing parser normalization."""
    if isinstance(value, dict):
        if "operation" in value:
            yield value
        for child in value.values():
            yield from raw_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from raw_nodes(child)


def knn_nodes(root):
    return [node for node in flatten_nodes(root)
            if is_knn_scan(node["details"])]


def svg_payload(document):
    """Literal readable strings, after XML has decoded escaped content."""
    return "\n".join([element.text or "" for element in document.iter()] +
                     [value for element in document.iter()
                      for value in element.attrib.values()])


class TestVillageSQLKnnParsing(unittest.TestCase):
    def test_source_fixtures_preserve_every_knn_access_field(self):
        fields = ("index_access_type", "index_name", "lookup_condition", "covering", "limit")
        for path in FIXTURES:
            with self.subTest(fixture=path.name):
                text = path.read_text(encoding="utf-8")
                expected = [node for node in raw_nodes(json.loads(text))
                            if node.get("index_access_type") == "index_knn_scan"]
                root = parse_explain(text)
                actual = knn_nodes(root)
                self.assertEqual(len(expected), 1, "fixture must exercise KNN")
                self.assertEqual(len(actual), len(expected))
                for source, node in zip(expected, actual):
                    self.assertEqual({key: node["details"].get(key) for key in fields},
                                     {key: source[key] for key in fields})
                    self.assertEqual(operator_family(node["details"]), "index_scan")
                    self.assertIsNotNone(node["details"]["actual_last_row_ms"])

    def test_knn_keeps_index_family_and_specific_folded_label(self):
        node = parsed(knn_raw())
        self.assertEqual(operator_family(node["details"]), "index_scan")
        self.assertEqual(build_folded_label(knn_raw()), FOLDED)
        self.assertEqual(node["folded_label"], FOLDED)
        self.assertEqual(node["details"]["lookup_condition"], LOOKUP)
        self.assertEqual(node["details"]["limit"], 5)

    def test_covering_knn_does_not_collapse_into_covering_index_label(self):
        node = parsed(knn_raw(covering=True, operation=(
            "Covering index KNN scan on vectors using idx_embedding "
            "(embedding <-> '[1,0,0]'), limit 5")))
        self.assertEqual(node["folded_label"], FOLDED)
        self.assertTrue(node["details"]["covering"])
        self.assertEqual(operator_family(node["details"]), "index_scan")

    def test_structured_knn_subtype_wins_over_words_inside_identifiers(self):
        node = parsed(knn_raw(
            table_name="sorted_vectors", index_name="lookup_idx",
            operation="Index KNN scan on sorted_vectors using lookup_idx "
                      "(embedding <-> '[1,0,0]'), limit 5"))
        self.assertEqual(operator_family(node["details"]), "index_scan")
        self.assertEqual(_kind(node), "index")
        self.assertEqual(node["folded_label"],
                         "KNN SCAN [sorted_vectors.lookup_idx] K=5 starts=1 rows=5")

    def test_knn_limit_is_independent_of_rows_returned(self):
        node = parsed(knn_raw(limit=100, actual_rows=3.0))
        self.assertEqual(node["details"]["limit"], 100)
        self.assertEqual(node["folded_label"],
                         "KNN SCAN [vectors.idx_embedding] K=100 starts=1 rows=3")

    def test_zero_limit_survives_truthiness_checks(self):
        node = parsed(knn_raw(limit=0, actual_rows=0.0))
        self.assertEqual(node["details"]["limit"], 0)
        self.assertEqual(node["folded_label"],
                         "KNN SCAN [vectors.idx_embedding] K=0 starts=1 rows=0")
        self.assertEqual(build_chart_data(node, compact=True)["nodes"][0]["limit"], 0)

    def test_absent_or_null_limit_does_not_invent_k_from_row_count(self):
        missing = knn_raw()
        missing.pop("limit")
        for value in (missing, knn_raw(limit=None)):
            with self.subTest(value=value):
                node = parsed(value)
                self.assertEqual(node["folded_label"],
                                 "KNN SCAN [vectors.idx_embedding] starts=1 rows=5")
                self.assertNotIn("limit", build_chart_data(node)["nodes"][0])
                self.assertIn("Nearest rows by " + LOOKUP, _details(node))
                if "limit" in value:
                    self.assertIn("limit", node["details"])
                    self.assertIsNone(node["details"]["limit"])
                else:
                    self.assertNotIn("limit", node["details"])

    def test_invalid_limit_is_rejected_before_it_reaches_labels_or_schema(self):
        for value in (-1, 1.5, True, False, "5", [], {}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "KNN limit"):
                    parsed(knn_raw(limit=value))

    def test_nonstring_lookup_condition_is_rejected_including_false_values(self):
        for value in (0, False, 3.5, [], {}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "lookup_condition"):
                    parsed(knn_raw(lookup_condition=value))

    def test_join_fixture_preserves_outer_and_inner_access_order(self):
        root = parse_explain(FIXTURES[1].read_text(encoding="utf-8"))
        joins = [node for node in flatten_nodes(root)
                 if node["details"].get("join_algorithm") == "nested_loop"]
        self.assertEqual(len(joins), 1)
        children = joins[0]["children"]
        self.assertEqual(len(children), 2)
        self.assertEqual(len(knn_nodes(children[0])), 1, "KNN must remain the outer input")
        self.assertEqual(children[1]["details"]["index_name"], "PRIMARY")
        self.assertEqual(children[1]["details"]["index_access_type"], "index_lookup")


class TestVillageSQLReleasedKnn(unittest.TestCase):
    def test_recognition_distinguishes_released_custom_and_upstream_spatial_access(self):
        for details in (
                {"index_access_type": "index_knn_scan", "access_type": "index"},
                {"index_access_type": "INDEX_KNN_SCAN", "access_type": "index"},
                {"access_type": "index_distance_scan"},
                {"access_type": "INDEX_DISTANCE_SCAN"},
                {"access_type": "index_distance"},
                {"access_type": "INDEX_DISTANCE"}):
            with self.subTest(details=details):
                self.assertTrue(is_knn_scan(details))
        for details in (
                {"access_type": "index", "index_access_type": "index_distance_scan"},
                {"access_type": "INDEX", "index_access_type": "INDEX_DISTANCE_SCAN"},
                {"operation": "Custom index distance scan on idx_v"},
                {"access_type": "table", "operation": "Table scan on knn_scan"}):
            with self.subTest(details=details):
                self.assertFalse(is_knn_scan(details))

    def test_every_routed_live_json_shape_preserves_known_fields_and_unknowns(self):
        for path, scenario, plan_format in released_fixtures(("s1", "s2", "s3", "s5")):
            with self.subTest(fixture=path.name):
                root = parse_explain(path.read_text(encoding="utf-8"))
                self.assertEqual(root["engine"], "mysql")
                accesses = knn_nodes(root)
                self.assertEqual(len(accesses), 1)
                node = accesses[0]
                details = node["details"]
                self.assertEqual(operator_family(details), "index_scan")
                self.assertEqual(_kind(node), "index")
                self.assertIsNone(compute_complexity(node))
                self.assertNotIn("misestimate", details)
                self.assertFalse(details.get("lookup_condition"))
                self.assertNotIn(" by ", knn_description(details))
                if plan_format == "json-v1":
                    self.assertEqual(details["access_type"], "index_distance")
                    self.assertEqual(details["table_name"], "t")
                    self.assertEqual(details["index_name"], "")
                    self.assertIsNone(details.get("limit"))
                    self.assertTrue(node["short_label"].startswith("KNN SCAN [t"))
                    self.assertNotIn("idx_v", node["short_label"])
                    self.assertFalse(details["rows_measured"])
                else:
                    self.assertEqual(details["access_type"], "index_distance_scan")
                    self.assertEqual(details["table_name"], "")
                    self.assertEqual(details["index_name"], "idx_v")
                    # S2 has a Filter between the Limit and access; S3 has
                    # a join there. Their ancestor K is not the access K.
                    expected_k = 10 if scenario in ("s1", "s5") else None
                    self.assertEqual(details.get("limit"), expected_k)
                    expected_label = "KNN SCAN [idx_v]" + (" K=10" if expected_k else "")
                    self.assertEqual(node["short_label"], expected_label)
                    self.assertTrue(node["folded_label"].startswith(expected_label + " starts=1"))
                    self.assertEqual(knn_description(details), "%s through custom index idx_v"
                                     % ("Up to 10 nearest rows requested" if expected_k else "Nearest rows"))
                    if plan_format == "analyze-json-v2":
                        expected_rows = 12.0 if scenario == "s2" else 10.0
                        self.assertEqual(details["actual_rows"], expected_rows)
                        self.assertGreater(details["estimated_rows"], expected_rows)
                        self.assertIn(" rows=%d" % expected_rows, node["folded_label"])
                    else:
                        self.assertIsNone(details["actual_rows"])
                analysis = analyze_plan(root)
                advise(analysis, schema={"t": {"engine": "InnoDB", "indexes": []}})
                for key in ("full_scans", "filesorts", "hash_joins", "nonsargable_joins",
                            "misestimates", "index_suggestions"):
                    self.assertEqual(analysis[key], [], key)
                if scenario == "s3":
                    self.assertIn("nested loop join", analysis["optimizer_features"])

    def test_released_fields_reach_both_chart_schemas_without_changing_node_order(self):
        for path, _scenario, _plan_format in released_fixtures(("s1", "s2", "s3", "s5")):
            with self.subTest(fixture=path.name):
                root = parse_explain(path.read_text(encoding="utf-8"))
                before = copy.deepcopy(root)
                node = knn_nodes(root)[0]
                ordered_ids = [item["node_id"] for item in flatten_nodes(root)]
                for compact, schema in ((False, SCHEMA), (True, COMPACT_SCHEMA)):
                    data = build_chart_data(root, compact=compact)
                    validate(data, schema)
                    self.assertEqual([row["id"] for row in data["nodes"]], ordered_ids)
                    row = next(row for row in data["nodes"] if row["id"] == node["node_id"])
                    self.assertEqual(row["family"], "index_scan")
                    self.assertEqual(row["wb_kind"], "index")
                    self.assertEqual(row["table"], node["details"]["table_name"])
                    self.assertEqual(row["index"], node["details"]["index_name"])
                    self.assertEqual(row.get("limit"), node["details"].get("limit"))
                    self.assertEqual(row["access_description"], knn_description(node["details"]))
                    self.assertIn(row["access_description"], row["details_text"])
                    self.assertIn(row["access_description"], row["flame_details"])
                    self.assertIsNone(row["complexity"])
                self.assertEqual(root, before)

    def test_fallback_live_plans_keep_full_scan_and_sort_findings(self):
        for path, _scenario, _plan_format in released_fixtures(("s4",)):
            with self.subTest(fixture=path.name):
                root = parse_explain(path.read_text(encoding="utf-8"))
                self.assertEqual(knn_nodes(root), [])
                analysis = analyze_plan(root)
                advise(analysis, schema={"t": {"engine": "InnoDB", "indexes": []}})
                self.assertEqual([scan["table"] for scan in analysis["full_scans"]], ["t"])
                self.assertEqual(len(analysis["filesorts"]), 1)
                self.assertTrue(any("scan" in warning.lower() for warning in analysis["warnings"]))
                self.assertTrue(any("sort" in warning.lower() for warning in analysis["warnings"]))

    def test_limit_inheritance_preserves_raw_inputs_and_direct_parent_boundary(self):
        leaf = released_knn_raw()
        source = limit_raw(leaf)
        before = copy.deepcopy(source)
        child = parse_node(source)["children"][0]
        self.assertEqual(child["details"]["limit"], 10)
        self.assertEqual(child["details"]["index_name"], "idx_v")
        self.assertEqual(source, before)
        self.assertIs(source["inputs"][0], leaf)
        self.assertNotIn("limit", leaf)
        self.assertNotIn("index_name", leaf)
        for intermediate in (
                {"access_type": "filter", "operation": "Filter: t.id > 2", "inputs": [leaf]},
                {"access_type": "join", "operation": "Nested loop inner join",
                 "join_algorithm": "nested_loop", "inputs": [leaf, {
                     "access_type": "index", "index_access_type": "index_lookup",
                     "operation": "Single-row index lookup on labels using PRIMARY",
                     "table_name": "labels", "index_name": "PRIMARY"}]}):
            with self.subTest(parent=intermediate["access_type"]):
                source = limit_raw(intermediate)
                before = copy.deepcopy(source)
                root = parse_node(source)
                self.assertIsNone(knn_nodes(root)[0]["details"].get("limit"))
                self.assertEqual(source, before)
                if intermediate["access_type"] == "join":
                    children = root["children"][0]["children"]
                    self.assertTrue(is_knn_scan(children[0]["details"]))
                    self.assertEqual(children[1]["details"]["index_name"], "PRIMARY")
        self.assertNotIn("limit", parsed(leaf)["details"])

    def test_leaf_limit_and_explicit_index_win_and_zero_parent_limit_is_preserved(self):
        node = knn_nodes(parsed(limit_raw(released_knn_raw(limit=3, index_name="reported_idx"))))[0]
        self.assertEqual(node["details"]["limit"], 3)
        self.assertEqual(node["details"]["index_name"], "reported_idx")
        node = knn_nodes(parsed(limit_raw(released_knn_raw(), count=0)))[0]
        self.assertEqual(node["details"]["limit"], 0)
        self.assertEqual(node["short_label"], "KNN SCAN [idx_v] K=0")
        for count in (-1, 18446744073709551615):
            with self.subTest(unbounded=count):
                node = knn_nodes(parsed(limit_raw(released_knn_raw(), count=count)))[0]
                self.assertNotIn("limit", node["details"])
                self.assertNotIn("K=", node["folded_label"])
        for count in (1.5, True, "10", [], {}):
            with self.subTest(count=count):
                with self.assertRaisesRegex(ValueError, "KNN limit"):
                    parsed(limit_raw(released_knn_raw(), count=count))

    def test_misestimate_early_stop_matches_other_index_scans_without_blanket_suppression(self):
        # Genuine 0.0.8 v2 captures omit table_name, which independently
        # excludes statistics advice. Supply one here to exercise the
        # existing early-stop rule and keep unrelated misestimates visible.
        leaf = released_knn_raw(table_name="t")
        analysis = analyze_plan(parsed(leaf))
        self.assertEqual(len(analysis["misestimates"]), 1)
        self.assertFalse(analysis["misestimates"][0]["under"])
        for child in (leaf, {"access_type": "filter", "operation": "Filter: t.id > 2",
                             "inputs": [leaf]}):
            root = parsed(limit_raw(child))
            self.assertEqual(analyze_plan(root)["misestimates"], [])
            self.assertTrue(all("misestimate" not in node["details"] for node in flatten_nodes(root)))
        underestimated = released_knn_raw(table_name="t", estimated_rows=1.0, actual_rows=2005.0)
        analysis = analyze_plan(parsed(limit_raw(underestimated, count=5000)))
        self.assertEqual(len(analysis["misestimates"]), 1)
        self.assertTrue(analysis["misestimates"][0]["under"])

    def test_hostile_index_operation_is_data_for_advice_and_all_svg_views(self):
        for index in ("idx join hash sort", "idx hash join sort LOWER(t.c)",
                      'idx<script>knn-released-injected</script>&"\''):
            with self.subTest(index=index):
                root = parsed(limit_raw(released_knn_raw(
                    operation="Custom index distance scan on " + index)))
                node = knn_nodes(root)[0]
                self.assertEqual(node["details"]["index_name"], index)
                self.assertEqual(node["details"]["table_name"], "")
                self.assertEqual(_kind(node), "index")
                self.assertEqual(operator_family(node["details"]), "index_scan")
                analysis = analyze_plan(root)
                advise(analysis, schema={})
                for key in ("hash_joins", "filesorts", "full_scans", "misestimates",
                            "nonsargable_joins", "optimizer_switches", "optimizer_features",
                            "index_suggestions"):
                    self.assertEqual(analysis[key], [], key)
                meaning = "Up to 10 nearest rows requested through custom index " + index
                for view in ("flamegraph", "bargraph", "tree", "diagram", "workbench"):
                    with self.subTest(view=view):
                        svg = _render_svg(root, view, 1440, "KNN plan", "ms")
                        document = ET.fromstring(svg)
                        self.assertIn(meaning, svg_payload(document))
                        self.assertNotIn("<script>knn-released-injected", svg)
                        for script in document.iter(SVG + "script"):
                            self.assertNotIn("knn-released-injected", script.text or "")

    def test_released_live_analyze_plans_keep_meaning_in_every_svg_view_at_both_widths(self):
        for path, _scenario, _plan_format in released_fixtures(("s1", "s3", "s5"), ("analyze-json-v2",)):
            root = parse_explain(path.read_text(encoding="utf-8"))
            meaning = knn_description(knn_nodes(root)[0]["details"])
            for width in (720, 1440):
                for view in ("flamegraph", "bargraph", "tree", "diagram", "workbench"):
                    with self.subTest(fixture=path.name, width=width, view=view):
                        document = ET.fromstring(_render_svg(root, view, width, "KNN plan", "ms"))
                        self.assertIn(meaning, svg_payload(document))
                        self.assertEqual(float(document.attrib["height"]),
                                         float(document.attrib["viewBox"].split()[3]))


class TestVillageSQLKnnPresentation(unittest.TestCase):
    def test_large_points_are_bounded_in_every_display_without_changing_source_json(self):
        for name, point, counts in long_knn_cases():
            with self.subTest(point=name):
                raw = raw_with_point(point)
                original_raw = copy.deepcopy(raw)
                root = parsed(raw)
                original_root = copy.deepcopy(root)
                preview = knn_lookup_preview(root["details"])
                self.assertTrue(preview.startswith("embedding <-> " + point[:12]))
                self.assertIn("…", preview)
                self.assertTrue(any(count in preview for count in counts), preview)
                self.assertLess(len(preview), 250)
                meaning = knn_description(root["details"])
                self.assertEqual(meaning, "Up to 5 nearest rows requested by %s through custom index idx_embedding" % preview)
                displays = {
                    "full_label": root["full_label"],
                    "flame tooltip": enhance_tooltip_flame(FOLDED, {FOLDED: root["details"]}),
                    "inspector": _details(root),
                }
                for field, text in displays.items():
                    with self.subTest(display=field):
                        self.assertIn(preview, text)
                        self.assertNotIn(point, text)
                        self.assertLess(len(text), 2000)
                for compact, schema in ((False, SCHEMA), (True, COMPACT_SCHEMA)):
                    data = build_chart_data(root, compact=compact)
                    validate(data, schema)
                    row = data["nodes"][0]
                    self.assertEqual(row["lookup_condition"], preview)
                    self.assertEqual(row["access_description"], meaning)
                    for field in ("operation", "full_label", "details_text", "flame_details"):
                        with self.subTest(compact=compact, field=field):
                            self.assertIn(preview, row[field])
                            self.assertLess(len(row[field]), 2000)
                    self.assertNotIn(point, json.dumps(data, ensure_ascii=False))
                self.assertEqual(root["details"]["lookup_condition"], preview)
                self.assertIn(preview, root["details"]["operation"])
                self.assertNotIn(point, json.dumps(root, ensure_ascii=False))
                self.assertEqual(root, original_root)
                # Exercise the object-taking entry too: normalization must
                # copy its input, not overwrite the caller's original plan.
                self.assertEqual(parse_node(raw)["details"]["lookup_condition"], preview)
                self.assertEqual(raw, original_raw)

    def test_point_preview_boundary_and_repeated_projection_are_stable(self):
        for length in (127, 128, 129):
            with self.subTest(length=length):
                point = "'" + "x" * (length - 2) + "'"
                raw = raw_with_point(point)
                preview = knn_lookup_preview(raw)
                if length <= 128:
                    self.assertEqual(preview, raw["lookup_condition"])
                else:
                    self.assertIn("…", preview)
                    self.assertIn("129 characters", preview)
                    self.assertLessEqual(len(preview.split(" <-> ", 1)[1]), 128)
                again = dict(raw, lookup_condition=preview)
                self.assertEqual(knn_lookup_preview(again), preview)
                root = parsed(raw)
                self.assertEqual(root["details"]["lookup_condition"], preview)
                self.assertEqual(knn_lookup_preview(root["details"]), preview)

    def test_repeated_point_in_index_annotations_and_expressions_stays_bounded(self):
        for name, point, _counts in long_knn_cases():
            with self.subTest(point=name):
                raw = raw_with_point(point)
                expression = "distance(embedding, %s) < 1" % point
                raw["operation"] += ", with index condition: " + expression
                raw["condition"] = expression
                for field in ("ranges", "hash_condition", "sort_fields"):
                    raw[field] = [expression]
                original = copy.deepcopy(raw)
                root = parsed(raw)
                preview = knn_lookup_preview(root["details"])
                self.assertIn(", with index condition: distance(embedding, ", root["full_label"])
                for field in ("operation", "condition", "ranges", "hash_condition", "sort_fields"):
                    value = root["details"][field]
                    text = value if isinstance(value, str) else " ".join(value)
                    self.assertNotIn(point, text)
                    self.assertIn("…", text)
                    self.assertLess(len(text), 2000)
                self.assertNotIn(point, json.dumps(root, ensure_ascii=False))
                self.assertLess(len(root["full_label"]), 2000)
                for compact, schema in ((False, SCHEMA), (True, COMPACT_SCHEMA)):
                    data = build_chart_data(root, compact=compact)
                    validate(data, schema)
                    self.assertNotIn(point, json.dumps(data, ensure_ascii=False))
                    self.assertLess(len(data["nodes"][0]["details_text"]), 3000)
                for width in (720, 1440):
                    for view in ("flamegraph", "bargraph", "tree", "diagram", "workbench"):
                        with self.subTest(width=width, view=view):
                            document = ET.fromstring(_render_svg(root, view, width, "KNN plan", "ms"))
                            text = svg_payload(document)
                            self.assertIn(preview, text)
                            self.assertNotIn(point, text)
                            self.assertEqual(float(document.attrib["height"]),
                                             float(document.attrib["viewBox"].split()[3]))
                self.assertEqual(parse_node(raw)["details"], root["details"])
                self.assertEqual(raw, original)

    def test_large_point_previews_survive_every_svg_view_at_both_widths(self):
        for name, point, _counts in long_knn_cases():
            root = parsed(raw_with_point(point))
            expected = knn_description(root["details"])
            for width in (720, 1440):
                for view in ("flamegraph", "bargraph", "tree", "diagram", "workbench"):
                    with self.subTest(point=name, width=width, view=view):
                        document = ET.fromstring(_render_svg(root, view, width, "KNN plan", "ms"))
                        text = svg_payload(document)
                        self.assertIn(expected, text)
                        self.assertNotIn(point, text)
                        self.assertEqual(float(document.attrib["height"]),
                                         float(document.attrib["viewBox"].split()[3]))

    def test_short_points_stay_exact_and_long_points_are_redacted_before_previewing(self):
        self.assertEqual(knn_lookup_preview(knn_raw()), LOOKUP)
        for name, point, _counts in long_knn_cases():
            with self.subTest(point=name):
                root = parsed(raw_with_point(point))
                hidden = hide_parsed_plan_literals(root)
                self.assertEqual(hidden["details"]["lookup_condition"], "embedding <-> ?")
                self.assertEqual(knn_lookup_preview(hidden["details"]), "embedding <-> ?")
                for compact in (False, True):
                    row = build_chart_data(hidden, compact=compact)["nodes"][0]
                    self.assertEqual(row["lookup_condition"], "embedding <-> ?")
                    for field in ("full_label", "operation", "access_description", "details_text", "flame_details"):
                        self.assertNotIn(point[:12], row[field])
                        self.assertNotIn("characters)", row[field])
                        self.assertNotIn("values)", row[field])

    def test_truncated_points_are_private_for_every_recognized_subtype_case(self):
        # Truncation ends exactly after the exponent marker. Treating the
        # preview as original SQL can misclassify the remaining "e" as a name.
        number = "7" * 109 + "e" + "2" * 90
        cases = [("unquoted exponent", number)] + [
            (name, point) for name, point, _counts in long_knn_cases()]
        for subtype in ("index_knn_scan", "INDEX_KNN_SCAN", "Index_Knn_Scan"):
            for name, point in cases:
                with self.subTest(subtype=subtype, point=name):
                    raw = raw_with_point(point)
                    raw["index_access_type"] = subtype
                    root = parsed(raw)
                    if name == "unquoted exponent":
                        self.assertIn("e… (200 characters)", root["details"]["lookup_condition"])
                    self.assertEqual(operator_family(root["details"]), "index_scan")
                    hidden = hide_parsed_plan_literals(root)
                    self.assertEqual(hidden["details"]["lookup_condition"], "embedding <-> ?")
                    for compact in (False, True):
                        row = build_chart_data(hidden, compact=compact)["nodes"][0]
                        self.assertEqual(row["lookup_condition"], "embedding <-> ?")
                        for field in ("full_label", "operation", "access_description", "details_text", "flame_details"):
                            self.assertNotIn(point[:12], row[field])
                            self.assertNotIn("e…", row[field])
                            self.assertNotIn("characters)", row[field])
                            self.assertNotIn("values)", row[field])

    def test_tooltip_and_inspector_explain_custom_index_nearest_rows(self):
        node = parsed(knn_raw())
        tooltip = enhance_tooltip_flame(FOLDED, {FOLDED: node["details"]})
        self.assertIn(MEANING, tooltip)
        self.assertIn(MEANING, _details(node))
        for compact in (False, True):
            row = build_chart_data(node, compact=compact)["nodes"][0]
            self.assertIn(MEANING, row["flame_details"])
            self.assertIn(MEANING, row["details_text"])

    def test_chart_data_carries_limit_without_mutating_the_tree(self):
        root = parsed(knn_raw())
        before = copy.deepcopy(root)
        for compact, schema in ((False, SCHEMA), (True, COMPACT_SCHEMA)):
            with self.subTest(compact=compact):
                data = build_chart_data(root, compact=compact)
                validate(data, schema)
                row = data["nodes"][0]
                self.assertEqual(row["limit"], 5)
                self.assertEqual(row["index_access_type"], "index_knn_scan")
                self.assertEqual(row["index"], "idx_embedding")
                self.assertEqual(row["lookup_condition"], LOOKUP)
                self.assertEqual(row["access_description"], MEANING)
                self.assertIs(row["covering"], False)
                self.assertEqual(row["folded_label"], FOLDED)
                self.assertEqual(row["family"], "index_scan")
                self.assertEqual(row["wb_kind"], "index")
                self.assertIsNone(row["complexity"])
                self.assertEqual(json.loads(json.dumps(data, allow_nan=False)), data)
        self.assertEqual(root, before)

    def test_source_fixtures_validate_both_chart_schemas_and_keep_node_order(self):
        for path in FIXTURES:
            with self.subTest(fixture=path.name):
                root = parse_explain(path.read_text(encoding="utf-8"))
                ordered_ids = [node["node_id"] for node in flatten_nodes(root)]
                for compact, schema in ((False, SCHEMA), (True, COMPACT_SCHEMA)):
                    data = build_chart_data(root, compact=compact)
                    validate(data, schema)
                    self.assertEqual([row["id"] for row in data["nodes"]], ordered_ids)
                    rows = [row for row in data["nodes"]
                            if row["index_access_type"] == "index_knn_scan"]
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["limit"], knn_nodes(root)[0]["details"]["limit"])

    def test_every_svg_view_preserves_the_knn_meaning(self):
        root = parsed(knn_raw())
        for view in ("flamegraph", "bargraph", "tree", "diagram", "workbench"):
            with self.subTest(view=view):
                svg = _render_svg(root, view, 1440, "KNN plan", "ms")
                document = ET.fromstring(svg)
                self.assertIn(MEANING, svg_payload(document))
                self.assertEqual(float(document.attrib["height"]),
                                 float(document.attrib["viewBox"].split()[3]))

    def test_workbench_draws_knn_as_index_access(self):
        root = parsed(knn_raw())
        self.assertEqual(_kind(root), "index")
        document = ET.fromstring(render_workbench(root))
        nodes = [element for element in document.iter(SVG + "g")
                 if element.attrib.get("class") == "wb-node"]
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].attrib["data-kind"], "index")
        self.assertIn(MEANING, nodes[0].attrib["data-details"])
        self.assertNotIn("Full table scan", nodes[0].attrib["data-details"])

    def test_untrusted_lookup_text_is_literal_in_every_svg_view(self):
        hostile = '<script>knn-injected</script>&"\''
        root = parsed(knn_raw(lookup_condition="embedding <-> " + hostile))
        expected = "Up to 5 nearest rows requested by embedding <-> " + hostile + " through custom index idx_embedding"
        for view in ("flamegraph", "bargraph", "tree", "diagram", "workbench"):
            with self.subTest(view=view):
                svg = _render_svg(root, view, 1440, "KNN plan", "ms")
                document = ET.fromstring(svg)
                self.assertIn(expected, svg_payload(document))
                self.assertNotIn("<script>knn-injected", svg)
                for script in document.iter(SVG + "script"):
                    self.assertNotIn("knn-injected", script.text or "")

    def test_shared_knn_plan_redacts_lookup_literals_and_keeps_knn_labels(self):
        raw = knn_raw(lookup_condition="embedding <-> 'private-vector-123'")
        root = parsed(raw)
        hidden = hide_parsed_plan_literals(root)
        self.assertEqual(hidden["details"]["lookup_condition"], "embedding <-> ?")
        self.assertEqual(hidden["short_label"], "KNN SCAN [vectors.idx_embedding] K=5")
        self.assertEqual(hidden["folded_label"], FOLDED)
        self.assertEqual(hidden["node_id"], root["node_id"])
        self.assertEqual(hidden["total_time"], root["total_time"])
        self.assertIn("private-vector-123", root["details"]["lookup_condition"])
        for compact in (False, True):
            self.assertNotIn("private-vector-123",
                             json.dumps(build_chart_data(hidden, compact=compact)))
        raw_hidden = json.loads(hide_plan_json_literals(json.dumps(raw)))
        self.assertEqual(raw_hidden["lookup_condition"], "embedding <-> ?")


class TestVillageSQLKnnFindings(unittest.TestCase):
    def test_knn_identifiers_and_point_literals_are_not_optimizer_or_join_evidence(self):
        # Each payload is short enough to survive the point preview in full,
        # so truncation cannot accidentally make a broken classifier pass.
        hostiles = (
            "join sort filter LOWER(t.c)", "hash join LOWER(t.c)",
            "nested loop join LOWER(t.c)", "semijoin LOWER(t.c)", "antijoin LOWER(t.c)",
            "batch_key_access", "batched key access", "multi-range", "skip scan",
            "sort-deduplicate by row id", "deduplicate rows sorted by row id",
            "intersect rows sorted by row id", "first match", "loose scan", "weedout",
            "remove duplicate", "hash union", "hash intersect", "hash except",
        )
        for hostile in hostiles:
            for field in ("table_name", "index_name", "lookup_condition"):
                with self.subTest(field=field, text=hostile):
                    raw = knn_raw()
                    raw[field] = "embedding <-> '%s'" % hostile if field == "lookup_condition" else hostile
                    raw["operation"] = "Index KNN scan on %s using %s (%s), limit 5" % (
                        raw["table_name"], raw["index_name"], raw["lookup_condition"])
                    root = parsed(raw)
                    self.assertEqual(_detect_optimizer_switches(root), [])
                    self.assertEqual(_detect_nonsargable_joins(root), [])
                    analysis = analyze_plan(root)
                    for key in ("hash_joins", "filesorts", "temp_tables", "full_scans",
                                "nonsargable_joins", "optimizer_switches", "optimizer_features"):
                        self.assertEqual(analysis[key], [], key)
                    self.assertEqual(operator_family(root["details"]), "index_scan")
                    self.assertEqual(_kind(root), "index")
                    self.assert_no_scan_findings(root)

    def test_real_join_around_knn_retains_its_features_and_nonsargable_predicate(self):
        root = parsed({
            "operation": "Nested loop inner join (LOWER(t.c) = u.c)",
            "access_type": "join", "join_algorithm": "nested_loop",
            "condition": "LOWER(t.c) = u.c",
            "inputs": [knn_raw(), {
                "operation": "Inner hash join (no condition)", "access_type": "join",
                "join_algorithm": "hash", "inputs": [
                    {"operation": "Table scan on u", "access_type": "table", "table_name": "u"},
                    {"operation": "Table scan on v", "access_type": "table", "table_name": "v"},
                ],
            }],
        })
        analysis = analyze_plan(root)
        self.assertEqual(len(analysis["hash_joins"]), 1)
        self.assertEqual([item["name"] for item in analysis["optimizer_switches"]], ["hash_join"])
        self.assertEqual([item["function"] for item in analysis["nonsargable_joins"]], ["LOWER"])
        self.assertEqual(analysis["nonsargable_joins"][0]["predicate"], "LOWER(t.c) = u.c")
        self.assertIn("nested loop join", analysis["optimizer_features"])

    def test_non_knn_text_fallbacks_keep_existing_optimizer_and_join_detections(self):
        cases = (("Inner hash join (no condition)", "hash_join"),
                 ("Batched key access inner join", "batched_key_access"),
                 ("Index scan with multi-range read", "mrr"),
                 ("Index skip scan on t using idx", "skip_scan"),
                 ("Sort-deduplicate by row ID", "index_merge_sort_union"),
                 ("Nested loop semijoin", "semijoin"),
                 ("Hash union", "hash_set_operations"))
        for operation, flag in cases:
            with self.subTest(operation=operation):
                root = parsed({"operation": operation})
                self.assertIn(flag, [item["name"] for item in _detect_optimizer_switches(root)])
        root = parsed({"operation": "Nested loop join (LOWER(t.c) = u.c)"})
        self.assertEqual([item["function"] for item in _detect_nonsargable_joins(root)], ["LOWER"])

    def assert_no_scan_findings(self, root):
        analysis = analyze_plan(root)
        advise(analysis, schema={"vectors": {"engine": "InnoDB", "indexes": []}})
        self.assertEqual(analysis["full_scans"], [])
        self.assertEqual(analysis["index_suggestions"], [])
        text = json.dumps({key: analysis.get(key) for key in (
            "warnings", "suggestions", "node_highlights", "environment_findings")}).lower()
        for claim in ("full table scan", "full index scan", "missing index",
                      "no index covering", "reads every index entry", "create index"):
            self.assertNotIn(claim, text)

    def test_knn_has_no_scan_or_missing_index_findings(self):
        self.assert_no_scan_findings(parsed(knn_raw()))
        for path in FIXTURES:
            with self.subTest(fixture=path.name):
                self.assert_no_scan_findings(parse_explain(path.read_text(encoding="utf-8")))

    def test_filter_above_knn_does_not_recommend_an_ordinary_index(self):
        root = parsed({"operation": "Filter: (vectors.category = 7)",
                       "access_type": "filter", "condition": "vectors.category = 7",
                       "inputs": [knn_raw()]})
        self.assert_no_scan_findings(root)

    def test_sort_above_knn_does_not_claim_a_btree_replaces_the_custom_index(self):
        root = parsed({"operation": "Sort: vectors.category", "access_type": "sort",
                       "sort_fields": ["vectors.category"], "inputs": [knn_raw()]})
        self.assert_no_scan_findings(root)

    def test_knn_and_covering_knn_have_no_complexity_formula(self):
        for covering in (False, True):
            with self.subTest(covering=covering):
                node = parsed(knn_raw(covering=covering))
                self.assertIsNone(compute_complexity(node))
                self.assertNotIn("complexity", node["details"])
                for field in ("details_text", "flame_details"):
                    self.assertNotIn("O(", build_chart_data(node)["nodes"][0][field])

    def test_knn_inner_join_does_not_invent_a_linear_inner_scan_cost(self):
        root = parsed({
            "operation": "Nested loop inner join", "access_type": "join",
            "join_algorithm": "nested_loop", "join_type": "inner join",
            "inputs": [{"operation": "Table scan on outer_rows", "access_type": "table",
                        "table_name": "outer_rows"}, knn_raw()],
        })
        self.assertIsNone(compute_complexity(root))
        self.assertNotIn("complexity", root["details"])

    def test_upstream_distance_scan_retains_existing_index_scan_behavior(self):
        raw = knn_raw(operation="Index distance scan on places using idx_location",
                      index_access_type="index_distance_scan", table_name="places",
                      index_name="idx_location")
        raw.pop("limit")
        raw.pop("lookup_condition")
        node = parsed(raw)
        self.assertEqual(operator_family(node["details"]), "index_scan")
        self.assertEqual(_kind(node), "index")
        self.assertEqual(node["folded_label"],
                         "Index distance scan on places using idx_location starts=1 rows=5")
        self.assertEqual(compute_complexity(node)["big_o"], "O(n)")
        self.assertNotIn("Nearest", _details(node))
        for compact, schema in ((False, SCHEMA), (True, COMPACT_SCHEMA)):
            data = build_chart_data(node, compact=compact)
            self.assertNotIn("limit", data["nodes"][0])
            validate(data, schema)

    def test_mysql_and_mariadb_table_scans_keep_their_existing_findings(self):
        plans = (
            {"operation": "Table scan on vectors", "access_type": "table",
             "table_name": "vectors", "condition": "vectors.category = 7",
             "actual_rows": 1000.0, "estimated_rows": 1000.0,
             "actual_loops": 1, "actual_last_row_ms": 1.0},
            {"query_block": {"select_id": 1, "table": {
                "table_name": "vectors", "access_type": "ALL", "rows": 1000,
                "attached_condition": "vectors.category = 7"}}},
        )
        for engine, raw in zip(("mysql", "mariadb"), plans):
            with self.subTest(engine=engine):
                analysis = analyze_plan(parsed(raw))
                advise(analysis, schema={"vectors": {"engine": "InnoDB", "indexes": []}})
                self.assertEqual([scan["table"] for scan in analysis["full_scans"]], ["vectors"])
                self.assertEqual([hint["columns"] for hint in analysis["index_suggestions"]],
                                 [["category"]])
                self.assertIn("no index covering", " ".join(analysis["environment_warnings"]))


if __name__ == "__main__":
    unittest.main()
