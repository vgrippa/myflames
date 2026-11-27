"""FX4 regressions: index identity, userstat, vendor links and privileges.

Column types below are synthetic metadata in the appended VillageSQL
STATISTICS projection, not new live server recordings.
"""
import hashlib
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _perf_replay import assert_read_only, replay as replay_old_columns
from test_indexes import raw, replay, stat
from myflames import indexes, parameters, privileges, server_info
from myflames.perf_common import Server


def village_server(codebase="mysql-8.4_0.0.8", ps="ON", userstat="OFF"):
    values = {"version": "8.4.11-villagesql-0.0.8", "version_comment": "Source distribution",
              "performance_schema": ps, "userstat": userstat}
    if codebase is not None:
        values["villagesql_server_version"] = codebase
    return Server(values, {"Uptime": "864000"})


class TestCustomIndexIdentity(unittest.TestCase):
    def report(self, data_type="vsql_vector.VECTOR", itype="BTREE"):
        rows = [stat("vectors", "PRIMARY", 1, "id", 0) + ["int"],
                stat("vectors", "custom", 1, "v", itype=itype) + [data_type],
                stat("vectors", "custom_dup", 1, "v", itype=itype) + [data_type],
                stat("vectors", "plain", 1, "category") + ["int"]]
        usage = [["shop", "vectors", "PRIMARY", "10", "10", "1000"],
                 ["shop", "vectors", "custom", "0", "0", "0"],
                 ["shop", "vectors", "custom_dup", "0", "0", "0"],
                 ["shop", "vectors", "plain", "0", "0", "0"],
                 ["shop", "vectors", None, "100", "100", "10000"]]
        return indexes.compute_indexes(village_server(), raw(rows, usage), limit=None)

    def test_custom_type_named_like_a_builtin_never_gets_actions_or_advice(self):
        for itype in ("BTREE", "HASH", "FULLTEXT", "SPATIAL", "RTREE", "HNSW"):
            with self.subTest(itype=itype):
                report = self.report(itype=itype)
                self.assertEqual([r["index"] for r in report["unused"]], ["plain"])
                self.assertEqual(report["redundant"], [])
                self.assertEqual(report["full_scans"], [])
                self.assertEqual(report["no_data"][0]["reason"], "custom_index")
                self.assertEqual(report["no_data"][0]["indexes"], 2)
                for row in report["indexes"]:
                    self.assertEqual("action_blocked_reason" in row, row["index"].startswith("custom"))

    def test_missing_first_column_metadata_is_conservatively_exempt(self):
        for data_type in (None, ""):
            with self.subTest(data_type=data_type):
                report = self.report(data_type=data_type)
                self.assertEqual([r["index"] for r in report["unused"]], ["plain"])
                self.assertEqual(report["redundant"], [])
                self.assertEqual(report["full_scans"], [])

    def test_plain_first_column_preserves_builtin_index_analysis(self):
        report = self.report(data_type="varbinary")
        self.assertEqual({r["index"] for r in report["unused"]}, {"custom", "custom_dup", "plain"})
        self.assertEqual(len(report["redundant"]), 1)
        self.assertEqual(len(report["full_scans"]), 1)
        self.assertEqual(report["no_data"], [])

    def test_first_column_metadata_is_selected_in_the_existing_statement(self):
        sql = indexes._statistics_sql(village_server(), ("shop",))
        self.assertIn("information_schema.COLUMNS", sql)
        self.assertIn("DATA_TYPE", sql)
        self.assertIn("SEQ_IN_INDEX = 1", sql)
        self.assertIn("c.TABLE_SCHEMA = s.TABLE_SCHEMA", sql)
        self.assertIn("c.TABLE_NAME = s.TABLE_NAME", sql)
        self.assertIn("c.COLUMN_NAME = s.COLUMN_NAME", sql)

    def test_first_column_is_authoritative_even_when_statistics_rows_are_unordered(self):
        rows = [stat("vectors", "custom", 2, "category") + [None],
                stat("vectors", "custom", 1, "v") + ["vsql_vector.VECTOR"],
                stat("vectors", "plain", 2, "v") + [None],
                stat("vectors", "plain", 1, "category") + ["int"]]
        tables = indexes._tables(rows, "mysql")
        self.assertTrue(indexes._custom_index(village_server(), tables[("shop", "vectors")]["custom"]))
        self.assertFalse(indexes._custom_index(village_server(), tables[("shop", "vectors")]["plain"]))
        self.assertEqual(tables[("shop", "vectors")]["custom"]["columns"], ["v", "category"])

    def test_collector_uses_column_evidence_without_an_additional_statement(self):
        conn, data = replay_old_columns("indexes", "villagesql-8.4-custom-index")
        recorded_sql, recorded_text = next(call[:2] for call in data["calls"]
                                           if "FROM information_schema.STATISTICS " in call[0])
        # Reuse recorded reads, but simulate the extended projection and a
        # registration named btree. This is deliberately not live evidence.
        rows = []
        for line in recorded_text.splitlines():
            cells = line.split("\t")
            custom = cells[2] == "knn_vec"
            if custom:
                cells[8] = "BTREE"
            cells.append("vsql_vector.VECTOR" if custom else "int" if cells[4] == "1" else "NULL")
            rows.append("\t".join(cells))
        sql = indexes.INDEXES_TAG + indexes._statistics_sql(village_server(), ("review",))
        conn.answers[sql] = ["\n".join(rows) + "\n"]
        report = indexes.index_report(conn, **data["args"])
        assert_read_only(self, conn, indexes.INDEXES_TAG)
        custom = next(row for row in report["indexes"] if row["index"] == "knn_vec")
        self.assertIn("action_blocked_reason", custom)
        self.assertFalse([row for row in report["unused"] + report["redundant"] if row["index"] == "knn_vec"])
        sent = [statement for statement, flags in conn.calls]
        self.assertEqual(sent, [sql if call[0] == recorded_sql else call[0] for call in data["calls"]])


