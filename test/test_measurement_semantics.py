"""Measured, estimated and unavailable plan numbers across every projection.

Covers the parser's row and loop semantics (MariaDB r_rows / r_loops, MySQL
actual_rows / actual_loops), set operation and join cardinality, estimated
plans in the full-scan gate, hash join diagnoses, the sidecar summary and
its digest, HTML and executive-summary projections, XML escaping, flame
tooltip enhancement, the CLI engine and stdout contracts, and MariaDB 10.6
plans that repeat the "table" key.

Server semantics (verified in the server sources):

* MariaDB ``r_loops`` counts scans of a table and ``r_rows`` is the average
  rows read per scan, null when the table was never scanned
  (sql/sql_explain.cc Explain_table_access::print_explain_json,
  sql/sql_analyze_stmt.h Table_access_tracker).
* A MariaDB set operation's ``union_result`` r_rows is read from the result
  table by the fake select, after INTERSECT/EXCEPT removed rows
  (Explain_union::print_explain_json_regular, sql/sql_union.cc).
* MySQL ``actual_rows`` is rows per init call and ``actual_loops`` the
  number of init calls; both are null for an iterator that never ran
  (sql/join_optimizer/explain_access_path.cc).
* A MySQL hash join builds from its second child, heading "Hash"
  (explain_access_path.cc HASH_JOIN; access_path.cc HashJoinIterator).
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(TEST_DIR)
FIXTURES = os.path.join(TEST_DIR, "fixtures")
sys.path.insert(0, REPO_DIR)

from myflames import cli, parser as P  # noqa: E402
from myflames.chart_data import build_chart_data  # noqa: E402
from myflames.digest import build_digest  # noqa: E402
from myflames.findings import evaluate_check  # noqa: E402
from myflames.glossary import _pick_primary_issue, generate_executive_summary  # noqa: E402
from myflames.output_sidecar import (  # noqa: E402
    SidecarValidationError, _compute_plan_summary, build_sidecar, validate_sidecar,
)
from myflames.parser import analyze_plan, flatten_nodes, load_explain_json, parse_explain  # noqa: E402


def _text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _fixture(name):
    return os.path.join(FIXTURES, name)


def _by_label(root, needle):
    return [n for n in flatten_nodes(root) if needle in n["short_label"]]


def _sidecar(root):
    return build_sidecar(root, analyze_plan(root), engine=root.get("engine"))


# MySQL EXPLAIN FORMAT=JSON (no ANALYZE): one estimated million-row scan.
ESTIMATED_SCAN = json.dumps({
    "query": "select * from t1", "operation": "Table scan on t1", "table_name": "t1",
    "access_type": "table", "schema_name": "test", "estimated_rows": 1000000.0,
    "estimated_total_cost": 100000.0, "inputs": [],
})

# MySQL EXPLAIN ANALYZE: 10,000 outer rows, 10,000 inner lookups of 2 rows.
NESTED_LOOP = json.dumps({
    "operation": "Nested loop inner join", "access_type": "join", "join_type": "inner join",
    "join_algorithm": "nested_loop", "actual_rows": 20000.0, "actual_loops": 1,
    "actual_first_row_ms": 0.1, "actual_last_row_ms": 50.0, "estimated_rows": 20000.0,
    "inputs": [
        {"operation": "Table scan on o", "table_name": "o", "access_type": "table",
         "actual_rows": 10000.0, "actual_loops": 1, "actual_first_row_ms": 0.01,
         "actual_last_row_ms": 10.0, "estimated_rows": 10000.0, "inputs": []},
        {"operation": "Index lookup on i using k (a=o.a)", "table_name": "i", "index_name": "k",
         "access_type": "index", "index_access_type": "index_lookup", "actual_rows": 2.0,
         "actual_loops": 10000, "actual_first_row_ms": 0.001, "actual_last_row_ms": 0.003,
         "estimated_rows": 2.0, "inputs": []},
    ],
})


class TestMariaDBUnavailableRows(unittest.TestCase):
    """Finding 7: a null r_rows is not a measured zero."""

    def test_null_r_rows_stays_unknown(self):
        root = parse_explain(_text(_fixture("mariadb-11.4-004-pk-lookup.json")))
        d = root["details"]
        self.assertIsNone(d["actual_rows"])
        self.assertFalse(d["rows_measured"])
        # r_loops 0 is a measured count: the table was never scanned during
        # execution (a const table is read during optimization).
        self.assertEqual(d["actual_loops"], 0)
        node = build_chart_data(root)["nodes"][0]
        self.assertIsNone(node["actual_rows"])
        self.assertEqual(node["metrics"]["rows"], [None, "Not measured"])

    def test_measured_r_rows_kept_per_scan(self):
        raw = load_explain_json(_text(_fixture("mariadb-13.0-analyze-join-3t.json")))
        tables = {}
        for entry in raw["query_block"]["nested_loop"]:
            t = entry.get("table") or entry["block-nl-join"]["table"]
            tables[t["table_name"]] = t
        root = parse_explain(_text(_fixture("mariadb-13.0-analyze-join-3t.json")))
        for node in flatten_nodes(root):
            name = node["details"]["table_name"]
            if name in tables:
                self.assertAlmostEqual(node["details"]["actual_rows"], float(tables[name]["r_rows"]))
                self.assertEqual(node["details"]["actual_loops"], tables[name]["r_loops"])


class TestMariaDBJoinCardinality(unittest.TestCase):
    """MariaDB has no counter on the join itself. The rows a join returns are
    the next table's r_loops (sub_select counts one r_scans per incoming
    row, sql/sql_select.cc), or the block-nl-join r_loops when that table
    uses a join buffer (jbuf_loops_tracker in sub_select_cache, MDEV-30806,
    10.6.13+). The last join of a select is not measured."""

    def test_join_rows_are_the_next_tables_loops(self):
        text = _text(_fixture("mariadb-13.0-analyze-join-3t.json"))
        last = load_explain_json(text)["query_block"]["nested_loop"][2]
        reached = (last.get("table") or last["block-nl-join"]["table"])["r_loops"]
        root = parse_explain(text)
        inner_join = root["children"][0]
        self.assertEqual(inner_join["details"]["access_type"], "join")
        self.assertEqual(inner_join["details"]["actual_rows"], float(reached))
        self.assertTrue(inner_join["details"]["rows_measured"])
        chart = {n["id"]: n for n in build_chart_data(root)["nodes"]}
        self.assertEqual(chart[inner_join["node_id"]]["actual_rows"], float(reached))
        # The last join has no next table: not measured.
        self.assertIsNone(root["details"]["actual_rows"])
        self.assertIsNone(chart[root["node_id"]]["actual_rows"])
        self.assertGreater(root["total_time"], 0)

    def test_buffered_next_table_uses_block_loops(self):
        plan = {"query_block": {"select_id": 1, "r_loops": 1, "r_total_time_ms": 2.0, "nested_loop": [
            {"table": {"table_name": "t1", "access_type": "ALL", "r_loops": 1, "rows": 10,
                       "r_rows": 10, "r_filtered": 100, "r_table_time_ms": 0.1, "r_other_time_ms": 0.1}},
            {"table": {"table_name": "t2", "access_type": "ref", "r_loops": 10, "rows": 3,
                       "r_rows": 3, "r_filtered": 50, "r_table_time_ms": 0.1, "r_other_time_ms": 0.1}},
            {"block-nl-join": {"table": {"table_name": "t3", "access_type": "ALL", "r_loops": 1,
                                         "rows": 40, "r_rows": 40, "r_filtered": 100,
                                         "r_table_time_ms": 0.1, "r_other_time_ms": 0.1},
                               "buffer_type": "flat", "join_type": "BNL", "r_loops": 15,
                               "r_filtered": 10}}]}}
        root = parse_explain(json.dumps(plan))
        # (t1 NL t2) returned 15 rows: each was put in t3's join buffer. t3's
        # own r_loops (1) counts buffer refills, not rows.
        self.assertEqual(root["children"][0]["details"]["actual_rows"], 15.0)
        self.assertIsNone(root["details"]["actual_rows"])

    def test_weedout_output_not_copied_from_input(self):
        root = parse_explain(_text(os.path.join(TEST_DIR, "mariadb-explain-weedout.json")))
        self.assertTrue(root["full_label"].startswith("Remove duplicates"))
        self.assertIsNone(root["details"]["actual_rows"])

    def test_mysql_join_rows_unchanged(self):
        """MySQL reports each join's own actual_rows; nothing is derived."""
        root = parse_explain(NESTED_LOOP)
        self.assertEqual(root["details"]["actual_rows"], 20000.0)


