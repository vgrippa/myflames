"""VillageSQL behaviors the first test pass left unexercised.

Every case here is synthetic or replays committed recordings; nothing
connects to a server. Tests that reach the UI handler or lifecycle jobs
reuse those suites' setUp, which isolates HOME and USERPROFILE in a
temporary directory with MYFLAMES_SECRET_BACKEND=vault.

Engine facts the expectations rely on (checked in source, 2026-11-27):

* MySQL JSON v2 writes Limit's ``limit`` as the stop count including the
  offset and the offset separately (mysql-server
  sql/join_optimizer/explain_access_path.cc:1797-1814), and VillageSQL
  passes ``select_limit_cnt`` (row count plus offset, sql/sql_lex.cc
  Query_expression::set_limit) to the KNN index as K
  (villagesql-server villagesql/sql/optimizer.cc:148-201).
* VillageSQL refuses every non-internal statement on villagesql.* tables
  (villagesql-server sql/dd/impl/dictionary_impl.cc:333-340, raised as
  ER_NO_SYSTEM_TABLE_ACCESS from sql/sql_parse.cc:6259). mysqldump
  --all-databases skips only information_schema, performance_schema, sys
  and ndbinfo (client/mysqldump.cc dump_all_databases), and MySQL Shell's
  dumpInstance always excludes only information_schema, mysql, ndbinfo,
  performance_schema and sys (mysql-shell
  modules/util/common/dump/constants.h:61-63).
* On the MySQL engine, only Percona Server adds userstat; MySQL has none.

Tests marked expectedFailure expose defects in the change; each docstring
names the file, the input and the wrong output. MariaDB cannot produce a
VillageSQL KNN access or identity, so its cases assert unchanged behavior.
"""
import glob
import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Modules, not classes: importing a TestCase class by name would run its
# tests again in this module.
import test_extensions as _extensions_suite  # noqa: E402
import test_lifecycle as _lifecycle_suite  # noqa: E402
from _perf_replay import FakeTime, assert_read_only, expanded_preflight_sql, replay  # noqa: E402
from test_chart_data import COMPACT_SCHEMA, SCHEMA, validate  # noqa: E402
from test_indexes import raw as index_raw, stat  # noqa: E402
from test_io_stats import snap  # noqa: E402

from myflames import (config_review, dbops, extensions, health, indexes, io_stats, lifecycle as lc,  # noqa: E402
                      parameters, perf_common, ps_instruments, schema_health, server_info, ui, variables, waits)
from myflames.chart_data import build_chart_data  # noqa: E402
from myflames.connector import ConnectorError, MySQLConnection  # noqa: E402
from myflames.parser import (flatten_nodes, hide_parsed_plan_literals, is_knn_scan, knn_description,  # noqa: E402
                             parse_explain)
from myflames.perf_common import Server  # noqa: E402

TEST = Path(__file__).resolve().parent
VERSION = "8.4.11-villagesql-0.0.8"
SOURCE = "Source distribution"
PERCONA_COMMENT = "Percona Server (GPL), Release 11, Revision 57878ff8"
OTHER_PRODUCTS = (
    ("8.4.11", "MySQL Community Server - GPL", "mysql", "MySQL"),
    ("8.4.11-11", PERCONA_COMMENT, "percona", "Percona Server"),
    ("11.4.13-MariaDB", "MariaDB Server", "mariadb", "MariaDB"),
    ("8.0.26", "Readyset", "readyset", "Readyset"),
)


def parsed(value):
    return parse_explain(json.dumps(value))


def released_knn(**overrides):
    """The released 0.0.8 v2 access node: no table, the index only in prose."""
    value = {"operation": "Custom index distance scan on idx_v", "access_type": "index_distance_scan",
             "actual_rows": 15.0, "actual_loops": 1, "estimated_rows": 2005.0,
             "actual_first_row_ms": 0.9, "actual_last_row_ms": 1.0}
    value.update(overrides)
    return value


def knn_nodes(root):
    return [node for node in flatten_nodes(root) if is_knn_scan(node["details"])]


def village_server(codebase=None, ps="ON", userstat="OFF", comment=SOURCE):
    values = {"version": VERSION, "version_comment": comment, "performance_schema": ps, "userstat": userstat}
    if codebase is not None:
        values["villagesql_server_version"] = codebase
    return Server(values, {"Uptime": "864000"})


def corpus_plans():
    """Every committed MySQL and MariaDB plan; VillageSQL plans live in a subfolder."""
    for path in sorted(glob.glob(str(TEST / "fixtures" / "*.json")) + glob.glob(str(TEST / "*.json"))):
        try:
            with open(path, encoding="utf-8") as handle:
                yield path, parse_explain(handle.read())
        except ValueError:
            continue  # Non-plan JSON (schemas, scorecards) shares these folders.


# ---------------------------------------------------------------------------
# KNN plans: K inheritance boundaries and unchanged MySQL/MariaDB plans
# ---------------------------------------------------------------------------


