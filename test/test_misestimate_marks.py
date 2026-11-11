"""Row-misestimate records on the parsed tree, the advisor's statistics
evidence, the chart-data field, and the marks every plan view draws from
them (Visual Explain, execution tree, bar chart, flame graph).

The two live fixtures were recorded from the lab (MySQL 9.7.2 and MariaDB
11.8.9) on a table whose InnoDB statistics were made stale on purpose:
ANALYZE TABLE ran while each customer had two orders, then STATS_AUTO_RECALC=0
kept that cardinality while 18,000 orders moved onto 20 customers. Expected
numbers come from the raw plans (MySQL estimated_rows/actual_rows/
actual_loops, MariaDB rows/r_rows/r_loops) and the recorded captures, never
from the parser helpers under test.
"""
import copy
import json
import os
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from myflames import flamegraph
from myflames.advisor import advise, statistics_evidence
from myflames.chart_data import build_chart_data
from myflames.exploration import project_tree
from myflames.output_bargraph import render_bargraph
from myflames.output_html_report import _render_svg
from myflames.output_tree import render_tree
from myflames.output_workbench import BOX_W, MARK_COLOR, render_workbench
from myflames.parser import (
    _factor_text, _rows_text, analyze_plan, flatten_nodes, hide_parsed_plan_literals,
    misestimate_marks, parse_explain,
)

TEST = Path(__file__).resolve().parent
REPO = TEST.parent
MYSQL_PLAN = TEST / "mysql-9.7-stale-stats-join.json"
MARIADB_PLAN = TEST / "mariadb-11.8-stale-stats-join.json"
MYSQL_CAPTURE = TEST / "fixtures" / "captures" / "mysql-9.7-stale-stats.capture.json"
MARIADB_CAPTURE = TEST / "fixtures" / "captures" / "mariadb-11.8-stale-stats.capture.json"
LIMIT_PLAN = TEST / "fixtures" / "explain-063-limit-no-order-users.json"
SVG = "{http://www.w3.org/2000/svg}"
FIXTURES = sorted(list((TEST / "fixtures").glob("*.json")) + list(TEST.glob("*.json")))


def _read(path):
    return Path(path).read_text(encoding="utf-8")


def _marked(root):
    return [n for n in flatten_nodes(root) if isinstance(n["details"].get("misestimate"), dict)]


def _raw_mysql_nodes(node):
    """Every operator object of a MySQL format-2 plan (raw JSON)."""
    yield node
    for child in node.get("inputs") or []:
        yield from _raw_mysql_nodes(child)


def _scan(operation, table, est, act, loops=1, **extra):
    node = {"operation": operation, "access_type": "table", "table_name": table,
            "estimated_rows": est, "actual_rows": act, "actual_loops": loops,
            "actual_first_row_ms": 0.1, "actual_last_row_ms": 1.0}
    node.update(extra)
    return node


def _capture_analysis(path):
    capture = json.loads(_read(path))
    root = parse_explain(capture["raw"])
    analysis = analyze_plan(root)
    advise(analysis, schema=capture.get("schema"), stats=capture.get("table_stats"),
           variables=capture.get("variables"), freshness=capture.get("stats_freshness"), capture=capture)
    return capture, root, analysis


