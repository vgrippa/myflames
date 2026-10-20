"""
Unit tests for :mod:`myflames.collectors`.

The collector functions talk to a ``MySQLConnection``; these tests use a
minimal stub connection that returns canned rows. That keeps the tests fast
and database-free while still exercising the real parsing and query
assembly paths.
"""
import os
import sys
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(TEST_DIR))

from myflames.collectors import (
    extract_table_names,
    parse_show_create_table,
    collect_schema,
    collect_stats,
    collect_session_variables,
    ADVISOR_VARIABLES,
)


class StubConn:
    """Tiny stand-in for MySQLConnection used throughout the tests."""

    def __init__(self, responses):
        # responses: list of (predicate, rows_or_raises). Each SQL maps to
        # the first predicate that matches it.
        self.responses = responses
        self.calls = []

    def _match(self, sql):
        for pred, rows in self.responses:
            if pred(sql):
                return rows
        raise AssertionError("No stub response for SQL: " + sql)

    def query_rows(self, sql):
        self.calls.append(sql)
        rows = self._match(sql)
        if isinstance(rows, Exception):
            raise rows
        return rows

    def query_kv(self, sql):
        rows = self.query_rows(sql)
        return {r[0]: r[1] for r in rows if len(r) >= 2}

    def query_dicts(self, sql, columns):
        rows = self.query_rows(sql)
        out = []
        for row in rows:
            d = {}
            for i, col in enumerate(columns):
                d[col] = row[i] if i < len(row) else ""
            out.append(d)
        return out


# ---------------------------------------------------------------------------
# extract_table_names
# ---------------------------------------------------------------------------

class TestExtractTableNames(unittest.TestCase):

    def test_simple_select(self):
        self.assertEqual(
            extract_table_names("SELECT * FROM users WHERE id = 1"),
            ["users"],
        )

    def test_schema_qualified(self):
        self.assertEqual(
            extract_table_names("SELECT * FROM shop.orders"),
            ["shop.orders"],
        )

    def test_backtick_identifier(self):
        self.assertEqual(
            extract_table_names("SELECT * FROM `my-db`.`weird name`"),
            ["my-db.weird name"],
        )

    def test_multiple_joins(self):
        sql = """
            SELECT *
            FROM users u
            JOIN orders o ON o.user_id = u.id
            LEFT JOIN order_items i ON i.order_id = o.id
            WHERE u.country = 'US'
        """
        tables = extract_table_names(sql)
        self.assertIn("users", tables)
        self.assertIn("orders", tables)
        self.assertIn("order_items", tables)

    def test_default_schema_applied(self):
        tables = extract_table_names("SELECT 1 FROM t1", default_schema="app")
        self.assertEqual(tables, ["app.t1"])

    def test_literal_from_inside_string_ignored(self):
        # 'FROM' inside a string literal must not be parsed as a table ref.
        tables = extract_table_names(
            "SELECT 'FROM nowhere' FROM real_table WHERE x = 1"
        )
        self.assertEqual(tables, ["real_table"])

    def test_comments_ignored(self):
        sql = "SELECT * /* FROM fake_table */ FROM real_table -- FROM also_fake"
        tables = extract_table_names(sql)
        self.assertEqual(tables, ["real_table"])

    def test_derived_subquery_skipped(self):
        sql = "SELECT * FROM (SELECT id FROM inner_table) t"
        tables = extract_table_names(sql)
        self.assertEqual(tables, ["inner_table"])  # outer paren skipped

    def test_deduplication_preserves_order(self):
        sql = "SELECT * FROM users, orders, users o2"
        tables = extract_table_names(sql)
        self.assertEqual(tables, ["users", "orders"])  # no duplicates

    def test_synthetic_derived_name_skipped(self):
        sql = "SELECT * FROM <derived2>"
        self.assertEqual(extract_table_names(sql), [])

    def test_empty_sql(self):
        self.assertEqual(extract_table_names(""), [])
        self.assertEqual(extract_table_names(None), [])


# ---------------------------------------------------------------------------
# parse_show_create_table
# ---------------------------------------------------------------------------

