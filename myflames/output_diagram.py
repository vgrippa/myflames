"""Generate Visual Explain–style execution plan diagram from parsed EXPLAIN tree.

Implementation follows the replication guide in:
  mysql-workbench/docs/VISUAL_EXPLAIN_PLAN_CONTEXT.md

That document describes MySQL Workbench's Visual Explain: data source (EXPLAIN FORMAT=JSON),
handler chain (query_block → nested_loop/table/operations), node model (QueryBlockNode,
NestedLoopNode, TableNode, etc.), layout, and rendering. Our mapping:

- Data: We consume MySQL 8.4 EXPLAIN ANALYZE FORMAT=JSON (root with `operation` + `inputs[]`).
  For 5.6/5.7 style (query_block + nested_loop array) a separate parser would be needed;
  our parser produces an equivalent tree for 8.4, and this module renders it.

- Node model (context §4.3, §5):
  - QueryBlockNode → "query_block #1" box (rightmost).
  - NestedLoopNode → diamond "nested loop"; two inputs: child_aside (left/main line) and
    child_below (inner table, drawn below diamond with vertical arrow). Layout matches
    context: "one child to the side, one below; horizontal spacing between branches."
  - TableNode → rectangle with access type label, table name, key (and time/cost/rows).
  - We add time-based hot/cold coloring (cold=blue, hot=red); Workbench colors by access_type.

- Layout (context §6): We use similar spacing (vspacing/hspacing ~50), global_padding 20,
  and mirror the "diamond centered over the join; one child to the side, one below."

- Cost/rows: Context §5 – "Number in top left = cost; number to the right of diamonds = rows
  produced." We show actual time (with unit) above each node and row counts on arrows and
  below boxes; cost in tooltips when available (estimated_total_cost).

- Tooltips: Per-node hint text (context §5.1, §10 item 6); we show operation, time, cost,
  rows, loops, table, index in details bar and in <title>.
"""
import math
import re
from .parser import xml_escape, build_diagram_steps, render_info_panel

# Sequential color scale: low time = light, high time = dark (standard for intensity/cost)
# ColorBrewer-style sequential Blues: light #deebf7 -> dark #2171b5
_LOW_RGB = (0xde, 0xeb, 0xf7)   # light blue (low time)
_HIGH_RGB = (0x21, 0x71, 0xb5)  # dark blue (high time)


def _text_color(fill_hex):
    """Return dark (#111) or white (#fff) for best contrast on fill_hex background."""
    try:
        r = int(fill_hex[1:3], 16)
        g = int(fill_hex[3:5], 16)
        b = int(fill_hex[5:7], 16)
        luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
        return "#111111" if luminance > 0.45 else "#ffffff"
    except (ValueError, IndexError):
        return "#111111"


def _join_type_label(node):
    """Extract a short join type label from the node operation string."""
    op = (node.get("full_label") or (node.get("details") or {}).get("operation") or "").lower()
    if "hash" in op:
        return "hash join"
    if "anti" in op:
        return "anti join"
    if "semi" in op:
        return "semi join"
    if "left" in op:
        return "left join"
    if "inner" in op:
        return "inner join"
    return "join"


def _time_to_fill(time_ms, max_time_ms):
    """Return SVG fill color (hex): sequential blue — light = low self-time, dark = high.
    Uses logarithmic scale so small and large values are visually distinct."""
    if max_time_ms is None or max_time_ms <= 0 or time_ms is None or time_ms < 0:
        return "#e0e0e0"  # neutral light grey
    t = float(time_ms)
    m = float(max_time_ms)
    if t <= 0:
        return "#e0e0e0"
    ratio = math.log1p(t) / math.log1p(m)
    ratio = min(1.0, max(0.0, ratio))
    r = int(_LOW_RGB[0] + (_HIGH_RGB[0] - _LOW_RGB[0]) * ratio)
    g = int(_LOW_RGB[1] + (_HIGH_RGB[1] - _LOW_RGB[1]) * ratio)
    b = int(_LOW_RGB[2] + (_HIGH_RGB[2] - _LOW_RGB[2]) * ratio)
    return f"#{r:02x}{g:02x}{b:02x}"


