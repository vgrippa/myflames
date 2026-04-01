"""
Single-file HTML report: embeds SVG chart + analysis panel + metadata.
"""
from .parser import (
    parse_explain,
    flatten_nodes,
    analyze_plan,
    format_sql,
    xml_escape,
)
from .flamegraph import folded_to_svg
from .output_bargraph import render_bargraph
from .output_treemap import render_treemap
from .output_diagram import render_diagram
from .output_tree import render_tree


def _render_svg(root, view_type, analysis, width, title, unit, **kwargs):
    """Produce the SVG string for a given view type."""
    from .parser import (
        build_flame_entries,
        flatten_nodes as _flat,
        enhance_tooltip_flame,
        render_info_panel,
    )
    import re as _re

    if view_type == "bargraph":
        total_time = root["total_time"]
        return render_bargraph(root, width=width, title=title,
                               unit_display=unit, total_time=total_time,
                               analysis=analysis)

    if view_type == "treemap":
        return render_treemap(root, width=width, title=title,
                              unit_display=unit, analysis=analysis)

    if view_type == "diagram":
        return render_diagram(root, width=width, title=title,
                              unit_display=unit, analysis=analysis)

    if view_type == "tree":
        return render_tree(root, width=width, title=title,
                           unit_display=unit, analysis=analysis)

    # flamegraph (default)
    max_time = root["total_time"]
    use_microseconds = max_time > 0 and max_time < 1
    multiplier = 1000 if use_microseconds else 1
    entries = list(build_flame_entries(root))
    folded_lines = []
    for path, time in entries:
        t = time * multiplier
        t = int(t + 0.5)
        t = 1 if t == 0 and len(path) == 1 else t
        if t <= 0:
            continue
        folded_lines.append(";".join(path) + " " + str(t))
    folded_text = "\n".join(folded_lines)
    if not folded_text.strip():
        return ""
    svg = folded_to_svg(
        folded_text, title=title, width=width,
        height=kwargs.get("frame_height", 32),
        countname=unit,
        inverted=kwargs.get("inverted", False),
        colors=kwargs.get("colors", "hot"),
    )
    # Enhance tooltips
    op_details = {n["folded_label"]: n["details"] for n in _flat(root)}
    def repl(m):
        original = m.group(1).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&quot;", '"')
        enhanced = enhance_tooltip_flame(original, op_details)
        return "<title>" + xml_escape(enhanced).replace("\n", "&#10;") + "</title>"
    svg = _re.sub(r"<title>([^<]+)</title>", repl, svg)
    # Inject info panel
    m_height = _re.search(r'<svg[^>]+height="(\d+)"', svg)
    m_width = _re.search(r'<svg[^>]+width="(\d+)"', svg)
    if m_height and m_width:
        old_h = int(m_height.group(1))
        svg_w = int(m_width.group(1))
        panel_lines, panel_h = render_info_panel(analysis, 4, old_h + 8, svg_w - 8, view_type="flamegraph")
        new_h = old_h + 8 + panel_h
        panel_svg = "\n".join(panel_lines)
        svg = _re.sub(r'(height=")(\d+)(")', lambda mo: '%s%s%s' % (mo.group(1), new_h, mo.group(3)), svg, count=1)
        svg = _re.sub(
            r'(viewBox="0 0 \d+ )(\d+)(")',
            lambda mo: '%s%s%s' % (mo.group(1), new_h, mo.group(3)),
            svg, count=1
        )
        svg = svg.replace("</svg>", panel_svg + "\n</svg>", 1)
    return svg


def _format_time(ms):
    if ms is None:
        return "-"
    if ms >= 1000:
        return "%.2f s" % (ms / 1000)
    if ms >= 1:
        return "%.2f ms" % ms
    return "%.0f us" % (ms * 1000)