class TestParseShowCreateTable(unittest.TestCase):

    DDL = """CREATE TABLE `orders` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `status` varchar(20) NOT NULL DEFAULT 'pending',
  `total` decimal(10,2) DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_user` (`user_id`),
  KEY `idx_status` (`status`,`created_at`),
  UNIQUE KEY `uniq_ref` (`id`,`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci"""

    def test_table_name(self):
        parsed = parse_show_create_table(self.DDL)
        self.assertEqual(parsed["table_name"], "orders")

    def test_engine_and_charset(self):
        parsed = parse_show_create_table(self.DDL)
        self.assertEqual(parsed["engine"], "InnoDB")
        self.assertEqual(parsed["charset"], "utf8mb4")

    def test_columns_parsed(self):
        parsed = parse_show_create_table(self.DDL)
        cols = [c["name"] for c in parsed["columns"]]
        self.assertEqual(cols, ["id", "user_id", "status", "total", "created_at"])
        self.assertEqual(parsed["columns"][0]["type"], "int")
        self.assertEqual(parsed["columns"][2]["type"], "varchar(20)")

    def test_indexes_parsed(self):
        parsed = parse_show_create_table(self.DDL)
        names = [i["name"] for i in parsed["indexes"]]
        self.assertIn(None, names)  # PRIMARY has no name
        self.assertIn("idx_user", names)
        self.assertIn("idx_status", names)
        self.assertIn("uniq_ref", names)

    def test_multi_column_index(self):
        parsed = parse_show_create_table(self.DDL)
        status_idx = next(i for i in parsed["indexes"] if i["name"] == "idx_status")
        self.assertEqual(status_idx["columns"], ["status", "created_at"])

    def test_unique_key_kind(self):
        parsed = parse_show_create_table(self.DDL)
        uniq = next(i for i in parsed["indexes"] if i["name"] == "uniq_ref")
        self.assertIn("UNIQUE", uniq["kind"])

    def test_empty_ddl_graceful(self):
        parsed = parse_show_create_table("")
        self.assertIsNone(parsed["table_name"])
        self.assertEqual(parsed["columns"], [])
        self.assertEqual(parsed["indexes"], [])

    def test_mariadb_ddl(self):
        """MariaDB's SHOW CREATE TABLE output is very close to MySQL's."""
        ddl = (
            "CREATE TABLE `t1` (\n"
            "  `id` int(11) NOT NULL,\n"
            "  `a` int(11) DEFAULT NULL,\n"
            "  PRIMARY KEY (`id`),\n"
            "  KEY `idx_a` (`a`)\n"
            ") ENGINE=Aria DEFAULT CHARSET=utf8"
        )
        parsed = parse_show_create_table(ddl)
        self.assertEqual(parsed["engine"], "Aria")
        self.assertEqual(len(parsed["columns"]), 2)
        self.assertEqual(len(parsed["indexes"]), 2)


# ---------------------------------------------------------------------------
# collect_schema
# ---------------------------------------------------------------------------

class TestCollectSchema(unittest.TestCase):

    def test_collects_single_table(self):
        ddl = "CREATE TABLE `t1` (\n  `id` int NOT NULL,\n  PRIMARY KEY (`id`)\n) ENGINE=InnoDB"
        conn = StubConn([
            (lambda sql: sql.startswith("SHOW CREATE TABLE"), [["t1", ddl]]),
        ])
        schema = collect_schema(conn, ["t1"])
        self.assertIn("t1", schema)
        self.assertEqual(schema["t1"]["engine"], "InnoDB")

    def test_schema_qualified_quoted(self):
        ddl = "CREATE TABLE `orders` (\n  `id` int NOT NULL,\n  PRIMARY KEY (`id`)\n) ENGINE=InnoDB"
        conn = StubConn([
            (lambda sql: "shop" in sql and "orders" in sql, [["orders", ddl]]),
        ])
        schema = collect_schema(conn, ["shop.orders"])
        self.assertIn("shop.orders", schema)
        # The generated SQL must quote both halves
        self.assertTrue(any("`shop`.`orders`" in c for c in conn.calls))

    def test_skips_tables_with_errors(self):
        from myflames.connector import ConnectorError
        ddl = "CREATE TABLE `t1` (\n  `id` int NOT NULL\n) ENGINE=InnoDB"
        conn = StubConn([
            (lambda sql: "t1" in sql, [["t1", ddl]]),
            (lambda sql: "t_missing" in sql, ConnectorError("no perms")),
        ])
        schema = collect_schema(conn, ["t1", "t_missing"])
        self.assertIn("t1", schema)
        self.assertNotIn("t_missing", schema)


# ---------------------------------------------------------------------------
# collect_stats
# ---------------------------------------------------------------------------