class TestMisestimateRecord(unittest.TestCase):
    def test_mysql_live_fixture_flags_only_the_stale_index_lookup(self):
        raw = json.loads(_read(MYSQL_PLAN))["query_plan"]
        lookups = [n for n in _raw_mysql_nodes(raw) if n["operation"].startswith("Index lookup on o ")]
        self.assertEqual(len(lookups), 1)
        lookup = lookups[0]
        # The stale cardinality says one order per customer; 451 per loop came back.
        self.assertEqual((lookup["estimated_rows"], lookup["actual_rows"], lookup["actual_loops"]), (1.0, 451.0, 40))
        root = parse_explain(_read(MYSQL_PLAN))
        marked = _marked(root)
        self.assertEqual([n["details"]["operation"] for n in marked], [lookup["operation"]],
                         "only the misestimated access carries a record (not the join above it)")
        self.assertEqual(marked[0]["details"]["misestimate"], {
            "direction": "under", "factor": 451.0, "estimated": 1.0, "actual": 451.0, "loops": 40,
            "table": "myflames_w2c_stale.orders", "kind": "access", "label": "rows ×451 under",
            "text": "451× under (estimated 1, actual 451 rows per loop)",
            "reason": "Rows returned per loop differ from the optimizer's estimate by 10× or more, "
                      "on at least 1,000 rows across loops.",
        })

    def test_mariadb_live_fixture_flags_the_stale_ref_lookup(self):
        raw = json.loads(_read(MARIADB_PLAN))
        tables = [t["table"] for t in raw["query_block"]["filesort"]["temporary_table"]["nested_loop"]]
        ref = [t for t in tables if t["access_type"] == "ref"][0]
        self.assertEqual((ref["table_name"], ref["rows"], ref["r_rows"], ref["r_loops"]), ("orders", 1, 451, 40))
        root = parse_explain(_read(MARIADB_PLAN))
        self.assertEqual(root["engine"], "mariadb")
        marked = _marked(root)
        self.assertEqual(len(marked), 1)
        record = marked[0]["details"]["misestimate"]
        self.assertEqual((record["direction"], record["factor"], record["estimated"], record["actual"],
                          record["loops"], record["table"], record["kind"]),
                         ("under", 451.0, 1.0, 451.0, 40, "orders", "access"))
        self.assertEqual(record["label"], "rows ×451 under")
        # MariaDB counts rows examined per lookup and hides LIMIT.
        self.assertIn("Rows examined per lookup, before the WHERE condition", record["reason"])
        self.assertIn("only underestimates are flagged", record["reason"])

    def test_records_agree_with_the_plan_warning_on_every_fixture(self):
        """The chart marks and the 'Row estimate off' warning never disagree:
        a renderer-side rule or a second walk would drift from it."""
        self.assertGreater(len(FIXTURES), 150)
        flagged = 0
        for path in FIXTURES:
            with self.subTest(fixture=path.name):
                root = parse_explain(_read(path))
                warning = sorted((m["table"], m["kind"], m["ratio"], m["under"])
                                 for m in analyze_plan(root)["misestimates"])
                marks = sorted((r["table"], r["kind"], r["factor"], r["direction"] == "under")
                               for r in (n["details"]["misestimate"] for n in _marked(root)))
                self.assertEqual(marks, warning)
                flagged += bool(marks)
        self.assertGreaterEqual(flagged, 6, "both live fixtures and the four older ones are flagged")

    def test_overestimate_below_a_limit_is_not_flagged(self):
        raw = json.loads(_read(LIMIT_PLAN))
        scan = raw["inputs"][0]
        # 3,000 estimated, 50 read: a Limit stops the scan early, as expected.
        self.assertEqual((raw["operation"], scan["estimated_rows"], scan["actual_rows"]),
                         ("Limit: 50 row(s)", 3000.0, 50.0))
        self.assertEqual(_marked(parse_explain(_read(LIMIT_PLAN))), [])
        # The same scan without the Limit is a 60x overestimate.
        alone = parse_explain(json.dumps(_scan("Table scan on users", "users", 3000.0, 50.0)))
        self.assertEqual([n["details"]["misestimate"]["direction"] for n in _marked(alone)], ["over"])
        # Below a Limit an underestimate is still flagged.
        under = parse_explain(json.dumps({"operation": "Limit: 50 row(s)", "access_type": "limit",
                                          "actual_rows": 50.0, "actual_loops": 1, "actual_last_row_ms": 2.0,
                                          "inputs": [_scan("Table scan on users", "users", 50.0, 3000.0)]}))
        self.assertEqual([n["details"]["misestimate"]["label"] for n in _marked(under)], ["rows ×60 under"])

    def test_cte_references_and_recursive_work_tables_are_not_flagged(self):
        plan = {"operation": "Nested loop inner join", "access_type": "join", "join_algorithm": "nested_loop",
                "estimated_rows": 2.0, "actual_rows": 5000.0, "actual_loops": 1, "actual_last_row_ms": 9.0,
                "inputs": [
                    _scan("Table scan on chain", "chain", 2.0, 5000.0),
                    {"operation": "Materialize CTE chain", "access_type": "materialize",
                     "estimated_rows": 2.0, "actual_rows": 5000.0, "actual_loops": 1, "actual_last_row_ms": 5.0,
                     "inputs": [_scan("Table scan on t", "t", 2.0, 5000.0)]}]}
        root = parse_explain(json.dumps(plan))
        # The CTE reference has no statistics to refresh; the base table under it does.
        self.assertEqual([n["details"]["table_name"] for n in _marked(root)], ["t"])
        recursive = {"operation": "Table scan on chain", "table_name": "chain", "access_type": "table",
                     "estimated_rows": 2, "actual_rows": 5000, "actual_loops": 1, "actual_last_row_ms": 2.0,
                     "inputs": [{"operation": "Materialize recursive CTE chain", "access_type": "materialize",
                                 "estimated_rows": 2, "actual_rows": 5000, "actual_loops": 1, "actual_last_row_ms": 1.9,
                                 "inputs": [{"operation": "Filter: (c.n < 5000)", "access_type": "filter",
                                             "condition": "(c.n < 5000)", "estimated_rows": 1, "actual_rows": 4999,
                                             "actual_loops": 1, "actual_last_row_ms": 1.5,
                                             "inputs": [{"operation": "Scan new records on c",
                                                         "access_type": "scan_new_records", "table_name": "c",
                                                         "estimated_rows": 2, "actual_rows": 5000,
                                                         "actual_loops": 1, "actual_last_row_ms": 1.0}]}]}]}
        self.assertEqual(_marked(parse_explain(json.dumps(recursive))), [])

    def test_filter_record_names_the_condition_selectivity(self):
        plan = {"operation": "Filter: (o.total > 490)", "access_type": "filter", "condition": "(o.total > 490)",
                "estimated_rows": 50.0, "actual_rows": 9000.0, "actual_loops": 1, "actual_last_row_ms": 3.0,
                "inputs": [_scan("Table scan on o", "orders", 20000.0, 20000.0)]}
        marked = _marked(parse_explain(json.dumps(plan)))
        self.assertEqual([n["details"]["operation"] for n in marked], ["Filter: (o.total > 490)"])
        record = marked[0]["details"]["misestimate"]
        self.assertEqual((record["kind"], record["label"]), ("filter", "rows ×180 under"))
        self.assertIn("estimated selectivity of the condition is off", record["reason"])

    def test_copies_keep_the_whole_plan_verdict(self):
        """A focused branch keeps the record computed with the whole plan's
        early-stop context, so focusing never flips a marker."""
        plan = {"operation": "Limit: 50 row(s)", "access_type": "limit", "actual_rows": 50.0, "actual_loops": 1,
                "actual_last_row_ms": 2.0, "inputs": [{
                    "operation": "Nested loop inner join", "access_type": "join", "join_algorithm": "nested_loop",
                    "actual_rows": 50.0, "actual_loops": 1, "actual_last_row_ms": 1.9, "inputs": [
                        _scan("Table scan on a", "a", 3000.0, 50.0),
                        _scan("Table scan on b", "b", 1.0, 400.0, loops=50)]}]}
        root = parse_explain(json.dumps(plan))
        self.assertEqual([n["details"]["table_name"] for n in _marked(root)], ["b"])
        join = root["children"][0]
        focused = project_tree(root, join["node_id"], [])
        self.assertEqual([n["details"]["table_name"] for n in _marked(focused)], ["b"],
                         "the overestimate on a stays unflagged when the Limit is outside the focus")
        hidden = hide_parsed_plan_literals(root)
        self.assertEqual([n["details"]["misestimate"] for n in _marked(hidden)],
                         [n["details"]["misestimate"] for n in _marked(root)])

    def test_marker_and_row_text(self):
        for ratio, text in ((10.0, "10"), (451.0, "451"), (9999.4, "9,999"), (9999.6, "10K"),
                            (12345.0, "12K"), (999499.0, "999K"), (999500.0, "1M"), (2.5e7, "25M")):
            self.assertEqual(_factor_text(ratio), text, ratio)
        for value, text in ((1.0, "1"), (0.5, "0.5"), (3.3333, "3.33"), (451.25, "451"), (12345.0, "12,345")):
            self.assertEqual(_rows_text(value), text, value)


