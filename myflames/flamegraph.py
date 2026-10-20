# SPDX-License-Identifier: Apache-2.0
"""Interactive flame graph SVG for myflames.

This module is an original implementation written for myflames from a
functional specification, licensed under the Apache License 2.0. It turns
folded stacks (``frame1;frame2;...;frameN <value>``) into a standalone SVG
that zooms, searches and shows details without any network access.

The browser chart runtime draws the same picture from chart-data-1.0 in
``apps/workspace/src/charts/flamegraph.ts``: frame geometry, order, label
fitting and colors match (``apps/workspace/test/parity.mjs``). The color
tables below must stay identical to the ones in that file
(``test/test_flamegraph.py`` compares them, ``apps/workspace/test/charts.mjs``
compares the computed colors).

Layout rules:

* Stacks sharing a prefix are merged. A frame's width is its own value plus
  everything beneath it; children sit left to right in the order their stacks
  first appear (the plan's input order, never alphabetical), packed from the
  parent's left edge, so a parent's own time is the uncovered part on its
  right.
* Frames narrower than :data:`MIN_FRAME_PX` are dropped.
* ``inverted=False`` puts the root row at the bottom (flame graph);
  ``inverted=True`` puts it at the top (icicle).

Plan callers build folded text with :func:`plan_folded`: whole microseconds
for any plan with measured time (:func:`flame_scale`), never less than 1 µs
for positive time (:func:`flame_units`), so widths keep sub-millisecond
detail and no measured operator disappears to rounding. That count unit
sets widths only: tooltips print each time in a readable unit
(:func:`flame_value_text`).
"""
import math
import re

from .parser import xml_escape

__all__ = [
    "folded_to_svg", "frame_color", "label_hash", "fit_frame_label",
    "flame_scale", "flame_units", "plan_folded", "flame_value_text", "flame_header", "NO_TIME",
    "FAMILY_COLORS", "SEPARATOR_COLORS", "PALETTES", "MATCH_COLOR",
]

# ---------------------------------------------------------------------------
# Geometry and typography (keep in step with flamegraph.ts)
# ---------------------------------------------------------------------------

SIDE_MARGIN = 10        # px left and right of the frames
HEADER_H = 36           # title and controls above the first row
FOOTER_H = 30           # details line and search readout below the last row
TITLE_Y = 24
ROW_GAP = 1             # px between rows
FONT_SIZE = 12
CHAR_RATIO = 0.59       # average glyph width as a fraction of the font size
CHAR_W = FONT_SIZE * CHAR_RATIO
LABEL_INSET = 3         # px between a frame's left edge and its label
MIN_FRAME_PX = 0.1
DETAILS_RESERVE = 150   # px kept free on the details line for "Matched: …"
BACKGROUND = "#fafafa"

EMPTY_MESSAGE = "No valid stack counts found."
HINT = ("Hover a frame for details. Click to zoom, double-click the "
        "background to reset, Ctrl+F to search.")

# ---------------------------------------------------------------------------
# Colors (identical tables live in flamegraph.ts)
# ---------------------------------------------------------------------------

#: Operator family -> fill. Families with a Visual Explain counterpart reuse
#: ``output_workbench.COLORS`` (table, index, lookup, range, nested_loop,
#: hash, materialize, operation) so a color means the same thing in both
#: views. aggregate, sort and limit have no dedicated Visual Explain kind and
#: get their own muted tones in the same lightness band. ``other`` has no
#: entry: those frames use the selected palette.
FAMILY_COLORS = {
    "table_scan": "#db9292",
    "index_scan": "#e4b181",
    "index_lookup": "#9ac59d",
    "range": "#e3ca83",
    "nested_loop_join": "#c998b3",
    "hash_join": "#9ecbcb",
    "temp_table": "#b4a4d8",
    "filter": "#d1cfad",
    "aggregate": "#a8bfdc",
    "sort": "#c7b49b",
    "limit": "#c5cfa0",
}

#: Separator frames are drawn in neutral greys.
SEPARATOR_COLORS = {"--": "#9aa0a6", "-": "#c4c8cc"}

