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
from typing import Callable, Dict

from .cache_family import LESSONS as CACHE_FAMILY_LESSONS
from .index_family import LESSONS as INDEX_FAMILY_LESSONS
from .join_family import LESSONS as JOIN_FAMILY_LESSONS
from .scan_family import LESSONS as SCAN_FAMILY_LESSONS

Lesson = Dict[str, object]

LESSON_FAMILIES: Dict[str, Dict[str, Lesson]] = {
    "join_family": JOIN_FAMILY_LESSONS,
    "index_family": INDEX_FAMILY_LESSONS,
    "scan_family": SCAN_FAMILY_LESSONS,
    "cache_family": CACHE_FAMILY_LESSONS,
}

# Map family keys to the output subdirectory name under docs/teach/.
FAMILY_DIRS = {
    "join_family": "join",
    "index_family": "index",
    "scan_family": "scan",
    "cache_family": "cache",
}

LESSONS: Dict[str, Lesson] = {}
for _family in ("join_family", "index_family", "scan_family", "cache_family"):
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
    "semijoin_weedout",
    "derived_table",   # scan_family → materialization
    "lru",             # cache_family
    "buffer_pool_warmup",  # cache_family: why cold queries hurt
]


def render_catalog_html(title: str = "myflames teach — algorithm catalog") -> str:
    """Render the full lesson catalog as a self-contained HTML index page.

    This is the "myteach" hub — the central entry-point every report
    links to. Groups lessons by family, shows the curriculum track at
    the top, and links to the individual self-contained lesson pages
    alongside this index (convention: ``teach/<lesson>.html``).

    Zero external dependencies — pure CSS + one <a> per lesson. The
    output is offline-first like every other myflames page.
    """
    track = [k for k in CURRICULUM if k in LESSONS]
    track_html = ""
    if track:
        items = []
        for i, k in enumerate(track):
            lesson = LESSONS.get(k) or {}
            title_text = lesson.get("title") or k
            items.append(
                '<li><span class="step">{n:02d}</span>'
                '<a href="./{slug}.html">{title}</a></li>'.format(
                    n=i + 1, slug=k, title=title_text
                )
            )
        track_html = (
            '<section class="curriculum" aria-labelledby="curr-heading">\n'
            '  <h2 id="curr-heading">Start here — recommended order</h2>\n'
            '  <ol class="curriculum-list">\n    '
            + '\n    '.join(items) +
            '\n  </ol>\n'
            '</section>\n'
        )

    family_sections = []
    for family_key in ("scan_family", "index_family", "join_family", "cache_family"):
        family_lessons = LESSON_FAMILIES.get(family_key) or {}
        if not family_lessons:
            continue
        rows = []
        for name, lesson in family_lessons.items():
            title_text = lesson.get("title") or name
            summary = lesson.get("summary") or ""
            rows.append(
                '<li><a href="./{slug}.html">'
                '<span class="lesson-title">{title}</span>'
                '<span class="lesson-summary">{summary}</span>'
                '</a></li>'.format(
                    slug=name, title=title_text, summary=summary,
                )
            )
        family_sections.append(
            '<section class="family">\n'
            '  <h2>{label}</h2>\n'
            '  <ul class="lesson-list">\n    {rows}\n  </ul>\n'
            '</section>'.format(
                label=_FAMILY_LABELS.get(family_key, family_key),
                rows='\n    '.join(rows),
            )
        )

    families_html = '\n\n'.join(family_sections)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <meta name="description" content="Catalog of interactive myflames teach lessons: join strategies, index access, scans, cache.">
  <style>
    :root {{
      --bg: #f5f6fa; --card: #fff; --fg: #111827;
      --muted: #4a5260; --accent: #1a73e8;
      --accent-soft: #e8eaf6; --border: #dbe4f0;
      --rad: 10px; --rad-sm: 6px;
      --font-ui: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; }}
    body {{
      background: var(--bg); color: var(--fg);
      font-family: var(--font-ui); line-height: 1.55; font-size: 15px;
    }}
    :focus-visible {{
      outline: 2px solid var(--accent); outline-offset: 2px;
      border-radius: var(--rad-sm);
    }}
    header.hub-header {{
      background: #1a1a2e; color: #fff;
      padding: 18px 28px;
    }}
    header.hub-header h1 {{
      font-size: 20px; font-weight: 600; margin: 0 0 4px 0;
    }}
    header.hub-header p {{
      margin: 0; opacity: 0.8; font-size: 13px;
    }}
    main {{
      max-width: 1100px; margin: 28px auto; padding: 0 24px;
      display: flex; flex-direction: column; gap: 24px;
    }}
    section.curriculum, section.family {{
      background: var(--card); border: 1px solid var(--border);
      border-radius: var(--rad); padding: 18px 22px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.04);
    }}
    section h2 {{
      margin: 0 0 12px 0; font-size: 17px; font-weight: 600;
    }}
    .curriculum-list, .lesson-list {{
      list-style: none; padding: 0; margin: 0;
      display: grid; gap: 8px;
    }}
    .curriculum-list {{
      grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    }}
    .curriculum-list li {{
      display: flex; align-items: center; gap: 10px;
      padding: 8px 12px; background: var(--accent-soft);
      border-radius: var(--rad-sm);
    }}
    .curriculum-list .step {{
      font-family: ui-monospace, Menlo, monospace;
      font-size: 11px; color: var(--muted);
      flex: 0 0 auto;
    }}
    .curriculum-list a {{
      color: var(--accent); text-decoration: none; font-weight: 500;
    }}
    .curriculum-list a:hover {{ text-decoration: underline; }}
    .lesson-list li {{
      padding: 0;
    }}
    .lesson-list a {{
      display: flex; flex-direction: column; gap: 4px;
      padding: 12px 14px; border: 1px solid var(--border);
      border-radius: var(--rad-sm);
      text-decoration: none; color: inherit;
      transition: background 0.12s, border-color 0.12s;
    }}
    .lesson-list a:hover {{
      background: var(--accent-soft); border-color: var(--accent);
    }}
    .lesson-title {{
      font-weight: 600; color: var(--accent);
    }}
    .lesson-summary {{
      color: var(--muted); font-size: 13px;
    }}
    footer {{
      max-width: 1100px; margin: 24px auto 48px;
      padding: 0 24px; color: var(--muted); font-size: 12px;
    }}
    footer a {{ color: var(--accent); }}
  </style>
