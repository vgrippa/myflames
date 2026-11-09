"""Tests for importing MySQL Shell slow-query diagnostics bundles.

The synthetic archives below mirror what MySQL Shell's
``util.debug.collectSlowQueryDiagnostics()`` writes (see the layout notes in
``myflames/shell_import.py``): every member under one ``<bundle>/`` prefix,
TSV members produced by ``dump_query`` (comment header, column line, raw
multi-line values, optional ``# Warnings`` block) and YAML members produced
by PyYAML ``yaml.dump`` with Shell's literal-block override. The YAML text is
verbatim PyYAML 6 output for the same data.

Run:
  python3 -m unittest discover -s test -p "test_shell_import.py" -v
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(TEST_DIR)
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

from myflames import shell_import  # noqa: E402
from myflames.shell_import import (  # noqa: E402
    is_shell_diagnostics_zip, list_shell_diagnostics_queries, load_shell_diagnostics,
    _yaml_load,
)
from myflames.parser import parse_explain  # noqa: E402
from myflames.live import normalize_capture  # noqa: E402

ANALYZE_FIXTURE = os.path.join(TEST_DIR, "fixtures", "explain-015-index-range-scan-reviews-rating.json")
QUERY = "SELECT * FROM reviews WHERE rating >= 4"
JOIN_QUERY = ("SELECT r.id, u.name\nFROM reviews r JOIN users u ON u.id = r.user_id\n"
              "WHERE r.rating >= 4")

# EXPLAIN FORMAT=JSON as MySQL 8.0-9.4 prints it by default
# (explain_json_format_version=1, a query_block tree).
ESTIMATED_V1 = json.dumps({
    "query_block": {
        "select_id": 1,
        "cost_info": {"query_cost": "2163.13"},
        "nested_loop": [
            {"table": {
                "table_name": "r", "access_type": "ALL", "possible_keys": ["idx_rating"],
                "rows_examined_per_scan": 9921, "rows_produced_per_join": 3306,
                "filtered": "33.33",
                "cost_info": {"read_cost": "674.43", "eval_cost": "330.67",
                              "prefix_cost": "1005.10", "data_read_per_join": "2M"},
                "used_columns": ["id", "user_id", "rating"],
                "attached_condition": "((`testdb`.`r`.`rating` >= 4) and "
                                      "(`testdb`.`r`.`user_id` is not null))"}},
            {"table": {
                "table_name": "u", "access_type": "eq_ref", "possible_keys": ["PRIMARY"],
                "key": "PRIMARY", "used_key_parts": ["id"], "key_length": "4",
                "ref": ["testdb.r.user_id"], "rows_examined_per_scan": 1,
                "rows_produced_per_join": 3306, "filtered": "100.00",
                "cost_info": {"read_cost": "827.36", "eval_cost": "330.67",
                              "prefix_cost": "2163.13", "data_read_per_join": "1M"},
                "used_columns": ["id", "name"]}},
        ],
    }
}, indent=2)

TREE_TEXT = (
    "-> Nested loop inner join  (cost=2163 rows=3306) (actual time=0.0721..6.41 rows=4000 loops=1)\n"
    "    -> Filter: ((r.rating >= 4) and (r.user_id is not null))  (cost=1005 rows=3306) "
    "(actual time=0.0563..3.12 rows=4000 loops=1)\n"
    "        -> Table scan on r  (cost=1005 rows=9921) (actual time=0.0541..2.4 rows=10000 loops=1)\n"
    "    -> Single-row index lookup on u using PRIMARY (id=r.user_id)  (cost=0.25 rows=1) "
    "(actual time=699e-6..717e-6 rows=1 loops=4000)"
)

TRACE = {"steps": [
    {"join_preparation": {"select#": 1, "steps": [{"expanded_query": "/* select#1 */ select `reviews`.`id` AS `id`"}]}},
    {"join_optimization": {"select#": 1, "steps": [{"rows_estimation": [
        {"table": "`reviews`", "range_analysis": {"table_scan": {"rows": 9921, "cost": 1005.1}}}]}]}},
]}

INSTANCE = (
    "Hostname: db1 (report_host=None)\nPort: 3306 (report_port=3306)\n"
    "Server UUID: 3e11fa47-71ca-11e1-9e33-c80aa9429562\nServer ID: 1\n"
    "Connection Endpoint: root@localhost:3306\n"
    "Version: MySQL Community Server - GPL 8.4.2\n"
    "UTC Time: 2024-05-01 12:00:00\nLocal Time: 2024-05-01 14:00:00\n"
    "Time Zone: SYSTEM\nSystem Time Zone: CEST\nTime Zone Offset: 02:00:00\n"
    "SYS Schema Version: 8.4.2\nPFS Enabled: True\nHas InnoDB Cluster: False\n"
    "Has NDB: False\nHas Rapid: False\n"
)

SHELL_INFO_SLOW = (
    "command: collectSlowQueryDiagnostics\ncommandOptions: {}\n"
    "destination: root@localhost:3306\nhostname: laptop\noptions:\n  autocomplete.nameCache: true\n"
    "version: 8.4.1\n"
)

# Verbatim PyYAML output (sort_keys, block style, '|' for multi-line strings).
REFERENCED_TABLE_YAML = """DDL: |-
  CREATE TABLE `reviews` (
    `id` int NOT NULL AUTO_INCREMENT,
    `product_id` int NOT NULL,
    `rating` tinyint NOT NULL,
    PRIMARY KEY (`id`),
    KEY `idx_rating` (`rating`)
  ) ENGINE=InnoDB AUTO_INCREMENT=5001 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
