"""myflames teach — interactive database algorithm lessons.

Self-contained HTML files that animate database algorithms with correct
MySQL 8.4 and MariaDB 11.x cost models. Every lesson is a single HTML
page with in-page sliders; no CLI parameters, no re-running.

Public surface::

    from myflames.teach import render_lesson, cmd_teach, LESSONS

    html = render_lesson("btree")
    # → complete HTML document string
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable, Dict, List

from . import _html

from .cache_family import LESSONS as CACHE_FAMILY_LESSONS
from .index_family import LESSONS as INDEX_FAMILY_LESSONS
from .join_family import LESSONS as JOIN_FAMILY_LESSONS
from .planner_family import LESSONS as PLANNER_FAMILY_LESSONS
from .scan_family import LESSONS as SCAN_FAMILY_LESSONS

Lesson = Dict[str, object]

LESSON_FAMILIES: Dict[str, Dict[str, Lesson]] = {
    "join_family": JOIN_FAMILY_LESSONS,
    "index_family": INDEX_FAMILY_LESSONS,
    "scan_family": SCAN_FAMILY_LESSONS,
    "cache_family": CACHE_FAMILY_LESSONS,
    "planner_family": PLANNER_FAMILY_LESSONS,
}

# Map family keys to the output subdirectory name under docs/teach/.
FAMILY_DIRS = {
    "join_family": "join",
    "index_family": "index",
    "scan_family": "scan",
    "cache_family": "cache",
    "planner_family": "planner",
}

LESSONS: Dict[str, Lesson] = {}
for _family in (
    "join_family", "index_family", "scan_family", "cache_family", "planner_family",
):
    for _name, _lesson in LESSON_FAMILIES[_family].items():
        _lesson["family"] = _family
    LESSONS.update(LESSON_FAMILIES[_family])


# Slice 4 / T1: curriculum order. A first-time reader following this
# sequence will encounter each concept in the order the earlier lessons
# build up — full_scan first (why indexes exist), then B-tree lookup
# (what an index is), then the join family (how lookups compose into
# plans), then scan-avoidance (covering / skip / range), then cache
# layer. Junior-developer-first per the teaching skill.
#
# Only lesson keys that actually exist under myflames/teach/*_family/
# are in the list; renderers filter against LESSONS so a missing key
# degrades to "curriculum skips this step" rather than 404.
CURRICULUM = [
    "full_scan",       # scan_family
    "btree",           # index_family — flagship
    "unique_lookup",   # index_family
    "non_unique_lookup",
    "icp",
    "covering_index",  # scan_family: when index avoids the clustered lookup
    "nested_loop",     # join_family
    "hash",
    "bnl",
    "bka_join",
    "semijoin_firstmatch",  # join_family — semijoin cluster flagship
    "semijoin_weedout",
    "derived_table",   # scan_family → materialization
    "join_order",      # planner_family: how the planner *chose* the order above
    "lru",             # cache_family
    "buffer_pool_warmup",  # cache_family: why cold queries hurt
]


def render_catalog_html(title: str = "myflames teach: algorithm catalog") -> str:
    """Render the full lesson catalog as a self-contained HTML index page.

    This is the "myteach" hub — the central entry-point every report
    links to. Groups lessons by family, shows the curriculum track at
    the top, and links to the individual self-contained lesson pages
    alongside this index (convention: ``teach/<lesson>.html``).

    Zero external dependencies — pure CSS + one <a> per lesson. The
    output is offline-first like every other myflames page.
    """
    def _href_for(lesson_key: str) -> str:
        """Return the path from teach/index.html to the lesson HTML.

        Lessons are emitted into family subdirectories (docs/teach/join/,
        /index/, /scan/, /cache/, /planner/) by the build scripts, so
        the catalog has to include the family directory in its hrefs.
        """
        lesson = LESSONS.get(lesson_key) or {}
        family = lesson.get("family")
        subdir = FAMILY_DIRS.get(family) if family else None
        if subdir:
            return f"./{subdir}/{lesson_key}.html"
        return f"./{lesson_key}.html"

    track = [k for k in CURRICULUM if k in LESSONS]
    track_html = ""
    if track:
        items = []
        for i, k in enumerate(track):
            lesson = LESSONS.get(k) or {}
            title_text = lesson.get("title") or k
            items.append(
                '<li><a class="lesson-row" href="{href}"><span class="step">{n:02d}</span>'
                '<span>{title}</span></a></li>'.format(
                    n=i + 1, href=_html.esc(_href_for(k)), title=_html.esc(title_text)
                )
            )
        track_html = (
            '<section class="curriculum" aria-labelledby="curr-heading">\n'
            '  <h2 id="curr-heading">Start here: recommended order</h2>\n'
            '  <ol class="curriculum-list">\n    '
            + '\n    '.join(items) +
            '\n  </ol>\n'
            '</section>\n'
        )

    family_sections = []
    for family_key in (
        "scan_family", "index_family", "join_family", "cache_family", "planner_family",
    ):
        family_lessons = LESSON_FAMILIES.get(family_key) or {}
        if not family_lessons:
            continue
        rows = []
        for name, lesson in family_lessons.items():
            title_text = lesson.get("title") or name
            summary = lesson.get("summary") or ""
            rows.append(
                '<li data-family="{family}"><a class="lesson-row" href="{href}">'
                '<span class="lesson-title">{title}</span>'
                '<span class="lesson-summary">{summary}</span>'
                '</a></li>'.format(
                    href=_html.esc(_href_for(name)), title=_html.esc(title_text),
                    summary=_html.esc(summary), family=_html.esc(family_key),
                )
            )
        family_sections.append(
            '<section class="family" data-family="{family}">\n'
            '  <h2>{label}<span class="family-count">{count}</span></h2>\n'
            '  <ul class="lesson-list">\n    {rows}\n  </ul>\n'
            '</section>'.format(
                label=_html.esc(_FAMILY_LABELS.get(family_key, family_key)),
                family=_html.esc(family_key), count=len(family_lessons),
                rows='\n    '.join(rows),
            )
        )

    families_html = '\n\n'.join(family_sections)
    family_options = '<option value="">All families</option>' + ''.join(
        '<option value="{key}">{label}</option>'.format(
            key=_html.esc(key), label=_html.esc(_FAMILY_LABELS.get(key, key))
        ) for key in LESSON_FAMILIES
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_html.esc(title)}</title>
  <meta name="description" content="Catalog of interactive myflames teach lessons: join strategies, index access, scans, cache.">
  <style>
    {_html._THEME_CSS}
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; }}
    body {{ background: var(--bg); color: var(--ink); font: var(--text-md)/1.5 var(--font-sans); }}
    :focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
    [hidden] {{ display: none !important; }}
    .hub-header {{ background: var(--surface-muted); border-bottom: 1px solid var(--line); padding: 10px 24px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; }}
    .hub-header h1 {{ font-size: var(--text-lg); font-weight: 600; margin: 0; }}
    .hub-header p {{ margin: 0; font-size: var(--text-sm); color: var(--ink-muted); }}
    .catalog-filters {{ display: flex; flex-wrap: wrap; gap: 8px; min-width: 0; }}
    input, select, button {{ font: inherit; font-size: var(--text-sm); min-height: var(--control-h); padding: 3px 8px; border: 1px solid var(--control-line); border-radius: var(--radius-sm); color: var(--ink); background: var(--surface); min-width: 0; max-width: 100%; }}
    button {{ cursor: pointer; }}
    select {{ appearance: none; padding-right: 24px; background-image: linear-gradient(45deg, transparent 50%, var(--ink-muted) 50%), linear-gradient(135deg, var(--ink-muted) 50%, transparent 50%); background-position: calc(100% - 12px) 50%, calc(100% - 8px) 50%; background-size: 4px 4px; background-repeat: no-repeat; }}
    main {{ max-width: 1120px; margin: 14px auto 28px; padding: 0 20px; display: flex; flex-direction: column; gap: 14px; }}
    section.curriculum, section.family {{ background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius-md); overflow: hidden; }}
    section h2 {{ margin: 0; padding: 7px 12px; background: var(--surface-muted); border-bottom: 1px solid var(--line); color: var(--ink-secondary); font-size: var(--text-sm); font-weight: 600; display: flex; align-items: center; justify-content: space-between; gap: 8px; }}
    .family-count {{ color: var(--ink-muted); font-variant-numeric: tabular-nums; }}
    .curriculum-list, .lesson-list {{ list-style: none; padding: 0; margin: 0; }}
    .lesson-row {{ display: flex; align-items: baseline; justify-content: space-between; gap: 16px; padding: 8px 12px; border-bottom: 1px solid var(--line-subtle); text-decoration: none; color: var(--ink); }}
    li:last-child > .lesson-row {{ border-bottom: 0; }}
    .lesson-row:hover {{ background: var(--surface-subtle); }}
    .lesson-title {{ flex: 1; font-weight: 500; }}
    .lesson-summary {{ flex: 1; color: var(--ink-muted); font-size: var(--text-sm); }}
    .curriculum-list .lesson-row {{ justify-content: flex-start; gap: 12px; }}
    .step {{ flex: 0 0 auto; color: var(--ink-muted); font: var(--text-xs) var(--font-mono); }}
    footer {{ max-width: 1120px; margin: 24px auto; padding: 0 20px; color: var(--ink-muted); font-size: var(--text-sm); }}
    footer a {{ color: var(--accent); }}
    @media (max-width: 650px) {{
      :root {{ --control-h: 32px; }}
      .hub-header {{ padding: 12px; }}
      .catalog-filters, .catalog-filters > * {{ width: 100%; }}
      main {{ padding: 0 12px; }}
      .lesson-list .lesson-row {{ flex-direction: column; gap: 2px; }}
    }}
  </style>
</head>
<body>
  <header class="hub-header">
    <div>
      <h1>myflames · Learn</h1>
      <p>Interactive MySQL 8.4 &amp; MariaDB 10.11+ execution lessons</p>
    </div>
    <div class="catalog-filters">
      <input type="search" id="catalog-search" aria-label="Search lessons" placeholder="Filter lessons…">
      <select id="catalog-family" aria-label="Lesson family">{family_options}</select>
    </div>
  </header>
  <main>
    {track_html}
    {families_html}
    <p id="catalog-empty" hidden>No lessons match. <button id="catalog-clear" type="button">Clear filters</button></p>
    <p id="catalog-status" role="status" aria-live="polite" hidden></p>
  </main>
  <footer>
    Generated by <a href="https://github.com/vgrippa/myflames">myflames</a>.
    Lessons verified against MySQL 8.4 and MariaDB 11.4 source trees.
  </footer>
  <script>{_html._THEME_JS}
  (function() {{
    var search = document.getElementById("catalog-search");
    var family = document.getElementById("catalog-family");
    var groups = document.querySelectorAll("section.family");
    function filterLessons() {{
      var query = search.value.trim().toLowerCase();
      var count = 0;
      groups.forEach(function(group) {{
        var matches = 0;
        group.querySelectorAll("li").forEach(function(row) {{
          var visible = (!family.value || row.dataset.family === family.value) &&
            row.textContent.toLowerCase().indexOf(query) !== -1;
          row.hidden = !visible;
          if (visible) matches++;
        }});
        group.hidden = matches === 0;
        group.querySelector(".family-count").textContent = matches;
        count += matches;
      }});
      var track = document.querySelector(".curriculum");
      if (track) track.hidden = !!query || !!family.value;
      document.getElementById("catalog-empty").hidden = count !== 0;
      var status = document.getElementById("catalog-status");
      status.textContent = count + (count === 1 ? " lesson" : " lessons");
      status.hidden = !query && !family.value;
    }}
    search.addEventListener("input", filterLessons);
    family.addEventListener("change", filterLessons);
    document.getElementById("catalog-clear").addEventListener("click", function() {{
      search.value = "";
      family.value = "";
      filterLessons();
      search.focus();
    }});
  }})();
  </script>
</body>
</html>
"""


