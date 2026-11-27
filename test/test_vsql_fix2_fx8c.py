"""FX8c integration contracts: shared release vectors and strict replay aliases.

Release parsing must agree across Python and TypeScript. Composite labels are
also checked for every parseable release; existing unknown-version presentation
is separate from the release grammar.
"""
import json
import os
import unittest

import _schema_replay as schema_replay
from _perf_replay import ReplayConn, expanded_preflight_sql
from myflames import server_info
from myflames.connector import ConnectorError
from test_vsql_fix2_fx8a import SchemaReplayConnection


class SharedReleaseLabels(unittest.TestCase):
    def test_shared_python_and_typescript_release_vectors(self):
        path = os.path.join(os.path.dirname(__file__), "fixtures", "villagesql", "version-labels.json")
        with open(path, encoding="utf-8") as handle:
            vectors = json.load(handle)
        self.assertGreaterEqual(len(vectors), 40)
        self.assertEqual(len({item["name"] for item in vectors}), len(vectors))
        for item in vectors:
            with self.subTest(name=item["name"]):
                self.assertEqual(server_info.villagesql_version(item["version"]), item["expected_release"])
                report = {"server_version": item["version"], "flavor": "villagesql",
                          "server_base": item["base"]}
                if item["expected_release"] is not None:
                    self.assertEqual(server_info.server_label(report), item["expected_label"])
                else:
                    # An explicit product identity stays safe even if the
                    # version is malformed. No suffix enters the fallback.
                    upstream = server_info.base_version(item["version"]) or "?"
                    codebase = {"mysql": "MySQL ", "percona": "Percona Server "}.get(item["base"], "")
                    self.assertEqual(server_info.server_label(report),
                                     "VillageSQL ? (%s%s)" % (codebase, upstream))


class StrictSchemaAliases(unittest.TestCase):
    def setUp(self):
        self.record = schema_replay.recording("mysql-8.4", "before")
        self.call = self.record["calls"][0]
        self.original = self.call["sql"]
        self.expanded = self.original[:-1] + ", 'villagesql_server_version')"

    def test_only_optional_name_is_added_and_recording_stays_unchanged(self):
        before = json.dumps(self.record, sort_keys=True)
        self.assertEqual(expanded_preflight_sql(self.original, self.call["stdout"]), self.expanded)
        conn = SchemaReplayConnection(self.record)
        self.assertEqual(conn.run(self.expanded, extra_flags=["--comments"]), self.call["stdout"])
        self.assertEqual(conn.sent, [("run", self.expanded, ["--comments"])])
        self.assertEqual(json.dumps(self.record, sort_keys=True), before)

    def test_reordered_duplicate_missing_and_reformatted_names_are_not_aliases(self):
        for bad in (self.original.replace("'hostname', 'lower_case_table_names'",
                                          "'lower_case_table_names', 'hostname'"),
                    self.original.replace("'hostname', ", "'hostname', 'hostname', "),
                    self.original.replace("'hostname', ", ""),
                    self.original.replace(", ", ","),
                    self.original + " LIMIT 1"):
            with self.subTest(sql=bad):
                self.assertNotEqual(bad, self.original)
                self.assertEqual(expanded_preflight_sql(bad, self.call["stdout"]), bad)
                conn = SchemaReplayConnection(self.record)
                with self.assertRaisesRegex(ConnectorError, "unexpected SQL"):
                    conn.run(bad)

    def test_villagesql_recording_never_synthesizes_the_codebase(self):
        response = self.call["stdout"].replace("8.4.11", "8.4.11-villagesql-0.0.8")
        self.assertEqual(expanded_preflight_sql(self.original, response), self.original)
        conn = ReplayConn([[self.original, response]])
        with self.assertRaisesRegex(AssertionError, "unexpected SQL"):
            conn.run(self.expanded)

    def test_real_expanded_stream_wins_over_a_synthetic_alias(self):
        for calls in ([[self.original, "old"], [self.expanded, "new-1"], [self.expanded, "new-2"]],
                      [[self.expanded, "new-1"], [self.original, "old"], [self.expanded, "new-2"]]):
            with self.subTest(calls=calls):
                conn = ReplayConn(calls)
                self.assertEqual(conn.run(self.expanded), "new-1")
                self.assertEqual(conn.run(self.original), "old")
                self.assertEqual(conn.run(self.expanded), "new-2")
                self.assertNotIn(self.expanded, conn.aliases)


if __name__ == "__main__":
    unittest.main()
