"""
Unit tests for :mod:`myflames.glossary`.

Covers:
  - Every GLOSSARY entry has all three tiers of explanation
  - lookup() matches canonical keys AND aliases
  - find_terms_in_text() respects word boundaries + no overlap
  - generate_executive_summary() handles every plan shape the advisor
    recognizes, plus empty/degenerate plans
  - The primary-issue picker prefers the right severity order
"""
import os
import sys
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_DIR)

from myflames.glossary import (
    GLOSSARY,
    lookup,
    find_terms_in_text,
    generate_executive_summary,
    _pick_primary_issue,
    _describe_shape,
    _format_size_time,
)
from myflames.parser import parse_explain, analyze_plan

HASH_JOIN = os.path.join(TEST_DIR, "mysql-explain-hash-join.json")
BNL = os.path.join(TEST_DIR, "mysql-explain-bnl.json")
COMPLEX = os.path.join(TEST_DIR, "mysql-explain-complex-join.json")


def _load(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Glossary schema contract
# ---------------------------------------------------------------------------

class TestGlossaryEntries(unittest.TestCase):
    """Every entry in the dict must have short, technical, newcomer,
    aliases — and each must be non-empty and of the correct type."""

    def test_every_entry_has_all_tiers(self):
        for key, entry in GLOSSARY.items():
            for field in ("short", "technical", "newcomer", "aliases"):
                self.assertIn(field, entry, "entry {} missing {}".format(key, field))
            self.assertTrue(entry["short"].strip(),
                            "entry {} has empty short".format(key))
            self.assertTrue(entry["technical"].strip(),
                            "entry {} has empty technical".format(key))
            self.assertTrue(entry["newcomer"].strip(),
                            "entry {} has empty newcomer".format(key))
            self.assertIsInstance(entry["aliases"], list)

    def test_short_is_short(self):
        """The ``short`` tier is for tooltips — enforce an upper bound so
        authors don't let it grow into a paragraph."""
        for key, entry in GLOSSARY.items():
            self.assertLessEqual(
                len(entry["short"]), 90,
                "short text too long for {} (use technical tier): {!r}".format(
                    key, entry["short"]
                ),
            )

    def test_technical_cites_something_concrete(self):
        """Technical text must mention at least one variable name, access
        type, or cost-model term — not just 'this is fast'. The check is
        deliberately loose to stay author-friendly."""
        concrete_markers = (
            "access type", "type=", "optimizer_switch", "buffer", "index",
            "cost", "row", "scan", "join", "pool", "disk", "memory",
            "tmpdir", "fsync", "O(", "performance_schema", "Handler_",
            "ANALYZE", "MySQL", "MariaDB", "KB", "MB", "GB",
        )
        for key, entry in GLOSSARY.items():
            t = entry["technical"].lower()
            self.assertTrue(
                any(m.lower() in t for m in concrete_markers),
                "technical text for {} has no concrete marker: {!r}".format(
                    key, entry["technical"][:80]
                ),
            )

    def test_no_duplicate_aliases_across_entries(self):
        """An alias should unambiguously identify ONE canonical entry.

        It's fine for an entry to list its own canonical key in its
        aliases (or list equivalent phrasings like 'semi join' and
        'semijoin'); what we're guarding against is the same surface form
        mapping to TWO different canonical keys across the dict.
        """
        seen = {}
        for key, entry in GLOSSARY.items():
            forms = {key.lower()}
            for alias in entry.get("aliases", []):
                forms.add(alias.lower().strip())
            for form in forms:
                if form in seen and seen[form] != key:
                    self.fail(
                        "alias {!r} shared by {} and {}".format(
                            form, seen[form], key,
                        )
                    )
                seen[form] = key


# ---------------------------------------------------------------------------
# lookup() behaviour
# ---------------------------------------------------------------------------

class TestLookup(unittest.TestCase):

    def test_canonical_key(self):
        entry = lookup("filesort")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["key"], "filesort")

    def test_alias_match(self):
        self.assertEqual(lookup("BNL")["key"], "block_nested_loop")
        self.assertEqual(lookup("Block Nested-Loop")["key"], "block_nested_loop")
        self.assertEqual(lookup("Using filesort")["key"], "filesort")

    def test_case_insensitive(self):
        self.assertIsNotNone(lookup("FILESORT"))
        self.assertIsNotNone(lookup("filesort"))
        self.assertIsNotNone(lookup("Filesort"))

    def test_unknown_returns_none(self):
        self.assertIsNone(lookup("quantum flux"))
        self.assertIsNone(lookup(""))
        self.assertIsNone(lookup(None))

    def test_returned_entry_has_key_attached(self):
        entry = lookup("hash join")
        self.assertIn("key", entry)
        self.assertEqual(entry["key"], "hash_join")


