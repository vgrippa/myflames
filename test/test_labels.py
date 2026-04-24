"""
Unit tests for myflames._labels — the shared fit_label() helper.

These are pure-function tests — no SVG, no DOM, no rendering. Deterministic.
"""
import os
import sys
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_DIR)

from myflames._labels import fit_label, ELLIPSIS, _middle_ellipsis, _width_px


class TestWidthPx(unittest.TestCase):

    def test_empty_is_zero(self):
        self.assertEqual(_width_px("", 12, 0.59), 0.0)

    def test_ascii_is_linear(self):
        # "ABC" at 12 px with 0.59 ratio ≈ 3 * 12 * 0.59 ≈ 21.24
        w = _width_px("ABC", 12, 0.59)
        self.assertAlmostEqual(w, 3 * 12 * 0.59, places=3)

    def test_cjk_doubles_ascii(self):
        """Fullwidth CJK should count as ~2x ASCII width."""
        ascii_w = _width_px("AB", 12, 0.59)
        cjk_w = _width_px("中文", 12, 0.59)
        # 2 CJK chars ≈ 4 ASCII chars' worth of width.
        self.assertAlmostEqual(cjk_w, ascii_w * 2, places=3)

    def test_surrogate_pair_counted_as_two(self):
        """Emoji / SMP chars should count as 2x base width."""
        ascii_w = _width_px("AA", 12, 0.59)
        emoji_w = _width_px("🔥", 12, 0.59)
        # Python iteration over a surrogate pair yields one "char" with cp > 0xFFFF.
        # So one emoji ≈ 2 ASCII chars worth.
        self.assertAlmostEqual(emoji_w, ascii_w, places=3)


class TestFitLabel(unittest.TestCase):

    def test_short_fits_unchanged(self):
        self.assertEqual(fit_label("users", 200, 12), "users")

    def test_empty_returns_empty(self):
        self.assertEqual(fit_label("", 200, 12), "")

    def test_zero_width_returns_ellipsis(self):
        # One ellipsis is the minimum representation of "had content but no space".
        self.assertEqual(fit_label("something", 0, 12), ELLIPSIS)

    def test_qualified_name_preserves_tail(self):
        """users.very_long_name should elide from the MIDDLE so the
        ``_name`` tail (the discriminating token) survives."""
        fitted = fit_label(
            "users.very_long_column_name", px_width=70, font_size=12)
        self.assertIn(ELLIPSIS, fitted)
        # Tail must survive — at minimum the last few chars of "_name".
        self.assertTrue(
            fitted.endswith("name") or fitted.endswith("ame") or
            fitted.endswith("me"),
            "middle-ellipsis dropped the tail: " + fitted,
        )
        # And we must NOT have dropped the entire table name.
        self.assertTrue(
            fitted.startswith("u") or fitted.startswith("us"),
            "middle-ellipsis dropped the head: " + fitted,
        )

    def test_single_identifier_uses_end_ellipsis(self):
        """No dot → no qualified-name structure → end-ellipsis."""
        fitted = fit_label("verylongoperatorname", px_width=60, font_size=12)
        self.assertTrue(fitted.endswith(ELLIPSIS))
        self.assertFalse(fitted.startswith(ELLIPSIS))

    def test_prefer_middle_false_forces_end_ellipsis(self):
        fitted = fit_label(
            "users.col", px_width=40, font_size=12, prefer_middle=False)
        self.assertTrue(fitted.endswith(ELLIPSIS))

    def test_cjk_label_fits_tighter_budget(self):
        """CJK counts as 2x width, so the same pixel budget yields
        fewer fitted characters. Just verify we don't overflow."""
        fitted = fit_label("中文表.非常长的列名", px_width=80, font_size=12)
        # Result must fit within budget.
        self.assertLessEqual(
            _width_px(fitted, 12, 0.59), 80 + 0.01,  # tolerate rounding
            "CJK fit overflowed budget: " + fitted,
        )

    def test_never_returns_literal_triple_dot(self):
        """We committed to Unicode … — never the three-dot sequence."""
        fitted = fit_label("x" * 200, px_width=40, font_size=12)
        self.assertNotIn("...", fitted)
        self.assertIn(ELLIPSIS, fitted)


class TestMiddleEllipsis(unittest.TestCase):

    def test_keep_chars_one_returns_ellipsis(self):
        self.assertEqual(_middle_ellipsis("users.col", 1), ELLIPSIS)

    def test_shorter_than_keep_returns_input(self):
        self.assertEqual(_middle_ellipsis("u.c", 10), "u.c")

    def test_qualified_preserves_dot_prefix(self):
        out = _middle_ellipsis("users.very_long_column", 10)
        # The dotted-prefix ("users.") should be preserved as a whole
        # when it fits inside the budget.
        self.assertTrue(
            out.startswith("users."),
            "qualified dotted prefix dropped: " + out,
        )


if __name__ == "__main__":
    unittest.main()