class TestKnnLimitBoundaries(unittest.TestCase):
    def test_limit_offset_k_excludes_the_offset(self):
        # F13 / K1: LIMIT 10 OFFSET 5. MySQL's JSON Limit node `limit` is the
        # stop count including the offset (15, limit_offset=5), but released
        # VillageSQL 0.0.8 hands the custom scan query_block->get_limit() without
        # the offset (sql/sql_executor.cc:3849-3851), so the inherited K is 10.
        source = {"operation": "Limit/Offset: 10/5 row(s)", "access_type": "limit",
                  "limit": 15, "limit_offset": 5, "inputs": [released_knn()]}
        root = parsed(source)
        node = knn_nodes(root)[0]
        self.assertEqual(node["details"]["limit"], 10)
        self.assertEqual(node["short_label"], "KNN SCAN [idx_v] K=10")
        self.assertTrue(node["folded_label"].startswith("KNN SCAN [idx_v] K=10 starts=1"))
        self.assertEqual(knn_description(node["details"]),
                         "Up to 10 nearest rows requested through custom index idx_v")
        row = next(row for row in build_chart_data(root)["nodes"] if row["id"] == node["node_id"])
        self.assertEqual(row["limit"], 10)
        self.assertNotIn("limit", source["inputs"][0], "the caller's plan JSON is not rewritten")

    def test_select_list_subquery_under_limit_does_not_inherit_k(self):
        """parser.parse_node: only regular inputs receive Limit's K; a
        SELECT-list subquery runs per outer row and is not bounded by it."""
        source = {"operation": "Limit: 10 row(s)", "access_type": "limit", "limit": 10,
                  "inputs": [{"operation": "Table scan on o", "access_type": "table", "table_name": "o"}],
                  "inputs_from_select_list": [released_knn()]}
        root = parsed(source)
        node = knn_nodes(root)[0]
        self.assertEqual(node["details"].get("input_kind"), "select_list")
        self.assertNotIn("limit", node["details"])
        self.assertEqual(node["short_label"], "KNN SCAN [idx_v]")
        self.assertEqual(knn_description(node["details"]), "Nearest rows through custom index idx_v")

    def test_knn_below_sort_or_stream_under_limit_has_no_k(self):
        """Only Limit's direct input inherits K; any other iterator in
        between may read more rows than Limit returns."""
        for operation, access in (("Sort: t.id", "sort"), ("Stream results", "stream"),
                                  ("Materialize", "materialize")):
            with self.subTest(intermediate=operation):
                root = parsed({"operation": "Limit: 10 row(s)", "access_type": "limit", "limit": 10,
                               "inputs": [{"operation": operation, "access_type": access,
                                           "inputs": [released_knn()]}]})
                node = knn_nodes(root)[0]
                self.assertNotIn("limit", node["details"])
                self.assertEqual(node["short_label"], "KNN SCAN [idx_v]")

    def test_ordinary_index_scan_under_limit_gains_no_limit_field(self):
        """K inheritance is KNN-only: an ordinary index read under Limit
        keeps byte-identical chart data in both versions."""
        index = {"operation": "Index scan on t using idx", "access_type": "index",
                 "index_access_type": "index_scan", "table_name": "t", "index_name": "idx",
                 "actual_rows": 10.0, "actual_loops": 1, "estimated_rows": 100.0}
        root = parsed({"operation": "Limit: 10 row(s)", "access_type": "limit", "limit": 10, "inputs": [index]})
        child = root["children"][0]
        self.assertNotIn("limit", child["details"])
        self.assertEqual(root["details"]["limit"], 10, "the Limit node keeps its own field")
        for compact, schema in ((False, SCHEMA), (True, COMPACT_SCHEMA)):
            data = build_chart_data(root, compact=compact)
            validate(data, schema)
            for row in data["nodes"]:
                for key in ("limit", "lookup_condition", "access_description"):
                    self.assertNotIn(key, row, (compact, row["id"], key))


class TestUnchangedPlanCorpus(unittest.TestCase):
    def test_existing_mysql_and_mariadb_chart_data_gains_no_knn_fields(self):
        """chart_data adds limit/lookup_condition/access_description only for
        KNN reads; every committed MySQL and MariaDB plan must stay as before
        in both schema versions."""
        count = 0
        for path, root in corpus_plans():
            count += 1
            for compact in (False, True):
                for row in build_chart_data(root, compact=compact)["nodes"]:
                    for key in ("limit", "lookup_condition", "access_description"):
                        self.assertNotIn(key, row, "%s node %s gained %s" % (os.path.basename(path), row["id"], key))
        self.assertGreater(count, 150, "the corpus must not silently shrink")

    def test_ordinary_lookup_condition_literals_are_hidden_on_the_sharing_path(self):
        """parse_node now copies lookup_condition into details for every
        MySQL v2 index lookup, not only KNN. plan_privacy.LITERAL_FIELDS must
        hide its literals, or a shared plan leaks values such as country='US'."""
        literal = re.compile(r"'[^']*'|=\s*-?\d")
        checked = 0
        for path, root in corpus_plans():
            hidden = hide_parsed_plan_literals(root)
            for before, after in zip(flatten_nodes(root), flatten_nodes(hidden)):
                value = before["details"].get("lookup_condition")
                if value and literal.search(value):
                    checked += 1
                    self.assertFalse(literal.search(after["details"]["lookup_condition"]),
                                     "%s: %s" % (os.path.basename(path), after["details"]["lookup_condition"]))
                    self.assertNotIn(value, json.dumps(after))
        self.assertGreater(checked, 10, "the corpus must exercise literal lookups")
        raw = {"operation": "Single-row index lookup on u using email (email = 'alice@example.com')",
               "access_type": "index", "index_access_type": "index_lookup", "table_name": "u",
               "index_name": "email", "lookup_condition": "email = 'alice@example.com'", "estimated_rows": 1.0}
        root = parsed(raw)
        self.assertEqual(root["details"]["lookup_condition"], "email = 'alice@example.com'")
        shared = hide_parsed_plan_literals(root)
        self.assertNotIn("alice@example.com", json.dumps(shared))
        self.assertEqual(shared["details"]["lookup_condition"], "email = ?")