</head>
<body>
  <header class="hub-header">
    <h1>myflames teach</h1>
    <p>Interactive database algorithm lessons — offline, stdlib-only,
       each file self-contained.</p>
  </header>
  <main>
    {track_html}
    {families_html}
  </main>
  <footer>
    Generated by <a href="https://github.com/vgrippa/myflames">myflames</a>.
    Lessons verified against MySQL 8.4 and MariaDB 11.4 source trees.
  </footer>
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
    "join_family": "Join Family",
    "index_family": "Index Access Family",
    "scan_family": "Scan / Sort / Temp Family",
    "cache_family": "Cache / Memory Family",
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

    for family_key in ("join_family", "index_family", "scan_family", "cache_family"):
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
    """Dispatch `myflames teach ...`. Called from cli.py."""
    # --index (or --catalog) emits the HTML hub rather than a single
    # lesson. Accept the flag without a positional lesson.
    if argv and argv[0] in ("--index", "--catalog"):
        # Emit the myteach catalog HTML — a centralized index of every
        # lesson with links. Canonical save location is
        # ``teach/index.html`` so the report's myteach section
        # deep-links work.
        idx_parser = argparse.ArgumentParser(
            prog="myflames teach --index",
            description="Emit the myteach catalog HTML.",
        )
        idx_parser.add_argument(
            "--output", "-o", default=None, metavar="PATH",
            help="Write the index HTML to PATH. If omitted, stdout.",
        )
        # Skip argv[0] (the --index flag itself).
        args = idx_parser.parse_args(argv[1:])
        html = render_catalog_html()
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(html)
        else:
            sys.stdout.write(html)
        sys.exit(0)

    if not argv:
        _print_catalog()
        sys.exit(0)

    parser = argparse.ArgumentParser(
        prog="myflames teach",
        description="Render an interactive database algorithm lesson as "
        "self-contained HTML.",
    )
    parser.add_argument(
        "lesson",
        choices=sorted(LESSONS.keys()),
        help="Which lesson to render. Pass --index to emit the catalog hub instead.",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        metavar="PATH",
        help="Write HTML to PATH. If omitted, writes to stdout.",
    )
    parser.add_argument(
        "--title",
        default=None,
        help="Override the lesson's title (optional).",
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        # argparse exits 2 on unknown choice — we want the catalog printed first
        if e.code == 2:
            _print_catalog(stream=sys.stderr)
        raise

    html = render_lesson(args.lesson)
    if args.title:
        html = html.replace(
            f"<title>{LESSONS[args.lesson]['title']}</title>",
            f"<title>{args.title}</title>",
            1,
        )

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(html)
    else:
        sys.stdout.write(html)
