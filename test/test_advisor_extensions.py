"""Row-estimate, statistics freshness, redundant-index, and cold-cache rules,
plus the rule catalog and the ``--ignore`` / ``--list-rules`` CLI controls."""
import json
import os
import subprocess
import sys
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_DIR)

from myflames.advisor import advise, RULE_CATEGORIES  # noqa: E402
from myflames.collectors import collect_stats_freshness, parse_show_create_table  # noqa: E402
from myflames.connector import ConnectorError  # noqa: E402
from myflames.findings import (  # noqa: E402
    RULES, apply_ignore, build_findings, rule_catalog, summarize, unknown_ignores,
)
from myflames.live import normalize_capture  # noqa: E402
from myflames.output_sidecar import (  # noqa: E402
    _WARNING_CATEGORIES, build_sidecar, validate_sidecar,
)
from myflames.parser import analyze_plan, parse_explain  # noqa: E402

BKA = os.path.join(TEST_DIR, "mysql-explain-bka-mrr.json")
LIMIT_SCAN = os.path.join(TEST_DIR, "fixtures", "explain-063-limit-no-order-users.json")
HASH_JOIN = os.path.join(TEST_DIR, "mysql-explain-hash-join.json")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _node(operation, access_type, table="", est=None, act=None, loops=1, children=None,
          measured=True):
    return {
        "short_label": operation,
        "children": children or [],
        "details": {
            "operation": operation, "access_type": access_type, "table_name": table,
            "estimated_rows": est, "actual_rows": act, "actual_loops": loops,
            "rows_measured": measured,
        },
    }


def _misestimates(root, engine="mysql"):
    from myflames.parser import _collect_misestimates
    root["engine"] = engine
    return _collect_misestimates(root)


DDL = """CREATE TABLE `orders` (
  `id` int NOT NULL,
  `user_id` int NOT NULL,
  `status` varchar(20) NOT NULL,
  `note` varchar(200) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `u_user_status` (`user_id`,`status`),
  KEY `k_user` (`user_id`),
  KEY `k_user_dup` (`user_id`),
  KEY `k_status_desc` (`status` DESC),
  KEY `k_status_created` (`status`,`id`),
  KEY `k_note10` (`note`(10)),
  KEY `k_note_status` (`note`,`status`),
  KEY `k_expr` ((lower(`status`))),
  FULLTEXT KEY `ft_note` (`note`)
) ENGINE=InnoDB"""


class TestShowCreateTableParts(unittest.TestCase):
    def setUp(self):
        self.indexes = {i["name"] or "PRIMARY": i for i in parse_show_create_table(DDL)["indexes"]}

    def test_prefix_length_keeps_following_columns(self):
        ddl = "CREATE TABLE `t` (\n  `a` text,\n  `b` int,\n  KEY `ab` (`a`(10),`b`)\n) ENGINE=InnoDB"
        index = parse_show_create_table(ddl)["indexes"][0]
        self.assertEqual(index["columns"], ["a", "b"])
        self.assertEqual(index["parts"][0]["length"], 10)

    def test_direction_expression_and_kinds(self):
        self.assertTrue(self.indexes["k_status_desc"]["parts"][0]["desc"])
        self.assertTrue(self.indexes["k_expr"]["parts"][0]["expression"])
        self.assertEqual(self.indexes["ft_note"]["kind"], "FULLTEXT KEY")
        self.assertEqual(self.indexes["PRIMARY"]["columns"], ["id"])

    def test_using_hash_is_recorded(self):
        ddl = "CREATE TABLE `t` (\n  `a` int,\n  KEY `h` (`a`) USING HASH\n) ENGINE=MEMORY"
        self.assertEqual(parse_show_create_table(ddl)["indexes"][0]["using"], "HASH")