#: Fill of frames matched by a search.
MATCH_COLOR = "#ff38c8"

#: Palette name -> (red, green, blue) channel ranges, inclusive. Every range
#: keeps 12 px black labels at a contrast ratio of at least 4.5:1.
PALETTES = {
    "hot": ((210, 250), (80, 200), (40, 80)),
    "mem": ((90, 150), (175, 225), (90, 140)),
    "io": ((90, 150), (140, 195), (205, 250)),
    "red": ((215, 250), (90, 150), (90, 150)),
    "green": ((100, 160), (195, 240), (100, 160)),
    "blue": ((110, 165), (150, 200), (220, 255)),
}

_MASK32 = 0xFFFFFFFF


def label_hash(text):
    """Stable 32-bit hash of *text* (same result as ``labelHash`` in TS).

    FNV-1a over each code point taken as three little-endian bytes, then a
    final avalanche so labels that differ only in their last characters
    still land far apart in the palette.
    """
    h = 0x811C9DC5
    for ch in text:
        cp = ord(ch)
        for shift in (0, 8, 16):
            h = ((h ^ ((cp >> shift) & 0xFF)) * 0x01000193) & _MASK32
    h ^= h >> 16
    h = (h * 0x85EBCA6B) & _MASK32
    h ^= h >> 13
    h = (h * 0xC2B2AE35) & _MASK32
    h ^= h >> 16
    return h


def _palette_name(colors):
    name = colors.lower() if isinstance(colors, str) else ""
    return name if name in PALETTES else "hot"


