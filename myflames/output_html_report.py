"""
Single-file HTML report for a myflames EXPLAIN plan.

This module is the main public entry point for ``myflames --output foo.html``.
It consumes the same sidecar payload that :mod:`myflames.output_sidecar`
produces, so the human-readable HTML and the machine-readable JSON never drift.

Design
------
The HTML is designed for three audiences at once (see
``.claude/skills/progressive-ux/SKILL.md``):

* **Newcomers** land on a plain-English summary and a single primary action
  above the fold. Jargon terms (``filesort``, ``BNL``, ``hash join``, …)
  get glossary-chip tooltips via ``<abbr title="…">`` — zero-JS, keyboard
  accessible, screen-reader friendly.
* **Senior DBAs** see all metrics and suggestions in dense sections below,
  with every SET/CREATE/ALTER statement in a selectable ``<pre>`` so they
  can paste into Slack or a ticket without OCR'ing the SVG.
* **AI agents and external tools** read the same data from a JSON-LD block
  embedded in ``<head>`` (identical schema to the standalone sidecar file).

The visualization SVG is embedded WITHOUT its internal info panel — the
HTML owns the panels, selectable and glossary-aware. A standalone SVG
(e.g. ``myflames --output foo.svg``) still carries its embedded panel so
it works on its own.
"""
import json
import re as _re

from .parser import (
    parse_explain,
    flatten_nodes,
    analyze_plan,
    format_sql,
    xml_escape,
)
from .glossary import (
    find_terms_in_text,
    lookup as glossary_lookup,
    generate_executive_summary,
)
from .output_sidecar import build_sidecar, _compute_plan_summary


# ---------------------------------------------------------------------------
# SVG rendering (without info panel — the HTML owns the panels)
# ---------------------------------------------------------------------------

def _render_svg(root, view_type, width, title, unit, **kwargs):
    """Produce an SVG for a given view type with NO embedded info panel.

    We pass ``analysis=None`` to every renderer so the resulting SVG is a
    pure visualization — the HTML layer renders warnings / suggestions /
    environment as semantic HTML instead.
    """
    from .flamegraph import folded_to_svg
    from .output_bargraph import render_bargraph
    from .output_treemap import render_treemap
    from .output_diagram import render_diagram
    from .output_tree import render_tree
    from .parser import (
        build_flame_entries, flatten_nodes as _flat,
        enhance_tooltip_flame,
    )

    if view_type == "bargraph":
        return render_bargraph(
            root, width=width, title=title,
            unit_display=unit, total_time=root["total_time"],
            analysis=None,
        )
    if view_type == "treemap":
        return render_treemap(
            root, width=width, title=title, unit_display=unit, analysis=None,
        )
    if view_type == "diagram":
        return render_diagram(
            root, width=width, title=title, unit_display=unit, analysis=None,
        )
    if view_type == "tree":
        return render_tree(
            root, width=width, title=title, unit_display=unit, analysis=None,
        )

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
    # Enhance tooltips with parser details (still useful on hover).
    op_details = {n["folded_label"]: n["details"] for n in _flat(root)}

    def repl(m):
        original = (m.group(1)
                    .replace("&lt;", "<").replace("&gt;", ">")
                    .replace("&amp;", "&").replace("&quot;", '"'))
        enhanced = enhance_tooltip_flame(original, op_details)
        return "<title>" + xml_escape(enhanced).replace("\n", "&#10;") + "</title>"
    svg = _re.sub(r"<title>([^<]+)</title>", repl, svg)
    return svg


# ---------------------------------------------------------------------------
# Glossary-aware text rendering
# ---------------------------------------------------------------------------

def _chipify(text):
    """Return *text* HTML-escaped, with jargon terms wrapped in ``<abbr>``.

    Each jargon hit uses :func:`glossary.find_terms_in_text` + lookup to
    find the short definition, and wraps the surface text in ``<abbr>``.
    This gives zero-JS tooltips on hover, and screen readers announce the
    full definition. Overlapping matches are impossible — ``find_terms_in_text``
    already picks the longest phrase per span.
    """
    if not text:
        return ""
    hits = find_terms_in_text(text)
    if not hits:
        return xml_escape(text)
    out = []
    cursor = 0
    for h in hits:
        if h["start"] > cursor:
            out.append(xml_escape(text[cursor:h["start"]]))
        entry = glossary_lookup(h["key"])
        tooltip = (entry or {}).get("short", "")
        out.append(
            '<abbr class="glossary-chip" title="{}">{}</abbr>'.format(
                xml_escape(tooltip),
                xml_escape(h["term"]),
            )
        )
        cursor = h["end"]
    if cursor < len(text):
        out.append(xml_escape(text[cursor:]))
    return "".join(out)