class TestRowMisestimate(unittest.TestCase):
    def test_underestimate_on_base_table_is_flagged(self):
        root = _node("Table scan on t", "table", "t", est=50, act=12000)
        found = _misestimates(root)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["under"])
        self.assertGreaterEqual(found[0]["ratio"], 10)

    def test_overestimate_under_limit_is_expected(self):
        scan = _node("Table scan on users", "table", "users", est=3000, act=50)
        self.assertEqual(_misestimates(_node("Limit: 50 row(s)", "limit", children=[scan])), [])

    def test_underestimate_under_limit_is_still_flagged(self):
        scan = _node("Table scan on users", "table", "users", est=50, act=3000)
        self.assertEqual(len(_misestimates(_node("Limit: 50 row(s)", "limit", children=[scan]))), 1)

    def test_semijoin_inner_side_stops_early(self):
        outer = _node("Table scan on p", "table", "p", est=100, act=100)
        inner = _node("Index lookup on r", "index", "r", est=5000, act=1, loops=100)
        join = _node("Nested loop semijoin", "join", children=[outer, inner])
        self.assertEqual(_misestimates(join), [])

    def test_small_or_unmeasured_or_materialized_nodes_are_ignored(self):
        self.assertEqual(_misestimates(_node("Table scan on t", "table", "t", est=1, act=50)), [])
        self.assertEqual(_misestimates(_node("Table scan on t", "table", "t", est=50, act=12000,
                                             measured=False)), [])
        derived = _node("Table scan on <derived2>", "table", "<derived2>", est=5, act=9000)
        self.assertEqual(_misestimates(derived), [])
        never = _node("Table scan on t", "table", "t", est=50, act=12000, loops=0)
        self.assertEqual(_misestimates(never), [])

    def test_filter_reported_only_when_access_is_accurate(self):
        accurate = _node("Table scan on o", "table", "o", est=20000, act=20000)
        flt = _node("Filter: (o.total > 490)", "filter", est=50, act=9000, children=[accurate])
        found = _misestimates(flt)
        self.assertEqual([(m["table"], m["kind"]) for m in found], [("o", "filter")])
        wrong = _node("Table scan on o", "table", "o", est=200, act=20000)
        flt = _node("Filter: (o.total > 490)", "filter", est=50, act=9000, children=[wrong])
        self.assertEqual([m["kind"] for m in _misestimates(flt)], ["access"])

    def test_mariadb_compares_index_lookups_only(self):
        scan = _node("Table scan on o", "table", "o", est=50, act=12000)
        self.assertEqual(_misestimates(scan, "mariadb"), [])
        lookup = _node("Index lookup on oi", "ref", "oi", est=1, act=40, loops=100)
        self.assertEqual(len(_misestimates(lookup, "mariadb")), 1)
        # MariaDB plans hide LIMIT, which can cut actual rows: no overestimates.
        over = _node("Index lookup on oi", "ref", "oi", est=10000, act=10)
        self.assertEqual(_misestimates(over, "mariadb"), [])

    def test_mysql_v1_union_is_labeled_mysql(self):
        spec = {"query_block": {"select_id": 1, "table": {
            "table_name": "t", "access_type": "ALL", "rows_examined_per_scan": 5}}}
        plan = {"query_block": {"union_result": {"query_specifications": [spec, spec]}}}
        self.assertEqual(parse_explain(json.dumps(plan))["engine"], "mysql")

    def test_memory_tables_are_skipped_by_redundant_index_rule(self):
        ddl = "CREATE TABLE `m` (\n  `a` int,\n  `b` int,\n  KEY `k_a` (`a`),\n  KEY `k_ab` (`a`,`b`)\n) ENGINE=MEMORY"
        analysis = {}
        advise(analysis, schema={"db.m": parse_show_create_table(ddl)})
        self.assertFalse([f for f in analysis["environment_findings"] if f["rule_id"] == "REDUNDANT_INDEX"])

    def test_parse_explain_tags_engine(self):
        self.assertEqual(parse_explain(_read(HASH_JOIN))["engine"], "mysql")
        maria = os.path.join(TEST_DIR, "fixtures", "mariadb-11.4-012-sort-with-filter.json")
        self.assertEqual(parse_explain(_read(maria))["engine"], "mariadb")

    def test_plan_warning_suggestion_and_sidecar_categories(self):
        root = parse_explain(_read(BKA))
        analysis = analyze_plan(root)
        self.assertTrue(analysis["misestimates"])
        payload = build_sidecar(root, analysis, engine="mysql")
        validate_sidecar(payload)
        warning = [w for w in payload["warnings"] if w["category"] == "misestimate"]
        self.assertEqual(len(warning), 1)
        self.assertIn("250× under", warning[0]["text"])
        self.assertTrue(warning[0].get("node_labels"))
        statistics = [s for s in payload["suggestions"] if s["category"] == "statistics"]
        self.assertEqual(len(statistics), 1)
        self.assertIn("ANALYZE TABLE t1", statistics[0]["action"])
        self.assertIn("PERSISTENT FOR COLUMNS", statistics[0]["action"])

    def test_limit_fixture_has_no_misestimate(self):
        analysis = analyze_plan(parse_explain(_read(LIMIT_SCAN)))
        self.assertEqual(analysis["misestimates"], [])


