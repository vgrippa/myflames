"""Generate diagram via Graphviz (DOT) for automatic layout. Optional backend when --diagram-engine graphviz."""

import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET

from .parser import build_diagram_steps, xml_escape

# Reuse helpers from the built-in diagram for labels and tooltips
from .output_diagram import (
    _access_diagram_label,
    _format_loops,
    _format_rows,
    _format_time,
    _node_tooltip,
    _time_to_fill,
)


def _dot_escape(s):
    """Escape string for use in a DOT label (backslash and double-quote)."""
    if s is None:
        return ""
    s = str(s).replace("\\", "\\\\").replace('"', '\\"')
    return s.replace("\n", "\\n")


def _build_diagram_data(root):
    """Build main_line and inner_list from root (same logic as output_diagram.render_diagram)."""
    steps = build_diagram_steps(root)
    if not steps:
        return None, None, None, None, None

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

    main_line = [items[0]]
    inner_list = []
    for j in range(1, len(items), 2):
        main_line.append(items[j])
        if j + 1 < len(items):
            inner_list.append(items[j + 1])

    total_time_ms = float(root.get("total_time") or 0)
    root_self_ms = float(root.get("self_time") or 0)
    max_self_ms = root_self_ms
    for it in items:
        s = it[3]
        if s is not None and s > 0:
            max_self_ms = max(max_self_ms or 0, s)
    if not max_self_ms or max_self_ms <= 0:
        max_self_ms = 1.0

    return main_line, inner_list, total_time_ms, root_self_ms, max_self_ms


def _access_node_label(node, total_ms, self_ms, cost_val, rows):
    """Short multi-line label for an access node in DOT."""
    label, sublabel, _ = _access_diagram_label(node)
    details = node.get("details") or {}
    label_short = (label[:24] + "…") if len(label) > 24 else label
    lines = [
        _format_time(self_ms) if self_ms is not None else "—",
        label_short,
    ]
    for line in sublabel.split("\n")[:2]:
        line_short = (line[:22] + "…") if len(line) > 24 else line
        lines.append(line_short)
    rows_str = _format_rows(rows)
    lines.append(f"{rows_str} rows")
    loops = node.get("loops") or details.get("actual_loops")
    if loops and loops > 1:
        lines.append(f"↻ {_format_loops(loops) or loops} loops")
    if details.get("covering") is True:
        lines.append("Index only")
    return "\\n".join(_dot_escape(l) for l in lines)


def _join_node_label(node, self_ms, rows):
    """Short label for a join diamond in DOT."""
    rows_str = _format_rows(rows)
    lines = ["nested loop", f"{rows_str} rows out"]
    if self_ms is not None:
        lines.append(_format_time(self_ms))
    loops = node.get("loops") or (node.get("details") or {}).get("actual_loops")
    if loops and loops > 1:
        lines.append(f"↻ {_format_loops(loops) or loops} loops")
    return "\\n".join(_dot_escape(l) for l in lines)


def _build_dot(root, main_line, inner_list, total_time_ms, root_self_ms, max_self_ms):
    """Build DOT source string. Returns (dot_string, node_info dict, node_order list).
    node_order is the list of our node ids in the order they appear in DOT (for Graphviz node1, node2, ...)."""
    total_time_str = _format_time(total_time_ms)
    lines = [
        "digraph G {",
        "  rankdir=LR;",
        "  node [shape=box, style=filled, fontname=Arial, fontsize=11, margin=0.2];",
        "  edge [fontname=Arial, fontsize=9];",
        "",
    ]

    node_info = {}
    node_order = []

    # First access (main line)
    kind, node, total_ms, self_ms, cost_val, rows = main_line[0]
    nid = "access_0"
    node_order.append(nid)
    label = _access_node_label(node, total_ms, self_ms, cost_val, rows)
    fill = _time_to_fill(self_ms, max_self_ms)
    total_str = _format_time(total_ms)
    rows_str = _format_rows(rows)
    node_info[nid] = _node_tooltip(node, kind, total_str, cost_val, rows_str, total_ms, self_ms)
    lines.append(f'  {nid} [label="{label}", fillcolor="{fill}"];')
    lines.append("")

    # Joins and inner accesses
    num_joins = len(inner_list)
    for k in range(num_joins):
        jkind, jnode, jtotal_ms, jself_ms, jcost_val, jrows = main_line[k + 1]
        join_id = f"join_{k + 1}"
        node_order.append(join_id)
        jlabel = _join_node_label(jnode, jself_ms, jrows)
        jfill = _time_to_fill(jself_ms, max_self_ms)
        jrows_str = _format_rows(jrows)
        jtotal_str = _format_time(jtotal_ms)
        node_info[join_id] = _node_tooltip(jnode, "join", jtotal_str, jcost_val, jrows_str, jtotal_ms, jself_ms)
        lines.append(f'  {join_id} [label="{jlabel}", shape=diamond, fillcolor="{jfill}"];')

        if k < len(inner_list):
            ikind, inode, itotal_ms, iself_ms, icost_val, irows = inner_list[k]
            inner_id = f"inner_{k + 1}"
            node_order.append(inner_id)
            ilabel = _access_node_label(inode, itotal_ms, iself_ms, icost_val, irows)
            ifill = _time_to_fill(iself_ms, max_self_ms)
            irows_str = _format_rows(irows)
            itotal_str = _format_time(itotal_ms)
            node_info[inner_id] = _node_tooltip(inode, ikind, itotal_str, icost_val, irows_str, itotal_ms, iself_ms)
            lines.append(f'  {inner_id} [label="{ilabel}", fillcolor="{ifill}"];')
            lines.append(f"  {inner_id} -> {join_id};")
        lines.append("")

    # Main chain edges: access_0 -> join_1 -> join_2 -> ... -> query_block
    chain = ["access_0"]
    for k in range(num_joins):
        chain.append(f"join_{k + 1}")
    chain.append("query_block")
    lines.append("  " + " -> ".join(chain) + ";")
    lines.append("")

    # Query block node
    node_order.append("query_block")
    qfill = _time_to_fill(root_self_ms, max_self_ms)
    root_self_str = _format_time(root_self_ms) if root_self_ms else "—"
    qlabel = _dot_escape("query_block #1") + "\\n" + _dot_escape(root_self_str)
    node_info["query_block"] = f"Total: {total_time_str}"
    lines.append(f'  query_block [label="{qlabel}", fillcolor="{qfill}"];')
    lines.append("")

    # Ranks: main line on top, inner row below
    lines.append("  { rank=same; " + "; ".join(chain) + "; }")
    if inner_list:
        inner_ids = [f"inner_{k + 1}" for k in range(len(inner_list))]
        lines.append("  { rank=same; " + "; ".join(inner_ids) + "; }")
    lines.append("}")

    return "\n".join(lines), node_info, node_order