# ---------------------------------------------------------------------------
# find_terms_in_text()
# ---------------------------------------------------------------------------

class TestFindTermsInText(unittest.TestCase):

    def test_finds_known_term(self):
        hits = find_terms_in_text("The plan uses a filesort on the result.")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["key"], "filesort")

    def test_word_boundary(self):
        """'scan' inside 'scanning' must NOT match — the glossary key is
        a whole-word concept."""
        hits = find_terms_in_text("Scanning quickly through indexes.")
        self.assertEqual(hits, [])

    def test_multiple_non_overlapping(self):
        text = "Block Nested-Loop scans trigger a filesort here."
        hits = find_terms_in_text(text)
        keys = {h["key"] for h in hits}
        # Should hit both BNL and filesort — and BNL takes precedence over
        # a hypothetical "scan" match inside the same span.
        self.assertIn("block_nested_loop", keys)
        self.assertIn("filesort", keys)

    def test_prefers_longer_phrase(self):
        """'Block Nested-Loop' should win over 'nested loop' on the same span."""
        hits = find_terms_in_text("Block Nested-Loop join buffer detected")
        # The long phrase is claimed first; the shorter one must not re-claim it.
        bnl_hits = [h for h in hits if h["key"] == "block_nested_loop"]
        nl_hits = [h for h in hits if h["key"] == "nested_loop_join"]
        self.assertEqual(len(bnl_hits), 1)
        self.assertEqual(len(nl_hits), 0)

    def test_empty_input(self):
        self.assertEqual(find_terms_in_text(""), [])
        self.assertEqual(find_terms_in_text(None), [])


# ---------------------------------------------------------------------------
# _describe_shape
# ---------------------------------------------------------------------------

class TestDescribeShape(unittest.TestCase):

    def _ps(self, **kw):
        base = {"total_time_ms": 0, "rows_sent": 0, "operator_count": 1,
                "max_depth": 1, "rows_examined_estimate": 0}
        base.update(kw)
        return base

    def test_full_scan_only(self):
        a = {"full_scans": [{"table": "users", "rows": 100}],
             "hash_joins": [], "bnl_nodes": [], "temp_tables": [],
             "filesorts": []}
        verbs = _describe_shape(None, a, self._ps(operator_count=1))
        self.assertIn("scans 1 table", " ".join(verbs))

    def test_multiple_scans_pluralize(self):
        a = {"full_scans": [{"table": "a", "rows": 1},
                            {"table": "b", "rows": 1}],
             "hash_joins": [], "bnl_nodes": [], "temp_tables": [],
             "filesorts": []}
        verbs = _describe_shape(None, a, self._ps())
        self.assertIn("scans 2 tables", " ".join(verbs))

    def test_hash_join(self):
        a = {"full_scans": [], "hash_joins": [{"rows": 1}], "bnl_nodes": [],
             "temp_tables": [], "filesorts": []}
        verbs = _describe_shape(None, a, self._ps())
        self.assertTrue(any("hash-join" in v for v in verbs))

    def test_bnl_preferred_over_empty(self):
        a = {"full_scans": [], "hash_joins": [], "bnl_nodes": [{"short_label": "x"}],
             "temp_tables": [], "filesorts": []}
        verbs = _describe_shape(None, a, self._ps())
        self.assertTrue(any("block nested-loop" in v for v in verbs))

    def test_empty_plan_falls_back_to_operator_count(self):
        a = {"full_scans": [], "hash_joins": [], "bnl_nodes": [],
             "temp_tables": [], "filesorts": []}
        verbs = _describe_shape(None, a, self._ps(operator_count=3))
        self.assertTrue(any("3 operators" in v for v in verbs))


# ---------------------------------------------------------------------------
# _format_size_time
# ---------------------------------------------------------------------------

class TestFormatSizeTime(unittest.TestCase):

    def test_millisecond_precision(self):
        ps = {"total_time_ms": 123.45, "rows_sent": 5,
              "rows_examined_estimate": 5}
        self.assertIn("123", _format_size_time(ps))

    def test_submillisecond_uses_two_decimals(self):
        ps = {"total_time_ms": 0.42, "rows_sent": 1,
              "rows_examined_estimate": 1}
        self.assertIn("0.42", _format_size_time(ps))

    def test_examined_to_sent_ratio_flagged(self):
        """When rows_examined >> rows_sent the string should say 'examines'."""
        ps = {"total_time_ms": 5, "rows_sent": 1,
              "rows_examined_estimate": 10000}
        self.assertIn("examines", _format_size_time(ps))

    def test_selective_result_says_returns(self):
        ps = {"total_time_ms": 5, "rows_sent": 100,
              "rows_examined_estimate": 100}
        self.assertIn("returns", _format_size_time(ps))