class TestRedundantIndexes(unittest.TestCase):
    def setUp(self):
        self.schema = {"shop.orders": parse_show_create_table(DDL)}
        analysis = {"index_suggestions": []}
        advise(analysis, schema=self.schema)
        self.findings = [f for f in analysis["environment_findings"]
                         if f["rule_id"] == "REDUNDANT_INDEX"]
        self.text = "\n".join(f["warning"] for f in self.findings)

    def test_prefix_and_duplicates_are_flagged(self):
        self.assertIn("k_user (user_id) is a leftmost prefix of u_user_status", self.text)
        self.assertIn("k_user_dup (user_id) duplicates k_user (user_id)", self.text)

    def test_constraints_lengths_directions_and_special_kinds_are_kept(self):
        for name in ("PRIMARY", "u_user_status", "k_status_desc", "k_note10", "k_expr", "ft_note"):
            self.assertNotIn("index on shop.orders: " + name + " ", self.text)
        self.assertIn("ALTER TABLE `shop`.`orders` DROP INDEX `k_user`.", self.findings[0]["suggestion"])
        self.assertIn("INVISIBLE", self.findings[0]["suggestion"])
        self.assertIn("IGNORED", self.findings[0]["suggestion"])

    def test_suggested_index_makes_existing_prefix_redundant(self):
        ddl = "CREATE TABLE `t` (\n  `a` int,\n  `b` int,\n  KEY `k_a` (`a`)\n) ENGINE=InnoDB"
        analysis = {"index_suggestions": [{"table": "t", "columns": ["a", "b"]}]}
        advise(analysis, schema={"db.t": parse_show_create_table(ddl)})
        found = [f for f in analysis["environment_findings"] if f["rule_id"] == "REDUNDANT_INDEX"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["warning"], "")
        self.assertIn("drop k_a (a)", found[0]["suggestion"])


class TestStaleStatistics(unittest.TestCase):
    def test_freshness_is_reported_for_misestimated_tables(self):
        analysis = {"misestimates": [{"table": "t1"}, {"table": "other"}]}
        freshness = {"db.t1": {"last_update": "2026-01-02 03:04:05", "n_rows": 1000,
                               "histograms": []}}
        advise(analysis, freshness=freshness)
        found = [f for f in analysis["environment_findings"] if f["rule_id"] == "STALE_STATS"]
        self.assertEqual(len(found), 1)
        self.assertIn("last updated 2026-01-02 03:04:05 (n_rows 1,000)", found[0]["warning"])
        self.assertIn("no column histograms", found[0]["warning"])

    def test_no_freshness_no_finding(self):
        analysis = {"misestimates": [{"table": "t1"}]}
        advise(analysis)
        self.assertFalse([f for f in analysis["environment_findings"]
                          if f["rule_id"] == "STALE_STATS"])


def _capture(reads, times, mode="analyze"):
    return {"mode": mode, "runs": [
        {"total_time_ms": ms, "session_status_delta": {"Innodb_buffer_pool_reads": r}}
        for r, ms in zip(reads, times)]}