class TestStatisticsEvidence(unittest.TestCase):
    def test_mariadb_alias_collision_never_attaches_another_tables_statistics(self):
        """A unique suffix is not proof: orders is an alias for a.items."""
        root = parse_explain(json.dumps({"query_block": {"select_id": 1, "r_total_time_ms": 2,
            "nested_loop": [
                {"table": {"table_name": "orders", "access_type": "ref", "key": "k",
                           "rows": 1, "r_rows": 1000, "r_loops": 1, "r_table_time_ms": 1}},
                {"table": {"table_name": "actual_orders", "access_type": "eq_ref", "key": "PRIMARY",
                           "rows": 1, "r_rows": 1, "r_loops": 1000, "r_table_time_ms": 1}}]}}))
        for query in (
                "SELECT * FROM a.items AS orders JOIN b.orders AS actual_orders ON orders.id=actual_orders.id",
                "SELECT * FROM a.items orders JOIN b.orders actual_orders ON orders.id=actual_orders.id"):
            with self.subTest(query=query):
                analysis = analyze_plan(root)
                advise(analysis, freshness={"a.items": {"n_rows": 10}, "b.orders": {"n_rows": 20}},
                       capture={"engine": "mariadb", "query": query})
                self.assertEqual(analysis["statistics_evidence"], {})
                self.assertFalse([f for f in analysis["environment_findings"] if f["rule_id"] == "STALE_STATS"])
                for compact in (False, True):
                    data = build_chart_data(root, analysis=analysis, compact=compact)
                    marks = [n["misestimate"] for n in data["nodes"] if "misestimate" in n]
                    self.assertEqual(len(marks), 1)
                    self.assertEqual(marks[0]["table"], "orders")
                    self.assertIsNone(marks[0]["evidence"])
                    self.assertNotIn("ANALYZE TABLE", json.dumps(data))

    def test_mariadb_unaliased_table_uses_only_its_proven_schema(self):
        root = parse_explain(json.dumps({"query_block": {"select_id": 1, "r_total_time_ms": 1,
            "nested_loop": [{"table": {"table_name": "orders", "access_type": "ref", "key": "k",
                "rows": 1, "r_rows": 1000, "r_loops": 1, "r_table_time_ms": 1}}]}}))
        for query in ("SELECT * FROM a.orders WHERE id=1", "SELECT * FROM `a`.`orders` WHERE id=1"):
            with self.subTest(query=query):
                analysis = analyze_plan(root)
                advise(analysis, freshness={"a.orders": {"n_rows": 10}, "b.orders": {"n_rows": 20}},
                       capture={"engine": "mariadb", "query": query})
                evidence = analysis["statistics_evidence"]["orders"]
                self.assertEqual(evidence["table"], "a.orders")
                self.assertEqual(evidence["n_rows"], 10)
                self.assertEqual(evidence["statement"], "ANALYZE TABLE `a`.`orders`;")

    def test_bare_name_without_capture_identity_has_no_evidence(self):
        for capture in (None, {"engine": "mariadb", "query": "SELECT * FROM orders"},
                        {"engine": "mariadb", "query": "SELECT * FROM orders",
                         "source": {"kind": "digest", "schema": "a"}},
                        {"engine": "mariadb", "query": "SELECT * FROM (SELECT * FROM a.orders) orders"},
                        {"engine": "mariadb", "query": "WITH orders AS (SELECT * FROM a.orders) SELECT * FROM orders"},
                        {"engine": "mariadb", "query": "SELECT * FROM a.orders AS orders"},
                        {"engine": "mariadb", "query": "SELECT * FROM a.orders FOR SYSTEM_TIME AS OF TIMESTAMP '2026-01-01' AS other JOIN b.items AS orders ON other.id=orders.id"},
                        {"engine": "mariadb", "query": "SELECT * FROM a.orders JOIN b.orders USING (id)"}):
            with self.subTest(capture=capture):
                analysis = {"misestimates": [{"table": "orders"}]}
                advise(analysis, freshness={"a.orders": {"n_rows": 10}}, capture=capture)
                self.assertEqual(analysis["statistics_evidence"], {})

    def test_dotted_mariadb_alias_is_not_a_schema_qualified_base_table(self):
        root = parse_explain(json.dumps({"query_block": {"select_id": 1, "r_total_time_ms": 1,
            "nested_loop": [{"table": {"table_name": "b.orders", "access_type": "ref", "key": "k",
                "rows": 1, "r_rows": 1000, "r_loops": 1, "r_table_time_ms": 1}}]}}))
        for capture in (None, {"engine": "mariadb", "query": "SELECT * FROM a.items AS `b.orders`"}):
            with self.subTest(capture=capture):
                analysis = analyze_plan(root)
                advise(analysis, freshness={"b.orders": {"n_rows": 20}}, capture=capture)
                self.assertEqual(len(_marked(root)), 1)
                self.assertEqual(analysis["statistics_evidence"], {})

    def test_dots_inside_identifiers_do_not_make_ambiguous_statistics_keys(self):
        for schema, table in (("a.b", "orders"), ("a", "b.orders")):
            with self.subTest(schema=schema, table=table):
                root = parse_explain(json.dumps(_scan("Table scan on orders", table, 1, 1000,
                                                      schema_name=schema)))
                analysis = analyze_plan(root)
                advise(analysis, freshness={"a.b.orders": {"n_rows": 20}})
                self.assertEqual(len(_marked(root)), 1)
                self.assertEqual(analysis["statistics_evidence"], {})

    def test_same_table_name_in_two_schemas_keeps_its_own_evidence(self):
        """MySQL supplies schema_name on the access, including below a filter."""
        plan = {"operation": "Append", "actual_last_row_ms": 4, "actual_loops": 1, "inputs": [
            _scan("Table scan on t", "t", 1, 1000, schema_name="a"),
            {"operation": "Filter: (t.x > 0)", "access_type": "filter", "estimated_rows": 1,
             "actual_rows": 1000, "actual_loops": 1, "actual_last_row_ms": 2, "inputs": [
                 _scan("Table scan on t", "t", 1000, 1000, schema_name="b")]}]}
        root = parse_explain(json.dumps(plan))
        analysis = analyze_plan(root)
        advise(analysis, freshness={
            "a.t": {"last_update": "2026-01-01", "n_rows": 10, "histograms": []},
            "b.t": {"last_update": "2026-02-02", "n_rows": 20, "histograms": ["x"]}})
        self.assertEqual([n["details"]["misestimate"]["table"] for n in _marked(root)], ["a.t", "b.t"])
        self.assertEqual(set(analysis["statistics_evidence"]), {"a.t", "b.t"})
        for compact in (False, True):
            marks = [n["misestimate"] for n in build_chart_data(root, analysis=analysis, compact=compact)["nodes"]
                     if "misestimate" in n]
            self.assertEqual([(m["table"], m["evidence"]["table"], m["evidence"]["n_rows"],
                               m["evidence"]["statement"]) for m in marks], [
                ("a.t", "a.t", 10, "ANALYZE TABLE `a`.`t`;"),
                ("b.t", "b.t", 20, "ANALYZE TABLE `b`.`t`;")])
        svg = ET.fromstring(render_workbench(root, analysis=analysis))
        for node in _marked(root):
            card = next(g for g in svg.iter(SVG + "g") if g.get("data-node-id") == node["node_id"])
            schema = node["details"]["misestimate"]["table"].split(".")[0]
            self.assertIn("Suggested SQL: ANALYZE TABLE `{}`.`t`;".format(schema), card.get("data-details"))

    def test_ambiguous_bare_names_and_missing_qualified_names_have_no_evidence(self):
        for table in ("t", "missing.t"):
            with self.subTest(table=table):
                analysis = {"misestimates": [{"table": table}]}
                advise(analysis, freshness={"a.t": {"n_rows": 10}, "b.t": {"n_rows": 20},
                                             "prefix.missing.t": {"n_rows": 30}})
                self.assertEqual(analysis["statistics_evidence"], {})
                self.assertFalse([f for f in analysis["environment_findings"] if f["rule_id"] == "STALE_STATS"])

    def test_mariadb_bare_name_does_not_choose_between_schemas(self):
        root = parse_explain(json.dumps({"query_block": {"select_id": 1, "r_total_time_ms": 1,
            "nested_loop": [{"table": {"table_name": "t", "access_type": "ref", "key": "k",
                "rows": 1, "r_rows": 1000, "r_loops": 1, "r_table_time_ms": 1}}]}}))
        analysis = analyze_plan(root)
        advise(analysis, freshness={"a.t": {"n_rows": 10}, "b.t": {"n_rows": 20}})
        marks = [n["misestimate"] for n in build_chart_data(root, analysis=analysis)["nodes"] if "misestimate" in n]
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0]["table"], "t")
        self.assertIsNone(marks[0]["evidence"])

    def _check_capture(self, path, last_update, n_rows):
        capture, root, analysis = _capture_analysis(path)
        freshness = capture["stats_freshness"]["myflames_w2c_stale.orders"]
        self.assertEqual((freshness["last_update"], freshness["n_rows"], freshness["histograms"]),
                         (last_update, n_rows, []))
        evidence = analysis["statistics_evidence"]
        identity = "myflames_w2c_stale.orders" if root["engine"] == "mysql" else "orders"
        self.assertEqual(list(evidence), [identity])
        self.assertEqual(evidence[identity], {
            "table": "myflames_w2c_stale.orders", "last_update": last_update, "n_rows": n_rows,
            "histograms": [],
            "text": "InnoDB statistics last updated {} (n_rows {:,}); no column histograms".format(last_update, n_rows),
            "statement": "ANALYZE TABLE `myflames_w2c_stale`.`orders`;",
        })
        finding = [f["warning"] for f in analysis["environment_findings"] if f["rule_id"] == "STALE_STATS"]
        self.assertEqual(finding, ["Statistics for misestimated table myflames_w2c_stale.orders: "
                                   + evidence[identity]["text"] + "."],
                         "the evidence restates the STALE_STATS finding, not a second rule")
        return root, analysis

    def test_mysql_capture_evidence_and_statement(self):
        self._check_capture(MYSQL_CAPTURE, "2026-11-08 22:10:20", 6852)

    def test_mariadb_capture_evidence_and_statement(self):
        self._check_capture(MARIADB_CAPTURE, "2026-11-08 22:10:32", 19907)

    def test_mariadb_alias_has_no_evidence_and_no_statement(self):
        """MariaDB writes the alias as table_name (sql_select.cc), which the
        freshness map of real tables cannot name: no evidence, no statement."""
        plan = {"query_block": {"select_id": 1, "r_loops": 1, "r_total_time_ms": 5.0, "nested_loop": [
            {"table": {"table_name": "c", "access_type": "ALL", "r_loops": 1, "rows": 10, "r_rows": 10,
                       "r_table_time_ms": 0.1, "r_other_time_ms": 0.0}},
            {"table": {"table_name": "o", "access_type": "ref", "key": "idx_customer", "loops": 10,
                       "r_loops": 10, "rows": 2, "r_rows": 500, "r_table_time_ms": 2.0, "r_other_time_ms": 0.5}}]}}
        root = parse_explain(json.dumps(plan))
        self.assertEqual([n["details"]["misestimate"]["table"] for n in _marked(root)], ["o"])
        analysis = analyze_plan(root)
        advise(analysis, freshness={"shop.orders": {"last_update": "2026-01-01 00:00:00", "n_rows": 9,
                                                    "histograms": []}})
        self.assertEqual(analysis["statistics_evidence"], {})
        data = build_chart_data(root, analysis=analysis, compact=True)
        mark = [n["misestimate"] for n in data["nodes"] if "misestimate" in n][0]
        self.assertIsNone(mark["evidence"])
        self.assertNotIn("ANALYZE TABLE", json.dumps(data))

    def test_identifiers_are_quoted_for_copy_and_paste(self):
        analysis = {"misestimates": [{"table": "t`x"}]}
        evidence = statistics_evidence(analysis, {"we`ird.t`x": {"last_update": None, "n_rows": None,
                                                                  "histograms": ["c1"]}},
                                       capture={"engine": "mariadb", "query": "SELECT * FROM `we``ird`.`t``x`"})
        self.assertEqual(evidence["t`x"]["statement"], "ANALYZE TABLE `we``ird`.`t``x`;")
        self.assertEqual(evidence["t`x"]["text"], "histograms on c1")

    def test_without_freshness_there_is_no_evidence(self):
        root = parse_explain(_read(MYSQL_PLAN))
        analysis = analyze_plan(root)
        advise(analysis)
        self.assertEqual(analysis["statistics_evidence"], {})