def _is_sql_action(text):
    """True if *text* looks like an executable SET / DDL / CALL statement.

    Used to decide whether a suggestion action should render as a selectable
    ``<pre><code>`` (copy-paste target) vs regular prose.
    """
    if not text:
        return False
    head = text.strip().upper().split()[0] if text.strip() else ""
    return head in {
        "SET", "CREATE", "ALTER", "DROP", "CALL", "GRANT", "REVOKE",
        "ANALYZE", "OPTIMIZE", "RENAME", "TRUNCATE",
    }


def _sanitize_for_jsonld(payload):
    """Escape characters that would break a ``<script>`` block.

    JSON itself is already valid inside ``<script>`` tags except when the
    literal string ``</`` appears (which would terminate the script early).
    We also escape ``<!--`` and ``-->`` defensively.
    """
    s = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False)
    s = s.replace("</", "<\\/")
    s = s.replace("<!--", "<\\!--").replace("-->", "--\\>")
    return s


def _format_time(ms):
    if ms is None:
        return "-"
    if ms >= 1000:
        return "{:.2f} s".format(ms / 1000)
    if ms >= 1:
        return "{:.2f} ms".format(ms)
    if ms > 0:
        return "{:.0f} µs".format(ms * 1000)
    return "-"


def _format_rows(n):
    n = int(n or 0)
    if n >= 1_000_000:
        return "{:.2f}M".format(n / 1_000_000)
    if n >= 1_000:
        return "{:.2f}K".format(n / 1_000)
    return "{:,}".format(n)


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def _render_exec_summary(sidecar):
    """Top strip: executive summary + quick stats grid.

    The text is the canonical sentence from glossary.generate_executive_summary
    and must match what the sidecar carries — no re-derivation, no drift.
    """
    ps = sidecar["plan_summary"]
    parts = ['<section class="exec-summary" aria-labelledby="exec-heading">']
    parts.append('  <h2 id="exec-heading" class="visually-hidden">Summary</h2>')
    parts.append('  <p class="exec-text">{}</p>'.format(_chipify(sidecar["executive_summary"])))
    parts.append('  <dl class="quick-stats">')
    parts.append(
        '    <div><dt>Total time</dt><dd>{}</dd></div>'.format(
            xml_escape(_format_time(ps["total_time_ms"]))
        )
    )
    parts.append(
        '    <div><dt>Rows returned</dt><dd>{}</dd></div>'.format(
            xml_escape(_format_rows(ps["rows_sent"]))
        )
    )
    parts.append(
        '    <div><dt>Rows examined</dt><dd>{}</dd></div>'.format(
            xml_escape(_format_rows(ps["rows_examined_estimate"]))
        )
    )
    parts.append(
        '    <div><dt>Operators</dt><dd>{}</dd></div>'.format(ps["operator_count"])
    )
    parts.append('  </dl>')
    parts.append('</section>')
    return "\n".join(parts)


def _render_primary_action(sidecar):
    """The single "Fix first" card above the fold.

    Uses ``sidecar.primary_action.ref`` to dereference the picked suggestion.
    Returns an empty string when there's no suggestion to promote so the
    page doesn't show an empty card.
    """
    ref = (sidecar.get("primary_action") or {}).get("ref") or ""
    m = _re.match(r"suggestions\[(\d+)\]", ref)
    if not m:
        return ""
    idx = int(m.group(1))
    suggestions = sidecar.get("suggestions") or []
    if idx >= len(suggestions):
        return ""
    s = suggestions[idx]
    sev = s.get("severity", "medium")
    action = s.get("action", "")
    why = s.get("why", "")

    parts = [
        '<section class="primary-action sev-{}" aria-labelledby="primary-heading" role="region">'.format(
            xml_escape(sev)
        ),
        '  <header class="primary-action-header">',
        '    <span class="badge badge-{}">Fix first</span>'.format(xml_escape(sev)),
        '    <h2 id="primary-heading">Primary recommendation</h2>',
        '  </header>',
    ]
    if _is_sql_action(action):
        parts.append('  <pre class="sql-action"><code>{}</code></pre>'.format(xml_escape(action)))
    else:
        parts.append('  <p class="action-text">{}</p>'.format(_chipify(action)))
    if why:
        parts.append('  <details class="why-details" open>')
        parts.append('    <summary>Why does this help?</summary>')
        parts.append('    <p>{}</p>'.format(_chipify(why)))
        parts.append('  </details>')
    parts.append('</section>')
    return "\n".join(parts)