def curriculum_neighbors(lesson_key):
    """Return ``(prev_key, next_key)`` for T1's Prev/Next footer.

    Skips curriculum entries whose lessons don't exist yet. Returns
    ``(None, None)`` for unknown lessons so callers can choose not to
    render a nav bar for out-of-curriculum lessons.
    """
    if lesson_key not in LESSONS:
        return (None, None)
    track = [k for k in CURRICULUM if k in LESSONS]
    if lesson_key not in track:
        return (None, None)
    i = track.index(lesson_key)
    prev_key = track[i - 1] if i > 0 else None
    next_key = track[i + 1] if i + 1 < len(track) else None
    return (prev_key, next_key)


def render_lesson(name: str) -> str:
    """Render lesson *name* to a complete HTML document string."""
    if name not in LESSONS:
        raise KeyError(
            f"unknown lesson {name!r}; available: {', '.join(sorted(LESSONS))}"
        )
    render_fn = LESSONS[name]["render"]
    assert callable(render_fn)
    return render_fn()


_FAMILY_LABELS = {
    "join_family": "Join family",
    "index_family": "Index access family",
    "scan_family": "Scan, sort and temp family",
    "cache_family": "Cache and memory family",
    "planner_family": "Planner and optimizer family",
}


def _print_catalog(stream=sys.stdout) -> None:
    """Print the list of available lessons grouped by family.

    Also emits the curriculum (T1) at the top so first-time readers
    get a "start here" path instead of dict-insertion order.
    """
    print("myflames teach \u2014 interactive database algorithm lessons\n", file=stream)
    print("Usage: myflames teach <lesson> [-o out.html]\n", file=stream)

    track = [k for k in CURRICULUM if k in LESSONS]
    if track:
        print("  Start here (recommended order):", file=stream)
        print("    " + " → ".join(track), file=stream)
        print("", file=stream)

    for family_key in (
        "join_family", "index_family", "scan_family", "cache_family", "planner_family",
    ):
        family_lessons = LESSON_FAMILIES[family_key]
        if not family_lessons:
            continue
        print(f"  {_FAMILY_LABELS[family_key]}:", file=stream)
        for name, lesson in family_lessons.items():
            print(f"    {name:<22} {lesson['summary']}", file=stream)
        print("", file=stream)
    print(
        "Every lesson is a self-contained HTML page with in-page sliders \u2014\n"
        "no CLI parameters required.",
        file=stream,
    )


