"""Percona Server for MySQL: identity and the evidence Percona adds.

Percona Server and Percona XtraDB Cluster are MySQL builds (engine ``mysql``,
flavor ``percona``): every MySQL rule applies, only the product name and the
features Percona adds differ. The replays come from the GA matrix's
percona/percona-server 8.0.46-37.1 and 8.4.11-11.1 containers
(scripts/ga-matrix.sh: percona-8.0 runs the thread pool and the slow log,
percona-8.4 runs userstat=ON), recorded with scripts/record-perf-fixtures.py.
The ``-partitioned`` and ``-psoff`` replays come from throwaway
percona/percona-server:8.4.11-11.1 containers with userstat=ON (Performance
Schema on, and off) and mariadb-11.4-partitioned from a throwaway
mariadb:11.4.13 one, over a ``review`` schema of partitioned and plain tables
where a range read through ``parted_pk.kw`` was missing from user statistics.
"""
import json
import os
import re
import unittest

from _perf_replay import ReplayConn, assert_read_only, fixture
from myflames import config_review, health, indexes, perf_common, schema_health, server_info
from myflames.perf_common import Server

PERCONA_80 = "Percona Server (GPL), Release 37, Revision 39e2b60e"
PERCONA_84 = "Percona Server (GPL), Release 11, Revision 57878ff8"
PXC_84 = "Percona XtraDB Cluster (GPL), Release rel11, Revision 33f7510, WSREP version 26.1.4.3"
HEALTH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "health")


def replay(view, server, edit=None):
    data = fixture(view, server)
    return ReplayConn(data["calls"], edit=edit), data


class TestIdentity(unittest.TestCase):
    def test_comments_read_live_name_the_product(self):
        cases = [
            ("8.0.46-37", PERCONA_80, ("mysql", "percona", "Percona Server")),
            ("8.4.11-11", PERCONA_84, ("mysql", "percona", "Percona Server")),
            ("8.4.11-11.1", PXC_84, ("mysql", "percona", "Percona XtraDB Cluster")),
            ("8.4.11", "MySQL Community Server - GPL", ("mysql", "mysql", "MySQL")),
            ("11.8.9-MariaDB-ubu2404-log", "mariadb.org binary distribution", ("mariadb", "mariadb", "MariaDB")),
            ("8.4.11", "", ("mysql", "mysql", "MySQL")),
        ]
        for version, comment, expected in cases:
            with self.subTest(version=version, comment=comment):
                found = server_info.product_identity(version, comment)
                self.assertEqual((found["engine"], found["flavor"], found["product"]), expected)
                # The engine decision is unchanged: Percona is MySQL.
                self.assertEqual(server_info.detect_flavor(version, comment), expected[0])

    def test_a_version_alone_names_percona_by_its_release_suffix(self):
        for version, product in (("8.4.11-11", "Percona Server"), ("8.0.46-37", "Percona Server"),
                                 ("8.4.11-11.1", "Percona Server"), ("8.4.11", "MySQL"),
                                 ("9.7.2-commercial", "MySQL"), ("8.4.11-debug", "MySQL"),
                                 ("11.8.9-MariaDB-log", "MariaDB")):
            with self.subTest(version=version):
                self.assertEqual(server_info.identity_from_version(version)["product"], product)

    def test_percona_variables_are_marked_only_on_percona(self):
        self.assertTrue(server_info.percona_variable("thread_pool_size", "percona", (8, 4, 11)))
        self.assertTrue(server_info.percona_variable("USERSTAT", "percona", (8, 0, 46)))
        # MariaDB's thread pool and MySQL Enterprise's share the name: never marked there.
        self.assertFalse(server_info.percona_variable("thread_pool_size", "mariadb", (11, 8, 9)))
        self.assertFalse(server_info.percona_variable("thread_pool_size", "mysql", (8, 4, 11)))
        # MySQL defines innodb_numa_interleave itself; table_open_cache_triggers from 9.x.
        self.assertFalse(server_info.percona_variable("innodb_numa_interleave", "percona", (8, 4, 11)))
        self.assertTrue(server_info.percona_variable("table_open_cache_triggers", "percona", (8, 4, 11)))
        self.assertFalse(server_info.percona_variable("table_open_cache_triggers", "percona", (9, 7, 2)))
        self.assertFalse(server_info.percona_variable("innodb_buffer_pool_size", "percona", (8, 4, 11)))

    def test_upstream_mysql_catalog_has_none_of_the_marked_names(self):
        from myflames.mysql_sysvars import SYSVARS
        overlap = sorted(n for n in server_info.PERCONA_VARIABLES if n in SYSVARS)
        self.assertEqual(overlap, ["table_open_cache_triggers"])  # MySQL 9.x upstreamed it

    def test_supported_floor_note_names_percona(self):
        note = server_info.unsupported_version_note("8.0.46-37", PERCONA_80)
        self.assertIn("this server is Percona Server 8.0.46-37", note)
        self.assertIn("MySQL 8.4 or later", note)
        self.assertIsNone(server_info.unsupported_version_note("8.4.11-11", PERCONA_84))
        self.assertIn("this server is MySQL 8.0.46,", server_info.unsupported_version_note("8.0.46", ""))

    def test_labels_fall_back_to_the_engine_for_older_reports(self):
        self.assertEqual(server_info.server_label({"engine": "mysql", "server_version": "8.4.11"}), "MySQL 8.4.11")
        self.assertEqual(server_info.server_label({"engine": "mysql", "server_product": "Percona Server",
                                                   "server_version": "8.4.11-11"}), "Percona Server 8.4.11-11")
        self.assertEqual(server_info.product_name({"engine": "mariadb"}), "MariaDB")

    def test_every_view_envelope_carries_flavor_and_product(self):
        server = Server({"version": "8.4.11-11", "version_comment": PERCONA_84, "performance_schema": "ON"}, {})
        self.assertEqual((server.engine, server.flavor, server.product, server.label),
                         ("mysql", "percona", "Percona Server", "Percona Server"))
        report = perf_common.base_report("x-1.0", server)
        self.assertEqual((report["engine"], report["flavor"], report["server_product"]),
                         ("mysql", "percona", "Percona Server"))
        self.assertEqual(server.describe()["product"], "Percona Server")
        self.assertEqual(perf_common.identity_fields("mariadb", "11.8.9-MariaDB"),
                         {"flavor": "mariadb", "server_product": "MariaDB"})