Indexes:
- Cardinality: 4980
  Column_name: id
  Index_type: BTREE
  Key_name: PRIMARY
  Non_unique: 0
  Seq_in_index: 1
  Sub_part: null
  Table: reviews
  Visible: 'YES'
- Cardinality: 5
  Column_name: rating
  Index_type: BTREE
  Key_name: idx_rating
  Non_unique: 1
  Seq_in_index: 1
  Sub_part: null
  Table: reviews
  Visible: 'YES'
InnoDB Index Stats: []
InnoDB Table Stats:
- database_name: testdb
  last_update: '2024-05-01 10:00:00'
  n_rows: 4980
  table_name: reviews
Table Name: reviews
Table Schema: testdb
Table Status:
  AUTO_INCREMENT: 5001
  CREATE_TIME: '2024-05-01 10:00:00'
  DATA_FREE: 0
  DATA_LENGTH: 278528
  ENGINE: InnoDB
  INDEX_LENGTH: 98304
  ROW_FORMAT: Dynamic
  TABLE_CATALOG: def
  TABLE_COMMENT: ''
  TABLE_NAME: reviews
  TABLE_ROWS: 4980
  TABLE_SCHEMA: testdb
  TABLE_TYPE: BASE TABLE
  UPDATE_TIME: null
Triggers: []
"""

QUERY_INFO_YAML = """Affected Rows: 0
Execution Time: 0.0123 sec
Query: SELECT r.id, r.rating FROM reviews r WHERE r.rating >= 4 AND r.product_id BETWEEN
  10 AND 500 ORDER BY r.rating DESC
Result Fetch Time: 0.000412
Result Rows: 120
Warnings: []

