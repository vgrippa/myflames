"""
Unit tests for :mod:`myflames.tokens` — the token estimator, the compact
digest builder, and the raw-plan-vs-digest savings comparison.

The estimator is heuristic by design (myflames vendors no tokenizer), so the
tests assert *invariants and relationships* (monotonicity, ordering, the
savings ratio on a real plan) rather than exact token counts, which would be
brittle against any tokenizer change.
"""
import json
import os
import sys
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(TEST_DIR))

from myflames.parser import parse_explain, analyze_plan
from myflames.output_sidecar import build_sidecar
from myflames.tokens import (
    estimate_tokens, build_digest, compare, format_report,
    build_raw_prompt, build_digest_prompt, make_counter, PRICING,
    build_compare_digest,
)
from myflames.output_compare_sidecar import build_compare_sidecar

FIXTURE_DIR = os.path.join(TEST_DIR, "fixtures")
# A non-trivial multi-table plan — the case where the digest wins big.
COMPLEX_FIXTURE = os.path.join(FIXTURE_DIR, "explain-099-hypergraph-on-5t-avg-salary.json")


def _build_payload(path):
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    root = parse_explain(raw)
    analysis = analyze_plan(root)
    payload = build_sidecar(root, analysis, source_type="file", engine="mysql")
    return raw, payload


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------

class TestEstimateTokens(unittest.TestCase):

    def test_empty_is_zero(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens(None), 0)

    def test_monotonic_in_length(self):
        short = estimate_tokens("SELECT * FROM users")
        long = estimate_tokens("SELECT * FROM users WHERE id = 42 AND name = 'alice'")
        self.assertGreater(long, short)

    def test_roughly_one_token_per_few_chars(self):
        # ~4 chars/token rule of thumb: a 200-char English string should land
        # in a sane band, not off by an order of magnitude either way.
        text = "the quick brown fox jumps over the lazy dog " * 5  # 220 chars
        tokens = estimate_tokens(text)
        self.assertTrue(30 <= tokens <= 90, "got %d tokens" % tokens)

    def test_handles_minified_json(self):
        # Minified JSON has no whitespace; a naive split() would undercount
        # badly. The estimator must still scale with content.
        minified = json.dumps({"a": 1, "b": [2, 3, 4], "c": {"d": "eee"}})
        self.assertGreater(estimate_tokens(minified), 5)

    def test_pretty_json_costs_more_than_minified(self):
        obj = {"query_block": {"select_id": 1, "cost_info": {"x": 1.5}}}
        minified = json.dumps(obj, separators=(",", ":"))
        pretty = json.dumps(obj, indent=4)
        # Indentation adds tokens — pretty should be at least as large.
        self.assertGreaterEqual(estimate_tokens(pretty), estimate_tokens(minified))


# ---------------------------------------------------------------------------
# build_digest
# ---------------------------------------------------------------------------

class TestBuildDigest(unittest.TestCase):

    def setUp(self):
        self.raw, self.payload = _build_payload(COMPLEX_FIXTURE)
        self.digest = build_digest(self.payload)

    def test_digest_is_nonempty_text(self):
        self.assertIsInstance(self.digest, str)
        self.assertTrue(self.digest.strip())

    def test_digest_contains_header_and_summary(self):
        self.assertIn("myflames digest", self.digest)
        self.assertIn(self.payload["executive_summary"], self.digest)

    def test_digest_carries_warning_text_verbatim(self):
        # The digest must not paraphrase warnings — an agent relies on the
        # same text the HTML shows (one source, two projections).
        for w in self.payload["warnings"]:
            self.assertIn(w["text"], self.digest)

    def test_digest_renders_plan_skeleton(self):
        self.assertIn("PLAN:", self.digest)
        # The root operator's short_label should appear in the tree.
        root_label = self.payload["plan_tree"]["short_label"]
        self.assertIn(root_label, self.digest)

    def test_digest_under_token_budget(self):
        # Design target: a typical digest fits comfortably for an agent to
        # load. Even this 5-table plan should stay well under 1000 tokens.
        self.assertLess(estimate_tokens(self.digest), 1000)


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------

