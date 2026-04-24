"""
Slice 1 contracts: advisor-digest goldens (P2) + MariaDB normalization
invariants (P3).

The advisor digest is intentionally *presentation-free* — it captures
only ``rule_id`` and ``severity`` per finding, so unrelated copy/UI
tweaks never churn these goldens. Any intentional rule change here is a
deliberate correctness decision (see Slice 1 M1–M5 in the plan).
"""

import glob
import json
import os
import sys
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_DIR)

from myflames.parser import parse_explain, analyze_plan
from myflames.advisor import advise


def _load_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _analysis_for(fixture_path):
    raw = _load_text(fixture_path)
    root = parse_explain(raw)
    analysis = analyze_plan(root)
    # analyze_plan returns a flat dict including query_text_lines etc.
    # Make sure fields the advisor reads exist, even as empty lists.
    for k in ("full_scans", "hash_joins", "temp_tables", "filesorts",
              "bnl_nodes", "range_scans", "index_suggestions",
              "optimizer_features", "warnings", "suggestions",
              "query_text_lines"):
        analysis.setdefault(k, [])
    return analysis


def _digest(analysis):
    """Return the presentation-free advisor digest: sorted list of
    ``{rule_id, severity}`` tuples. Suggestion prose and warning text
    are deliberately excluded."""
    findings = analysis.get("environment_findings") or []
    return sorted(
        (f["rule_id"], f["severity"]) for f in findings
    )


# ---------------------------------------------------------------------------
# P2 — advisor-digest goldens under synthetic ctx (one per rule)
# ---------------------------------------------------------------------------

# A "stress" variables bag that would force M1/M2/M5 rules to fire when
# the plan shape matches. Kept minimal so only the rule under test can
# possibly fire.
_MYSQL_STRESS_VARS = {
    "optimizer_switch":
        "hash_join=on,block_nested_loop=on,mrr=off,mrr_cost_based=off,"
        "derived_condition_pushdown=on",
    "innodb_flush_log_at_trx_commit": "1",  # safe — only the write-plan
    "sort_buffer_size": str(8 * 1024 * 1024),
    "join_buffer_size": str(8 * 1024 * 1024),
    "tmp_table_size": str(64 * 1024 * 1024),
    "max_heap_table_size": str(64 * 1024 * 1024),
    "innodb_buffer_pool_size": str(8 * 1024 * 1024 * 1024),
}

_MARIADB_STRESS_VARS = dict(_MYSQL_STRESS_VARS)
_MARIADB_STRESS_VARS["optimizer_switch"] = (
    # MariaDB-flavored switch string: the presence of join_cache_hashed
    # is what flips the advisor into MariaDB mode.
    "mrr=off,mrr_cost_based=off,"
    "join_cache_hashed=off,join_cache_bka=off,join_cache_incremental=on,"
    "derived_condition_pushdown=on"
)


class TestAdvisorDigestGoldens(unittest.TestCase):
    """Each case: (fixture, vars, description) -> expected rule_ids.

    The expected set is *deliberately small* — each case targets exactly
    one M-correction from Slice 1. A regression here means a rule
    started firing (or stopped firing) where it shouldn't.
    """

    def _run(self, fixture_rel, variables):
        path = os.path.join(TEST_DIR, "fixtures", fixture_rel)
        self.assertTrue(os.path.exists(path),
                        "fixture missing: " + fixture_rel)
        analysis = _analysis_for(path)
        advise(analysis, schema={}, stats={}, variables=variables)
        return _digest(analysis)

    # ---- M2: MRR gating -------------------------------------------------

    def test_mrr_off_with_range_scan_plan_fires(self):
        """Range-scan MySQL plan + mrr=off → MRR rule fires."""
        digest = self._run(
            "explain-011-index-range-scan-users-created.json",
            _MYSQL_STRESS_VARS,
        )
        rule_ids = [rid for rid, _ in digest]
        self.assertIn("MRR_OFF_WITH_RANGE_SCAN", rule_ids)

    def test_mrr_off_with_table_scan_only_does_not_fire(self):
        """Counter-example: plan has no range scan → MRR rule silent,
        even with mrr=off. This is the M2 correction."""
        digest = self._run(
            "explain-001-table-scan-users-no-filter.json",
            _MYSQL_STRESS_VARS,
        )
        rule_ids = [rid for rid, _ in digest]
        self.assertNotIn("MRR_OFF_WITH_RANGE_SCAN", rule_ids)

    # ---- M1: BNL advice inversion is gone ------------------------------

    def test_mysql_plan_never_suggests_hash_join_switch(self):
        """Across every MySQL fixture, the digest must never include a
        rule that recommends the no-op hash_join switch."""
        for path in sorted(glob.glob(os.path.join(
                TEST_DIR, "fixtures", "explain-*.json"))):
            analysis = _analysis_for(path)
            advise(analysis, schema={}, stats={}, variables=_MYSQL_STRESS_VARS)
            for f in analysis.get("environment_findings") or []:
                self.assertNotIn(
                    "hash_join=on", f["suggestion"],
                    "stale advice in {}: {}".format(
                        os.path.basename(path), f["suggestion"][:80]),
                )
                # And must not recommend disabling BNL (would kill the
                # executor's BNL→hash rewrite on 8.0.20+).
                self.assertNotIn(
                    "SET SESSION optimizer_switch='hash_join=on,"
                    "block_nested_loop=off'",
                    f["suggestion"],
                )


# ---------------------------------------------------------------------------
# P3 — MariaDB normalization invariants
# ---------------------------------------------------------------------------

def _walk(node, visit):
    visit(node)
    for child in node.get("children") or []:
        _walk(child, visit)


