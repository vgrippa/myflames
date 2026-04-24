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
import os as _os
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
from .teach import render_lesson
from .teach_hooks import build_teach_hooks, build_teach_index_maps
from .complexity_animation import render_complexity_animation_svg


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

    teach_index_by_folded = kwargs.get("teach_index_by_folded") or {}

    if view_type == "bargraph":
        return render_bargraph(
            root, width=width, title=title,
            unit_display=unit, total_time=root["total_time"],
            analysis=None,
            teach_index_by_folded=teach_index_by_folded,
        )
    if view_type == "treemap":
        return render_treemap(
            root, width=width, title=title, unit_display=unit, analysis=None,
            teach_index_by_folded=teach_index_by_folded,
        )
    if view_type == "diagram":
        return render_diagram(
            root, width=width, title=title, unit_display=unit, analysis=None,
            teach_index_by_folded=teach_index_by_folded,
        )
    if view_type == "tree":
        return render_tree(
            root, width=width, title=title, unit_display=unit, analysis=None,
            teach_index_by_folded=teach_index_by_folded,
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
    _complexity_by_folded = {}
    for _n in _flat(root):
        _c = (_n.get("details") or {}).get("complexity")
        if isinstance(_c, dict) and _c.get("big_o"):
            _complexity_by_folded.setdefault(_n.get("folded_label") or "", _c)
    svg = folded_to_svg(
        folded_text, title=title, width=width,
        height=kwargs.get("frame_height", 32),
        countname=unit,
        inverted=kwargs.get("inverted", False),
        colors=kwargs.get("colors", "hot"),
        teach_index_by_folded=teach_index_by_folded,
        complexity_by_folded=_complexity_by_folded or None,
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


#: Category-level fallback *why* strings. Used by :func:`_render_primary_action`
#: when the advisor emits a suggestion without a ``why`` field. This is Slice 4
#: U1: the "Fix first" card must always carry a rationale or the newcomer just
#: sees a bare imperative with no path to understand it.
_WHY_BY_CATEGORY = {
    "index": (
        "An index turns a full-table scan into a direct lookup — MySQL "
        "jumps straight to the matching rows instead of reading every row "
        "and discarding the ones that don't match. For joins, the benefit "
        "compounds: a nested loop over N outer rows that had to scan the "
        "inner table becomes N fast lookups."
    ),
    "tuning_variable": (
        "The knob being recommended controls how much memory a specific "
        "step of the plan gets to use. Too little memory forces the step "
        "to spill to disk, which is typically 10–100× slower than the "
        "in-memory version. Raising the variable lets the work stay in "
        "RAM."
    ),
    "optimizer_switch": (
        "optimizer_switch flags steer which execution strategies the "
        "planner is allowed to pick. Flipping one changes the *plan*, "
        "not the data — so you can try the change in a session and roll "
        "back instantly if it doesn't help."
    ),
    "engine": (
        "InnoDB is the storage engine every other piece of modern MySQL "
        "tuning assumes — row-level locking, the buffer pool, MVCC, "
        "transactions, and crash recovery are all InnoDB-only. Non-InnoDB "
        "engines make most of the advice in this report inapplicable."
    ),
    "durability": (
        "Durability settings control what you lose on a crash. The "
        "default (=1) fsyncs on every commit so no committed "
        "transaction is ever lost. Relaxed values trade durability for "
        "write throughput — only accept the trade if the data is "
        "genuinely reproducible."
    ),
    "rewrite": (
        "Rewriting the query changes the shape the optimizer sees. A "
        "non-sargable predicate (a function applied to a column in WHERE "
        "or ON) can't use an index; moving the function to the constant "
        "side lets the index apply."
    ),
}


def _why_fallback(suggestion):
    """Return a non-empty `why` for the primary-action card.

    Preference order: the advisor's explicit `why` → a category-keyed
    fallback → a generic last-resort line. This guarantees the
    "Why does this help?" details block is never empty (Slice 4 / U1).
    """
    why = (suggestion.get("why") or "").strip()
    if why:
        return why
    cat = (suggestion.get("category") or "").strip().lower()
    if cat in _WHY_BY_CATEGORY:
        return _WHY_BY_CATEGORY[cat]
    return (
        "This recommendation follows the pattern the advisor matched "
        "in your plan — expand the full suggestion below to see the "
        "detailed reasoning behind it."
    )


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
    # Slice 4 / U1: every primary action must carry a why — backstop
    # category-level suggestions where the advisor left it empty.
    why = _why_fallback(s)

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
        '  <div class="teach-cta-row">\n'
        '    <p id="teach-cta-hint" class="teach-cta-hint">Click a plan operator to inspect details.</p>\n'
        '    <button id="open-teach-btn" class="teach-cta-btn" type="button" hidden>Learn This Operator</button>\n'
        '  </div>\n'
        '  <div class="chart-panel" id="chart-panel">\n{}\n  </div>\n'
        '</section>'
    ).format(xml_escape(view_type), svg_embed)


def _render_teach_templates(sidecar):
    hooks = sidecar.get("teach_hooks") or []
    lessons = []
    seen = set()
    for hook in hooks:
        lesson = (hook.get("lesson") or "").strip()
        if not lesson or lesson in seen:
            continue
        seen.add(lesson)
        lessons.append(lesson)
    if not lessons:
        return ""
    parts = ['<section class="teach-assets" aria-hidden="true">']
    for lesson in lessons:
        try:
            html = render_lesson(lesson)
        except Exception:
            continue
        parts.append(
            '<template id="teach-tpl-{}" data-lesson="{}">{}</template>'.format(
                xml_escape(lesson), xml_escape(lesson), html
            )
        )
    parts.append("</section>")
    parts.append(
        '<dialog id="teach-dialog" class="teach-dialog" aria-modal="true" aria-labelledby="teach-dialog-title">'
        '<div class="teach-dialog-shell">'
        '<header class="teach-dialog-header">'
        '<div class="teach-dialog-titles">'
        '<h2 id="teach-dialog-title">Teach: Operator deep dive</h2>'
        '<p id="teach-dialog-subtitle"></p>'
        '</div>'
        '<button id="teach-dialog-close" type="button" aria-label="Close teach panel">Close</button>'
        '</header>'
        '<div class="teach-dialog-body">'
        '<iframe id="teach-dialog-frame" title="Teach lesson" sandbox="allow-scripts allow-same-origin"></iframe>'
        '</div>'
        '</div>'
        '</dialog>'
    )
    return "\n".join(parts)


def _build_complexity_lookup(sidecar):
    """Build a folded_label → complexity dict from the sidecar's
    ``operator_complexities`` array. Used by the teach-bridge JS to
    populate the complexity panel when a specific operator is opened."""
    out = {}
    for entry in sidecar.get("operator_complexities") or []:
        folded = (entry.get("folded_label") or "").strip()
        c = entry.get("complexity") or {}
        if folded and isinstance(c, dict) and c.get("big_o"):
            out.setdefault(folded, {
                "big_o": c.get("big_o", ""),
                "short": c.get("short", ""),
                "severity": c.get("severity", "medium"),
                "rationale": c.get("rationale", ""),
                "confidence": c.get("confidence", "exact"),
                "kind": _classify_for_js(c.get("big_o", "")),
            })
    return out


def _kind_to_placeholder_big_o(kind):
    """Placeholder formula used when pre-rendering a chart variant for a
    specific curve-kind — picks the one string that triggers the
    classifier inside ``render_complexity_animation_svg`` so the right
    curve is highlighted (and the corner badge is drawn with a sensible
    default text, though the real text is rewritten client-side)."""
    return {
        "const": "O(1)",
        "log":   "O(log n)",
        "linear":"O(n)",
        "nlogn": "O(n log n)",
        "quad":  "O(n²)",
        "exp":   "O(2ⁿ)",
    }.get(kind, "")


def _classify_for_js(big_o):
    """Return the curve-kind key (``'const'|'log'|'linear'|'nlogn'|'quad'|'exp'``)
    that matches ``big_o``, so the JS can toggle the right highlight class."""
    s = (big_o or "").lower()
    if "2" in s and ("^" in s or "ⁿ" in s or "2^" in s):
        return "exp"
    # order matters — check the more specific patterns first
    if "n²" in s or "n^2" in s or "n * n" in s or "n · m" in s or "n×m" in s:
        return "quad"
    if "n log n" in s or "n·log" in s or "n * log" in s or "nlog" in s or "n · log m" in s or "n log m" in s:
        return "nlogn"
    if "log n" in s or "log m" in s or "log k" in s:
        return "log"
    if "o(1)" in s:
        return "const"
    if "o(n" in s or "+ m" in s or "n+m" in s:
        return "linear"
    return ""


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

_DIR = _os.path.dirname(_os.path.abspath(__file__))

with open(_os.path.join(_DIR, "output_html_report.css"), encoding="utf-8") as _f:
    _CSS = _f.read()

with open(_os.path.join(_DIR, "output_html_report.js"), encoding="utf-8") as _f:
    _JS = _f.read()


def render_html_report(json_text, view_type="flamegraph", width=1200,
                       title="MySQL Query Plan", query_text="",
                       live_artifacts=None, alternate_json_href=None,
                       **kwargs):
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
    teach_hooks = build_teach_hooks(
        root,
        query_sql=query_text,
        variables=(live_artifacts or {}).get("variables") or analysis.get("collected_variables"),
        stats=(live_artifacts or {}).get("stats") or analysis.get("collected_stats") or {},
    )
    teach_maps = build_teach_index_maps(teach_hooks)

    svg_width = kwargs.get("width") or width
    if view_type == "flamegraph" and "width" not in kwargs:
        svg_width = 1800

    svg = _render_svg(
        root,
        view_type,
        svg_width,
        title,
        unit,
        teach_index_by_folded=teach_maps["by_folded_label"],
        **kwargs
    ) or ""

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
        teach_hooks=teach_hooks,
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
        _render_teach_templates(sidecar),
    ]
    sections_html = "\n\n".join(s for s in sections if s)

    # Slice 6 / S2: Real JSON-LD. Wrap the sidecar in a
    # schema.org-compatible envelope so crawlers and LLM retrieval
    # pipelines recognize the payload. ``@context`` / ``@type`` /
    # ``@id`` are the three fields that turn a ``<script
    # type="application/ld+json">`` from opaque JSON into actual
    # linked data.
    jsonld_payload = {
        "@context": "https://myflames.dev/ns/v1",
        "@type": "QueryPlanAnalysis",
        "@id": sidecar.get("$schema", ""),
    }
    jsonld_payload.update(sidecar)
    jsonld = _sanitize_for_jsonld(jsonld_payload)
    description = sidecar.get("executive_summary", "")
    teach_hooks_json = _sanitize_for_jsonld(sidecar.get("teach_hooks") or [])
    complexity_json = _sanitize_for_jsonld(_build_complexity_lookup(sidecar))
    # Pre-render one chart variant per curve-kind (plus a generic fallback).
    # The teach-bridge picks the right variant at runtime based on the
    # selected operator's classification and injects the section directly
    # into the lesson iframe's content as part of the normal flow.
    _variants = {
        "": render_complexity_animation_svg(complexity_dict=None, width=640, height=300),
    }
    for _k in ("const", "log", "linear", "nlogn", "quad", "exp"):
        _variants[_k] = render_complexity_animation_svg(
            complexity_dict={"big_o": _kind_to_placeholder_big_o(_k)},
            width=640, height=300,
        )
    complexity_charts_json = _sanitize_for_jsonld(_variants)

    # Slice 6 / S3: cross-link the machine-readable sibling when the
    # caller knows it. ``alternate_json_href`` is a relative path like
    # ``"./foo.json"`` that resolves next to the HTML file.
    if alternate_json_href:
        alternate_link = (
            '  <link rel="alternate" type="application/json" href="{}">\n'
            .format(xml_escape(alternate_json_href))
        )
    else:
        alternate_link = ""

    html = (
        '<!DOCTYPE html>\n'
        '<html lang="en">\n'
        '<head>\n'
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '  <title>{title_esc} — myflames report</title>\n'
        '  <meta name="description" content="{desc_esc}">\n'
        '{alternate_link}'
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
        '  <script>window.__MYFLAMES_TEACH_HOOKS = {teach_hooks_json};</script>\n'
        '  <script>window.__MYFLAMES_COMPLEXITY = {complexity_json};</script>\n'
        '  <script>window.__MYFLAMES_COMPLEXITY_CHARTS = {complexity_charts_json};</script>\n'
        '  <script>{js}</script>\n'
        '</body>\n'
        '</html>\n'
    ).format(
        title_esc=xml_escape(title),
        desc_esc=xml_escape(description),
        alternate_link=alternate_link,
        jsonld=jsonld,
        css=_CSS,
        sections=sections_html,
        teach_hooks_json=teach_hooks_json,
        complexity_json=complexity_json,
        complexity_charts_json=complexity_charts_json,
        js=_JS,
    )
    return html
