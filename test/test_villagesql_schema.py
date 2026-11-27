"""VillageSQL custom-index safety and internal-schema exclusions.

The live capture is unmodified server evidence. The small arithmetic cases
exercise combinations that the live HNSW extension does not expose, such as
an unknown future index type and unavailable usage counters.
"""
import hashlib
import json
import os
import re
import unittest

from _perf_replay import assert_read_only, batch, replay
from myflames import accounts, indexes, io_stats, lifecycle, perf_common, privileges, schema_health
from myflames.perf_common import Server
from test_indexes import raw, stat as index_stat
from test_schema_health import key, raw as table_raw, table


CUSTOM_REASON = "Performance Schema does not record reads through VillageSQL custom indexes"


def stat(*args, **kwargs):
    """Synthetic MySQL rows include VillageSQL's first-column projection.

    A built-in column type keeps ordinary indexes in the usage tests.
    Cases for extension types or unavailable metadata override it explicitly.
    Other engines retain their original STATISTICS row shape.
    """
    first_column_type = kwargs.pop("first_column_type", "int")
    row = index_stat(*args, **kwargs)
    if kwargs.get("engine", "mysql") == "mysql":
        row.append(first_column_type if row[4] == "1" else None)
    return row


def village_server(codebase="mysql-8.4", ps="ON", userstat="OFF"):
    return Server({"version": "8.4.11-villagesql-0.0.8", "version_comment": "Source distribution",
                   "villagesql_server_version": codebase + "_0.0.8", "performance_schema": ps,
                   "userstat": userstat}, {"Uptime": "86400"})


def recorded_indexes(name):
    """Keep old SQL/results intact, supplementing only the later count gate.

    Older captures lack the count statement. Derive its test-only response
    from the recorded STATISTICS rows, including the successful fallback on
    MySQL 5.7 after IS_VISIBLE and EXPRESSION were refused.
    """
    conn, data = replay("indexes", name)
    definitions = next(text for sql, text, *rest in data["calls"]
                       if "FROM information_schema.STATISTICS" in sql and text)
    count = len({tuple(line.split("\t")[:2]) for line in definitions.split("\n") if line})
    server = Server({"version": "11.4.13-MariaDB" if name.startswith("mariadb") else "8.4.11"}, {})
    for schemas in (None, ("mfshop",)):
        sql = perf_common._table_count_sql(server, False, perf_common.scope_sql("TABLE_SCHEMA", schemas))
        conn.answers[indexes.INDEXES_TAG + sql] = [batch([[count]])]
    result = indexes.index_report(conn, **data["args"])
    return result, conn