class TestChartDataField(unittest.TestCase):
    def test_field_matches_the_record_and_carries_the_evidence(self):
        from test_chart_data import COMPACT_SCHEMA, SCHEMA, validate
        _capture, root, analysis = _capture_analysis(MYSQL_CAPTURE)
        node = _marked(root)[0]
        for compact, schema in ((True, COMPACT_SCHEMA), (False, SCHEMA)):
            data = build_chart_data(root, analysis=analysis, compact=compact)
            validate(data, schema)
            rows = [row for row in data["nodes"] if "misestimate" in row]
            self.assertEqual([row["id"] for row in rows], [node["node_id"]])
            mark = rows[0]["misestimate"]
            expected = dict(node["details"]["misestimate"])
            expected["evidence"] = analysis["statistics_evidence"]["myflames_w2c_stale.orders"]
            self.assertEqual(mark, expected)
            self.assertIn("Row estimate: 451× under (estimated 1, actual 451 rows per loop)", rows[0]["details_text"])
            self.assertIn("Suggested SQL: ANALYZE TABLE `myflames_w2c_stale`.`orders`;", rows[0]["details_text"])
            self.assertIn("Row estimate: 451× under", rows[0]["flame_details"])

    def test_plan_without_misestimates_serializes_as_before(self):
        root = parse_explain(_read(LIMIT_PLAN))
        data = build_chart_data(root, analysis=analyze_plan(root), compact=True)
        self.assertNotIn("misestimate", json.dumps(data))

    def test_plan_text_stays_text(self):
        from myflames.chart_assets import script_json
        nasty = 'o</script><img src=x onerror="alert(1)">'
        root = parse_explain(json.dumps({"operation": "Index lookup on o", "access_type": "index",
                                         "index_access_type": "index_lookup", "table_name": nasty,
                                         "index_name": "k", "estimated_rows": 1.0, "actual_rows": 451.0,
                                         "actual_loops": 40, "actual_last_row_ms": 1.0}))
        analysis = analyze_plan(root)
        advise(analysis, freshness={"db." + nasty: {"last_update": "x", "n_rows": 1, "histograms": []}})
        data = build_chart_data(root, analysis=analysis, compact=True)
        self.assertEqual(json.loads(script_json(data)), data)
        self.assertNotIn("</script", script_json(data))
        for svg in (render_workbench(root, analysis=analysis), render_tree(root), render_bargraph(root),
                    _render_svg(root, "flamegraph", 1200, "Query plan", "ms", node_identity=True)):
            ET.fromstring(svg)
            self.assertNotIn("<img", svg)