class TestCollectStats(unittest.TestCase):

    def test_batches_800_tables_and_keeps_requested_order(self):
        """MySQL and MariaDB expose the same TABLES columns used here."""
        import re
        rows = [["db", "t%d" % i, str(i), "1024", "512", "0", "NULL", "InnoDB", "Dynamic"]
                for i in range(800)]

        class Conn(StubConn):
            def query_dicts(self, sql, columns):
                self.calls.append(sql)
                ids = [int(i) for i in re.findall(r"SELECT (\d+) AS request_id", sql)]
                if not ids:  # The old per-table implementation.
                    ids = [int(re.search(r"table_name = 't(\d+)'", sql).group(1))]
                return [dict(zip(columns, ([str(i)] if columns[0] == "request_id" else []) + rows[i]))
                        for i in reversed(ids)]

        conn = Conn([])
        names = ["db.t%d" % i for i in range(800)]
        stats = collect_stats(conn, names)
        self.assertEqual(len(conn.calls), 8)
        for sql in conn.calls:
            self.assertEqual(sql.count(" AS request_id"), 100)
            self.assertLess(len(sql.encode("utf-8")), 64 * 1024)
            self.assertEqual(sql.count("FROM information_schema.tables WHERE "), 100)
        self.assertEqual(list(stats), names)
        self.assertEqual([stats[name]["table_rows"] for name in names], list(range(800)))
        self.assertTrue(all(set(row) == set((
            "table_schema", "table_name", "table_rows", "data_length", "index_length", "data_free",
            "auto_increment", "engine", "row_format")) for row in stats.values()))

    def test_batch_failures_keep_readable_tables_and_requested_aliases(self):
        """A refused request must not discard the other tables in its batch."""
        import re
        from myflames.connector import ConnectorError
        names = ["db.users", "users", "secret.hidden", "other.users", "missing", "db.users", "DB.USERS"]
        rows = {0: ["db", "users"], 1: ["db", "users"], 3: ["other", "users"],
                5: ["db", "users"], 6: ["db", "users"]}

        class Conn(StubConn):
            def query_dicts(self, sql, columns):
                self.calls.append(sql)
                ids = [int(i) for i in re.findall(r"SELECT (\d+) AS request_id", sql)]
                if not ids:
                    raise AssertionError("Expected batched request identities")
                if 2 in ids:
                    raise ConnectorError("Access denied for hidden")
                return [dict(zip(columns, [str(i)] + rows[i] +
                                 ["4", "NULL", "bad", "0", "5", "InnoDB", "Dynamic"]))
                        for i in reversed(ids) if i in rows]

        conn = Conn([])
        stats = collect_stats(conn, names)
        self.assertEqual(list(stats), ["db.users", "users", "other.users", "DB.USERS"])
        self.assertEqual(stats["db.users"], stats["users"])
        self.assertEqual(stats["db.users"], stats["DB.USERS"])
        self.assertEqual(stats["other.users"]["table_schema"], "other")
        self.assertEqual(stats["db.users"]["index_length"], 0)
        self.assertTrue(any("DATABASE()" in sql for sql in conn.calls))

    def test_batches_stay_under_the_command_line_length(self):
        """The SQL goes on the client's command line (mysql -e): long names split batches."""
        import re
        from myflames import collectors
        names = ["s%03d%s.t%03d%s" % (i, "'" * 30 + "x" * 30, i, "\\" * 30) for i in range(300)]

        class Conn(StubConn):
            def query_dicts(self, sql, columns):
                self.calls.append(sql)
                return [dict(zip(columns, [i, "s", "t", "1", "1", "1", "0", "0", "InnoDB", "Dynamic"]))
                        for i in re.findall(r"SELECT (\d+) AS request_id", sql)]

        conn = Conn([])
        stats = collect_stats(conn, names)
        self.assertEqual(list(stats), names)
        self.assertGreater(len(conn.calls), 3, "long names need more, shorter batches")
        for sql in conn.calls:
            self.assertLessEqual(len(sql), collectors._STATS_BATCH_CHARS)
            self.assertLessEqual(sql.count(" AS request_id"), collectors._STATS_BATCH_SIZE)

    def test_failed_batch_costs_one_extra_call_and_keeps_per_table_errors(self):
        """As the per-table reads did: a table whose read fails is left out, and
        a client that fails every read is asked once per table plus once per batch."""
        import re
        from myflames.connector import ConnectorError
        for poisoned in (set(range(150)), {7}, set()):
            with self.subTest(failing=len(poisoned)):
                class Conn(StubConn):
                    def query_dicts(self, sql, columns):
                        self.calls.append(sql)
                        ids = [int(i) for i in re.findall(r"SELECT (\d+) AS request_id", sql)]
                        if poisoned.intersection(ids):
                            raise ConnectorError("mysql client exited 1: ERROR 2013 (HY000): Lost connection")
                        return [dict(zip(columns, [str(i), "db", "t%d" % i, "1", "1", "1", "0", "0", "InnoDB",
                                                   "Dynamic"])) for i in ids]

                conn = Conn([])
                stats = collect_stats(conn, ["db.t%d" % i for i in range(150)])
                self.assertEqual(sorted(int(k[4:]) for k in stats), sorted(set(range(150)) - poisoned))
                expected = {150: 2 + 150, 1: 2 + 100, 0: 2}[len(poisoned)]
                self.assertEqual(len(conn.calls), expected)

    def test_rows_answer_only_their_own_request(self):
        conn = StubConn([(lambda sql: "information_schema.tables" in sql, [
            ["x", "db", "a", "1", "1", "1", "0", "0", "InnoDB", "Dynamic"],
            ["7", "db", "a", "1", "1", "1", "0", "0", "InnoDB", "Dynamic"],
            ["", "db", "a", "1", "1", "1", "0", "0", "InnoDB", "Dynamic"],
            ["1", "db", "b", "2", "1", "1", "0", "0", "InnoDB", "Dynamic"],
            ["1", "db", "B", "3", "1", "1", "0", "0", "InnoDB", "Dynamic"],
        ])])
        stats = collect_stats(conn, ["db.a", "db.b"])
        self.assertEqual(list(stats), ["db.b"])
        self.assertEqual(stats["db.b"]["table_rows"], 2, "the first row the server returns, as before")
        self.assertEqual(len(conn.calls), 1)

    def test_any_iterable_of_names_is_read_once(self):
        row = ["0", "db", "t", "5", "1", "1", "0", "0", "InnoDB", "Dynamic"]
        for names in (["db.t"], ("db.t",), iter(["db.t"]), (n for n in ["db.t"])):
            with self.subTest(kind=type(names).__name__):
                conn = StubConn([(lambda sql: "information_schema.tables" in sql, [row])])
                self.assertEqual(collect_stats(conn, names)["db.t"]["table_rows"], 5)
                self.assertEqual(len(conn.calls), 1)
        self.assertEqual(collect_stats(StubConn([]), None), {})

    def test_empty_stats_never_reads(self):
        conn = StubConn([])
        self.assertEqual(collect_stats(conn, []), {})
        self.assertEqual(conn.calls, [])

    def test_parses_numeric_columns(self):
        row = [
            "0", "testdb", "users", "10000", "1048576", "524288",
            "0", "10001", "InnoDB", "Dynamic",
        ]
        conn = StubConn([
            (lambda sql: "information_schema.tables" in sql, [row]),
        ])
        stats = collect_stats(conn, ["testdb.users"])
        self.assertIn("testdb.users", stats)
        s = stats["testdb.users"]
        self.assertEqual(s["table_rows"], 10000)
        self.assertEqual(s["data_length"], 1048576)
        self.assertEqual(s["index_length"], 524288)
        self.assertEqual(s["engine"], "InnoDB")

    def test_missing_table_returns_empty_entry(self):
        conn = StubConn([
            (lambda sql: "information_schema.tables" in sql, []),
        ])
        stats = collect_stats(conn, ["nowhere"])
        self.assertEqual(stats, {})

    def test_non_numeric_coerced_to_zero(self):
        row = ["0", "testdb", "t", "NULL", "", "not_a_number", "0", "0", "InnoDB", "Dynamic"]
        conn = StubConn([
            (lambda sql: "information_schema.tables" in sql, [row]),
        ])
        stats = collect_stats(conn, ["testdb.t"])
        s = stats["testdb.t"]
        self.assertEqual(s["table_rows"], 0)
        self.assertEqual(s["data_length"], 0)
        self.assertEqual(s["index_length"], 0)


