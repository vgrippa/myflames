"""
Unit tests for :mod:`myflames.output_sidecar`.

Covers:
  - Shape contract (all required top-level keys, enum discipline)
  - Validation (every known failure mode raises SidecarValidationError)
  - Classifiers (warning + suggestion heuristics, action/why splitting)
  - Roundtrip (build → write → load → semantic equality)
  - CLI integration (auto-emit next to --output, --sidecar PATH, --no-sidecar)
  - Real fixture end-to-end (parses a hash-join plan, produces a sidecar
    that contains every expected section)
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_DIR)

from myflames.output_sidecar import (
    SCHEMA_VERSION,
    SidecarValidationError,
    build_sidecar,
    validate_sidecar,
    write_sidecar,
    load_sidecar,
    sidecar_path_for,
    _classify_plan_warning,
    _classify_suggestion,
    _split_action_why,
    _compute_plan_summary,
    _pick_primary_action,
)
# Executive summary moved to glossary.py (mysql-expert skill owns the
# domain knowledge; sidecar just calls it as a dependency).
from myflames.glossary import generate_executive_summary as _executive_summary
from myflames.parser import parse_explain, analyze_plan
from myflames.advisor import advise


HASH_JOIN = os.path.join(TEST_DIR, "mysql-explain-hash-join.json")
COMPLEX = os.path.join(TEST_DIR, "mysql-explain-complex-join.json")


def _load_fixture(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Classifiers
# ---------------------------------------------------------------------------

class TestClassifyWarning(unittest.TestCase):

    def test_full_scan(self):
        self.assertEqual(
            _classify_plan_warning("Full table scan: users (3000 rows)"),
            ("warn", "full_scan"),
        )

    def test_hash_join(self):
        self.assertEqual(
            _classify_plan_warning("1 hash join(s) — uses join_buffer_size"),
            ("warn", "hash_join"),
        )

    def test_bnl(self):
        sev, cat = _classify_plan_warning(
            "Block Nested-Loop (BNL) join buffer detected — uses join_buffer_size"
        )
        self.assertEqual((sev, cat), ("warn", "bnl"))

    def test_temp_table(self):
        self.assertEqual(
            _classify_plan_warning("1 temp table(s) (Materialize) — may spill to disk"),
            ("warn", "temp_table"),
        )

    def test_filesort(self):
        self.assertEqual(
            _classify_plan_warning("1 sort operation(s) — 1000 rows"),
            ("warn", "filesort"),
        )

    def test_unknown_falls_back_to_other(self):
        self.assertEqual(
            _classify_plan_warning("Something weird"),
            ("warn", "other"),
        )


class TestClassifySuggestion(unittest.TestCase):

    def test_buffer_pool_is_high(self):
        sev, cat, var = _classify_suggestion(
            "Raise innodb_buffer_pool_size to at least 2.0 GB ..."
        )
        self.assertEqual(sev, "high")
        self.assertEqual(cat, "tuning_variable")
        self.assertEqual(var, "innodb_buffer_pool_size")

    def test_sort_buffer_is_medium(self):
        sev, cat, var = _classify_suggestion("Raise sort_buffer_size to 2M–8M ...")
        self.assertEqual(sev, "medium")
        self.assertEqual(cat, "tuning_variable")
        self.assertEqual(var, "sort_buffer_size")

    def test_create_index_is_high(self):
        sev, cat, var = _classify_suggestion("CREATE INDEX idx_foo ON users (email);")
        self.assertEqual(sev, "high")
        self.assertEqual(cat, "index")
        self.assertIsNone(var)

    def test_engine_alter(self):
        sev, cat, _ = _classify_suggestion("ALTER TABLE t1 ENGINE=InnoDB; Why: ...")
        self.assertEqual(sev, "high")
        self.assertEqual(cat, "engine")

    def test_optimizer_switch(self):
        sev, cat, _ = _classify_suggestion(
            "SET SESSION optimizer_switch='hash_join=on'; Why: ..."
        )
        self.assertEqual(cat, "optimizer_switch")

    def test_flush_log_is_durability(self):
        sev, cat, var = _classify_suggestion(
            "SET GLOBAL innodb_flush_log_at_trx_commit=1;"
        )
        self.assertEqual(sev, "high")
        self.assertEqual(cat, "durability")
        self.assertEqual(var, "innodb_flush_log_at_trx_commit")

    def test_unknown_is_low_other(self):
        self.assertEqual(
            _classify_suggestion("Something vague"),
            ("low", "other", None),
        )


class TestSplitActionWhy(unittest.TestCase):

    def test_splits_on_why_marker(self):
        action, why = _split_action_why(
            "Raise sort_buffer_size to 2M–8M. Why: when the sort set does not fit, "
            "MySQL writes sorted runs to tmpdir."
        )
        self.assertIn("Raise sort_buffer_size", action)
        self.assertIn("when the sort set", why)

    def test_missing_why_keeps_full_text(self):
        action, why = _split_action_why("Add an index on (user_id).")
        self.assertEqual(action, "Add an index on (user_id).")
        self.assertEqual(why, "")

    def test_empty_input(self):
        self.assertEqual(_split_action_why(""), ("", ""))
        self.assertEqual(_split_action_why(None), ("", ""))

    def test_case_insensitive(self):
        action, why = _split_action_why("Do X. WHY: because Y.")
        self.assertEqual(action.rstrip("."), "Do X")
        self.assertEqual(why, "because Y.")


# ---------------------------------------------------------------------------
# Plan summary + executive summary + primary action
# ---------------------------------------------------------------------------

class TestPlanSummary(unittest.TestCase):

    def test_all_numeric(self):
        root = parse_explain(_load_fixture(HASH_JOIN))
        ps = _compute_plan_summary(root)
        for k in ("total_time_ms", "rows_sent", "rows_examined_estimate",
                  "operator_count", "max_depth"):
            self.assertIn(k, ps)
            self.assertIsInstance(ps[k], (int, float))

    def test_operator_count_positive(self):
        root = parse_explain(_load_fixture(HASH_JOIN))
        ps = _compute_plan_summary(root)
        self.assertGreater(ps["operator_count"], 0)
        self.assertGreater(ps["max_depth"], 0)


class TestExecutiveSummary(unittest.TestCase):
    """Spot-checks on glossary.generate_executive_summary wired through
    build_sidecar — full coverage lives in test_glossary.py."""

    def test_non_empty_for_real_plan(self):
        root = parse_explain(_load_fixture(HASH_JOIN))
        a = analyze_plan(root)
        payload = build_sidecar(root, a, source_type="file", engine="mysql")
        self.assertTrue(payload["executive_summary"])
        self.assertIsInstance(payload["executive_summary"], str)

    def test_mentions_shape_and_finding(self):
        root = parse_explain(_load_fixture(HASH_JOIN))
        a = analyze_plan(root)
        payload = build_sidecar(root, a, source_type="file", engine="mysql")
        summary = payload["executive_summary"].lower()
        # Hash join fixture → summary should mention the plan shape AND
        # surface a "Main finding" line.
        self.assertTrue("hash" in summary or "join" in summary or "scan" in summary)
        self.assertIn("main finding", summary)


class TestPickPrimaryAction(unittest.TestCase):

    def test_empty_returns_none(self):
        self.assertIsNone(_pick_primary_action([]))

    def test_prefers_high_severity(self):
        suggestions = [
            {"severity": "low", "action": "x"},
            {"severity": "high", "action": "y"},
            {"severity": "medium", "action": "z"},
        ]
        self.assertEqual(_pick_primary_action(suggestions), 1)

    def test_falls_back_to_first(self):
        suggestions = [
            {"severity": "low", "action": "x"},
            {"severity": "medium", "action": "y"},
        ]
        self.assertEqual(_pick_primary_action(suggestions), 0)


# ---------------------------------------------------------------------------
# build_sidecar end-to-end on real fixtures
# ---------------------------------------------------------------------------

class TestBuildSidecarRealFixtures(unittest.TestCase):

    def test_hash_join_fixture(self):
        root = parse_explain(_load_fixture(HASH_JOIN))
        a = analyze_plan(root)
        payload = build_sidecar(root, a, source_type="file", engine="mysql",
                                fixture_path=HASH_JOIN)
        # Top-level invariants
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(payload["source"]["type"], "file")
        self.assertEqual(payload["source"]["engine"], "mysql")
        # Hash join plan must yield a hash_join optimizer switch entry
        sw_names = [s["name"] for s in payload["optimizer_switches"]]
        self.assertIn("hash_join", sw_names)
        # At least one warning and one suggestion for this plan
        self.assertGreater(len(payload["warnings"]), 0)
        self.assertGreater(len(payload["suggestions"]), 0)
        # Every suggestion has severity + category + source
        for s in payload["suggestions"]:
            self.assertIn(s["severity"], ("high", "medium", "low"))
            self.assertIn(s["source"], ("plan", "environment"))
            self.assertTrue(s["action"])

    def test_complex_fixture(self):
        root = parse_explain(_load_fixture(COMPLEX))
        a = analyze_plan(root)
        payload = build_sidecar(root, a, source_type="file", engine="mysql",
                                fixture_path=COMPLEX)
        validate_sidecar(payload)
        self.assertGreater(payload["plan_summary"]["operator_count"], 2)

    def test_no_query_metadata_omitted(self):
        root = parse_explain(_load_fixture(HASH_JOIN))
        a = analyze_plan(root)
        payload = build_sidecar(root, a, source_type="file", engine="mysql")
        # When query_raw/query_beautified are None, the key is omitted
        # entirely (not set to null or empty).
        self.assertNotIn("query", payload)

    def test_query_metadata_included(self):
        root = parse_explain(_load_fixture(HASH_JOIN))
        a = analyze_plan(root)
        payload = build_sidecar(
            root, a, source_type="file", engine="mysql",
            query_raw="SELECT * FROM t",
            query_beautified="SELECT *\nFROM t",
        )
        self.assertIn("query", payload)
        self.assertEqual(payload["query"]["raw"], "SELECT * FROM t")
        self.assertIn("\n", payload["query"]["beautified"])

    def test_primary_action_refs_existing_suggestion(self):
        root = parse_explain(_load_fixture(HASH_JOIN))
        a = analyze_plan(root)
        payload = build_sidecar(root, a, source_type="file", engine="mysql")
        ref = payload.get("primary_action", {}).get("ref", "")
        if ref:
            # ref format: "suggestions[N]"
            import re
            m = re.match(r"suggestions\[(\d+)\]", ref)
            self.assertIsNotNone(m)
            idx = int(m.group(1))
            self.assertLess(idx, len(payload["suggestions"]))

    def test_live_mode_collected_section(self):
        """When advise() populated the analysis dict, the sidecar surfaces
        collected_variables/stats/schema under the ``collected`` key."""
        root = parse_explain(_load_fixture(HASH_JOIN))
        a = analyze_plan(root)
        advise(a,
               schema={"users": {"engine": "InnoDB", "indexes": []}},
               stats={"users": {"table_rows": 100, "data_length": 4096,
                                "index_length": 2048}},
               variables={"innodb_buffer_pool_size": "134217728",
                          "sort_buffer_size": "262144"})
        payload = build_sidecar(root, a, source_type="live",
                                engine="mysql", engine_version="8.4.8")
        self.assertEqual(payload["source"]["type"], "live")
        self.assertEqual(payload["source"]["engine_version"], "8.4.8")
        self.assertIn("collected", payload)
        self.assertIn("variables", payload["collected"])
        self.assertIn("innodb_buffer_pool_size", payload["collected"]["variables"])


# ---------------------------------------------------------------------------
# validate_sidecar negative tests
# ---------------------------------------------------------------------------

class TestValidateSidecarNegative(unittest.TestCase):
    """Every validation branch must raise — protects the schema contract."""

    def _minimal(self):
        """Return the smallest valid payload we can build without fixtures."""
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": "2026-04-10T15:00:00Z",
            "myflames_version": "1.2.0",
            "source": {"type": "file"},
            "plan_summary": {
                "total_time_ms": 0,
                "rows_sent": 0,
                "rows_examined_estimate": 0,
                "operator_count": 1,
                "max_depth": 1,
            },
            "optimizer_switches": [],
            "warnings": [],
            "suggestions": [],
            "executive_summary": "Empty plan.",
        }

    def test_minimal_validates(self):
        validate_sidecar(self._minimal())  # should not raise

    def test_non_dict_raises(self):
        with self.assertRaises(SidecarValidationError):
            validate_sidecar([])

    def test_missing_schema_version(self):
        p = self._minimal(); del p["schema_version"]
        with self.assertRaises(SidecarValidationError):
            validate_sidecar(p)

    def test_wrong_schema_version(self):
        p = self._minimal(); p["schema_version"] = "99.0"
        with self.assertRaises(SidecarValidationError):
            validate_sidecar(p)

    def test_bad_source_type(self):
        p = self._minimal(); p["source"]["type"] = "magic"
        with self.assertRaises(SidecarValidationError):
            validate_sidecar(p)

    def test_bad_engine(self):
        p = self._minimal(); p["source"]["engine"] = "postgres"
        with self.assertRaises(SidecarValidationError):
            validate_sidecar(p)

    def test_plan_summary_missing_field(self):
        p = self._minimal(); del p["plan_summary"]["total_time_ms"]
        with self.assertRaises(SidecarValidationError):
            validate_sidecar(p)

    def test_plan_summary_non_numeric(self):
        p = self._minimal(); p["plan_summary"]["total_time_ms"] = "fast"
        with self.assertRaises(SidecarValidationError):
            validate_sidecar(p)

    def test_warning_bad_severity(self):
        p = self._minimal()
        p["warnings"] = [{"severity": "scary", "category": "full_scan",
                          "text": "x", "source": "plan"}]
        with self.assertRaises(SidecarValidationError):
            validate_sidecar(p)

    def test_warning_bad_category(self):
        p = self._minimal()
        p["warnings"] = [{"severity": "warn", "category": "bikeshed",
                          "text": "x", "source": "plan"}]
        with self.assertRaises(SidecarValidationError):
            validate_sidecar(p)

    def test_warning_missing_text(self):
        p = self._minimal()
        p["warnings"] = [{"severity": "warn", "category": "full_scan",
                          "source": "plan"}]
        with self.assertRaises(SidecarValidationError):
            validate_sidecar(p)

    def test_suggestion_missing_action(self):
        p = self._minimal()
        p["suggestions"] = [{"severity": "high", "category": "index",
                             "source": "plan"}]
        with self.assertRaises(SidecarValidationError):
            validate_sidecar(p)

    def test_suggestion_bad_severity(self):
        p = self._minimal()
        p["suggestions"] = [{"severity": "urgent", "category": "index",
                             "action": "x", "source": "plan"}]
        with self.assertRaises(SidecarValidationError):
            validate_sidecar(p)

    def test_empty_executive_summary(self):
        p = self._minimal(); p["executive_summary"] = ""
        with self.assertRaises(SidecarValidationError):
            validate_sidecar(p)


# ---------------------------------------------------------------------------
# write_sidecar / load_sidecar roundtrip
# ---------------------------------------------------------------------------

class TestSidecarRoundtrip(unittest.TestCase):

    def test_write_then_load_equal(self):
        root = parse_explain(_load_fixture(HASH_JOIN))
        a = analyze_plan(root)
        payload = build_sidecar(root, a, source_type="file",
                                engine="mysql", fixture_path=HASH_JOIN)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
            path = tf.name
        try:
            write_sidecar(path, payload)
            loaded = load_sidecar(path)
            # Semantic equality: every non-timestamp key should survive.
            payload.pop("generated_at")
            loaded.pop("generated_at")
            self.assertEqual(payload, loaded)
        finally:
            os.unlink(path)

    def test_written_file_is_valid_json(self):
        root = parse_explain(_load_fixture(HASH_JOIN))
        a = analyze_plan(root)
        payload = build_sidecar(root, a, source_type="file", engine="mysql")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
            path = tf.name
        try:
            write_sidecar(path, payload)
            with open(path) as f:
                reparsed = json.load(f)
            self.assertEqual(reparsed["schema_version"], SCHEMA_VERSION)
        finally:
            os.unlink(path)


class TestSidecarPathFor(unittest.TestCase):

    def test_svg_extension(self):
        self.assertEqual(sidecar_path_for("docs/demos/foo.svg"),
                         "docs/demos/foo.json")

    def test_html_extension(self):
        self.assertEqual(sidecar_path_for("x.html"), "x.json")

    def test_no_extension_appends(self):
        self.assertEqual(sidecar_path_for("/tmp/plan"), "/tmp/plan.json")

    def test_none_returns_none(self):
        self.assertIsNone(sidecar_path_for(None))

    def test_empty_returns_none(self):
        self.assertIsNone(sidecar_path_for(""))


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------

class TestSidecarCLIIntegration(unittest.TestCase):
    """End-to-end: run the myflames CLI and inspect the emitted sidecar."""

    def _run_cli(self, *args):
        cmd = [sys.executable, "-m", "myflames"] + list(args)
        return subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=REPO_DIR, timeout=30,
        )

    def test_auto_emits_next_to_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            svg = os.path.join(tmp, "out.svg")
            r = self._run_cli("--type", "bargraph",
                              "-o", svg, HASH_JOIN)
            self.assertEqual(r.returncode, 0,
                             "cli failed: " + r.stderr.decode())
            sidecar = os.path.join(tmp, "out.json")
            self.assertTrue(os.path.exists(sidecar),
                            "expected sidecar at " + sidecar)
            with open(sidecar) as f:
                data = json.load(f)
            self.assertEqual(data["schema_version"], SCHEMA_VERSION)
            self.assertEqual(data["source"]["type"], "file")
            self.assertEqual(data["source"]["engine"], "mysql")

    def test_no_sidecar_flag_suppresses(self):
        with tempfile.TemporaryDirectory() as tmp:
            svg = os.path.join(tmp, "out.svg")
            r = self._run_cli("--type", "bargraph", "--no-sidecar",
                              "-o", svg, HASH_JOIN)
            self.assertEqual(r.returncode, 0)
            sidecar = os.path.join(tmp, "out.json")
            self.assertFalse(os.path.exists(sidecar))

    def test_explicit_sidecar_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            svg = os.path.join(tmp, "out.svg")
            custom = os.path.join(tmp, "custom.json")
            r = self._run_cli("--type", "bargraph",
                              "--sidecar", custom,
                              "-o", svg, HASH_JOIN)
            self.assertEqual(r.returncode, 0)
            self.assertTrue(os.path.exists(custom))
            self.assertFalse(os.path.exists(os.path.join(tmp, "out.json")))

    def test_sidecar_dash_suppresses(self):
        with tempfile.TemporaryDirectory() as tmp:
            svg = os.path.join(tmp, "out.svg")
            r = self._run_cli("--type", "bargraph",
                              "--sidecar", "-",
                              "-o", svg, HASH_JOIN)
            self.assertEqual(r.returncode, 0)
            self.assertFalse(os.path.exists(os.path.join(tmp, "out.json")))


if __name__ == "__main__":
    unittest.main()