def render_html_report(json_text, view_type="flamegraph", width=1200,
                       title="MySQL Query Plan", query_text="", **kwargs):
    """Produce a self-contained HTML report string."""
    root = parse_explain(json_text)
    max_time = root["total_time"]
    use_microseconds = max_time > 0 and max_time < 1
    unit = "\u00b5s" if use_microseconds else "ms"

    analysis = analyze_plan(root)
    if query_text:
        analysis["query_text_lines"] = format_sql(query_text)

    if view_type == "flamegraph":
        svg_width = kwargs.get("width", 1800) if kwargs.get("width") else 1800
    else:
        svg_width = width

    svg = _render_svg(root, view_type, analysis, svg_width, title, unit, **kwargs)

    # Strip XML declaration and DOCTYPE from SVG for embedding
    import re
    svg_embed = re.sub(r'^<\?xml[^?]*\?>\s*', '', svg)
    svg_embed = re.sub(r'<!DOCTYPE[^>]*>\s*', '', svg_embed)

    # Build analysis summary for HTML sidebar
    total_time_str = _format_time(root["total_time"])
    nodes = list(flatten_nodes(root))
    node_count = len(nodes)

    warnings_html = ""
    for w in analysis.get("warnings", []):
        warnings_html += '<li class="warn-item">%s</li>\n' % xml_escape(w)

    suggestions_html = ""
    for s in analysis.get("suggestions", []):
        suggestions_html += '<li class="suggest-item">%s</li>\n' % xml_escape(s)

    features_html = ""
    for f in analysis.get("optimizer_features", []):
        features_html += '<span class="feature-tag">%s</span>\n' % xml_escape(f)

    index_html = ""
    for idx in analysis.get("index_suggestions", []):
        index_html += '<li class="idx-item"><code>%s</code><br><small>%s</small></li>\n' % (
            xml_escape(idx.get("ddl", "")), xml_escape(idx.get("reason", "")))

    query_html = ""
    for line in analysis.get("query_text_lines", []):
        query_html += xml_escape(line) + "\n"

    # Full scans summary
    full_scans = analysis.get("full_scans", [])
    scans_html = ""
    for fs in full_scans:
        scans_html += '<li>%s (%s rows, %d loops)</li>\n' % (
            xml_escape(fs.get("table", "?")),
            _format_rows(fs.get("rows", 0)),
            fs.get("loops", 1))

    html = _HTML_TEMPLATE.replace("{{TITLE}}", xml_escape(title))
    html = html.replace("{{SVG_CONTENT}}", svg_embed)
    html = html.replace("{{TOTAL_TIME}}", xml_escape(total_time_str))
    html = html.replace("{{NODE_COUNT}}", str(node_count))
    html = html.replace("{{VIEW_TYPE}}", xml_escape(view_type))
    html = html.replace("{{WARNINGS_HTML}}", warnings_html)
    html = html.replace("{{SUGGESTIONS_HTML}}", suggestions_html)
    html = html.replace("{{FEATURES_HTML}}", features_html)
    html = html.replace("{{INDEX_HTML}}", index_html)
    html = html.replace("{{QUERY_HTML}}", query_html)
    html = html.replace("{{SCANS_HTML}}", scans_html)
    return html


def _format_rows(n):
    if n is None:
        return "?"
    if n >= 1e6:
        return "%.2fM" % (n / 1e6)
    if n >= 1e3:
        return "%.2fK" % (n / 1e3)
    return str(int(n))