class TestVillageSQLCustomIndexes(unittest.TestCase):
    def assert_custom_safe(self, report, names):
        names = set(names)
        self.assertFalse(names.intersection(row["index"] for row in report["unused"]))
        self.assertFalse(names.intersection(row["index"] for row in report["redundant"]))
        self.assertFalse(names.intersection(row["dominant"] for row in report["redundant"]))
        found = [row for row in report["indexes"] if row["index"] in names]
        self.assertEqual({row["index"] for row in found}, names)
        for row in found:
            self.assertEqual(row.get("action_blocked_reason"), CUSTOM_REASON)
            self.assertFalse(row.get("drop_sql"))
            self.assertFalse(row.get("test_sql"))
        custom = [row for row in report["no_data"] if row["reason"] == "custom_index"]
        self.assertTrue(custom)
        for row in custom:
            self.assertIn(CUSTOM_REASON, row["reason_text"])
            self.assertFalse(row.get("drop_sql"))
            self.assertFalse(row.get("test_sql"))

    def test_recorded_hnsw_index_is_no_data_and_has_no_actions(self):
        report, conn = recorded_indexes("villagesql-8.4-custom-index")
        assert_read_only(self, conn, indexes.INDEXES_TAG)
        self.assertEqual(report["flavor"], "villagesql")
        custom = [row for row in report["indexes"] if row["type"] == "HNSW"]
        self.assertTrue(custom, "The live fixture must include the installed custom index")
        self.assert_custom_safe(report, [row["index"] for row in custom])
        self.assertIn("Performance Schema does not record reads", indexes.format_text(report))
        json.dumps(report, allow_nan=False)

    def test_custom_indexes_cannot_duplicate_or_dominate_btree_indexes(self):
        rows = [stat("vectors", "PRIMARY", 1, "id", 0),
                stat("vectors", "a_custom", 1, "v", itype="HNSW"),
                stat("vectors", "a_custom", 2, "id", itype="HNSW"),
                stat("vectors", "z_custom", 1, "v", itype="HNSW"),
                stat("vectors", "b_tree", 1, "v"),
                stat("vectors", "b_tree_dup", 1, "v")]
        usage = [["shop", "vectors", name, "0", "0", "0"]
                 for name in ("a_custom", "z_custom", "b_tree", "b_tree_dup")]
        usage.append(["shop", "vectors", "PRIMARY", "10", "10", "1000"])
        for codebase in ("mysql-8.4", "percona-8.4", "mysql-9.7"):
            with self.subTest(codebase=codebase):
                report = indexes.compute_indexes(village_server(codebase), raw(rows, usage), limit=None)
                self.assert_custom_safe(report, ("a_custom", "z_custom"))
                self.assertEqual({row["index"] for row in report["unused"]}, {"b_tree", "b_tree_dup"})
                self.assertEqual([(row["index"], row["dominant"]) for row in report["redundant"]],
                                 [("b_tree_dup", "b_tree")])
                self.assertEqual(report["summary"]["indexes"], 5)
                self.assertEqual(report["no_data"][0]["indexes"], 2)

    def test_custom_reason_survives_absent_disabled_and_nonzero_counters(self):
        definitions = [stat("vectors", "knn", 1, "v", itype="HNSW")]
        cases = [raw(definitions, []),
                 raw(definitions, [["shop", "vectors", "knn", "0", "0", "0"]]),
                 raw(definitions, [["shop", "vectors", "knn", "99", "99", "1000"]]),
                 raw(definitions, None),
                 raw(definitions, [], instrument=(("wait/io/table/sql/handler", "NO"),)),
                 raw(definitions, [], consumers={"global_instrumentation": False}),
                 raw(definitions, [], objects=[["shop", "vectors", "NO"]]),
                 raw(definitions, [], partitioned=[["shop", "vectors", "partitioned"]])]
        for number, captured in enumerate(cases):
            with self.subTest(case=number):
                result = indexes.compute_indexes(village_server(), captured, limit=None)
                self.assert_custom_safe(result, ("knn",))
                self.assertEqual(len(result["no_data"]), 1)

    def test_percona_userstat_does_not_make_custom_indexes_candidates(self):
        rows = [stat("vectors", "knn", 1, "v", itype="HNSW"),
                stat("vectors", "btree", 1, "v")]
        captured = raw(rows, None, index_stats=[], table_stats=[["shop", "vectors", "100"]])
        report = indexes.compute_indexes(village_server("percona-8.4", ps="OFF", userstat="ON"),
                                         captured, limit=None)
        self.assertEqual(report["source"], "userstat")
        self.assert_custom_safe(report, ("knn",))
        self.assertEqual([row["index"] for row in report["unused"]], ["btree"])

    def test_unknown_custom_type_and_unique_or_primary_name_are_protected(self):
        for itype in ("hNsW", "FUTURE_EXTENSION_TYPE", "", None):
            for name, non_unique in (("custom", 1), ("unique_custom", 0), ("PRIMARY", 0)):
                with self.subTest(itype=itype, name=name):
                    definitions = [stat("vectors", name, 1, "v", itype=itype, non_unique=non_unique)]
                    result = indexes.compute_indexes(village_server(), raw(definitions, []), limit=None)
                    self.assert_custom_safe(result, (name,))

    def test_builtin_types_are_not_given_the_custom_exemption(self):
        for itype in ("BTREE", "btree", "HASH", "FULLTEXT", "SPATIAL", "RTREE"):
            with self.subTest(itype=itype):
                rows = [stat("t", "k", 1, "v", itype=itype)]
                usage = [["shop", "t", None, "10", "10", "1000"],
                         ["shop", "t", "k", "0", "0", "0"]]
                report = indexes.compute_indexes(village_server(), raw(rows, usage), limit=None)
                self.assertFalse([row for row in report["no_data"] if row["reason"] == "custom_index"])
                self.assertNotIn("action_blocked_reason", report["indexes"][0])

    def test_builtin_index_names_with_custom_or_missing_column_types_are_protected(self):
        for itype in ("BTREE", "HASH", "FULLTEXT", "SPATIAL", "RTREE"):
            for data_type in ("vsql_vector.SVECTOR", None, ""):
                with self.subTest(itype=itype, first_column_type=data_type):
                    rows = [stat("vectors", "k", 1, "v", itype=itype,
                                 first_column_type=data_type)]
                    usage = [["shop", "vectors", None, "100", "100", "10000"],
                             ["shop", "vectors", "k", "0", "0", "0"]]
                    report = indexes.compute_indexes(village_server(), raw(rows, usage), limit=None)
                    self.assert_custom_safe(report, ("k",))
                    self.assertEqual(report["full_scans"], [])

    def test_only_first_key_part_supplies_the_column_type(self):
        for first_type, second_type, custom in (("int", None, False),
                                                ("vsql_vector.SVECTOR", None, True),
                                                (None, "int", True)):
            for reverse in (False, True):
                with self.subTest(first_type=first_type, second_type=second_type, reverse=reverse):
                    first = stat("vectors", "k", 1, "v", first_column_type=first_type)
                    second = index_stat("vectors", "k", 2, "id") + [second_type]
                    rows = [second, first] if reverse else [first, second]
                    usage = [["shop", "vectors", None, "10", "10", "1000"],
                             ["shop", "vectors", "k", "0", "0", "0"]]
                    report = indexes.compute_indexes(village_server(), raw(rows, usage), limit=None)
                    if custom:
                        self.assert_custom_safe(report, ("k",))
                    else:
                        self.assertEqual([row["index"] for row in report["unused"]], ["k"])
                        self.assertNotIn("action_blocked_reason", report["indexes"][0])

    def test_unknown_type_on_other_flavors_keeps_existing_behavior(self):
        for version, comment in (("8.4.11", "MySQL Community Server - GPL"),
                                 ("8.4.11-11", "Percona Server"),
                                 ("11.4.13-MariaDB", "MariaDB Server")):
            with self.subTest(version=version):
                server = Server({"version": version, "version_comment": comment,
                                 "performance_schema": "ON"}, {"Uptime": "86400"})
                rows = [stat("t", "k", 1, "v", itype="HNSW", engine=server.engine)]
                usage = [["shop", "t", None, "10", "10", "1000"],
                         ["shop", "t", "k", "0", "0", "0"]]
                report = indexes.compute_indexes(server, raw(rows, usage), limit=None)
                self.assertEqual([row["index"] for row in report["unused"]], ["k"])
                self.assertNotIn("action_blocked_reason", report["indexes"][0])

    def test_table_no_data_combines_custom_indexes_without_duplicate_rows(self):
        rows = [stat("vectors", "knn", 1, "v", itype="HNSW"),
                stat("vectors", "btree", 1, "id")]
        report = indexes.compute_indexes(village_server(), raw(rows, []), limit=None)
        self.assert_custom_safe(report, ("knn",))
        self.assertEqual(len(report["no_data"]), 1)
        self.assertEqual(report["no_data"][0]["indexes"], 2)

    def test_incomplete_custom_reads_cannot_make_table_mostly_full_scans(self):
        definitions = [stat("vectors", "knn", 1, "v", itype="HNSW"),
                       stat("vectors", "btree", 1, "id"),
                       stat("ordinary", "btree", 1, "id")]
        usage = [["shop", name, None, "100", "100", "10000"] for name in ("vectors", "ordinary")]
        usage += [["shop", name, "btree", "10", "10", "1000"] for name in ("vectors", "ordinary")]
        usage.append(["shop", "vectors", "knn", "0", "0", "0"])
        cases = [(village_server(), raw(definitions, usage)),
                 (village_server("percona-8.4", ps="OFF", userstat="ON"),
                  raw(definitions, None,
                      index_stats=[["shop", name, "btree", "10"] for name in ("vectors", "ordinary")],
                      table_stats=[["shop", name, "110"] for name in ("vectors", "ordinary")]))]
        for server, captured in cases:
            with self.subTest(source="performance_schema" if server.ps_on else "userstat"):
                report = indexes.compute_indexes(server, captured, limit=None)
                self.assert_custom_safe(report, ("knn",))
                self.assertEqual([row["table"] for row in report["full_scans"]], ["ordinary"])
                self.assertTrue(report["full_scans"][0]["mostly"])
                self.assertEqual(report["summary"]["mostly_full_scans"], 1)