# ---------------------------------------------------------------------------
# Percona capabilities on VillageSQL with a proved, MySQL or unknown base
# ---------------------------------------------------------------------------


def strip_codebase(sql, text, n):
    """Model unavailable metadata: SHOW omits the name, as on a server lacking it."""
    if "SHOW GLOBAL VARIABLES WHERE" in sql and "villagesql_server_version" in sql and text:
        return "".join(line + "\n" for line in text.split("\n")
                       if line and not line.startswith("villagesql_server_version\t"))
    return text


class TestPerconaCapabilitySites(unittest.TestCase):
    def test_config_reads_percona_slow_log_settings_only_on_proved_percona_base(self):
        """config_review.config_report: the recorded Percona codebase reads the
        Percona slow-log variables; the MySQL codebase and the same Percona
        server without codebase metadata (unknown base) do not."""
        cases = (("villagesql-percona-8.4", None, "percona", 1),
                 ("villagesql-percona-8.4", strip_codebase, None, 0),
                 ("villagesql-8.4", None, "mysql", 0),
                 ("villagesql-8.4", strip_codebase, None, 0))
        for name, edit, base, slow_reads in cases:
            with self.subTest(server=name, codebase=edit is None):
                conn, data = replay("config", name, edit=edit)
                report = config_review.config_report(conn, **data["args"])
                assert_read_only(self, conn, config_review.CONFIG_TAG)
                self.assertEqual((report["flavor"], report["server_base"]), ("villagesql", base))
                sent = [sql for sql, _flags in conn.calls]
                self.assertEqual(sum("log_slow_rate_limit" in sql for sql in sent), slow_reads)
                head = config_review.format_text(report).splitlines()[0]
                expected = {"percona": "VillageSQL 0.0.8 (Percona Server 8.4.11)",
                            "mysql": "VillageSQL 0.0.8 (MySQL 8.4.11)", None: "VillageSQL 0.0.8 (8.4.11)"}[base]
                self.assertTrue(head.startswith(expected + ","), head)

    def test_index_and_io_userstat_hints_need_a_proved_percona_base(self):
        """indexes._notes and io_stats._notes suggest SET GLOBAL userstat = ON
        only where the variable exists: MariaDB, Percona, VillageSQL Percona."""
        rows = [stat("orders", "PRIMARY", 1, "id", 0), stat("orders", "k_user", 1, "user_id")]
        for codebase, expected in (("percona-8.4_0.0.8", True), ("mysql-8.4_0.0.8", False), (None, False),
                                   ("percona-8.4_0.0.8\n", False)):
            with self.subTest(codebase=codebase):
                server = village_server(codebase, ps="OFF")
                report = indexes.compute_indexes(server, index_raw(rows, None), limit=None)
                self.assertEqual(report["source"], None)
                self.assertEqual(any("SET GLOBAL userstat = ON" in note for note in report["notes"]), expected)
                io = io_stats.compute_io(server, snap(0), snap(10), "status", 10, None, 20)
                self.assertEqual(any("SET GLOBAL userstat = ON" in note for note in io["notes"]), expected)
                self.assertEqual(io["server_base"], "percona" if expected else
                                 ("mysql" if codebase == "mysql-8.4_0.0.8" else None))

    def userstat_report(self, server):
        rows = [stat("orders", "PRIMARY", 1, "id", 0), stat("orders", "k_user", 1, "user_id")]
        # Partitioning metadata unavailable: the table is not known to be plain.
        captured = index_raw(rows, None, index_stats=[["shop", "orders", "PRIMARY", "10"],
                                                      ["shop", "orders", "k_user", "5"]],
                             table_stats=[["shop", "orders", "1000"]], partitioned=[])
        return indexes.compute_indexes(server, captured, limit=None)

    def test_percona_userstat_guard_applies_on_proved_percona_base(self):
        for server in (Server({"version": "8.4.11-11", "version_comment": PERCONA_COMMENT,
                               "performance_schema": "OFF", "userstat": "ON"}, {"Uptime": "864000"}),
                       village_server("percona-8.4_0.0.8", ps="OFF", userstat="ON")):
            with self.subTest(flavor=server.flavor):
                report = self.userstat_report(server)
                self.assertEqual(report["source"], "userstat")
                self.assertEqual(report["full_scans"], [])
                self.assertTrue(any("shown only for known plain tables" in note for note in report["notes"]))
        # MariaDB keeps its own userstat rules: no Percona partition guard.
        maria = Server({"version": "11.4.13-MariaDB", "version_comment": "MariaDB Server",
                        "performance_schema": "OFF", "userstat": "ON"}, {"Uptime": "864000"})
        self.assertEqual([row["table"] for row in self.userstat_report(maria)["full_scans"]], ["orders"])

    def test_unknown_base_with_userstat_does_not_claim_full_scans_on_unknown_partitioning(self):
        """DEFECT (fail-open). myflames/indexes.py:685 skips the Percona
        partition guard unless has_percona_features(flavor, base) is true.
        Input: VillageSQL 8.4.11-villagesql-0.0.8, codebase metadata missing
        (base None), performance_schema=OFF, userstat=ON, table shop.orders with
        unknown partitioning, TABLE_STATISTICS rows_read=1000, INDEX_STATISTICS
        15. Output: full_scans=[{table: orders, mostly: True}], while Percona
        Server and VillageSQL with base percona report none. On the MySQL
        engine only Percona has userstat, so this server is Percona-based; an
        unknown base disables the safety guard while the Percona data source
        is still used."""
        report = self.userstat_report(village_server(None, ps="OFF", userstat="ON"))
        self.assertEqual(report["full_scans"], [])

    def test_single_variable_vendor_follows_proved_base(self):
        base = {"engine": "mysql", "flavor": "villagesql", "server_product": "VillageSQL",
                "server_version": VERSION, "version_tuple": (8, 4, 11), "in_global": True, "in_session": False,
                "global_value": "ON", "session_value": None, "catalog": None, "info": None, "metadata": None,
                "persisted_globals_load": "ON", "persisted": {"present": False, "value": None, "read": True},
                "notes": []}
        for server_base, vendor in (("percona", "percona"), ("mysql", None), (None, None)):
            with self.subTest(base=server_base):
                report = variables.describe(dict(base, name="userstat", server_base=server_base))
                self.assertEqual(report["vendor"], vendor)
                self.assertEqual(report["server_base"], server_base)

    def test_percona_server_documents_its_own_variables(self):
        """Companion to the expectedFailure below: Percona Server links a
        Percona-only variable to Percona's manual, and VillageSQL's Percona
        codebase marks the same variable with vendor percona."""
        rows = [["binlog_space_limit", "0"], ["max_connections", "151"]]
        percona = parameters.compute_parameters("mysql", (8, 4, 11), rows=rows, info_rows=[], persisted_rows=[],
                                                flavor="percona")
        found = {item["name"]: item for item in percona["items"]}
        self.assertEqual(found["binlog_space_limit"]["vendor"], "percona")
        self.assertIn("docs.percona.com", found["binlog_space_limit"]["documentation"]["url"])
        village = parameters.compute_parameters("mysql", (8, 4, 11), rows=rows, info_rows=[], persisted_rows=[],
                                                flavor="villagesql", base="percona")
        self.assertEqual({item["name"]: item for item in village["items"]}["binlog_space_limit"]["vendor"], "percona")

    def test_villagesql_percona_codebase_keeps_percona_documentation_links(self):
        """DEFECT. myflames/parameters.py:100 returns no documentation unless
        flavor == "percona"; compute_parameters (parameters.py:217) passes
        flavor "villagesql" and no base. Input: compute_parameters("mysql",
        (8, 4, 11), rows=[["binlog_space_limit", "0"]], flavor="villagesql",
        base="percona"). Output: vendor "percona" but documentation None,
        where Percona Server gets docs.percona.com/.../binlog-space.html.
        DEVELOPER_REFERENCE "Server identity: VillageSQL" says
        has_percona_features controls parameter vendor links."""
        report = parameters.compute_parameters("mysql", (8, 4, 11), rows=[["binlog_space_limit", "0"]],
                                               info_rows=[], persisted_rows=[], flavor="villagesql", base="percona")
        item = report["items"][0]
        self.assertEqual(item["vendor"], "percona")
        self.assertIsNotNone(item["documentation"])
        self.assertIn("docs.percona.com", item["documentation"]["url"])