class TestSetOperationRows(unittest.TestCase):
    """Finding 1: the set operation's measured result, not a sum."""

    CASES = (
        ("mariadb-11.4-025-union-distinct.json", "Union"),
        ("mariadb-11.8-intersect.json", "Intersect"),
        ("mariadb-13.0-except.json", "Except"),
    )

    def test_result_rows_come_from_union_result(self):
        for name, op in self.CASES:
            with self.subTest(fixture=name):
                text = _text(_fixture(name))
                expected = load_explain_json(text)["query_block"]["union_result"]["r_rows"]
                root = parse_explain(text)
                self.assertEqual(root["full_label"], op)
                self.assertEqual(root["details"]["actual_rows"], float(expected))
                members = sum(c["details"]["actual_rows"] for c in root["children"])
                self.assertNotEqual(root["details"]["actual_rows"], members)
                self.assertEqual(_sidecar(root)["plan_summary"]["rows_sent"], int(expected))

    def test_streamed_union_all_is_not_measured(self):
        """UNION ALL without a result table streams rows; a LIMIT/OFFSET on
        the whole unit is applied when sending (select_union_direct::send_data,
        sql/sql_union.cc) and not shown in the plan, and a table member's
        r_rows counts rows before its WHERE. The output is unknown."""
        root = parse_explain(_text(_fixture("mariadb-11.4-024-union-all.json")))
        self.assertIsNone(root["details"]["actual_rows"])
        plan = {"query_block": {"union_result": {"query_specifications": [
            {"query_block": {"select_id": 1, "r_loops": 1, "nested_loop": [{"table": {
                "table_name": "a", "access_type": "ALL", "r_loops": 1, "rows": 1000,
                "r_rows": 1000, "r_filtered": 1, "attached_condition": "a.x = 1"}}]}},
            {"query_block": {"select_id": 2, "operation": "UNION", "r_loops": 1, "nested_loop": [{"table": {
                "table_name": "b", "access_type": "ALL", "r_loops": 1, "rows": 500,
                "r_rows": 500, "r_filtered": 2, "attached_condition": "b.x = 1"}}]}}]}}}
        root = parse_explain(json.dumps(plan))
        self.assertIsNone(root["details"]["actual_rows"])
        self.assertIsNone(_compute_plan_summary(root)["rows_sent"])

    def test_unread_result_table_is_unknown(self):
        """A recursive CTE's union_result prints r_rows null (never read by
        the fake select): unknown, not the members' sum."""
        root = parse_explain(_text(_fixture("mariadb-13.0-cte-recursive-hierarchy-subq.json")))
        unions = [n for n in flatten_nodes(root) if n["details"]["access_type"] == "union"]
        self.assertTrue(unions)
        for node in unions:
            self.assertIsNone(node["details"]["actual_rows"])

    def test_mysql_union_rows_are_reported_ones(self):
        text = _text(_fixture("explain-062-union-distinct-shipped-delivered.json"))
        root = parse_explain(text)
        self.assertEqual(root["details"]["actual_rows"], json.loads(text)["actual_rows"])


class TestRowsExamined(unittest.TestCase):
    """Finding 3: rows examined multiplies per-loop rows by loops."""

    def test_mysql_inner_lookups_count_every_loop(self):
        ps = _compute_plan_summary(parse_explain(NESTED_LOOP))
        self.assertEqual(ps["rows_examined_estimate"], 10000 + 2 * 10000)
        self.assertEqual(ps["rows_sent"], 20000)

    def test_mariadb_r_rows_times_r_loops(self):
        text = _text(_fixture("mariadb-13.0-analyze-join-3t.json"))
        expected = 0.0
        for entry in load_explain_json(text)["query_block"]["nested_loop"]:
            t = entry.get("table") or entry["block-nl-join"]["table"]
            expected += t["r_rows"] * t["r_loops"]
        ps = _compute_plan_summary(parse_explain(text))
        self.assertEqual(ps["rows_examined_estimate"], int(round(expected)))
        # A MariaDB join reports no output count.
        self.assertIsNone(ps["rows_sent"])
        self.assertIn("rows sent not measured / {:,} examined".format(int(round(expected))),
                      build_digest(_sidecar(parse_explain(text))))


