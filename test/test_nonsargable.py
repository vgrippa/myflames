"""
Tests for the non-sargable join-predicate rule and responsive SVG output.

Covers:
  * ``parser._detect_nonsargable_joins`` matches against the function set
    with column references AND rejects constant-only calls.
  * ``analyze_plan`` exposes the ``nonsargable_joins`` list and emits a
    first-priority warning + suggestion pair when hits exist.
  * ``glossary._pick_primary_issue`` promotes non-sargable joins above
    every other finding.
  * ``glossary.lookup("non-sargable")`` returns the new glossary entry.
  * End-to-end fixture check on ``mariadb-explain-block-nl-join.json``
    (the live-captured fixture whose join uses ``CONVERT(CONCAT(...))``).
  * CLI integration: a generated ``.svg`` carries ``style="max-width:…"``
    and a ``viewBox`` so standalone browser viewing scales responsively.
"""
import os
import re
import subprocess
import sys
import tempfile
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_DIR)

from myflames.parser import (
    parse_explain,
    analyze_plan,
    _detect_nonsargable_joins,
    _NONSARGABLE_RE,
)
from myflames.glossary import _pick_primary_issue, lookup, generate_executive_summary


BNL_FIXTURE = os.path.join(TEST_DIR, "mariadb-explain-block-nl-join.json")
HASH_JOIN_FIXTURE = os.path.join(TEST_DIR, "mysql-explain-hash-join.json")


def _load(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Regex-level tests: _NONSARGABLE_RE recognises column-wrapping function calls
# ---------------------------------------------------------------------------

class TestNonsargableRegex(unittest.TestCase):
    """The regex must match function(column) but NOT function(literal)."""

    def _hit(self, text):
        m = _NONSARGABLE_RE.search(text)
        return m.group(1).upper() if m else None

    def test_concat_on_column(self):
        self.assertEqual(self._hit("CONCAT(o.user_id, 'x')"), "CONCAT")

    def test_concat_on_qualified_column(self):
        self.assertEqual(self._hit("concat(a.id) = concat(b.user_id)"), "CONCAT")

    def test_lower_on_column(self):
        self.assertEqual(self._hit("LOWER(u.email) = 'a@b'"), "LOWER")

    def test_date_on_column(self):
        self.assertEqual(self._hit("DATE(o.created_at)"), "DATE")

    def test_cast_on_column(self):
        self.assertEqual(self._hit("CAST(o.user_id AS CHAR)"), "CAST")

    def test_convert_on_column(self):
        self.assertEqual(self._hit("convert(t1.d using utf8mb4)"), "CONVERT")

    def test_concat_on_literal_does_not_match(self):
        """Constant-only function calls are compile-time foldable and do NOT
        break sargability, so the regex should skip them."""
        self.assertIsNone(self._hit("CONCAT('u', 'v')"))

    def test_date_on_literal_does_not_match(self):
        self.assertIsNone(self._hit("DATE('2024-01-01')"))

    def test_plain_equality_does_not_match(self):
        self.assertIsNone(self._hit("u.id = o.user_id"))

    def test_function_without_column_does_not_match(self):
        self.assertIsNone(self._hit("now() = now()"))


# ---------------------------------------------------------------------------
# _detect_nonsargable_joins tree walk
# ---------------------------------------------------------------------------

def _make_fake_node(operation="Inner hash join", access="join",
                    condition="", hash_condition=None, children=None):
    """Build a minimal node dict shaped like parser.parse_node output."""
    return {
        "short_label": operation[:40],
        "folded_label": operation,
        "full_label": operation,
        "details": {
            "operation": operation,
            "access_type": access,
            "condition": condition,
            "hash_condition": hash_condition or [],
        },
        "self_time": 0,
        "total_time": 0,
        "rows": 0,
        "loops": 1,
        "children": children or [],
    }


class TestDetectNonsargableJoins(unittest.TestCase):

    def test_detects_concat_in_hash_condition(self):
        """MySQL hash joins put the predicate in hash_condition[]."""
        root = _make_fake_node(
            operation="Inner hash join (concat('u', o.user_id) = concat('u', u.id))",
            hash_condition=["(concat('u', o.user_id) = concat('u', u.id))"],
        )
        hits = _detect_nonsargable_joins(root)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["function"], "CONCAT")

    def test_detects_cast_in_condition(self):
        root = _make_fake_node(
            operation="Nested loop inner join",
            condition="CAST(o.user_id AS CHAR) = u.id",
        )
        hits = _detect_nonsargable_joins(root)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["function"], "CAST")

    def test_no_hit_on_plain_join(self):
        root = _make_fake_node(
            operation="Nested loop inner join",
            condition="u.id = o.user_id",
        )
        self.assertEqual(_detect_nonsargable_joins(root), [])

    def test_no_hit_on_constant_only_concat(self):
        root = _make_fake_node(
            operation="Inner hash join (a = concat('x', 'y'))",
        )
        self.assertEqual(_detect_nonsargable_joins(root), [])

    def test_deduplicates_same_predicate(self):
        """If two nodes carry the same non-sargable predicate, only emit
        one hit — otherwise the warning becomes noisy."""
        inner = _make_fake_node(
            operation="Table scan [users]", access="table",
            condition="LOWER(u.email) = 'x'",
        )
        outer = _make_fake_node(
            operation="Nested loop inner join",
            condition="LOWER(u.email) = 'x'",
            children=[inner],
        )
        hits = _detect_nonsargable_joins(outer)
        # Only the join-node context counts; the outer hit is enough.
        self.assertEqual(len(hits), 1)

    def test_returns_short_label(self):
        root = _make_fake_node(
            operation="Inner hash join",
            hash_condition=["(DATE(o.created_at) = u.last_login)"],
        )
        hits = _detect_nonsargable_joins(root)
        self.assertTrue(hits[0]["short_label"])


