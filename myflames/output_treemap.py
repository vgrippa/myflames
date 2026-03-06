"""Generate treemap SVG from parsed EXPLAIN tree."""
from .parser import xml_escape, flatten_nodes, render_info_panel


def _layout_treemap(node, x, y, w, h, depth, results):
    if w < 4 or h < 4:
        return
    results.append({"x": x, "y": y, "w": w, "h": h, "node": node, "depth": depth})
    children = node.get("children") or []
    if not children:
        return
    total = sum(c["total_time"] for c in children)
    if total <= 0:
        return
    ax, ay, aw, ah = x, y, w, h
    if w >= h:
        for i, c in enumerate(children):
            frac = c["total_time"] / total
            cw = (w - (ax - x)) if i == len(children) - 1 else int(w * frac + 0.5)
            _layout_treemap(c, ax, ay, cw, ah, depth + 1, results)
            ax += cw
    else:
        for i, c in enumerate(children):
            frac = c["total_time"] / total
            ch = (h - (ay - y)) if i == len(children) - 1 else int(h * frac + 0.5)
            _layout_treemap(c, ax, ay, aw, ch, depth + 1, results)
            ay += ch


def attr_escape(s):
    if s is None:
        return ""
    s = str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("\n", "&#10;").replace("\r", "")
    return s


def _cell_info_text(node, unit_display):
    """Rich details for treemap cell (operation, self/total time, rows, loops, table, index)."""
    parts = [node.get("short_label") or "—"]
    st = node["self_time"]
    tt = node["total_time"]
    st_str = f"{st:.0f}" if st >= 1 else f"{st:.3f}"
    tt_str = f"{tt:.0f}" if tt >= 1 else f"{tt:.3f}"
    parts.append(f"Self: {st_str} {unit_display} | Total: {tt_str} {unit_display}")
    parts.append(f"Rows: {node.get('rows') or 0:.0f} | Loops: {node.get('loops') or 1}")
    details = node.get("details") or {}
    if details.get("table_name"):
        parts.append("Table: " + details["table_name"])
    if details.get("index_name"):
        parts.append("Index: " + details["index_name"])
    return "  ·  ".join(parts)