class TestColdCache(unittest.TestCase):
    def _found(self, capture):
        analysis = {}
        advise(analysis, capture=capture)
        return [f for f in analysis["environment_findings"] if f["rule_id"] == "COLD_CACHE"]

    def test_cold_first_run_is_reported(self):
        found = self._found(_capture([2400, 3, 1], [900.0, 120.0, 110.0]))
        self.assertEqual(len(found), 1)
        self.assertIn("Run 1 read 2,400 pages", found[0]["warning"])
        self.assertIn("consistent with a cold buffer pool", found[0]["warning"])

    def test_not_reported_without_contrast(self):
        self.assertEqual(self._found(_capture([2400, 2000], [900.0, 880.0])), [])
        self.assertEqual(self._found(_capture([50, 0], [900.0, 100.0])), [])
        self.assertEqual(self._found(_capture([2400, 0], [130.0, 120.0])), [])
        self.assertEqual(self._found(_capture([2400], [900.0])), [])
        self.assertEqual(self._found(_capture([2400, 0], [900.0, 100.0], mode="explain")), [])
        self.assertEqual(self._found({"mode": "analyze", "runs": [
            {"total_time_ms": 900.0, "session_status_delta": {}},
            {"total_time_ms": 100.0, "session_status_delta": {}}]}), [])


class TestSidecarEnvironmentCategories(unittest.TestCase):
    def test_new_rules_keep_their_category_and_others_stay_env(self):
        root = parse_explain(_read(HASH_JOIN))
        analysis = analyze_plan(root)
        ddl = "CREATE TABLE `t` (\n  `a` int,\n  `b` int,\n  KEY `k_a` (`a`),\n  KEY `k_ab` (`a`,`b`)\n) ENGINE=MyISAM"
        advise(analysis, schema={"db.t": parse_show_create_table(ddl)},
               capture=_capture([2400, 0], [900.0, 100.0]))
        payload = build_sidecar(root, analysis, engine="mysql")
        validate_sidecar(payload)
        env = {w["category"]: w for w in payload["warnings"] if w["source"] == "environment"}
        self.assertIn("redundant_index", env)
        self.assertEqual(env["cold_cache"]["severity"], "info")
        self.assertIn("env", env)  # the MyISAM engine rule keeps its old category
        cats = {s["category"] for s in payload["suggestions"] if s["source"] == "environment"}
        self.assertIn("index", cats)

    def test_rule_category_map_uses_known_categories(self):
        for category, severity, suggestion in RULE_CATEGORIES.values():
            self.assertIn(category, _WARNING_CATEGORIES)


class FakeConn:
    def __init__(self, responses):
        self.responses = responses
        self.sql = []

    def query_rows(self, sql):
        self.sql.append(sql)
        for needle, rows in self.responses:
            if needle in sql:
                if isinstance(rows, Exception):
                    raise rows
                return rows
        return []


class TestStatsFreshnessCollector(unittest.TestCase):
    def test_mysql_collects_last_update_and_histograms(self):
        conn = FakeConn([
            ("innodb_table_stats", [["shop", "orders", "2026-01-02 03:04:05", "1000"]]),
            ("COLUMN_STATISTICS", [["shop", "orders", "status"]]),
        ])
        out = collect_stats_freshness(conn, ["shop.orders", "users"])
        self.assertEqual(out["shop.orders"]["n_rows"], 1000)
        self.assertEqual(out["shop.orders"]["histograms"], ["status"])
        self.assertIn("database_name = DATABASE() AND table_name = 'users'", conn.sql[0])

    def test_mariadb_uses_column_stats_and_quotes_names(self):
        conn = FakeConn([("column_stats", [["shop", "o'rders", "status"]])])
        out = collect_stats_freshness(conn, ["shop.o'rders"], mariadb=True)
        self.assertEqual(out["shop.o'rders"]["histograms"], ["status"])
        self.assertIn("hist_size > 0", conn.sql[1])
        self.assertIn("'o''rders'", conn.sql[1])

    def test_privilege_errors_degrade_to_empty(self):
        conn = FakeConn([("mysql.", ConnectorError("denied")), ("information_schema", ConnectorError("denied"))])
        self.assertEqual(collect_stats_freshness(conn, ["shop.orders"]), {})
        self.assertEqual(collect_stats_freshness(conn, []), {})

    def test_normalize_capture_keeps_freshness(self):
        capture = normalize_capture({"mode": "explain", "stats_freshness": {"db.t": {"n_rows": 1}}})
        self.assertEqual(capture["stats_freshness"], {"db.t": {"n_rows": 1}})