# ---------------------------------------------------------------------------
# analyze_plan integration
# ---------------------------------------------------------------------------

class TestAnalyzePlanNonsargable(unittest.TestCase):

    def test_nonsargable_key_present(self):
        root = parse_explain(_load(HASH_JOIN_FIXTURE))
        a = analyze_plan(root)
        self.assertIn("nonsargable_joins", a)
        self.assertIsInstance(a["nonsargable_joins"], list)

    @unittest.skipUnless(os.path.exists(BNL_FIXTURE), "BNL fixture missing")
    def test_mariadb_bnl_fixture_fires_rule(self):
        """The live-captured MariaDB BNL fixture uses
        ``t1.d = convert(concat('r', t2.y) using utf8mb4)`` which is
        doubly non-sargable (CONCAT + CONVERT on the join column)."""
        root = parse_explain(_load(BNL_FIXTURE))
        a = analyze_plan(root)
        self.assertTrue(
            a["nonsargable_joins"],
            "expected non-sargable join detected on BNL fixture",
        )
        # The warning must lead the list (priority order matters).
        self.assertTrue(
            a["warnings"][0].startswith("Non-sargable join predicate"),
            "non-sargable warning should be the first warning: " + a["warnings"][0],
        )

    @unittest.skipUnless(os.path.exists(BNL_FIXTURE), "BNL fixture missing")
    def test_executive_summary_leads_with_rewrite(self):
        root = parse_explain(_load(BNL_FIXTURE))
        a = analyze_plan(root)
        summary = generate_executive_summary(root, a)
        self.assertIn("no index can be used", summary.lower())

    @unittest.skipUnless(os.path.exists(BNL_FIXTURE), "BNL fixture missing")
    def test_primary_issue_is_nonsargable(self):
        root = parse_explain(_load(BNL_FIXTURE))
        a = analyze_plan(root)
        kind, _ = _pick_primary_issue(a)
        self.assertEqual(kind, "nonsargable_join")


# ---------------------------------------------------------------------------
# Glossary entry
# ---------------------------------------------------------------------------

class TestSargableGlossaryEntry(unittest.TestCase):

    def test_lookup_sargable(self):
        entry = lookup("sargable")
        self.assertIsNotNone(entry)
        self.assertIn("function", entry["technical"].lower())

    def test_lookup_non_sargable_alias(self):
        self.assertIsNotNone(lookup("non-sargable"))
        self.assertIsNotNone(lookup("nonsargable"))

    def test_entry_has_all_tiers(self):
        entry = lookup("sargable")
        for tier in ("short", "technical", "newcomer"):
            self.assertTrue(entry.get(tier, "").strip(),
                            "sargable entry missing {}".format(tier))


# ---------------------------------------------------------------------------
# Responsive SVG output (Fix 1)
# ---------------------------------------------------------------------------

class TestResponsiveSVG(unittest.TestCase):
    """CLI-generated ``.svg`` files must carry max-width:100% + viewBox so
    standalone browser viewing scales to the viewport."""

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "myflames"] + list(args),
            cwd=REPO_DIR, capture_output=True, timeout=30,
        )

    @unittest.skipUnless(os.path.exists(HASH_JOIN_FIXTURE), "fixture missing")
    def test_flamegraph_svg_responsive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.svg")
            r = self._run("--type", "flamegraph",
                          "--no-sidecar", "-o", path, HASH_JOIN_FIXTURE)
            self.assertEqual(r.returncode, 0, r.stderr.decode())
            with open(path) as f:
                svg = f.read()
            # Match the FIRST <svg> tag (the root) and verify both attributes.
            m = re.search(r"<svg\b[^>]*>", svg)
            self.assertIsNotNone(m)
            root_tag = m.group(0)
            self.assertIn("max-width: 100%", root_tag)
            self.assertIn("viewBox", root_tag)

    @unittest.skipUnless(os.path.exists(HASH_JOIN_FIXTURE), "fixture missing")
    def test_bargraph_svg_gets_viewbox_backfilled(self):
        """The bargraph renderer doesn't emit its own viewBox — the CLI
        post-processor must backfill one from the width/height attributes
        so responsive CSS actually preserves the aspect ratio."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.svg")
            r = self._run("--type", "bargraph",
                          "--no-sidecar", "-o", path, HASH_JOIN_FIXTURE)
            self.assertEqual(r.returncode, 0, r.stderr.decode())
            with open(path) as f:
                svg = f.read()
            m = re.search(r"<svg\b[^>]*>", svg)
            self.assertIn("viewBox", m.group(0))
            self.assertIn("max-width: 100%", m.group(0))

    @unittest.skipUnless(os.path.exists(HASH_JOIN_FIXTURE), "fixture missing")
    def test_existing_style_not_clobbered(self):
        """The responsive injector must not overwrite an existing style=
        attribute — unlikely in practice, but the invariant keeps custom
        themes safe."""
        from myflames.cli import _make_svg_responsive
        original = '<svg width="100" height="50" style="background: red;">x</svg>'
        result = _make_svg_responsive(original)
        self.assertIn("background: red", result)
        # max-width should NOT be appended because a style was present
        self.assertNotIn("max-width", result)


if __name__ == "__main__":
    unittest.main()