def frame_color(label, family=None, colors="hot"):
    """Fill for a frame: separator grey, family color, or palette hash."""
    label = "" if label is None else str(label)
    if label in SEPARATOR_COLORS:
        return SEPARATOR_COLORS[label]
    if isinstance(family, str) and family in FAMILY_COLORS:
        return FAMILY_COLORS[family]
    h = label_hash(label)
    channels = []
    for i, (low, high) in enumerate(PALETTES[_palette_name(colors)]):
        byte = (h >> (8 * i)) & 0xFF
        channels.append(low + (byte * (high - low)) // 255)
    return "#%02x%02x%02x" % tuple(channels)


def fit_frame_label(label, px):
    """Label text that fits *px* at 12 px, or "" when under 3 characters fit."""
    room = int(math.floor((px - 2 * LABEL_INSET) / CHAR_W)) if px > 0 else 0
    if room < 3:
        return ""
    if len(label) <= room:
        return label
    return label[:room - 2] + ".."


def _fit_text(text, px, char_w):
    room = int(math.floor(px / char_w)) if px > 0 else 0
    if len(text) <= room:
        return text
    return text[:room - 2] + ".." if room >= 3 else ""


# ---------------------------------------------------------------------------
# Plan times -> whole display units (same rule as flameScale/flameUnits in TS)
# ---------------------------------------------------------------------------

def _ms(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def flame_scale(self_times_ms, total_ms):
    """Return ``(unit, multiplier)`` for a plan's flame graph.

    Microseconds (``("µs", 1000)``) whenever the plan has measured time (a
    positive total or any positive self time), so widths keep their
    proportions (1.4 ms vs 1.6 ms is 1400:1600, not 1:2). A plan with no
    measured time at all gets ``(NO_TIME, 1)``: its placeholder frames are
    drawn, but their text states no time or share (:func:`flame_header`).
    """
    if _ms(total_ms) > 0:
        return "µs", 1000
    for value in self_times_ms:
        if _ms(value) > 0:
            return "µs", 1000
    return NO_TIME, 1


def flame_units(self_ms, multiplier):
    """Whole display units for one self time, rounded half up.

    Zero or unmeasured time is 0; positive time is at least 1 unit.
    """
    ms = _ms(self_ms)
    if ms <= 0:
        return 0
    return max(1, int(math.floor(ms * multiplier + 0.5)))


def plan_folded(entries, total_ms, keep_root=False):
    """Folded text and unit for ``(path, self_ms)`` plan entries.

    *keep_root* gives a root with no self time one unit, so a plan whose
    operators were never timed still draws its root frame.
    """
    entries = list(entries)
    unit, multiplier = flame_scale([ms for _, ms in entries], total_ms)
    lines = []
    for path, ms in entries:
        units = flame_units(ms, multiplier)
        if units == 0 and keep_root and len(path) == 1:
            units = 1
        if units > 0:
            lines.append(";".join(path) + " " + str(units))
    return "\n".join(lines), unit


#: Count name for a plan without measured time (EXPLAIN without ANALYZE).
NO_TIME = "no measured time"

#: Count units that are times, as microseconds per count. The count unit only
#: sets frame widths; values in these units print with flame_value_text.
TIME_UNITS = {"ms": 1000, "µs": 1}


def _half_up(value):
    return int(math.floor(value + 0.5))


def flame_value_text(value, countname):
    """Visible text for a frame value, e.g. ``"6,357 ms"`` or ``"12 samples"``.

    When *countname* is a time unit ("ms" or "µs") each value reads in its
    own unit: ``0 ms``; whole microseconds below 1 ms (at least ``1 µs``);
    2 decimals of ms below 10 ms, 1 decimal below 100 ms, then whole ms with
    thousands separators. Rounding is half up on the microsecond count, and
    a value that rounds up to the next band's limit moves to that band, so
    999.6 µs reads ``1.00 ms``. Any other *countname* keeps the integer with
    thousands separators. Same rule as ``flameValueText`` in flamegraph.ts.
    """
    unit = "" if countname is None else str(countname)
    per = TIME_UNITS.get(unit)
    if per is None:
        return "%s %s" % (_count(value), unit)
    micros = value * per
    if not micros > 0:
        return "0 ms"
    whole = _half_up(micros)
    if whole < 1000:
        return "%d µs" % max(1, whole)
    hundredths = _half_up(micros / 10.0)
    if hundredths < 1000:
        return "%d.%02d ms" % (hundredths // 100, hundredths % 100)
    tenths = _half_up(micros / 100.0)
    if tenths < 1000:
        return "%d.%d ms" % (tenths // 10, tenths % 10)
    return "{:,} ms".format(_half_up(micros / 1000.0))


def flame_header(label, value, total, countname):
    """Tooltip header (unescaped): ``"<label> (<value>, <percent>%)"``.

    With *countname* :data:`NO_TIME` it is ``"<label> (no measured time)"``:
    no time and no share. Same text as ``flameHeader`` in flamegraph.ts.
    """
    if countname == NO_TIME:
        return "%s (%s)" % (label, NO_TIME)
    return "%s (%s, %.2f%%)" % (label, flame_value_text(value, countname), 100.0 * value / total)


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------

_VALUE = re.compile(r"^(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")


def _lines(folded_text):
    if folded_text is None:
        return []
    if isinstance(folded_text, bytes):
        folded_text = folded_text.decode("utf-8", "replace")
    if isinstance(folded_text, str):
        return folded_text.split("\n")
    return list(folded_text)


def _parse(folded_text):
    """Yield ``(frames, value)`` for every valid folded line."""
    for raw in _lines(folded_text):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        if not isinstance(raw, str):
            continue
        parts = raw.strip().rsplit(None, 1)
        if len(parts) != 2 or not _VALUE.match(parts[1]):
            continue
        token = parts[1]
        value = int(token) if token.isdigit() else float(token)
        if isinstance(value, float) and not math.isfinite(value):
            continue
        yield parts[0].split(";"), value


class _Stack(object):
    """One merged frame: its token, inclusive value and ordered children."""

    __slots__ = ("token", "value", "children")

    def __init__(self, token):
        self.token = token
        self.value = 0
        self.children = {}  # token -> _Stack, in first-appearance order


def _merge(stacks):
    top = _Stack(None)
    for frames, value in stacks:
        node = top
        node.value += value
        for token in frames:
            child = node.children.get(token)
            if child is None:
                child = _Stack(token)
                node.children[token] = child
            child.value += value
            node = child
    return top


def _place(top, span):
    """Pre-order ``(stack, depth, start)`` for every frame at least 0.1 px wide."""
    total = top.value
    placed = []
    pending = []
    cursor = 0
    for child in top.children.values():
        pending.append((child, 0, cursor))
        cursor += child.value
    pending.reverse()
    while pending:
        node, depth, start = pending.pop()
        if node.value / total * span < MIN_FRAME_PX:
            continue
        placed.append((node, depth, start))
        inner = []
        cursor = start
        for child in node.children.values():
            inner.append((child, depth + 1, cursor))
            cursor += child.value
        pending.extend(reversed(inner))
    return placed


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _esc(value):
    """XML-escape text or an attribute value: the shared
    :func:`myflames.parser.xml_escape`, which also replaces XML-invalid
    characters with U+FFFD."""
    return xml_escape(value)


def _count(value):
    return "{:,}".format(int(math.floor(value + 0.5)))


def _px(value):
    return "%.2f" % value


def _lookup(mapping, key):
    if not mapping:
        return None
    value = mapping.get(key)
    if value is None and isinstance(key, str) and key.strip() != key:
        value = mapping.get(key.strip())
    return value


STYLE = (
    '.mfc-flame{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}'
    ".mfc-flame text{font-size:12px;fill:#1d1d1f}"
    ".mfc-flame .fg-title{font-size:17px;font-weight:600}"
    ".mfc-flame .fg-reset,.mfc-flame .fg-search,.mfc-flame .fg-ic{fill:#176aa8;cursor:pointer}"
    ".mfc-flame .fg-reset:hover,.mfc-flame .fg-search:hover,.mfc-flame .fg-ic:hover{text-decoration:underline}"
    ".mfc-flame .fg-ic.fg-on{font-weight:700;text-decoration:underline}"
    ".mfc-flame .fg-off,.mfc-flame .fg-hidden{display:none}"
    ".mfc-flame .frames>g{cursor:pointer}"
    ".mfc-flame .frames>g:hover>rect{stroke:#1d1d1f;stroke-width:.6}"
    ".mfc-flame .frames text{fill:#000;pointer-events:none}"
    ".mfc-flame .fg-ancestor{opacity:.55}"
    ".mfc-flame .fg-details{fill:#3a3a3c}"
    ".mfc-flame .fg-matched{font-weight:600}"
)

# Standalone behavior. ES5 so any SVG viewer with scripting can run it; it
# finds its own <svg> (several flame graphs may share an HTML page), never
# throws when an element is missing, and needs no network.
SCRIPT = r"""
(function () {
  "use strict";
  var doc = document;
  var me = doc.currentScript;
  var svg = me && me.ownerSVGElement ? me.ownerSVGElement : doc.documentElement;
  if (!svg || !svg.querySelector) return;
  var holder = svg.querySelector(".frames");
  if (!holder) return;
  var MATCH = holder.getAttribute("data-match") || "#ff38c8";
  var left = Number(holder.getAttribute("data-left")) || 0;
  var span = Number(holder.getAttribute("data-span")) || 1;
  var icicle = holder.getAttribute("data-icicle") === "1";
  // A plan without measured time states no share: count matching frames.
  var measured = holder.getAttribute("data-measured") !== "0";
  var CHAR = 12 * 0.59;
  var EPS = 0.02;
  var resetBtn = svg.querySelector(".fg-reset");
  var searchBtn = svg.querySelector(".fg-search");
  var caseBtn = svg.querySelector(".fg-ic");
  var details = svg.querySelector(".fg-details");
  var matched = svg.querySelector(".fg-matched");
  var hint = details ? details.textContent : "";
  var frames = [];
  var groups = [];
  var zoomed = null;
  var term = "";
  var ignoreCase = false;

  function chars(text) { return Array.from ? Array.from(text) : String(text).split(""); }
  // Same rule as fit_frame_label: under 3 characters of room draws nothing.
  function fitLabel(label, w) {
    var list = chars(label);
    var room = Math.floor((w - 6) / CHAR);
    if (room < 3) return "";
    return list.length <= room ? label : list.slice(0, room - 2).join("") + "..";
  }
  function fitLine(text, px) {
    var list = chars(text);
    var room = Math.floor(px / CHAR);
    if (list.length <= room) return text;
    return room >= 3 ? list.slice(0, room - 2).join("") + ".." : "";
  }
  function mark(el, name, on) {
    if (!el) return;
    var names = (el.getAttribute("class") || "").split(/\s+/).filter(function (n) { return n && n !== name; });
    if (on) names.push(name);
    el.setAttribute("class", names.join(" "));
  }
  function child(el, tag) {
    var found = el.getElementsByTagName(tag);
    return found.length ? found[0] : null;
  }
  function num(el, name) { return el ? Number(el.getAttribute(name)) || 0 : 0; }

  var nodes = holder.childNodes;
  for (var i = 0; i < nodes.length; i++) {
    var g = nodes[i];
    if (!g || g.nodeType !== 1) continue;
    var rect = child(g, "rect");
    if (!rect) continue;
    var text = child(g, "text");
    var tip = child(g, "title");
    var y = num(rect, "y");
    groups.push(g);
    frames.push({
      g: g, rect: rect, text: text, x: num(rect, "x"), w: num(rect, "width"),
      level: icicle ? y : -y, label: g.getAttribute("data-label") || "",
      fill: rect.getAttribute("fill"), shown: text ? text.textContent : "",
      info: tip ? tip.textContent : ""
    });
  }

  function show(text) {
    if (!details) return;
    var lines = String(text || "").split("\n").filter(function (l) { return /\S/.test(l); });
    details.textContent = fitLine(lines.join("  ·  "), span - 150);
  }
  function put(f, x, w, label) {
    f.rect.setAttribute("x", x.toFixed(2));
    f.rect.setAttribute("width", w.toFixed(2));
    if (f.text) {
      f.text.setAttribute("x", (x + 3).toFixed(2));
      f.text.textContent = label;
    }
  }
  function within(outer, inner) {
    return outer.x - EPS <= inner.x && inner.x + inner.w <= outer.x + outer.w + EPS;
  }
  function reset() {
    frames.forEach(function (f) {
      put(f, f.x, f.w, f.shown);
      mark(f.g, "fg-hidden", false);
      mark(f.g, "fg-ancestor", false);
    });
    zoomed = null;
    mark(resetBtn, "fg-off", true);
  }
  function zoom(target) {
    var ancestors = frames.filter(function (f) { return f.level < target.level && within(f, target); });
    if (!ancestors.length && target.w >= span - EPS) { reset(); return; }
    var scale = span / target.w;
    frames.forEach(function (f) {
      if (f === target || (f.level > target.level && within(target, f))) {
        var w = f.w * scale;
        put(f, left + (f.x - target.x) * scale, w, fitLabel(f.label, w));
        mark(f.g, "fg-hidden", false);
        mark(f.g, "fg-ancestor", false);
      } else if (ancestors.indexOf(f) !== -1) {
        put(f, left, span, fitLabel(f.label, span));
        mark(f.g, "fg-hidden", false);
        mark(f.g, "fg-ancestor", true);
      } else {
        mark(f.g, "fg-hidden", true);
      }
    });
    zoomed = target;
    mark(resetBtn, "fg-off", false);
  }
  function coverage(spans) {
    spans.sort(function (a, b) { return a[0] - b[0]; });
    var covered = 0, start = null, end = null;
    spans.forEach(function (s) {
      if (start === null || s[0] > end + EPS) {
        if (start !== null) covered += end - start;
        start = s[0];
        end = s[1];
      } else if (s[1] > end) {
        end = s[1];
      }
    });
    if (start !== null) covered += end - start;
    return covered;
  }
  function search(value) {
    term = value || "";
    var pattern = null;
    if (term) {
      try { pattern = new RegExp(term, ignoreCase ? "i" : ""); }
      catch (err) { pattern = null; show("Invalid regular expression: " + term); }
    }
    var spans = [];
    frames.forEach(function (f) {
      var hit = !!pattern && pattern.test(f.label);
      f.rect.setAttribute("fill", hit ? MATCH : f.fill);
      if (hit) spans.push([f.x, f.x + f.w]);
    });
    if (matched) {
      matched.textContent = !pattern ? ""
        : measured ? "Matched: " + Math.min(100, coverage(spans) / span * 100).toFixed(1) + "%"
        : "Matched: " + spans.length + (spans.length === 1 ? " frame" : " frames");
    }
  }
  function ask() {
    var win = doc.defaultView || (typeof window !== "undefined" ? window : null);
    if (!win || typeof win.prompt !== "function") return;
    var value = win.prompt("Search frames (regular expression, empty clears)", term);
    if (value !== null && value !== undefined) search(value);
  }
  function frameOf(el) {
    while (el && el !== holder && el !== svg) {
      if (el.parentNode === holder) {
        var at = groups.indexOf(el);
        return at === -1 ? null : frames[at];
      }
      el = el.parentNode;
    }
    return null;
  }

  holder.addEventListener("mouseover", function (event) {
    var f = frameOf(event.target);
    if (f) show(f.info || f.label);
  });
  holder.addEventListener("mouseout", function (event) {
    var f = frameOf(event.target);
    if (f && frameOf(event.relatedTarget) !== f) show(hint);
  });
  holder.addEventListener("click", function (event) {
    var f = frameOf(event.target);
    if (f) zoom(f);
  });
  svg.addEventListener("dblclick", function (event) {
    if (!frameOf(event.target)) reset();
  });
  if (resetBtn) resetBtn.addEventListener("click", reset);
  if (searchBtn) searchBtn.addEventListener("click", ask);
  if (caseBtn) {
    caseBtn.addEventListener("click", function () {
      ignoreCase = !ignoreCase;
      mark(caseBtn, "fg-on", ignoreCase);
      if (term) search(term);
    });
  }
  // Standalone: the whole document. Inline in a page: only this chart, so
  // the browser's own find keeps working elsewhere.
  var keys = svg === doc.documentElement ? doc : svg;
  if (keys === svg && !svg.hasAttribute("tabindex")) svg.setAttribute("tabindex", "-1");
  keys.addEventListener("keydown", function (event) {
    var key = event.key || "";
    if (key === "F3" || ((event.ctrlKey || event.metaKey) && !event.altKey && (key === "f" || key === "F"))) {
      event.preventDefault();
      ask();
    } else if (key === "Escape") {
      if (term) search(""); else if (zoomed) reset();
    }
  });
})();
"""


def _empty_svg(width):
    height = 60
    return "\n".join([
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>',
        '<svg xmlns="http://www.w3.org/2000/svg" version="1.1" width="%d" height="%d" '
        'viewBox="0 0 %d %d" class="mfc-flame">' % (width, height, width, height),
        "<style>%s</style>" % STYLE,
        '<rect x="0" y="0" width="100%%" height="100%%" fill="%s"/>' % BACKGROUND,
        '<text x="%s" y="34" text-anchor="middle">%s</text>' % (_px(width / 2.0), _esc(EMPTY_MESSAGE)),
        "</svg>",
        "",
    ])


def folded_to_svg(folded_text, title="Flame Graph", width=1200, height=16,
                  countname="samples", inverted=False, colors="hot",
                  teach_index_by_folded=None, complexity_by_folded=None,
                  family_by_folded=None, frame_labels=None, frame_ids=None):
    """Render folded stacks as a standalone interactive SVG string.

    ``frame_labels`` maps a folded token to the label to display and
    ``frame_ids`` maps a token to a stable node id (``data-node-id``).
    ``teach_index_by_folded`` and ``family_by_folded`` are keyed by the
    displayed label. ``complexity_by_folded`` is accepted for compatibility
    and ignored. Output is deterministic for a given input.
    """
    try:
        width = int(width)
    except (TypeError, ValueError):
        width = 1200
    width = max(1, width)
    try:
        row_h = max(1, int(height))
    except (TypeError, ValueError):
        row_h = 16
    span = max(1, width - 2 * SIDE_MARGIN)

    top = _merge(_parse(folded_text))
    placed = _place(top, span) if top.value > 0 else []
    if not placed:
        return _empty_svg(width)

    total = top.value
    rows = max(depth for _, depth, _ in placed) + 1
    pitch = row_h + ROW_GAP
    svg_h = HEADER_H + rows * pitch + FOOTER_H
    labels = frame_labels or {}
    ids = frame_ids or {}

    out = [
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>',
        '<svg xmlns="http://www.w3.org/2000/svg" version="1.1" width="%d" height="%d" '
        'viewBox="0 0 %d %d" class="mfc-flame" aria-label="%s">'
        % (width, svg_h, width, svg_h, _esc(title)),
        "<style>%s</style>" % STYLE,
        '<rect x="0" y="0" width="100%%" height="100%%" fill="%s"/>' % BACKGROUND,
        '<text class="fg-title" x="%s" y="%d" text-anchor="middle">%s</text>'
        % (_px(width / 2.0), TITLE_Y, _esc(_fit_text("" if title is None else str(title),
                                                     max(0, width - 240), 17 * CHAR_RATIO))),
        '<text class="fg-reset fg-off" x="%d" y="%d" role="button">Reset zoom</text>'
        % (SIDE_MARGIN, TITLE_Y),
        '<text class="fg-ic" x="%d" y="%d" text-anchor="end" role="button">ic</text>'
        % (width - SIDE_MARGIN - 60, TITLE_Y),
        '<text class="fg-search" x="%d" y="%d" text-anchor="end" role="button">Search</text>'
        % (width - SIDE_MARGIN, TITLE_Y),
        '<g class="frames" data-left="%d" data-span="%d" data-icicle="%d" data-measured="%d" data-match="%s">'
        % (SIDE_MARGIN, span, 1 if inverted else 0, 0 if countname == NO_TIME else 1, MATCH_COLOR),
    ]
    for node, depth, start in placed:
        token = node.token
        label = labels.get(token)
        label = token if label is None else str(label)
        x = SIDE_MARGIN + start / total * span
        w = node.value / total * span
        row = depth if inverted else rows - 1 - depth
        y = HEADER_H + row * pitch
        tip = _esc(flame_header(label, node.value, total, countname))
        attrs = []
        node_id = ids.get(token)
        if node_id is not None:
            attrs.append('data-node-id="%s"' % _esc(node_id))
        teach = _lookup(teach_index_by_folded, label)
        if teach is not None:
            attrs.append('data-teach-index="%s"' % _esc(teach))
        attrs.append('data-label="%s"' % _esc(label))
        fill = frame_color(label, _lookup(family_by_folded, label), colors)
        out.append("<g %s>" % " ".join(attrs))
        out.append("<title>%s</title>" % tip)
        out.append('<rect x="%s" y="%d" width="%s" height="%d" rx="2" ry="2" fill="%s"/>'
                   % (_px(x), y, _px(w), row_h, fill))
        out.append('<text x="%s" y="%s">%s</text>'
                   % (_px(x + LABEL_INSET), _px(y + row_h / 2.0 + 4), _esc(fit_frame_label(label, w))))
        out.append("</g>")
    out.append("</g>")
    out.append('<text class="fg-details" x="%d" y="%d">%s</text>'
               % (SIDE_MARGIN, svg_h - 10, _esc(_fit_text(HINT, span - DETAILS_RESERVE, CHAR_W))))
    out.append('<text class="fg-matched" x="%d" y="%d" text-anchor="end"></text>'
               % (width - SIDE_MARGIN, svg_h - 10))
    out.append('<script type="text/ecmascript"><![CDATA[%s]]></script>' % SCRIPT)
    out.append("</svg>")
    out.append("")
    return "\n".join(out)