class TestRuleCatalog(unittest.TestCase):
    def test_every_warning_category_is_documented_once(self):
        cats = [r[0] for r in RULES]
        self.assertEqual(sorted(cats), sorted(set(cats)))
        self.assertEqual(set(cats), set(_WARNING_CATEGORIES))
        for entry in rule_catalog():
            self.assertTrue(entry["description"])
            self.assertTrue(entry["docs"] is None or entry["docs"].startswith("https://"))

    def test_findings_carry_docs_and_ignore_filters(self):
        root = parse_explain(_read(HASH_JOIN))
        payload = build_sidecar(root, analyze_plan(root), engine="mysql")
        findings = build_findings(payload)
        warnings = [f for f in findings if f["kind"] == "warning"]
        self.assertTrue(all(f["docs"].startswith("https://") for f in warnings))
        trimmed = apply_ignore(payload, ["hash_join", "tuning_variable"])
        self.assertNotIn("hash_join", {w["category"] for w in trimmed["warnings"]})
        self.assertNotIn("tuning_variable", {s["category"] for s in trimmed["suggestions"]})
        self.assertIn("hash_join", {w["category"] for w in payload["warnings"]})
        self.assertEqual(unknown_ignores(["hash_join", "fullscan", " "]), ["fullscan"])
        counts = summarize(findings)
        self.assertEqual(sum(counts["warnings"].values()), len(warnings))