_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}} — myflames report</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    background: #f5f5f5; color: #222; line-height: 1.5;
  }
  .header {
    background: #1a1a2e; color: #fff; padding: 16px 24px;
    display: flex; align-items: center; justify-content: space-between;
  }
  .header h1 { font-size: 18px; font-weight: 600; }
  .header .meta { font-size: 13px; color: #aab; }
  .header .meta span { margin-left: 16px; }
  .main { display: flex; gap: 0; min-height: calc(100vh - 56px); }
  .chart-panel {
    flex: 1; overflow: auto; padding: 16px; background: #fff;
    border-right: 1px solid #e0e0e0;
  }
  .chart-panel svg { max-width: 100%; height: auto; }
  .sidebar {
    width: 340px; min-width: 280px; padding: 16px; overflow-y: auto;
    background: #fafafa; font-size: 13px;
  }
  .sidebar h2 { font-size: 14px; font-weight: 600; margin: 16px 0 8px; border-bottom: 1px solid #e0e0e0; padding-bottom: 4px; }
  .sidebar h2:first-child { margin-top: 0; }
  .sidebar ul { list-style: none; padding: 0; }
  .sidebar li { padding: 4px 0; border-bottom: 1px solid #f0f0f0; }
  .warn-item { color: #c62828; }
  .suggest-item { color: #2e7d32; }
  .idx-item code { font-size: 12px; background: #fff8e1; padding: 2px 4px; border-radius: 3px; display: inline-block; margin-bottom: 2px; }
  .idx-item small { color: #666; }
  .feature-tag {
    display: inline-block; background: #e3f2fd; color: #1565c0;
    padding: 2px 8px; border-radius: 12px; font-size: 11px; margin: 2px 4px 2px 0;
  }
  .query-block {
    background: #f5f5f5; border: 1px solid #e0e0e0; border-radius: 4px;
    padding: 8px 10px; font-family: "SFMono-Regular", Consolas, monospace;
    font-size: 12px; white-space: pre-wrap; word-break: break-word;
    max-height: 200px; overflow-y: auto;
  }
  .toolbar {
    padding: 8px 16px; background: #fff; border-bottom: 1px solid #e0e0e0;
    display: flex; gap: 8px; align-items: center;
  }
  .toolbar button {
    padding: 6px 14px; border: 1px solid #ccc; border-radius: 4px;
    background: #fff; cursor: pointer; font-size: 12px;
  }
  .toolbar button:hover { background: #f0f0f0; }
  .stat-card {
    display: inline-block; background: #fff; border: 1px solid #e0e0e0;
    border-radius: 6px; padding: 8px 14px; margin: 4px 4px 4px 0; text-align: center;
  }
  .stat-card .val { font-size: 20px; font-weight: 700; color: #1565c0; }
  .stat-card .lbl { font-size: 11px; color: #888; }
  @media (max-width: 900px) {
    .main { flex-direction: column; }
    .sidebar { width: 100%; min-width: 0; border-top: 1px solid #e0e0e0; }
    .chart-panel { border-right: none; }
  }
</style>
</head>
<body>

<div class="header">
  <h1>{{TITLE}}</h1>
  <div class="meta">
    <span>View: {{VIEW_TYPE}}</span>
    <span>Total: {{TOTAL_TIME}}</span>
    <span>Operators: {{NODE_COUNT}}</span>
  </div>
</div>

<div class="toolbar">
  <button onclick="exportSVG()">Export SVG</button>
  <button onclick="exportJSON()">Export Analysis JSON</button>
  <button onclick="window.print()">Print / PDF</button>
</div>

<div class="main">
  <div class="chart-panel" id="chart-panel">
    {{SVG_CONTENT}}
  </div>
  <div class="sidebar">
    <div>
      <div class="stat-card"><div class="val">{{TOTAL_TIME}}</div><div class="lbl">Total Time</div></div>
      <div class="stat-card"><div class="val">{{NODE_COUNT}}</div><div class="lbl">Operators</div></div>
    </div>

    <h2>SQL Query</h2>
    <div class="query-block">{{QUERY_HTML}}</div>

    <h2>Optimizer Features</h2>
    <div>{{FEATURES_HTML}}</div>

    <h2>Warnings</h2>
    <ul>{{WARNINGS_HTML}}</ul>

    <h2>Suggestions</h2>
    <ul>{{SUGGESTIONS_HTML}}</ul>

    <h2>Index Suggestions</h2>
    <ul>{{INDEX_HTML}}</ul>

    <h2>Full Table Scans</h2>
    <ul>{{SCANS_HTML}}</ul>
  </div>
</div>

<script>
function exportSVG() {
  var svg = document.querySelector('#chart-panel svg');
  if (!svg) { alert('No SVG found'); return; }
  var data = new XMLSerializer().serializeToString(svg);
  var blob = new Blob(['<?xml version="1.0" standalone="no"?>\n', data], {type: 'image/svg+xml'});
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'query-plan.svg';
  a.click();
  URL.revokeObjectURL(a.href);
}
function exportJSON() {
  var data = {
    title: document.querySelector('.header h1').textContent,
    total_time: '{{TOTAL_TIME}}',
    node_count: {{NODE_COUNT}},
    view_type: '{{VIEW_TYPE}}'
  };
  var blob = new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'});
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'query-analysis.json';
  a.click();
  URL.revokeObjectURL(a.href);
}
</script>
</body>
</html>
"""
