"""Generate bar chart SVG from parsed EXPLAIN tree."""
from .parser import xml_escape, flatten_nodes, render_info_panel


def format_number(n):
    s = str(int(n))
    if len(s) > 3:
        result = []
        for i, c in enumerate(reversed(s)):
            if i and i % 3 == 0:
                result.append(",")
            result.append(c)
        return "".join(reversed(result))
    return s


def _attr_escape(s):
    if s is None:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _build_bar_info(op, st, pct, unit_display):
    """Build rich info string shown in the details bar on hover."""
    full_label = op.get("full_label") or op["short_label"] or ""
    tt_val = op["total_time"]
    tt = f"{tt_val:.0f}" if tt_val >= 1 else f"{tt_val:.3f}"
    loops_t = format_number(op["loops"])
    details = op.get("details") or {}
    parts = [full_label]
    parts.append(f"Self: {st} {unit_display} ({pct:.1f}%)")
    if abs(tt_val - op["self_time"]) > 0.001:
        parts.append(f"Total: {tt} {unit_display}")
    parts.append(f"Rows: {op['rows']:.0f}")
    parts.append(f"Loops: {loops_t}")
    table = details.get("table_name") or ""
    index = details.get("index_name") or ""
    access_type = details.get("access_type") or ""
    if table:
        parts.append(f"Table: {table}")
    if index:
        parts.append(f"Index: {index}")
    if access_type:
        parts.append(f"Access: {access_type}")
    actual_rows = float(op.get("rows") or 0)
    est_rows = details.get("estimated_rows")
    if est_rows is not None and float(est_rows) > 0:
        ratio = actual_rows / float(est_rows)
        est_str = f"Est.rows: {float(est_rows):.0f}"
        if ratio > 2:
            est_str += f"  \u26a0 Optimizer underestimate ({ratio:.1f}\u00d7)"
        elif ratio < 0.5 and actual_rows > 0:
            est_str += "  \u26a0 Optimizer overestimate"
        parts.append(est_str)
    cond = details.get("condition") or ""
    if cond:
        c_short = (cond[:50] + "\u2026") if len(cond) > 52 else cond
        parts.append(f"Cond: {c_short}")
    return "  \u00b7  ".join(parts)