# ---------------------------------------------------------------------------
# Optional codebase reads on adapters and connection paths
# ---------------------------------------------------------------------------


class QueryRowsOnly(object):
    """An older adapter: no cached reader, only query_rows."""

    def __init__(self, answer="percona-8.4_0.0.8", failure=None):
        self.answer, self.failure, self.calls = answer, failure, []

    def query_rows(self, sql):
        self.calls.append(sql)
        if self.failure:
            raise ConnectorError(self.failure)
        return [[self.answer]] if self.answer is not None else []


class TestCodebaseReadFallbacks(unittest.TestCase):
    def test_query_rows_fallback_reads_only_on_villagesql_and_failures_are_optional(self):
        conn = QueryRowsOnly()
        self.assertEqual(perf_common.connection_codebase(conn, VERSION, SOURCE), "percona-8.4_0.0.8")
        self.assertEqual(conn.calls, ["SELECT @@villagesql_server_version"])
        self.assertEqual(perf_common.conn_identity(QueryRowsOnly(), "mysql", VERSION)["server_base"], "percona")
        for conn, expected in ((QueryRowsOnly(failure="ERROR 1193 (HY000): Unknown system variable"), None),
                               (QueryRowsOnly(answer=None), None)):
            self.assertEqual(perf_common.connection_codebase(conn, VERSION, SOURCE), expected)
        self.assertIsNone(perf_common.connection_codebase(object(), VERSION, SOURCE))
        for version, comment, _flavor, _product in OTHER_PRODUCTS:
            with self.subTest(version=version):
                conn = QueryRowsOnly()
                self.assertIsNone(perf_common.connection_codebase(conn, version, comment))
                self.assertEqual(conn.calls, [], "other servers reject the variable")

    def connection_test(self, version, comment, codebase=None, failure=None):
        answers = {"SELECT @@version": version + "\n", "SELECT @@version_comment": comment + "\n",
                   "SHOW DATABASES": "shop\n"}
        sent = []

        def run(sql, **kwargs):
            sent.append(sql)
            if sql == "SELECT @@villagesql_server_version":
                if failure:
                    raise ConnectorError(failure)
                return codebase + "\n"
            return answers[sql]

        conn = MySQLConnection(host="identity.invalid", user="test")
        with mock.patch.object(MySQLConnection, "run", side_effect=run):
            return ui._connection_test_report(conn, {}, None), sent

    def test_connection_test_names_villagesql_base_and_probes_no_other_product(self):
        """ui._connection_test_report with the real cached connection readers."""
        for codebase, failure, base in (("percona-8.4_0.0.8", None, "percona"), ("mysql-8.4_0.0.8", None, "mysql"),
                                        (None, "ERROR 1193 (HY000): Unknown system variable", None)):
            with self.subTest(codebase=codebase):
                result, sent = self.connection_test(VERSION, SOURCE, codebase, failure)
                self.assertEqual((result["engine"], result["flavor"], result["product"], result["server_base"]),
                                 ("mysql", "villagesql", "VillageSQL", base))
                self.assertEqual(result["databases"], ["shop"])
                self.assertEqual(sent.count("SELECT @@villagesql_server_version"), 1)
        for version, comment, flavor, product in OTHER_PRODUCTS[:3]:
            with self.subTest(product=product):
                result, sent = self.connection_test(version, comment)
                self.assertEqual((result["flavor"], result["product"]), (flavor, product))
                self.assertNotIn("server_base", result)
                self.assertFalse(any("villagesql" in sql for sql in sent), sent)

    def test_health_reads_the_codebase_inside_its_one_show_statement(self):
        # H1: Health's existing SHOW GLOBAL VARIABLES list carries
        # villagesql_server_version (SHOW omits unknown names elsewhere), so
        # no separate codebase statement is ever sent, on any product.
        class HealthConn(object):
            def __init__(self, version, comment, codebase=None):
                self.version, self.comment, self.codebase = version, comment, codebase
                self.codebase_calls, self.sent = [], []

            def run(self, sql, extra_flags=None, **kwargs):
                self.sent.append(sql)
                text = "version\t%s\nversion_comment\t%s\nthread_handling\tone-thread-per-connection\n" % (
                    self.version, self.comment)
                if self.codebase and "'villagesql_server_version'" in sql:
                    text += "villagesql_server_version\t%s\n" % self.codebase
                return text

            def villagesql_server_version(self, version=None, version_comment=None):
                self.codebase_calls.append((version, version_comment))
                return "percona-8.4_0.0.8"

        conn = HealthConn(VERSION, SOURCE, "percona-8.4_0.0.8")
        found = health.read_variables(conn)
        self.assertEqual(found.get("villagesql_server_version"), "percona-8.4_0.0.8")
        self.assertEqual(conn.codebase_calls, [], "no separate codebase read")
        self.assertEqual(len(conn.sent), 1, conn.sent)
        self.assertNotIn("@@villagesql", conn.sent[0])
        with open(TEST / "fixtures" / "health" / "villagesql-percona-8.4.json", encoding="utf-8") as handle:
            data = json.load(handle)
        report = health.compute_health(data["samples"][0], data["samples"][1], found, 5)
        self.assertEqual((report["flavor"], report["server_base"]), ("villagesql", "percona"))
        for version, comment, flavor, _product in OTHER_PRODUCTS[:3]:
            with self.subTest(flavor=flavor):
                conn = HealthConn(version, comment)
                found = health.read_variables(conn)
                self.assertNotIn("villagesql_server_version", found)
                self.assertEqual(conn.codebase_calls, [])


