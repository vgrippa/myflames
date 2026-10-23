"""Tests for myflames/flamegraph.py (folded stacks -> interactive SVG).

Covers input parsing, prefix merging in first-appearance order, geometry
(margins, rows, inverted mode, height/viewBox lockstep, the 0.1 px drop),
tooltips, label fitting, escaping, identity attributes, colors, the
standalone script, and that the color tables match the TypeScript view in
apps/workspace/src/charts/flamegraph.ts.
"""
import os
import re
import subprocess
import sys
import unittest
import xml.etree.ElementTree as ET

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_DIR)
sys.path.insert(0, TEST_DIR)

from _svg_contract import assert_svg_contract  # noqa: E402

from myflames import flamegraph  # noqa: E402
from myflames.flamegraph import (  # noqa: E402
    FAMILY_COLORS, MATCH_COLOR, PALETTES, SEPARATOR_COLORS,
    fit_frame_label, folded_to_svg, frame_color, label_hash,
)

NS = "{http://www.w3.org/2000/svg}"
TS_SOURCE = os.path.join(REPO_DIR, "apps", "workspace", "src", "charts", "flamegraph.ts")
HASH_JOIN = os.path.join(TEST_DIR, "mysql-explain-hash-join.json")


def _frames(svg):
    """[(label, x, y, w, h, fill, title, text, attrs)] in document order."""
    root = ET.fromstring(svg.encode("utf-8"))
    holder = root.find(NS + "g")
    out = []
    for g in holder.findall(NS + "g"):
        rect = g.find(NS + "rect")
        out.append({
            "label": g.get("data-label"), "x": float(rect.get("x")), "y": int(rect.get("y")),
            "w": float(rect.get("width")), "h": int(rect.get("height")), "fill": rect.get("fill"),
            "title": g.find(NS + "title").text, "text": g.find(NS + "text").text or "",
            "attrs": dict(g.attrib),
        })
    return root, out


def _value(frame):
    """The ``<value>, <percent>%)`` part of a frame's tooltip header."""
    return frame["title"].split("\n")[0].rsplit(" (", 1)[1]


