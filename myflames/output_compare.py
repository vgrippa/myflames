"""
Before vs After comparison: produces a self-contained HTML diff report.
"""
from .parser import parse_explain, flatten_nodes, analyze_plan, format_sql, xml_escape


def _format_time(ms):
    if ms is None:
        return "-"
    if ms >= 1000:
        return "%.2f s" % (ms / 1000)
    if ms >= 1:
        return "%.2f ms" % ms
    return "%.0f \u00b5s" % (ms * 1000)


def _format_rows(n):
    if n is None:
        return "?"
    if n >= 1e6:
        return "%.2fM" % (n / 1e6)
    if n >= 1e3:
        return "%.2fK" % (n / 1e3)
    return str(int(n))


def _delta_str(before, after, unit="ms", lower_is_better=True):
    """Return a delta string with arrow and color class."""
    if before is None or after is None:
        return "", "neutral"
    diff = after - before
    if abs(diff) < 0.001:
        return "no change", "neutral"
    arrow = "\u2191" if diff > 0 else "\u2193"
    sign = "+" if diff > 0 else ""
    improved = (diff < 0) if lower_is_better else (diff > 0)
    cls = "better" if improved else "worse"
    # Avoid absurd percentages when before is zero or near-zero
    if before == 0 or abs(diff / before) > 100:
        return "%s new" % arrow if before == 0 else "%s large change" % arrow, cls
    pct = diff / before * 100
    return "%s%s%.1f%%" % (arrow, sign, pct), cls


def _match_nodes(nodes_before, nodes_after):
    """Match nodes between two plans by short_label."""
    by_label_b = {}
    for n in nodes_before:
        lbl = n.get("short_label", "")
        if lbl not in by_label_b:
            by_label_b[lbl] = n

    by_label_a = {}
    for n in nodes_after:
        lbl = n.get("short_label", "")
        if lbl not in by_label_a:
            by_label_a[lbl] = n

    all_labels = list(by_label_b.keys())
    for lbl in by_label_a:
        if lbl not in by_label_b:
            all_labels.append(lbl)

    matched = []
    for lbl in all_labels:
        matched.append((lbl, by_label_b.get(lbl), by_label_a.get(lbl)))
    return matched


def render_compare(json_before, json_after, title="Query Plan Comparison"):
    """Produce a self-contained HTML comparison report."""
    root_b = parse_explain(json_before)
    root_a = parse_explain(json_after)
    analysis_b = analyze_plan(root_b)
    analysis_a = analyze_plan(root_a)

    nodes_b = list(flatten_nodes(root_b))
    nodes_a = list(flatten_nodes(root_a))

    total_b = root_b["total_time"]
    total_a = root_a["total_time"]
    time_delta, time_cls = _delta_str(total_b, total_a)

    # Matched operator rows
    matched = _match_nodes(nodes_b, nodes_a)
    rows_html = []
    for label, nb, na in matched:
        st_b = nb["self_time"] if nb else None
        st_a = na["self_time"] if na else None
        rows_b = nb["rows"] if nb else None
        rows_a = na["rows"] if na else None
        loops_b = nb["loops"] if nb else None
        loops_a = na["loops"] if na else None

        delta_time, delta_cls = _delta_str(st_b, st_a)

        status = ""
        if nb is None:
            status = '<span class="badge new">NEW</span>'
        elif na is None:
            status = '<span class="badge removed">REMOVED</span>'

        rows_html.append(
            '<tr class="%s">'
            "<td>%s %s</td>"
            "<td>%s</td><td>%s</td>"
            "<td>%s</td><td>%s</td>"
            "<td>%s</td><td>%s</td>"
            '<td class="%s">%s</td>'
            "</tr>" % (
                delta_cls,
                xml_escape(label), status,
                _format_time(st_b) if st_b is not None else "-",
                _format_time(st_a) if st_a is not None else "-",
                _format_rows(rows_b) if rows_b is not None else "-",
                _format_rows(rows_a) if rows_a is not None else "-",
                str(loops_b) if loops_b is not None else "-",
                str(loops_a) if loops_a is not None else "-",
                delta_cls, delta_time,
            )
        )

    # Full scans comparison
    scans_b = set(fs["table"] for fs in analysis_b.get("full_scans", []))
    scans_a = set(fs["table"] for fs in analysis_a.get("full_scans", []))
    new_scans = scans_a - scans_b
    removed_scans = scans_b - scans_a

    # Warnings comparison
    warns_b = set(analysis_b.get("warnings", []))
    warns_a = set(analysis_a.get("warnings", []))
    new_warns = warns_a - warns_b
    resolved_warns = warns_b - warns_a

    # Summary items
    summary_items = []
    if total_a < total_b:
        summary_items.append(('<span class="better">\u2713 Query got faster: %s \u2192 %s (%s)</span>' %
                              (xml_escape(_format_time(total_b)), xml_escape(_format_time(total_a)), time_delta)))
    elif total_a > total_b:
        summary_items.append(('<span class="worse">\u2717 Query got slower: %s \u2192 %s (%s)</span>' %
                              (xml_escape(_format_time(total_b)), xml_escape(_format_time(total_a)), time_delta)))
    else:
        summary_items.append('<span class="neutral">Query time unchanged: %s</span>' % xml_escape(_format_time(total_b)))

    if removed_scans:
        for t in sorted(removed_scans):
            summary_items.append('<span class="better">\u2713 Full scan removed: %s</span>' % xml_escape(t))
    if new_scans:
        for t in sorted(new_scans):
            summary_items.append('<span class="worse">\u2717 New full scan: %s</span>' % xml_escape(t))
    if resolved_warns:
        for w in sorted(resolved_warns):
            summary_items.append('<span class="better">\u2713 Resolved: %s</span>' % xml_escape(w))
    if new_warns:
        for w in sorted(new_warns):
            summary_items.append('<span class="worse">\u2717 New warning: %s</span>' % xml_escape(w))

    summary_html = "\n".join('<li>%s</li>' % s for s in summary_items)

    html = _COMPARE_TEMPLATE
    html = html.replace("{{TITLE}}", xml_escape(title))
    html = html.replace("{{TOTAL_BEFORE}}", xml_escape(_format_time(total_b)))
    html = html.replace("{{TOTAL_AFTER}}", xml_escape(_format_time(total_a)))
    html = html.replace("{{TIME_DELTA}}", time_delta)
    html = html.replace("{{TIME_CLS}}", time_cls)
    html = html.replace("{{OPS_BEFORE}}", str(len(nodes_b)))
    html = html.replace("{{OPS_AFTER}}", str(len(nodes_a)))
    html = html.replace("{{ROWS_HTML}}", "\n".join(rows_html))
    html = html.replace("{{SUMMARY_HTML}}", summary_html)
    return html


