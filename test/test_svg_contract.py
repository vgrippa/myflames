"""
Tests that (a) the ``_svg_contract.assert_svg_contract`` helper works,
and (b) every renderer's SVG already meets the contract today.

Slice 3 / P4. The helper lives at ``test/_svg_contract.py`` (leading
underscore so unittest discovery skips it).
"""
import glob
import os
import sys
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_DIR)
sys.path.insert(0, TEST_DIR)

from _svg_contract import assert_svg_contract

from myflames.parser import parse_explain
from myflames.flamegraph import folded_to_svg
from myflames.output_bargraph import render_bargraph
from myflames.output_treemap import render_treemap


def _load_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestContractHelperItself(unittest.TestCase):
    """Does the helper actually catch the things it says it catches?"""

    def test_flags_linear_calcmode(self):
        bad = (
            '<svg viewBox="0 0 10 10">'
            '<animate calcMode="linear" attributeName="x" dur="1s"'
            ' repeatCount="indefinite"/></svg>'
        )
        with self.assertRaises(AssertionError):
            assert_svg_contract(bad, self)

    def test_flags_unbounded_anim_without_handle(self):
        bad = (
            '<svg viewBox="0 0 10 10">'
            '<animate attributeName="x" dur="1s" repeatCount="indefinite"/>'
            '</svg>'
        )
        with self.assertRaises(AssertionError):
            assert_svg_contract(bad, self)

    def test_accepts_bounded_or_classed_anim(self):
        good = (
            '<svg viewBox="0 0 10 10">'
            '<animate class="anim" attributeName="x" dur="1s"'
            ' repeatCount="indefinite"/></svg>'
        )
        # Should not raise.
        assert_svg_contract(good, self)

    def test_flags_missing_viewbox(self):
        bad = '<svg>x</svg>'
        with self.assertRaises(AssertionError):
            assert_svg_contract(bad, self)

    def test_flags_triple_dot_when_opted_in(self):
        bad = '<svg viewBox="0 0 10 10"><text>foo...</text></svg>'
        with self.assertRaises(AssertionError):
            assert_svg_contract(bad, self, forbid_triple_dot=True)

    def test_ignores_triple_dot_by_default(self):
        svg = '<svg viewBox="0 0 10 10"><text>foo...</text></svg>'
        # No forbid_triple_dot flag → must not raise.
        assert_svg_contract(svg, self)


class TestRenderersMeetContract(unittest.TestCase):
    """Every production SVG we emit must satisfy the contract."""

    FIXTURE = os.path.join(
        TEST_DIR, "fixtures",
        "explain-045-join-4t-users-orders-items-products.json")

    def setUp(self):
        self.assertTrue(
            os.path.exists(self.FIXTURE),
            "required fixture missing: " + self.FIXTURE,
        )
        self.root = parse_explain(_load_text(self.FIXTURE))

    def test_bargraph_contract(self):
        svg = render_bargraph(self.root, total_time=self.root["total_time"])
        assert_svg_contract(svg, self, forbid_triple_dot=True)

    def test_treemap_contract(self):
        svg = render_treemap(self.root)
        assert_svg_contract(svg, self, forbid_triple_dot=True)


if __name__ == "__main__":
    unittest.main()
