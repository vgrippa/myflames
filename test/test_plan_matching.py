"""Structural comparison identities, ambiguity and machine-readable metadata."""
import copy
import json
import unittest

from myflames.parser import parse_explain, flatten_nodes
from myflames.plan_matching import match_plan_nodes
from myflames.output_compare import _match_nodes, render_compare
from myflames.output_compare_sidecar import build_compare_sidecar


def scan(table, index=None, duration=1):
    value = {"operation": "Table scan on " + table, "table_name": table,
             "access_type": "table", "actual_last_row_ms": duration,
             "actual_rows": 10, "actual_loops": 1}
    if index:
        value.update(operation="Index lookup on " + table + " using " + index,
                     index_name=index, access_type="index", index_access_type="ref")
    return value


def wrap(operation, children, **details):
    value = {"operation": operation, "inputs": children, "actual_last_row_ms": 20,
             "actual_rows": 10, "actual_loops": 1}
    value.update(details)
    return value


def compare(before, after):
    return build_compare_sidecar(json.dumps(before), json.dumps(after))


class TestPlanMatching(unittest.TestCase):
    def test_reordered_branches_match_semantics_not_occurrence(self):
        left = wrap("Filter: a=1", [scan("t")], condition="a=1")
        right = wrap("Filter: a=2", [scan("t", duration=2)], condition="a=2")
        before = wrap("Append", [left, right])
        after = copy.deepcopy(before)
        after["inputs"].reverse()
        result = compare(before, after)
        scans = [item for item in result["deltas"] if item["short_label"] == "Table scan [t]"]
        self.assertEqual(len(scans), 2)
        self.assertEqual([item["self_time_ms"]["after"] for item in scans], [1, 2])
        self.assertTrue(all(item["matching"]["method"] == "parent_context" for item in scans))
        self.assertEqual(result["summary"]["regressions"], 0)

    def test_indistinguishable_repeats_are_uncertain_even_with_same_paths(self):
        before = wrap("Nested loop inner join", [scan("t"), scan("t")])
        after = copy.deepcopy(before)
        after["inputs"][1]["actual_last_row_ms"] = 5
        result = compare(before, after)
        repeated = result["deltas"][1:]
        self.assertEqual(len(repeated), 2)
        self.assertTrue(all(item["classification"] == "uncertain" for item in repeated))
        self.assertEqual(result["summary"]["uncertain"], 2)
        self.assertEqual(result["summary"]["regressions"], 0)
        html = render_compare(json.dumps(before), json.dumps(after))
        self.assertIn("UNCERTAIN MATCH", html)
        self.assertIn('data-match-confidence="uncertain"', html)

    def test_access_path_change_is_linked_by_table(self):
        result = compare(scan("users", duration=10), scan("users", "country", duration=1))
        self.assertEqual(len(result["deltas"]), 1)
        item = result["deltas"][0]
        self.assertEqual(item["matching"], {"method": "unique_table", "confidence": "medium", "status": "changed"})
        self.assertEqual(item["classification"], "improved")
        self.assertNotEqual(item["before_label"], item["after_label"])
        self.assertTrue(item["before_node_id"].startswith("n:"))
        self.assertTrue(item["after_node_id"].startswith("n:"))

    def test_same_table_name_different_schema_is_not_a_match(self):
        before, after = scan("users"), scan("users", "country")
        before["schema_name"], after["schema_name"] = "prod", "archive"
        result = compare(before, after)
        self.assertEqual([item["matching"]["status"] for item in result["deltas"]], ["removed", "added"])

    def test_changed_predicate_follows_matched_child(self):
        before = wrap("Filter: a=1", [scan("t")], condition="a=1")
        after = wrap("Filter: a=2", [scan("t")], condition="a=2")
        result = compare(before, after)
        self.assertEqual(result["deltas"][0]["matching"], {"method": "child_context", "confidence": "medium", "status": "changed"})

    def test_added_removed_and_unique_reordered_subtree(self):
        before = wrap("Append", [scan("users"), scan("removed")])
        after = wrap("Append", [scan("added"), scan("users")])
        result = compare(before, after)
        self.assertEqual(result["summary"]["added"], 1)
        self.assertEqual(result["summary"]["removed"], 1)
        user = next(item for item in result["deltas"] if item["short_label"] == "Table scan [users]")
        self.assertEqual(user["matching"]["method"], "unique_subtree")
        self.assertNotEqual(user["before_node_id"], user["after_node_id"])

    def test_every_node_appears_once_and_inputs_are_unchanged(self):
        root_b = parse_explain(json.dumps(wrap("Append", [scan("t"), scan("t"), scan("x")])))
        root_a = parse_explain(json.dumps(wrap("Append", [scan("t"), scan("y")])))
        old_b, old_a = copy.deepcopy(root_b), copy.deepcopy(root_a)
        nodes_b, nodes_a = list(flatten_nodes(root_b)), list(flatten_nodes(root_a))
        matches = match_plan_nodes(nodes_b, nodes_a)
        self.assertEqual([id(item["before"]) for item in matches if item["before"]], [id(node) for node in nodes_b])
        self.assertEqual(sorted(id(item["after"]) for item in matches if item["after"]), sorted(id(node) for node in nodes_a))
        self.assertEqual(root_b, old_b)
        self.assertEqual(root_a, old_a)
        self.assertTrue(all(len(item) == 3 for item in _match_nodes(nodes_b, nodes_a)))

    def test_json_roundtrip_metadata_and_html_escape(self):
        plan = scan('<script>unsafe</script>')
        result = compare(plan, plan)
        self.assertEqual(json.loads(json.dumps(result)), result)
        item = result["deltas"][0]
        self.assertEqual(item["matching"]["status"], "unchanged")
        html = render_compare(json.dumps(plan), json.dumps(plan))
        self.assertNotIn('<script>unsafe</script>', html)
        self.assertIn('data-before-node-id="' + item["before_node_id"] + '"', html)

    def test_unique_operator_timing_change_still_classifies(self):
        result = compare(scan("users", duration=1), scan("users", duration=2))
        self.assertEqual(result["summary"]["regressions"], 1)
        self.assertEqual(result["deltas"][0]["matching"]["status"], "unchanged")