class TestVersionSupport(unittest.TestCase):
    def test_percona_never_inherits_mysql_lifecycle_dates(self):
        import datetime
        from myflames import server_checks
        server = Server({"version": "8.0.46-37", "version_comment": PERCONA_80}, {})
        check = server_checks._version_support(server, datetime.date(2026, 10, 21))
        self.assertEqual(check["status"], "unknown")
        self.assertIn("Percona Server 8.0.46-37: Percona publishes its own lifecycle", check["why"])
        self.assertIn("for reference, MySQL 8.0 maintenance ends", check["why"])
        mysql = server_checks._version_support(Server({"version": "8.0.46"}, {}), datetime.date(2026, 10, 21))
        self.assertEqual(mysql["status"], "problem")
        self.assertIn("MySQL 8.0.46: past end of life", mysql["why"])


class TestIndexes(unittest.TestCase):
    maxDiff = None

    def read(self, edit=None):
        conn, data = replay("indexes", "percona-8.4", edit=edit)
        report = indexes.index_report(conn, **data["args"])
        assert_read_only(self, conn, indexes.INDEXES_TAG)
        json.dumps(report, allow_nan=False)
        return report, conn

    def test_userstat_is_evidence_beside_performance_schema(self):
        report, conn = self.read()
        self.assertEqual((report["flavor"], report["server_product"]), ("percona", "Percona Server"))
        self.assertEqual(report["source"], "performance_schema")
        self.assertEqual(report["userstat"], {"enabled": True, "read": True, "role": "evidence"})
        self.assertTrue(any("INDEX_STATISTICS" in sql for sql, _ in conn.calls))
        candidates = [u for u in report["unused"] if u["verdict"] == "candidate"]
        self.assertTrue(candidates)
        for u in candidates:
            # Live: INDEX_STATISTICS has no row for them, so zero reads in its window too.
            self.assertEqual(u["userstat_rows_read"], 0, u)
            self.assertIn("COUNT_STAR = 0", u["why"])
        fk = [u for u in report["unused"] if u["keep_reason"] == "foreign_key"]
        self.assertEqual([u["index"] for u in fk], ["fk_invoice_account"])
        self.assertTrue(any("userstat=ON" in n for n in report["notes"]))
        self.assertTrue(indexes.format_text(report).startswith("Percona Server 8.4.11-11"))

    def test_an_index_userstat_saw_read_is_kept(self):
        target = self.read()[0]
        first = next(u for u in target["unused"] if u["verdict"] == "candidate")

        def edit(sql, text, n):
            if "information_schema.INDEX_STATISTICS" in sql:
                return text + "%s\t%s\t%s\t42\n" % (first["schema"], first["table"], first["index"])
            return text
        report, _ = self.read(edit)
        entry = next(u for u in report["unused"] if u["index"] == first["index"] and u["table"] == first["table"])
        self.assertEqual((entry["verdict"], entry["keep_reason"], entry["userstat_rows_read"]),
                         ("keep", "userstat", 42))
        self.assertNotIn("drop_sql", entry)
        self.assertIn("INDEX_STATISTICS", entry["why"])
        self.assertIn("42 rows read", entry["why"])
        self.assertEqual(report["summary"]["keep_userstat"], 1)
        self.assertRegex(indexes.format_text(report), r"%s +%s +.* keep: read per user statistics"
                         % (re.escape(first["table"]), re.escape(first["index"])))
        self.assertEqual(report["summary"]["unused_candidates"], target["summary"]["unused_candidates"] - 1)

    def test_userstat_is_the_source_when_performance_schema_is_off(self):
        server = Server({"version": "8.4.11-11", "version_comment": PERCONA_84, "performance_schema": "OFF",
                         "userstat": "ON"}, {"Uptime": "100"})
        stats = [["shop", "t", "PRIMARY", "1", "1", "id", None, "A", "BTREE", "YES", None],
                 ["shop", "t", "k_a", "1", "1", "a", None, "A", "BTREE", "YES", None],
                 ["shop", "t", "k_b", "1", "1", "b", None, "A", "BTREE", "YES", None]]
        stats[0][3] = "0"
        raw = {"statistics": stats, "foreign_keys": [], "usage": None, "instrument": None, "consumers": None,
               "actors": None, "objects": None, "index_stats": [["shop", "t", "k_a", "10"]],
               "table_stats": [["shop", "t", "30"]], "partitioned": []}
        report = indexes.compute_indexes(server, raw, ["shop"])
        self.assertEqual(report["source"], "userstat")
        self.assertEqual(report["userstat"]["role"], "source")
        self.assertEqual([u["index"] for u in report["unused"]], ["k_b"])
        self.assertTrue(any("Percona Server user statistics" in n for n in report["notes"]))

    def test_partitioned_tables_are_no_data_when_userstat_is_the_source(self):
        conn, data = replay("indexes", "percona-8.4-psoff")
        report = indexes.index_report(conn, **data["args"])
        assert_read_only(self, conn, indexes.INDEXES_TAG)
        self.assertEqual(report["source"], "userstat")
        self.assertTrue(any("CREATE_OPTIONS LIKE '%partitioned%'" in sql for sql, _ in conn.calls))
        no_data = {n["table"]: n["reason"] for n in report["no_data"]}
        self.assertEqual((no_data["parted_pk"], no_data["parted_nopk"]), ("partitioned", "partitioned"))
        # Live: a range read through parted_pk.kw is missing from INDEX_STATISTICS; it is never called unused.
        self.assertFalse([u for u in report["unused"] if u["table"].startswith("parted")])
        self.assertEqual([(u["table"], u["index"]) for u in report["unused"]], [("plain_pk", "kz")])
        self.assertTrue(any("indexes of the 2 partitioned tables in scope are under No data" in n
                            for n in report["notes"]))
        text = indexes.format_text(report)
        self.assertIn("usage from Percona Server user statistics (userstat)", text)
        self.assertIn("  partitioned, which Percona Server's user statistics do not count: review.parted_nopk, "
                      "review.parted_pk\n", text)
        self.assertNotIn("MariaDB", text)

    def test_partitioned_counts_are_unknown_beside_performance_schema(self):
        def edit(sql, text, n):
            # Give parted_pk Performance Schema activity so its indexes are judged.
            if "table_io_waits_summary_by_index_usage" in sql:
                return re.sub(r"review\tparted_pk\tPRIMARY\t0\t0\t0", "review\tparted_pk\tPRIMARY\t5\t5\t1000", text)
            return text
        conn, data = replay("indexes", "percona-8.4-partitioned", edit=edit)
        report = indexes.index_report(conn, **data["args"])
        unused = {(u["table"], u["index"]): u for u in report["unused"]}
        # Absent from INDEX_STATISTICS on a partitioned table: unknown, not zero.
        self.assertIsNone(unused[("parted_pk", "kz")]["userstat_rows_read"])
        self.assertIsNone(unused[("parted_pk", "kw")]["userstat_rows_read"])
        # A count above zero is still a read (live: 3 through parted_pk.kv).
        self.assertEqual((unused[("parted_pk", "kv")]["verdict"], unused[("parted_pk", "kv")]["userstat_rows_read"]),
                         ("keep", 3))
        self.assertEqual(unused[("plain_pk", "kz")]["userstat_rows_read"], 0)
        self.assertTrue(any("for the 2 partitioned tables in scope they can show that an index was read, never that "
                            "it was not" in n for n in report["notes"]))

    def test_partitioned_tables_unlisted_leave_userstat_unused_unknown(self):
        data = fixture("indexes", "percona-8.4-psoff")
        calls = [[c[0], None, "ERROR 1356 (HY000): refused"] if "CREATE_OPTIONS" in c[0] else c
                 for c in data["calls"]]
        report = indexes.index_report(ReplayConn(calls), **data["args"])
        self.assertEqual(report["unused"], [])
        self.assertTrue(report["no_data"])
        self.assertEqual({n["reason"] for n in report["no_data"]} - {"not_read"}, {"userstat_unknown"})
        self.assertTrue(any("could not list them" in n for n in report["notes"]))

    def test_mariadb_userstat_reads_no_partition_list(self):
        import test_indexes
        conn, data = test_indexes.replay("indexes", "mariadb-11.4-userstat")
        indexes.index_report(conn, **data["args"])
        self.assertFalse(any("CREATE_OPTIONS" in sql for sql, _ in conn.calls))

    def test_mysql_without_userstat_reads_no_userstat_table(self):
        import test_indexes
        conn, data = test_indexes.replay("indexes", "mysql-8.4")
        report = indexes.index_report(conn, **data["args"])
        self.assertFalse(any("_STATISTICS" in sql for sql, _ in conn.calls))
        self.assertEqual(report["userstat"], {"enabled": False, "read": False, "role": None})
        self.assertTrue(all(u["userstat_rows_read"] is None for u in report["unused"]))