class TestMariaDBNormalizationInvariants(unittest.TestCase):
    """Shape guarantees every normalized MariaDB tree must satisfy.

    If these go red, the parser's _normalize_mariadb_* functions
    produced a tree that the renderers / advisor cannot read uniformly.
    """

    FIXTURES = sorted(
        glob.glob(os.path.join(TEST_DIR, "fixtures", "mariadb-*.json"))
    )

    def test_mariadb_fixtures_exist(self):
        self.assertGreater(
            len(self.FIXTURES), 0,
            "expected MariaDB fixtures under test/fixtures/mariadb-*.json",
        )

    def test_every_node_has_a_short_label(self):
        """No nameless nodes — renderers rely on short_label to draw a bar."""
        missing = []
        for path in self.FIXTURES:
            root = parse_explain(_load_text(path))

            def visit(n, _path=path):
                if not n.get("short_label"):
                    missing.append((os.path.basename(_path),
                                    n.get("details", {}).get("operation")))
            _walk(root, visit)
        self.assertEqual(missing, [])

    def test_every_node_has_rows_numeric(self):
        """rows must be an int / float (renderers do arithmetic on it)."""
        bad = []
        for path in self.FIXTURES:
            root = parse_explain(_load_text(path))

            def visit(n, _path=path):
                r = n.get("rows", 0)
                if not isinstance(r, (int, float)):
                    bad.append((os.path.basename(_path), type(r).__name__))
            _walk(root, visit)
        self.assertEqual(bad, [])

    def test_range_checked_wrapper_never_collapses_to_noop(self):
        """If any fixture contains range-checked-for-each-record in its
        raw JSON, the normalized tree must surface it (via the new M4
        ``mariadb_range_checked`` annotation) — the wrapper must not
        silently disappear."""
        for path in self.FIXTURES:
            raw_text = _load_text(path)
            if "range-checked-for-each-record" not in raw_text:
                continue
            root = parse_explain(_load_text(path))
            found = []
            _walk(root, lambda n: found.append(
                (n.get("details") or {}).get("range_checked_per_record")))
            self.assertTrue(
                any(found),
                "{} mentions range-checked-for-each-record but normalized "
                "tree dropped the annotation".format(os.path.basename(path)),
            )


# ---------------------------------------------------------------------------
# Slice 2 — node_id stability + schema backbone
# ---------------------------------------------------------------------------

class TestNodeIdStability(unittest.TestCase):
    """node_id is the Slice 2 primitive. It must be:
      - deterministic (same input → same ids across re-parses)
      - shaped like ``n:<12 hex chars>``
      - present on every tree node including children
    """

    def _fixtures(self):
        mysql = sorted(glob.glob(os.path.join(
            TEST_DIR, "fixtures", "explain-*.json")))
        mariadb = sorted(glob.glob(os.path.join(
            TEST_DIR, "fixtures", "mariadb-*.json")))
        # Smoke a representative subset to keep the suite fast.
        return (mysql[:20] + mariadb[:20])

    def test_ids_are_deterministic_across_runs(self):
        for path in self._fixtures():
            r1 = parse_explain(_load_text(path))
            r2 = parse_explain(_load_text(path))
            ids1, ids2 = [], []
            _walk(r1, lambda n: ids1.append(n.get("node_id")))
            _walk(r2, lambda n: ids2.append(n.get("node_id")))
            self.assertEqual(
                ids1, ids2,
                "node_id drifted across re-parses for " + os.path.basename(path),
            )

    def test_every_node_has_a_valid_node_id(self):
        import re as _re
        pat = _re.compile(r"^n:[0-9a-f]{12}$")
        for path in self._fixtures():
            root = parse_explain(_load_text(path))

            def visit(n, _path=path):
                nid = n.get("node_id") or ""
                self.assertTrue(
                    pat.match(nid),
                    "bad node_id {!r} in {}".format(
                        nid, os.path.basename(_path)),
                )
            _walk(root, visit)


class TestSidecarSchemaBackbone(unittest.TestCase):
    """Slice 2 / S1: every sidecar carries $schema + plan_tree, and
    operator_complexities entries carry node_id."""

    def _build_sidecar_for(self, fixture_rel):
        from myflames.output_sidecar import build_sidecar
        path = os.path.join(TEST_DIR, "fixtures", fixture_rel)
        analysis = _analysis_for(path)
        return build_sidecar(
            root=parse_explain(_load_text(path)),
            analysis=analysis,
            source_type="file",
            engine="mysql",
            fixture_path=path,
        )

    def test_payload_has_schema_url(self):
        s = self._build_sidecar_for(
            "explain-011-index-range-scan-users-created.json")
        self.assertEqual(s.get("schema_version"), "1.3")
        self.assertIn("$schema", s)
        self.assertTrue(s["$schema"].startswith("https://"))

    def test_plan_tree_is_present_and_has_ids(self):
        s = self._build_sidecar_for(
            "explain-011-index-range-scan-users-created.json")
        tree = s.get("plan_tree")
        self.assertIsNotNone(tree, "plan_tree missing")
        self.assertIn("node_id", tree)
        self.assertTrue(tree["node_id"].startswith("n:"))

    def test_operator_complexities_reference_node_id(self):
        s = self._build_sidecar_for(
            "explain-011-index-range-scan-users-created.json")
        entries = s.get("operator_complexities") or []
        # Every entry must have a non-empty node_id.
        for e in entries:
            self.assertIn("node_id", e)
            self.assertTrue(
                e["node_id"].startswith("n:"),
                "operator_complexity entry missing node_id: " + str(e)[:120],
            )


if __name__ == "__main__":
    unittest.main()