"""


def dump_query_tsv(statement, columns, rows, warnings=None):
    """Mirror sql_collector.dump_query + write_tsv output."""
    header = "# Query:\n" + "\n".join("#\t" + l for l in statement.split("\n")) + "\n"
    header += "#\n# Started: 2024-05-01T14:00:00.123456\n"
    header += "# Execution Time: 0.0012 sec\n#\n"
    out = ["# " + "\t".join(columns)]
    for row in rows:
        out.append("\t".join("NULL" if f is None else str(f) for f in row))
    if warnings:
        out.append("# Warnings")
        out.extend("\t".join(str(f) for f in w) for w in warnings)
    return header + "".join(line + "\n" for line in out)


def dump_query_error(statement, error):
    header = "# Query:\n" + "\n".join("#\t" + l for l in statement.split("\n")) + "\n"
    header += "#\n# Started: 2024-05-01T14:00:00.123456\n"
    return "{}\n# {}\n".format(header, error)


def trace_tsv(query, trace_text, missing=0, denied=0):
    return dump_query_tsv("SELECT * FROM information_schema.optimizer_trace",
                          ["QUERY", "TRACE", "MISSING_BYTES_BEYOND_MAX_MEM_SIZE",
                           "INSUFFICIENT_PRIVILEGES"],
                          [["EXPLAIN " + query, trace_text, missing, denied]])


def read_fixture():
    with open(ANALYZE_FIXTURE) as fh:
        return fh.read().strip()


def bundle_members(prefix="mysql-diagnostics-20240501-140000/", query=QUERY,
                   analyze=None, estimated=None, tree=None, trace=True,
                   query_info=True, tables=True, variables=True, instance=INSTANCE):
    """Return {member name: text} for a collectSlowQueryDiagnostics bundle."""
    m = {}
    m[prefix + "shell_info.yaml"] = SHELL_INFO_SLOW
    m[prefix + "uri"] = "root@localhost:3306\n"
    if instance is not None:
        m[prefix + "instance"] = instance
    m[prefix + "error_log"] = ""
    m[prefix + "explain.tsv"] = dump_query_tsv(
        "EXPLAIN " + query,
        ["id", "select_type", "table", "partitions", "type", "possible_keys", "key",
         "key_len", "ref", "rows", "filtered", "Extra"],
        [[1, "SIMPLE", "reviews", None, "ALL", "idx_rating", None, None, None, 9921,
          33.33, "Using where"]],
        warnings=[["Note", 1003, "/* select#1 */ select `testdb`.`reviews`.`id` AS `id`"]])
    if estimated is not None:
        m[prefix + "explain_json.tsv"] = dump_query_tsv(
            "EXPLAIN format=json " + query, ["EXPLAIN"], [[estimated]],
            warnings=[["Note", 1003, "/* select#1 */ select `testdb`.`reviews`.`id` AS `id`"]])
    if analyze is not None or tree is not None:
        m[prefix + "explain_analyze.tsv"] = dump_query_tsv(
            "EXPLAIN ANALYZE " + query, ["EXPLAIN"], [[analyze if analyze is not None else tree]])
    if trace is True:
        m[prefix + "explain-optimizer_trace.tsv"] = trace_tsv(query, json.dumps(TRACE, indent=2))
    elif trace:
        m[prefix + "explain-optimizer_trace.tsv"] = trace
    if tables:
        m[prefix + "referenced_table-testdb.reviews.yaml"] = REFERENCED_TABLE_YAML
    if variables:
        m[prefix + "global_variables.tsv"] = dump_query_tsv(
            "SELECT g.variable_name name, g.variable_value value",
            ["name", "value", "source"],
            [["innodb_buffer_pool_size", "134217728", "COMPILED"],
             ["optimizer_switch", "index_merge=on,mrr=on,hash_join=on", "COMPILED"],
             ["sort_buffer_size", "262144", "COMPILED"],
             ["wait_timeout", "28800", "COMPILED"]])
    if query_info:
        m[prefix + "query-info.yaml"] = QUERY_INFO_YAML
    m[prefix + "diagnostics-raw/iteration-1.metrics.tsv"] = "# Variable_name\tVariable_value\n"
    return m


def make_zip(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in members.items():
            zf.writestr(name, text.encode("utf-8") if isinstance(text, str) else text)
    return buf.getvalue()


class TestShellImportAnalyze(unittest.TestCase):
    """explain_format=JSON sessions store EXPLAIN ANALYZE as JSON."""

    def setUp(self):
        self.analyze = read_fixture()
        self.data = make_zip(bundle_members(analyze=self.analyze, estimated=ESTIMATED_V1))
        self.result = load_shell_diagnostics(self.data)

    def test_mode_and_raw_plan(self):
        r = self.result
        self.assertEqual(r["schema_version"], "capture-1.0")
        self.assertEqual(r["source"], "mysqlsh-diagnostics")
        self.assertEqual(r["mode"], "analyze")
        self.assertEqual(json.loads(r["raw"]), json.loads(self.analyze))
        root = parse_explain(r["raw"])
        self.assertAlmostEqual(root["total_time"], 2.366285, places=5)
        self.assertEqual(r["query"], QUERY)
        self.assertEqual(r["attachments"], {})

    def test_server_metadata(self):
        r = self.result
        self.assertEqual(r["engine"], "mysql")
        self.assertEqual(r["server_version"], "8.4.2")
        self.assertEqual(r["captured_at"], "2024-05-01T12:00:00+00:00")
        self.assertEqual(r["conditions"], "")

    def test_run_and_measurements(self):
        r = self.result
        self.assertEqual(len(r["runs"]), 1)
        run = r["runs"][0]
        self.assertAlmostEqual(run["total_time_ms"], 2.366285, places=5)
        self.assertEqual(run["session_status_delta"], {})
        self.assertEqual(run["plan"], r["raw"])
        self.assertAlmostEqual(run["statistics"]["client_execution_ms"], 12.3)
        self.assertAlmostEqual(run["statistics"]["result_fetch_ms"], 0.412)
        self.assertEqual(run["statistics"]["rows_sent"], 120)
        self.assertEqual(run["statistics"]["affected_rows"], 0)
        self.assertEqual(r["measurements"]["count"], 1)
        self.assertAlmostEqual(r["measurements"]["median_ms"], 2.366285, places=5)

    def test_trace(self):
        trace = self.result["trace"]
        self.assertEqual(trace["data"], TRACE)
        self.assertEqual(trace["missing_bytes"], 0)
        self.assertEqual(trace["insufficient_privileges"], 0)

    def test_schema_shape_matches_collectors(self):
        schema = self.result["schema"]
        self.assertEqual(list(schema), ["testdb.reviews"])
        table = schema["testdb.reviews"]
        for key in ("ddl", "table_name", "columns", "indexes", "engine", "charset"):
            self.assertIn(key, table)
        self.assertEqual(table["table_name"], "reviews")
        self.assertTrue(table["ddl"].startswith("CREATE TABLE `reviews` (\n  `id` int"))
        self.assertFalse(table["ddl"].endswith("\n"))
        self.assertEqual([c["name"] for c in table["columns"]], ["id", "product_id", "rating"])
        self.assertIn("idx_rating", [i["name"] for i in table["indexes"]])
        self.assertEqual(table["engine"], "InnoDB")
        self.assertEqual(table["charset"], "utf8mb4")

    def test_table_stats_shape_matches_collectors(self):
        stats = self.result["table_stats"]["testdb.reviews"]
        self.assertEqual(stats, {
            "table_schema": "testdb", "table_name": "reviews", "table_rows": 4980,
            "data_length": 278528, "index_length": 98304, "data_free": 0,
            "auto_increment": 5001, "engine": "InnoDB", "row_format": "Dynamic"})

    def test_variables_filtered(self):
        variables = self.result["variables"]
        self.assertEqual(variables["innodb_buffer_pool_size"], "134217728")
        self.assertEqual(variables["sort_buffer_size"], "262144")
        self.assertIn("optimizer_switch", variables)
        self.assertNotIn("wait_timeout", variables)
        self.assertTrue(any("GLOBAL" in w for w in self.result["warnings"]))

    def test_normalize_capture_accepts_result(self):
        normalized = normalize_capture(self.result)
        self.assertEqual(normalized["mode"], "analyze")
        self.assertEqual(normalized["raw"], self.result["raw"])
        self.assertEqual(normalized["trace"], self.result["trace"])
        json.dumps(self.result, allow_nan=False)

    def test_path_and_file_object_inputs(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "bundle.zip")
            with open(path, "wb") as fh:
                fh.write(self.data)
            self.assertTrue(is_shell_diagnostics_zip(path))
            self.assertEqual(load_shell_diagnostics(path)["raw"], self.result["raw"])
            self.assertEqual(load_shell_diagnostics(io.BytesIO(self.data))["mode"], "analyze")
        finally:
            shutil.rmtree(tmp)


class TestShellImportTreeFallback(unittest.TestCase):
    """Default sessions store EXPLAIN ANALYZE as TREE text."""

    def setUp(self):
        members = bundle_members(query=JOIN_QUERY, tree=TREE_TEXT, estimated=ESTIMATED_V1)
        self.result = load_shell_diagnostics(make_zip(members))

    def test_falls_back_to_estimated_json(self):
        r = self.result
        self.assertEqual(r["mode"], "explain")
        self.assertEqual(json.loads(r["raw"]), json.loads(ESTIMATED_V1))
        root = parse_explain(r["raw"])
        self.assertTrue(root.get("children"))
        self.assertEqual(r["query"], JOIN_QUERY)

    def test_tree_kept_as_attachment_with_warning(self):
        r = self.result
        self.assertEqual(r["attachments"]["explain_analyze_tree"], TREE_TEXT)
        self.assertTrue(any("EXPLAIN ANALYZE was stored as TREE text; using the estimated"
                            in w for w in r["warnings"]))

    def test_run_has_client_timing_but_no_plan_time(self):
        r = self.result
        self.assertEqual(len(r["runs"]), 1)
        self.assertIsNone(r["runs"][0]["total_time_ms"])
        self.assertAlmostEqual(r["runs"][0]["statistics"]["client_execution_ms"], 12.3)
        self.assertEqual(r["measurements"]["count"], 0)
        self.assertIsNone(r["measurements"]["median_ms"])
        normalize_capture(r)

    def test_no_query_info_means_no_runs(self):
        members = bundle_members(tree=TREE_TEXT, estimated=ESTIMATED_V1, query_info=False)
        r = load_shell_diagnostics(make_zip(members))
        self.assertEqual(r["runs"], [])
        self.assertEqual(r["measurements"]["count"], 0)

    def test_pre_8018_bundle_without_analyze_member(self):
        members = bundle_members(estimated=ESTIMATED_V1)
        r = load_shell_diagnostics(make_zip(members))
        self.assertEqual(r["mode"], "explain")
        self.assertTrue(any("no EXPLAIN ANALYZE output" in w for w in r["warnings"]))

    def test_analyze_error_member_reported(self):
        members = bundle_members(estimated=ESTIMATED_V1)
        members["mysql-diagnostics-20240501-140000/explain_analyze.error"] = dump_query_error(
            "EXPLAIN ANALYZE " + QUERY, "MySQL Error (1235): This version of MySQL doesn't yet "
            "support 'EXPLAIN ANALYZE on this query'")
        r = load_shell_diagnostics(make_zip(members))
        self.assertTrue(any("EXPLAIN ANALYZE failed" in w and "1235" in w for w in r["warnings"]))

    def test_tree_only_raises(self):
        members = bundle_members(tree=TREE_TEXT)
        with self.assertRaises(ValueError) as ctx:
            load_shell_diagnostics(make_zip(members))
        self.assertIn("TREE text cannot be imported", str(ctx.exception))

    def test_unreadable_v1_plan_explains_how_to_recollect(self):
        members = bundle_members(tree=TREE_TEXT, estimated='{"query_block": {"select_id": 1}}')
        with self.assertRaises(ValueError) as ctx:
            load_shell_diagnostics(make_zip(members))
        self.assertIn("explain_json_format_version=2", str(ctx.exception))

    def test_mariadb_version_sets_engine(self):
        members = bundle_members(estimated=ESTIMATED_V1, instance=INSTANCE.replace(
            "MySQL Community Server - GPL 8.4.2", "mariadb.org binary distribution 11.4.2-MariaDB"))
        r = load_shell_diagnostics(make_zip(members))
        self.assertEqual(r["engine"], "mariadb")
        self.assertEqual(r["server_version"], "11.4.2-MariaDB")


class TestShellImportTrace(unittest.TestCase):

    def load(self, trace):
        members = bundle_members(analyze=read_fixture(), trace=trace)
        return load_shell_diagnostics(make_zip(members))

    def test_query_containing_tabs_is_split_correctly(self):
        r = self.load(trace_tsv("SELECT *\tFROM reviews", json.dumps(TRACE, indent=2), 0, 0))
        self.assertEqual(r["trace"]["data"], TRACE)

    def test_truncated_trace(self):
        text = json.dumps(TRACE, indent=2)[:40]
        r = self.load(trace_tsv(QUERY, text, 1234, 0))
        self.assertIsNone(r["trace"])
        self.assertTrue(any("1234 bytes missing" in w for w in r["warnings"]))

    def test_insufficient_privileges(self):
        r = self.load(trace_tsv(QUERY, "", 0, 1))
        self.assertIsNone(r["trace"])
        self.assertTrue(any("insufficient privileges" in w for w in r["warnings"]))

    def test_no_trace_member(self):
        r = self.load(False)
        self.assertIsNone(r["trace"])


class TestShellImportMultipleQueries(unittest.TestCase):

    def setUp(self):
        members = bundle_members(prefix="slow-a/", query=QUERY, analyze=read_fixture())
        members.update(bundle_members(prefix="slow-b/", query=JOIN_QUERY, tree=TREE_TEXT,
                                      estimated=ESTIMATED_V1))
        self.data = make_zip(members)

    def test_list_queries(self):
        queries = list_shell_diagnostics_queries(self.data)
        self.assertEqual([q["index"] for q in queries], [0, 1])
        self.assertEqual([q["prefix"] for q in queries], ["slow-a/", "slow-b/"])
        self.assertEqual(queries[1]["query"], JOIN_QUERY)

    def test_select_by_index(self):
        r = load_shell_diagnostics(self.data, query_index=1)
        self.assertEqual(r["query"], JOIN_QUERY)
        self.assertEqual(r["mode"], "explain")
        r = load_shell_diagnostics(self.data, query_index=0)
        self.assertEqual(r["mode"], "analyze")
        self.assertFalse(any("contains 2 queries" in w for w in r["warnings"]))

    def test_default_picks_first_with_warning(self):
        r = load_shell_diagnostics(self.data)
        self.assertEqual(r["query"], QUERY)
        self.assertTrue(any("contains 2 queries" in w for w in r["warnings"]))

    def test_bad_index_lists_available(self):
        with self.assertRaises(ValueError) as ctx:
            load_shell_diagnostics(self.data, query_index=2)
        message = str(ctx.exception)
        self.assertIn("out of range", message)
        self.assertIn("0: slow-a/", message)
        self.assertIn("1: slow-b/", message)
        for bad in (-1, True, "1", 1.0):
            with self.assertRaises(ValueError):
                load_shell_diagnostics(self.data, query_index=bad)


class TestShellImportSafety(unittest.TestCase):

    def setUp(self):
        self.saved = (shell_import.MAX_MEMBER_BYTES, shell_import.MAX_TOTAL_BYTES,
                      shell_import.MAX_MEMBERS)

    def tearDown(self):
        (shell_import.MAX_MEMBER_BYTES, shell_import.MAX_TOTAL_BYTES,
         shell_import.MAX_MEMBERS) = self.saved

    def test_oversized_member(self):
        data = make_zip(bundle_members(analyze=read_fixture()))
        shell_import.MAX_MEMBER_BYTES = 512
        with self.assertRaises(ValueError) as ctx:
            load_shell_diagnostics(data)
        self.assertIn("per-file import limit", str(ctx.exception))

    def test_total_limit(self):
        data = make_zip(bundle_members(analyze=read_fixture()))
        shell_import.MAX_TOTAL_BYTES = 3000
        with self.assertRaises(ValueError) as ctx:
            load_shell_diagnostics(data)
        self.assertIn("total import limit", str(ctx.exception))

    def test_too_many_members(self):
        data = make_zip(bundle_members(analyze=read_fixture()))
        shell_import.MAX_MEMBERS = 3
        with self.assertRaises(ValueError):
            load_shell_diagnostics(data)
        self.assertFalse(is_shell_diagnostics_zip(data))

    def test_unsafe_member_names_ignored(self):
        plan = read_fixture()
        members = {}
        for name, text in bundle_members(prefix="x/", analyze=plan).items():
            members["../" + name] = text
            members["/abs/" + name] = text
        data = make_zip(members)
        self.assertFalse(is_shell_diagnostics_zip(data))
        with self.assertRaises(ValueError) as ctx:
            load_shell_diagnostics(data)
        self.assertIn("not a MySQL Shell diagnostics bundle", str(ctx.exception))

    def test_unsafe_duplicate_does_not_shadow_safe_member(self):
        members = bundle_members(analyze=read_fixture())
        members["../mysql-diagnostics-20240501-140000/explain_analyze.tsv"] = "garbage"
        r = load_shell_diagnostics(make_zip(members))
        self.assertEqual(r["mode"], "analyze")

    def test_nothing_written_to_disk(self):
        tmp = tempfile.mkdtemp()
        cwd = os.getcwd()
        try:
            os.chdir(tmp)
            load_shell_diagnostics(make_zip(bundle_members(analyze=read_fixture())))
            self.assertEqual(os.listdir(tmp), [])
        finally:
            os.chdir(cwd)
            shutil.rmtree(tmp)


class TestShellImportMalformed(unittest.TestCase):

    def test_not_a_zip(self):
        self.assertFalse(is_shell_diagnostics_zip(b"{\"query_block\": {}}"))
        with self.assertRaises(ValueError) as ctx:
            load_shell_diagnostics(b"not a zip at all")
        self.assertIn("not a ZIP archive", str(ctx.exception))

    def test_truncated_zip(self):
        data = make_zip(bundle_members(analyze=read_fixture()))
        with self.assertRaises(ValueError):
            load_shell_diagnostics(data[: len(data) // 2])
        self.assertFalse(is_shell_diagnostics_zip(data[: len(data) // 2]))

    def test_foreign_zip(self):
        data = make_zip({"README.md": "hello", "src/main.py": "print(1)\n"})
        self.assertFalse(is_shell_diagnostics_zip(data))
        with self.assertRaises(ValueError) as ctx:
            load_shell_diagnostics(data)
        self.assertIn("not a MySQL Shell diagnostics bundle", str(ctx.exception))

    def test_high_load_bundle_has_no_plan(self):
        data = make_zip({
            "hl/shell_info.yaml": SHELL_INFO_SLOW.replace(
                "collectSlowQueryDiagnostics", "collectHighLoadDiagnostics"),
            "hl/instance": INSTANCE,
            "hl/slow_queries_in_95_pctile.tsv": "# query\n",
        })
        self.assertTrue(is_shell_diagnostics_zip(data))
        with self.assertRaises(ValueError) as ctx:
            load_shell_diagnostics(data)
        self.assertIn("collectHighLoadDiagnostics", str(ctx.exception))
        self.assertIn("collectSlowQueryDiagnostics", str(ctx.exception))

    def test_garbage_plan_members(self):
        members = bundle_members(estimated="{not json", analyze="also not json")
        with self.assertRaises(ValueError) as ctx:
            load_shell_diagnostics(make_zip(members))
        self.assertIn("No usable JSON plan", str(ctx.exception))

    def test_missing_path_and_bad_type(self):
        with self.assertRaises(ValueError):
            load_shell_diagnostics(os.path.join(tempfile.gettempdir(), "no-such-bundle.zip"))
        with self.assertRaises(ValueError):
            load_shell_diagnostics(12345)
        self.assertFalse(is_shell_diagnostics_zip(12345))

    def test_unreadable_yaml_is_a_warning(self):
        members = bundle_members(analyze=read_fixture())
        members["mysql-diagnostics-20240501-140000/referenced_table-testdb.users.yaml"] = "- just\n- a list\n"
        r = load_shell_diagnostics(make_zip(members))
        self.assertIn("testdb.reviews", r["schema"])
        self.assertTrue(any("referenced_table-testdb.users.yaml" in w for w in r["warnings"]))


class TestMiniYaml(unittest.TestCase):
    """The YAML subset PyYAML's yaml.dump emits for Shell's data."""

    def test_block_mapping_and_sequences(self):
        data = _yaml_load(REFERENCED_TABLE_YAML)
        self.assertEqual(data["Table Status"]["TABLE_ROWS"], 4980)
        self.assertIsNone(data["Table Status"]["UPDATE_TIME"])
        self.assertEqual(data["Table Status"]["TABLE_COMMENT"], "")
        self.assertEqual(data["Table Status"]["CREATE_TIME"], "2024-05-01 10:00:00")
        self.assertEqual(data["Indexes"][1]["Key_name"], "idx_rating")
        self.assertEqual(data["Indexes"][0]["Visible"], "YES")
        self.assertEqual(data["Triggers"], [])
        self.assertTrue(data["DDL"].endswith("COLLATE=utf8mb4_0900_ai_ci"))

    def test_folded_plain_scalar(self):
        data = _yaml_load(QUERY_INFO_YAML)
        self.assertEqual(data["Query"], "SELECT r.id, r.rating FROM reviews r WHERE r.rating >= 4 "
                                        "AND r.product_id BETWEEN 10 AND 500 ORDER BY r.rating DESC")
        self.assertEqual(data["Execution Time"], "0.0123 sec")
        self.assertEqual(data["Result Fetch Time"], 0.000412)

    def test_quoted_scalars(self):
        # PyYAML output for {"a": "it's", "b": "tab\there é", "c": "x: y",
        # "d": long string with an escaped line break}
        text = ("a: it's\nb: \"tab\\there \\xE9\"\nc: 'x: y'\n"
                "d: \"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\\\n"
                "  \\ bbbbb\\t\"\ne: 'it''s\n\n  two'\n")
        data = _yaml_load(text)
        self.assertEqual(data["a"], "it's")
        self.assertEqual(data["b"], "tab\there \u00e9")
        self.assertEqual(data["c"], "x: y")
        self.assertEqual(data["d"], "a" * 74 + " bbbbb\t")
        self.assertEqual(data["e"], "it's\ntwo")

    def test_literal_block_chomping_and_indent_indicator(self):
        text = "a: |\n  one\n  two\nb: |2-\n    indented\n  next\nc: |+\n  keep\n\nd: 1\n"
        data = _yaml_load(text)
        self.assertEqual(data["a"], "one\ntwo\n")
        self.assertEqual(data["b"], "  indented\nnext")
        self.assertEqual(data["c"], "keep\n\n")
        self.assertEqual(data["d"], 1)

    def test_flow_style_from_older_pyyaml(self):
        text = ("Indexes:\n- {Cardinality: 4980, Key_name: PRIMARY, Sub_part: null, Visible: 'YES'}\n"
                "Warnings: []\nTable Status: {TABLE_ROWS: 10, ENGINE: InnoDB,\n  ROW_FORMAT: Dynamic}\n")
        data = _yaml_load(text)
        self.assertEqual(data["Indexes"][0], {"Cardinality": 4980, "Key_name": "PRIMARY",
                                              "Sub_part": None, "Visible": "YES"})
        self.assertEqual(data["Table Status"]["ROW_FORMAT"], "Dynamic")
        self.assertEqual(data["Warnings"], [])

    def test_shell_seconds(self):
        self.assertAlmostEqual(shell_import._shell_seconds("0.0123 sec"), 0.0123)
        self.assertAlmostEqual(shell_import._shell_seconds("2 min 3.5000 sec"), 123.5)
        self.assertAlmostEqual(shell_import._shell_seconds("1 day 2 hours 0.0000 sec"), 93600.0)
        self.assertIsNone(shell_import._shell_seconds("soon"))
        self.assertIsNone(shell_import._shell_seconds(None))


if __name__ == "__main__":
    unittest.main()