class TestRuleFlagsCli(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run([sys.executable, "-m", "myflames"] + list(args),
                              cwd=REPO_DIR, capture_output=True, text=True)

    def test_list_rules_needs_no_input(self):
        for command in ("check", "advise"):
            r = self._run(command, "--list-rules")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("misestimate", r.stdout)
            self.assertIn("https://dev.mysql.com/doc/refman/8.4/en/hash-joins.html", r.stdout)

    def test_missing_input_and_unknown_ignore_exit_2(self):
        self.assertEqual(self._run("check").returncode, 2)
        r = self._run("check", HASH_JOIN, "--ignore", "fullscan")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown --ignore category: fullscan", r.stderr)

    def test_ignore_turns_a_failing_gate_green(self):
        self.assertEqual(self._run("check", HASH_JOIN, "--fail-on", "hash_join").returncode, 1)
        r = self._run("check", HASH_JOIN, "--fail-on", "hash_join", "--ignore", "hash_join")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_advise_summary_and_json_docs(self):
        r = self._run("advise", HASH_JOIN)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Docs: https://", r.stdout)
        self.assertTrue(r.stdout.strip().splitlines()[-1].startswith("Summary: "))
        data = json.loads(self._run("advise", HASH_JOIN, "--json", "--ignore", "full_scan").stdout)
        self.assertNotIn("full_scan", {f["category"] for f in data})
        self.assertTrue(any(f.get("docs") for f in data))


class TestMySQLFormatVersion1(unittest.TestCase):
    """explain_json_format_version=1 plans (EXPLAIN FOR CONNECTION, MySQL Shell
    bundles before 9.5) parse as estimated MySQL plans."""

    TABLE = {"table_name": "users", "access_type": "ALL", "rows_examined_per_scan": 1000,
             "filtered": "100.00", "cost_info": {"read_cost": "1.25"}}

    def _parse(self, body):
        block = dict({"select_id": 1, "cost_info": {"query_cost": "101.25"}}, **body)
        return parse_explain(json.dumps({"query_block": block}))

    def test_single_table(self):
        root = self._parse({"table": self.TABLE})
        self.assertEqual(root["engine"], "mysql")
        self.assertEqual(root["details"]["estimated_rows"], 1000.0)
        self.assertFalse(root["details"]["rows_measured"])

    def test_ordering_and_grouping_wrappers(self):
        root = self._parse({"ordering_operation": {"using_filesort": True, "table": self.TABLE}})
        self.assertEqual(root["details"]["operation"], "Sort")
        self.assertEqual(root["children"][0]["details"]["table_name"], "users")
        root = self._parse({"grouping_operation": {"using_filesort": False, "table": self.TABLE}})
        self.assertEqual(root["children"][0]["details"]["table_name"], "users")

    def test_estimated_plan_never_reports_misestimates(self):
        root = self._parse({"table": self.TABLE})
        self.assertEqual(analyze_plan(root)["misestimates"], [])


class TestWorkspaceAnalyzeContext(unittest.TestCase):
    def test_analyze_passes_runs_and_freshness_to_the_advisor(self):
        from myflames.ui import api_response
        raw = _read(BKA)
        capture = {"mode": "analyze", "raw": raw, "runs": [
            {"total_time_ms": 900.0, "session_status_delta": {"Innodb_buffer_pool_reads": 2400}, "plan": ""},
            {"total_time_ms": 100.0, "session_status_delta": {"Innodb_buffer_pool_reads": 0}, "plan": ""}],
            "stats_freshness": {"db.t1": {"last_update": "2026-01-02 03:04:05", "n_rows": 10,
                                          "histograms": []}}}
        result = api_response("/api/analyze", {"plan": raw, "capture": capture})
        categories = {w["category"] for w in result["analysis"]["warnings"]}
        self.assertIn("cold_cache", categories)
        texts = [w["text"] for w in result["analysis"]["warnings"] if w["category"] == "misestimate"]
        self.assertTrue(any("Statistics for misestimated table db.t1" in text for text in texts))


class TestReviewRegressions(unittest.TestCase):
    def test_collector_sql_escapes_backslashes_and_quotes(self):
        conn = FakeConn([])
        collect_stats_freshness(conn, ["x\\", ") OR 1=1; DROP TABLE victim; #"])
        sql = conn.sql[0]
        self.assertIn("table_name = 'x\\\\')", sql)
        self.assertNotIn("'x\\')", sql)
        from myflames.collectors import collect_stats

        class DictConn(FakeConn):
            def query_dicts(self, sql, columns):
                self.sql.append(sql)
                return []
        conn = DictConn([])
        collect_stats(conn, ["s\\.t'x"])
        self.assertIn("table_name = 't''x' AND table_schema = 's\\\\'", conn.sql[0])

    def test_freshness_shapes_are_validated(self):
        for bad in ({"db.t": 5}, {"db.t": {"n_rows": "12"}}, {"db.t": {"histograms": "a"}},
                    {"db.t": {"last_update": 3}}):
            with self.assertRaises(ValueError):
                normalize_capture({"mode": "explain", "stats_freshness": bad})
        analysis = {"misestimates": [{"table": "t"}]}
        advise(analysis, freshness={"db.t": {"n_rows": "12", "histograms": "abc"}})
        found = [f for f in analysis["environment_findings"] if f["rule_id"] == "STALE_STATS"]
        self.assertIn("no column histograms", found[0]["warning"])

    def test_list_rules_json(self):
        r = subprocess.run([sys.executable, "-m", "myflames", "advise", "--list-rules", "--json"],
                           cwd=REPO_DIR, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual({e["category"] for e in json.loads(r.stdout)}, set(_WARNING_CATEGORIES))

    def test_subqueries_are_preserved(self):
        from myflames.parser import flatten_nodes
        for name, tables in (("mariadb-10.11-030-complex-correlated-subquery.json", {"p", "r"}),
                             ("mariadb-10.11-021-subquery-not-in.json", {"orders", "users"})):
            root = parse_explain(_read(os.path.join(TEST_DIR, "fixtures", name)))
            got = {n["details"]["table_name"] for n in flatten_nodes(root)} - {""}
            self.assertEqual(got, tables, name)
        attached = {"query_block": {"select_id": 1, "cost_info": {"query_cost": "1"}, "table": {
            "table_name": "reviews", "access_type": "ALL", "rows_examined_per_scan": 100,
            "attached_subqueries": [{"dependent": True, "query_block": {
                "select_id": 2, "cost_info": {"query_cost": "1"}, "table": {
                    "table_name": "products", "access_type": "eq_ref",
                    "rows_examined_per_scan": 1}}}]}}}
        root = parse_explain(json.dumps(attached))
        self.assertEqual([(n["details"]["table_name"], n["details"].get("input_kind"))
                          for n in flatten_nodes(root)],
                         [("reviews", None), ("products", "subquery")])
        derived = {"query_block": {"select_id": 1, "cost_info": {"query_cost": "1"}, "table": {
            "table_name": "d", "access_type": "ALL", "rows_examined_per_scan": 5,
            "materialized_from_subquery": {"query_block": {
                "select_id": 2, "cost_info": {"query_cost": "1"}, "table": {
                    "table_name": "orders", "access_type": "ALL",
                    "rows_examined_per_scan": 50}}}}}}
        names = [n["details"]["table_name"] for n in flatten_nodes(parse_explain(json.dumps(derived)))]
        self.assertEqual(names, ["d", "orders"])

    def test_scan_with_attached_subquery_is_still_a_base_scan(self):
        scan = _node("Table scan on t", "table", "t", est=50, act=12000)
        sub = _node("Index lookup on u", "ref", "u", est=1, act=1, loops=12000)
        sub["details"]["input_kind"] = "subquery"
        scan["children"] = [sub]
        self.assertEqual([m["table"] for m in _misestimates(scan)], ["t"])


class TestColdCacheReadAhead(unittest.TestCase):
    def _found(self, engine):
        runs = [{"total_time_ms": 900.0, "session_status_delta": {
                    "Innodb_buffer_pool_reads": 60, "Innodb_buffer_pool_read_ahead": 4000}},
                {"total_time_ms": 100.0, "session_status_delta": {
                    "Innodb_buffer_pool_reads": 0, "Innodb_buffer_pool_read_ahead": 0}}]
        analysis = {}
        advise(analysis, capture={"mode": "analyze", "engine": engine, "runs": runs})
        return [f for f in analysis["environment_findings"] if f["rule_id"] == "COLD_CACHE"]

    def test_mysql_counts_read_ahead_pages(self):
        found = self._found("mysql")
        self.assertEqual(len(found), 1)
        self.assertIn("Run 1 read 4,060 pages", found[0]["warning"])

    def test_mariadb_does_not_double_count(self):
        self.assertEqual(self._found("mariadb"), [])


class TestCaptureFileInput(unittest.TestCase):
    """check / advise / digest read capture-1.0 files (from `myflames capture`
    or the workspace) and reject JSON that is not a plan."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        raw = _read(BKA)
        capture = {"schema_version": "capture-1.0", "mode": "analyze", "engine": "mysql",
                   "raw": raw, "query": "SELECT 1", "runs": [
                       {"total_time_ms": 900.0, "plan": "", "session_status_delta": {"Innodb_buffer_pool_reads": 2400}},
                       {"total_time_ms": 100.0, "plan": "", "session_status_delta": {"Innodb_buffer_pool_reads": 0}}]}
        self.capture = os.path.join(self.tmp, "capture.json")
        with open(self.capture, "w") as f:
            json.dump(capture, f)
        self.bogus = os.path.join(self.tmp, "bogus.json")
        with open(self.bogus, "w") as f:
            f.write('{"foo": 1}')

    def _run(self, *args):
        return subprocess.run([sys.executable, "-m", "myflames"] + list(args),
                              cwd=REPO_DIR, capture_output=True, text=True)

    def test_capture_file_is_analyzed_with_its_evidence(self):
        data = json.loads(self._run("advise", self.capture, "--json").stdout)
        categories = {f["category"] for f in data}
        self.assertIn("misestimate", categories)
        self.assertIn("cold_cache", categories)
        self.assertEqual(self._run("check", self.capture, "--fail-on", "misestimate", "-q").returncode, 1)

    def test_non_plan_json_is_rejected(self):
        for command in ("check", "advise", "digest"):
            r = self._run(command, self.bogus)
            self.assertEqual(r.returncode, 2, (command, r.stdout, r.stderr))
            self.assertIn("not an EXPLAIN", r.stderr)


if __name__ == "__main__":
    unittest.main()