# ---------------------------------------------------------------------------
# Recorded VillageSQL views and unchanged MariaDB / MySQL 5.7 recordings
# ---------------------------------------------------------------------------


class TestRecordedVillageViews(unittest.TestCase):
    """These recordings were committed but no test replayed them."""

    LABELS = {"villagesql-8.4": ("mysql", "VillageSQL 0.0.8 (MySQL 8.4.11)"),
              "villagesql-percona-8.4": ("percona", "VillageSQL 0.0.8 (Percona Server 8.4.11)")}

    def test_config_tables_and_indexes_replay_every_recorded_statement(self):
        views = (("config", config_review.config_report, config_review.format_text, config_review.CONFIG_TAG),
                 ("tables", schema_health.table_report, schema_health.format_text, schema_health.TABLES_TAG),
                 ("indexes", indexes.index_report, indexes.format_text, indexes.INDEXES_TAG))
        for name, (base, label) in sorted(self.LABELS.items()):
            for view, read, text, tag in views:
                with self.subTest(server=name, view=view):
                    conn, data = replay(view, name)
                    report = read(conn, **data["args"])
                    assert_read_only(self, conn, tag)
                    json.dumps(report, allow_nan=False)
                    # A VillageSQL recording answers its own codebase: no alias.
                    self.assertEqual(conn.aliases, {})
                    self.assertEqual([sql for sql, _flags in conn.calls], [call[0] for call in data["calls"]])
                    self.assertEqual((report["engine"], report["flavor"], report["server_product"],
                                      report["server_base"]), ("mysql", "villagesql", "VillageSQL", base))
                    self.assertTrue(text(report).startswith(label + ","), text(report).splitlines()[0])

    def test_recorded_health_samples_keep_product_and_base(self):
        for name, (base, label) in sorted(self.LABELS.items()):
            with self.subTest(server=name):
                with open(TEST / "fixtures" / "health" / (name + ".json"), encoding="utf-8") as handle:
                    data = json.load(handle)
                report = health.compute_health(data["samples"][0], data["samples"][1], data["variables"], 5)
                self.assertEqual((report["flavor"], report["server_product"], report["server_base"]),
                                 ("villagesql", "VillageSQL", base))
                self.assertTrue(health.format_text(report).startswith(label + ","))
                json.dumps(report, allow_nan=False)