def _render_query_card(sidecar):
    query = sidecar.get("query") or {}
    beautified = query.get("beautified") or query.get("raw") or ""
    if not beautified:
        return ""
    return (
        '<section class="query-card" aria-labelledby="query-heading">\n'
        '  <h2 id="query-heading">SQL</h2>\n'
        '  <pre class="sql-text"><code>{}</code></pre>\n'
        '</section>'
    ).format(xml_escape(beautified))


def _render_viz_card(svg_embed, view_type):
    return (
        '<section class="viz-card" aria-labelledby="viz-heading">\n'
        '  <h2 id="viz-heading">Execution plan ({})</h2>\n'
        '  <div class="chart-panel" id="chart-panel">\n{}\n  </div>\n'
        '</section>'
    ).format(xml_escape(view_type), svg_embed)


def _render_warnings(sidecar):
    warnings = sidecar.get("warnings") or []
    if not warnings:
        return ""
    parts = [
        '<section class="warnings-section" aria-labelledby="warn-heading">',
        '  <h2 id="warn-heading">Warnings <span class="count">({})</span></h2>'.format(len(warnings)),
        '  <ol class="warning-list">',
    ]
    for w in warnings:
        sev = w.get("severity", "warn")
        labels = w.get("node_labels") or []
        labels_html = ""
        if labels:
            chips = " ".join(
                '<code class="node-label">{}</code>'.format(xml_escape(l))
                for l in labels[:4]
            )
            labels_html = '<p class="where">Labeled: {}</p>'.format(chips)
        parts.append(
            '    <li class="warning sev-{}">\n'
            '      <span class="badge badge-{}">{}</span>\n'
            '      <p class="text">{}</p>\n'
            '      {}\n'
            '    </li>'.format(
                xml_escape(sev), xml_escape(sev),
                xml_escape(sev.upper()),
                _chipify(w.get("text", "")),
                labels_html,
            )
        )
    parts.append('  </ol>')
    parts.append('</section>')
    return "\n".join(parts)


def _render_suggestions(sidecar):
    suggestions = sidecar.get("suggestions") or []
    if not suggestions:
        return ""
    parts = [
        '<section class="suggestions-section" aria-labelledby="sug-heading">',
        '  <h2 id="sug-heading">Suggestions <span class="count">({})</span></h2>'.format(len(suggestions)),
        '  <ol class="suggestion-list">',
    ]
    for s in suggestions:
        sev = s.get("severity", "low")
        action = s.get("action", "")
        why = s.get("why", "")
        if _is_sql_action(action):
            action_block = (
                '<pre class="sql-action"><code>{}</code></pre>'.format(xml_escape(action))
            )
        else:
            action_block = (
                '<p class="action-text">{}</p>'.format(_chipify(action))
            )
        why_block = ""
        if why:
            why_block = (
                '<details class="why-details">\n'
                '        <summary>Why?</summary>\n'
                '        <p>{}</p>\n'
                '      </details>'.format(_chipify(why))
            )
        parts.append(
            '    <li class="suggestion sev-{}">\n'
            '      <span class="badge badge-{}">{}</span>\n'
            '      {}\n'
            '      {}\n'
            '    </li>'.format(
                xml_escape(sev), xml_escape(sev),
                xml_escape(sev.upper()),
                action_block, why_block,
            )
        )
    parts.append('  </ol>')
    parts.append('</section>')
    return "\n".join(parts)


