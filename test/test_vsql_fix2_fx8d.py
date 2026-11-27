"""FX8d integration: exact replay aliases and unknown VillageSQL labels."""
import copy
import json
import os
import unittest

from _perf_replay import expanded_preflight_sql
from myflames import server_info
from myflames.connector import ConnectorError
from test_lifecycle import StrictReplay
from test_vsql_fix2_fx8a import SchemaReplayConnection


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture(*parts):
    with open(os.path.join(FIXTURES, *parts), encoding="utf-8") as handle:
        return json.load(handle)


class LifecycleAliases(unittest.TestCase):
    def setUp(self):
        self.record = fixture("lifecycle", "native-8.0.46-to-8.4.0.json")
        self.call = self.record["calls"][0]
        self.original = self.call["sql"]
        self.expanded = expanded_preflight_sql(self.original, self.call["result"])
        self.assertNotEqual(self.expanded, self.original)

    def test_only_optional_name_changes_and_recorded_reads_stay_exact(self):
        before = copy.deepcopy(self.record)
        calls = self.record["calls"] + self.record.get("synthetic_calls", [])
        conn = StrictReplay(calls)
        self.assertEqual(conn.run(self.expanded), self.call["result"])
        for call in calls:
            self.assertEqual(conn.run(call["sql"]), call["result"])
        self.assertEqual(conn.calls, [(self.expanded, None)] +
                         [(call["sql"], None) for call in calls])
        self.assertEqual(self.record, before)

    def test_reordered_duplicate_missing_and_reformatted_names_are_refused(self):
        names = self.original.split(" IN (", 1)[1][:-1].split(",")
        prefix = self.original.split(" IN (", 1)[0] + " IN ("
        for bad in (prefix + ",".join([names[1], names[0]] + names[2:]) + ")",
                    prefix + ",".join([names[0]] + names) + ")",
                    prefix + ",".join(names[1:]) + ")",
                    prefix + ", ".join(names) + ")",
                    self.original + " LIMIT 1"):
            with self.subTest(sql=bad):
                self.assertNotEqual(bad, self.original)
                self.assertEqual(expanded_preflight_sql(bad, self.call["result"]), bad)
                with self.assertRaisesRegex(AssertionError, "unexpected SQL"):
                    StrictReplay([self.call]).run(bad)

    def test_real_expanded_answer_wins_in_both_recording_orders(self):
        expanded = {"sql": self.expanded, "result": "recorded expanded answer"}
        for calls in ([self.call, expanded], [expanded, self.call]):
            with self.subTest(calls=calls):
                conn = StrictReplay(calls)
                self.assertEqual(conn.run(self.expanded), expanded["result"])
                self.assertEqual(conn.run(self.original), self.call["result"])

    def test_villagesql_recording_cannot_supply_an_unrecorded_codebase(self):
        call = dict(self.call, result=self.call["result"].replace(
            "8.0.46", "8.0.46-villagesql-0.0.8"))
        self.assertIn("villagesql", call["result"])
        self.assertEqual(expanded_preflight_sql(self.original, call["result"]), self.original)
        with self.assertRaisesRegex(AssertionError, "unexpected SQL"):
            StrictReplay([call]).run(self.expanded)


class Schema57Alias(unittest.TestCase):
    def test_original_57_answer_and_recording_are_preserved(self):
        record = fixture("schema_export", "capture-mysql-5.7.json")
        before = copy.deepcopy(record)
        call = record["calls"][0]
        expanded = expanded_preflight_sql(call["sql"], call["stdout"])
        self.assertNotEqual(expanded, call["sql"])
        self.assertNotIn("villagesql_server_version", call["stdout"])
        conn = SchemaReplayConnection(record)
        self.assertEqual(conn.run(expanded, extra_flags=["--comments"]), call["stdout"])
        self.assertEqual(conn.sent, [("run", expanded, ["--comments"])])
        self.assertEqual(record, before)
        with self.assertRaisesRegex(ConnectorError, "unexpected SQL"):
            conn.run(expanded + " LIMIT 1")


class UnknownVillageSQLLabels(unittest.TestCase):
    def test_shared_explicit_identity_fallbacks_match_python(self):
        vectors = fixture("villagesql", "version-labels.json")
        explicit = [vector for vector in vectors if "expected_explicit_label" in vector]
        self.assertGreaterEqual(len(explicit), 3)
        for vector in explicit:
            with self.subTest(name=vector["name"]):
                self.assertIsNone(server_info.villagesql_version(vector["version"]))
                report = {"server_product": "VillageSQL", "server_version": vector["version"],
                          "server_base": vector["base"]}
                self.assertEqual(server_info.server_label(report), vector["expected_explicit_label"])


if __name__ == "__main__":
    unittest.main()