class TestUserstatPartitionGuard(unittest.TestCase):
    def report(self, partitioned, engine="mysql"):
        server = (village_server(None, ps="OFF", userstat="ON") if engine == "mysql" else
                  Server({"version": "11.4.13-MariaDB", "performance_schema": "OFF", "userstat": "ON"}, {}))
        rows = [stat("orders", "PRIMARY", 1, "id", 0, engine=engine),
                stat("orders", "k_user", 1, "user_id", engine=engine)]
        if engine == "mysql":
            rows = [row + ["int"] for row in rows]
        return indexes.compute_indexes(server, raw(rows, None,
            index_stats=[["shop", "orders", "PRIMARY", "10"], ["shop", "orders", "k_user", "5"]],
            table_stats=[["shop", "orders", "1000"]], partitioned=partitioned), limit=None)

    def test_unknown_mysql_base_requires_known_plain_table_for_userstat_shares(self):
        report = self.report([])
        self.assertEqual(report["source"], "userstat")
        self.assertEqual(report["full_scans"], [])
        self.assertTrue(any("shown only for known plain tables" in note for note in report["notes"]))

    def test_mysql_plain_tables_and_mariadb_keep_their_previous_shares(self):
        for engine, partitioned in (("mysql", [["shop", "orders", ""]]), ("mariadb", [])):
            with self.subTest(engine=engine):
                report = self.report(partitioned, engine)
                self.assertEqual(report["full_scans"][0]["rows_without_index"], 985)