class TestVillageSQLTableHealth(unittest.TestCase):
    def test_custom_unique_key_cannot_stand_in_for_a_primary_key(self):
        captured = table_raw([table("vectors")], keys=[key("vectors", "u_vector", 1, "v", itype="HNSW")])
        report = schema_health.compute_tables(village_server(), captured, limit=None)
        finding = report["missing_primary_keys"][0]
        self.assertEqual(finding["kind"], "no_usable_key")
        self.assertEqual(finding["unique_keys"][0]["why_not"], "a hnsw index")
        self.assertFalse(finding["unique_keys"][0]["usable"])
        self.assertFalse([fix for fix in finding["fixes"] if "DROP INDEX" in fix["sql"]])

    def test_btree_unique_key_is_still_usable_beside_custom_key(self):
        captured = table_raw([table("vectors")], keys=[key("vectors", "u_vector", 1, "v", itype="HNSW"),
                                                     key("vectors", "u_id", 1, "id")])
        report = schema_health.compute_tables(village_server(), captured, limit=None)
        finding = report["missing_primary_keys"][0]
        self.assertEqual(finding["kind"], "not_null_unique")
        self.assertEqual(finding["clustered_on"], "u_id")
        self.assertIn("DROP INDEX `u_id`", finding["fixes"][0]["sql"])
        self.assertNotIn("DROP INDEX `u_vector`", finding["fixes"][0]["sql"])

    def test_custom_key_metadata_does_not_change_sizes_or_fragmentation(self):
        captured = table_raw([table("vectors", data=16384, index=32768, free=0)])
        original = schema_health.compute_tables(village_server(), captured, limit=None)
        captured["keys"] = [key("vectors", "u_vector", 1, "v", itype="HNSW")]
        result = schema_health.compute_tables(village_server(), captured, limit=None)
        self.assertEqual(result["tables"], original["tables"])
        self.assertEqual(result["fragmentation"], original["fragmentation"])
        self.assertEqual(result["tables"][0]["index_bytes"], 32768)