_COMPARE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}} — myflames</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    background: #f5f5f5; color: #222; line-height: 1.5; padding: 24px;
  }
  h1 { font-size: 22px; margin-bottom: 4px; }
  .subtitle { color: #666; font-size: 14px; margin-bottom: 20px; }
  .cards { display: flex; gap: 16px; margin-bottom: 24px; flex-wrap: wrap; }
  .card {
    background: #fff; border: 1px solid #e0e0e0; border-radius: 8px;
    padding: 16px 24px; text-align: center; min-width: 160px;
  }
  .card .val { font-size: 28px; font-weight: 700; }
  .card .lbl { font-size: 12px; color: #888; margin-top: 4px; }
  .card.better .val { color: #2e7d32; }
  .card.worse .val { color: #c62828; }
  .card.neutral .val { color: #1565c0; }
  .section { background: #fff; border: 1px solid #e0e0e0; border-radius: 8px; padding: 20px; margin-bottom: 20px; }
  .section h2 { font-size: 16px; margin-bottom: 12px; border-bottom: 1px solid #eee; padding-bottom: 6px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { background: #f8f8f8; text-align: left; padding: 8px 10px; border-bottom: 2px solid #e0e0e0; font-weight: 600; }
  td { padding: 6px 10px; border-bottom: 1px solid #f0f0f0; }
  tr.worse { background: #fff5f5; }
  tr.better { background: #f5fff5; }
  .better { color: #2e7d32; }
  .worse { color: #c62828; }
  .neutral { color: #666; }
  .badge {
    display: inline-block; font-size: 10px; padding: 1px 6px; border-radius: 8px;
    font-weight: 600; margin-left: 6px; vertical-align: middle;
  }
  .badge.new { background: #e3f2fd; color: #1565c0; }
  .badge.removed { background: #fce4ec; color: #c62828; }
  ul { list-style: none; }
  ul li { padding: 4px 0; }
  .footer { margin-top: 24px; text-align: center; color: #aaa; font-size: 12px; }
</style>
</head>
<body>

<h1>{{TITLE}}</h1>
<p class="subtitle">Before vs After — generated by myflames</p>

<div class="cards">
  <div class="card neutral">
    <div class="val">{{TOTAL_BEFORE}}</div>
    <div class="lbl">Before</div>
  </div>
  <div class="card neutral">
    <div class="val">{{TOTAL_AFTER}}</div>
    <div class="lbl">After</div>
  </div>
  <div class="card {{TIME_CLS}}">
    <div class="val">{{TIME_DELTA}}</div>
    <div class="lbl">Change</div>
  </div>
  <div class="card neutral">
    <div class="val">{{OPS_BEFORE}} / {{OPS_AFTER}}</div>
    <div class="lbl">Operators (before / after)</div>
  </div>
</div>

<div class="section">
  <h2>What changed</h2>
  <ul>
    {{SUMMARY_HTML}}
  </ul>
</div>

<div class="section">
  <h2>Operator comparison</h2>
  <table>
    <thead>
      <tr>
        <th>Operator</th>
        <th>Self-time (before)</th>
        <th>Self-time (after)</th>
        <th>Rows (before)</th>
        <th>Rows (after)</th>
        <th>Loops (before)</th>
        <th>Loops (after)</th>
        <th>Delta</th>
      </tr>
    </thead>
    <tbody>
      {{ROWS_HTML}}
    </tbody>
  </table>
</div>

<div class="footer">Generated by myflames — MySQL Query Plan Visualizer</div>

</body>
</html>
"""