def _render_environment(sidecar):
    """Collapsed ``<details>`` containing every collected artifact.

    Only rendered when the advisor actually ran — file-mode reports have
    no collected data and this section is skipped entirely (so newcomers
    don't see an empty expandable).
    """
    collected = sidecar.get("collected") or {}
    if not collected:
        return ""
    parts = [
        '<details class="env-section">',
        '  <summary><h2>Collected environment</h2></summary>',
        '  <div class="env-grid">',
    ]
    variables = collected.get("variables") or {}
    if variables:
        # Show a curated subset first; then the rest in a nested details.
        SHOW = (
            "version", "innodb_buffer_pool_size", "sort_buffer_size",
            "join_buffer_size", "tmp_table_size", "max_heap_table_size",
            "innodb_flush_log_at_trx_commit", "optimizer_switch",
        )
        parts.append('    <div class="env-card">')
        parts.append('      <h3>Session variables</h3>')
        parts.append('      <table class="kv-table">')
        for name in SHOW:
            if name in variables and variables[name]:
                parts.append(
                    '        <tr><th>{}</th><td><code>{}</code></td></tr>'.format(
                        xml_escape(name), xml_escape(str(variables[name])),
                    )
                )
        parts.append('      </table>')
        parts.append('    </div>')
    stats = collected.get("stats") or {}
    if stats:
        parts.append('    <div class="env-card">')
        parts.append('      <h3>Table stats (information_schema.tables)</h3>')
        parts.append('      <table class="kv-table">')
        parts.append(
            '        <thead><tr><th>Table</th><th>Rows</th><th>Data</th><th>Index</th></tr></thead>'
        )
        for name, row in stats.items():
            parts.append(
                '        <tr><th>{}</th><td>{:,}</td><td>{:,}</td><td>{:,}</td></tr>'.format(
                    xml_escape(str(name)),
                    int(row.get("table_rows") or 0),
                    int(row.get("data_length") or 0),
                    int(row.get("index_length") or 0),
                )
            )
        parts.append('      </table>')
        parts.append('    </div>')
    schema = collected.get("schema") or {}
    if schema:
        parts.append('    <div class="env-card">')
        parts.append('      <h3>Schema (SHOW CREATE TABLE)</h3>')
        parts.append('      <ul class="schema-list">')
        for name, p in schema.items():
            cols = len(p.get("columns") or [])
            idxs = ", ".join(
                (i.get("name") or "PRIMARY")
                for i in (p.get("indexes") or [])
            ) or "none"
            parts.append(
                '        <li><strong>{}</strong> — {} columns · indexes: {}</li>'.format(
                    xml_escape(str(name)), cols, xml_escape(idxs),
                )
            )
        parts.append('      </ul>')
        parts.append('    </div>')
    parts.append('  </div>')
    parts.append('</details>')
    return "\n".join(parts)


def _render_glossary_aside(sidecar):
    """Collect every glossary entry referenced by the visible text and
    emit them as a final ``<aside>`` block for keyboard/screen-reader users
    who can't hover the inline ``<abbr>`` tooltips.
    """
    referenced = set()
    # Scan every text field we'll render on the page.
    fields = [sidecar.get("executive_summary") or ""]
    for w in sidecar.get("warnings") or []:
        fields.append(w.get("text", ""))
    for s in sidecar.get("suggestions") or []:
        fields.append(s.get("action", ""))
        fields.append(s.get("why", ""))
    for sw in sidecar.get("optimizer_switches") or []:
        fields.append(sw.get("explanation", ""))
    for text in fields:
        for hit in find_terms_in_text(text):
            referenced.add(hit["key"])
    if not referenced:
        return ""
    parts = [
        '<aside class="glossary-aside" aria-labelledby="glossary-heading">',
        '  <details>',
        '    <summary><h2 id="glossary-heading">Glossary ({} terms)</h2></summary>'.format(
            len(referenced)
        ),
        '    <dl class="glossary-list">',
    ]
    for key in sorted(referenced):
        entry = glossary_lookup(key)
        if not entry:
            continue
        canonical = key.replace("_", " ")
        parts.append('      <div class="glossary-item">')
        parts.append('        <dt><code>{}</code></dt>'.format(xml_escape(canonical)))
        parts.append('        <dd class="newcomer">{}</dd>'.format(xml_escape(entry.get("newcomer", ""))))
        parts.append('        <dd class="technical">{}</dd>'.format(xml_escape(entry.get("technical", ""))))
        parts.append('      </div>')
    parts.append('    </dl>')
    parts.append('  </details>')
    parts.append('</aside>')
    return "\n".join(parts)