class TestRendererMarks(unittest.TestCase):
    def test_mariadb_duplicate_sibling_labels_keep_separate_marks(self):
        from myflames.render import render_explain
        # MariaDB reports aliases, so distinct accesses can even have the
        # same folded path. Identity must keep both frames and tooltips.
        raw = json.dumps({"query_block": {"select_id": 1, "r_total_time_ms": 4, "nested_loop": [
            {"table": {"table_name": "t", "access_type": "ref", "key": "k", "rows": estimate,
                       "r_rows": 1000, "r_loops": 1, "r_table_time_ms": 1}}
            for estimate in (1, 1000)]}})
        root = parse_explain(raw)
        leaves = [n for n in flatten_nodes(root) if n["details"].get("table_name") == "t"]
        self.assertEqual(root["engine"], "mariadb")
        self.assertEqual(len(leaves), 2)
        self.assertEqual(leaves[0]["folded_label"], leaves[1]["folded_label"])
        for svg in (render_explain(raw, "flamegraph"),
                    _render_svg(root, "flamegraph", 1200, "Plan", "ms")):
            frames = [g for g in ET.fromstring(svg).iter(SVG + "g") if g.get("data-label") == leaves[0]["folded_label"]]
            self.assertEqual([g.get("data-node-id") for g in frames], [n["node_id"] for n in leaves])
            self.assertEqual([g.get("data-node-id") for g in frames if g.find(SVG + "line") is not None],
                             [leaves[0]["node_id"]])
            self.assertIn("Row estimate: 1,000× under", frames[0].find(SVG + "title").text)
            self.assertNotIn("Row estimate:", frames[1].find(SVG + "title").text)

    def test_duplicate_flame_labels_mark_only_the_misestimated_node(self):
        from myflames.cli import main
        from myflames.render import render_explain
        # Both leaves print TABLE SCAN [t] starts=1 rows=1000. Only the
        # first is wrong; estimates never formed part of the folded label.
        raw = json.dumps({"operation": "Append", "actual_last_row_ms": 4, "inputs": [
            {"operation": "Sort: t.x", "actual_last_row_ms": 2, "inputs": [
                _scan("Table scan on t", "t", 1, 1000)]},
            {"operation": "Aggregate: count(0)", "actual_last_row_ms": 2, "inputs": [
                _scan("Table scan on t", "t", 1000, 1000)]}]})
        root = parse_explain(raw)
        leaves = [n for n in flatten_nodes(root) if n["details"].get("table_name") == "t"]
        self.assertEqual(leaves[0]["folded_label"], leaves[1]["folded_label"])
        self.assertEqual(len(_marked(root)), 1)
        outputs = [render_explain(raw, "flamegraph"),
                   _render_svg(root, "flamegraph", 1200, "Plan", "ms"),
                   _render_svg(root, "flamegraph", 1200, "Plan", "ms", node_identity=True)]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "plan.json"
            source.write_text(raw, encoding="utf-8")
            with mock.patch.dict(os.environ, {"HOME": directory, "USERPROFILE": directory,
                                               "MYFLAMES_SECRET_BACKEND": "vault"}), redirect_stdout(StringIO()) as out:
                os.environ.pop("MYFLAMES_VAULT_PASSPHRASE", None)
                with mock.patch("sys.argv", ["myflames", str(source), "--type", "flamegraph", "--no-sidecar"]):
                    main()
            outputs.append(out.getvalue())
        for svg in outputs:
            frames = [g for g in ET.fromstring(svg).iter(SVG + "g") if g.get("data-label") == leaves[0]["folded_label"]]
            self.assertEqual(len(frames), 2)
            self.assertEqual(sum(g.find(SVG + "line") is not None for g in frames), 1)
            self.assertEqual([g.get("data-node-id") for g in frames], [n["node_id"] for n in leaves])
            self.assertEqual([g.get("data-node-id") for g in frames if g.find(SVG + "line") is not None],
                             [leaves[0]["node_id"]])
            self.assertIn("Row estimate: 1,000× under", frames[0].find(SVG + "title").text)
            self.assertNotIn("Row estimate:", frames[1].find(SVG + "title").text)

    def test_zero_self_time_filter_stays_in_data_but_has_no_bar(self):
        raw = {"operation": "Filter: (t.x > 0)", "access_type": "filter", "estimated_rows": 1,
               "actual_rows": 1000, "actual_loops": 1, "actual_last_row_ms": 1,
               "inputs": [_scan("Table scan on t", "t", 2000, 2000)]}
        root = parse_explain(json.dumps(raw))
        self.assertEqual(root["self_time"], 0)
        self.assertEqual(_marked(root), [root])
        data = build_chart_data(root, analysis=analyze_plan(root))
        self.assertEqual(data["nodes"][0]["misestimate"]["kind"], "filter")
        self.assertNotIn('data-node-id="{}"'.format(root["node_id"]), render_bargraph(root))

    def setUp(self):
        self.root = parse_explain(_read(MYSQL_PLAN))
        self.flagged = _marked(self.root)[0]
        self.label = self.flagged["details"]["misestimate"]["label"]
        # 6.2 px per code point at 11 px semibold plus 10 px (measured 91.4 px for this label).
        self.width = len(self.label) * 6.2 + 10

    def _clean(self):
        """The same plan without the record: what the views drew before."""
        root = copy.deepcopy(self.root)
        for node in flatten_nodes(root):
            node["details"].pop("misestimate", None)
        return root

    def test_visual_explain_card_has_chip_outline_and_fitted_title(self):
        doc = ET.fromstring(render_workbench(self.root))
        cards = {g.attrib["data-node-id"]: g for g in doc.iter(SVG + "g") if g.attrib.get("class") == "wb-node"}
        card = cards[self.flagged["node_id"]]
        chip = [r for r in card if r.attrib.get("class") == "mfc-mis-chip"]
        outline = [r for r in card if r.attrib.get("class") == "mfc-mis-outline"]
        self.assertEqual(len(chip), 1)
        self.assertEqual(len(outline), 1)
        self.assertAlmostEqual(float(chip[0].attrib["x"]) + float(chip[0].attrib["width"]), BOX_W - 8, places=1)
        self.assertAlmostEqual(float(chip[0].attrib["width"]), self.width, places=1)
        self.assertEqual(outline[0].attrib["stroke-dasharray"], "6 4")
        self.assertEqual(outline[0].attrib["stroke"], MARK_COLOR)
        self.assertEqual(outline[0].attrib["fill"], "none")
        # Outside the card, so the selection stroke and the edges stay clear.
        self.assertEqual((float(outline[0].attrib["x"]), float(outline[0].attrib["width"])), (-3.0, BOX_W + 6.0))
        texts = [t.text for t in card if t.tag == SVG + "text"]
        self.assertEqual(texts[1], self.label, "the chip follows the header title")
        title = texts[0]
        self.assertLessEqual(len(title) * 13 * 0.59, BOX_W - 24 - self.width - 6 + 0.01)
        self.assertEqual(card.attrib["aria-label"], self.flagged["short_label"] + ", " + self.label)
        self.assertIn("Row estimate: 451× under", card.find(SVG + "title").text)
        others = [g for nid, g in cards.items() if nid != self.flagged["node_id"]]
        self.assertTrue(others)
        for g in others:
            self.assertFalse([e for e in g if "mfc-mis" in e.attrib.get("class", "")])
        css = doc.find(SVG + "style").text
        self.assertIn(".wb-plan .mfc-mis{fill:%s;font-size:11px" % MARK_COLOR, css)

    def test_visual_explain_tooltip_names_evidence_only_with_analysis(self):
        _capture, root, analysis = _capture_analysis(MYSQL_CAPTURE)
        node = _marked(root)[0]["node_id"]
        with_evidence = ET.fromstring(render_workbench(root, analysis=analysis))
        card = [g for g in with_evidence.iter(SVG + "g") if g.attrib.get("data-node-id") == node][0]
        self.assertIn("Statistics: myflames_w2c_stale.orders: InnoDB statistics last updated 2026-11-08 22:10:20",
                      card.attrib["data-details"])
        self.assertIn("Suggested SQL: ANALYZE TABLE `myflames_w2c_stale`.`orders`;", card.attrib["data-details"])
        plain = ET.fromstring(render_workbench(root))
        card = [g for g in plain.iter(SVG + "g") if g.attrib.get("data-node-id") == node][0]
        self.assertNotIn("ANALYZE TABLE", card.attrib["data-details"])

    def test_tree_row_has_the_chip_at_the_end_of_its_label_column(self):
        doc = ET.fromstring(render_tree(self.root))
        rows = {g.attrib["data-node-id"]: g for g in doc.iter(SVG + "g") if g.attrib.get("class") == "tree-row"}
        row = rows[self.flagged["node_id"]]
        chip = [r for r in row if r.attrib.get("class") == "mfc-mis-chip"][0]
        # output_tree: LEFT 10, LABEL_W 440, 6 px before the number columns.
        self.assertAlmostEqual(float(chip.attrib["x"]) + float(chip.attrib["width"]), 10 + 440 - 6, places=1)
        mark = [t for t in row if t.attrib.get("class") == "mfc-mis"][0]
        self.assertEqual(mark.text, self.label)
        bg = [r for r in row if r.attrib.get("class") == "row-bg"][0]
        self.assertIn("Row estimate: 451× under (estimated 1, actual 451 rows per loop)", bg.attrib["data-info"])
        for nid, g in rows.items():
            if nid != self.flagged["node_id"]:
                self.assertFalse([e for e in g if "mfc-mis" in e.attrib.get("class", "")])

    def test_bar_chart_bar_has_the_chip_at_the_start_of_its_label_column(self):
        doc = ET.fromstring(render_bargraph(self.root))
        chips = [r for r in doc if r.attrib.get("class") == "mfc-mis-chip"]
        self.assertEqual(len(chips), 1)
        self.assertEqual(float(chips[0].attrib["x"]), 10.0)
        marks = [t for t in doc if t.attrib.get("class") == "mfc-mis"]
        self.assertEqual([t.text for t in marks], [self.label])
        bar = [r for r in doc.iter(SVG + "rect") if r.attrib.get("data-node-id") == self.flagged["node_id"]][0]
        self.assertAlmostEqual(float(chips[0].attrib["y"]), float(bar.attrib["y"]) + 6)
        self.assertIn("Row estimate: 451× under", bar.attrib["data-info"])

    def test_flame_frame_gets_an_underline_and_nothing_else_changes(self):
        marked = _render_svg(self.root, "flamegraph", 1200, "Query plan", "ms", node_identity=True)
        before = _render_svg(self._clean(), "flamegraph", 1200, "Query plan", "ms", node_identity=True)
        doc = ET.fromstring(marked)
        frames = {g.attrib.get("data-node-id"): g for g in doc.iter(SVG + "g") if g.attrib.get("data-node-id")}
        frame = frames[self.flagged["node_id"]]
        line = frame.find(SVG + "line")
        rect = frame.find(SVG + "rect")
        x, w = float(rect.attrib["x"]), float(rect.attrib["width"])
        y, h = float(rect.attrib["y"]), float(rect.attrib["height"])
        self.assertEqual(line.attrib["class"], "fg-mark")
        self.assertEqual(line.attrib["stroke"], flamegraph.MARK_COLOR)
        self.assertEqual(line.attrib["stroke-width"], "2")
        self.assertAlmostEqual(float(line.attrib["x1"]), x + 1, places=2)
        self.assertAlmostEqual(float(line.attrib["x2"]), x + w - 1, places=2)
        self.assertAlmostEqual(float(line.attrib["y1"]), y + h - 1, places=2)
        self.assertEqual(sum(1 for g in frames.values() if g.find(SVG + "line") is not None), 1)
        # Labels, '..' truncation, starts= and every header are as before;
        # only the underline and the frame's detail line are new.
        strip = re.sub(r'<line class="fg-mark"[^>]*/>\n', "", marked)
        self.assertEqual(re.findall(r"<text[^>]*>[^<]*</text>", strip), re.findall(r"<text[^>]*>[^<]*</text>", before))
        tips = re.findall(r"<title>([^<]*)</title>", strip)
        old_tips = re.findall(r"<title>([^<]*)</title>", before)
        self.assertEqual([t.split("&#10;")[0] for t in tips], [t.split("&#10;")[0] for t in old_tips])
        changed = [(a, b) for a, b in zip(tips, old_tips) if a != b]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0][0].replace("Row estimate: 451× under (estimated 1, actual 451 rows per loop)&#10;", ""),
                         changed[0][1])
        # The marked frame keeps its Tanel Poder style label (uppercase, starts=).
        self.assertEqual(frame.attrib["data-label"], "INDEX LOOKUP [orders.idx_customer] starts=40 rows=451")

    def test_standalone_flame_script_moves_the_underline_with_its_frame(self):
        script = flamegraph.SCRIPT
        put = script[script.index("function put("):script.index("function within(")]
        self.assertIn('f.mark.setAttribute("x1", (x + 1).toFixed(2));', put)
        self.assertIn('f.mark.setAttribute("x2", Math.max(x + 1, x + w - 1).toFixed(2));', put)
        self.assertIn('mark: child(g, "line")', script)

    def test_every_flame_path_marks_the_misestimated_frames(self):
        from myflames.render import render_explain
        svg = render_explain(_read(MYSQL_PLAN), "flamegraph")
        self.assertEqual(svg.count('class="fg-mark"'), 1)
        self.assertEqual(misestimate_marks(self.root), {"operator:" + self.flagged["node_id"]})
        clean = render_explain(json.dumps(json.loads(_read(LIMIT_PLAN))), "flamegraph")
        self.assertNotIn("fg-mark", clean)

    def test_marks_match_the_typescript_sources(self):
        charts = REPO / "apps" / "workspace" / "src" / "charts"
        if not charts.is_dir():
            self.skipTest("TypeScript sources are not in this checkout")
        marks = (charts / "core" / "marks.ts").read_text(encoding="utf-8")
        from myflames import output_tree, output_workbench
        self.assertIn('export const MARK_COLOR = "%s";' % output_workbench.MARK_COLOR, marks)
        self.assertIn('export const MARK_FILL = "%s";' % output_workbench.MARK_FILL, marks)
        self.assertIn("export const MARK_CHAR_W = %s;" % output_workbench.MARK_CHAR_W, marks)
        self.assertIn("export const MARK_PAD = %d;" % output_workbench.MARK_PAD, marks)
        self.assertIn("export const MARK_ROW_H = %d;" % output_tree.MARK_ROW_H, marks)
        self.assertIn("export const MARK_GAP = %d;" % output_tree.MARK_GAP, marks)
        workbench = (charts / "workbench.ts").read_text(encoding="utf-8")
        for name in ("MARK_INSET", "MARK_H", "OUTLINE_GAP"):
            self.assertIn("const %s = %d;" % (name, getattr(output_workbench, name)), workbench)
        flame = (charts / "flamegraph.ts").read_text(encoding="utf-8")
        self.assertIn('export const MARK_COLOR = "%s";' % flamegraph.MARK_COLOR, flame)
        theme_defaults = (charts / "core" / "themeDefaults.ts").read_text(encoding="utf-8")
        self.assertIn("--mfc-estimate:%s;" % output_workbench.MARK_COLOR, theme_defaults)
        # Never a status or search color.
        self.assertNotIn(output_workbench.MARK_COLOR, (flamegraph.MATCH_COLOR, "#b42318", "#007aff"))
        self.assertNotEqual(flamegraph.MARK_COLOR, flamegraph.MATCH_COLOR)


if __name__ == "__main__":
    unittest.main()