class TestUnchangedOlderRecordings(unittest.TestCase):
    """MariaDB and MySQL 5.7 recordings predate the optional codebase name."""

    SERVERS = ("mariadb-11.4", "mariadb-11.4-userstat", "mariadb-13.0", "mariadb-13.0-default", "mysql-5.7")
    CASES = (tuple(("config", server) for server in SERVERS) + tuple(("waits", server) for server in SERVERS) +
             tuple(("instruments", server) for server in ("mariadb-11.4", "mariadb-11.4-default", "mariadb-13.0",
                                                         "mariadb-13.0-default", "mariadb-13.0-minimal",
                                                         "mysql-5.7")) +
             (("io", "mariadb-13.0-default"), ("io", "mysql-5.7"),
              ("tables", "mariadb-11.4-partitioned"), ("tables", "mysql-5.7")))

    def run_view(self, view, server):
        clock = FakeTime()
        conn, data = replay(view, server, clock=clock)
        args = data.get("args") or {}
        if view == "config":
            return config_review.config_report(conn, **args), conn, data, config_review.CONFIG_TAG
        if view == "waits":
            return (waits.waits_report(conn, args["interval"], args["limit"], sleep=clock.sleep,
                                       clock=clock.monotonic), conn, data, waits.WAITS_TAG)
        if view == "io":
            return (io_stats.io_report(conn, args["interval"], args["schema"], args["limit"], sleep=clock.sleep,
                                       clock=clock.monotonic), conn, data, io_stats.IO_TAG)
        if view == "instruments":
            return ps_instruments.instruments_report(conn), conn, data, ps_instruments.INSTRUMENTS_TAG
        return schema_health.table_report(conn, **args), conn, data, schema_health.TABLES_TAG

    def test_only_the_preflight_in_list_gains_the_optional_name(self):
        """The preflight change may add villagesql_server_version to the one
        existing SHOW IN list and nothing else: same statements, same order,
        no probe, no extra exclusion, no server_base on these products."""
        for view, server in self.CASES:
            with self.subTest(view=view, server=server):
                report, conn, data, tag = self.run_view(view, server)
                assert_read_only(self, conn, tag)
                sent = [sql for sql, _flags in conn.calls]
                self.assertEqual(sent, [expanded_preflight_sql(call[0], call[1]) for call in data["calls"]])
                self.assertEqual(len(conn.aliases), 1, "exactly one preflight statement is expanded")
                self.assertIn(next(iter(conn.aliases)), sent)
                for sql in sent:
                    if sql not in conn.aliases:
                        self.assertNotIn("villagesql", sql.lower())
                self.assertEqual(report["flavor"], "mysql" if server == "mysql-5.7" else "mariadb")
                self.assertNotIn("server_base", report)


# ---------------------------------------------------------------------------
# Extensions endpoint on other products through the real UI handler
# ---------------------------------------------------------------------------


class TestExtensionsEndpointOnOtherProducts(unittest.TestCase):
    path = "/api/live/extensions"
    setUp = _extensions_suite.ExtensionsAPI.setUp
    post = _extensions_suite.ExtensionsAPI.post

    def test_other_products_get_an_unavailable_report_after_two_identity_reads(self):
        for version, comment, flavor, product in OTHER_PRODUCTS:
            with self.subTest(product=product):
                conn = _extensions_suite.Connection(version=version, comment=comment)
                with mock.patch("myflames.connector.MySQLConnection", _extensions_suite.FakeFactory(conn)):
                    status, report = self.post({"connection": self.connection})
                self.assertEqual(status, 200)
                _extensions_suite.validate(report, _extensions_suite.SCHEMA)
                self.assertEqual((report["schema_version"], report["flavor"], report["server_product"]),
                                 ("extensions-1.0", flavor, product))
                self.assertFalse(report["available"])
                self.assertEqual(report["reason"], "Extensions are available only on VillageSQL connections.")
                self.assertNotIn("server_base", report)
                self.assertIsNone(report["extensions"])
                self.assertIsNone(report["summary"]["installed"])
                self.assertEqual([sql for sql, _flags in conn.calls],
                                 [extensions.EXTENSIONS_TAG + extensions.IDENTITY_SQL,
                                  extensions.EXTENSIONS_TAG + extensions.UPTIME_SQL])
                self.assertNotIn(_extensions_suite.Connection.password, json.dumps(report))


# ---------------------------------------------------------------------------
# Parameters and single-variable reads redact the pre-existing inventory
# ---------------------------------------------------------------------------

# The secret inventory before this change (git HEAD), spelled out so a
# narrowed inventory fails here instead of quietly passing.
PRE_EXISTING_SECRETS = (
    "wsrep_sst_auth", "report_password", "audit_log_encryption_password",
    "keyring_encrypted_file_password", "keyring_hashicorp_secret_id", "s3_secret_key",
    "handlersocket_plain_secret", "handlersocket_plain_secret_wr", "utility_user_password",
    "file_key_management_filekey",
    "telemetry.otel_exporter_otlp_logs_headers", "telemetry.otel_exporter_otlp_metrics_headers",
    "telemetry.otel_exporter_otlp_traces_headers", "telemetry.otel_exporter_otlp_logs_secret_headers",
    "telemetry.otel_exporter_otlp_metrics_secret_headers", "telemetry.otel_exporter_otlp_traces_secret_headers",
)


