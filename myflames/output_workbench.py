"""Workbench-style Visual Explain, independently rendered from the canonical tree.

Design references: Workbench 26.7 util_plugin/visual/explain.py (JoinWidget,
BaseExplainWidget, node layout, estimated row arrows). No Workbench runtime or
source code is bundled. See docs/WORKBENCH_VIEW.md for mappings and differences.
"""
import math
import re
from pathlib import Path

from ._labels import fit_label
from .parser import xml_escape, render_info_panel

BOX_W = 256
BOX_H = 126
JOIN_H = 186
H_GAP = 76
V_GAP = 90
COLORS = {
    "table": "#db9292", "lookup": "#9ac59d", "range": "#e3ca83",
    "index": "#e4b181", "nested_loop": "#c998b3", "hash": "#9ecbcb",
    "bka": "#a9d8dd", "operation": "#d1cfad", "materialize": "#b4a4d8",
    "unknown": "#ccd2d8", "result": "#bec7ce",
}


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _fmt(value):
    value = _number(value)
    if value is None:
        return "n/a"
    if abs(value) >= 1000000:
        return "%.2fM" % (value / 1000000)
    if abs(value) >= 1000:
        return "%.2fK" % (value / 1000)
    return "%.3g" % value


def _time(value):
    value = _number(value)
    if value is None:
        return "n/a"
    if value >= 1000:
        return "%.2f s" % (value / 1000)
    if 0 < value < 1:
        return "%.1f \u00b5s" % (value * 1000)
    return "%.2f ms" % value


def _kind(node):
    """Use explicit metadata first; operation text covers normalized MariaDB."""
    d = node.get("details") or {}
    access = str(d.get("access_type") or "").lower()
    index = str(d.get("index_access_type") or "").lower()
    op = str(d.get("operation") or node.get("full_label") or "").lower()
    algorithm = str(d.get("join_algorithm") or "").lower()
    if access == "join" or algorithm or _join_type(node) != "unknown" or op.startswith(("nested loop", "inner hash join", "left hash join", "hash join", "hash semijoin", "hash antijoin", "block nested loop", "batched key")):
        if "hash" in algorithm or op.startswith(("inner hash", "left hash", "right hash", "full hash", "full outer hash", "hash ")):
            return "hash"
        if algorithm in ("batch_key_access", "bka") or "batched key" in op:
            return "bka"
        return "nested_loop"
    if access == "materialize" or op.startswith("materialize"):
        return "materialize"
    if index or access in ("index", "range", "ref", "eq_ref", "const", "index_merge") or op.startswith(("index ", "covering index", "single-row index")):
        if "range" in index or access == "range" or op.startswith(("index range", "covering index range")):
            return "range"
        if "lookup" in index or access in ("ref", "eq_ref", "const") or "lookup" in op:
            return "lookup"
        return "index"
    if access in ("table", "all") or op.startswith("table scan"):
        return "table"
    if access in ("filter", "sort", "aggregate", "group", "limit", "window", "union", "append", "stream") or node.get("children"):
        return "operation"
    return "unknown"


JOIN_LABELS = {
    "inner": ("Inner join", "Matching row combinations"),
    "left": ("Left join", "All left rows, with matching right rows; unmatched right values are NULL"),
    "right": ("Right join", "All right rows, with matching left rows; unmatched left values are NULL"),
    "full": ("Full outer join", "All rows from both sides, including unmatched rows"),
    "semi": ("Semijoin", "Left rows that have a right-side match"),
    "anti": ("Antijoin", "Left rows without a right-side match"),
    "cross": ("Cross join", "Every left and right row combination"),
    "unknown": ("Join", "Join type not reported"),
}


def _join_type(node):
    """Use reported semantics, never a keyword inside a condition/table name.

    MariaDB's synthetic joins explicitly carry 'unknown'; their historical
    operation labels are not evidence of inner-join semantics.
    """
    details = node.get("details") or {}
    explicit = str(details.get("join_type") or "").strip().lower()
    aliases = {"inner join": "inner", "left join": "left", "left outer join": "left",
               "right join": "right", "right outer join": "right", "full outer join": "full",
               "full join": "full", "semijoin": "semi", "semi join": "semi",
               "antijoin": "anti", "anti join": "anti", "cross join": "cross"}
    if explicit:
        return explicit if explicit in JOIN_LABELS else aliases.get(explicit, "unknown")
    operation = str(details.get("operation") or node.get("full_label") or "").strip().lower()
    # MySQL prefixes the logical type with the execution algorithm. Hash uses
    # 'Left hash join' but 'Hash antijoin', so normalize only leading words.
    operation = re.sub(r"^(?:nested loop|batched key access|block nested loop)\s+", "", operation)
    operation = re.sub(r"^(inner|left|right|full outer|full|cross)\s+hash\s+", r"\1 ", operation)
    operation = re.sub(r"^hash\s+", "", operation)
    for label in sorted(aliases, key=len, reverse=True):
        if re.match(re.escape(label) + r"(?:\b|$)", operation):
            return aliases[label]
    return "unknown"


