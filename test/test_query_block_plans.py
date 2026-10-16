"""query_block plans: MariaDB ANALYZE/EXPLAIN FORMAT=JSON and MySQL
explain_json_format_version=1.

Fixtures captured from MariaDB 11.4.13 / 11.8.9 / 13.0.2 and MySQL 8.4.11
(sakila) during the 2026-10 compatibility pass. Covers node preservation,
set-operation labels, message-only selects, estimate-only timing, and the
advisor wording that depends on these plans.
"""
import collections
import glob
import json
import os
import sys
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(TEST_DIR, "fixtures")
sys.path.insert(0, os.path.dirname(TEST_DIR))

from myflames.output_compare import render_compare  # noqa: E402
from myflames.output_compare_sidecar import build_compare_sidecar  # noqa: E402
from myflames.output_sidecar import build_sidecar, validate_sidecar  # noqa: E402
from myflames.parser import (  # noqa: E402
    _collect_misestimates, analyze_plan, build_short_label, flatten_nodes,
    load_explain_json, parse_explain,
)
from myflames.plan_matching import timing_available  # noqa: E402


def _text(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def _parse(name):
    return parse_explain(_text(name))


def _tables(root):
    return [n["details"]["table_name"] for n in flatten_nodes(root) if n["details"]["table_name"]]


def _raw_table_names(data):
    names = collections.Counter()
    stack = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                if key == "table_name" and isinstance(value, str):
                    names[value] += 1
                elif isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(item, list):
            stack.extend(item)
    return names


def _query_block_fixtures():
    paths = sorted(glob.glob(os.path.join(FIXTURES, "*.json")) + glob.glob(os.path.join(TEST_DIR, "*.json")))
    out = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            data = load_explain_json(f.read())
        if isinstance(data, dict) and "query_block" in data:
            out.append((path, data))
    return out


class TestNodePreservation(unittest.TestCase):
    def test_every_raw_table_name_is_in_the_parsed_tree(self):
        """Every table_name in a query_block plan (base tables, aliases,
        <derivedN>, <subqueryN>, <union1,2>, ...) appears in the parsed tree
        at least as often as in the raw JSON."""
        checked = 0
        for path, data in _query_block_fixtures():
            with self.subTest(fixture=os.path.basename(path)):
                root = parse_explain(json.dumps(data))
                parsed = collections.Counter(_tables(root))
                missing = {name: (count, parsed[name])
                           for name, count in _raw_table_names(data).items()
                           if parsed[name] < count}
                self.assertEqual(missing, {})
                checked += 1
        self.assertGreater(checked, 80)


class TestMariaDBStructures(unittest.TestCase):
    def test_top_level_temporary_table_parses(self):
        # SELECT DISTINCT district FROM address: query_block.temporary_table
        root = _parse("mariadb-13.0-distinct-temporary-table.json")
        self.assertEqual(root["engine"], "mariadb")
        self.assertEqual(root["details"]["operation"], "Temporary table")
        self.assertEqual(root["details"]["access_type"], "materialize")
        self.assertEqual(_tables(root), ["address"])
        self.assertTrue(timing_available(root))
        self.assertAlmostEqual(root["total_time"], 0.227724726)
        self.assertEqual(len(analyze_plan(root)["temp_tables"]), 1)

    def test_nested_filesort_temporary_table_chain_keeps_the_join(self):
        # filesort > temporary_table > filesort > temporary_table > nested_loop
        root = _parse("mariadb-11.4-029-complex-5t-aggregate.json")
        ops = [n["details"]["operation"] for n in flatten_nodes(root)]
        self.assertEqual(ops[:2], ["Sort: sum(oi.quantity * oi.unit_price) desc", "Sort: u.country"])
        self.assertEqual(sorted(_tables(root)), ["o", "oi", "u"])
        self.assertEqual(len(list(flatten_nodes(root))), 7)
        self.assertEqual(root["children"][0]["details"]["sort_fields"], ["u.country"])

    def test_having_subquery_is_kept(self):
        root = _parse("mariadb-11.8-subq-having.json")
        kinds = [(n["details"]["table_name"], n["details"].get("input_kind"))
                 for n in flatten_nodes(root) if n["details"]["table_name"]]
        self.assertEqual(kinds, [("payment", None), ("payment", "subquery")])

    def test_recursive_union_body_is_kept(self):
        root = _parse("mariadb-13.0-cte-recursive-hierarchy-subq.json")
        self.assertEqual(root["details"]["operation"], "Materialize")
        self.assertEqual(root["details"]["table_name"], "<derived2>")
        union = root["children"][0]
        self.assertEqual(union["details"]["operation"], "Recursive union")
        self.assertEqual(union["details"]["table_name"], "<union2,3>")
        self.assertEqual(len(union["children"]), 2)
        self.assertEqual(sorted(_tables(union)), ["<derived2>", "<union2,3>", "s", "staff"])
        sub = [n for n in root["children"] if n["details"].get("input_kind") == "subquery"]
        self.assertEqual([n["details"]["table_name"] for n in sub], ["p"])

    def test_recursive_anchor_message_is_a_node(self):
        root = _parse("mariadb-11.4-explain-cte-recursive-days.json")
        ops = [n["details"]["operation"] for n in flatten_nodes(root)]
        self.assertIn("Recursive union", ops)
        self.assertIn("No tables used", ops)

    def test_intersect_and_except_are_not_labeled_union(self):
        for name, expected in (("mariadb-11.8-intersect.json", "Intersect"),
                               ("mariadb-13.0-except.json", "Except"),
                               ("mysql-8.4-v1-intersect.json", "Intersect"),
                               ("mysql-8.4-v1-except.json", "Except")):
            with self.subTest(fixture=name):
                root = _parse(name)
                self.assertEqual(root["details"]["operation"], expected)
                self.assertEqual(root["short_label"], expected)
                self.assertEqual(len(root["children"]), 2)

    def test_mixed_set_operation_and_index_merge_labels(self):
        spec = {"nested_loop": [{"table": {"table_name": "t", "access_type": "ALL", "rows": 5}}]}
        plan = {"query_block": {"union_result": {"table_name": "<unit1,2,3>", "query_specifications": [
            {"query_block": dict(spec, select_id=1)},
            {"query_block": dict(spec, select_id=2, operation="UNION")},
            {"query_block": dict(spec, select_id=3, operation="EXCEPT")}]}}}
        root = parse_explain(json.dumps(plan))
        self.assertEqual(root["details"]["operation"], "Set operation (EXCEPT, UNION)")
        self.assertEqual(build_short_label("Intersect rows sorted by row ID on t"), "Intersect (row ID)")
        self.assertEqual(build_short_label("Intersect materialize with deduplication"),
                         "Intersect materialize with deduplication")

    def test_message_only_select(self):
        root = _parse("mariadb-11.4-impossible-where.json")
        self.assertEqual(root["details"]["operation"], "Impossible WHERE")
        self.assertEqual(root["children"], [])
        self.assertFalse(timing_available(root))

    def test_subquery_cache_wrapping_materialization(self):
        plan = {"query_block": {"select_id": 1, "nested_loop": [
            {"table": {"table_name": "t", "access_type": "ALL", "rows": 10}}],
            "subqueries": [{"subquery_cache": {"r_loops": 0, "materialization": {
                "query_block": {"select_id": 2, "nested_loop": [
                    {"table": {"table_name": "u", "access_type": "ALL", "rows": 3}}]}}}}]}}
        root = parse_explain(json.dumps(plan))
        self.assertEqual(_tables(root), ["t", "u"])

    def test_derived_table_inside_join_buffer_wrapper(self):
        # Explain_table_access closes "table" before the join-buffer members,
        # so "materialized" can sit on the block-nl-join wrapper.
        plan = {"query_block": {"select_id": 1, "nested_loop": [
            {"table": {"table_name": "a", "access_type": "ALL", "rows": 10}},
            {"block-nl-join": {"table": {"table_name": "<derived2>", "access_type": "ALL", "rows": 4},
                               "buffer_type": "flat", "join_type": "BNL",
                               "materialized": {"query_block": {"select_id": 2, "nested_loop": [
                                   {"table": {"table_name": "b", "access_type": "ALL", "rows": 4}}]}}}}]}}
        root = parse_explain(json.dumps(plan))
        self.assertEqual(_tables(root), ["a", "<derived2>", "b"])


class TestMySQLFormatVersion1Plans(unittest.TestCase):
    def test_message_only_blocks_are_single_estimate_nodes(self):
        for name, message in (("mysql-8.4-v1-no-tables-used.json", "No tables used"),
                              ("mysql-8.4-v1-select-tables-optimized-away.json",
                               "Select tables optimized away"),
                              ("mysql-8.4-v1-impossible-where.json", "Impossible WHERE")):
            with self.subTest(fixture=name):
                root = _parse(name)
                self.assertEqual(root["engine"], "mysql")
                self.assertEqual(root["details"]["operation"], message)
                self.assertEqual(root["short_label"], message)
                self.assertEqual(root["children"], [])
                self.assertIsNone(root["details"]["actual_last_row_ms"])
                self.assertIsNone(root["details"]["estimated_rows"])
                self.assertFalse(timing_available(root))
                analysis = analyze_plan(root)
                payload = build_sidecar(root, analysis, source_type="file", engine="mysql")
                validate_sidecar(payload)

    def test_windowing(self):
        root = _parse("mysql-8.4-v1-window.json")
        self.assertEqual(root["details"]["operation"], "Window functions")
        self.assertEqual(root["details"]["access_type"], "sort")  # using_filesort
        self.assertEqual(_tables(root), ["actor"])
        root = _parse("mysql-8.4-v1-derived-window-topn.json")
        self.assertEqual([n["details"]["operation"] for n in flatten_nodes(root)][:2],
                         ["Materialize", "Window functions"])
        self.assertEqual(_tables(root), ["x", "p"])

    def test_having_subqueries(self):
        root = _parse("mysql-8.4-v1-subq-having.json")
        self.assertEqual(root["details"]["operation"], "Group")
        kinds = [(n["details"]["table_name"], n["details"].get("input_kind"))
                 for n in flatten_nodes(root) if n["details"]["table_name"]]
        self.assertEqual(kinds, [("payment", None), ("payment", "subquery")])

    def test_attached_materialized_subqueries(self):
        root = _parse("mysql-8.4-v1-subq-or-two-in.json")
        subs = [n for n in root["children"] if n["details"].get("input_kind") == "subquery"]
        self.assertEqual([n["details"]["table_name"] for n in subs],
                         ["<materialized_subquery>", "<materialized_subquery>"])
        self.assertEqual(sorted(t for n in subs for t in _tables(n)[1:]), ["address", "rental"])

    def test_recursive_cte_and_weedout(self):
        root = _parse("mysql-8.4-v1-cte-recursive-days.json")
        ops = [n["details"]["operation"] for n in flatten_nodes(root)]
        self.assertIn("Recursive union", ops)
        self.assertIn("No tables used", ops)
        weedout = {"query_block": {"select_id": 1, "cost_info": {"query_cost": "1"},
                                   "duplicates_removal": {"using_temporary_table": True, "nested_loop": [
                                       {"table": {"table_name": "a", "access_type": "ALL",
                                                  "rows_examined_per_scan": 5}},
                                       {"table": {"table_name": "b", "access_type": "ALL",
                                                  "rows_examined_per_scan": 5}}]}}}
        root = parse_explain(json.dumps(weedout))
        self.assertEqual(root["details"]["access_type"], "weedout")
        self.assertEqual(_tables(root), ["a", "b"])

    def test_unparseable_v1_error_names_mysql(self):
        with self.assertRaises(ValueError) as caught:
            parse_explain('{"query_block": {"select_id": 1, "cost_info": {"query_cost": "1"}}}')
        self.assertIn("MySQL EXPLAIN FORMAT=JSON (explain_json_format_version=1)", str(caught.exception))
        self.assertNotIn("MariaDB", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            parse_explain('{"query_block": {"select_id": 1}}')
        self.assertIn("MariaDB", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            parse_explain('{"foo": 1}')
        self.assertIn("Not an EXPLAIN FORMAT=JSON plan", str(caught.exception))


class TestEstimateOnlyPlansAreNotTimed(unittest.TestCase):
    def test_mariadb_explain_and_mysql_v1_have_no_measurements(self):
        for name in ("mariadb-13.0-explain-join-3t.json", "mariadb-11.4-explain-cte-recursive-days.json",
                     "mysql-8.4-v1-subq-or-two-in.json", "mysql-8.4-v1-cte-recursive-days.json"):
            with self.subTest(fixture=name):
                for node in flatten_nodes(_parse(name)):
                    self.assertIsNone(node["details"]["actual_last_row_ms"], node["short_label"])
                    self.assertIsNone(node["details"]["actual_rows"], node["short_label"])
                    self.assertFalse(node["details"]["rows_measured"])
                    self.assertEqual(node["total_time"], 0)

    def test_show_analyze_partial_has_rows_but_no_time(self):
        root = _parse("mariadb-11.8-show-analyze-partial.json")
        self.assertFalse(timing_available(root))
        scan = [n for n in flatten_nodes(root) if n["details"]["table_name"] == "f"][0]
        self.assertEqual(scan["details"]["actual_rows"], 1000.0)
        self.assertIsNone(scan["details"]["actual_last_row_ms"])

    def test_mariadb_analyze_keeps_its_measurements(self):
        root = _parse("mariadb-13.0-analyze-join-3t.json")
        self.assertTrue(all(timing_available(n) for n in flatten_nodes(root)))
        self.assertGreater(root["total_time"], 0)

    def test_diff_of_estimate_and_analyze_reports_no_measured_regression(self):
        estimate = _text("mariadb-13.0-explain-join-3t.json")
        analyze = _text("mariadb-13.0-analyze-join-3t.json")
        for before, after in ((estimate, analyze), (analyze, estimate)):
            result = build_compare_sidecar(before, after)
            summary = result["summary"]
            self.assertFalse(summary["timing_available"])
            self.assertEqual(summary["regressions"], 0)
            self.assertEqual(summary["improvements"], 0)
            self.assertEqual(summary["unmeasured"], len(result["deltas"]))
            self.assertIn("Timing comparison unavailable", render_compare(before, after))


def _find(root, label_part):
    return [n for n in flatten_nodes(root) if label_part in n["short_label"]]


def _floored_ms(root):
    """Time the self-time floor (max(0, ...)) hid: children counted as input
    time that add up to more than their parent's total."""
    floored = 0.0
    for node in flatten_nodes(root):
        counted = sum(c["total_time"] for c in node["children"]
                      if c["details"].get("input_kind") != "select_list")
        floored += max(0.0, counted - node["total_time"])
    return floored


def _sorted_join_plan(qb_ms):
    """ORDER BY on the first table of a two-table join (read_sorted_file):
    filesort 0.5 ms over t1 (0.5 ms own), then t2 (0.3 ms)."""
    return json.dumps({"query_block": {
        "select_id": 1, "r_loops": 1, "r_total_time_ms": qb_ms,
        "nested_loop": [
            {"read_sorted_file": {"r_rows": 10, "filesort": {
                "sort_key": "t1.a", "r_loops": 1, "r_total_time_ms": 0.5, "r_output_rows": 10,
                "table": {"table_name": "t1", "access_type": "ALL", "r_loops": 1, "rows": 10,
                          "r_rows": 10, "r_table_time_ms": 0.4, "r_other_time_ms": 0.1}}}},
            {"table": {"table_name": "t2", "access_type": "eq_ref", "key": "PRIMARY",
                       "r_loops": 10, "rows": 1, "r_rows": 1,
                       "r_table_time_ms": 0.2, "r_other_time_ms": 0.1}}]}})


def _subquery(select_id, total_ms, table, own_ms):
    return {"query_block": {
        "select_id": select_id, "r_loops": 1, "r_total_time_ms": total_ms,
        "nested_loop": [{"table": {"table_name": table, "access_type": "ALL", "r_loops": 1,
                                   "rows": 10, "r_rows": 10, "r_table_time_ms": own_ms,
                                   "r_other_time_ms": 0.0}}]}}


def _sorted_join_with_subquery_plan():
    """The sorted first table's WHERE holds a subquery: filesort() evaluates
    it, so the filesort's 1.0 ms contains the subquery's 0.6 ms."""
    return json.dumps({"query_block": {
        "select_id": 1, "r_loops": 1, "r_total_time_ms": 1.5,
        "nested_loop": [
            {"read_sorted_file": {"r_rows": 10, "filesort": {
                "sort_key": "t1.a", "r_loops": 1, "r_total_time_ms": 1.0, "r_output_rows": 10,
                "table": {"table_name": "t1", "access_type": "ALL", "r_loops": 1, "rows": 10,
                          "r_rows": 10, "r_table_time_ms": 0.2, "r_other_time_ms": 0.1,
                          "attached_condition": "t1.b > (subquery#2)"}}}},
            {"table": {"table_name": "t2", "access_type": "eq_ref", "key": "PRIMARY",
                       "r_loops": 10, "rows": 1, "r_rows": 1,
                       "r_table_time_ms": 0.15, "r_other_time_ms": 0.05}}],
        "subqueries": [_subquery(2, 0.6, "t3", 0.55)]}})


def _having_subquery_under_order_by_plan():
    """GROUP BY t1.g HAVING ... > (subquery) ORDER BY cnt: the HAVING is the
    inner filesort's condition, so its 1.0 ms contains the subquery's 0.8 ms."""
    return json.dumps({"query_block": {
        "select_id": 1, "r_loops": 1, "r_total_time_ms": 2.6,
        "filesort": {
            "sort_key": "cnt desc", "r_loops": 1, "r_total_time_ms": 0.1,
            "temporary_table": {"filesort": {
                "sort_key": "t1.g", "r_loops": 1, "r_total_time_ms": 1.0,
                "temporary_table": {
                    "nested_loop": [{"table": {
                        "table_name": "t1", "access_type": "ALL", "r_loops": 1, "rows": 100,
                        "r_rows": 100, "r_table_time_ms": 1.0, "r_other_time_ms": 0.5}}],
                    "subqueries": [_subquery(2, 0.8, "t2", 0.75)]}}}}}})


def _table(name, own_ms, rows=100, access="ALL"):
    return {"table": {"table_name": name, "access_type": access, "r_loops": 1, "rows": rows,
                      "r_rows": rows, "r_table_time_ms": own_ms * 0.6, "r_other_time_ms": own_ms * 0.4}}


def _buffered(name, own_ms, join_type, unpack_ms, other_ms):
    """A join-buffer entry: same object and timers for BNL, BNLH, BKA, BKAH."""
    return {"block-nl-join": dict(_table(name, own_ms), **{
        "buffer_type": "flat", "buffer_size": "256Kb", "join_type": join_type,
        "attached_condition": "o.user_id = u.id", "r_loops": 100, "r_filtered": 10,
        "r_unpack_time_ms": unpack_ms, "r_other_time_ms": other_ms, "r_effective_rows": 50})}


def _sort_over_buffered_join_plan(join_type):
    """GROUP BY ... ORDER BY over u JOIN o through a join buffer: the sort is
    cheap (0.05 ms), the buffer work is not (3 + 5 ms)."""
    return json.dumps({"query_block": {
        "select_id": 1, "r_loops": 1, "r_total_time_ms": 10.0,
        "filesort": {"sort_key": "total desc", "r_loops": 1, "r_total_time_ms": 0.05,
                     "temporary_table": {"nested_loop": [
                         _table("u", 0.5), _buffered("o", 1.0, join_type, 3.0, 5.0)]}}}})


def _reported_select_times(data):
    """r_total_time_ms of every query_block that reports one."""
    out = []
    stack = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            qb = item.get("query_block")
            if isinstance(qb, dict) and qb.get("r_total_time_ms"):
                out.append(float(qb["r_total_time_ms"]))
            stack.extend(v for v in item.values() if isinstance(v, (dict, list)))
        elif isinstance(item, list):
            stack.extend(item)
    return out


class TestMariaDBInclusiveTimes(unittest.TestCase):
    """MariaDB ANALYZE times become inclusive node totals.

    Tables report self-only time (r_table_time_ms + r_other_time_ms); query
    blocks and filesorts report spans (sql_analyze_stmt.h, sql_explain.cc).
    parse_node computes self = total - inputs, floored at 0, so a total that
    leaves out a child double counts the child.
    """

    def test_materialized_derived_table_is_counted_once(self):
        # ORDER BY over a derived table whose GROUP BY scans users. The
        # <derived2> table's own 0.009 ms excludes the 0.836 ms fill.
        root = _parse("mariadb-analyze-derived-materialized.json")
        nodes = list(flatten_nodes(root))
        self.assertAlmostEqual(root["total_time"], 0.846583333, places=9)
        mat = _find(root, "Materialize")[0]
        self.assertAlmostEqual(mat["total_time"], 4.583333e-4 + 0.008583333 + 0.836291667, places=9)
        self.assertAlmostEqual(mat["self_time"], 4.583333e-4 + 0.008583333, places=9)
        derived_sort = mat["children"][0]
        self.assertAlmostEqual(derived_sort["total_time"], 0.836291667, places=9)
        self.assertAlmostEqual(derived_sort["self_time"], 0.836291667 - 0.819125, places=9)
        self.assertAlmostEqual(_find(root, "users")[0]["total_time"], 0.322666667 + 0.496458333, places=9)
        # The outer sort's self is the filesort plus the block's untimed rest.
        self.assertAlmostEqual(root["self_time"], 0.00125, places=6)
        self.assertAlmostEqual(sum(n["self_time"] for n in nodes), root["total_time"], places=9)
        self.assertEqual(_floored_ms(root), 0.0)

    def test_table_top_takes_the_select_time_and_its_subquery_counts_once(self):
        # A single-table select: the table node stands for the select, so it
        # carries the block's 1.6984 ms; the subquery carries its own block's
        # 1.0538 ms and is not part of the table's self time.
        with open(os.path.join(FIXTURES, "mariadb-10.11-021-subquery-not-in.json"), encoding="utf-8") as f:
            data = json.load(f)
        root = parse_explain(json.dumps(data))
        qb = data["query_block"]
        sub_qb = qb["subqueries"][0]["materialization"]["query_block"]
        table = qb["nested_loop"][0]["table"]
        sub = [c for c in root["children"] if c["details"].get("input_kind") == "subquery"][0]
        self.assertAlmostEqual(root["total_time"], qb["r_total_time_ms"], places=9)
        self.assertAlmostEqual(sub["total_time"], sub_qb["r_total_time_ms"], places=9)
        self.assertAlmostEqual(root["self_time"], qb["r_total_time_ms"] - sub_qb["r_total_time_ms"], places=9)
        self.assertGreaterEqual(root["self_time"], table["r_table_time_ms"] + table["r_other_time_ms"])

    def test_materialize_top_takes_the_select_time(self):
        root = _parse("mariadb-11.4-023-derived-table.json")
        self.assertIn("Materialize", root["short_label"])
        self.assertAlmostEqual(root["total_time"], 0.661810967, places=9)

    def test_subquery_inside_a_sort_over_a_table_counts_once(self):
        # Siblings must fit in 1.5 - 0.6 ms; the sort gives back up to
        # min(filesort, t1 + subquery) = 0.9 ms.
        root = parse_explain(_sorted_join_with_subquery_plan())
        sort = _find(root, "Sort")[0]
        self.assertAlmostEqual(sort["total_time"], 0.7, places=9)
        self.assertAlmostEqual(_find(root, "t1")[0]["total_time"], 0.3, places=9)
        self.assertAlmostEqual(root["total_time"], 1.5, places=9)
        self.assertAlmostEqual(sum(n["self_time"] for n in flatten_nodes(root)), 1.5, places=9)
        self.assertEqual(_floored_ms(root), 0.0)

    def test_having_subquery_inside_a_nested_sort_counts_once(self):
        root = parse_explain(_having_subquery_under_order_by_plan())
        group_sort = _find(root, "Sort")[1]
        self.assertEqual(group_sort["details"]["sort_fields"], ["t1.g"])
        self.assertAlmostEqual(group_sort["total_time"], 1.8, places=9)  # 2.6 - 0.8
        self.assertAlmostEqual(root["total_time"], 2.6, places=9)
        self.assertAlmostEqual(sum(n["self_time"] for n in flatten_nodes(root)), 2.6, places=9)
        self.assertEqual(_floored_ms(root), 0.0)

    def test_select_spanning_root_does_not_count_its_subquery_twice(self):
        # HAVING (SELECT AVG ...) runs inside the filesort over the grouped
        # temporary table, so the block's 4.25 ms already contains it.
        root = _parse("mariadb-11.8-subq-having.json")
        nodes = list(flatten_nodes(root))
        self.assertAlmostEqual(sum(n["self_time"] for n in nodes), root["total_time"], places=9)
        children = sum(c["total_time"] for c in root["children"])
        self.assertAlmostEqual(root["self_time"], root["total_time"] - children, places=9)

    def test_inner_nested_loop_joins_span_their_inputs(self):
        root = _parse("mariadb-10.11-019-join-4t-agg.json")
        joins = _find(root, "Nested loop")
        self.assertGreater(len(joins), 1)
        for join in joins:
            self.assertAlmostEqual(join["total_time"], sum(c["total_time"] for c in join["children"]), places=9)

    def test_sort_over_a_table_is_clamped_to_the_block_time(self):
        # filesort 0.5 + t1 0.5 may overlap by up to min(0.5, 0.5).
        root = parse_explain(_sorted_join_plan(1.0))
        sort = _find(root, "Sort")[0]
        self.assertAlmostEqual(sort["total_time"], 0.7, places=9)  # 1.0 - t2's 0.3
        self.assertAlmostEqual(_find(root, "t1")[0]["total_time"], 0.5, places=9)
        self.assertAlmostEqual(_find(root, "t2")[0]["total_time"], 0.3, places=9)
        self.assertAlmostEqual(root["total_time"], 1.0, places=9)
        self.assertAlmostEqual(sum(n["self_time"] for n in flatten_nodes(root)), 1.0, places=9)
        # Never below max(filesort, table), even when the block is shorter.
        sort = _find(parse_explain(_sorted_join_plan(0.6)), "Sort")[0]
        self.assertAlmostEqual(sort["total_time"], 0.5, places=9)
        # Without excess the sum stands.
        sort = _find(parse_explain(_sorted_join_plan(2.0)), "Sort")[0]
        self.assertAlmostEqual(sort["total_time"], 1.0, places=9)

    def test_join_buffer_time_belongs_to_the_join(self):
        with open(os.path.join(TEST_DIR, "mariadb-explain-block-nl-join.json"), encoding="utf-8") as f:
            root = parse_explain(f.read())
        t2, t1 = _find(root, "[t2]")[0], _find(root, "[t1]")[0]
        self.assertAlmostEqual(t2["self_time"], 0.064741575 + 0.03195256, places=9)
        self.assertAlmostEqual(t1["self_time"], 0.271889522 + 0.033249065, places=9)
        self.assertAlmostEqual(root["total_time"], 185.7013798, places=6)
        self.assertAlmostEqual(root["self_time"], 50.97537491 + 134.3207009, delta=0.01)

    def test_sort_over_a_buffered_join_keeps_a_small_self_time(self):
        for join_type in ("BNL", "BNLH", "BKA", "BKAH"):
            with self.subTest(join_type=join_type):
                root = parse_explain(_sort_over_buffered_join_plan(join_type))
                join = _find(root, "Nested loop")[0]
                self.assertAlmostEqual(root["total_time"], 10.0, places=9)
                self.assertAlmostEqual(root["self_time"], 0.5, places=9)  # 0.05 sort + untimed rest
                self.assertAlmostEqual(join["total_time"], 0.5 + 1.0 + 3.0 + 5.0, places=9)
                self.assertAlmostEqual(join["self_time"], 3.0 + 5.0, places=9)
                self.assertAlmostEqual(_find(root, "[o]")[0]["self_time"], 1.0, places=9)
                self.assertEqual(_floored_ms(root), 0.0)

    def test_join_buffer_time_goes_to_the_join_over_that_table(self):
        plan = json.dumps({"query_block": {
            "select_id": 1, "r_loops": 1, "r_total_time_ms": 20.0, "nested_loop": [
                _table("a", 1.0), _buffered("b", 1.0, "BNL", 2.0, 4.0), _table("c", 2.0, access="eq_ref")]}})
        root = parse_explain(plan)
        inner_join = root["children"][0]
        self.assertAlmostEqual(inner_join["total_time"], 8.0, places=9)
        self.assertAlmostEqual(inner_join["self_time"], 6.0, places=9)
        self.assertAlmostEqual(root["self_time"], 20.0 - 8.0 - 2.0, places=9)

    # MariaDB numbers that do not add up, with the floored ms they cause.
    FLOORED = {
        # The recursive member (select #3) reports 0.0124 ms while its two
        # tables report 0.0159 ms: handler time on the recursive work table
        # accrues outside that member's JOIN::exec. The block's self time is
        # floored at 0.
        "mariadb-13.0-cte-recursive-hierarchy-subq.json": 0.0036,
    }

    def test_every_mariadb_analyze_plan_adds_up(self):
        checked = 0
        for path, _data in _query_block_fixtures():
            with open(path, encoding="utf-8") as f:
                root = parse_explain(f.read())
            if root.get("engine") != "mariadb" or not timing_available(root):
                continue
            checked += 1
            name = os.path.basename(path)
            with self.subTest(fixture=name):
                nodes = list(flatten_nodes(root))
                tol = max(0.01, 0.01 * root["total_time"])
                self.assertAlmostEqual(sum(n["self_time"] for n in nodes), root["total_time"], delta=tol)
                for node in nodes:
                    for child in node["children"]:
                        self.assertLessEqual(child["total_time"], node["total_time"] + tol,
                                             "{} > {}".format(child["short_label"], node["short_label"]))
                # Sum of selves equals the root exactly unless a floor applied;
                # where it does, the plan's own numbers say so (FLOORED).
                self.assertLessEqual(_floored_ms(root), self.FLOORED.get(name, 1e-7))
                # Every select's top node carries its block's measured time.
                with open(path, encoding="utf-8") as f:
                    data = load_explain_json(f.read())
                totals = [n["total_time"] for n in nodes]
                for select_ms in _reported_select_times(data):
                    match = [t for t in totals if abs(t - select_ms) <= 1e-9 * max(1.0, select_ms)]
                    self.assertTrue(match, "no node carries the select time {}".format(select_ms))
                    totals.remove(match[0])
        self.assertGreater(checked, 60)


def _mariadb_misestimated_plan():
    return json.dumps({"query_block": {"select_id": 1, "r_loops": 1, "r_total_time_ms": 5.0, "nested_loop": [
        {"table": {"table_name": "c", "access_type": "ALL", "r_loops": 1, "rows": 10, "r_rows": 10,
                   "r_table_time_ms": 0.1, "r_other_time_ms": 0.0}},
        {"table": {"table_name": "p", "access_type": "ref", "key": "idx_fk_customer_id",
                   "loops": 10, "r_loops": 10, "rows": 2, "r_rows": 500,
                   "r_table_time_ms": 2.0, "r_other_time_ms": 0.5}}]}})


class TestAdvisorWording(unittest.TestCase):
    def test_mariadb_statistics_suggestion_names_the_alias(self):
        root = parse_explain(_mariadb_misestimated_plan())
        analysis = analyze_plan(root)
        self.assertEqual([m["table"] for m in analysis["misestimates"]], ["p"])
        text = [s for s in analysis["suggestions"] if s.startswith("Refresh statistics")][0]
        self.assertIn("ANALYZE TABLE on the base table behind alias p", text)
        self.assertNotIn("ANALYZE TABLE p", text)
        self.assertNotIn("UPDATE HISTOGRAM", text)
        payload = build_sidecar(root, analysis, source_type="file", engine="mariadb")
        validate_sidecar(payload)
        stats = [s for s in payload["suggestions"] if s["category"] == "statistics"]
        self.assertEqual(len(stats), 1)

    def test_recursive_cte_work_tables_are_not_misestimates(self):
        plan = {"operation": "Table scan on chain", "table_name": "chain", "access_type": "table",
                "estimated_rows": 2, "actual_rows": 5000, "actual_loops": 1, "actual_last_row_ms": 2.0,
                "inputs": [{"operation": "Materialize recursive CTE chain", "access_type": "materialize",
                            "estimated_rows": 2, "actual_rows": 5000, "actual_loops": 1,
                            "actual_last_row_ms": 1.9, "inputs": [
                                {"operation": "Rows fetched before execution",
                                 "access_type": "rows_fetched_before_execution", "estimated_rows": 1,
                                 "actual_rows": 1, "actual_loops": 1, "actual_last_row_ms": 0.0},
                                {"operation": "Filter: (c.n < 5000)", "access_type": "filter",
                                 "condition": "(c.n < 5000)", "estimated_rows": 1, "actual_rows": 4999,
                                 "actual_loops": 1, "actual_last_row_ms": 1.5, "inputs": [
                                     {"operation": "Scan new records on c", "access_type": "scan_new_records",
                                      "table_name": "c", "estimated_rows": 2, "actual_rows": 5000,
                                      "actual_loops": 1, "actual_last_row_ms": 1.0}]}]}]}
        root = parse_explain(json.dumps(plan))
        self.assertEqual(analyze_plan(root)["misestimates"], [])

        def node(op, access, table, children=()):
            return {"short_label": op, "children": list(children), "details": {
                "operation": op, "access_type": access, "table_name": table, "estimated_rows": 2.0,
                "actual_rows": 5000.0, "actual_loops": 1, "rows_measured": True}}
        tree = node("Nested loop inner join", "join", "", [
            node("Table scan on chain", "table", "chain"),
            node("Materialize CTE chain", "materialize", "", [node("Table scan on t", "table", "t")])])
        tree["engine"] = "mysql"
        self.assertEqual([m["table"] for m in _collect_misestimates(tree)], ["t"])

    def test_temp_table_suggestion_is_engine_specific(self):
        root = parse_explain(_text("explain-052-derived-table-top-spenders.json"))
        analysis = analyze_plan(root)
        text = [s for s in analysis["suggestions"] if "tmp_table_size" in s][0]
        self.assertNotIn("Increase tmp_table_size / max_heap_table_size", text)
        self.assertIn("internal_tmp_mem_storage_engine", text)
        self.assertIn("max_heap_table_size does not apply", text)
        payload = build_sidecar(root, analysis, source_type="file", engine="mysql")
        entry = [s for s in payload["suggestions"] if "internal_tmp_mem_storage_engine" in s["action"]][0]
        self.assertEqual((entry["category"], entry["target_variable"]), ("tuning_variable", "tmp_table_size"))

        root = _parse("mariadb-13.0-distinct-temporary-table.json")
        text = [s for s in analyze_plan(root)["suggestions"] if "tmp_table_size" in s][0]
        self.assertIn("smaller of the two", text)
        self.assertNotIn("TempTable", text)


if __name__ == "__main__":
    unittest.main()