class TestParameterDocumentation(unittest.TestCase):
    def test_percona_base_keeps_vendor_links_without_inventing_capabilities(self):
        for base, expected in (("percona", True), ("mysql", False), (None, False)):
            with self.subTest(base=base):
                report = parameters.compute_parameters("mysql", (8, 4, 11),
                    rows=[["binlog_space_limit", "0"], ["max_connections", "151"],
                          ["villagesql_schema_version", "1"]], info_rows=[], persisted_rows=[],
                    flavor="villagesql", base=base)
                items = {item["name"]: item for item in report["items"]}
                self.assertEqual(items["binlog_space_limit"]["vendor"], "percona" if expected else None)
                self.assertEqual(items["binlog_space_limit"]["documentation"] is not None, expected)
                if expected:
                    self.assertIn("docs.percona.com", items["binlog_space_limit"]["documentation"]["url"])
                self.assertIn("dev.mysql.com", items["max_connections"]["documentation"]["url"])
                self.assertIn("villagesql.com", items["villagesql_schema_version"]["documentation"]["url"])


class TestReportedPrivileges(unittest.TestCase):
    def test_server_reported_extension_admin_is_kept_on_every_mysql_product(self):
        rows = [["SELECT", "Tables", ""], ["EXTENSION_ADMIN", "Server Admin", ""],
                ["PLUGIN_PRIVILEGE_2", "Server Admin", ""]]
        for flavor in ("mysql", "percona", "readyset", "villagesql"):
            with self.subTest(flavor=flavor):
                catalog, meta = privileges.privilege_catalog("mysql", "8.4.11", rows, flavor=flavor)
                found = {item["name"]: item for item in catalog["global"]}
                self.assertEqual(set(found), {"SELECT", "EXTENSION_ADMIN", "PLUGIN_PRIVILEGE_2"})
                self.assertEqual(meta["source"], "server")
                self.assertTrue(found["EXTENSION_ADMIN"]["dynamic"])
                for level, items in catalog.items():
                    if level != "global":
                        self.assertNotIn("EXTENSION_ADMIN", [item["name"] for item in items])

    def test_static_extension_admin_fallback_remains_villagesql_only(self):
        for flavor in ("mysql", "percona", "readyset", "villagesql"):
            with self.subTest(flavor=flavor):
                catalog, _ = privileges.privilege_catalog("mysql", "8.4.11", flavor=flavor)
                self.assertEqual("EXTENSION_ADMIN" in {item["name"] for item in catalog["global"]},
                                 flavor == "villagesql")
                catalog, _ = privileges.privilege_catalog("mysql", "8.4.11", [["SELECT", "Tables", ""]],
                                                          flavor=flavor)
                self.assertNotIn("EXTENSION_ADMIN", {item["name"] for item in catalog["global"]})


class TestVillageVersionLabels(unittest.TestCase):
    def test_arbitrary_prerelease_labels_survive_without_the_commit_hash(self):
        for label in ("dev", "alpha", "rc.1", "RC1", "deadbee", "a" * 40):
            for suffix in ("", "-543f42e0601", "-" + "a" * 40):
                with self.subTest(label=label, suffix=suffix):
                    version = "8.4.11-villagesql-0.0.9-" + label + suffix
                    self.assertEqual(server_info.villagesql_version(version), "0.0.9-" + label)
                    self.assertEqual(server_info.server_label({"server_version": version,
                        "flavor": "villagesql", "server_base": "mysql"}),
                        "VillageSQL 0.0.9-%s (MySQL 8.4.11)" % label)

    def test_disallowed_prerelease_characters_never_reach_a_label(self):
        for suffix in ("preview-2", "custom_build", "rc.1\x1b[31m", "<b>rc.1</b>",
                       "K", "İ", "ı", "ſ"):
            for build in ("", "-543f42e0601", "-" + "a" * 40):
                with self.subTest(suffix=suffix, build=build):
                    version = "8.4.11-villagesql-0.0.9-" + suffix + build
                    self.assertIsNone(server_info.villagesql_version(version))
                    self.assertEqual(server_info.server_label({"server_version": version,
                        "flavor": "villagesql", "server_base": "mysql"}),
                        "VillageSQL ? (MySQL 8.4.11)")

    def test_release_and_non_villagesql_versions_keep_the_existing_contract(self):
        self.assertEqual(server_info.villagesql_version("8.4.11-villagesql-0.0.8"), "0.0.8")
        # A single hex suffix is a prerelease label, not a standalone build hash.
        self.assertEqual(server_info.villagesql_version("8.4.11-villagesql-0.0.8-543f42e0601"),
                         "0.0.8-543f42e0601")
        for version in (None, "", "8.4.11", "8.4.11-11", "11.4.13-MariaDB"):
            self.assertIsNone(server_info.villagesql_version(version))


