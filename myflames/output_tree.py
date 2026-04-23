"""Generate interactive collapsible plan tree SVG from parsed EXPLAIN tree."""
import math
from .parser import xml_escape, flatten_nodes, render_info_panel
from .complexity import SEVERITY_COLORS, SEVERITY_BORDERS

_LOW_RGB = (0xde, 0xeb, 0xf7)   # light blue (low self-time)
_HIGH_RGB = (0x21, 0x71, 0xb5)  # dark blue (high self-time)


def _flatten_with_depth(node, depth=0):
    """Yield (node, depth) tuples in pre-order."""
    yield node, depth
    for child in node.get("children") or []:
        yield from _flatten_with_depth(child, depth + 1)


def _format_time(ms):
    if ms is None or ms <= 0:
        return "\u2014"
    t = float(ms)
    if t >= 1000:
        return f"{t/1000:.2f} s"
    if t >= 1:
        return f"{t:.2f} ms" if t < 100 else f"{t:.0f} ms"
    return f"{t*1000:.1f} \u00b5s"


def _row_color(self_time, root_total):
    """Sequential blue: light = fast, dark = slow (same scale as diagram)."""
    if not root_total or self_time <= 0:
        return "#e8f4fd"
    ratio = min(1.0, math.log1p(self_time) / math.log1p(max(root_total, 0.001)))
    r = int(_LOW_RGB[0] + (_HIGH_RGB[0] - _LOW_RGB[0]) * ratio)
    g = int(_LOW_RGB[1] + (_HIGH_RGB[1] - _LOW_RGB[1]) * ratio)
    b = int(_LOW_RGB[2] + (_HIGH_RGB[2] - _LOW_RGB[2]) * ratio)
    return f"#{r:02x}{g:02x}{b:02x}"


def _attr_escape(s):
    if s is None:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _row_info(node, root_total, unit_display):
    """Build the rich info string shown in the details bar."""
    details = node.get("details") or {}
    sep = "  \u00b7  "
    parts = []
    full_label = node.get("full_label") or node.get("short_label") or ""
    if full_label:
        parts.append(full_label[:80])
    complexity = details.get("complexity")
    if isinstance(complexity, dict) and complexity.get("big_o"):
        conf = complexity.get("confidence", "exact")
        conf_tag = "" if conf == "exact" else " ({})".format(conf.replace("_", " "))
        parts.append("Complexity: {}{}".format(complexity["big_o"], conf_tag))
        rationale = complexity.get("rationale")
        if rationale:
            rat = rationale if len(rationale) <= 120 else rationale[:117] + "…"
            parts.append(rat)
    self_t = float(node.get("self_time") or 0)
    total_t = float(node.get("total_time") or 0)
    pct = (self_t / root_total * 100) if root_total > 0 else 0
    parts.append(f"Self: {_format_time(self_t)} ({pct:.1f}%)")
    if abs(total_t - self_t) > 0.001:
        parts.append(f"Total: {_format_time(total_t)}")
    rows = node.get("rows")
    if rows is not None:
        parts.append(f"Rows: {float(rows):.0f}")
    loops = node.get("loops")
    if loops and loops > 1:
        parts.append(f"Loops: {int(loops)}")
    table = details.get("table_name")
    if table:
        parts.append(f"Table: {table}")
    index = details.get("index_name")
    if index:
        parts.append(f"Index: {index}")
    access = details.get("access_type")
    if access:
        parts.append(f"Access: {access}")
    est = details.get("estimated_rows")
    if est is not None and rows is not None and float(est) > 0:
        ratio = float(rows) / float(est)
        est_str = f"Est.rows: {float(est):.0f}"
        if ratio > 2:
            est_str += f"  \u26a0 underestimate ({ratio:.1f}\u00d7)"
        elif ratio < 0.5 and float(rows) > 0:
            est_str += "  \u26a0 overestimate"
        parts.append(est_str)
    cond = details.get("condition")
    if cond:
        ellipsis = "\u2026"
        parts.append(f"Cond: {cond[:50] + ellipsis if len(cond) > 52 else cond}")
    return sep.join(parts)