class TestPreExistingSecretsInParameters(unittest.TestCase):
    def test_parameters_list_redacts_each_pre_existing_secret_variable(self):
        """parameters.compute_parameters redacts value and default per name;
        before this change Parameters returned these values verbatim."""
        for name in PRE_EXISTING_SECRETS:
            secret = "private-%s-value" % name.replace(".", "-")
            for shown in (name, name.upper()):
                with self.subTest(name=shown):
                    report = parameters.compute_parameters(
                        "mysql", (8, 4, 11), rows=[[shown, secret], ["max_connections", "151"]],
                        info_rows=[], persisted_rows=[], flavor="mysql")
                    self.assertNotIn(secret, json.dumps(report))
                    # Parameters lists names in lowercase, as SHOW returns them.
                    found = {item["name"]: item for item in report["items"]}
                    self.assertEqual(found[name]["value"], "[redacted]")
                    self.assertFalse(found[name]["truncated"])
                    self.assertEqual(found["max_connections"]["value"], "151")
        # MariaDB's catalog also carries the default and enum values.
        for name in ("wsrep_sst_auth", "report_password", "file_key_management_filekey", "s3_secret_key"):
            with self.subTest(engine="mariadb", name=name):
                secret = "maria-%s-secret" % name
                catalog = [[name.upper(), secret, "CONFIG", secret, "GLOBAL", "VARCHAR", None, None, None,
                            secret, "NO", None, "comment", "REQUIRED"]]
                report = parameters.compute_parameters("mariadb", (11, 4, 13), catalog_rows=catalog)
                self.assertNotIn(secret, json.dumps(report))
                item = report["items"][0]
                self.assertEqual((item["value"], item["default"], item["values"]), ("[redacted]", "[redacted]", None))

    def test_single_variable_read_redacts_each_pre_existing_secret_variable(self):
        for name in PRE_EXISTING_SECRETS:
            with self.subTest(name=name):
                info = {"name": name, "engine": "mysql", "flavor": "mysql", "server_product": "MySQL",
                        "server_version": "8.4.11", "version_tuple": (8, 4, 11), "in_global": True,
                        "in_session": True, "global_value": "g-private", "session_value": "s-private",
                        "catalog": None, "info": None, "metadata": None, "persisted_globals_load": "ON",
                        "persisted": {"present": True, "value": "p-private", "read": True}, "notes": []}
                report = variables.describe(info)
                encoded = json.dumps(report)
                for secret in ("g-private", "s-private", "p-private"):
                    self.assertNotIn(secret, encoded)
                self.assertEqual(report["global_value"], "[redacted]")
                self.assertEqual(report["persisted"]["value"], "[redacted]")
                self.assertTrue(dbops.sensitive(name))
        ordinary = variables.describe(dict(info, name="max_connections", global_value="151",
                                           persisted={"present": True, "value": "200", "read": True}))
        self.assertEqual((ordinary["global_value"], ordinary["persisted"]["value"]), ("151", "200"))


# ---------------------------------------------------------------------------
# Lifecycle: the protected schema on every dump, restore and upgrade path
# ---------------------------------------------------------------------------


