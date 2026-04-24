"""
Tests for myflames.output_compare_sidecar (Slice 6 / S4).
"""
import os
import re
import sys
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_DIR)

from myflames.output_compare_sidecar import (
    build_compare_sidecar,
    COMPARE_SCHEMA_VERSION,
    COMPARE_SCHEMA_URL,
)


def _load_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestCompareSidecar(unittest.TestCase):

    def setUp(self):
        self.before = _load_text(os.path.join(
            TEST_DIR, "fixtures",
            "explain-001-table-scan-users-no-filter.json"))
        self.after = _load_text(os.path.join(
            TEST_DIR, "fixtures",
            "explain-008-index-scan-users-by-country.json"))

    def test_payload_shape(self):
        p = build_compare_sidecar(self.before, self.after)
        self.assertEqual(p["schema_version"], COMPARE_SCHEMA_VERSION)
        self.assertEqual(p["$schema"], COMPARE_SCHEMA_URL)
        self.assertIn("before", p)
        self.assertIn("after", p)
        self.assertIn("summary", p)
        self.assertIn("deltas", p)
        for key in ("time_delta_ms", "regressions", "improvements",
                    "unchanged"):
            self.assertIn(key, p["summary"])

    def test_deltas_carry_classification_enum(self):
        p = build_compare_sidecar(self.before, self.after)
        allowed = {"improved", "regressed", "unchanged", "new_or_removed"}
        for d in p["deltas"]:
            self.assertIn(d["classification"], allowed)

    def test_summary_counts_sum_to_delta_count(self):
        p = build_compare_sidecar(self.before, self.after)
        # new_or_removed deltas aren't counted in the 3 summary buckets;
        # so the sum is <= len(deltas), never more.
        summed = (
            p["summary"]["regressions"]
            + p["summary"]["improvements"]
            + p["summary"]["unchanged"]
        )
        self.assertLessEqual(summed, len(p["deltas"]))

    def test_before_after_node_ids_are_valid(self):
        p = build_compare_sidecar(self.before, self.after)
        pat = re.compile(r"^n:[0-9a-f]{12}$")
        # Root ids always present.
        self.assertTrue(pat.match(p["before"]["root_node_id"]))
        self.assertTrue(pat.match(p["after"]["root_node_id"]))
        # Per-delta ids are either empty (label only on one side) or valid.
        for d in p["deltas"]:
            for k in ("before_node_id", "after_node_id"):
                v = d.get(k, "")
                self.assertTrue(v == "" or pat.match(v), k + "=" + v)

    def test_identical_plans_yield_all_unchanged(self):
        p = build_compare_sidecar(self.before, self.before)
        self.assertEqual(p["summary"]["regressions"], 0)
        self.assertEqual(p["summary"]["improvements"], 0)
        # All deltas should be classified unchanged.
        self.assertTrue(all(
            d["classification"] == "unchanged" for d in p["deltas"]))


if __name__ == "__main__":
    unittest.main()