def _render_raw_sidecar(sidecar):
    """Collapsed ``<details>`` with the full sidecar JSON — the same content
    the JSON-LD script tag carries. Useful for DBAs who want to verify
    exactly what myflames saw, and trivially copied with Ctrl-A / Ctrl-C.
    """
    pretty = json.dumps(sidecar, indent=2, ensure_ascii=False, sort_keys=False)
    return (
        '<details class="raw-sidecar">\n'
        '  <summary><h2>Raw sidecar (JSON)</h2></summary>\n'
        '  <pre class="raw-json"><code>{}</code></pre>\n'
        '</details>'
    ).format(xml_escape(pretty))


# ---------------------------------------------------------------------------
# Top-level template
# ---------------------------------------------------------------------------

_CSS = r"""
:root {
  --bg: #f5f6fa;
  --card: #ffffff;
  --fg: #1a1a2e;
  --muted: #5a6475;
  --border: #e0e4ec;
  --accent: #283593;
  --accent-soft: #e8eaf6;

  /* Severity palette (from progressive-ux skill) */
  --sev-error: #b71c1c;
  --sev-warn: #ef6c00;
  --sev-info: #0d47a1;
  --sev-ok: #1b5e20;

  --sev-high: var(--sev-error);
  --sev-medium: var(--sev-warn);
  --sev-low: var(--sev-info);

  --code-bg: #263238;
  --code-fg: #eceff1;
  --code-str: #c3e88d;
  --code-kw: #82aaff;
}

* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: var(--bg); color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  line-height: 1.55;
  font-size: 14px;
}
.visually-hidden {
  position: absolute !important; width: 1px; height: 1px;
  padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0);
  white-space: nowrap; border: 0;
}
.skip-link {
  position: absolute; top: -40px; left: 0;
  background: var(--accent); color: #fff; padding: 8px 16px;
  z-index: 100; text-decoration: none;
}
.skip-link:focus { top: 0; }

/* Header */
.site-header {
  background: #1a1a2e; color: #fff;
  padding: 14px 24px;
  display: flex; align-items: center; justify-content: space-between;
  flex-wrap: wrap; gap: 12px;
}
.site-header h1 {
  font-size: 17px; font-weight: 600; margin: 0;
}
.site-header nav.toolbar {
  display: flex; gap: 8px; flex-wrap: wrap;
}
.site-header nav.toolbar button {
  padding: 6px 14px; border: 1px solid rgba(255,255,255,0.3);
  border-radius: 6px; background: rgba(255,255,255,0.06);
  color: #fff; font-size: 12px; cursor: pointer;
}
.site-header nav.toolbar button:hover { background: rgba(255,255,255,0.15); }
.site-header nav.toolbar button:focus { outline: 2px solid #fff; outline-offset: 2px; }

/* Main layout */
main.report {
  max-width: 1400px; margin: 24px auto; padding: 0 24px;
  display: flex; flex-direction: column; gap: 20px;
}
main.report > section,
main.report > details,
main.report > aside {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 10px; padding: 18px 22px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.04);
}
main.report h2 {
  font-size: 15px; font-weight: 600; color: var(--fg);
  margin: 0 0 12px 0; display: inline-flex; align-items: baseline; gap: 8px;
}
main.report h2 .count {
  color: var(--muted); font-weight: 400; font-size: 13px;
}

/* Exec summary strip */
.exec-summary .exec-text {
  font-size: 16px; font-weight: 500; color: var(--fg);
  margin: 0 0 14px 0;
}
.exec-summary .quick-stats {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px; margin: 0;
}
.exec-summary .quick-stats > div {
  background: var(--accent-soft); border-radius: 6px;
  padding: 10px 14px; text-align: left;
}
.exec-summary .quick-stats dt {
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.6px;
  color: var(--muted); margin-bottom: 2px;
}
.exec-summary .quick-stats dd {
  font-size: 18px; font-weight: 700; color: var(--accent); margin: 0;
}

/* Primary action card */
.primary-action {
  border-left: 4px solid var(--sev-medium);
  background: #fff9f3;
}
.primary-action.sev-high { border-left-color: var(--sev-high); background: #fff3f3; }
.primary-action.sev-low  { border-left-color: var(--sev-low);  background: #f1f8ff; }
.primary-action-header {
  display: flex; align-items: center; gap: 10px; margin-bottom: 8px;
}
.primary-action-header h2 { margin: 0; font-size: 15px; }
.primary-action .action-text {
  font-size: 15px; color: var(--fg); margin: 8px 0;
}
.primary-action .sql-action {
  background: var(--code-bg); color: var(--code-fg);
  padding: 12px 16px; border-radius: 6px; margin: 8px 0;
  font-family: "SFMono-Regular", Menlo, Monaco, Consolas, monospace;
  font-size: 13px; overflow-x: auto;
}
.primary-action details.why-details {
  margin-top: 10px; background: rgba(255,255,255,0.5);
  border-radius: 6px; padding: 8px 12px;
}
.primary-action details.why-details summary {
  cursor: pointer; font-weight: 600; font-size: 13px; color: var(--muted);
}
.primary-action details.why-details[open] summary { margin-bottom: 6px; }

/* Severity badges */
.badge {
  display: inline-block; padding: 2px 10px; border-radius: 12px;
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.6px; background: var(--muted); color: #fff;
}
.badge-high,  .badge-error { background: var(--sev-error); }
.badge-medium, .badge-warn { background: var(--sev-warn); }
.badge-low,   .badge-info  { background: var(--sev-info); }
.badge-ok                  { background: var(--sev-ok); }

/* Query card */
.query-card pre {
  background: var(--code-bg); color: var(--code-fg);
  padding: 14px 18px; border-radius: 8px;
  font-family: "SFMono-Regular", Menlo, Monaco, Consolas, monospace;
  font-size: 13px; line-height: 1.55;
  white-space: pre-wrap; word-break: break-word;
  overflow-x: auto; margin: 0;
}

/* Visualization */
.viz-card .chart-panel {
  overflow-x: auto; padding: 4px; background: #fff;
  border-radius: 6px; border: 1px solid var(--border);
}
.viz-card svg { display: block; max-width: 100%; height: auto; }

/* Warnings + suggestions lists */
.warning-list, .suggestion-list {
  list-style: none; padding: 0; margin: 0;
  display: flex; flex-direction: column; gap: 10px;
}
.warning, .suggestion {
  background: var(--bg); border-radius: 8px; padding: 12px 14px;
  border-left: 3px solid var(--muted);
  display: grid; grid-template-columns: auto 1fr; column-gap: 12px; row-gap: 6px;
}
.warning.sev-error, .suggestion.sev-high    { border-left-color: var(--sev-high); }
.warning.sev-warn,  .suggestion.sev-medium  { border-left-color: var(--sev-warn); }
.warning.sev-info,  .suggestion.sev-low     { border-left-color: var(--sev-info); }
.warning .text, .suggestion .action-text { margin: 0; grid-column: 2; }
.warning .where { grid-column: 2; font-size: 12px; color: var(--muted); margin: 0; }
.suggestion .sql-action {
  grid-column: 1 / -1;
  background: var(--code-bg); color: var(--code-fg);
  padding: 10px 14px; border-radius: 6px; margin: 0;
  font-family: "SFMono-Regular", Menlo, Monaco, Consolas, monospace;
  font-size: 12.5px; overflow-x: auto;
}
.suggestion details.why-details {
  grid-column: 1 / -1; margin-top: 4px; background: var(--accent-soft);
  border-radius: 6px; padding: 8px 12px; font-size: 13px;
}
.suggestion details.why-details summary {
  cursor: pointer; font-weight: 600; color: var(--accent);
}
.suggestion details.why-details p {
  margin: 6px 0 0 0; color: var(--fg);
}
.node-label {
  display: inline-block; padding: 1px 6px; border-radius: 4px;
  background: rgba(40,53,147,0.1); color: var(--accent);
  font-size: 11px;
}

/* Glossary chips (<abbr>) */
abbr.glossary-chip {
  text-decoration: underline dotted rgba(40,53,147,0.5);
  cursor: help; text-underline-offset: 2px;
}

/* Environment section */
details.env-section summary,
details.raw-sidecar summary,
aside.glossary-aside summary {
  cursor: pointer; list-style: none;
}
details.env-section summary::-webkit-details-marker,
details.raw-sidecar summary::-webkit-details-marker,
aside.glossary-aside summary::-webkit-details-marker { display: none; }
details.env-section summary::before,
details.raw-sidecar summary::before,
aside.glossary-aside summary::before {
  content: "▸"; display: inline-block; margin-right: 6px;
  transition: transform 0.15s; color: var(--muted);
}
details.env-section[open] summary::before,
details.raw-sidecar[open] summary::before,
aside.glossary-aside details[open] summary::before { transform: rotate(90deg); }
details.env-section summary h2,
details.raw-sidecar summary h2,
aside.glossary-aside summary h2 { display: inline; margin: 0; }
.env-grid {
  margin-top: 14px; display: grid; gap: 14px;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
}
.env-card { background: var(--bg); border-radius: 8px; padding: 12px 14px; }
.env-card h3 {
  font-size: 13px; margin: 0 0 8px 0; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.6px;
}
.kv-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.kv-table th {
  text-align: left; font-weight: 600; padding: 4px 6px;
  vertical-align: top; color: var(--muted);
}
.kv-table td {
  padding: 4px 6px; word-break: break-all;
  font-family: "SFMono-Regular", Menlo, monospace; font-size: 12px;
}
.schema-list { list-style: none; padding: 0; margin: 0; font-size: 13px; }
.schema-list li { padding: 3px 0; }

/* Glossary aside */
aside.glossary-aside { background: #f1f8ff; }
.glossary-list { margin: 12px 0 0 0; display: grid; gap: 12px; }
.glossary-item dt code {
  background: var(--accent-soft); padding: 2px 6px;
  border-radius: 4px; font-size: 12px; color: var(--accent);
}
.glossary-item dd { margin: 4px 0 0 0; font-size: 13px; }
.glossary-item dd.newcomer { color: var(--fg); }
.glossary-item dd.technical {
  color: var(--muted); font-size: 12px; font-style: italic;
}

/* Raw sidecar JSON */
.raw-json {
  background: var(--code-bg); color: var(--code-fg);
  padding: 12px 14px; border-radius: 6px; overflow-x: auto;
  font-family: "SFMono-Regular", monospace; font-size: 11.5px;
  max-height: 400px;
}

/* Print tweaks */
@media print {
  body { background: #fff; }
  .site-header { background: #fff; color: #000; border-bottom: 2px solid #000; }
  .site-header nav { display: none; }
  details { page-break-inside: avoid; }
  details > summary { display: none; }
  details > *:not(summary) { display: block !important; }
}

/* Mobile */
@media (max-width: 720px) {
  main.report { padding: 0 12px; }
  .site-header { padding: 10px 14px; }
  .site-header h1 { font-size: 15px; }
}
"""