def render_bargraph(root, width=1200, title="MySQL Query Performance", unit_display="ms", total_time=None, analysis=None):
    """Generate bar chart SVG. root is parsed tree; total_time and unit_display from caller."""
    all_nodes = sorted(flatten_nodes(root), key=lambda n: n["self_time"], reverse=True)
    all_nodes = [n for n in all_nodes if n["self_time"] >= 0.001]
    total_time = total_time or max(0.001, sum(n["self_time"] for n in all_nodes))

    bar_height = 28
    bar_gap = 6
    left_margin = 10
    right_margin = 10
    top_margin = 80
    details_lines_n = 8          # number of pre-allocated detail text lines
    details_line_h = 14          # px per line
    bottom_margin = 16 + details_lines_n * details_line_h + 12  # ~140px
    label_width = 320
    loops_width = 80
    time_width = 120
    bar_area_width = width - left_margin - right_margin - label_width - loops_width - time_width - 20
    num_bars = len(all_nodes)

    _info_lines, info_panel_h = (
        render_info_panel(analysis, left_margin, 0, width - left_margin - right_margin, view_type="bargraph")
        if analysis is not None
        else ([], 0)
    )
    info_gap = 8 if analysis is not None else 0
    chart_height = top_margin + (num_bars * (bar_height + bar_gap)) + bottom_margin + info_gap + info_panel_h

    colors = [
        "rgb(255,90,90)", "rgb(255,130,70)", "rgb(255,165,50)", "rgb(255,200,50)",
        "rgb(255,220,80)", "rgb(200,200,100)", "rgb(150,200,150)", "rgb(100,180,180)",
    ]
    col_label_x = left_margin
    col_loops_x = left_margin + label_width
    col_bar_x = col_loops_x + loops_width
    col_time_x = col_bar_x + bar_area_width + 10

    total_str = f"{total_time:.0f}" if total_time >= 1 else f"{total_time:.3f}"
    sep_y = chart_height - bottom_margin + 6
    details_y_start = sep_y + 12

    lines = [
        '<?xml version="1.0" standalone="no"?>',
        '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">',
        f'<svg version="1.1" width="{width}" height="{chart_height}" onload="init(evt)" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">',
        "<style>",
        "  text { font-family: Arial, sans-serif; font-size: 12px; }",
        "  .title { font-size: 18px; font-weight: bold; }",
        "  .subtitle { font-size: 11px; fill: #666; }",
        "  .col-header { font-size: 10px; fill: #888; font-weight: bold; letter-spacing: 0.5px; }",
        "  .label { font-size: 11px; fill: #333; }",
        "  .loops { font-size: 10px; fill: #777; }",
        "  .value { font-size: 11px; font-weight: bold; fill: #222; }",
        "  .bar { transition: opacity 0.12s; }",
        "  .bar:hover { opacity: 0.82; cursor: pointer; stroke: #444; stroke-width: 2; }",
        "  .bar.in-query-analysis:hover { stroke: #c62828; stroke-width: 2.5; }",
        "  .bar.pinned { stroke: #1565c0; stroke-width: 2.5; }",
        "  .details-line { font-size: 12px; fill: #222; user-select: text; -webkit-user-select: text; }",
        "  .details-line.dim { fill: #999; }",
        "  #search { font-size: 12px; fill: #666; cursor: pointer; }",
        "  #search:hover { fill: #111; }",
        "</style>",
        '<rect width="100%" height="100%" fill="#fafafa"/>',
        f'<text x="{width/2}" y="28" text-anchor="middle" class="title">{xml_escape(title)}</text>',
        f'<text x="{width/2}" y="46" text-anchor="middle" class="subtitle">Self-time per operation  \u00b7  Total: {total_str} {unit_display}</text>',
        f'<text x="{col_label_x + label_width - 10}" y="68" text-anchor="end" class="col-header">OPERATION</text>',
        f'<text x="{col_loops_x + loops_width/2}" y="68" text-anchor="middle" class="col-header">LOOPS</text>',
        f'<text x="{col_bar_x + bar_area_width/2}" y="68" text-anchor="middle" class="col-header">SELF-TIME</text>',
        f'<text id="search" x="{width - right_margin}" y="68" text-anchor="end">Search</text>',
        f'<line x1="{left_margin}" y1="72" x2="{width - right_margin}" y2="72" stroke="#e0e0e0" stroke-width="1"/>',
        f'<line x1="{left_margin}" y1="{sep_y}" x2="{width - right_margin}" y2="{sep_y}" stroke="#e0e0e0" stroke-width="1"/>',
        f'<rect x="{left_margin}" y="{sep_y + 4}" width="{width - left_margin - right_margin}" height="{bottom_margin - 10}" fill="#f5f5f5" rx="4"/>',
    ]
    # Pre-allocate multi-line details area: first line is the default hint
    for di in range(details_lines_n):
        dy = details_y_start + di * details_line_h
        default_text = "Hover a bar for details; click to pin  \u00b7  Ctrl+F to search" if di == 0 else ""
        lines.append(
            f'<text id="details-l{di}" x="{left_margin + 10}" y="{dy}" '
            f'text-anchor="start" class="details-line">{xml_escape(default_text)}</text>'
        )
    lines.append("<!-- end details area -->")

    highlight_msg_by_label = {}
    if analysis is not None:
        for nh in analysis.get("node_highlights") or []:
            lbl = (nh.get("short_label") or "").strip()
            if lbl:
                highlight_msg_by_label[lbl] = nh.get("message") or ""

    y = top_margin
    for i, op in enumerate(all_nodes):
        pct = (op["self_time"] / total_time) * 100
        bar_width = max(3, (op["self_time"] / total_time) * bar_area_width) if op["self_time"] > 0 else 0
        color = colors[i % len(colors)]
        label = (op["short_label"] or "")[:48] + ("..." if len(op["short_label"] or "") > 48 else "")
        text_y = y + (bar_height / 2) + 4
        st = f"{op['self_time']:.0f}" if op["self_time"] >= 1 else f"{op['self_time']:.3f}"
        loops_t = format_number(op["loops"])
        info = _build_bar_info(op, st, pct, unit_display)
        bar_label_attr = _attr_escape(op.get("short_label") or op.get("full_label") or "")
        info_attr = _attr_escape(info)
        short_lbl = (op.get("short_label") or "").strip()
        analysis_msg = highlight_msg_by_label.get(short_lbl, "")
        analysis_attr = _attr_escape(analysis_msg)[:400] if analysis_msg else ""
        bar_class = "bar in-query-analysis" if analysis_attr else "bar"
        lines.append(f'<text x="{col_label_x + label_width - 10}" y="{text_y}" text-anchor="end" class="label">{xml_escape(label)}</text>')
        lines.append(f'<text x="{col_loops_x + loops_width/2}" y="{text_y}" text-anchor="middle" class="loops">{loops_t}</text>')
        lines.append(
            f'<rect class="{bar_class}" x="{col_bar_x}" y="{y}" width="{bar_width}" height="{bar_height}" fill="{color}" rx="3" ry="3" '
            f'data-info="{info_attr}" data-label="{bar_label_attr}"'
            + (f' data-analysis-msg="{analysis_attr}"' if analysis_attr else "")
            + "/>"
        )
        value_text = f"{st} {unit_display} ({pct:.1f}%)" if op["self_time"] >= 1 else f"{op['self_time']:.3f} {unit_display} ({pct:.1f}%)"
        lines.append(f'<text x="{col_time_x}" y="{text_y}" class="value">{value_text}</text>')
        y += bar_height + bar_gap

    if analysis is not None:
        panel_y = top_margin + (num_bars * (bar_height + bar_gap)) + bottom_margin + info_gap
        panel_w = width - left_margin - right_margin
        panel_lines, _ = render_info_panel(analysis, left_margin, panel_y, panel_w, view_type="bargraph")
        lines.extend(panel_lines)

    lines.append("""<script type="text/ecmascript"><![CDATA[
(function() {
  var detailLines = [];
  var N_LINES = 8;
  var searchBtn;
  var defaultHint = "Hover a bar for details; click a bar to pin details  \u00b7  Ctrl+F to search";
  var searching = false;
  var pinnedBar = null;

  function setDetailsText(parts) {
    for (var i = 0; i < N_LINES; i++) {
      if (!detailLines[i]) continue;
      detailLines[i].textContent = (i < parts.length) ? parts[i] : "";
      detailLines[i].setAttribute("class", "details-line" + (i > 0 && i >= parts.length - 1 && parts.length > 1 ? " dim" : ""));
    }
  }

  function clearDetails() {
    if (detailLines[0]) detailLines[0].textContent = defaultHint;
    for (var i = 1; i < N_LINES; i++) {
      if (detailLines[i]) { detailLines[i].textContent = ""; detailLines[i].setAttribute("class", "details-line"); }
    }
  }

  function setDetailsForBar(bar) {
    if (!bar || !detailLines[0]) return;
    var info = bar.getAttribute("data-info") || "";
    var analysisMsg = bar.getAttribute("data-analysis-msg");
    if (analysisMsg)
      info = info + "  \u00b7  \u26a0 In Query Analysis: " + analysisMsg;
    var sep = "  \u00b7  ";
    var parts = info.split(sep);
    setDetailsText(parts);
  }

  function init(evt) {
    for (var i = 0; i < N_LINES; i++) {
      var el = document.getElementById("details-l" + i);
      if (el) detailLines.push(el);
    }
    searchBtn = document.getElementById("search");
    if (!detailLines[0]) return;
    document.addEventListener("mouseover", function(e) {
      if (e.target.classList && e.target.classList.contains("bar")) {
        if (!pinnedBar) setDetailsForBar(e.target);
      }
    });
    document.addEventListener("mouseout", function(e) {
      if (e.target.classList && e.target.classList.contains("bar") && !searching && !pinnedBar)
        clearDetails();
    });
    document.addEventListener("click", function(e) {
      if (e.target.id === "search") { searchPrompt(); return; }
      if (e.target.classList && e.target.classList.contains("bar")) {
        var bars = document.querySelectorAll(".bar.pinned");
        for (var i = 0; i < bars.length; i++) bars[i].classList.remove("pinned");
        if (pinnedBar === e.target) {
          pinnedBar = null;
          clearDetails();
        } else {
          pinnedBar = e.target;
          pinnedBar.classList.add("pinned");
          setDetailsForBar(pinnedBar);
        }
        return;
      }
      if (pinnedBar) {
        pinnedBar.classList.remove("pinned");
        pinnedBar = null;
        clearDetails();
      }
    });
    document.addEventListener("keydown", function(e) {
      if (e.ctrlKey && e.key === "f") { e.preventDefault(); searchPrompt(); }
    });
  }

  function searchPrompt() {
    if (searchBtn.textContent === "Reset Search") {
      var bars = document.querySelectorAll(".bar");
      for (var i = 0; i < bars.length; i++) {
        bars[i].style.opacity = "";
        bars[i].style.stroke = "";
        bars[i].style.strokeWidth = "";
      }
      searchBtn.textContent = "Search";
      if (!pinnedBar) clearDetails();
      searching = false;
      return;
    }
    var term = prompt("Search (regex):");
    if (term == null) return;
    var re;
    try { re = new RegExp(term, "i"); } catch (err) { alert("Invalid regex"); return; }
    var bars = document.querySelectorAll(".bar");
    for (var i = 0; i < bars.length; i++) {
      var label = bars[i].getAttribute("data-label") || "";
      if (re.test(label)) {
        bars[i].style.opacity = "1";
        bars[i].style.stroke = "rgb(230,0,230)";
        bars[i].style.strokeWidth = "2";
      } else {
        bars[i].style.opacity = "0.3";
        bars[i].style.stroke = "";
        bars[i].style.strokeWidth = "";
      }
    }
    searchBtn.textContent = "Reset Search";
    searching = true;
    setDetailsText(["Matches: " + term + "  \u00b7  Click \u2018Reset Search\u2019 to clear"]);
  }

  window.init = init;
})();
]]></script>
</svg>
""")
    return "\n".join(lines)