class TestComparisonMeasurementAvailability(unittest.TestCase):
    def estimate(self):
        return {"operation": "Table scan on users", "table_name": "users",
                "access_type": "table", "estimated_rows": 100,
                "estimated_total_cost": 15}

    def test_estimate_vs_analyze_never_claims_measured_improvement(self):
        estimated, measured = self.estimate(), scan("users", duration=2)
        for before, after in ((estimated, measured), (measured, estimated)):
            with self.subTest(before_measured="actual_last_row_ms" in before):
                result = compare(before, after)
                self.assertFalse(result["summary"]["timing_available"])
                self.assertEqual(result["summary"]["time_delta_ms"], 0)
                self.assertIsNone(result["summary"]["time_delta_pct"])
                self.assertEqual(result["summary"]["regressions"], 0)
                self.assertEqual(result["summary"]["improvements"], 0)
                self.assertEqual(result["summary"]["unmeasured"], 1)
                delta = result["deltas"][0]
                self.assertFalse(delta["measured"])
                self.assertEqual(delta["classification"], "unmeasured")
                side = "before" if before is estimated else "after"
                self.assertFalse(result[side]["timing_available"])
                self.assertIsNone(delta["self_time_ms"][side])
                self.assertFalse(delta["self_time_ms"][side + "_measured"])
                self.assertIsNone(delta["rows"][side])
                self.assertIsNone(delta["rows"]["change_pct"])
                html = render_compare(json.dumps(before), json.dumps(after))
                self.assertIn('Timing comparison unavailable', html)
                self.assertIn('n/a', html)
                self.assertNotIn('Query got faster', html)
                self.assertNotIn('Query got slower', html)
                self.assertNotIn('Query time unchanged', html)

    def test_estimates_compared_to_estimates_are_not_measured_zero(self):
        result = compare(self.estimate(), self.estimate())
        self.assertFalse(result["before"]["timing_available"])
        self.assertFalse(result["after"]["timing_available"])
        self.assertEqual(result["deltas"][0]["classification"], "unmeasured")
        self.assertEqual(result["summary"]["unchanged"], 0)

    def test_measured_zero_remains_available_and_classifiable(self):
        zero = scan("users", duration=0)
        result = compare(zero, zero)
        self.assertTrue(result["before"]["timing_available"])
        self.assertTrue(result["after"]["timing_available"])
        self.assertTrue(result["deltas"][0]["measured"])
        self.assertEqual(result["deltas"][0]["classification"], "unchanged")
        self.assertEqual(result["deltas"][0]["self_time_ms"]["before"], 0)
        changed = compare(zero, scan("users", duration=1))
        self.assertEqual(changed["summary"]["regressions"], 1)
        self.assertIsNone(changed["summary"]["time_delta_pct"])
        self.assertTrue(changed["summary"]["timing_available"])
        html = render_compare(json.dumps(zero), json.dumps(zero))
        self.assertIn('Query time unchanged', html)
        self.assertNotIn('Timing comparison unavailable', html)

    def test_partial_timing_does_not_invent_self_time(self):
        before = wrap("Filter: a=1", [self.estimate()], condition="a=1")
        after = wrap("Filter: a=1", [scan("users")], condition="a=1")
        result = compare(before, after)
        self.assertTrue(result["before"]["timing_available"])
        self.assertTrue(result["summary"]["timing_available"])
        self.assertIsNone(result["deltas"][0]["self_time_ms"]["before"])
        self.assertEqual(result["deltas"][0]["classification"], "unmeasured")

    def test_availability_metadata_roundtrips(self):
        result = compare(self.estimate(), scan("users", duration=0))
        self.assertEqual(json.loads(json.dumps(result)), result)


if __name__ == "__main__":
    unittest.main()