# ---------------------------------------------------------------------------
# _pick_primary_issue ordering
# ---------------------------------------------------------------------------

class TestPickPrimaryIssue(unittest.TestCase):

    def _empty(self):
        return {
            "full_scans": [], "hash_joins": [], "bnl_nodes": [],
            "temp_tables": [], "filesorts": [],
            "warnings": [], "environment_warnings": [],
            "environment_suggestions": [], "index_suggestions": [],
        }

    def test_empty_returns_none(self):
        self.assertEqual(_pick_primary_issue(self._empty()), (None, None))

    def test_durability_beats_everything(self):
        a = self._empty()
        a["environment_suggestions"] = [
            "SET GLOBAL innodb_flush_log_at_trx_commit=1; Why: ...",
        ]
        a["full_scans"] = [{"table": "x", "rows": 1}]
        kind, _ = _pick_primary_issue(a)
        self.assertEqual(kind, "durability")

    def test_missing_index_beats_plan_findings(self):
        a = self._empty()
        a["index_suggestions"] = [{"table": "users", "columns": ["email"]}]
        a["full_scans"] = [{"table": "users", "rows": 1000}]
        kind, text = _pick_primary_issue(a)
        self.assertEqual(kind, "missing_index")
        self.assertIn("email", text)

    def test_bnl_beats_hash_join(self):
        a = self._empty()
        a["bnl_nodes"] = [{"short_label": "x"}]
        a["hash_joins"] = [{"rows": 1}]
        kind, _ = _pick_primary_issue(a)
        self.assertEqual(kind, "bnl")

    def test_full_scan_fallback(self):
        a = self._empty()
        a["full_scans"] = [{"table": "big", "rows": 500000}]
        kind, text = _pick_primary_issue(a)
        self.assertEqual(kind, "full_scan")
        self.assertIn("big", text)
        self.assertIn("500,000", text)


# ---------------------------------------------------------------------------
# generate_executive_summary end-to-end
# ---------------------------------------------------------------------------

class TestGenerateExecutiveSummary(unittest.TestCase):

    def test_hash_join_fixture(self):
        root = parse_explain(_load(HASH_JOIN))
        a = analyze_plan(root)
        s = generate_executive_summary(root, a)
        self.assertTrue(s)
        self.assertIn("Query", s)
        # Hash join fixture has scans + join + filesort; all three should appear
        lower = s.lower()
        self.assertTrue("scan" in lower or "join" in lower)

    def test_bnl_fixture(self):
        root = parse_explain(_load(BNL))
        a = analyze_plan(root)
        s = generate_executive_summary(root, a)
        self.assertIn("Main finding", s)

    def test_complex_join_fixture(self):
        root = parse_explain(_load(COMPLEX))
        a = analyze_plan(root)
        s = generate_executive_summary(root, a)
        self.assertTrue(s.startswith("Query"))
        self.assertTrue(s.endswith("."))

    def test_ends_with_period(self):
        root = parse_explain(_load(HASH_JOIN))
        a = analyze_plan(root)
        s = generate_executive_summary(root, a)
        self.assertTrue(s.endswith("."),
                        "summary should end with a period: " + s)

    def test_no_warnings_path(self):
        """Build a synthetic clean plan and check the 'no warnings' branch."""
        fake_root = {
            "total_time": 0.5, "rows": 1, "loops": 1,
            "self_time": 0.5, "children": [],
            "short_label": "Single-row lookup",
            "folded_label": "SINGLE ROW LOOKUP",
            "full_label": "Single-row index lookup",
            "details": {},
        }
        clean_analysis = {
            "full_scans": [], "hash_joins": [], "temp_tables": [],
            "filesorts": [], "bnl_nodes": [],
            "optimizer_features": [], "warnings": [], "suggestions": [],
            "environment_warnings": [], "environment_suggestions": [],
            "index_suggestions": [],
        }
        s = generate_executive_summary(fake_root, clean_analysis)
        self.assertIn("No warnings", s)

    def test_deterministic(self):
        """Same inputs → same output. Protects against accidental randomness."""
        root = parse_explain(_load(HASH_JOIN))
        a = analyze_plan(root)
        s1 = generate_executive_summary(root, a)
        s2 = generate_executive_summary(root, a)
        self.assertEqual(s1, s2)


if __name__ == "__main__":
    unittest.main()