# ---------------------------------------------------------------------------
# collect_session_variables
# ---------------------------------------------------------------------------

class TestCollectSessionVariables(unittest.TestCase):

    def test_filters_to_advisor_list(self):
        rows = [
            ["innodb_buffer_pool_size", "134217728"],
            ["sort_buffer_size", "262144"],
            ["something_irrelevant", "garbage"],
            ["optimizer_switch", "hash_join=on,block_nested_loop=off"],
        ]
        conn = StubConn([
            (lambda sql: sql.startswith("SHOW SESSION VARIABLES"), rows),
        ])
        variables = collect_session_variables(conn)
        self.assertIn("innodb_buffer_pool_size", variables)
        self.assertIn("optimizer_switch", variables)
        self.assertNotIn("something_irrelevant", variables)

    def test_custom_name_filter(self):
        rows = [["tmp_table_size", "16777216"], ["sort_buffer_size", "262144"]]
        conn = StubConn([
            (lambda sql: sql.startswith("SHOW SESSION VARIABLES"), rows),
        ])
        variables = collect_session_variables(conn, names=["tmp_table_size"])
        self.assertEqual(list(variables.keys()), ["tmp_table_size"])

    def test_advisor_variables_has_core_entries(self):
        """Sanity: ADVISOR_VARIABLES must include the variables each rule looks for."""
        essentials = {
            "innodb_buffer_pool_size",
            "sort_buffer_size",
            "join_buffer_size",
            "tmp_table_size",
            "max_heap_table_size",
            "optimizer_switch",
            "innodb_flush_log_at_trx_commit",
        }
        self.assertTrue(essentials.issubset(set(ADVISOR_VARIABLES)))


if __name__ == "__main__":
    unittest.main()