def _node_height(node):
    return JOIN_H if _kind(node) in ("nested_loop", "hash", "bka") else BOX_H


def _join_symbol(join_type):
    """Large Venn mark, using geometric paths rather than shared SVG IDs."""
    # Intersections of radius-44 circles at (96,60) and (160,60).
    top = 60 - math.sqrt(44 * 44 - 32 * 32)
    bottom = 120 - top
    lens = 'M128 %.3f A44 44 0 0 1 128 %.3f A44 44 0 0 1 128 %.3f Z' % (top, bottom, top)
    parts = ['<g class="wb-venn" role="img" aria-label="%s: %s" data-join-type="%s">' %
             (JOIN_LABELS[join_type][0], JOIN_LABELS[join_type][1], join_type)]
    for cx in (96, 160):
        parts.append('<circle cx="%d" cy="60" r="44" fill="#f5f5f7"/>' % cx)
    fill = '#b7d9ff'
    if join_type in ("left", "right", "full", "anti"):
        centers = (96, 160) if join_type == "full" else (160,) if join_type == "right" else (96,)
        for cx in centers:
            parts.append('<circle class="wb-join-highlight" data-region="%s" cx="%d" cy="60" r="44" fill="%s"/>' %
                         ('right' if cx == 160 else 'left', cx, fill))
        if join_type == "anti":
            parts.append('<path data-region="excluded-overlap" d="%s" fill="#f5f5f7"/>' % lens)
    elif join_type in ("inner", "semi"):
        parts.append('<path class="wb-join-highlight" data-region="overlap" d="%s" fill="%s"/>' % (lens, fill))
    for cx in (96, 160):
        parts.append('<circle cx="%d" cy="60" r="44" fill="none" stroke="#6e7d8c" stroke-width="1.5"/>' % cx)
    parts.append('<text x="78" y="64" text-anchor="middle" class="wb-side">L</text><text x="178" y="64" text-anchor="middle" class="wb-side">R</text>')
    if join_type == "cross":
        parts.append('<text x="128" y="64" text-anchor="middle">×</text>')
    parts.append('</g>')
    return ''.join(parts)


def _phase(parent, child, index):
    """Only infer phase labels where the tree supplies evidence."""
    if (child.get("details") or {}).get("input_kind") == "select_list":
        return "select-list subquery"
    kind = _kind(parent)
    if index >= 2:
        return "input %d" % (index + 1)
    if kind == "hash":
        children = parent.get("children") or []
        builds = [i for i, c in enumerate(children[:2])
                  if str((c.get("details") or {}).get("operation") or c.get("full_label") or "").strip().lower() == "hash"]
        if len(builds) == 1:
            return "build" if index == builds[0] else "probe"
        return "input %d" % (index + 1)
    if kind == "nested_loop":
        return "outer" if index == 0 else "inner"
    # BKA and unrecognized operators retain input order without inventing roles.
    return "input %d" % (index + 1) if len(parent.get("children") or []) > 1 else ""


def _arrow_width(node):
    estimate = _number((node.get("details") or {}).get("estimated_rows"))
    return min(16, 1 + int(math.log(estimate, 2))) if estimate is not None and estimate >= 1 else 1