def _run_graphviz(dot_source):
    """Run dot -Tsvg and return SVG string. Returns None on failure."""
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".dot", delete=False) as f:
            f.write(dot_source)
            dot_path = f.name
        result = subprocess.run(
            ["dot", "-Tsvg", dot_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        try:
            os.unlink(dot_path)
        except OSError:
            pass
        if result.returncode != 0:
            return None
        return result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _inject_ui(svg_string, title, total_time_str, node_info, node_order, width, pad=20, bottom_pad=72):
    """Inject title, subtitle, details bar, legend, and hover script into Graphviz SVG.
    node_order: list of our node ids in the same order as Graphviz node1, node2, ..."""
    root = ET.fromstring(svg_string)
    ns = {"svg": "http://www.w3.org/2000/svg"}
    # Strip default namespace for simpler find
    for elem in root.iter():
        if "}" in elem.tag:
            elem.tag = elem.tag.split("}")[1]
    # Get width/height from root
    w = root.get("width", str(width))
    h = root.get("height", "400")
    try:
        h_num = float(re.sub(r"[^0-9.]", "", h))
    except ValueError:
        h_num = 400
    # Add padding for title, details, legend
    title_h = 56
    extra_h = title_h + bottom_pad
    new_h = h_num + extra_h
    root.set("height", str(int(new_h)))
    try:
        w_num = float(re.sub(r"[^0-9.]", "", w))
    except ValueError:
        w_num = width
    root.set("width", str(max(int(w_num), width)))
    # Use a viewBox that matches our coordinate system so title, graph, and legend are all visible
    root.set("viewBox", f"0 0 {max(int(w_num), width)} {int(new_h)}")
    # So browsers render as SVG image, not generic XML tree
    root.set("xmlns", "http://www.w3.org/2000/svg")

    # Find the main graph group (Graphviz puts content in a <g> with id like "graph0")
    graph_g = None
    for g in root.findall(".//g"):
        if g.get("id", "").startswith("graph") or g.get("class") == "graph":
            graph_g = g
            break
    if graph_g is None:
        for g in root.findall(".//g"):
            if len(list(g)) > 0:
                graph_g = g
                break
    if graph_g is not None:
        # Graphviz uses negative y (top of graph ~ -814). Translate so graph is below the title.
        # Find min y from the graph's background polygon if present, else use a safe default.
        min_y = 0
        for poly in graph_g.findall(".//polygon"):
            pts = poly.get("points", "")
            for part in pts.replace(",", " ").split():
                try:
                    val = float(part)
                    if val < min_y:
                        min_y = val
                except ValueError:
                    pass
        # Translate so content that was at min_y (e.g. -814) moves to just below title
        graph_offset_y = title_h + (-min_y if min_y < 0 else 0)
        graph_g.set("transform", f"translate(0,{graph_offset_y})")

    # Add background rect, title, subtitle, details, legend, script
    def make_elem(tag, **attrs):
        e = ET.Element(tag)
        for k, v in attrs.items():
            if v is not None:
                e.set(k, str(v))
        return e

    def make_text(x, y, text, cls="cost", anchor="middle"):
        t = ET.Element("text")
        t.set("x", str(x))
        t.set("y", str(y))
        t.set("text-anchor", anchor)
        t.set("class", cls)
        t.text = text
        return t

    children = list(root)
    insert_idx = 0
    # Find where to insert (after first element, e.g. after defs or rect)
    for i, c in enumerate(children):
        if c.tag == "g" and (c.get("id") or "").startswith("graph"):
            insert_idx = i
            break

    # Prepend a group for our UI (title, details, legend)
    ui_g = ET.Element("g", id="diagram-ui")
    rect = make_elem("rect", width="100%", height="100%", fill="#fafafa")
    ui_g.append(rect)

    try:
        total_width = float(re.sub(r"[^0-9.]", "", root.get("width", str(width))))
    except ValueError:
        total_width = width
    cx = total_width / 2

    ui_g.append(make_text(cx, 28, title, "title"))
    ui_g.append(
        make_text(
            cx,
            46,
            f"Execution plan (left→right) · Total: {total_time_str} · Each stage shows self-time (darker = more)",
            "cost",
        )
    )
    details_el = make_text(pad, new_h - bottom_pad + 14, "Hover over a node for details", "cost", "start")
    details_el.set("id", "details")
    ui_g.append(details_el)
    # Legend
    ui_g.append(make_text(pad, new_h - bottom_pad - 24, "How to read", "legend-title", "start"))
    ui_g.append(
        make_text(
            pad,
            new_h - bottom_pad - 12,
            "Darker = more self-time (time in this step only).",
            "legend-item",
            "start",
        )
    )
    ui_g.append(
        make_text(
            pad + 320,
            new_h - bottom_pad - 12,
            "↻ Loops = times this step ran (e.g. inner side of join).",
            "legend-item",
            "start",
        )
    )
    ui_g.append(
        make_text(
            pad + 620,
            new_h - bottom_pad - 12,
            "Index only = covering index (no table read).",
            "legend-item",
            "start",
        )
    )

    # Add styles
    style = ET.Element("style")
    style.text = """
  text { font-family: Arial, sans-serif; font-size: 11px; }
  .title { font-size: 16px; font-weight: bold; }
  .cost { font-size: 10px; fill: #555; }
  .legend-title { font-size: 10px; font-weight: bold; fill: #333; }
  .legend-item { font-size: 9px; fill: #555; }
  #details { font-size: 11px; fill: #333; }
  .diagram-node { cursor: pointer; }
"""
    root.insert(0, style)

    # Insert UI at the beginning (after style)
    root.insert(1, ui_g)

    # Graphviz outputs node1, node2, ... in definition order. Map to our node_order and add data-info.
    for elem in root.iter():
        eid = elem.get("id") or ""
        if eid.startswith("node") and eid[4:].isdigit():
            idx = int(eid[4:])
            if 1 <= idx <= len(node_order):
                our_id = node_order[idx - 1]
                if our_id in node_info:
                    cls = (elem.get("class") or "").strip()
                    if "diagram-node" not in cls:
                        elem.set("class", (cls + " diagram-node").strip())
                    info = node_info[our_id]
                    info_attr = info.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
                    elem.set("data-info", info_attr)

    # Add onload and script for hover (find closing </svg> and insert before it)
    root.set("onload", "init(evt)")

    out = ET.tostring(root, encoding="unicode", default_namespace="", method="xml")
    if not out.strip().startswith("<?xml"):
        out = '<?xml version="1.0" encoding="UTF-8"?>\n' + out
    # Inject script as raw so && and < are not XML-escaped
    script_raw = """<script type="text/javascript"><![CDATA[
function init(evt) {
  var detailsEl = document.getElementById("details");
  if (!detailsEl) return;
  var nodes = document.querySelectorAll(".diagram-node");
  function showInfo(e) {
    var g = e.target.closest ? e.target.closest(".diagram-node") : (e.target.parentNode && e.target.parentNode.classList && e.target.parentNode.classList.contains("diagram-node") ? e.target.parentNode : null);
    if (g && g.getAttribute("data-info")) detailsEl.textContent = g.getAttribute("data-info");
  }
  function clearInfo() { detailsEl.textContent = "Hover over a node for details"; }
  for (var i = 0; i < nodes.length; i++) {
    nodes[i].addEventListener("mouseover", showInfo);
    nodes[i].addEventListener("mouseout", clearInfo);
  }
}
]]></script>"""
    out = out.replace("</svg>", script_raw + "\n</svg>")
    return out


def render_diagram_graphviz(root, width=1200, title="MySQL Query Plan", unit_display="ms"):
    """Render diagram using Graphviz. Returns SVG string or None if Graphviz is not available."""
    data = _build_diagram_data(root)
    if data[0] is None:
        from .output_diagram import _empty_svg
        return _empty_svg(width, title)

    main_line, inner_list, total_time_ms, root_self_ms, max_self_ms = data
    total_time_str = _format_time(total_time_ms)

    dot_source, node_info, node_order = _build_dot(root, main_line, inner_list, total_time_ms, root_self_ms, max_self_ms)
    svg_string = _run_graphviz(dot_source)
    if svg_string is None:
        return None
    return _inject_ui(svg_string, title, total_time_str, node_info, node_order, width)