class TestLifecycleVillageSchema(unittest.TestCase):
    setUp = _lifecycle_suite.LifecycleTests.setUp
    stop_detect = _lifecycle_suite.LifecycleTests.stop_detect
    close_jobs = _lifecycle_suite.LifecycleTests.close_jobs
    audit = _lifecycle_suite.LifecycleTests.audit
    native_tools = _lifecycle_suite.LifecycleTests.native_tools
    calls = _lifecycle_suite.LifecycleTests.calls
    make_dump = _lifecycle_suite.LifecycleTests.make_dump

    def fresh_output(self, label):
        # prepare_dump refuses a non-empty directory; each plan gets its own.
        self.outputs = getattr(self, "outputs", 0) + 1
        return "%s-%s-%d" % (self.output, label, self.outputs)

    def full_instance_dump(self, version, shell):
        # ReplayConnection has no cached handshake identity, like a fresh
        # LifecycleConnection: the flavor must come from read_server.
        conn = _lifecycle_suite.ReplayConnection(_lifecycle_suite.server_variables(version=version))
        self.assertIsNone(getattr(conn, "_version_cache", None))
        tools = self.tools if shell else self.native_tools()
        output = self.fresh_output("shell" if shell else "native")
        with mock.patch.object(lc, "detect_tools", return_value=tools):
            plan = lc.prepare_dump(conn, _lifecycle_suite.CONNECTION, output, tools=tools, prefer_shell=shell)
            prepared = len(conn.calls)
            job = lc.dump_job(_lifecycle_suite.CONNECTION, plan, confirmed=True, typed_target=plan["target"],
                              audit=self.audit("dump"), factory=lambda **kw: conn, start=False)
            result = job.run()
        self.assertEqual(result["state"], "done", result)
        catalog_sql = [sql for sql, _flags in conn.calls if "information_schema.TABLES" in sql
                       or "information_schema.SCHEMATA" in sql]
        argv = [call["argv"] for call in self.calls() if call["name"] == "mysqldump"]
        (self.bin / "calls.jsonl").unlink()
        return plan, catalog_sql, conn.calls[:prepared], argv

    def test_full_instance_dump_metadata_reads_exclude_the_protected_schema(self):
        """prepare_dump, _dump_size_estimate and _perform_dump pass the
        identified flavor; a fresh connection has no cached identity to fall
        back on. VillageSQL uses SQL dumps, which keep mysql and sys."""
        for shell in (False, True):
            with self.subTest(shell=shell):
                plan, catalog_sql, prepared, _argv = self.full_instance_dump(VERSION, shell)
                self.assertEqual(plan["server"]["flavor"], "villagesql")
                self.assertEqual(plan["tool"]["name"], "mysqldump")
                names = "'performance_schema','information_schema','villagesql'"
                self.assertGreaterEqual(len(catalog_sql), 4)
                for sql in catalog_sql:
                    self.assertIn("NOT IN (" + names + ")", sql)
                self.assertTrue(any("information_schema.TABLES" in sql for sql, _flags in prepared))
        for shell in (False, True):
            with self.subTest(product="MySQL", shell=shell):
                _plan, catalog_sql, _prepared, _argv = self.full_instance_dump("8.4.11", shell)
                for sql in catalog_sql:
                    self.assertNotIn("villagesql", sql)
                    self.assertIn("'performance_schema','information_schema')", sql)

    def test_mysql_full_instance_dump_commands_stay_unchanged(self):
        plan, _catalog, _prepared, argv = self.full_instance_dump("8.4.11", False)
        self.assertEqual(argv[0][-1], "--all-databases")
        plan, _catalog, _prepared, _argv = self.full_instance_dump("8.4.11", True)
        self.assertIn("util.dump_instance(", plan["commands"][0])
        self.assertNotIn("excludeSchemas", plan["commands"][0])

    def test_full_instance_dump_keeps_all_databases_and_uses_mysqldump(self):
        """D1: SQL instance dumps keep their existing all-databases scope."""
        # Live 2026-11-27: mysqldump 8.4.8 on villagesql-8.4 completed with
        # CREATE DATABASE IF NOT EXISTS / USE villagesql, no hidden tables
        # and no error 3554. Shell 9.6.0 instead rejected vsql_vector.SVECTOR.
        # The empty database declaration is harmless on restore.
        for prefer_shell in (False, True):
            with self.subTest(prefer_shell=prefer_shell):
                plan, _catalog, _prepared, argv = self.full_instance_dump(VERSION, prefer_shell)
                self.assertEqual(plan["tool"]["name"], "mysqldump")
                self.assertEqual(len(argv), 1)
                self.assertEqual(argv[0][-1], "--all-databases")
                self.assertFalse(any(arg.startswith("--ignore") for arg in argv[0]))
                self.assertIn("--all-databases", plan["commands"][0])

    def full_instance_restore(self, version):
        self.output = self.fresh_output("restore")
        self.make_dump()
        path = Path(self.output, lc.MANIFEST)
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["scope"] = {"schemas": [], "tables": []}
        path.write_text(json.dumps(manifest), encoding="utf-8")
        target = _lifecycle_suite.ReplayConnection(
            _lifecycle_suite.server_variables(identity="recipient", version=version), catalog=[],
            restored_marker=str(self.bin / "restored"))
        plan = lc.prepare_restore(target, _lifecycle_suite.CONNECTION, self.output, tools=self.native_tools(),
                                  existing_objects="drop")
        catalog_sql = [sql for sql, _flags in target.calls if "FROM information_schema.TABLES WHERE" in sql]
        return plan, catalog_sql

    def test_full_instance_restore_identifies_the_target_product(self):
        plan, catalog_sql = self.full_instance_restore(VERSION)
        self.assertEqual(plan["server"]["flavor"], "villagesql")
        self.assertEqual(len(catalog_sql), 1)
        plan, catalog_sql = self.full_instance_restore("8.4.11")
        self.assertTrue(catalog_sql[0].endswith("TABLE_SCHEMA NOT IN ('performance_schema','information_schema')"))

    def test_full_instance_restore_existing_objects_exclude_the_protected_schema(self):
        """D2: fresh restore connections use the target flavor from
        read_server, including when they have no cached handshake identity."""
        _plan, catalog_sql = self.full_instance_restore(VERSION)
        self.assertIn("'villagesql'", catalog_sql[0])

    def test_native_upgrade_checks_skip_the_protected_schema_only_on_villagesql(self):
        class Conn(object):
            def __init__(self):
                self.calls = []

            def run(self, sql, extra_flags=None, **kwargs):
                self.calls.append(sql)
                return ""

        for flavor, version, names in (
                ("villagesql", VERSION, "'mysql','sys','performance_schema','information_schema','villagesql'"),
                ("mysql", "8.4.11", "'mysql','sys','performance_schema','information_schema'"),
                ("percona", "8.4.11-11", "'mysql','sys','performance_schema','information_schema'")):
            with self.subTest(flavor=flavor):
                conn = Conn()
                checks = lc._native_upgrade(conn, {"version": version, "flavor": flavor}, (9, 7, 0))
                self.assertTrue(checks)
                filtered = [sql for sql in conn.calls if "NOT IN (" in sql]
                self.assertGreaterEqual(len(filtered), 3)
                for sql in filtered:
                    self.assertEqual(set(re.findall(r"NOT IN \(([^)]*)\)", sql)), {names})


if __name__ == "__main__":
    unittest.main()