_JS = r"""
function exportSVG() {
  var svg = document.querySelector('#chart-panel svg');
  if (!svg) { alert('No SVG found'); return; }
  var data = new XMLSerializer().serializeToString(svg);
  var blob = new Blob(['<?xml version="1.0" standalone="no"?>\n', data], {type: 'image/svg+xml'});
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'query-plan.svg';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(a.href);
}

function exportJSON() {
  // Export the embedded sidecar (same data the JSON-LD script carries).
  var script = document.querySelector('script[type="application/ld+json"]');
  if (!script) { alert('No sidecar found'); return; }
  var blob = new Blob([script.textContent], {type: 'application/json'});
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'query-analysis.json';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(a.href);
}
"""


def render_html_report(json_text, view_type="flamegraph", width=1200,
                       title="MySQL Query Plan", query_text="",
                       live_artifacts=None, **kwargs):
    """Produce a self-contained HTML report string for a parsed EXPLAIN plan.

    The return value is a single UTF-8 HTML document containing:

      * ``<head>``: ``<title>``, ``<meta name="description">`` with the
        executive summary, and a JSON-LD ``<script>`` carrying the full
        v1 sidecar so AI agents can read the report without HTML parsing.
      * ``<header>``: app title + toolbar (Export SVG / Export Analysis
        JSON / Print).
      * ``<main>``: executive summary strip, primary action card, query
        card, embedded visualization, warnings, suggestions, collected
        environment (collapsed), glossary (collapsed), and raw sidecar
        (collapsed).

    Parameters
    ----------
    live_artifacts : dict, optional
        When myflames ran in live-connection mode, the caller passes the
        ``{"schema", "stats", "variables"}`` dict produced by
        :func:`myflames.cli._run_live_explain`. We run the advisor on it
        so the HTML includes the Collected Environment section and the
        advisor-generated environment warnings / suggestions. When the
        caller doesn't supply it, the report is file-mode only.

    Existing integration tests assert on a handful of stable strings:
    ``<!DOCTYPE html>``, ``<svg``, the ``title`` param, ``Export SVG``,
    ``Export Analysis JSON``, ``Print``, ``Warnings``, ``Suggestions``.
    All of those are preserved here.
    """
    root = parse_explain(json_text)
    max_time = root["total_time"]
    use_microseconds = max_time > 0 and max_time < 1
    unit = "\u00b5s" if use_microseconds else "ms"

    analysis = analyze_plan(root)
    if query_text:
        analysis["query_text_lines"] = format_sql(query_text)

    # Feed live-mode collected data into the advisor so the HTML's
    # Environment section + environment-sourced warnings/suggestions
    # match what the standalone sidecar would show.
    if live_artifacts:
        from .advisor import advise
        advise(
            analysis,
            schema=live_artifacts.get("schema"),
            stats=live_artifacts.get("stats"),
            variables=live_artifacts.get("variables"),
        )

    svg_width = kwargs.get("width") or width
    if view_type == "flamegraph" and "width" not in kwargs:
        svg_width = 1800

    svg = _render_svg(root, view_type, svg_width, title, unit, **kwargs) or ""

    # Strip XML declaration / DOCTYPE from the SVG so it can be embedded
    # directly in HTML. Do NOT strip the <svg> element itself.
    svg_embed = _re.sub(r'^<\?xml[^?]*\?>\s*', '', svg)
    svg_embed = _re.sub(r'<!DOCTYPE[^>]*>\s*', '', svg_embed)

    beautified = "\n".join(format_sql(query_text)) if query_text else ""

    # Figure out engine metadata so the sidecar's source block is accurate.
    source_type = "live" if live_artifacts else "file"
    engine = None
    engine_version = None
    if live_artifacts:
        version_str = (live_artifacts.get("variables") or {}).get("version") or ""
        if "mariadb" in version_str.lower():
            engine = "mariadb"
        elif version_str:
            engine = "mysql"
        if version_str:
            engine_version = version_str

    # Build the sidecar from the same analysis the HTML will render.
    # This is the single source of truth for every warning, suggestion,
    # metric, and environment entry in the page.
    sidecar = build_sidecar(
        root, analysis,
        source_type=source_type,
        engine=engine,
        engine_version=engine_version,
        query_raw=query_text or None,
        query_beautified=beautified or None,
    )

    sections = [
        _render_exec_summary(sidecar),
        _render_primary_action(sidecar),
        _render_query_card(sidecar),
        _render_viz_card(svg_embed, view_type),
        _render_warnings(sidecar),
        _render_suggestions(sidecar),
        _render_environment(sidecar),
        _render_glossary_aside(sidecar),
        _render_raw_sidecar(sidecar),
    ]
    sections_html = "\n\n".join(s for s in sections if s)

    jsonld = _sanitize_for_jsonld(sidecar)
    description = sidecar.get("executive_summary", "")

    html = (
        '<!DOCTYPE html>\n'
        '<html lang="en">\n'
        '<head>\n'
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '  <title>{title_esc} — myflames report</title>\n'
        '  <meta name="description" content="{desc_esc}">\n'
        '  <script type="application/ld+json">\n{jsonld}\n  </script>\n'
        '  <style>{css}</style>\n'
        '</head>\n'
        '<body>\n'
        '  <a class="skip-link" href="#main-content">Skip to content</a>\n'
        '  <header class="site-header" role="banner">\n'
        '    <h1>{title_esc}</h1>\n'
        '    <nav class="toolbar" aria-label="Report actions">\n'
        '      <button type="button" onclick="exportSVG()">Export SVG</button>\n'
        '      <button type="button" onclick="exportJSON()">Export Analysis JSON</button>\n'
        '      <button type="button" onclick="window.print()">Print / PDF</button>\n'
        '    </nav>\n'
        '  </header>\n'
        '  <main id="main-content" class="report" role="main">\n'
        '{sections}\n'
        '  </main>\n'
        '  <script>{js}</script>\n'
        '</body>\n'
        '</html>\n'
    ).format(
        title_esc=xml_escape(title),
        desc_esc=xml_escape(description),
        jsonld=jsonld,
        css=_CSS,
        sections=sections_html,
        js=_JS,
    )
    return html
