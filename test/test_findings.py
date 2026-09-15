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
from myflames.findings import (
    build_findings,
    primary_suggestion_index,
    evaluate_check,
    available_triggers,
    known_triggers,
    unknown_triggers,
)

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


class TestPrimarySuggestionIndex(unittest.TestCase):
    """The one 'Fix first' selector — must rank suggestions by the same policy
    build_findings uses, so the HTML card and the ranked advise list agree."""

    def test_none_when_no_suggestions(self):
        self.assertIsNone(primary_suggestion_index({"suggestions": []}))
        self.assertIsNone(primary_suggestion_index({}))

    def test_high_severity_wins(self):
        payload = {"suggestions": [
            {"severity": "low", "category": "index", "action": "x"},
            {"severity": "high", "category": "index", "action": "y"},
        ]}
        self.assertEqual(primary_suggestion_index(payload), 1)

    def test_medium_outranks_low(self):
        # The behavior change #6 introduces: the card used to just take the
        # first suggestion; now medium outranks low, matching the ranked list.
        payload = {"suggestions": [
            {"severity": "low", "category": "index", "action": "x"},
            {"severity": "medium", "category": "index", "action": "y"},
        ]}
        self.assertEqual(primary_suggestion_index(payload), 1)

    def test_agrees_with_build_findings_on_real_plan(self):
        # On a real plan, the promoted suggestion must be the highest-ranked
        # *suggestion* in the unified build_findings order — never a suggestion
        # that build_findings ranks below another suggestion.
        payload = _payload(FULL_SCAN_FIXTURE)
        idx = primary_suggestion_index(payload)
        if idx is None:
            self.skipTest("plan has no suggestions to promote")
        picked_action = payload["suggestions"][idx].get("action", "")
        suggestion_findings = [f for f in build_findings(payload)
                               if f["kind"] == "suggestion"]
        self.assertTrue(suggestion_findings)
        self.assertEqual(suggestion_findings[0]["text"], picked_action)


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


class TestKnownTriggers(unittest.TestCase):
    """The --fail-on vocabulary validation: a misspelled trigger is rejected up
    front so `--fail-on full_scan` (valid, just unmatched on a clean plan) does
    not get silently treated the same as `--fail-on fullscan` (a typo)."""

    def test_full_scan_warn_and_any_are_all_known(self):
        known = known_triggers()
        self.assertIn("full_scan", known)   # a warning category
        self.assertIn("warn", known)        # a warning severity
        self.assertIn("any", known)         # the special catch-all

    def test_unknown_triggers_returns_only_typos_sorted(self):
        self.assertEqual(
            unknown_triggers(["full_scan", "fullscan", "filsort", "any"]),
            ["filsort", "fullscan"],
        )

    def test_unknown_triggers_empty_when_all_valid(self):
        self.assertEqual(
            unknown_triggers(["full_scan", "warn", "any"]),
            [],
        )


if __name__ == "__main__":
    unittest.main()