class TestEstimatedPlanSummary(unittest.TestCase):
    """Finding 8: unavailable measurements are null and read as such."""

    def _check(self, root):
        payload = _sidecar(root)
        validate_sidecar(payload)
        ps = payload["plan_summary"]
        self.assertIsNone(ps["total_time_ms"])
        self.assertIsNone(ps["rows_sent"])
        self.assertIsNone(ps["rows_examined_estimate"])
        digest = build_digest(payload)
        self.assertIn("rows not measured", digest)
        self.assertNotIn("0 sent", digest)
        self.assertNotIn("returns 0 rows", payload["executive_summary"])
        self.assertIn("rows not measured", payload["executive_summary"])
        from myflames.output_html_report import _render_exec_summary
        html = _render_exec_summary(payload)
        self.assertIn("<dt>Rows returned</dt><dd>Not measured</dd>", html)
        self.assertIn("<dt>Total time</dt><dd>Not measured</dd>", html)
        return payload

    def test_mysql_estimated_plan(self):
        self._check(parse_explain(ESTIMATED_SCAN))

    def test_mariadb_estimated_plan(self):
        self._check(parse_explain(_text(_fixture("mariadb-13.0-explain-join-3t.json"))))

    def test_validator_accepts_null_measurements_only(self):
        payload = _sidecar(parse_explain(ESTIMATED_SCAN))
        bad = dict(payload, plan_summary=dict(payload["plan_summary"], operator_count=None))
        with self.assertRaises(SidecarValidationError):
            validate_sidecar(bad)
        bad = dict(payload, plan_summary=dict(payload["plan_summary"], rows_sent=True))
        with self.assertRaises(SidecarValidationError):
            validate_sidecar(bad)

    def test_schema_documents_nullable_summary(self):
        with open(os.path.join(REPO_DIR, "docs", "schemas", "sidecar-v1.json"), encoding="utf-8") as f:
            schema = json.load(f)
        self.assertIn(_sidecar(parse_explain(ESTIMATED_SCAN))["schema_version"],
                      schema["properties"]["schema_version"]["enum"])
        props = schema["properties"]["plan_summary"]["properties"]
        for key in ("total_time_ms", "rows_sent", "rows_examined_estimate"):
            self.assertIn("null", props[key]["type"])


class TestEstimatedFullScanGate(unittest.TestCase):
    """Finding 4: an estimated plan's large scan trips the full_scan gate."""

    def test_mysql_estimated_scan_is_flagged_as_estimate(self):
        root = parse_explain(ESTIMATED_SCAN)
        a = analyze_plan(root)
        self.assertEqual(a["full_scans"][0]["table"], "t1")
        self.assertTrue(a["full_scans"][0]["estimated"])
        self.assertIn("Full table scan: t1 (about 1000000 rows, estimated)", a["warnings"])
        matched = evaluate_check(_sidecar(root), ["full_scan"])
        self.assertEqual([w["category"] for w in matched], ["full_scan"])
        kind, text = _pick_primary_issue(a)
        self.assertEqual(kind, "full_scan")
        self.assertEqual(text, "full scan of t1 (about 1,000,000 rows, estimated)")

    def test_check_cli_fails_on_estimated_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plan.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write(ESTIMATED_SCAN)
            proc = subprocess.run(
                [sys.executable, "-m", "myflames", "check", "--fail-on", "full_scan", path],
                cwd=REPO_DIR, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("[warn/full_scan] Full table scan: t1 (about 1000000 rows, estimated)",
                      proc.stdout + proc.stderr)

    def test_small_estimated_inner_scan_of_large_join_is_flagged(self):
        """Loops matter for estimates too: a 50-row inner scan run once per
        outer row of a 10,000-row scan reads 500,000 rows."""
        plan = json.dumps({
            "operation": "Nested loop inner join", "access_type": "join",
            "join_algorithm": "nested_loop", "estimated_rows": 500000.0, "inputs": [
                {"operation": "Table scan on big", "table_name": "big", "access_type": "table",
                 "estimated_rows": 10000.0, "inputs": []},
                {"operation": "Table scan on small", "table_name": "small", "access_type": "table",
                 "estimated_rows": 50.0, "inputs": []}]})
        tables = [s["table"] for s in analyze_plan(parse_explain(plan))["full_scans"]]
        self.assertEqual(tables, ["big", "small"])

    def test_mariadb_estimated_scan_is_flagged(self):
        root = parse_explain(_text(_fixture("mariadb-13.0-explain-join-3t.json")))
        scans = analyze_plan(root)["full_scans"]
        self.assertEqual([s["table"] for s in scans], ["c"])
        self.assertTrue(scans[0]["estimated"])

    def test_measured_trivial_scan_still_exempt(self):
        """The measured exemption is unchanged: a tiny measured scan is not
        flagged (MySQL: the probe fixture's scans are large, so use rows)."""
        plan = json.loads(NESTED_LOOP)
        plan["inputs"][0]["actual_rows"] = 10.0
        plan["inputs"][1]["actual_loops"] = 10
        self.assertEqual(analyze_plan(parse_explain(json.dumps(plan)))["full_scans"], [])

    def test_unexecuted_scan_in_analyzed_plan_is_not_flagged(self):
        """MySQL writes null counters for an iterator that never ran."""
        plan = json.loads(NESTED_LOOP)
        plan["inputs"].append({"operation": "Table scan on z", "table_name": "z", "access_type": "table",
                               "actual_rows": None, "actual_loops": None, "actual_last_row_ms": None,
                               "estimated_rows": 1000000.0, "inputs": []})
        tables = [s["table"] for s in analyze_plan(parse_explain(json.dumps(plan)))["full_scans"]]
        self.assertNotIn("z", tables)


class TestHashJoinDiagnoses(unittest.TestCase):
    """Findings 5 and 6."""

    def test_expression_hash_join_is_not_called_quadratic(self):
        root = parse_explain(_text(os.path.join(TEST_DIR, "mysql-explain-hash-join-probe.json")))
        a = analyze_plan(root)
        self.assertEqual(a["nonsargable_joins"], [])
        self.assertFalse(any("Non-sargable" in w for w in a["warnings"]))
        self.assertFalse(any("O(outer" in s for s in a["suggestions"]))
        self.assertEqual(evaluate_check(_sidecar(root), ["error"]), [])

    def test_hash_join_without_equijoin_condition_still_flagged(self):
        """Inner hash join (no condition) with extra conditions compares
        every row pair, so a function there is still reported."""
        plan = json.dumps({
            "operation": "Inner hash join (no condition), extra conditions: (t1.d like concat(t2.y,'%'))",
            "access_type": "join", "join_algorithm": "hash", "hash_condition": [],
            "actual_rows": 1.0, "actual_loops": 1, "actual_last_row_ms": 1.0, "inputs": [
                {"operation": "Table scan on t1", "table_name": "t1", "access_type": "table",
                 "actual_rows": 5.0, "actual_loops": 1, "actual_last_row_ms": 0.1, "inputs": []},
                {"heading": "Hash", "operation": "Table scan on t2", "table_name": "t2",
                 "access_type": "table", "actual_rows": 5.0, "actual_loops": 1,
                 "actual_last_row_ms": 0.1, "inputs": []}]})
        hits = analyze_plan(parse_explain(plan))["nonsargable_joins"]
        self.assertEqual([h["function"] for h in hits], ["CONCAT"])

    def test_mariadb_bnl_join_still_flagged(self):
        """MariaDB flat BNL compares every buffered row with every inner row."""
        a = analyze_plan(parse_explain(_text(os.path.join(TEST_DIR, "mariadb-explain-block-nl-join.json"))))
        self.assertTrue(a["nonsargable_joins"])

    def test_build_rows_come_from_the_hash_input(self):
        text = _text(_fixture("explain-099-hypergraph-on-5t-avg-salary.json"))
        root = parse_explain(text)
        hj = [n for n in flatten_nodes(root) if n["details"]["join_algorithm"] == "hash"][0]
        build = hj["children"][1]
        self.assertEqual(build["details"]["heading"], "Hash")
        a = analyze_plan(root)
        msg = "1 hash join(s) — uses join_buffer_size (~{} rows in build phase)".format(
            int(build["details"]["actual_rows"]))
        self.assertIn(msg, a["warnings"])
        self.assertNotEqual(int(build["details"]["actual_rows"]), int(hj["rows"]))
        self.assertTrue(all(h["message"] == msg for h in a["node_highlights"]
                            if h["short_label"] == hj["short_label"] and "hash join" in h["message"]))

    def test_mariadb_bnlh_build_count_is_block_loops(self):
        """MariaDB BNLH hashes the outer rows put in the join buffer; each is
        counted by jbuf_loops_tracker (sub_select_cache, sql/sql_select.cc)
        and printed as the block-nl-join r_loops (10.6.13+): the build
        count, summed over buffer refills."""
        text = _text(os.path.join(TEST_DIR, "mariadb-explain-block-nl-join.json"))
        raw = load_explain_json(text)
        block = raw["query_block"]["nested_loop"][1]["block-nl-join"]
        block["join_type"] = "BNLH"
        a = analyze_plan(parse_explain(json.dumps(raw)))
        self.assertEqual([(h["rows"], h["build"]) for h in a["hash_joins"]],
                         [(float(block["r_loops"]), "buffered")])
        self.assertIn("1 hash join(s) — uses join_buffer_size (~{} rows put in the join "
                      "buffer, over all refills)".format(block["r_loops"]), a["warnings"])
        # Before MDEV-30806 (10.6.13) the block has no r_loops: not shown yet.
        del block["r_loops"]
        a = analyze_plan(parse_explain(json.dumps(raw)))
        self.assertEqual([h["rows"] for h in a["hash_joins"]], [None])
        self.assertIn("1 hash join(s) — uses join_buffer_size", a["warnings"])


class TestXmlEscaping(unittest.TestCase):
    """Finding 9: XML-invalid characters never reach an SVG."""

    def test_helper_replaces_invalid_characters(self):
        self.assertEqual(P.xml_escape("a\x01b<\"&\x0b"), "a\ufffdb&lt;&quot;&amp;\ufffd")
        self.assertEqual(P.xml_escape("tab\tnl\ncr\r"), "tab\tnl\ncr\r")

    def test_every_renderer_stays_well_formed(self):
        plan = json.loads(_text(os.path.join(TEST_DIR, "mysql-explain-hash-join.json")))
        plan["operation"] += " \x01ctl"
        plan["condition"] = "x = '\x01\x02\x0b'"
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "ctl.json")
            with open(src, "w", encoding="utf-8") as f:
                json.dump(plan, f)
            for view in ("flamegraph", "bargraph", "tree", "workbench"):
                with self.subTest(view=view):
                    out = os.path.join(tmp, view + ".svg")
                    proc = subprocess.run([sys.executable, "-m", "myflames", "--type", view,
                                           src, "-o", out, "--no-sidecar"],
                                          cwd=REPO_DIR, capture_output=True, text=True)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    ET.parse(out)