def _luminance(rgb):
    def channel(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _hex_rgb(color):
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


class TestInputAndMerge(unittest.TestCase):
    def test_children_keep_first_appearance_order_not_alphabetical(self):
        svg = folded_to_svg("root;zeta 3\nroot 1\nroot;alpha 2\nroot;zeta;inner 1\n", width=420)
        _, frames = _frames(svg)
        self.assertEqual([f["label"] for f in frames], ["root", "zeta", "inner", "alpha"])
        by = {f["label"]: f for f in frames}
        # usable width 400; total 7 = root own 1 + zeta 3 + inner 1 + alpha 2.
        self.assertEqual(by["root"]["x"], 10.0)
        self.assertAlmostEqual(by["root"]["w"], 400.0, places=2)
        self.assertEqual(by["zeta"]["x"], 10.0, "children start at the parent's left edge")
        self.assertAlmostEqual(by["zeta"]["w"], 400 * 4 / 7.0, places=2)
        self.assertAlmostEqual(by["alpha"]["x"], 10 + 400 * 4 / 7.0, places=2)
        self.assertAlmostEqual(by["inner"]["x"], 10.0, places=2)
        # Parent own time is the uncovered gap on the right of the parent.
        self.assertAlmostEqual(by["alpha"]["x"] + by["alpha"]["w"], 10 + 400 * 6 / 7.0, places=1)

    def test_shared_prefixes_merge_and_sum(self):
        _, frames = _frames(folded_to_svg(["a;b 2", "a;b 3", "a;c 5"], width=220))
        self.assertEqual([f["label"] for f in frames], ["a", "b", "c"])
        self.assertIn("(5 samples, 50.00%)", frames[1]["title"])
        self.assertIn("(10 samples, 100.00%)", frames[0]["title"])

    def test_invalid_lines_are_skipped(self):
        text = "\n".join(["", "   ", "no_value", "a;b x", "a;b -3", "a;b nan", "a;b 1e999",
                          "a;b 1_000", "a;b ٢", "a;b 2", "a 0.5", "a;c .5", "a;d 1e1"])
        _, frames = _frames(folded_to_svg(text))
        self.assertEqual([f["label"] for f in frames], ["a", "b", "c", "d"])
        self.assertIn("(13 samples, 100.00%)", frames[0]["title"])

    def test_list_input_and_bytes_lines(self):
        self.assertEqual(folded_to_svg(["a 1", b"a;b 1"]), folded_to_svg("a 1\na;b 1"))

    def test_empty_input_returns_message_svg(self):
        for text in ("", "nothing here", "a 0\nb 0", None, []):
            svg = folded_to_svg(text, width=640)
            self.assertIn("No valid stack counts found.", svg)
            self.assertIn('width="640" height="60" viewBox="0 0 640 60"', svg)
            ET.fromstring(svg.encode("utf-8"))

    def test_frames_narrower_than_a_tenth_of_a_pixel_are_dropped(self):
        _, frames = _frames(folded_to_svg("a 100000\na;tiny 1\na;wide 5000\n", width=1200))
        self.assertEqual([f["label"] for f in frames], ["a", "wide"])


class TestGeometry(unittest.TestCase):
    def test_height_and_viewbox_agree_and_cover_every_row(self):
        svg = folded_to_svg("a;b;c 1\na 1\n", width=900, height=20)
        root, frames = _frames(svg)
        rows = 3
        expected = flamegraph.HEADER_H + rows * 21 + flamegraph.FOOTER_H
        self.assertEqual(root.get("height"), str(expected))
        self.assertEqual(root.get("viewBox"), "0 0 900 %d" % expected)
        self.assertEqual(root.get("width"), "900")
        self.assertTrue(all(f["h"] == 20 for f in frames))

    def test_flame_root_at_bottom_icicle_root_at_top(self):
        flame = {f["label"]: f["y"] for f in _frames(folded_to_svg("a;b 1"))[1]}
        icicle = {f["label"]: f["y"] for f in _frames(folded_to_svg("a;b 1", inverted=True))[1]}
        self.assertGreater(flame["a"], flame["b"])
        self.assertLess(icicle["a"], icicle["b"])
        self.assertEqual(icicle["a"], flamegraph.HEADER_H)
        self.assertEqual(flame["a"] - flame["b"], 17, "16 px rows with a 1 px gap")

    def test_label_baseline_and_inset(self):
        root = ET.fromstring(folded_to_svg("a 1", width=300, height=32).encode("utf-8"))
        text = root.find(NS + "g").find(NS + "g").find(NS + "text")
        self.assertEqual(text.get("x"), "13.00")
        self.assertEqual(text.get("y"), "%.2f" % (flamegraph.HEADER_H + 16 + 4))

    def test_output_is_deterministic(self):
        text = "x;y 3\nx;z;w 4\nx 1\n"
        self.assertEqual(folded_to_svg(text, title="T"), folded_to_svg(text, title="T"))


class TestTextAndAttributes(unittest.TestCase):
    def test_tooltip_header_uses_thousands_and_two_decimals(self):
        _, frames = _frames(folded_to_svg("a;b 1234567\na 1\n", countname="ms"))
        self.assertEqual(frames[0]["title"], "a (1,234,568 ms, 100.00%)")
        self.assertEqual(frames[1]["title"], "b (1,234,567 ms, 100.00%)")

    def test_float_values_round_in_the_tooltip(self):
        _, frames = _frames(folded_to_svg("a 2.5\na;b 1.25\n", countname="µs"))
        self.assertEqual(frames[0]["title"], "a (4 µs, 100.00%)")
        self.assertEqual(frames[1]["title"], "b (1 µs, 33.33%)")

    def test_fit_frame_label(self):
        char = 12 * 0.59
        self.assertEqual(fit_frame_label("TABLE SCAN [users]", 6 + 6.5 * char), "TABL..")
        self.assertEqual(fit_frame_label("TABLE SCAN [users]", 6 + 18.5 * char), "TABLE SCAN [users]")
        self.assertEqual(fit_frame_label("ab", 6 + 2.5 * char), "", "under 3 characters draws nothing")
        self.assertEqual(fit_frame_label("abc", 6 + 3.2 * char), "abc")
        self.assertEqual(fit_frame_label("abcdef", 6 + 3.2 * char), "a..")
        self.assertEqual(fit_frame_label("x", -5), "")
        self.assertEqual(fit_frame_label("表扫描表", 6 + 3.5 * char), "表..",
                         "counted by code point")

    def test_everything_is_escaped(self):
        hostile = 'SCAN [<b>&"x"]'
        svg = folded_to_svg(hostile + ";child 1", title='T <script>&"', countname="<u>",
                            frame_ids={hostile: 'n:"><x'})
        root, frames = _frames(svg)  # parses: nothing unescaped broke the XML
        self.assertEqual(frames[0]["label"], hostile)
        self.assertEqual(frames[0]["attrs"]["data-node-id"], 'n:"><x')
        self.assertTrue(frames[0]["title"].startswith(hostile + " (1 <u>, 100.00%)"))
        self.assertEqual(root.get("aria-label"), 'T <script>&"')
        self.assertNotIn("<b>", svg)
        self.assertNotIn("<script>&", svg)

    def test_control_characters_do_not_break_the_xml(self):
        svg = folded_to_svg("a\x01b;c\x0b 1")
        ET.fromstring(svg.encode("utf-8"))

    def test_identity_labels_ids_and_teach_indexes(self):
        svg = folded_to_svg(
            "operator:1;operator:2 3\noperator:1;operator:3 2\n",
            frame_labels={"operator:1": "NESTED LOOP INNER", "operator:2": "TABLE SCAN [t]",
                          "operator:3": "TABLE SCAN [t]"},
            frame_ids={"operator:1": "n:1", "operator:2": "n:2", "operator:3": "n:3"},
            teach_index_by_folded={"TABLE SCAN [t]": 4},
            family_by_folded={"TABLE SCAN [t]": "table_scan", "NESTED LOOP INNER": "nested_loop_join"},
            complexity_by_folded={"TABLE SCAN [t]": {"big_o": "O(n)"}},
        )
        _, frames = _frames(svg)
        self.assertEqual([f["attrs"].get("data-node-id") for f in frames], ["n:1", "n:2", "n:3"])
        self.assertEqual([f["label"] for f in frames], ["NESTED LOOP INNER", "TABLE SCAN [t]", "TABLE SCAN [t]"],
                         "equal display labels stay separate frames")
        self.assertEqual([f["attrs"].get("data-teach-index") for f in frames], [None, "4", "4"])
        self.assertEqual([f["fill"] for f in frames], ["#c998b3", "#db9292", "#db9292"])
        self.assertNotIn("O(n)", svg, "complexity is accepted and ignored")

    def test_frame_without_mapping_shows_its_token(self):
        _, frames = _frames(folded_to_svg("a;b 1", frame_labels={"a": None, "zzz": "Z"}))
        self.assertEqual([f["label"] for f in frames], ["a", "b"])

    def test_every_frame_has_a_text_element(self):
        _, frames = _frames(folded_to_svg("a 1000\na;narrow 9\n", width=600))
        narrow = [f for f in frames if f["label"] == "narrow"][0]
        self.assertEqual(narrow["text"], "")


class TestColors(unittest.TestCase):
    def test_families_reuse_visual_explain_colors(self):
        from myflames.output_workbench import COLORS
        pairs = {"table_scan": "table", "index_scan": "index", "index_lookup": "lookup",
                 "range": "range", "nested_loop_join": "nested_loop", "hash_join": "hash",
                 "temp_table": "materialize", "filter": "operation"}
        for family, kind in pairs.items():
            self.assertEqual(FAMILY_COLORS[family], COLORS[kind], family)

    def test_every_parser_family_but_other_has_a_color(self):
        from myflames.parser import OPERATOR_FAMILIES
        self.assertEqual(set(FAMILY_COLORS), set(OPERATOR_FAMILIES) - {"other"})

    def test_separators_are_grey_and_palette_fallbacks(self):
        self.assertEqual(frame_color("--", "table_scan"), SEPARATOR_COLORS["--"])
        self.assertEqual(frame_color("-"), SEPARATOR_COLORS["-"])
        self.assertEqual(frame_color("x", "other"), frame_color("x"))
        self.assertEqual(frame_color("x", None, "nope"), frame_color("x", None, "hot"))
        self.assertEqual(frame_color("x", None, None), frame_color("x", None, "hot"))
        self.assertEqual(frame_color("x", None, "MEM"), frame_color("x", None, "mem"))
        self.assertNotEqual(frame_color("x", None, "mem"), frame_color("x", None, "io"))

    def test_palette_colors_stay_in_range_and_readable(self):
        labels = ["", "a", "b", "TABLE SCAN [users]", "\U0001F600", "x" * 300] + ["op%d" % i for i in range(200)]
        for name, ranges in PALETTES.items():
            seen = set()
            for label in labels:
                color = frame_color(label, None, name)
                self.assertRegex(color, r"^#[0-9a-f]{6}$")
                rgb = _hex_rgb(color)
                for value, (low, high) in zip(rgb, ranges):
                    self.assertTrue(low <= value <= high, (name, label, color))
                seen.add(color)
            self.assertGreater(len(seen), 150, name + ": hash spreads labels over the palette")
            darkest = tuple(low for low, _ in ranges)
            self.assertGreaterEqual((_luminance(darkest) + 0.05) / 0.05, 4.5, name + ": black label contrast")
        for color in list(FAMILY_COLORS.values()) + list(SEPARATOR_COLORS.values()) + [MATCH_COLOR]:
            self.assertGreaterEqual((_luminance(_hex_rgb(color)) + 0.05) / 0.05, 4.5, color)

    def test_hash_is_stable(self):
        # Pinned values guard the cross-language contract with flamegraph.ts
        # (apps/workspace/test/charts.mjs compares the two implementations).
        self.assertEqual(label_hash(""), 2872998923)
        self.assertEqual(label_hash("TABLE SCAN [users]"), 1818958066)
        self.assertEqual(label_hash("\U0001F600"), 2464043637)
        self.assertEqual(frame_color("TABLE SCAN [users]"), "#f75b38")
        self.assertNotEqual(label_hash("ab"), label_hash("ba"))
        self.assertTrue(0 <= label_hash("x" * 1000) <= 0xFFFFFFFF)
        self.assertEqual(label_hash("\ud800"), label_hash("\ud800"), "lone surrogates hash by code point")

    def test_tables_match_the_typescript_view(self):
        if not os.path.exists(TS_SOURCE):
            self.skipTest("TypeScript sources are not in this checkout")
        with open(TS_SOURCE, encoding="utf-8") as f:
            source = f.read()

        def block(name):
            match = re.search(r"export const %s\b[^=]*=\s*\{(.*?)\};" % name, source, re.S)
            self.assertIsNotNone(match, name + " missing from flamegraph.ts")
            return match.group(1)

        families = dict(re.findall(r'"?([\w-]+)"?:\s*"(#[0-9a-f]{6})"', block("FAMILY_COLORS")))
        self.assertEqual(families, FAMILY_COLORS)
        separators = dict(re.findall(r'"([^"]+)":\s*"(#[0-9a-f]{6})"', block("SEPARATOR_COLORS")))
        self.assertEqual(separators, SEPARATOR_COLORS)
        palettes = {}
        for name, ranges in re.findall(r"(\w+):\s*\[(\[\d+, \d+\], \[\d+, \d+\], \[\d+, \d+\])\]",
                                       block("PALETTES")):
            numbers = [int(n) for n in re.findall(r"\d+", ranges)]
            palettes[name] = tuple(tuple(numbers[i:i + 2]) for i in (0, 2, 4))
        self.assertEqual(palettes, PALETTES)
        self.assertIn('MATCH_COLOR = "%s"' % MATCH_COLOR, source)
        for name in ("SIDE_MARGIN", "HEADER_H", "FOOTER_H", "TITLE_Y", "ROW_GAP", "FONT_SIZE",
                     "CHAR_RATIO", "LABEL_INSET", "MIN_FRAME_PX"):
            match = re.search(r"export const %s = ([\d.]+);" % name, source)
            self.assertIsNotNone(match, name)
            self.assertEqual(float(match.group(1)), float(getattr(flamegraph, name)), name)


class TestStandaloneDocument(unittest.TestCase):
    def setUp(self):
        self.svg = folded_to_svg("a;b 3\na;c 1\n", title="Plan", countname="ms")

    def test_well_formed_with_contract(self):
        self.assertTrue(self.svg.startswith('<?xml version="1.0"'))
        ET.fromstring(self.svg.encode("utf-8"))
        assert_svg_contract(self.svg, self, require_node_count_ge=4)

    def test_controls_and_script(self):
        for needle in ('class="fg-reset fg-off"', '>Reset zoom<', 'class="fg-search"', '>Search<',
                       'class="fg-details"', 'class="fg-matched"', "<![CDATA[", "currentScript",
                       "prompt(", '"F3"', "dblclick", "Matched: "):
            self.assertIn(needle, self.svg)
        script = self.svg.split("<![CDATA[", 1)[1].split("]]>", 1)[0]
        # The CLI rewrites <title>…</title> and splices its panel before the
        # first </svg>; neither may occur inside the script.
        self.assertNotIn("<title>", script)
        self.assertNotIn("</svg>", script)
        self.assertNotIn("innerHTML", script)
        self.assertEqual(self.svg.count("</svg>"), 1)

    def test_root_svg_has_no_style_attribute(self):
        # cli._make_svg_responsive only adds max-width when no style= exists.
        tag = re.search(r"<svg\b[^>]*>", self.svg).group(0)
        self.assertNotIn("style=", tag)
        self.assertEqual(re.search(r'height="(\d+)"', self.svg).group(1),
                         re.search(r'viewBox="0 0 \d+ (\d+)"', self.svg).group(1))


#: 4.1 ms plan, 7 operators; five have under 0.5 ms of self time, including
#: the whole users branch (Filter 0.15 ms over an index lookup of 0.3 ms).
SUB_MS_PLAN = """{"operation": "Nested loop inner join", "access_type": "join", "actual_last_row_ms": 4.1,
 "inputs": [
  {"operation": "Nested loop inner join", "access_type": "join", "actual_last_row_ms": 3.5, "inputs": [
    {"operation": "Filter: (o.total > 10)", "access_type": "filter", "condition": "(o.total > 10)",
     "actual_last_row_ms": 1.2, "inputs": [
       {"operation": "Table scan on o", "access_type": "table", "table_name": "orders", "actual_last_row_ms": 1.0}]},
    {"operation": "Index lookup on i using item_order (order_id = o.id)", "access_type": "index",
     "index_access_type": "index_lookup", "table_name": "items", "index_name": "item_order", "actual_last_row_ms": 2.0}]},
  {"operation": "Filter: (u.active = 1)", "access_type": "filter", "condition": "(u.active = 1)",
   "actual_last_row_ms": 0.45, "inputs": [
     {"operation": "Single-row index lookup on u using PRIMARY (id = o.user_id)", "access_type": "index",
      "index_access_type": "eq_ref", "table_name": "users", "index_name": "PRIMARY", "actual_last_row_ms": 0.3}]}]}"""


class TestPlanUnits(unittest.TestCase):
    def test_flame_scale(self):
        from myflames.flamegraph import flame_scale
        micro = ("\u00b5s", 1000)
        self.assertEqual(flame_scale([0.8], 0.8), micro, "sub-millisecond plan")
        self.assertEqual(flame_scale([3.0, 0.3], 4.1), micro)
        self.assertEqual(flame_scale([9.5, 0.5], 10), micro, "measured plans always count microseconds")
        self.assertEqual(flame_scale([6000, 357.2], 6357.2), micro, "even multi-second plans")
        self.assertEqual(flame_scale([10, 0, None, float("nan"), "x"], 10), micro)
        self.assertEqual(flame_scale([1.4], None), micro, "a positive self time is measured time")
        self.assertEqual(flame_scale([], 0), ("no measured time", 1), "nothing measured, nothing to count")
        self.assertEqual(flame_scale([0, None, -1], None), ("no measured time", 1))

    def test_flame_units(self):
        from myflames.flamegraph import flame_units
        self.assertEqual(flame_units(0, 1), 0)
        self.assertEqual(flame_units(None, 1000), 0)
        self.assertEqual(flame_units(-1, 1000), 0)
        self.assertEqual(flame_units(2.6, 1), 3)
        self.assertEqual(flame_units(0.3, 1000), 300)
        self.assertEqual(flame_units(0.0001, 1000), 1, "a measured operator keeps at least 1 unit")
        self.assertEqual(flame_units(0.2, 1), 1)

    def test_plan_folded_keep_root(self):
        from myflames.flamegraph import plan_folded
        self.assertEqual(plan_folded([(["r"], 0), (["r", "c"], 2)], 2), ("r;c 2000", "\u00b5s"))
        self.assertEqual(plan_folded([(["r"], 0), (["r", "c"], 2)], 2, keep_root=True),
                         ("r 1\nr;c 2000", "\u00b5s"))
        self.assertEqual(plan_folded([(["r"], 0)], 0), ("", "no measured time"))
        self.assertEqual(plan_folded([(["r"], 0)], 0, keep_root=True), ("r 1", "no measured time"),
                         "an untimed plan still draws its root")

    def test_widths_keep_sub_millisecond_proportions(self):
        import json
        from myflames.flamegraph import plan_folded
        from myflames.output_html_report import _render_svg
        from myflames.parser import parse_explain
        self.assertEqual(plan_folded([(["r"], 0), (["r", "a"], 1.4), (["r", "b"], 1.6)], 3),
                         ("r;a 1400\nr;b 1600", "\u00b5s"))
        plan = parse_explain(json.dumps({
            "operation": "Nested loop inner join", "access_type": "join", "actual_last_row_ms": 3.0,
            "inputs": [
                {"operation": "Table scan on a", "access_type": "table", "table_name": "a", "actual_last_row_ms": 1.4},
                {"operation": "Table scan on b", "access_type": "table", "table_name": "b", "actual_last_row_ms": 1.6}]}))
        _, frames = _frames(_render_svg(plan, "flamegraph", 1200, "Plan", "ms", node_identity=True))
        by = {f["label"].split(" ")[2]: f for f in frames if f["label"].startswith("TABLE SCAN")}
        self.assertAlmostEqual(by["[a]"]["w"] / by["[b]"]["w"], 14 / 16.0, places=3, msg="14:16, not 1:2")
        self.assertEqual([_value(f) for f in frames],
                         ["3.00 ms, 100.00%)", "1.40 ms, 46.67%)", "1.60 ms, 53.33%)"])

    def test_sub_millisecond_operators_keep_their_frames(self):
        from myflames.output_html_report import _render_svg
        from myflames.parser import flatten_nodes, parse_explain
        from myflames.render import render_explain
        plan = parse_explain(SUB_MS_PLAN)
        nodes = list(flatten_nodes(plan))
        self.assertEqual(len(nodes), 7)
        self.assertEqual(sum(1 for n in nodes if 0 < n["self_time"] < 0.5), 5)
        _, frames = _frames(_render_svg(plan, "flamegraph", 1200, "Plan", "ms", node_identity=True))
        self.assertEqual([f["attrs"]["data-node-id"] for f in frames], [n["node_id"] for n in nodes],
                         "every measured operator keeps its frame, in plan order")
        users = [f for f in frames if "users" in f["title"] or "u.active" in f["label"]]
        self.assertEqual(len(users), 2, "the users branch survives")
        # Counted in µs for the widths; each tooltip reads in its own unit.
        self.assertEqual([_value(f) for f in frames],
                         ["4.10 ms, 100.00%)", "3.50 ms, 85.37%)", "1.20 ms, 29.27%)", "1.00 ms, 24.39%)",
                          "2.00 ms, 48.78%)", "450 \u00b5s, 10.98%)", "300 \u00b5s, 7.32%)"])
        for svg in (render_explain(SUB_MS_PLAN, output_type="flamegraph", no_enhance=True),
                    _render_svg(plan, "flamegraph", 1200, "Plan", "ms")):
            _, folded = _frames(svg)
            self.assertEqual(len(folded), 7)
            self.assertEqual(_value(folded[0]), "4.10 ms, 100.00%)")
            self.assertEqual(sum(1 for f in folded if " \u00b5s, " in f["title"]), 2)

    def test_whole_millisecond_plans_count_microseconds(self):
        from myflames.output_html_report import _render_svg
        from myflames.parser import parse_explain
        plan = parse_explain('{"operation": "Sort", "access_type": "sort", "actual_last_row_ms": 12.5, '
                             '"inputs": [{"operation": "Table scan on t", "access_type": "table", '
                             '"table_name": "t", "actual_last_row_ms": 10.0}]}')
        _, frames = _frames(_render_svg(plan, "flamegraph", 1200, "Plan", "ms", node_identity=True))
        self.assertEqual([_value(f) for f in frames], ["12.5 ms, 100.00%)", "10.0 ms, 80.00%)"],
                         "no whole-ms rounding: 2.5 ms of sort time stays 2.5 ms")

    def test_cli_keeps_sub_millisecond_operators(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plan.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write(SUB_MS_PLAN)
            result = subprocess.run([sys.executable, "-m", "myflames", "--type", "flamegraph", "--no-enhance", path],
                                    cwd=REPO_DIR, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        _, frames = _frames(result.stdout)
        self.assertEqual(len(frames), 7)
        self.assertEqual(_value(frames[0]), "4.10 ms, 100.00%)")
        self.assertEqual(_value(frames[-1]), "300 \u00b5s, 7.32%)")


class TestReadableTimes(unittest.TestCase):
    """The count unit sets widths only; tooltips print each time readably."""

    def test_flame_value_text_bands(self):
        from myflames.flamegraph import flame_value_text
        cases = [
            (0, "ms", "0 ms"), (0, "\u00b5s", "0 ms"),
            (412, "\u00b5s", "412 \u00b5s"), (0.3, "\u00b5s", "1 \u00b5s"), (999.4, "\u00b5s", "999 \u00b5s"),
            (999.5, "\u00b5s", "1.00 ms"), (7127, "\u00b5s", "7.13 ms"), (7125, "\u00b5s", "7.13 ms"),
            (7, "ms", "7.00 ms"), (9995, "\u00b5s", "10.0 ms"), (71300, "\u00b5s", "71.3 ms"),
            (13, "ms", "13.0 ms"), (99950, "\u00b5s", "100 ms"), (6357231, "\u00b5s", "6,357 ms"),
            (1234568, "ms", "1,234,568 ms"),
        ]
        for value, unit, text in cases:
            self.assertEqual(flame_value_text(value, unit), text, (value, unit))

    def test_other_count_names_keep_integers(self):
        from myflames.flamegraph import flame_value_text
        self.assertEqual(flame_value_text(1234567, "samples"), "1,234,567 samples")
        self.assertEqual(flame_value_text(2.5, "samples"), "3 samples")
        self.assertEqual(flame_value_text(7127, "us"), "7,127 us")
        _, frames = _frames(folded_to_svg("a;b 7127\n", countname="widgets"))
        self.assertEqual(frames[0]["title"], "a (7,127 widgets, 100.00%)")

    def _root_header(self, path):
        from myflames.render import render_explain
        with open(path, encoding="utf-8") as f:
            svg = render_explain(f.read(), output_type="flamegraph")
        return _frames(svg)[1][0]["title"].split("\n")[0]

    def test_complex_join_root_reads_whole_milliseconds(self):
        path = os.path.join(TEST_DIR, "mysql-explain-complex-join.json")
        self.assertEqual(self._root_header(path), "SORT starts=1 rows=9 (6,357 ms, 100.00%)")

    def test_derived_table_root_reads_two_decimals(self):
        path = os.path.join(TEST_DIR, "fixtures", "explain-052-derived-table-top-spenders.json")
        self.assertIn("(7.13 ms, 100.00%)", self._root_header(path))

    def test_sub_millisecond_plan_reads_microseconds(self):
        from myflames.output_html_report import _render_svg
        from myflames.parser import parse_explain
        plan = parse_explain('{"operation": "Table scan on t", "access_type": "table", '
                             '"table_name": "t", "actual_last_row_ms": 0.2}')
        _, frames = _frames(_render_svg(plan, "flamegraph", 1200, "Plan", "ms", node_identity=True))
        self.assertEqual(_value(frames[0]), "200 \u00b5s, 100.00%)")

    def test_tiny_operator_in_a_multi_second_plan_reads_microseconds(self):
        import json
        from myflames.output_html_report import _render_svg
        from myflames.parser import parse_explain
        plan = parse_explain(json.dumps({
            "operation": "Nested loop inner join", "access_type": "join", "actual_last_row_ms": 2500.0,
            "inputs": [
                {"operation": "Table scan on a", "access_type": "table", "table_name": "a",
                 "actual_last_row_ms": 2000.0},
                {"operation": "Single-row index lookup on b using PRIMARY (id = a.b_id)", "access_type": "index",
                 "index_access_type": "eq_ref", "table_name": "b", "index_name": "PRIMARY",
                 "actual_last_row_ms": 0.3}]}))
        _, frames = _frames(_render_svg(plan, "flamegraph", 1200, "Plan", "ms", node_identity=True))
        self.assertEqual([_value(f) for f in frames],
                         ["2,500 ms, 100.00%)", "2,000 ms, 80.00%)", "300 \u00b5s, 0.01%)"])


class TestUnmeasuredPlans(unittest.TestCase):
    """A plan without measured time keeps its frames but states no time or share."""

    STATED = re.compile(r"\d\s*(?:ms|\u00b5s)\b|%")

    def test_flame_header(self):
        from myflames.flamegraph import NO_TIME, flame_header
        self.assertEqual(flame_header("Except", 1, 1, NO_TIME), "Except (no measured time)")
        self.assertEqual(flame_header("Sort", 7127, 7127, "\u00b5s"), "Sort (7.13 ms, 100.00%)")
        self.assertEqual(flame_header("a", 5, 10, "samples"), "a (5 samples, 50.00%)")

    def test_except_fixture_reads_no_measured_time(self):
        path = os.path.join(TEST_DIR, "fixtures", "mysql-8.4-v1-except.json")
        result = subprocess.run([sys.executable, "-m", "myflames", "--type", "flamegraph", path],
                                cwd=REPO_DIR, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        root, frames = _frames(result.stdout)
        self.assertTrue(frames, "placeholder frames still render")
        self.assertEqual(frames[0]["title"].split("\n")[0], "Except starts=1 (no measured time)")
        for frame in frames:
            self.assertIsNone(self.STATED.search(frame["title"]), frame["title"])
        self.assertEqual(root.find(NS + "g").get("data-measured"), "0")

    def test_folded_input_with_no_time_count_name(self):
        from myflames.flamegraph import NO_TIME
        svg = folded_to_svg("r 1\nr;c 1\n", countname=NO_TIME)
        _, frames = _frames(svg)
        self.assertEqual([f["title"] for f in frames], ["r (no measured time)", "c (no measured time)"])
        script = svg.split("<![CDATA[", 1)[1]
        self.assertIn('data-measured', script)
        self.assertIn('" frames"', script, "search counts frames instead of a share")

    def test_measured_plans_are_unchanged(self):
        root = ET.fromstring(folded_to_svg("a;b 3\n", countname="\u00b5s").encode("utf-8"))
        self.assertEqual(root.find(NS + "g").get("data-measured"), "1")


@unittest.skipUnless(os.path.exists(HASH_JOIN), "fixture missing")
class TestPipelines(unittest.TestCase):
    def test_render_explain_enhanced_tooltips_stay_well_formed(self):
        from myflames.render import render_explain
        with open(HASH_JOIN, encoding="utf-8") as f:
            svg = render_explain(f.read(), output_type="flamegraph")
        root = ET.fromstring(svg.encode("utf-8"))
        titles = [t.text for t in root.iter(NS + "title")]
        self.assertTrue(titles)
        self.assertTrue(any("\n" in t for t in titles), "parser details are appended to tooltips")
        self.assertEqual(root.get("height"), root.get("viewBox").split()[3])

    def test_identity_mode_frames_match_timed_operators(self):
        from myflames.output_html_report import _render_svg
        from myflames.parser import flatten_nodes, parse_explain
        with open(HASH_JOIN, encoding="utf-8") as f:
            plan = parse_explain(f.read())
        svg = _render_svg(plan, "flamegraph", 1200, "Plan", "ms", node_identity=True)
        _, frames = _frames(svg)
        ids = [f["attrs"]["data-node-id"] for f in frames]
        self.assertEqual(len(ids), len(set(ids)), "one frame per operator")
        order = [n["node_id"] for n in flatten_nodes(plan)]
        self.assertEqual(ids, [i for i in order if i in ids], "frames follow the plan's pre-order")

    def test_cli_inverted_svg_with_panel(self):
        result = subprocess.run(
            [sys.executable, "-m", "myflames", "--type", "flamegraph", "--inverted", HASH_JOIN],
            cwd=REPO_DIR, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        root = ET.fromstring(result.stdout.encode("utf-8"))
        self.assertEqual(root.get("height"), root.get("viewBox").split()[3])
        self.assertEqual(root.find(NS + "g").get("data-icicle"), "1")
        self.assertIn("max-width: 100%", root.get("style"))


if __name__ == "__main__":
    unittest.main()