def _layout(root):
    """Lay out whole child subtrees in order, align parent to its last input.

    Every parsed node is retained exactly once, including unary operations and
    every branch of unions/materialization. Coordinates are canvas-independent.
    """
    def measure(node):
        children = [measure(child) for child in node.get("children") or []]
        width = sum(child["width"] for child in children) + H_GAP * max(0, len(children) - 1)
        depth = max((child["height"] for child in children), default=0)
        last_x = sum(child["width"] + H_GAP for child in children[:-1])
        return {"node": node, "children": children, "width": max(BOX_W, width),
                "height": _node_height(node) + (depth + V_GAP if children else 0),
                "cx": last_x + children[-1]["cx"] if children else BOX_W / 2,
                "cy": depth + V_GAP if children else 0}

    layout = measure(root)
    nodes, edges = [], []

    def place(item, x, y):
        node = item["node"]
        here = {"node": node, "x": x + item["cx"] - BOX_W / 2, "y": y + item["cy"], "height": _node_height(node)}
        nodes.append(here)
        offset = 0
        for index, child in enumerate(item["children"]):
            child_y = y + item["cy"] - V_GAP - child["height"]
            there = place(child, x + offset, child_y)
            edges.append({"from": there, "to": here, "phase": _phase(node, child["node"], index)})
            offset += child["width"] + H_GAP
        return here

    place(layout, 0, 0)
    return nodes, edges, layout["width"], layout["height"]


def _details(node):
    d = node.get("details") or {}
    measured = _number(d.get("actual_last_row_ms")) is not None
    fields = [
        ("Operation", node.get("full_label") or node.get("short_label") or "Unknown"),
        ("Actual rows / loop", d.get("actual_rows")),
        ("Estimated rows / loop", d.get("estimated_rows")),
        ("Actual loops", d.get("actual_loops")),
        ("Actual first row (ms / loop)", d.get("actual_first_row_ms")),
        ("Actual last row (ms / loop)", d.get("actual_last_row_ms")),
        ("Total across loops (ms)", node.get("total_time") if measured else None),
        ("Self time across loops (ms)", node.get("self_time") if measured else None),
        ("Estimated optimizer cost", d.get("estimated_total_cost")),
        ("Table", d.get("table_name")), ("Index", d.get("index_name")),
        ("Access type", d.get("access_type")), ("Index access", d.get("index_access_type")),
        ("Condition", d.get("condition")), ("Ranges", d.get("ranges")),
        ("Join algorithm", d.get("join_algorithm")), ("Covering index", d.get("covering")),
    ]
    if _kind(node) in ("nested_loop", "hash", "bka"):
        label, meaning = JOIN_LABELS[_join_type(node)]
        fields.extend([("Join type", label), ("Highlighted region", meaning),
                       ("Join symbol", "Logical join semantics, not row counts or duplicates. L/R refer to this plan operator, not SQL text order.")])
    return "\n".join("%s: %s" % (label, value) for label, value in fields if value is not None and value != "" and value != [])