class TestTables(unittest.TestCase):
    def test_rows_changed_weigh_tables_without_a_key(self):
        conn, data = replay("tables", "percona-8.4")
        report = schema_health.table_report(conn, **data["args"])
        self.assertEqual(report["flavor"], "percona")
        self.assertEqual(report["userstat"], {"enabled": True, "read": True})
        self.assertTrue(any("TABLE_STATISTICS" in sql for sql, _ in conn.calls))
        missing = {m["table"]: m for m in report["missing_primary_keys"]}
        no_pk = missing["no_pk_events"]
        # Live: the 3,000 seeded rows were inserted after userstat started.
        self.assertEqual(no_pk["rows_changed"], 3000)
        self.assertIn("3,000 rows changed (inserted, updated or deleted)", no_pk["why"])
        self.assertIn("TABLE_STATISTICS", no_pk["why"])
        # Inserts need no search on a replica: the count is write activity, not searches.
        self.assertIn("inserts need no search, and the count does not separate them", no_pk["why"])
        self.assertNotIn("Changed rows are the ones", no_pk["why"])
        rows = {t["table"]: t for t in report["tables"]}
        self.assertIn("rows_read", rows["churn"])
        self.assertGreater(rows["churn"]["rows_changed"], 0)
        self.assertTrue(schema_health.format_text(report).startswith("Percona Server 8.4.11-11"))

    def test_partitioned_tables_have_unknown_activity(self):
        conn, data = replay("tables", "percona-8.4-partitioned")
        report = schema_health.table_report(conn, **data["args"])
        rows = {t["table"]: t for t in report["tables"]}
        for name in ("parted_pk", "parted_nopk"):
            self.assertTrue(rows[name]["partitioned"])
            self.assertEqual((rows[name]["rows_read"], rows[name]["rows_changed"]), (None, None), name)
        self.assertEqual((rows["plain_nopk"]["rows_read"], rows["plain_nopk"]["rows_changed"]), (6, 4))
        missing = {m["table"]: m for m in report["missing_primary_keys"]}
        self.assertIsNone(missing["parted_nopk"]["rows_changed"])
        self.assertNotIn("TABLE_STATISTICS", missing["parted_nopk"]["why"])
        self.assertIn("4 rows changed", missing["plain_nopk"]["why"])
        self.assertTrue(any("rows read and changed are unknown for the 2 partitioned tables in scope" in n
                            for n in report["notes"]))
        json.dumps(report, allow_nan=False)

    def test_partitioned_table_absent_from_table_statistics_is_unknown_not_idle(self):
        def edit(sql, text, n):
            if "information_schema.TABLE_STATISTICS" in sql:
                return "".join(line + "\n" for line in text.splitlines() if "\tparted_nopk\t" not in line)
            return text
        conn, data = replay("tables", "percona-8.4-partitioned", edit=edit)
        report = schema_health.table_report(conn, **data["args"])
        entry = next(m for m in report["missing_primary_keys"] if m["table"] == "parted_nopk")
        self.assertEqual((entry["rows_read"], entry["rows_changed"]), (None, None))
        self.assertNotIn("no rows changed", entry["why"])
        self.assertNotIn("replicas had no", entry["why"])
        # A plain table absent from it had no changes in that window.
        def edit_plain(sql, text, n):
            if "information_schema.TABLE_STATISTICS" in sql:
                return "".join(line + "\n" for line in text.splitlines() if "\tplain_nopk\t" not in line)
            return text
        conn, data = replay("tables", "percona-8.4-partitioned", edit=edit_plain)
        report = schema_health.table_report(conn, **data["args"])
        entry = next(m for m in report["missing_primary_keys"] if m["table"] == "plain_nopk")
        self.assertEqual((entry["rows_read"], entry["rows_changed"]), (0, 0))
        self.assertIn("count no rows changed", entry["why"])

    def test_mariadb_partitioned_tables_have_unknown_activity(self):
        conn, data = replay("tables", "mariadb-11.4-partitioned")
        report = schema_health.table_report(conn, **data["args"])
        rows = {t["table"]: t for t in report["tables"]}
        # Live on MariaDB 11.4.13: a range read through parted_pk.kw left its ROWS_READ at 0.
        self.assertEqual((rows["parted_pk"]["rows_read"], rows["parted_pk"]["rows_changed"]), (None, None))
        self.assertEqual((rows["plain_pk"]["rows_read"], rows["plain_pk"]["rows_changed"]), (3, 3))
        self.assertTrue(any("unknown for the partitioned table in scope" in n for n in report["notes"]))

    def test_mysql_reads_no_table_statistics(self):
        import test_schema_health
        conn, data = test_schema_health.replay("mysql-8.4")
        report = schema_health.table_report(conn, **data["args"])
        self.assertFalse(any("TABLE_STATISTICS" in sql for sql, _ in conn.calls))
        self.assertEqual(report["userstat"], {"enabled": False, "read": False})
        self.assertTrue(all("rows_changed" not in m for m in report["missing_primary_keys"]))