def render_treemap(root, width=1200, title="MySQL Query Plan", unit_display="ms", analysis=None):
    """Generate treemap SVG. root is parsed tree."""
    top_margin = 70
    pad = 2
    treemap_width = width - 2 * pad
    treemap_height = 600
    details_lines_n = 5
    details_line_h = 14
    details_strip_h = 12 + details_lines_n * details_line_h  # ~82px

    _info_lines, info_panel_h = (
        render_info_panel(analysis, pad, 0, treemap_width, view_type="treemap")
        if analysis is not None
        else ([], 0)
    )
    info_gap = 8 if analysis is not None else 0
    chart_height = top_margin + treemap_height + details_strip_h + info_gap + info_panel_h
    details_y_start = top_margin + treemap_height + 20

    rects = []
    _layout_treemap(root, pad, top_margin, treemap_width, treemap_height, 0, rects)

    highlight_msg_by_label = {}
    if analysis is not None:
        for nh in analysis.get("node_highlights") or []:
            lbl = (nh.get("short_label") or "").strip()
            if lbl:
                highlight_msg_by_label[lbl] = nh.get("message") or ""

    tm_colors = [
        "rgb(255,99,71)", "rgb(255,160,122)", "rgb(255,218,185)",
        "rgb(176,224,230)", "rgb(135,206,250)", "rgb(173,216,230)",
        "rgb(144,238,144)", "rgb(152,251,152)", "rgb(255,250,205)",
    ]

    total_str = f"{root['total_time']:.0f}" if root["total_time"] >= 1 else f"{root['total_time']:.3f}"

    lines = [
        '<?xml version="1.0" standalone="no"?>',
        '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">',
        f'<svg version="1.1" width="{width}" height="{chart_height}" onload="init(evt)" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">',
        "<style>",
        "  text { font-family: Arial, sans-serif; font-size: 11px; }",
        "  .title { font-size: 18px; font-weight: bold; }",
        "  .subtitle { font-size: 11px; fill: #666; }",
        "  .treemap-cell { stroke: #fff; stroke-width: 1; cursor: pointer; }",
        "  .treemap-cell:hover { opacity: 0.9; stroke: #333; stroke-width: 1.5; }",
        "  .treemap-cell.in-query-analysis:hover { stroke: #c62828; stroke-width: 2.5; }",
        "  .treemap-cell.highlight { stroke: rgb(230,0,230); stroke-width: 2; }",
        "  .treemap-cell.zoomed { stroke: #111; stroke-width: 2; }",
        "  #unzoom, #search { font-size: 12px; fill: #666; cursor: pointer; }",
        "  #unzoom:hover, #search:hover { fill: #111; }",
        "  #unzoom.hide { display: none; }",
        "  .details-line { font-size: 12px; fill: #222; user-select: text; -webkit-user-select: text; }",
        "  .details-line.dim { fill: #999; }",
        "</style>",
        '<rect width="100%" height="100%" fill="#fafafa"/>',
        f'<text x="{width/2}" y="26" text-anchor="middle" class="title">{xml_escape(title)}</text>',
        f'<text x="{width/2}" y="48" text-anchor="middle" class="subtitle">Treemap by total time (hierarchy)  \u00b7  Total: {total_str} {unit_display}</text>',
        f'<text id="unzoom" class="hide" x="{pad}" y="64" text-anchor="start">Reset Zoom</text>',
        f'<text id="breadcrumb" x="{width/2}" y="64" text-anchor="middle" class="subtitle"></text>',
        f'<text id="search" x="{width - pad - 60}" y="64" text-anchor="end">Search</text>',
        '<g id="zoomable">',
    ]

    for cell_id, r in enumerate(rects):
        n = r["node"]
        x, y, w, h = r["x"], r["y"], r["w"], r["h"]
        d = r["depth"]
        if w < 8 or h < 8:
            continue
        color = tm_colors[d % len(tm_colors)]
        short_label = n["short_label"]
        label = (short_label[:28] + "...") if len(short_label) > 28 else short_label
        info_text = _cell_info_text(n, unit_display)
        short_lbl = (short_label or "").strip()
        analysis_msg = highlight_msg_by_label.get(short_lbl, "")
        if analysis_msg:
            info_text = info_text + "  ·  ⚠ In Query Analysis: " + analysis_msg
        info_attr = attr_escape(info_text)[:500]
        label_attr = attr_escape(short_label)
        analysis_attr = attr_escape(analysis_msg)[:400] if analysis_msg else ""
        cell_class = "treemap-cell in-query-analysis" if analysis_attr else "treemap-cell"
        title_text = info_text.replace("  ·  ", "\n")
        lines.append(
            f'<rect id="cell-{cell_id}" class="{cell_class}" x="{x}" y="{y}" width="{w}" height="{h}" fill="{color}" '
            f'data-x="{x}" data-y="{y}" data-w="{w}" data-h="{h}" data-label="{label_attr}" data-info="{info_attr}"'
            + (f' data-analysis-msg="{analysis_attr}"' if analysis_attr else "")
            + f'>'
            f"<title>{xml_escape(title_text)}</title></rect>"
        )
        if w > 40 and h > 14:
            tx, ty = x + 4, y + h - 4
            lines.append(f'<text x="{tx}" y="{ty}" fill="#333" font-size="10" pointer-events="none">{xml_escape(label)}</text>')

    lines.append("</g>")

    # Separator and pre-allocated multi-line details strip
    sep_y = top_margin + treemap_height + 6
    lines.append(f'<line x1="{pad}" y1="{sep_y}" x2="{width - pad}" y2="{sep_y}" stroke="#e0e0e0" stroke-width="1"/>')
    lines.append(f'<rect x="{pad}" y="{sep_y + 4}" width="{treemap_width}" height="{details_strip_h - 8}" fill="#f5f5f5" rx="4"/>')
    for di in range(details_lines_n):
        dy = details_y_start + di * details_line_h
        default_text = "Hover a cell for details; click a cell to zoom and pin details  \u00b7  Ctrl+F to search" if di == 0 else ""
        lines.append(
            f'<text id="details-l{di}" x="{pad + 10}" y="{dy}" '
            f'text-anchor="start" class="details-line">{xml_escape(default_text)}</text>'
        )
    lines.append("<!-- end details area -->")

    if analysis is not None:
        panel_y = top_margin + treemap_height + details_strip_h + info_gap
        panel_lines, _ = render_info_panel(analysis, pad, panel_y, treemap_width, view_type="treemap")
        lines.extend(panel_lines)

    lines.append(f"""<script type="text/ecmascript"><![CDATA[
(function() {{
  var pad = {pad}, topMargin = {top_margin}, tmWidth = {treemap_width}, tmHeight = {treemap_height}, svgWidth = {width}, svgHeight = {chart_height};
  var N_LINES = {details_lines_n};
  var zoomable, unzoomBtn, searchBtn, pinnedCell = null;
  var detailLines = [];
  var zoomState = {{ scale: 1, tx: pad, ty: topMargin }};
  var defaultDetailsHint = "Hover a cell for details; click a cell to zoom and pin details  \u00b7  Ctrl+F to search";

  function setDetailsText(parts) {{
    for (var i = 0; i < N_LINES; i++) {{
      if (!detailLines[i]) continue;
      detailLines[i].textContent = (i < parts.length) ? parts[i] : "";
    }}
  }}

  function clearDetails() {{
    if (detailLines[0]) detailLines[0].textContent = defaultDetailsHint;
    for (var i = 1; i < N_LINES; i++) {{
      if (detailLines[i]) detailLines[i].textContent = "";
    }}
  }}

  function setDetailsForCell(cell) {{
    var info = (cell.getAttribute("data-info") || "").replace(/&#10;/g, " | ");
    var sep = "  \u00b7  ";
    var parts = info.split(sep);
    setDetailsText(parts);
  }}

  function init(evt) {{
    zoomable = document.getElementById("zoomable");
    unzoomBtn = document.getElementById("unzoom");
    searchBtn = document.getElementById("search");
    for (var i = 0; i < N_LINES; i++) {{
      var el = document.getElementById("details-l" + i);
      if (el) detailLines.push(el);
    }}
    if (!zoomable) return;
    zoomState = {{ scale: 1, tx: 0, ty: 0 }};
    document.addEventListener("click", function(e) {{
      var t = e.target;
      if (t.id === "unzoom") {{ resetZoom(); return; }}
      if (t.id === "search") {{ searchPrompt(); return; }}
      if (t.classList && t.classList.contains("treemap-cell")) {{
        var x = parseFloat(t.getAttribute("data-x")), y = parseFloat(t.getAttribute("data-y")), w = parseFloat(t.getAttribute("data-w")), h = parseFloat(t.getAttribute("data-h"));
        if (zoomState.scale !== 1 && t.classList.contains("zoomed")) {{ resetZoom(); return; }}
        var label = t.getAttribute("data-label") || "";
        zoomTo(x, y, w, h, label);
        t.classList.add("zoomed");
        pinnedCell = t;
        setDetailsForCell(t);
      }}
    }});
    document.addEventListener("mouseover", function(e) {{
      if (e.target.classList && e.target.classList.contains("treemap-cell")) {{
        if (pinnedCell === null) setDetailsForCell(e.target);
        else if (e.target === pinnedCell) setDetailsForCell(e.target);
      }}
    }});
    document.addEventListener("mouseout", function(e) {{
      if (e.target.classList && e.target.classList.contains("treemap-cell") && !e.relatedTarget?.classList?.contains("treemap-cell"))
        if (pinnedCell === null) clearDetails();
    }});
    document.addEventListener("keydown", function(e) {{
      if (e.ctrlKey && e.key === "f") {{ e.preventDefault(); searchPrompt(); }}
    }});
  }}
  function zoomTo(x, y, w, h, label) {{
    var scale = Math.min(tmWidth / w, tmHeight / h);
    var tx = pad - x * scale;
    var ty = topMargin - y * scale;
    zoomable.setAttribute("transform", "translate(" + tx + "," + ty + ") scale(" + scale + ")");
    zoomState = {{ scale: scale, tx: tx, ty: ty }};
    unzoomBtn.classList.remove("hide");
    var bcEl = document.getElementById("breadcrumb");
    if (bcEl) bcEl.textContent = label ? "All \u203a " + label : "";
  }}
  function resetZoom() {{
    zoomable.setAttribute("transform", "");
    zoomState = {{ scale: 1, tx: 0, ty: 0 }};
    unzoomBtn.classList.add("hide");
    var cells = document.querySelectorAll(".treemap-cell.zoomed");
    for (var i = 0; i < cells.length; i++) cells[i].classList.remove("zoomed");
    pinnedCell = null;
    clearDetails();
    var bcEl = document.getElementById("breadcrumb");
    if (bcEl) bcEl.textContent = "";
  }}
  function searchPrompt() {{
    if (searchBtn.textContent === "Reset Search") {{
      var cells = document.querySelectorAll(".treemap-cell");
      for (var i = 0; i < cells.length; i++) cells[i].classList.remove("highlight");
      searchBtn.textContent = "Search";
      if (!pinnedCell) clearDetails();
      return;
    }}
    var term = prompt("Search (regex):");
    if (term == null) return;
    var re;
    try {{ re = new RegExp(term, "i"); }} catch (err) {{ alert("Invalid regex"); return; }}
    var cells = document.querySelectorAll(".treemap-cell");
    for (var i = 0; i < cells.length; i++) {{
      var label = cells[i].getAttribute("data-label") || "";
      cells[i].classList.toggle("highlight", re.test(label));
    }}
    searchBtn.textContent = "Reset Search";
    setDetailsText(["Matches: " + term + "  \u00b7  Click \u2018Reset Search\u2019 to clear"]);
  }}
  window.init = init;
}})();
]]></script>
</svg>
""")
    return "\n".join(lines)