class TestFlameTooltipEnhancement(unittest.TestCase):
    """Finding 10: each frame gets its own operator's details, linearly."""

    PLAN = json.dumps({
        "operation": "Append", "access_type": "append", "actual_rows": 2.0, "actual_loops": 1,
        "actual_last_row_ms": 5.0, "inputs": [
            {"operation": "Table scan on t1", "table_name": "t1", "access_type": "table",
             "actual_rows": 11.0, "actual_loops": 1, "actual_last_row_ms": 1.0, "inputs": []},
            {"operation": "Table scan on t10", "table_name": "t10", "access_type": "table",
             "actual_rows": 22.0, "actual_loops": 1, "actual_last_row_ms": 2.0, "inputs": []}]})

    def _svg(self, root):
        from myflames.flamegraph import folded_to_svg, plan_folded
        folded, unit = plan_folded(P.build_flame_entries(root), root["total_time"], keep_root=True)
        return folded_to_svg(folded, countname=unit)

    def test_similar_labels_get_their_own_details(self):
        root = parse_explain(self.PLAN)
        out = P.enhance_flame_titles(self._svg(root), root)
        titles = {t.split(" (")[0]: t for t in
                  (e.text for e in ET.fromstring(out).iter("{http://www.w3.org/2000/svg}title"))}
        t10 = titles["TABLE SCAN [t10] starts=1 rows=22"]
        self.assertIn("Table: t10\n", t10)
        self.assertIn("Rows: 22 actual", t10)
        self.assertIn("Table: t1\n", titles["TABLE SCAN [t1] starts=1 rows=11"])

    def test_each_title_looks_up_one_operator(self):
        root = parse_explain(self.PLAN)
        sizes = []
        real = P.enhance_tooltip_flame

        def spy(original, op_details):
            sizes.append(len(op_details))
            return real(original, op_details)
        with mock.patch.object(P, "enhance_tooltip_flame", spy):
            P.enhance_flame_titles(self._svg(root), root)
        self.assertEqual(sizes, [1, 1, 1])