class TestHealth(unittest.TestCase):
    def fixture(self, name):
        with open(os.path.join(HEALTH, name + ".json"), encoding="utf-8") as handle:
            return json.load(handle)

    def report(self, name):
        data = self.fixture(name)
        first, second = data["samples"]
        return health.compute_health(first, second, data["variables"], 2), data

    def test_thread_pool_numbers_replace_the_thread_cache(self):
        report, data = self.report("percona-8.0")
        self.assertEqual((report["flavor"], report["server_product"]), ("percona", "Percona Server"))
        self.assertEqual(data["variables"]["thread_handling"], "pool-of-threads")
        diag = next(d for d in report["diagnostics"] if d["key"] == "thread_cache")
        self.assertEqual((diag["status"], diag["state"], diag["title"]), ("info", "not_applicable", "Thread pool"))
        self.assertRegex(diag["headline"], r"^\d+ threads, \d+ idle$")
        self.assertIn("Percona Server's thread pool", diag["verdict"])
        keys = [n["key"] for n in diag["numbers"]]
        self.assertEqual(keys[:2], ["pool_threads", "pool_idle"])
        threads = next(n for n in diag["numbers"] if n["key"] == "pool_threads")["value"]
        self.assertEqual(threads, int(data["samples"][1]["status"]["Threadpool_threads"]))
        self.assertIn("thread_pool_size", [s["name"] for s in diag["settings"]])
        self.assertTrue(health.format_text(report).startswith("Percona Server 8.0.46-37"))

    def test_percona_84_queue_counters_when_the_pool_is_off(self):
        report, data = self.report("percona-8.4")
        self.assertEqual(data["variables"]["thread_handling"], "one-thread-per-connection")
        diag = next(d for d in report["diagnostics"] if d["key"] == "thread_cache")
        # One thread per connection: the thread cache rule applies as on MySQL.
        self.assertNotEqual(diag["state"], "not_applicable")
        self.assertIn("Threadpool_requests_waiting_in_queue", data["samples"][1]["status"])

    def test_mariadb_pool_names_mariadb(self):
        data = self.fixture("percona-8.0")
        variables = dict(data["variables"], version="11.8.9-MariaDB", version_comment="mariadb.org binary distribution")
        first, second = data["samples"]
        report = health.compute_health(first, second, variables, 2)
        diag = next(d for d in report["diagnostics"] if d["key"] == "thread_cache")
        self.assertIn("MariaDB's thread pool", diag["verdict"])