def render_workbench(root, width=1200, title="MySQL Query Plan", unit_display="ms",
                     analysis=None, teach_index_by_folded=None):
    """Produce portable interactive SVG using Workbench 26.7 visual conventions."""
    width = max(720, int(width or 1200))
    nodes, edges, graph_w, graph_h = _layout(root)
    # Add a synthetic output terminal; it is not another measured operator.
    graph_h += 90
    viewport_y, viewport_h = 136, 660
    details_y = viewport_y + viewport_h + 8
    height = details_y + 180
    info, info_h = render_info_panel(analysis, 20, height + 12, width - 40, view_type="workbench") if analysis is not None else ([], 0)
    if info:
        height += info_h + 24
    attr = lambda value: xml_escape(str(value)).replace('"', '&quot;').replace("'", '&#39;').replace('\n', '&#10;')
    text = lambda value: xml_escape(str(value))
    lines = [
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xhtml="http://www.w3.org/1999/xhtml" '
        'width="%d" height="%d" viewBox="0 0 %d %d" class="wb-plan" role="group" aria-label="Visual Explain">' % (width, height, width, height),
        '<title>%s — Visual Explain</title>' % text(title),
        '<style>.wb-plan{font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;background:#f8fafb;color:#243341}.wb-plan text{fill:#243341;font-size:12px}.wb-plan .wb-heading{font-size:19px;font-weight:650}.wb-plan .wb-caption{fill:#596a79;font-size:11px}.wb-plan .wb-title{font-size:13px;font-weight:650}.wb-plan .wb-node{cursor:pointer;outline:none}.wb-plan .wb-node:hover .wb-box,.wb-plan .wb-node:focus .wb-box,.wb-plan .wb-node.selected .wb-box{stroke:#176aa8;stroke-width:3}.wb-plan .wb-node.dim{opacity:.25}.wb-plan .wb-controls button,.wb-plan .wb-controls input{font:12px system-ui;margin-right:6px;padding:6px 10px;border:1px solid #bbc8d3;border-radius:5px;background:white;color:#243341}.wb-plan .wb-controls button{cursor:pointer;min-width:32px}.wb-plan .wb-controls button:hover:not(:disabled){background:#e8f1fa;border-color:#176aa8}.wb-plan .wb-controls button:active:not(:disabled),.wb-plan .wb-controls button.wb-activated:not(:disabled){background:#176aa8;border-color:#176aa8;color:#fff}.wb-plan .wb-controls button:disabled{opacity:.4;cursor:default}.wb-plan .wb-controls [data-action="reset"]{min-width:64px;font-variant-numeric:tabular-nums}.wb-plan button:focus-visible,.wb-plan input:focus-visible{outline:2px solid #176aa8}.wb-plan .wb-details{user-select:text;font:12px/1.6 system-ui;color:#243341;white-space:pre-wrap;overflow:auto;height:100%;box-sizing:border-box;padding:12px;border:1px solid #d8e0e5;border-radius:6px;background:#fff}.wb-plan .wb-edge-label{font-size:11px;paint-order:stroke;stroke:#f8fafb;stroke-width:4px;stroke-linejoin:round}</style>',
        '<rect width="100%" height="100%" fill="#f8fafb"/>',
        '<text x="20" y="29" class="wb-heading">%s</text>' % text(title or "Visual Explain"),
        '<text x="20" y="50" class="wb-caption">Data flows downward · Colors identify operators · Arrow width = estimated rows (log scale)</text>',
        '<foreignObject x="20" y="61" width="%d" height="42"><div xmlns="http://www.w3.org/1999/xhtml" class="wb-controls"><button type="button" data-action="fit">Fit plan</button><button type="button" data-action="in" aria-label="Zoom in">+</button><button type="button" data-action="out" aria-label="Zoom out">−</button><button type="button" data-action="reset" aria-label="Reset zoom to 100%%" title="Reset zoom to 100%%">100%%</button><input type="search" aria-label="Search plan nodes" placeholder="Search plan nodes"/><span class="wb-search-status" aria-live="polite"></span></div></foreignObject>' % (width - 40),
    ]
    for i, (label, kind) in enumerate([("Table scan", "table"), ("Index lookup", "lookup"), ("Index range", "range"), ("Index scan", "index"), ("Join", "nested_loop"), ("Hash", "hash"), ("Operation", "operation"), ("Materialize", "materialize")]):
        x = 20 + i * ((width - 40) / 8)
        lines.append('<rect x="%.1f" y="112" width="10" height="10" fill="%s"/><text x="%.1f" y="121" class="wb-caption">%s</text>' % (x, COLORS[kind], x + 15, label))
    lines.extend([
        '<svg x="0" y="%d" width="%d" height="%d" viewBox="0 0 %d %d" class="wb-viewport" style="touch-action:none;cursor:grab;overflow:hidden" data-graph-width="%s" data-graph-height="%s">' % (viewport_y, width, viewport_h, width, viewport_h, graph_w, graph_h),
        '<defs><marker id="wb-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto" markerUnits="userSpaceOnUse"><path d="M0 0 L8 4 L0 8 Z" fill="#8795a1"/></marker></defs>',
        '<rect x="-100000" y="-100000" width="200000" height="200000" fill="#f8fafb"/>',
        '<g class="wb-graph">',
    ])
    for edge in edges:
        source, target = edge["from"], edge["to"]
        sx, sy = source["x"] + BOX_W / 2, source["y"] + source["height"]
        tx, ty = target["x"] + BOX_W / 2, target["y"]
        bend = sy + V_GAP * .63
        d = "M%.1f %.1f V%.1f H%.1f V%.1f" % (sx, sy, bend, tx, ty - 5)
        child = source["node"]
        details = child.get("details") or {}
        lines.append('<path class="wb-edge" data-from="%s" data-to="%s" d="%s" fill="none" stroke="#8795a1" stroke-width="%s" marker-end="url(#wb-arrow)"/>' % (attr(child.get("node_id", "")), attr(target["node"].get("node_id", "")), d, _arrow_width(child)))
        rows = "Rows/loop: %s actual · %s est." % (_fmt(details.get("actual_rows")), _fmt(details.get("estimated_rows")))
        lines.append('<text x="%.1f" y="%.1f" class="wb-edge-label">%s</text>' % (source["x"] + 8, sy + 23, text(rows)))
        if edge["phase"]:
            lines.append('<text x="%.1f" y="%.1f" class="wb-edge-label">%s</text>' % (source["x"] + 8, sy + 41, text(edge["phase"])))
    for item in nodes:
        node, x, y = item["node"], item["x"], item["y"]
        d = node.get("details") or {}
        kind = _kind(node)
        is_join = kind in ("nested_loop", "hash", "bka")
        label = node.get("short_label") or "Unknown operator"
        detail = _details(node)
        teach = (teach_index_by_folded or {}).get((node.get("folded_label") or "").strip())
        teach_attr = ' data-teach-index="%s"' % teach if teach is not None else ''
        lines.append('<g class="wb-node" role="button" tabindex="0" aria-label="%s" data-node-id="%s" data-details="%s" data-label="%s" data-kind="%s" transform="translate(%.1f %.1f)"%s><title>%s</title>' % (attr(label), attr(node.get("node_id", "")), attr(detail), attr(label), kind, x, y, teach_attr, text(detail)))
        lines.append('<rect class="wb-box" width="%d" height="%d" rx="7" fill="white" stroke="#c4cfd6"/>' % (BOX_W, item["height"]))
        if is_join:
            join_type = _join_type(node)
            lines.append('<rect x="8" width="240" height="3" rx="1.5" fill="%s"/>' % COLORS[kind])
            lines.append(_join_symbol(join_type))
            algorithm = {"hash": "Hash", "nested_loop": "Nested loop", "bka": "BKA"}[kind]
            heading = JOIN_LABELS[join_type][0] + " · " + algorithm
            lines.append('<text x="128" y="124" text-anchor="middle" class="wb-title">%s</text>' % text(fit_label(heading, BOX_W - 24, 13)))
        else:
            lines.append('<path d="M7 0 H249 Q256 0 256 7 V34 H0 V7 Q0 0 7 0" fill="%s"/>' % COLORS[kind])
            lines.append('<text x="12" y="23" class="wb-title">%s</text>' % text(fit_label(label, BOX_W - 24, 13)))
            secondary = d.get("table_name") or d.get("condition") or d.get("index_name") or d.get("operation") or ""
            if d.get("table_name") and d.get("index_name"):
                secondary += " · " + d["index_name"]
            lines.append('<text x="12" y="57">%s</text>' % text(fit_label(str(secondary), BOX_W - 24, 12)))
        measured = _number(d.get("actual_last_row_ms")) is not None
        timing = "Self %s · total %s" % (_time(node.get("self_time") if measured else None), _time(node.get("total_time") if measured else None))
        lines.append('<text x="12" y="%d" class="wb-caption">%s</text>' % (item["height"] - 35, text(timing)))
        lines.append('<text x="12" y="%d" class="wb-caption">Est. cost %s · loops %s</text></g>' % (item["height"] - 14, text(_fmt(d.get("estimated_total_cost"))), text(_fmt(d.get("actual_loops")))))
    terminal_x = nodes[0]["x"] + BOX_W / 2
    terminal_y = nodes[0]["y"] + nodes[0]["height"]
    lines.extend([
        '<path d="M%.1f %.1f v40" stroke="#8795a1" stroke-width="2" marker-end="url(#wb-arrow)"/>' % (terminal_x, terminal_y),
        '<rect x="%.1f" y="%.1f" width="160" height="32" rx="5" fill="#dbe3e8"/><text x="%.1f" y="%.1f" text-anchor="middle">Plan output</text>' % (terminal_x - 80, terminal_y + 46, terminal_x, terminal_y + 67),
        '</g></svg>',
        '<foreignObject x="20" y="%d" width="%d" height="150"><div xmlns="http://www.w3.org/1999/xhtml" class="wb-details" tabindex="0" role="status">Select a node to inspect its details. Drag to pan; scroll gently to zoom. Timings are across loops; optimizer cost is a separate estimate.</div></foreignObject>' % (details_y, width - 40),
        '<text x="20" y="%d" class="wb-caption">Visual conventions: MySQL Workbench 26.7 · Data and analysis: myflames</text>' % (details_y + 168),
    ])
    lines.extend(info)
    script = Path(__file__).with_name("workbench.js").read_text(encoding="utf-8")
    lines.extend(['<script type="text/ecmascript"><![CDATA[', script, ']]></script>', '</svg>'])
    return "\n".join(lines)