class TestCompare(unittest.TestCase):

    def setUp(self):
        self.raw, self.payload = _build_payload(COMPLEX_FIXTURE)
        self.digest = build_digest(self.payload)
        self.raw_prompt = build_raw_prompt(self.raw)
        self.digest_prompt = build_digest_prompt(self.digest)
        self.result = compare(self.raw_prompt, self.digest_prompt)

    def test_has_stable_keys(self):
        for key in ("raw_tokens", "digest_tokens", "raw_chars", "digest_chars",
                    "tokens_saved", "reduction_pct", "ratio", "method",
                    "pricing_as_of", "cost_by_model"):
            self.assertIn(key, self.result)

    def test_prompts_include_the_question(self):
        # Both sides carry the same question framing — keeps the saving honest.
        self.assertIn("Why is this query slow", self.raw_prompt)
        self.assertIn("Why is this query slow", self.digest_prompt)
        # And the payload each wraps.
        self.assertIn(self.raw.strip(), self.raw_prompt)
        self.assertIn(self.digest.strip(), self.digest_prompt)

    def test_digest_is_smaller_than_raw(self):
        self.assertLess(self.result["digest_tokens"], self.result["raw_tokens"])
        self.assertGreater(self.result["tokens_saved"], 0)

    def test_complex_plan_saves_meaningfully(self):
        # The whole pitch: a complex plan compresses by multiples, not a few
        # percent. Assert a conservative floor so the headline stays honest.
        self.assertGreater(self.result["ratio"], 3.0)
        self.assertGreater(self.result["reduction_pct"], 50.0)

    def test_internal_consistency(self):
        r = self.result
        self.assertEqual(r["tokens_saved"], r["raw_tokens"] - r["digest_tokens"])

    def test_cost_breakdown_present_and_positive(self):
        cbm = self.result["cost_by_model"]
        for model_id in PRICING:
            self.assertIn(model_id, cbm)
            entry = cbm[model_id]
            # Saving is positive and equals raw - digest cost.
            self.assertGreater(entry["saved_usd"], 0)
            self.assertAlmostEqual(
                entry["saved_usd"], entry["raw_usd"] - entry["digest_usd"], places=5
            )
        # Opus (priciest input) saves more dollars than Haiku (cheapest).
        self.assertGreater(
            cbm["claude-opus-4-8"]["saved_usd"], cbm["claude-haiku-4-5"]["saved_usd"]
        )

    def test_empty_raw_does_not_divide_by_zero(self):
        result = compare("", "")
        self.assertEqual(result["raw_tokens"], 0)
        self.assertEqual(result["reduction_pct"], 0.0)
        self.assertEqual(result["ratio"], 0.0)

    def test_make_counter_defaults_to_heuristic(self):
        count_fn, method = make_counter(exact=False)
        self.assertEqual(count_fn("the quick brown fox"), estimate_tokens("the quick brown fox"))
        self.assertIn("heuristic", method)

    def test_format_report_renders(self):
        report = format_report(self.result)
        self.assertIn("Token cost", report)
        self.assertIn("myflames digest", report)
        self.assertIn("$", report)  # cost is shown


class TestCompareDigest(unittest.TestCase):

    def setUp(self):
        before = os.path.join(FIXTURE_DIR, "explain-001-table-scan-users-no-filter.json")
        after = os.path.join(FIXTURE_DIR, "explain-008-index-scan-users-by-country.json")
        with open(before, "r", encoding="utf-8") as f:
            self.b = f.read()
        with open(after, "r", encoding="utf-8") as f:
            self.a = f.read()
        self.sidecar = build_compare_sidecar(self.b, self.a)
        self.digest = build_compare_digest(self.sidecar)

    def test_digest_is_compact_text_with_header(self):
        self.assertIsInstance(self.digest, str)
        self.assertIn("Plan comparison", self.digest)
        self.assertIn("total time", self.digest)

    def test_digest_far_smaller_than_two_raw_plans(self):
        # The whole point of the diff digest: cheaper than pasting both plans.
        combined = estimate_tokens(self.b) + estimate_tokens(self.a)
        self.assertLess(estimate_tokens(self.digest), combined)


if __name__ == "__main__":
    unittest.main()