class TestConfig(unittest.TestCase):
    def read(self, server, edit=None):
        conn, data = replay("config", server, edit=edit)
        report = config_review.config_report(conn, **data["args"])
        return report, conn

    def test_percona_variables_are_marked_in_parameters(self):
        report, conn = self.read("percona-8.4")
        self.assertEqual(report["flavor"], "percona")
        params = report["parameters"]
        self.assertEqual(params["flavor"], "percona")
        vendor = {i["name"]: i["vendor"] for i in params["items"]}
        for name in ("thread_pool_size", "userstat", "log_slow_rate_limit", "kill_idle_transaction"):
            self.assertEqual(vendor[name], "percona", name)
        for name in ("innodb_buffer_pool_size", "max_connections", "innodb_numa_interleave"):
            if name in vendor:
                self.assertIsNone(vendor[name], name)
        marked = sorted(n for n, v in vendor.items() if v)
        self.assertEqual(marked, fixture("config", "percona-8.4")["report"]["parameters"]["vendor_items"])
        self.assertTrue(set(marked) <= server_info.PERCONA_VARIABLES)
        # Percona's slow log sampling is read in its own statement, on Percona only.
        self.assertTrue(any("log_slow_rate_limit" in sql for sql, _ in conn.calls))

    def test_mysql_reads_no_percona_variables(self):
        conn, data = replay("config", "mysql-8.4")
        report = config_review.config_report(conn, **data["args"])
        self.assertEqual(report["flavor"], "mysql")
        self.assertFalse(any("log_slow_rate_limit" in sql for sql, _ in conn.calls))

    def test_a_sampled_slow_log_is_said(self):
        report, _ = self.read("percona-8.0")
        slow = next(c for c in report["checks"] if c["key"] == "slow_log")
        self.assertEqual(slow["status"], "ok")  # live: log_slow_rate_limit=1

        def edit(sql, text, n):
            if "log_slow_rate_limit" in sql and "slow_query_log_always_write_time" in sql:
                return re.sub(r"log_slow_rate_limit\t1\n", "log_slow_rate_limit\t100\n",
                              re.sub(r"log_slow_rate_type\t\w+", "log_slow_rate_type\tquery", text))
            return text
        report, _ = self.read("percona-8.0", edit)
        slow = next(c for c in report["checks"] if c["key"] == "slow_log")
        self.assertEqual(slow["status"], "info")
        self.assertIn("On, logging statements over", slow["why"])
        self.assertIn("but log_slow_rate_limit=100 logs only 1 in 100 queries", slow["why"])
        self.assertIn("about 1 in 100 of the sampled statements is written", slow["caveat"])
        self.assertIn("Statements that run 10 s or longer (slow_query_log_always_write_time) are always written",
                      slow["caveat"])
        # Live long_query_time is 0.2 s: Percona logs every statement of 1 s or more then.
        self.assertIn("statements that run 1 s or longer are always written too", slow["caveat"])
        self.assertIn("log_slow_rate_limit", [s["name"] for s in slow["settings"]])

    def slow(self, long_query_time, always, limit="100", rate_type="query", no_index="OFF", errors=""):
        def edit(sql, text, n):
            if "log_slow_rate_limit" in sql and "slow_query_log_always_write_time" in sql:
                text = re.sub(r"log_queries_not_using_indexes\t\w+", "log_queries_not_using_indexes\t" + no_index,
                              text)
                text = re.sub(r"log_query_errors\t[^\n]*", "log_query_errors\t" + errors, text)
                text = re.sub(r"log_slow_rate_limit\t\d+", "log_slow_rate_limit\t" + limit, text)
                text = re.sub(r"log_slow_rate_type\t\w+", "log_slow_rate_type\t" + rate_type, text)
                return re.sub(r"slow_query_log_always_write_time\t[\d.]+",
                              "slow_query_log_always_write_time\t" + always, text)
            if "long_query_time\t" in text and "'long_query_time'" in sql:
                return re.sub(r"long_query_time\t[\d.]+", "long_query_time\t" + long_query_time, text)
            return text
        report, _ = self.read("percona-8.0", edit)
        return next(c for c in report["checks"] if c["key"] == "slow_log")

    def test_sampling_is_said_above_a_10_s_threshold(self):
        # Statements between 10 and 100 s are sampled (Percona sql/log.cc log_slow_applicable).
        slow = self.slow("10.000000", "100.000000")
        self.assertEqual(slow["status"], "info")
        self.assertIn("On, but only statements over 10", slow["why"])
        self.assertIn("and log_slow_rate_limit=100 logs only 1 in 100 queries", slow["why"])
        self.assertIn("Statements that run 100 s or longer", slow["caveat"])
        self.assertNotIn("1 s or longer", slow["caveat"])

    def test_no_sampling_when_always_write_time_is_at_or_below_the_threshold(self):
        for long_query_time, always in (("10.000000", "10.000000"), ("12.000000", "5.000000"),
                                        ("0.200000", "0.000000")):
            with self.subTest(long_query_time=long_query_time, always=always):
                slow = self.slow(long_query_time, always)
                self.assertNotIn("logs only 1 in", slow["why"])
                self.assertNotIn("log_slow_rate_limit", [s["name"] for s in slow["settings"]])
        self.assertEqual(self.slow("0.200000", "0.000000")["status"], "ok")
        self.assertEqual(self.slow("10.000000", "10.000000")["status"], "info")

    def test_session_sampling_names_sessions(self):
        slow = self.slow("0.200000", "10.000000", limit="20", rate_type="session")
        # Percona keeps the connections whose ID is a multiple of the limit: no share of statements.
        self.assertIn("logs only the sessions whose connection ID is a multiple of 20", slow["why"])
        self.assertIn("misses an unknown share", slow["caveat"])
        self.assertNotIn("1 in 20", slow["caveat"])

    def test_statements_added_without_the_threshold_are_still_sampled(self):
        # Live: with long_query_time 10, slow_query_log_always_write_time 10 and log_slow_rate_limit 1000,
        # none of 6 statements log_queries_not_using_indexes added was logged.
        slow = self.slow("10.000000", "10.000000", limit="1000", no_index="ON")
        self.assertEqual(slow["status"], "info")
        self.assertIn("On, but only statements over 10", slow["why"])
        self.assertIn("log_slow_rate_limit=1,000 samples the statements log_queries_not_using_indexes adds: only "
                      "1 in 1,000 queries", slow["why"])
        self.assertIn("log_queries_not_using_indexes", [x["name"] for x in slow["settings"]])
        slow = self.slow("0.200000", "0.100000", errors="ALL")
        self.assertIn("samples the statements log_query_errors adds", slow["why"])
        # 0 never samples anything, whatever adds the statement.
        self.assertNotIn("log_slow_rate_limit=", self.slow("10.000000", "0.000000", no_index="ON",
                                                           errors="ALL")["why"])


if __name__ == "__main__":
    unittest.main()
