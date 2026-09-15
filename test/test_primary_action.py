"""
Unit tests for the HTML "Fix first" / "All clear" card (output_html_report).

Focused and fixture-independent: they drive _render_primary_action with small
synthetic sidecars so the three states are unambiguous — a promoted fix, a
genuinely clean plan (all-clear), and a plan with warnings but no promotable
suggestion (render nothing, never a false all-clear).
"""
import os
import sys
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(TEST_DIR))

from myflames.output_html_report import _render_primary_action, _render_all_clear_card


class TestPrimaryActionCard(unittest.TestCase):

    def test_clean_plan_renders_all_clear(self):
        # No suggestions AND no warnings → say "nothing to fix" out loud.
        html = _render_primary_action({"warnings": [], "suggestions": []})
        self.assertIn("No blocking issues found", html)
        self.assertIn("all-clear", html)

    def test_warnings_without_suggestion_render_nothing(self):
        # A warning is unaddressed → must NOT claim "all clear", and there's no
        # suggestion to promote, so the card renders nothing.
        sidecar = {
            "warnings": [{"severity": "warn", "category": "full_scan",
                          "text": "Full table scan: users"}],
            "suggestions": [],
        }
        self.assertEqual(_render_primary_action(sidecar), "")

    def test_promoted_suggestion_renders_fix_first(self):
        sidecar = {
            "warnings": [],
            "suggestions": [{"severity": "high", "category": "index",
                             "action": "Add an index on users(country)"}],
            "primary_action": {"ref": "suggestions[0]"},
        }
        html = _render_primary_action(sidecar)
        self.assertIn("Fix first", html)
        self.assertIn("users(country)", html)
        self.assertNotIn("all-clear", html)

    def test_all_clear_card_is_wellformed(self):
        html = _render_all_clear_card()
        self.assertIn('role="region"', html)
        self.assertIn("All clear", html)


if __name__ == "__main__":
    unittest.main()