class TestCliContracts(unittest.TestCase):
    """Findings 11 and 12."""

    def test_digest_uses_the_parser_engine(self):
        path = _fixture("mysql-8.4-v1-intersect.json")
        self.assertEqual(parse_explain(_text(path))["engine"], "mysql")
        proc = subprocess.run([sys.executable, "-m", "myflames", "digest", path],
                              cwd=REPO_DIR, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("engine mysql |", proc.stdout)

    def test_file_sidecar_uses_the_parser_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "plan.svg")
            proc = subprocess.run([sys.executable, "-m", "myflames", "--type", "tree",
                                   _fixture("mysql-8.4-v1-intersect.json"), "-o", out],
                                  cwd=REPO_DIR, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(os.path.join(tmp, "plan.json"), encoding="utf-8") as f:
                self.assertEqual(json.load(f)["source"]["engine"], "mysql")

    def test_mariadb_engine_unchanged(self):
        root = parse_explain(_text(_fixture("mariadb-11.4-025-union-distinct.json")))
        self.assertEqual(root["engine"], "mariadb")

    def test_stdout_failure_exits_2_with_diagnostic(self):
        class Broken(io.StringIO):
            def write(self, _):
                raise BrokenPipeError(32, "Broken pipe")
        err = io.StringIO()
        with mock.patch.object(sys, "stdout", Broken()), mock.patch.object(sys, "stderr", err):
            with self.assertRaises(SystemExit) as ctx:
                cli._write_output("data", None)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("Cannot write to standard output: Broken pipe", err.getvalue())

    def test_closed_pipe_has_no_traceback(self):
        plan = json.loads(_text(os.path.join(TEST_DIR, "mysql-explain-hash-join.json")))
        big = {"operation": "Append", "access_type": "append", "actual_rows": 1.0,
               "actual_loops": 1, "actual_last_row_ms": 10.0, "inputs": [plan] * 200}
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "big.json")
            with open(src, "w", encoding="utf-8") as f:
                json.dump(big, f)
            proc = subprocess.Popen([sys.executable, "-m", "myflames", "--type", "tree", src],
                                    cwd=REPO_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            proc.stdout.close()
            err = proc.stderr.read().decode("utf-8", "replace")
            proc.stderr.close()
            code = proc.wait()
        self.assertEqual(code, 2, err)
        self.assertNotIn("Traceback", err)
        self.assertIn("Cannot write to standard output", err)


class TestMariaDB106RepeatedTableKeys(unittest.TestCase):
    """MariaDB 10.7 and earlier write a join's members as repeated "table"
    keys in one object (origin/10.6 sql/sql_explain.cc,
    Explain_basic_join::print_explain_json_interns); 10.8.1 and later write
    a "nested_loop" array (MDEV-27036, commit 73df7a30094). The fixtures are
    live captures whose capture recorded server_version
    10.6.28-MariaDB-ubu2204-log."""

    def test_join_keeps_every_table(self):
        root = parse_explain(_text(_fixture("mariadb-10.6-analyze-join-group-repeated-table.json")))
        tables = [n["details"]["table_name"] for n in flatten_nodes(root) if n["details"]["table_name"]]
        self.assertEqual(tables, ["u", "o"])
        ps = _compute_plan_summary(root)
        self.assertEqual(ps["rows_examined_estimate"], 3000 + 4 * 3000)

    def test_subquery_plan_keeps_its_full_scan(self):
        root = parse_explain(_text(_fixture("mariadb-10.6-analyze-in-subquery-repeated-table.json")))
        tables = [n["details"]["table_name"] for n in flatten_nodes(root) if n["details"]["table_name"]]
        self.assertEqual(tables, ["users", "<subquery2>", "orders"])
        self.assertIn("users", [s["table"] for s in analyze_plan(root)["full_scans"]])

    def test_hook_builds_ordered_nested_loop(self):
        data = load_explain_json('{"a": 1, "table": {"n": 1}, "block-nl-join": {"table": {"n": 2}}, '
                                 '"table": {"n": 3}, "b": 2}')
        self.assertEqual(list(data), ["a", "nested_loop", "b"])
        self.assertEqual(data["nested_loop"], [{"table": {"n": 1}},
                                               {"block-nl-join": {"table": {"n": 2}}},
                                               {"table": {"n": 3}}])

    def test_single_member_objects_unchanged(self):
        """MySQL format 1 and MariaDB 10.8.1+ never put two members in one
        object, so their documents load exactly as json.loads loads them."""
        for path in (_fixture("mysql-8.4-v1-intersect.json"),
                     _fixture("mariadb-11.4-025-union-distinct.json"),
                     os.path.join(TEST_DIR, "mysql-explain-hash-join.json")):
            with self.subTest(path=os.path.basename(path)):
                text = _text(path)
                self.assertEqual(load_explain_json(text), json.loads(text))


class TestTempTableSpillWording(unittest.TestCase):
    """The executive summary never claims a spill or an on-disk engine from
    configuration size: no plan format reports one (MariaDB temporary_table
    has no counters, Explain_aggr_tmp_table in sql/sql_explain.h; MySQL's
    MATERIALIZE JSON has no disk field)."""

    WARNINGS = (
        ("mariadb", "Query materializes 1 temp table(s); tmp_table_size/max_heap_table_size "
                    "is 16.0 MB. This limit alone does not show whether a table spilled to disk."),
        ("mysql", "Query materializes 1 temp table(s); tmp_table_size is 1.5 MB. "
                  "This limit alone does not show whether a table spilled to disk."),
    )

    def test_wording_is_evidence_based(self):
        for engine, warning in self.WARNINGS:
            with self.subTest(engine=engine):
                kind, text = _pick_primary_issue({"environment_warnings": [warning]})
                self.assertEqual(kind, "tmp_spill")
                self.assertNotIn("InnoDB", text)
                self.assertNotIn("will spill", text)
                self.assertIn("does not show whether it spilled to disk", text)
                self.assertIn(warning.split("; ")[1].split(". This")[0], text)


class TestUnmeasuredRowsInChartText(unittest.TestCase):
    """Bar chart and tree details print "not measured", never "Rows: 0"
    (the TS views match, apps/workspace/test/parity.mjs)."""

    def test_bar_and_tree_details(self):
        from myflames.output_bargraph import _build_bar_info
        from myflames.output_tree import _row_info
        root = parse_explain(_text(_fixture("mariadb-13.0-analyze-join-3t.json")))
        self.assertIn("Rows: not measured", _row_info(root, root["total_time"], "ms"))
        self.assertIn("Rows: not measured", _build_bar_info(root, "0.1", 10.0, "ms"))
        table = _by_label(root, "Table scan [c]")[0]
        self.assertIn("Rows: 12", _row_info(table, root["total_time"], "ms"))


class TestExecutiveSummaryRows(unittest.TestCase):

    def test_measured_examined_without_rows_sent(self):
        root = parse_explain(_text(_fixture("mariadb-13.0-analyze-join-3t.json")))
        summary = generate_executive_summary(root, analyze_plan(root))
        self.assertIn("rows returned not measured", summary)
        self.assertNotIn("returns 1 row", summary)


def _capture(raw, engine="mysql", version="9.7.2", runs=None, variables=None, mode="analyze"):
    return json.dumps({
        "schema_version": "capture-1.0", "mode": mode, "raw": raw, "query": "select 1",
        "engine": engine, "server_version": version, "variables": variables or {},
        "runs": runs if runs is not None else [],
    })


class TestCaptureEngineAndVersion(unittest.TestCase):
    """The engine comes from the capture's server record, else from
    parse_explain; never from envelope sniffing."""

    def test_recorded_engine_wins(self):
        root = parse_explain(_text(_fixture("mysql-8.4-v1-intersect.json")))
        self.assertEqual(cli._plan_engine(root, {"engine": "mariadb"}), "mariadb")
        self.assertEqual(cli._plan_engine(root, {"engine": ""}), "mysql")
        self.assertEqual(cli._plan_engine(root, None), "mysql")

    def test_estimated_mysql_v1_capture_digest(self):
        raw = _text(_fixture("mysql-8.4-v1-intersect.json"))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cap.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write(_capture(raw, version="8.0.46", mode="explain"))
            proc = subprocess.run([sys.executable, "-m", "myflames", "digest", path],
                                  cwd=REPO_DIR, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("engine mysql 8.0.46 |", proc.stdout)
        self.assertIn("rows not measured", proc.stdout)
        self.assertNotIn("rows 0", proc.stdout)


GROUP_SORT = json.dumps({
    "operation": "Sort: `count(*)` DESC", "access_type": "sort", "actual_rows": 5.0,
    "actual_loops": 1, "actual_last_row_ms": 1.6, "sort_fields": ["`count(*)` DESC"], "inputs": [
        {"operation": "Group aggregate: count(0)", "access_type": "aggregate", "actual_rows": 5.0,
         "actual_loops": 1, "actual_last_row_ms": 1.5, "inputs": [
             {"operation": "Covering index scan on orders using idx_status", "table_name": "orders",
              "index_name": "idx_status", "access_type": "index", "index_access_type": "index_scan",
              "covering": True, "actual_rows": 12000.0, "actual_loops": 1,
              "actual_last_row_ms": 0.8, "estimated_rows": 11834.0, "inputs": []}]}]})


class TestSortSpillEvidence(unittest.TestCase):
    """A spill is claimed only from merge passes: MySQL and MariaDB count
    one per merge of on-disk sorted runs (filesort.cc merge_buffers) and
    none for a sort that fits in memory (filesort.cc: "The whole set is in
    memory"); MariaDB ANALYZE prints r_sort_passes only when nonzero
    (Filesort_tracker::print_json_members)."""

    VARS = {"sort_buffer_size": "262144"}

    def _advise(self, runs, plan=GROUP_SORT):
        from myflames.advisor import advise
        root = parse_explain(plan)
        analysis = analyze_plan(root)
        capture = {"mode": "analyze", "engine": "mysql", "runs": runs}
        advise(analysis, variables=self.VARS, capture=capture)
        return root, analysis

    def test_zero_merge_passes_means_no_spill_claim(self):
        root, a = self._advise([{"statistics": {"sort_merge_passes": 0}}])
        self.assertFalse(any("sort_buffer_size" in w for w in a["environment_warnings"]))
        summary = generate_executive_summary(root, a)
        self.assertNotIn("spill", summary)

    def test_measured_merge_passes_are_reported(self):
        root, a = self._advise([{"statistics": {"sort_merge_passes": 3}}])
        self.assertIn("Filesort merged sorted runs from disk: 3 sort merge pass(es) for this "
                      "query; sort_buffer_size is 256.0 KB.", a["environment_warnings"])
        kind, text = _pick_primary_issue(a)
        self.assertEqual(kind, "sort_spill")
        self.assertIn("merged sorted runs from disk (3 merge passes)", text)

    def test_session_status_delta_is_a_fallback(self):
        _, a = self._advise([{"statistics": None, "session_status_delta": {"Sort_merge_passes": 2}}])
        self.assertTrue(any(w.startswith("Filesort merged sorted runs from disk: 2")
                            for w in a["environment_warnings"]))

    def test_no_evidence_says_the_plan_cannot_tell(self):
        _, a = self._advise([])
        kind, text = _pick_primary_issue(a)
        self.assertEqual(kind, "sort_spill")
        self.assertNotIn("likely", text)
        self.assertIn("the plan does not show whether the sort spilled to disk", text)
        self.assertIn("256.0 KB", text)

    def _mariadb(self, **filesort):
        raw = load_explain_json(_text(_fixture("mariadb-10.6-analyze-join-group-repeated-table.json")))
        raw["query_block"]["filesort"].update(filesort)
        return analyze_plan(parse_explain(json.dumps(raw)))

    def test_mariadb_plan_merge_passes(self):
        """r_sort_passes is the total divided by r_loops, rounded, printed
        only when the total is nonzero (Filesort_tracker::print_json_members):
        the total is about r_sort_passes x r_loops."""
        a = self._mariadb(r_sort_passes=2)
        self.assertEqual(a["filesorts"][0]["merge_passes"], 2)
        self.assertIn("1 sort operation(s) — 11 rows; merged sorted runs from disk "
                      "(2 sort merge passes)", a["warnings"])
        self.assertIn(P.SORT_SUGGESTION, a["suggestions"])
        a = self._mariadb(r_sort_passes=2, r_loops=3)
        self.assertIn("1 sort operation(s) — 11 rows; merged sorted runs from disk "
                      "(about 6 sort merge passes)", a["warnings"])
        a = self._mariadb(r_sort_passes=0, r_loops=3)
        self.assertEqual(a["filesorts"][0]["merge_passes"], 1)
        a = self._mariadb()
        self.assertIn("1 sort operation(s) — 11 rows; sorted in memory (0 sort merge passes measured)",
                      a["warnings"])
        self.assertIn(P.SORT_SUGGESTION_IN_MEMORY, a["suggestions"])
        self.assertNotIn(P.SORT_SUGGESTION, a["suggestions"])

    def test_estimated_mariadb_sort_stays_hedged(self):
        a = analyze_plan(parse_explain(_text(_fixture("mariadb-11.4-explain-cte-recursive-days.json"))))
        self.assertIsNone(a["filesorts"][0]["merge_passes"])
        self.assertTrue(any("may use disk-based filesort" in w for w in a["warnings"]))


class TestAdviceDuplicatesAndDocs(unittest.TestCase):

    def test_temp_table_advice_appears_once(self):
        from myflames.advisor import advise
        from myflames.parser import TEMP_TABLE_SUGGESTION_MYSQL
        root = parse_explain(_text(os.path.join(TEST_DIR, "mysql-explain-complex-cte-window.json")))
        analysis = analyze_plan(root)
        self.assertTrue(analysis["temp_tables"])
        self.assertIn(TEMP_TABLE_SUGGESTION_MYSQL, analysis["suggestions"])
        advise(analysis, variables={"tmp_table_size": "16777216", "max_heap_table_size": "16777216",
                                    "internal_tmp_mem_storage_engine": "TempTable", "version": "9.7.2"})
        texts = analysis["suggestions"] + analysis["environment_suggestions"]
        self.assertNotIn(TEMP_TABLE_SUGGESTION_MYSQL, texts)
        self.assertEqual(sum(1 for t in texts if "tmp_table_size" in t), 1)

    def test_docs_follow_the_server_version(self):
        from myflames.findings import build_findings
        root = parse_explain(_text(os.path.join(TEST_DIR, "mysql-explain-hash-join.json")))
        base = "https://dev.mysql.com/doc/refman/{}/en/hash-joins.html"
        for engine, version, expected in (
                ("mysql", "9.7.2", base.format("9.7")),
                ("mysql", "26.7.0", base.format("26.7")),
                ("mysql", "8.0.46", base.format("8.0")),
                ("mysql", "9.3.0", "https://dev.mysql.com/doc/refman/en/hash-joins.html"),
                ("mysql", None, base.format("8.4")),
                ("mariadb", "11.4.8-MariaDB",
                 "https://mariadb.com/docs/server/ha-and-performance/optimization-and-tuning/"
                 "query-optimizer/block-based-join-algorithms")):
            with self.subTest(version=version, engine=engine):
                payload = build_sidecar(root, analyze_plan(root), engine=engine, engine_version=version)
                docs = [f["docs"] for f in build_findings(payload) if f.get("category") == "hash_join"]
                self.assertEqual(docs, [expected])

    def test_semijoin_page_name_per_manual(self):
        from myflames.findings import _docs_for
        w = {"category": "semijoin", "text": "x"}
        self.assertEqual(_docs_for(w, {"source": {"engine": "mysql", "engine_version": "8.0.46"}}),
                         "https://dev.mysql.com/doc/refman/8.0/en/semijoins.html")
        self.assertEqual(_docs_for(w, {"source": {"engine": "mysql", "engine_version": "9.7.2"}}),
                         "https://dev.mysql.com/doc/refman/9.7/en/semijoins-antijoins.html")

    def test_missing_index_warning_links_index_docs(self):
        from myflames.findings import _docs_for
        w = {"category": "env", "text": "Table orders has no index covering (total)."}
        self.assertEqual(_docs_for(w, {}), "https://dev.mysql.com/doc/refman/8.4/en/mysql-indexes.html")
        w = {"category": "env", "text": "Query materializes 1 temp table(s); tmp_table_size is 16.0 MB."}
        self.assertEqual(_docs_for(w, {}),
                         "https://dev.mysql.com/doc/refman/8.4/en/server-system-variables.html")

    def test_summary_has_one_final_period(self):
        root = parse_explain(_text(os.path.join(TEST_DIR, "mysql-explain-hash-join.json")))
        analysis = analyze_plan(root)
        analysis["environment_warnings"] = []
        with mock.patch("myflames.glossary._pick_primary_issue",
                        return_value=("redundant_index", "Redundant index on t: a is a prefix of b.")):
            summary = generate_executive_summary(root, analysis)
        self.assertTrue(summary.endswith("a is a prefix of b."), summary)
        self.assertNotIn("..", summary)



class TestReviewCorrections(unittest.TestCase):
    """Corrections from the source review of the measurement semantics."""

    def test_one_table_mariadb_root_rows_sent_after_condition(self):
        """W2: r_rows counts rows before the attached condition
        (evaluate_join_record, sql/sql_select.cc); rows sent are
        r_rows x r_filtered / 100 x r_loops."""
        plan = {"query_block": {"select_id": 1, "r_loops": 1, "r_total_time_ms": 1.0, "nested_loop": [
            {"table": {"table_name": "t", "access_type": "ALL", "r_loops": 1, "rows": 1000,
                       "r_rows": 1000, "r_filtered": 1.5, "r_table_time_ms": 0.5,
                       "r_other_time_ms": 0.1, "attached_condition": "t.x = 1"}}]}}
        root = parse_explain(json.dumps(plan))
        self.assertEqual(root["details"]["actual_rows"], 1000.0)
        self.assertEqual(root["details"]["rows_returned"], 15.0)
        ps = _compute_plan_summary(root)
        self.assertEqual(ps["rows_sent"], 15)
        self.assertEqual(ps["rows_examined_estimate"], 1000)

    def test_mariadb_10_8_note_in_hook(self):
        """W5: MDEV-27036 (commit 73df7a30094) is first in mariadb-10.8.1."""
        doc = P._join_members_hook.__doc__
        self.assertIn("10.8.1", doc)
        self.assertIn("10.7 and earlier", doc)
        self.assertNotIn("10.9", doc)

    def _estimated(self, nested_loop):
        root = parse_explain(json.dumps({"query_block": {"select_id": 1, "nested_loop": nested_loop}}))
        loops = P._estimated_loops(root)
        return root, {n["details"]["table_name"]: loops[n["node_id"]]
                      for n in flatten_nodes(root) if n["details"]["table_name"]}

    def test_estimated_loops_use_filtered_rows(self):
        """W6: MariaDB "rows" is rows examined before "filtered"
        (sql/sql_select.cc); the next table runs once per row that passed."""
        _, loops = self._estimated([
            {"table": {"table_name": "t1", "access_type": "ALL", "rows": 100000, "filtered": 1}},
            {"table": {"table_name": "t2", "access_type": "ALL", "rows": 50, "filtered": 100}}])
        self.assertEqual(loops["t2"], 1000.0)

    def test_estimated_loops_prefer_printed_loops(self):
        """W6: MariaDB 11.0.1+ prints the prefix count as "loops"
        (POSITION::loops = record_count; MDEV-30032)."""
        _, loops = self._estimated([
            {"table": {"table_name": "t1", "access_type": "ALL", "rows": 100000, "filtered": 1}},
            {"table": {"table_name": "t2", "access_type": "ALL", "rows": 50, "filtered": 100,
                       "loops": 777}}])
        self.assertEqual(loops["t2"], 777.0)

    def test_buffered_inner_table_is_not_read_per_outer_row(self):
        """W6: a table under a join buffer is read once per refill
        (sub_select_cache), not per outer row; EXPLAIN prints no refill
        count, so it counts as one read."""
        root, loops = self._estimated([
            {"table": {"table_name": "t1", "access_type": "ALL", "rows": 100000, "filtered": 100}},
            {"block-nl-join": {"table": {"table_name": "t2", "access_type": "ALL", "rows": 50,
                                         "filtered": 100, "loops": 100000},
                               "buffer_type": "flat", "join_type": "BNL"}}])
        self.assertEqual(loops["t2"], 1.0)
        scans = analyze_plan(root)["full_scans"]
        self.assertEqual([(s["table"], s["loops"]) for s in scans], [("t1", 1.0)])

    def test_mariadb_docs_or_no_link(self):
        """W7: a MariaDB plan links MariaDB pages, or nothing when no page
        fits (a MySQL page would describe different behavior)."""
        from myflames.findings import _docs_for
        maria = {"source": {"engine": "mariadb", "engine_version": "11.8.3-MariaDB"}}
        self.assertEqual(_docs_for({"category": "bnl", "text": "x"}, maria),
                         "https://mariadb.com/docs/server/ha-and-performance/optimization-and-tuning/"
                         "query-optimizer/block-based-join-algorithms")
        self.assertEqual(_docs_for({"category": "env", "text": "Table t has no index covering (a)."}, maria),
                         "https://mariadb.com/docs/server/ha-and-performance/optimization-and-tuning/"
                         "optimization-and-indexes/building-the-best-index-for-a-given-select")
        for category in ("temp_table", "nonsargable_join", "other"):
            with self.subTest(category=category):
                self.assertIsNone(_docs_for({"category": category, "text": "x"}, maria))
        root = parse_explain(_text(os.path.join(TEST_DIR, "mariadb-explain-block-nl-join.json")))
        from myflames.findings import build_findings
        docs = [f.get("docs") for f in build_findings(_sidecar(root)) if f["kind"] == "warning"]
        self.assertTrue(docs)
        self.assertFalse(any(d and "dev.mysql.com" in d for d in docs), docs)

    def test_sort_advice_wording(self):
        """W8: a bigger buffer can avoid the merge, never the sort."""
        a = analyze_plan(parse_explain(GROUP_SORT))
        self.assertIn(P.SORT_SUGGESTION, a["suggestions"])
        self.assertFalse(any(s.startswith("Increase sort_buffer_size") for s in a["suggestions"]))
        from myflames.output_sidecar import _classify_suggestion
        self.assertEqual(_classify_suggestion(P.SORT_SUGGESTION_IN_MEMORY)[1], "index")

    def test_measured_zero_passes_restate_the_plan(self):
        """W8: 0 captured sort merge passes: sorted in memory, no disk
        hedge, and only the index advice."""
        from myflames.advisor import advise
        a = analyze_plan(parse_explain(GROUP_SORT))
        advise(a, variables={"sort_buffer_size": "262144"},
               capture={"mode": "analyze", "runs": [{"statistics": {"sort_merge_passes": 0}}]})
        self.assertIn("1 sort operation(s) — 5 rows; sorted in memory (0 sort merge passes measured)",
                      a["warnings"])
        self.assertFalse(any("may use disk-based filesort" in w for w in a["warnings"]))
        self.assertIn(P.SORT_SUGGESTION_IN_MEMORY, a["suggestions"])
        self.assertNotIn(P.SORT_SUGGESTION, a["suggestions"])
        self.assertTrue(all("disk-based" not in h["message"] for h in a["node_highlights"]))

    def test_measured_passes_replace_plan_advice(self):
        from myflames.advisor import advise
        a = analyze_plan(parse_explain(GROUP_SORT))
        advise(a, variables={"sort_buffer_size": "262144"},
               capture={"mode": "analyze", "runs": [{"statistics": {"sort_merge_passes": 4}}]})
        self.assertIn("1 sort operation(s) — 5 rows; merged sorted runs from disk "
                      "(4 sort merge passes measured)", a["warnings"])
        self.assertNotIn(P.SORT_SUGGESTION, a["suggestions"])
        self.assertTrue(any(s.startswith("Test a larger sort_buffer_size") for s in a["environment_suggestions"]))

    def test_join_buffer_advice_per_engine(self):
        """MariaDB BNL/BNLH refill the join buffer in memory and read the
        inner table once per refill (join_matching_records,
        sql/sql_join_cache.cc); nothing spills."""
        text = _text(os.path.join(TEST_DIR, "mariadb-explain-block-nl-join.json"))
        raw = load_explain_json(text)
        raw["query_block"]["nested_loop"][1]["block-nl-join"]["join_type"] = "BNLH"
        a = analyze_plan(parse_explain(json.dumps(raw)))
        joined = " ".join(a["suggestions"])
        self.assertNotIn("spill", joined)
        self.assertIn("once per refill", joined)
        mysql = analyze_plan(parse_explain(_text(os.path.join(TEST_DIR, "mysql-explain-hash-join.json"))))
        self.assertIn("Increase join_buffer_size if hash joins are slow or spill to disk", mysql["suggestions"])
        from myflames.advisor import _rule_join_buffer_vs_hash_or_bnl
        w, sug = _rule_join_buffer_vs_hash_or_bnl({"hash_joins": [{"rows": 1}]}, None, None,
                                                  {"join_buffer_size": "262144", "version": "11.8.3-MariaDB"})
        self.assertNotIn("spill", w + sug)
        self.assertIn("once per join-buffer refill", w)

    def test_temp_table_limit_wording_per_engine(self):
        from myflames.advisor import _rule_tmp_table_size_vs_materialize
        tt = {"temp_tables": [{"rows": 1}]}
        w, _ = _rule_tmp_table_size_vs_materialize(tt, None, None, {
            "tmp_table_size": "16777216", "max_heap_table_size": "16777216", "version": "11.8.3-MariaDB"})
        self.assertIn("the smaller of tmp_table_size and max_heap_table_size is 16.0 MB", w)
        kind, text = _pick_primary_issue({"environment_warnings": [w]})
        self.assertIn("the smaller of tmp_table_size and max_heap_table_size is 16.0 MB", text)
        w, _ = _rule_tmp_table_size_vs_materialize(tt, None, None, {
            "tmp_table_size": "16777216", "max_heap_table_size": "1048576",
            "internal_tmp_mem_storage_engine": "TempTable", "version": "9.7.2"})
        self.assertIn("; tmp_table_size is 16.0 MB.", w)
        self.assertNotIn("max_heap_table_size is", w)

    def test_temp_table_warning_shows_what_the_plan_has(self):
        """F: no spill claim, and the size is the rows the input wrote (an
        upper bound), never a fraction of a lookup rounded to 0."""
        text = _text(os.path.join(TEST_DIR, "mariadb-11.8-semijoin.json"))
        a = analyze_plan(parse_explain(text))
        warning = [w for w in a["warnings"] if "temp table" in w][0]
        self.assertNotIn("may spill", warning)
        self.assertNotIn("up to 0 rows", warning)
        self.assertIn("the plan does not show whether they spilled to disk", warning)
        root = parse_explain(text)
        mat = [n for n in flatten_nodes(root) if n["full_label"] == "Materialize"][0]
        d = mat["children"][0]["details"]
        self.assertIn("up to {} rows written".format(int(round(d["rows_returned"]))), warning)

    def test_never_executed_mysql_operator_reads_not_executed(self):
        from myflames.output_bargraph import _build_bar_info
        from myflames.output_tree import _row_info
        root = parse_explain(_text(_fixture("explain-046-join-5t-full-chain.json")))
        never = [n for n in flatten_nodes(root) if n["details"]["actual_loops"] is None]
        self.assertTrue(never)
        self.assertIn("Rows: not executed", _row_info(never[0], root["total_time"], "ms"))
        self.assertIn("Rows: not executed", _build_bar_info(never[0], "0", 0.0, "ms"))
        estimated = parse_explain(ESTIMATED_SCAN)
        self.assertIn("Rows: not measured", _row_info(estimated, 0, "ms"))


class TestReviewGateRegressions(unittest.TestCase):
    """Second Codex review gate, 2026-10-19."""

    def test_estimated_capture_proves_nothing_about_sorts(self):
        from myflames.advisor import _captured_sort_merge_passes
        runs = [{"statistics": {"sort_merge_passes": 0}, "session_status_delta": {"Sort_merge_passes": 0}}]
        # EXPLAIN never runs the statement: its counters are not the query's.
        self.assertIsNone(_captured_sort_merge_passes({"capture": {"mode": "explain", "runs": runs}}))
        self.assertIsNone(_captured_sort_merge_passes({"capture": {"runs": runs}}))
        self.assertEqual(_captured_sort_merge_passes({"capture": {"mode": "analyze", "runs": runs}}), 0)

    def test_multi_input_materialization_has_no_size_bound(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mysql-explain-complex-recursive-cte.json")
        with open(path) as f:
            analysis = analyze_plan(parse_explain(f.read()))
        # The anchor and the recursive part both write into the CTE's table,
        # so the first input's rows (100) are no upper bound for the 1,000 rows.
        self.assertEqual([t["rows"] for t in analysis["temp_tables"]], [None])
        warning = [w for w in analysis["warnings"] if "temp table" in w]
        self.assertTrue(warning)
        self.assertNotIn("up to", warning[0])


if __name__ == "__main__":
    unittest.main()