class TestVillageSQLSystemSchema(unittest.TestCase):
    FLAVORS = (
        ("8.4.11", "MySQL Community Server - GPL", "mysql"),
        ("8.4.11-11", "Percona Server (GPL)", "percona"),
        ("11.4.13-MariaDB", "MariaDB Server", "mariadb"),
        ("8.4.11-villagesql-0.0.8", "Source distribution", "villagesql"),
    )

    def test_shared_filters_preserve_exact_legacy_sql_on_other_flavors(self):
        legacy = "('mysql', 'performance_schema', 'information_schema', 'sys')"
        self.assertEqual(perf_common.SYSTEM_SCHEMAS,
                         ("mysql", "performance_schema", "information_schema", "sys"))
        self.assertEqual(perf_common.VILLAGESQL_INTERNAL_SCHEMAS, ("villagesql",))
        for flavor in (None, "mysql", "percona", "mariadb", "villagesql"):
            with self.subTest(flavor=flavor):
                names = legacy[:-1] + ", 'villagesql')" if flavor == "villagesql" else legacy
                self.assertEqual(perf_common.scope_sql("TABLE_SCHEMA", None, flavor),
                                 "TABLE_SCHEMA NOT IN " + names)
                self.assertEqual(indexes._scope_sql("TABLE_SCHEMA", None, flavor),
                                 "TABLE_SCHEMA NOT IN " + names)
                self.assertEqual(io_stats._scope(None, flavor), "OBJECT_SCHEMA NOT IN " + names)
                # Explicit scope remains exactly the same on every flavor.
                for schemas, suffix in ((("review",), " = CONVERT(X'726576696577' USING utf8mb4)"),
                                        (("review", "app"), " IN (CONVERT(X'726576696577' USING utf8mb4), CONVERT(X'617070' USING utf8mb4))")):
                    self.assertEqual(perf_common.scope_sql("TABLE_SCHEMA", schemas, flavor),
                                     "TABLE_SCHEMA" + suffix)
                self.assertEqual(perf_common.is_system_schema("VILLAGESQL", flavor), flavor == "villagesql")
                self.assertTrue(perf_common.is_system_schema("#mysql50#lost+found", flavor))

    def test_collectors_pass_known_flavor_to_all_schema_filters(self):
        legacy = "('mysql', 'performance_schema', 'information_schema', 'sys')"
        for version, comment, flavor in self.FLAVORS:
            with self.subTest(flavor=flavor):
                server = Server({"version": version, "version_comment": comment}, {})
                expected = legacy[:-1] + ", 'villagesql')" if flavor == "villagesql" else legacy
                expressions = [indexes._statistics_sql(server, None), indexes._fk_sql(None, flavor),
                               indexes._usage_sql(None, flavor), schema_health._tables_sql(None, flavor),
                               schema_health._userstat_sql(None, flavor), schema_health._keys_sql(server, None),
                               schema_health._columns_sql(server, None)]
                expressions.extend(io_stats._instance_queries(None, flavor)[name] for name in ("tables", "userstat"))
                for sql in expressions:
                    self.assertTrue(sql.endswith("NOT IN " + expected), sql)
                    self.assertEqual("'villagesql'" in sql, flavor == "villagesql")
                # Collection gates must use the same exclusion as the metadata read.
                class Reader(object):
                    def rows(self, sql):
                        self.sql = sql
                        return [["0"]]
                reader = Reader()
                perf_common.require_choice(reader, server, None)
                self.assertEqual(reader.sql, perf_common._table_count_sql(
                    server, False, "TABLE_SCHEMA NOT IN " + expected))

    def test_shared_schema_catalog_marks_internal_only_for_villagesql(self):
        for version, comment, flavor in self.FLAVORS:
            with self.subTest(flavor=flavor):
                server = Server({"version": version, "version_comment": comment}, {})
                class Reader(object):
                    def rows(self, sql):
                        if "SCHEMATA" in sql:
                            return [["villagesql"], ["VILLAGESQL"], ["review"]]
                        return [["villagesql", "8"], ["VILLAGESQL", "2"], ["review", "3"]]
                catalog = perf_common.list_schemas(Reader(), server)
                self.assertEqual(perf_common.user_table_count(catalog), 3 if flavor == "villagesql" else 13)
                self.assertEqual({row["name"] for row in catalog if row["system"]},
                                 {"villagesql", "VILLAGESQL"} if flavor == "villagesql" else set())

    def test_schema_health_exclusions_and_notes_follow_flavor(self):
        for version, comment, flavor in self.FLAVORS:
            with self.subTest(flavor=flavor):
                server = Server({"version": version, "version_comment": comment}, {})
                report = schema_health.compute_tables(server, table_raw([]), limit=None)
                expected = ["mysql", "sys", "performance_schema", "information_schema"]
                if flavor == "villagesql":
                    expected.append("villagesql")
                self.assertEqual(report["scope"]["excluded"], expected)
                self.assertIn("Read every schema except %s and %s." %
                              (", ".join(expected[:-1]), expected[-1]), report["notes"])
                scoped = schema_health.compute_tables(server, table_raw([]), schemas=["review"], limit=None)
                self.assertEqual(scoped["scope"]["excluded"], [])

    def test_account_database_scope_follows_known_identity(self):
        for version, comment, flavor in self.FLAVORS:
            with self.subTest(flavor=flavor):
                account = {"engine": "mariadb" if flavor == "mariadb" else "mysql", "version": version,
                           "version_comment": comment, "name": "reader", "host": "%",
                           "grants": [], "model": privileges.parse_grants([]),
                           "privilege_catalog": {level: [] for level in privileges.GRANT_LEVELS}}
                scopes = accounts._grant_scopes(account, ["villagesql", "review"])
                by_name = {scope["database"]: scope for scope in scopes if scope["level"] == "database"}
                self.assertEqual(by_name["villagesql"]["system"], flavor == "villagesql")
                self.assertFalse(by_name["review"]["system"])

    def test_lifecycle_sql_preserves_order_and_uses_only_cached_identity(self):
        for version, comment, flavor in self.FLAVORS:
            for include_system in (False, True):
                with self.subTest(flavor=flavor, include_system=include_system):
                    names = "'performance_schema','information_schema'" if include_system else (
                        "'mysql','sys','performance_schema','information_schema'")
                    if flavor == "villagesql":
                        names += ",'villagesql'"
                    expected = "TABLE_SCHEMA NOT IN (" + names + ")"
                    self.assertEqual(lifecycle._catalog_where(include_system=include_system, flavor=flavor), expected)
                    class Conn(object):
                        _version_cache = version
                        _version_comment_cache = comment
                        def __init__(self):
                            self.calls = []
                        def run(self, sql, extra_flags=None, timeout=None):
                            self.calls.append(sql)
                            return ""
                    conn = Conn()
                    self.assertEqual(lifecycle._catalog(conn, include_system=include_system), [])
                    self.assertEqual(conn.calls, [lifecycle.TAG + "SELECT TABLE_SCHEMA,TABLE_NAME,TABLE_TYPE,ENGINE,"
                                                  "COALESCE(DATA_LENGTH,0),COALESCE(INDEX_LENGTH,0) "
                                                  "FROM information_schema.TABLES WHERE " + expected])
                    self.assertEqual(lifecycle._schema_defaults(conn, None, include_system=include_system), {})
                    self.assertEqual(conn.calls[-1], lifecycle.TAG + "SELECT SCHEMA_NAME,DEFAULT_CHARACTER_SET_NAME,"
                                     "DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA WHERE " +
                                     expected.replace("TABLE_SCHEMA", "SCHEMA_NAME"))
                    self.assertEqual(len(conn.calls), 2, "Identity must not add a probe")
                    self.assertEqual(lifecycle._catalog_where(["review"], ["t"], include_system, flavor),
                                     "TABLE_SCHEMA IN (CONVERT(X'726576696577' USING utf8mb4)) "
                                     "AND TABLE_NAME IN (CONVERT(X'74' USING utf8mb4))")
        self.assertEqual(lifecycle._catalog_where(),
                         "TABLE_SCHEMA NOT IN ('mysql','sys','performance_schema','information_schema')")

    def test_workspace_system_database_lists_include_villagesql(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for filename, name in (("SchemaVersions.tsx", "SYSTEM"), ("Connections.tsx", "SYSTEM_DATABASES")):
            with self.subTest(filename=filename):
                with open(os.path.join(root, "apps", "workspace", "src", filename), encoding="utf-8") as handle:
                    source = handle.read()
                declaration = re.search(r"const " + name + r" = (?:new Set\()?\[([^\]]+)\]", source)
                self.assertIsNotNone(declaration)
                self.assertIn('"villagesql"', declaration.group(1))


class TestUnchangedIndexFixtures(unittest.TestCase):
    # Canonical full-report SHA-256 values recorded immediately before the L6
    # implementation. Only taken_at is excluded because it is wall-clock time.
    BASELINES = {
        "mariadb-11.4": "d76709adfb05ad7d1fdb6a347719801d7cc833eb52020252306348d43a51bcb9",
        "mariadb-11.4-userstat": "4c1b5b348e9e97cbf55db491d68a5fea5411f541a02942b742c04312ecbf1a29",
        "mariadb-13.0": "e854178db75754d53d1f9360fb09dbbc28114b072b80c7ca25093005eb5e0079",
        "mariadb-13.0-default": "319cd6b6f3674474c74162ce73fdc2c17e43185484d6a29d098b3c2704d42f23",
        "mysql-26.7": "1dd233c6a310f2e5f304d7fc208293fed9226e3bf3e928a4773606dfc16c6dab",
        "mysql-5.7": "9d0f29688125102adc745016529aab9bdb14b57cc16bb37ce976a4fbbd9b60f8",
        "mysql-8.4": "f6d9c5849014eabf7c8df985f108e3c1ac0546ed7084ef0cb0548ca5365f30e6",
        "mysql-9.7": "cbd8fee06be25ea123af4e5704e3ac82964995c87ee8459f6e5bd8f2ca373204",
        "percona-8.4": "6b16c7c3e246919df72fa3d47398cd54c8a1d3606805af04d35bf0f491fa69d9",
        "percona-8.4-partitioned": "c1a2ee9458c34521e599b3486dd30e7e8366ff2c9d7ac651caaea934892a519e",
        "percona-8.4-psoff": "f0c1e67a0a057638fb94a8d3b7e56316fb7d34876f33dae637845ecfc6e62086",
    }

    def test_mysql_percona_mariadb_reports_match_prechange_results(self):
        for name, expected in sorted(self.BASELINES.items()):
            with self.subTest(server=name):
                report, conn = recorded_indexes(name)
                assert_read_only(self, conn, indexes.INDEXES_TAG)
                report.pop("taken_at")
                actual = hashlib.sha256(json.dumps(report, sort_keys=True, separators=(",", ":"))
                                        .encode("utf-8")).hexdigest()
                self.assertEqual(actual, expected, "An existing server's index report changed")


if __name__ == "__main__":
    unittest.main()