def cmd_teach(argv) -> None:
    """Dispatch `myflames teach ...` with the shared artifact-output contract."""
    import io
    from ..cli import _add_quiet_flag, _write_output
    parser = argparse.ArgumentParser(
        prog="myflames teach",
        description="Render an interactive database algorithm lesson as self-contained HTML.",
    )
    parser.add_argument("lesson", nargs="?", choices=sorted(LESSONS.keys()),
                        help="Which lesson to render. Pass --index for the catalog hub.")
    parser.add_argument("--index", "--catalog", action="store_true", help="Emit the myteach catalog HTML.")
    parser.add_argument("--output", "-o", default=None, metavar="PATH",
                        help="Write HTML to PATH. If omitted, writes to stdout.")
    parser.add_argument("--title", default=None, help="Override the lesson's title (optional).")
    _add_quiet_flag(parser)
    try:
        args = parser.parse_args(argv)
        if args.index and args.lesson:
            parser.error("choose a lesson or --index, not both")
    except SystemExit as e:
        if e.code == 2:
            _print_catalog(stream=sys.stderr)
        raise
    if args.index:
        _write_output(render_catalog_html(), args.output, quiet=args.quiet)
        return
    if args.lesson is None:
        catalog = io.StringIO()
        _print_catalog(stream=catalog)
        _write_output(catalog.getvalue(), args.output, quiet=args.quiet)
        return
    html = render_lesson(args.lesson)
    if args.title:
        html = html.replace(
            f"<title>{LESSONS[args.lesson]['title']}</title>",
            f"<title>{args.title}</title>", 1,
        )
    _write_output(html, args.output, quiet=args.quiet)
