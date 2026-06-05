"""
Unit tests for :mod:`myflames.findings` — the ranked findings list and the CI
``--fail-on`` gate evaluation.
"""
import os
import sys
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(TEST_DIR))

from myflames.parser import parse_explain, analyze_plan
from myflames.output_sidecar import build_sidecar
from myflames.findings import build_findings, evaluate_check, available_triggers

FIXTURE_DIR = os.path.join(TEST_DIR, "fixtures")
FULL_SCAN_FIXTURE = os.path.join(FIXTURE_DIR, "explain-001-table-scan-users-no-filter.json")


def _payload(path):
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    root = parse_explain(raw)
    return build_sidecar(root, analyze_plan(root), source_type="file", engine="mysql")


class TestBuildFindings(unittest.TestCase):

    def setUp(self):
        self.payload = _payload(FULL_SCAN_FIXTURE)
        self.findings = build_findings(self.payload)

    def test_returns_findings(self):
        self.assertTrue(self.findings)

    def test_each_finding_has_stable_shape(self):
        for f in self.findings:
            for key in ("kind", "severity", "category", "text", "source", "confidence"):
                self.assertIn(key, f)
            self.assertIn(f["kind"], ("warning", "suggestion"))
            self.assertIn(f["confidence"], ("high", "medium", "low"))

    def test_ranked_high_severity_first(self):
        ranks = {"error": 0, "high": 0, "warn": 1, "medium": 1, "info": 2, "low": 2}
        seq = [ranks.get(f["severity"], 3) for f in self.findings]
        self.assertEqual(seq, sorted(seq), "findings are not severity-ranked")

    def test_full_scan_warning_is_high_confidence(self):
        scans = [f for f in self.findings
                 if f["kind"] == "warning" and f["category"] == "full_scan"]
        self.assertTrue(scans)
        self.assertEqual(scans[0]["confidence"], "high")

    def test_empty_payload_yields_no_findings(self):
        empty = {"warnings": [], "suggestions": []}
        self.assertEqual(build_findings(empty), [])


class TestEvaluateCheck(unittest.TestCase):

    def setUp(self):
        self.payload = _payload(FULL_SCAN_FIXTURE)

    def test_matching_category_trips_gate(self):
        matched = evaluate_check(self.payload, ["full_scan"])
        self.assertTrue(matched)
        self.assertEqual(matched[0]["category"], "full_scan")

    def test_non_matching_category_passes(self):
        self.assertEqual(evaluate_check(self.payload, ["filesort"]), [])

    def test_any_matches_all_warnings(self):
        matched = evaluate_check(self.payload, ["any"])
        self.assertEqual(len(matched), len(self.payload.get("warnings") or []))

    def test_severity_trigger(self):
        # The full-scan plan emits a 'warn' severity warning.
        matched = evaluate_check(self.payload, ["warn"])
        self.assertTrue(matched)

    def test_triggers_are_case_insensitive_and_trimmed(self):
        self.assertTrue(evaluate_check(self.payload, [" FULL_SCAN "]))

    def test_available_triggers_includes_present_categories(self):
        trig = available_triggers(self.payload)
        self.assertIn("full_scan", trig)


if __name__ == "__main__":
    unittest.main()