def _format_time(ms):
    """Human-readable time: seconds (≥1000 ms), milliseconds (≥1 ms), or microseconds (<1 ms)."""
    if ms is None or (isinstance(ms, float) and ms <= 0):
        return "—"
    t = float(ms)
    if t >= 1000:
        return f"{t/1000:.2f} s"
    if t >= 1:
        return f"{t:.2f} ms" if t < 100 else f"{t:.0f} ms"
    # Sub-millisecond: show in microseconds
    return f"{t*1000:.1f} µs" if t >= 0.001 else f"{t*1e6:.0f} µs"


def _access_diagram_label(node):
    """Return (label, sublabel, is_table_scan) for diagram box."""
    details = node.get("details") or {}
    op = (node.get("full_label") or details.get("operation") or "").replace("`", "")
    table = node.get("table_name") or details.get("table_name") or ""
    index = node.get("index_name") or details.get("index_name") or ""
    if re.match(r"^Table scan", op, re.I):
        return ("Full Table Scan", table, True)
    if re.match(r"^Single-row index lookup", op, re.I):
        return ("Unique Key Lookup", f"{table}\n{index}" if index else table, False)
    if re.match(r"^Index lookup", op, re.I):
        return ("Non-Unique Key Lookup", f"{table}\n{index}" if index else table, False)
    if re.match(r"^Index range scan", op, re.I):
        return ("Non-Unique Key Lookup", f"{table}\n{index}" if index else table, False)
    if re.match(r"^Index scan", op, re.I):
        return ("Index Scan", f"{table}\n{index}" if index else table, False)
    if re.match(r"^Covering index", op, re.I):
        return ("Covering Index", f"{table}\n{index}" if index else table, False)
    # Generic
    short = (op[:30] + "..") if len(op) > 32 else op
    return (short, table or "—", False)


def _format_rows(rows):
    if rows is None:
        return "—"
    r = float(rows)
    if r >= 1000:
        return f"{r/1000:.2f}K"
    return f"{int(r + 0.5)}"


def _format_loops(loops):
    """Format loop count for display (e.g. 30938 → 30.9K)."""
    if loops is None or loops < 1:
        return None
    n = int(loops)
    if n >= 1000:
        return f"{n/1000:.1f}K".replace(".0K", "K")
    return str(n)


def _node_tooltip(node, kind, time_str, cost_val, rows_str, total_ms=None, self_ms=None):
    """Build rich tooltip: plain-English hint first, then full technical details."""
    details = node.get("details") or {}
    parts = []
    # Plain-English hint for junior DBAs
    covering = details.get("covering")
    loops = node.get("loops") or details.get("actual_loops")
    if kind == "access" and covering is True:
        parts.append("Index only (no table read – often faster).")
    if loops is not None and loops > 1:
        parts.append(f"Ran {_format_loops(loops) or loops} times (e.g. inner side of nested loop).")
    if parts:
        parts.append("—")
    # Technical: operation
    op = node.get("full_label") or details.get("operation") or node.get("short_label") or "—"
    op_short = (op[:70] + "…") if len(op) > 72 else op
    parts.append(op_short)
    if time_str and time_str != "—":
        parts.append(f"Total: {time_str}")
    if total_ms is not None and self_ms is not None and self_ms != total_ms:
        parts.append(f"Self: {_format_time(self_ms)}")
    if cost_val is not None and cost_val > 0:
        parts.append(f"Cost: {cost_val:.2f}")
    if rows_str and rows_str != "—":
        parts.append(f"Rows: {rows_str}")
    loops = node.get("loops") or details.get("actual_loops")
    if loops is not None:
        parts.append(f"Loops: {loops}")
    table = node.get("table_name") or details.get("table_name")
    if table:
        parts.append(f"Table: {table}")
    index = details.get("index_name")
    if index:
        parts.append(f"Index: {index}")
    est_rows = details.get("estimated_rows")
    if est_rows is not None:
        parts.append(f"Est.rows: {est_rows:.0f}" if isinstance(est_rows, (int, float)) else f"Est.rows: {est_rows}")
    cond = details.get("condition")
    if cond:
        c_short = (cond[:50] + "…") if len(cond) > 52 else cond
        parts.append(f"Cond: {c_short}")
    ranges = details.get("ranges") or []
    if ranges:
        parts.append("Ranges: " + "; ".join(str(r) for r in ranges[:2]))
    if details.get("covering") is not None:
        parts.append("Covering: " + ("yes" if details["covering"] else "no"))
    return "  ·  ".join(parts)


