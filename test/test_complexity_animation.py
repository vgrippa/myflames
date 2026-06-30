"""
Smoke + contract coverage for the two pedagogy SVG artifacts that previously
had zero tests:

* :mod:`myflames.complexity_animation` — the animated log-log Big O chart
  emitted into the "Learn this operator" teach dialog.
* :mod:`myflames.complexity_legend` — the shared Big O legend fragment.

These assert the public contracts (well-formed SVG, stable LEGEND_ROWS shape)
and that the renderer never raises on degenerate inputs — not pixel output,
which is the renderer-builder's domain.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from myflames.complexity_animation import render_complexity_animation_svg  # noqa: E402
from myflames.complexity_legend import (  # noqa: E402
    render_complexity_legend_svg,
    LEGEND_ROWS,
)


class TestComplexityAnimation(unittest.TestCase):

    def test_returns_wellformed_svg_with_matching_viewbox(self):
        svg = render_complexity_animation_svg(
            {"big_o": "O(n)", "severity": "medium"}, width=560, height=280)
        self.assertIsInstance(svg, str)
        self.assertTrue(svg.strip())
        self.assertIn("<svg", svg)
        self.assertIn("</svg>", svg)
        # viewBox must reflect the requested canvas dimensions.
        self.assertIn('viewBox="0 0 560 280"', svg)

    def test_generic_chart_without_complexity_dict(self):
        # None => the "generic" template chart (no highlighted curve / badge).
        svg = render_complexity_animation_svg(None)
        self.assertIn("<svg", svg)
        self.assertIn("</svg>", svg)

    def test_highlighted_curve_is_marked(self):
        # A recognizable big_o should highlight exactly that curve class.
        svg = render_complexity_animation_svg({"big_o": "O(n²)", "severity": "bad"})
        self.assertIn('class="complexity-curve highlighted"', svg)
        self.assertIn('data-complexity-kind="quad"', svg)
        # And the badge carries the (escaped) formula.
        self.assertIn("complexity-animation-badge", svg)

    def test_does_not_raise_on_edge_inputs(self):
        # Boundary / degenerate inputs must not blow up the renderer.
        edge_cases = [
            {},                                   # empty dict
            {"big_o": ""},                        # empty formula
            {"big_o": "O(2ⁿ)", "severity": "bad"},  # exponential exits chart
            {"big_o": "totally unknown class"},   # unmatched => no highlight
        ]
        for cd in edge_cases:
            with self.subTest(cd=cd):
                svg = render_complexity_animation_svg(cd)
                self.assertIn("<svg", svg)
                self.assertIn("</svg>", svg)

    def test_does_not_raise_on_tiny_n_max(self):
        # A single decade of n (smallest sensible domain) still renders.
        svg = render_complexity_animation_svg(
            {"big_o": "O(log n)"}, n_max=10, ops_max=10)
        self.assertIn("<svg", svg)
        self.assertIn("</svg>", svg)


class TestComplexityLegend(unittest.TestCase):

    def test_legend_rows_nonempty_and_3_tuples(self):
        self.assertTrue(LEGEND_ROWS)
        for row in LEGEND_ROWS:
            self.assertEqual(len(row), 3, "each legend row is (big_o, sev, text)")
            big_o, sev, text = row
            self.assertIsInstance(big_o, str)
            self.assertIn(sev, {"good", "medium", "bad"})
            self.assertIsInstance(text, str)
            self.assertTrue(text.strip())

    def test_hash_lookup_moved_to_o1_row(self):
        # The fix moved the hash-lookup example from the O(log n) row to the
        # O(1) row. O(1) must mention hash; O(log n) must NOT.
        rows = {big_o: text for big_o, _sev, text in LEGEND_ROWS}
        self.assertIn("O(1)", rows)
        self.assertIn("O(log n)", rows)
        self.assertIn("hash", rows["O(1)"].lower())
        self.assertNotIn("hash", rows["O(log n)"].lower())

    def test_render_returns_lines_and_height(self):
        lines, height = render_complexity_legend_svg(x=16, y=0, width=1200)
        self.assertIsInstance(lines, list)
        self.assertTrue(lines)
        joined = "".join(lines)
        self.assertIn("<g", joined)
        self.assertIn("</g>", joined)
        self.assertIsInstance(height, int)
        self.assertGreater(height, 0)
        # Every legend row's formula appears in the rendered fragment.
        for big_o, _sev, _text in LEGEND_ROWS:
            self.assertIn(big_o, joined)


if __name__ == "__main__":
    unittest.main()