class TestRecordedStatementParity(unittest.TestCase):
    # Captured before the FX4 edits by replaying the current tree. Each hash
    # covers the exact ordered SQL strings, including retry and scope reads.
    BASELINES = {
        "mariadb-11.4-userstat": (8, "047d4601a0d7e7bea0a0b51c07faa88b9195e0ca6700e1ad5d531eba06d77232"),
        "mariadb-11.4": (11, "7285c9759c9cddb4233d3fa56e6e7cab4c43773e037762b217d8fced0efdacb7"),
        "mariadb-13.0-default": (5, "7c53e8dec8fad4908dd7c7dbb6282f34c220f8344bf78007f42e72f63250719c"),
        "mariadb-13.0": (11, "7285c9759c9cddb4233d3fa56e6e7cab4c43773e037762b217d8fced0efdacb7"),
        "mysql-26.7": (11, "f5f57753340ba39ea4069a97289c3bcde70122dd2e117e44bb4dd5ba1cbce562"),
        "mysql-5.7": (14, "940551eabd4e2cc74876f504446fd2e0388dc6c1e0f26860099859080983c4af"),
        "mysql-8.4": (11, "f5f57753340ba39ea4069a97289c3bcde70122dd2e117e44bb4dd5ba1cbce562"),
        "mysql-9.7": (11, "f5f57753340ba39ea4069a97289c3bcde70122dd2e117e44bb4dd5ba1cbce562"),
        "percona-8.4-partitioned": (13, "44a9c9e8b7418318d70ced1e30d9beb1c783556e81f1227b2215b38758d4bdf9"),
        "percona-8.4-psoff": (8, "66381ed2c0d010bf917a3001d446a497c8392220922a593128c2ee5c9e02d95f"),
        "percona-8.4": (14, "36dec538de4079d1f40a4f13dd4fe5619e8883d14a94146add140120f2efefa8"),
    }

    def test_all_eleven_index_recordings_keep_byte_identical_sql(self):
        for name, (count, digest) in sorted(self.BASELINES.items()):
            with self.subTest(server=name):
                loader = replay_old_columns if name == "mysql-5.7" else replay
                conn, data = loader("indexes", name)
                indexes.index_report(conn, **data["args"])
                assert_read_only(self, conn, indexes.INDEXES_TAG)
                calls = [sql for sql, flags in conn.calls]
                self.assertEqual(len(calls), count)
                self.assertEqual(hashlib.sha256(json.dumps(calls, ensure_ascii=True).encode("ascii")).hexdigest(),
                                 digest)

    def test_readyset_identity_keeps_the_mysql_recorded_statement_sequence(self):
        def readyset_identity(sql, text, count):
            if "SHOW GLOBAL VARIABLES WHERE Variable_name IN" not in sql:
                return text
            rows = [line.split("\t") for line in text.splitlines()]
            for row in rows:
                if row[0] == "version_comment":
                    row[1] = "Readyset"
            return "\n".join("\t".join(row) for row in rows) + "\n"

        conn, data = replay("indexes", "mysql-8.4", edit=readyset_identity)
        report = indexes.index_report(conn, **data["args"])
        self.assertEqual(report["flavor"], "readyset")
        calls = [sql for sql, flags in conn.calls]
        count, digest = self.BASELINES["mysql-8.4"]
        self.assertEqual(len(calls), count)
        self.assertEqual(hashlib.sha256(json.dumps(calls, ensure_ascii=True).encode("ascii")).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