def render_diagram(root, width=1200, title="MySQL Query Plan", unit_display="ms", analysis=None):
    """Render a left-to-right execution plan diagram (Visual Explain style).
    Each nested loop shows two inputs: outer (main line) and inner (branch from below into the diamond).
    """
    steps = build_diagram_steps(root)
    if not steps:
        return _empty_svg(width, title)

    # Layout constants (VISUAL_EXPLAIN_PLAN_CONTEXT.md §6: vspacing=50, hspacing=50, global_padding=20)
    global_padding = 20
    vspacing = 50
    hspacing = 50
    pad = global_padding
    box_w = 240
    box_h = 112
    diamond_r = 56
    arrow_len = hspacing
    row_h = 100
    cost_y_offset = -20
    title_h = 56
    bottom_pad = 72
    gap_between_rows = vspacing
    inner_row_offset = box_h + gap_between_rows
    # Per-join horizontal step: must be >= box_w so inner boxes (below diamonds) don't overlap
    join_segment = max(2 * diamond_r + arrow_len, box_w + hspacing)

    # Build list of (kind, node, total_time_ms, self_time_ms, cost, rows) for each step
    # Color by self_time (block's own contribution) so the query block isn't always hottest
    items = []
    for s in steps:
        node = s["node"]
        details = node.get("details") or {}
        total_ms = float(node.get("total_time") or 0)
        self_ms = float(node.get("self_time") or 0)
        cost = details.get("estimated_total_cost")
        cost_val = float(cost) if cost is not None else None
        rows = node.get("rows") or node.get("actual_rows") or details.get("actual_rows")
        rows_out = node.get("actual_rows") or rows
        items.append((s["type"], s["node"], total_ms, self_ms, cost_val, rows_out))
    total_time_ms = float(root.get("total_time") or 0)
    total_cost = root.get("details", {}).get("estimated_total_cost")
    total_cost_val = float(total_cost) if total_cost is not None else None

    # Split into main line (first access + all joins) and inner accesses (one per join)
    # items = [access0, join0, access_inner0, join1, access_inner1, ...]
    main_line = [items[0]]  # first (outer) access
    inner_list = []
    for j in range(1, len(items), 2):
        main_line.append(items[j])  # join
        if j + 1 < len(items):
            inner_list.append(items[j + 1])  # inner access for this join

    num_joins = len(inner_list)
    # Each stage shows self-time (time in that stage only). Color scale by self_time.
    root_self_ms = float(root.get("self_time") or 0)
    max_self_ms = root_self_ms
    for it in items:
        s = it[3]
        if s is not None and s > 0:
            max_self_ms = max(max_self_ms or 0, s)
    if not max_self_ms or max_self_ms <= 0:
        max_self_ms = 1.0

    # Main line: first access, then join segment per join, then query block (no overlap of inner boxes)
    total_width = pad * 2 + box_w + arrow_len + num_joins * join_segment + arrow_len + 160
    total_width = max(width, total_width)
    y_main = title_h + row_h
    y_inner = y_main + inner_row_offset
    diagram_height = y_inner + box_h + 56 + bottom_pad

    _info_lines, info_panel_h = (
        render_info_panel(analysis, pad, 0, total_width - 2 * pad, view_type="diagram")
        if analysis is not None
        else ([], 0)
    )
    info_gap = 8 if analysis is not None else 0
    height = diagram_height + info_gap + info_panel_h

    # Escape for use in HTML attribute (tooltip / details bar)
    def attr_escape(s):
        if s is None:
            return ""
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("\n", " ")

    total_time_str = _format_time(total_time_ms)

    highlight_msg_by_label = {}
    if analysis is not None:
        for nh in analysis.get("node_highlights") or []:
            lbl = (nh.get("short_label") or "").strip()
            if lbl:
                highlight_msg_by_label[lbl] = nh.get("message") or ""

    details_lines_n = 6
    details_line_h = 16
    details_area_h = 16 + details_lines_n * details_line_h  # ~112px strip
    details_y_start = diagram_height - bottom_pad + 14
    details_sep_y = diagram_height - bottom_pad + 2

    lines = [
        '<?xml version="1.0" standalone="no"?>',
        '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">',
        f'<svg version="1.1" width="{total_width}" height="{height}" onload="init(evt)" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">',
        '<defs><marker id="arrowhead" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto"><polygon points="0 0, 10 3.5, 0 7" fill="#333"/></marker></defs>',
        "<style>",
        "  text { font-family: Arial, sans-serif; font-size: 11px; }",
        "  .title { font-size: 16px; font-weight: bold; }",
        "  .subtitle { font-size: 11px; fill: #666; }",
        "  .cost { font-size: 10px; fill: #555; }",
        "  .node-label { font-weight: bold; font-size: 11px; }",
        "  .node-sublabel { font-size: 9px; }",
        "  .row-count { font-size: 10px; }",
        "  .arrow-label { font-size: 9px; fill: #444; }",
        "  .diagram-node { cursor: pointer; }",
        "  .diagram-node:hover rect, .diagram-node:hover polygon { opacity: 0.9; stroke-width: 2; }",
        "  .badge-covering { font-size: 8px; fill: #1b5e20; font-weight: bold; }",
        "  .loops-line { font-size: 9px; fill: #555; }",
        "  .details-line { font-size: 12px; fill: #222; user-select: text; -webkit-user-select: text; cursor: text; }",
        "  .details-line.dim { fill: #999; }",
        "  #search { font-size: 12px; fill: #666; cursor: pointer; }",
        "  #search:hover { fill: #000; }",
        "  .diagram-node.pinned rect, .diagram-node.pinned polygon { stroke: #e600e6; stroke-width: 3; }",
        "  .diagram-node.dim { opacity: 0.25; }",
        "</style>",
        '<rect width="100%" height="100%" fill="#fafafa"/>',
        f'<text x="{total_width/2}" y="28" text-anchor="middle" class="title">{xml_escape(title)}</text>',
        f'<text x="{total_width/2}" y="46" text-anchor="middle" class="subtitle">Execution plan (left \u2192 right)  \u00b7  Total: {total_time_str}  \u00b7  Darker = slower  \u00b7  Click node to pin details</text>',
        # Separator + details area
        f'<line x1="{pad}" y1="{details_sep_y}" x2="{total_width - pad}" y2="{details_sep_y}" stroke="#e0e0e0" stroke-width="1"/>',
        f'<rect x="{pad}" y="{details_sep_y + 4}" width="{total_width - 2*pad}" height="{details_area_h}" fill="#f5f5f5" rx="4"/>',
        f'<text id="search" x="{total_width - pad - 8}" y="{details_y_start}" text-anchor="end">Search</text>',
    ]
    # Pre-allocate detail lines
    for di in range(details_lines_n):
        dy = details_y_start + di * details_line_h
        default_text = "Click a node to pin details  \u00b7  Scroll to zoom (graph only)  \u00b7  Drag to pan  \u00b7  Dbl-click to reset  \u00b7  Ctrl+F to search" if di == 0 else ""
        lines.append(
            f'<text id="details-l{di}" x="{pad + 10}" y="{dy}" '
            f'text-anchor="start" class="details-line">{xml_escape(default_text)}</text>'
        )

    lines.append('<g id="diagram-content">')

    clip_counter = [0]

    def draw_access_box(x, y, kind, node, total_ms, self_ms, cost_val, rows):
        # Show self-time on the box (time spent in this stage only)
        time_str = _format_time(self_ms) if self_ms is not None else "—"
        rows_str = _format_rows(rows)
        total_str = _format_time(total_ms) if total_ms else "—"
        tooltip = _node_tooltip(node, kind, total_str, cost_val, rows_str, total_ms, self_ms)
        analysis_msg = highlight_msg_by_label.get((node.get("short_label") or "").strip(), "")
        if analysis_msg:
            tooltip = tooltip + "  ·  ⚠ In Query Analysis: " + analysis_msg
        info_attr = attr_escape(tooltip)
        analysis_attr = attr_escape(analysis_msg)[:400] if analysis_msg else ""
        label, sublabel, _ = _access_diagram_label(node)
        fill = _time_to_fill(self_ms, max_self_ms)
        cid = clip_counter[0]
        clip_counter[0] += 1
        node_extra = f' data-analysis-msg="{analysis_attr}"' if analysis_attr else ""
        lines.append(f'<g class="diagram-node" data-info="{info_attr}"{node_extra}>')
        lines.append(f'<title>{xml_escape(tooltip)}</title>')
        lines.append(f'<defs><clipPath id="clipbox-{cid}"><rect x="{x}" y="{y}" width="{box_w}" height="{box_h}" rx="6" ry="6"/></clipPath></defs>')
        lines.append(f'<g clip-path="url(#clipbox-{cid})">')
        lines.append(f'<rect x="{x}" y="{y}" width="{box_w}" height="{box_h}" fill="{fill}" stroke="#333" stroke-width="1" rx="6" ry="6"/>')
        tc = _text_color(fill)
        # Covering index badge (top-right)
        details = node.get("details") or {}
        if details.get("covering") is True:
            lines.append(f'<text x="{x + box_w - 8}" y="{y + 14}" text-anchor="end" class="badge-covering" fill="{tc}">Index only</text>')
        # All text inside the box: time (top), label, sublabels, loops (if >1), row count (bottom)
        lines.append(f'<text x="{x + box_w/2}" y="{y + 16}" text-anchor="middle" class="cost" fill="{tc}">{time_str}</text>')
        label_short = (label[:20] + "…") if len(label) > 22 else label
        lines.append(f'<text x="{x + box_w/2}" y="{y + 34}" text-anchor="middle" class="node-label" fill="{tc}">{xml_escape(label_short)}</text>')
        sublines = sublabel.split("\n")[:3]
        for j, line in enumerate(sublines):
            line_short = (line[:24] + "…") if len(line) > 26 else line
            lines.append(f'<text x="{x + box_w/2}" y="{y + 52 + j*16}" text-anchor="middle" class="node-sublabel" fill="{tc}">{xml_escape(line_short)}</text>')
        loops = node.get("loops") or details.get("actual_loops")
        loops_str = _format_loops(loops) if (loops and loops > 1) else None
        if loops_str:
            lines.append(f'<text x="{x + box_w/2}" y="{y + box_h - 24}" text-anchor="middle" class="loops-line" fill="{tc}">↻ {xml_escape(loops_str)} loops</text>')
        lines.append(f'<text x="{x + box_w/2}" y="{y + box_h - 10}" text-anchor="middle" class="row-count" fill="{tc}">{rows_str} rows</text>')
        lines.append("</g>")
        lines.append("</g>")

    def draw_join_diamond(cx, cy, node, total_ms, self_ms, cost_val, rows):
        # Show self-time on the diamond (time spent in this join stage only)
        time_str = _format_time(self_ms) if self_ms is not None else "—"
        rows_str = _format_rows(rows)
        total_str = _format_time(total_ms) if total_ms else "—"
        tooltip = _node_tooltip(node, "join", total_str, cost_val, rows_str, total_ms, self_ms)
        analysis_msg = highlight_msg_by_label.get((node.get("short_label") or "").strip(), "")
        if analysis_msg:
            tooltip = tooltip + "  ·  ⚠ In Query Analysis: " + analysis_msg
        info_attr = attr_escape(tooltip)
        analysis_attr = attr_escape(analysis_msg)[:400] if analysis_msg else ""
        fill = _time_to_fill(self_ms, max_self_ms)
        tc = _text_color(fill)
        pts = [
            (cx, cy - diamond_r),
            (cx + diamond_r, cy),
            (cx, cy + diamond_r),
            (cx - diamond_r, cy),
        ]
        path_pts = " ".join(f"{p[0]},{p[1]}" for p in pts)
        j_extra = f' data-analysis-msg="{analysis_attr}"' if analysis_attr else ""
        lines.append(f'<g class="diagram-node" data-info="{info_attr}"{j_extra}>')
        lines.append(f'<title>{xml_escape(tooltip)}</title>')
        # Self-time above the diamond (outside, always readable)
        lines.append(f'<text x="{cx}" y="{cy - diamond_r - 6}" text-anchor="middle" class="cost">{time_str}</text>')
        lines.append(f'<polygon points="{path_pts}" fill="{fill}" stroke="#333" stroke-width="1"/>')
        # Join type label at center
        join_lbl = _join_type_label(node)
        lines.append(f'<text x="{cx}" y="{cy - 4}" text-anchor="middle" class="node-label" fill="{tc}">{xml_escape(join_lbl)}</text>')
        # Rows produced just below center
        rows_out_text = rows_str + " rows"
        lines.append(f'<text x="{cx}" y="{cy + 12}" text-anchor="middle" class="node-sublabel" fill="{tc}">{xml_escape(rows_out_text)}</text>')
        jloops = node.get("loops") or (node.get("details") or {}).get("actual_loops")
        jloops_str = _format_loops(jloops) if (jloops and jloops > 1) else None
        if jloops_str:
            lines.append(f'<text x="{cx}" y="{cy + 26}" text-anchor="middle" class="loops-line" fill="{tc}">↻ {xml_escape(jloops_str)}</text>')
        lines.append("</g>")

    cy_main = y_main + box_h / 2

    # ---- Main line: first access ----
    x = pad
    kind, node, total_ms, self_ms, cost_val, rows = main_line[0]
    draw_access_box(x, y_main, kind, node, total_ms, self_ms, cost_val, rows)
    x += box_w
    # Arrow to first join
    lines.append(f'<line x1="{x}" y1="{cy_main}" x2="{x + arrow_len}" y2="{cy_main}" stroke="#333" stroke-width="2" marker-end="url(#arrowhead)"/>')
    lines.append(f'<text x="{x + arrow_len/2}" y="{cy_main - 6}" text-anchor="middle" class="arrow-label">{_format_rows(rows)} rows</text>')
    x += arrow_len

    # ---- Joins on main line, with inner accesses below ----
    for k in range(num_joins):
        join_item = main_line[k + 1]
        jkind, jnode, jtotal_ms, jself_ms, jcost_val, jrows = join_item
        cx = x + diamond_r
        cy = cy_main
        draw_join_diamond(cx, cy, jnode, jtotal_ms, jself_ms, jcost_val, jrows)
        # Inner access below this join
        if k < len(inner_list):
            inner_item = inner_list[k]
            ikind, inode, itotal_ms, iself_ms, icost_val, irows = inner_item
            inner_x = cx - box_w / 2
            draw_access_box(inner_x, y_inner, ikind, inode, itotal_ms, iself_ms, icost_val, irows)
            # Arrow from inner box (top center) up to diamond (bottom vertex)
            ax1, ay1 = cx, y_inner
            ax2, ay2 = cx, cy + diamond_r
            lines.append(f'<line x1="{ax1}" y1="{ay1}" x2="{ax2}" y2="{ay2}" stroke="#333" stroke-width="2" marker-end="url(#arrowhead)"/>')
            lines.append(f'<text x="{cx + 14}" y="{(ay1+ay2)/2}" text-anchor="start" class="arrow-label">{_format_rows(irows)} rows</text>')
        x += 2 * diamond_r
        # Arrow from right tip of diamond to left tip of next (or to query block); full gap so arrows connect
        arrow_h_len = join_segment - 2 * diamond_r
        if k < num_joins - 1:
            lines.append(f'<line x1="{x}" y1="{cy_main}" x2="{x + arrow_h_len}" y2="{cy_main}" stroke="#333" stroke-width="2" marker-end="url(#arrowhead)"/>')
            lines.append(f'<text x="{x + arrow_h_len/2}" y="{cy_main - 6}" text-anchor="middle" class="arrow-label">{_format_rows(jrows)} rows</text>')
            x += arrow_h_len
        else:
            lines.append(f'<line x1="{x}" y1="{cy_main}" x2="{x + arrow_h_len}" y2="{cy_main}" stroke="#333" stroke-width="2" marker-end="url(#arrowhead)"/>')
            lines.append(f'<text x="{x + arrow_h_len/2}" y="{cy_main - 6}" text-anchor="middle" class="arrow-label">{_format_rows(jrows)} rows</text>')
            x += arrow_h_len

    # ---- Query block (rightmost); show total, color by root self-time ----
    qbox_w = 160
    qbox_h = 56
    qbox_fill = _time_to_fill(root_self_ms, max_self_ms)
    qbox_tc = _text_color(qbox_fill)
    root_self_str = _format_time(root_self_ms) if root_self_ms else "—"
    qbox_tooltip = f"Total: {total_time_str}"
    if total_cost_val is not None:
        qbox_tooltip += f" | Cost: {total_cost_val:.2f}"
    lines.append(f'<g class="diagram-node" data-info="{attr_escape(qbox_tooltip)}">')
    lines.append(f'<title>{xml_escape(qbox_tooltip)}</title>')
    lines.append(f'<rect x="{x}" y="{y_main + (box_h - qbox_h)/2}" width="{qbox_w}" height="{qbox_h}" fill="{qbox_fill}" stroke="#333" stroke-width="1" rx="6"/>')
    lines.append(f'<text x="{x + qbox_w/2}" y="{y_main + box_h/2 - 8}" text-anchor="middle" class="node-label" fill="{qbox_tc}">query_block #1</text>')
    lines.append(f'<text x="{x + qbox_w/2}" y="{y_main + box_h/2 + 10}" text-anchor="middle" class="cost" fill="{qbox_tc}">{root_self_str}</text>')
    lines.append("</g>")

    lines.append("</g>")  # close diagram-content

    lines.append(f"""<script type="text/javascript"><![CDATA[
(function() {{
  var N_LINES = {details_lines_n};
  var detailLines = [];
  var searchBtn, content, svgEl;
  var diagramBottom = {diagram_height};
  var defaultHint = "Click a node to pin details  \u00b7  Scroll (graph only) to zoom  \u00b7  Drag to pan  \u00b7  Dbl-click to reset  \u00b7  Ctrl+F to search";
  var pinned = false, searchActive = false;
  var vx = 0, vy = 0, vs = 1;
  var dragging = false, dragStart = {{x:0,y:0}}, dragOrigin = {{x:0,y:0}};

  function svgYFromEvent(e) {{
    var rect = svgEl.getBoundingClientRect();
    var svgH = parseFloat(svgEl.getAttribute('height')) || rect.height;
    var scale = rect.height > 0 ? svgH / rect.height : 1;
    return (e.clientY - rect.top) * scale;
  }}

  function setDetailsText(parts) {{
    for (var i = 0; i < N_LINES; i++) {{
      if (!detailLines[i]) continue;
      detailLines[i].textContent = (i < parts.length) ? parts[i] : "";
    }}
  }}

  function clearDetails() {{
    if (detailLines[0]) detailLines[0].textContent = defaultHint;
    for (var i = 1; i < N_LINES; i++) {{
      if (detailLines[i]) detailLines[i].textContent = "";
    }}
  }}

  function setDetailsForNode(infoStr) {{
    if (!infoStr) {{ clearDetails(); return; }}
    var sep = "  \u00b7  ";
    var parts = infoStr.split(sep);
    setDetailsText(parts);
  }}

  function init(evt) {{
    svgEl = document.querySelector("svg");
    content = document.getElementById("diagram-content");
    searchBtn = document.getElementById("search");
    for (var i = 0; i < N_LINES; i++) {{
      var el = document.getElementById("details-l" + i);
      if (el) detailLines.push(el);
    }}
    if (!content) return;

    // Zoom: only within diagram area (not title bar or info panel below)
    svgEl.addEventListener("wheel", function(e) {{
      if (svgYFromEvent(e) > diagramBottom) return;
      e.preventDefault();
      var delta = e.deltaY > 0 ? 0.85 : 1.18;
      var rect = svgEl.getBoundingClientRect();
      var mx = e.clientX - rect.left, my = e.clientY - rect.top;
      vx = mx + (vx - mx) * delta;
      vy = my + (vy - my) * delta;
      vs *= delta;
      applyTransform();
    }}, {{passive: false}});

    // Drag: only in diagram area, not on text and not on nodes
    svgEl.addEventListener("mousedown", function(e) {{
      var tag = e.target.tagName ? e.target.tagName.toLowerCase() : '';
      if (tag === 'text' || tag === 'tspan') return;
      if (e.target.closest && e.target.closest(".diagram-node")) return;
      if (svgYFromEvent(e) > diagramBottom) return;
      dragging = true;
      dragStart = {{x: e.clientX, y: e.clientY}};
      dragOrigin = {{x: vx, y: vy}};
      svgEl.style.cursor = "grabbing";
      e.preventDefault();
    }});
    document.addEventListener("mousemove", function(e) {{
      if (!dragging) return;
      vx = dragOrigin.x + (e.clientX - dragStart.x);
      vy = dragOrigin.y + (e.clientY - dragStart.y);
      applyTransform();
    }});
    document.addEventListener("mouseup", function() {{
      if (dragging) {{ dragging = false; svgEl.style.cursor = ""; }}
    }});
    svgEl.addEventListener("dblclick", function(e) {{
      if (e.target.closest && e.target.closest(".diagram-node")) return;
      if (svgYFromEvent(e) > diagramBottom) return;
      vx = 0; vy = 0; vs = 1;
      applyTransform();
    }});

    // Hover and click-to-pin on nodes
    var nodes = document.querySelectorAll(".diagram-node");
    for (var i = 0; i < nodes.length; i++) {{
      nodes[i].addEventListener("mouseover", onHover);
      nodes[i].addEventListener("mouseout", onOut);
      nodes[i].addEventListener("click", onNodeClick);
    }}
    svgEl.addEventListener("click", function(e) {{
      if (pinned && !(e.target.closest && e.target.closest(".diagram-node"))) unpinAll();
    }});

    if (searchBtn) searchBtn.addEventListener("click", searchPrompt);
    document.addEventListener("keydown", function(e) {{
      if (e.ctrlKey && e.key === "f") {{ e.preventDefault(); searchPrompt(); }}
    }});
  }}

  function applyTransform() {{
    content.setAttribute("transform", "translate(" + vx + "," + vy + ") scale(" + vs + ")");
  }}

  function onHover(e) {{
    if (!pinned) setDetailsForNode(e.currentTarget.getAttribute("data-info") || "");
  }}
  function onOut() {{
    if (!pinned) clearDetails();
  }}
  function onNodeClick(e) {{
    var g = e.currentTarget;
    if (g.classList.contains("pinned")) {{
      unpinAll();
    }} else {{
      unpinAll();
      g.classList.add("pinned");
      pinned = true;
      setDetailsForNode(g.getAttribute("data-info") || "");
    }}
    e.stopPropagation();
  }}
  function unpinAll() {{
    var prev = document.querySelectorAll(".diagram-node.pinned");
    for (var i = 0; i < prev.length; i++) prev[i].classList.remove("pinned");
    pinned = false;
    clearDetails();
  }}

  function searchPrompt() {{
    if (searchActive) {{
      var all = document.querySelectorAll(".diagram-node");
      for (var i = 0; i < all.length; i++) all[i].classList.remove("dim");
      searchBtn.textContent = "Search";
      searchActive = false;
      clearDetails();
      return;
    }}
    var term = prompt("Search (regex):");
    if (term == null) return;
    var re;
    try {{ re = new RegExp(term, "i"); }} catch (err) {{ alert("Invalid regex"); return; }}
    var all = document.querySelectorAll(".diagram-node");
    for (var i = 0; i < all.length; i++) {{
      var info = all[i].getAttribute("data-info") || "";
      all[i].classList.toggle("dim", !re.test(info));
    }}
    searchBtn.textContent = "Reset Search";
    searchActive = true;
    setDetailsText(["Matches: " + term + "  \u00b7  Click \u2018Reset Search\u2019 to clear"]);
  }}

  window.init = init;
}})();
]]></script>""")

    if analysis is not None:
        panel_y = diagram_height + info_gap
        panel_lines, _ = render_info_panel(analysis, pad, panel_y, total_width - 2 * pad, view_type="diagram")
        lines.extend(panel_lines)

    lines.append("</svg>\n")
    return "\n".join(lines)


def _empty_svg(width, title):
    h = 120
    return f'''<?xml version="1.0" standalone="no"?>
<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">
<svg version="1.1" width="{width}" height="{h}" xmlns="http://www.w3.org/2000/svg">
<text x="{width/2}" y="60" text-anchor="middle">{xml_escape(title)}</text>
<text x="{width/2}" y="85" text-anchor="middle" font-size="12" fill="#666">No plan steps to display</text>
</svg>
'''