def render_tree(root, width=1200, title="MySQL Query Plan", unit_display="ms", analysis=None, teach_index_by_folded=None):
    """Render an interactive collapsible plan tree SVG.

    Each row is one plan operation. Click the toggle triangle (▾/▸) to
    expand/collapse a subtree. Click anywhere else on a row to pin its
    details in the strip below. Ctrl+F to search.
    """
    rows_data = list(_flatten_with_depth(root))
    n_rows = len(rows_data)
    root_total = float(root.get("total_time") or 0.001)

    # Layout
    ROW_H = 26
    LEFT = 10
    INDENT = 18
    TOGGLE_W = 16
    LABEL_W = 400
    SELF_W = 90
    TOTAL_W = 90
    PCT_W = 55
    # Big O complexity column — auto-hidden on narrow canvases to avoid
    # crowding the self-time bar (same width threshold as bargraph).
    COMPLEXITY_W = 0 if width < 900 else 108
    HEADER_H = 72
    BAR_X = LEFT + LABEL_W + SELF_W + TOTAL_W + PCT_W + COMPLEXITY_W + 20
    BAR_MAX_W = max(80, width - BAR_X - LEFT - 10)

    self_col_x = LEFT + LABEL_W
    total_col_x = self_col_x + SELF_W
    pct_col_x = total_col_x + TOTAL_W
    complexity_col_x = pct_col_x + PCT_W

    details_lines_n = 6
    details_line_h = 16
    details_area_h = 20 + details_lines_n * details_line_h

    _info_lines, info_panel_h = (
        render_info_panel(analysis, LEFT, 0, width - 2 * LEFT, view_type="tree")
        if analysis is not None
        else ([], 0)
    )
    info_gap = 8 if analysis is not None else 0

    tree_body_h = n_rows * ROW_H
    bottom_y = HEADER_H + tree_body_h + 12
    total_h = bottom_y + details_area_h + info_gap + info_panel_h + 16

    # Analysis warning labels for row tinting
    highlight_set = set()
    if analysis is not None:
        for nh in (analysis.get("node_highlights") or []):
            lbl = (nh.get("short_label") or "").strip()
            if lbl:
                highlight_set.add(lbl)

    lines = [
        '<?xml version="1.0" standalone="no"?>',
        '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">',
        f'<svg id="tree-svg" version="1.1" width="{width}" height="{total_h}" viewBox="0 0 {width} {total_h}"'
        f' style="max-width:100%;height:auto;" onload="init(evt)"'
        f' xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">',
        "<style>",
        "  text { font-family: Arial, sans-serif; font-size: 11px; }",
        "  .title { font-size: 16px; font-weight: bold; }",
        "  .subtitle { font-size: 11px; fill: #666; }",
        "  .col-hdr { font-size: 10px; fill: #888; font-weight: bold; }",
        "  .row-label { font-size: 11px; fill: #222; }",
        "  .row-label.warn { fill: #b71c1c; }",
        "  .toggle { font-size: 13px; fill: #333; cursor: pointer; }",
        "  .toggle:hover { fill: #000; }",
        "  .leaf-indicator { font-size: 13px; fill: #bbb; cursor: default; }",
        "  .row-bg { cursor: pointer; transition: opacity 0.15s ease; }",
        "  .row-bg:hover { fill: rgba(0,0,0,0.04); }",
        "  .row-bg.pinned { fill: rgba(0,90,200,0.15); stroke: #1565c0; stroke-width: 1; }",
        "  .row-time { font-size: 11px; fill: #444; }",
        "  .row-pct { font-size: 11px; fill: #555; font-weight: bold; }",
        "  .complexity-chip-rect { pointer-events: none; }",
        "  .complexity-chip-text { font-size: 10px; font-weight: 700; fill: #0f172a; letter-spacing: 0.1px; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }",
        "  .details-line { font-size: 12px; fill: #222; user-select: text;"
        " -webkit-user-select: text; cursor: text; }",
        "  #search-btn { font-size: 11px; fill: #666; cursor: pointer; }",
        "  #search-btn:hover { fill: #0d47a1; }",
        "  .ctrl-link { font-size: 10px; fill: #1565c0; cursor: pointer; }",
        "  .ctrl-link:hover { fill: #0d47a1; }",
        "</style>",
        '<rect width="100%" height="100%" fill="#fafafa"/>',
        f'<text x="{width / 2}" y="26" text-anchor="middle" class="title">{xml_escape(title)}</text>',
        f'<text x="{LEFT}" y="44" text-anchor="start" class="subtitle">'
        f'Execution tree  \u00b7  Total: {_format_time(root_total)}'
        f'  \u00b7  Click row to pin  \u00b7  Ctrl+F to search</text>',
        f'<text id="expand-all" class="ctrl-link" x="{width - 100}" y="44" text-anchor="start">Expand All</text>',
        f'<text id="collapse-all" class="ctrl-link" x="{width - LEFT}" y="44" text-anchor="end">Collapse All</text>',
        f'<line x1="{LEFT}" y1="52" x2="{width - LEFT}" y2="52" stroke="#ddd" stroke-width="1"/>',
        f'<text x="{self_col_x + SELF_W - 4}" y="65" text-anchor="end" class="col-hdr">SELF</text>',
        f'<text x="{total_col_x + TOTAL_W - 4}" y="65" text-anchor="end" class="col-hdr">TOTAL</text>',
        f'<text x="{pct_col_x + PCT_W - 4}" y="65" text-anchor="end" class="col-hdr">%</text>',
    ]
    if COMPLEXITY_W > 0:
        lines.append(
            f'<text x="{complexity_col_x + COMPLEXITY_W/2}" y="65" text-anchor="middle" class="col-hdr">COMPLEXITY</text>'
        )
    lines.extend([
        f'<text x="{BAR_X}" y="65" text-anchor="start" class="col-hdr">SELF TIME BAR</text>',
        f'<text id="search-btn" x="{width - LEFT}" y="65" text-anchor="end">Search</text>',
        f'<line x1="{LEFT}" y1="68" x2="{width - LEFT}" y2="68" stroke="#ddd" stroke-width="1"/>',
    ])

    for idx, (node, depth) in enumerate(rows_data):
        y = HEADER_H + idx * ROW_H
        has_children = bool(node.get("children"))
        self_t = float(node.get("self_time") or 0)
        total_t = float(node.get("total_time") or 0)
        pct = (self_t / root_total * 100) if root_total > 0 else 0

        label = node.get("short_label") or ""
        if len(label) > 46:
            label = label[:45] + "\u2026"

        is_warn = (node.get("short_label") or "").strip() in highlight_set
        info_str = _row_info(node, root_total, unit_display)
        color = _row_color(self_t, root_total)

        self_bar_w = max(0.0, (self_t / root_total) * BAR_MAX_W) if root_total > 0 else 0.0
        total_bar_w = max(0.0, (total_t / root_total) * BAR_MAX_W) if root_total > 0 else 0.0

        text_y = y + ROW_H - 8
        indent_x = LEFT + depth * INDENT
        label_x = indent_x + TOGGLE_W

        folded = (node.get("folded_label") or "").strip()
        teach_attr = ""
        if teach_index_by_folded and folded in teach_index_by_folded:
            teach_attr = f' data-teach-index="{teach_index_by_folded[folded]}"'
        lines.append(
            f'<g class="tree-row" data-idx="{idx}" data-depth="{depth}"'
            f' data-has-children="{1 if has_children else 0}"{teach_attr}>'
        )
        # Hover/click background (full-width hit area)
        lines.append(
            f'<rect class="row-bg" x="{LEFT}" y="{y}" width="{width - 2 * LEFT}"'
            f' height="{ROW_H}" fill="transparent" data-info="{_attr_escape(info_str)}"/>'
        )
        # Warning left accent
        if is_warn:
            lines.append(
                f'<rect x="{LEFT}" y="{y}" width="4" height="{ROW_H}" fill="#e65100" pointer-events="none"/>'
            )
        # Zebra stripe
        if idx % 2 == 1:
            lines.append(
                f'<rect x="{LEFT}" y="{y}" width="{width - 2 * LEFT}" height="{ROW_H}"'
                f' fill="rgba(0,0,0,0.018)" pointer-events="none"/>'
            )
        # Toggle triangle (▾ expanded / · leaf)
        if has_children:
            lines.append(
                f'<text class="toggle" x="{indent_x + 2}" y="{text_y}"'
                f' data-idx="{idx}">\u25be</text>'
            )
        else:
            lines.append(
                f'<text class="leaf-indicator" x="{indent_x + 2}" y="{text_y}"'
                f' pointer-events="none">\u00b7</text>'
            )
        # Label
        warn_prefix = "\u26a0 " if is_warn else ""
        label_class = "row-label warn" if is_warn else "row-label"
        lines.append(
            f'<text class="{label_class}" x="{label_x}" y="{text_y}"'
            f' pointer-events="none">{xml_escape(warn_prefix + label)}</text>'
        )
        # Self time
        lines.append(
            f'<text class="row-time" x="{self_col_x + SELF_W - 4}" y="{text_y}"'
            f' text-anchor="end" pointer-events="none">{xml_escape(_format_time(self_t))}</text>'
        )
        # Total time (blank if same as self)
        if abs(total_t - self_t) > 0.001:
            lines.append(
                f'<text class="row-time" x="{total_col_x + TOTAL_W - 4}" y="{text_y}"'
                f' text-anchor="end" pointer-events="none">{xml_escape(_format_time(total_t))}</text>'
            )
        # Percentage
        pct_str = f"{pct:.0f}%" if pct >= 1 else f"{pct:.1f}%"
        lines.append(
            f'<text class="row-pct" x="{pct_col_x + PCT_W - 4}" y="{text_y}"'
            f' text-anchor="end" pointer-events="none">{xml_escape(pct_str)}</text>'
        )
        # Big O complexity chip (column is hidden on narrow canvases).
        if COMPLEXITY_W > 0:
            complexity = (node.get("details") or {}).get("complexity")
            if isinstance(complexity, dict) and complexity.get("short"):
                chip_short = complexity["short"]
                if complexity.get("confidence", "exact") != "exact":
                    chip_short = "~" + chip_short
                sev = complexity.get("severity", "medium")
                chip_fill = SEVERITY_COLORS.get(sev, SEVERITY_COLORS["medium"])
                chip_stroke = SEVERITY_BORDERS.get(sev, SEVERITY_BORDERS["medium"])
                chip_w = min(COMPLEXITY_W - 10, max(44, 7 * len(chip_short) + 14))
                chip_h = 16
                chip_x = complexity_col_x + (COMPLEXITY_W - chip_w) / 2
                chip_y = y + (ROW_H - chip_h) / 2
                chip_title = xml_escape(complexity.get("big_o") or chip_short)
                lines.append(
                    f'<g pointer-events="none">'
                    f'<title>{chip_title}</title>'
                    f'<rect class="complexity-chip-rect" x="{chip_x:.1f}" y="{chip_y:.1f}" '
                    f'width="{chip_w:.1f}" height="{chip_h}" rx="8" ry="8" '
                    f'fill="{chip_fill}" stroke="{chip_stroke}" stroke-width="0.8"/>'
                    f'<text x="{chip_x + chip_w/2:.1f}" y="{chip_y + chip_h - 4:.1f}" '
                    f'text-anchor="middle" class="complexity-chip-text">{xml_escape(chip_short)}</text>'
                    f'</g>'
                )
        # Total bar (outline, drawn first so self bar appears on top)
        if total_bar_w > 0.5:
            lines.append(
                f'<rect x="{BAR_X}" y="{y + 5}" width="{total_bar_w:.1f}" height="{ROW_H - 10}"'
                f' fill="none" stroke="#c0d8ee" stroke-width="1" rx="2" pointer-events="none"/>'
            )
        # Self bar (filled)
        if self_bar_w > 0.5:
            lines.append(
                f'<rect x="{BAR_X}" y="{y + 5}" width="{self_bar_w:.1f}" height="{ROW_H - 10}"'
                f' fill="{color}" rx="2" pointer-events="none"/>'
            )
        # Row separator
        lines.append(
            f'<line x1="{LEFT}" y1="{y + ROW_H}" x2="{width - LEFT}" y2="{y + ROW_H}"'
            f' stroke="#eeeeee" stroke-width="1"/>'
        )
        lines.append("</g>")

    # Details strip
    sep_y = bottom_y + 4
    details_y_start = sep_y + 20
    lines.append(
        f'<line x1="{LEFT}" y1="{sep_y}" x2="{width - LEFT}" y2="{sep_y}"'
        f' stroke="#e0e0e0" stroke-width="1"/>'
    )
    lines.append(
        f'<rect x="{LEFT}" y="{sep_y + 4}" width="{width - 2 * LEFT}"'
        f' height="{details_area_h}" fill="#eeeeee" rx="4"/>'
    )
    for di in range(details_lines_n):
        dy = details_y_start + di * details_line_h
        default_text = (
            "Click a row to pin details  \u00b7"
            "  Click \u25be/\u25b8 to expand/collapse  \u00b7  Ctrl+F to search"
            if di == 0 else ""
        )
        lines.append(
            f'<text id="details-l{di}" x="{LEFT + 10}" y="{dy}"'
            f' class="details-line">{xml_escape(default_text)}</text>'
        )

    # Query Analysis panel
    if analysis is not None:
        panel_y = bottom_y + details_area_h + info_gap + 16
        panel_lines, _ = render_info_panel(analysis, LEFT, panel_y, width - 2 * LEFT, view_type="tree")
        lines.extend(panel_lines)

    lines.append(f"""<script type="text/javascript"><![CDATA[
(function() {{
  var ROW_H = {ROW_H};
  var HEADER_H = {HEADER_H};
  var N_LINES = {details_lines_n};
  var detailLines = [];
  var searchBtn, svgEl;
  var defaultHint = "Click a row to pin details  \u00b7  Click \u25be/\u25b8 to expand/collapse  \u00b7  Ctrl+F to search";
  var pinnedBg = null;
  var searchActive = false;
  var rows = [];

  function setDetailsText(parts) {{
    for (var i = 0; i < N_LINES; i++) {{
      if (detailLines[i]) detailLines[i].textContent = i < parts.length ? parts[i] : "";
    }}
  }}
  function clearDetails() {{
    if (detailLines[0]) detailLines[0].textContent = defaultHint;
    for (var i = 1; i < N_LINES; i++) if (detailLines[i]) detailLines[i].textContent = "";
  }}
  function showInfo(infoStr) {{
    if (!infoStr) {{ clearDetails(); return; }}
    setDetailsText(infoStr.split("  \u00b7  "));
  }}

  function init(evt) {{
    svgEl = document.querySelector("svg");
    for (var i = 0; i < N_LINES; i++) {{
      var el = document.getElementById("details-l" + i);
      if (el) detailLines.push(el);
    }}
    searchBtn = document.getElementById("search-btn");

    var rowEls = document.querySelectorAll(".tree-row");
    for (var i = 0; i < rowEls.length; i++) {{
      rows.push({{
        el: rowEls[i],
        idx: i,
        depth: parseInt(rowEls[i].getAttribute("data-depth"), 10),
        hasChildren: rowEls[i].getAttribute("data-has-children") === "1",
        hidden: false,
        collapsed: false
      }});
    }}

    document.addEventListener("click", function(e) {{
      var t = e.target;
      if (t.id === "search-btn") {{ searchPrompt(); return; }}
      if (t.id === "expand-all") {{ expandAll(); return; }}
      if (t.id === "collapse-all") {{ collapseAll(); return; }}
      if (t.classList && t.classList.contains("toggle")) {{
        var idx = parseInt(t.getAttribute("data-idx"), 10);
        if (!isNaN(idx) && rows[idx] && rows[idx].hasChildren) toggle(idx);
        return;
      }}
      if (t.classList && t.classList.contains("row-bg")) {{
        var info = t.getAttribute("data-info") || "";
        if (pinnedBg === t) {{
          t.classList.remove("pinned");
          pinnedBg = null;
          clearDetails();
        }} else {{
          if (pinnedBg) pinnedBg.classList.remove("pinned");
          pinnedBg = t;
          t.classList.add("pinned");
          showInfo(info);
        }}
        return;
      }}
      if (pinnedBg) {{
        pinnedBg.classList.remove("pinned");
        pinnedBg = null;
        clearDetails();
      }}
    }});

    document.addEventListener("mouseover", function(e) {{
      if (!pinnedBg && e.target.classList && e.target.classList.contains("row-bg"))
        showInfo(e.target.getAttribute("data-info") || "");
    }});
    document.addEventListener("mouseout", function(e) {{
      if (!pinnedBg && e.target.classList && e.target.classList.contains("row-bg"))
        clearDetails();
    }});
    document.addEventListener("keydown", function(e) {{
      if (e.ctrlKey && e.key === "f") {{ e.preventDefault(); searchPrompt(); }}
    }});
  }}

  function toggle(idx) {{
    var collapsing = !rows[idx].collapsed;
    rows[idx].collapsed = collapsing;
    var toggleEl = rows[idx].el.querySelector(".toggle");
    if (toggleEl) toggleEl.textContent = collapsing ? "\u25b8" : "\u25be";
    setSubtreeHidden(idx, collapsing);
    reflow();
  }}

  function setSubtreeHidden(parentIdx, hidden) {{
    var depth = rows[parentIdx].depth;
    for (var j = parentIdx + 1; j < rows.length; j++) {{
      if (rows[j].depth <= depth) break;
      rows[j].hidden = hidden;
      if (!hidden && rows[j].collapsed) setSubtreeHidden(j, true);
    }}
  }}

  function reflow() {{
    var y = HEADER_H;
    for (var i = 0; i < rows.length; i++) {{
      if (rows[i].hidden) {{
        rows[i].el.style.display = "none";
      }} else {{
        rows[i].el.style.display = "";
        var origY = HEADER_H + i * ROW_H;
        var dy = y - origY;
        rows[i].el.setAttribute("transform", dy !== 0 ? "translate(0," + dy + ")" : "");
        y += ROW_H;
      }}
    }}
  }}

  function expandAll() {{
    for (var i = 0; i < rows.length; i++) {{
      rows[i].hidden = false;
      rows[i].collapsed = false;
      var t = rows[i].el.querySelector(".toggle");
      if (t && rows[i].hasChildren) t.textContent = "\u25be";
    }}
    reflow();
  }}

  function collapseAll() {{
    for (var i = 0; i < rows.length; i++) {{
      if (rows[i].depth > 0) rows[i].hidden = true;
      if (rows[i].hasChildren) {{
        rows[i].collapsed = true;
        var t = rows[i].el.querySelector(".toggle");
        if (t) t.textContent = "\u25b8";
      }}
    }}
    reflow();
  }}

  function searchPrompt() {{
    if (searchActive) {{
      var bgs = document.querySelectorAll(".row-bg");
      for (var i = 0; i < bgs.length; i++) bgs[i].style.opacity = "";
      searchBtn.textContent = "Search";
      searchActive = false;
      clearDetails();
      return;
    }}
    var term = prompt("Search (regex):");
    if (term == null) return;
    var re;
    try {{ re = new RegExp(term, "i"); }} catch (err) {{ alert("Invalid regex"); return; }}
    var bgs = document.querySelectorAll(".row-bg");
    for (var i = 0; i < bgs.length; i++) {{
      var info = bgs[i].getAttribute("data-info") || "";
      bgs[i].style.opacity = re.test(info) ? "1" : "0.2";
    }}
    searchBtn.textContent = "Reset Search";
    searchActive = true;
    setDetailsText(["Matches: " + term + "  \u00b7  Click \u2018Reset Search\u2019 to clear"]);
  }}

  window.init = init;
}})();
]]></script>
</svg>
""")
    return "\n".join(lines)
