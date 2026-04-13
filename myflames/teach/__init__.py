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
    """Print the list of available lessons grouped by family."""
    print("myflames teach \u2014 interactive database algorithm lessons\n", file=stream)
    print("Usage: myflames teach <lesson> [-o out.html]\n", file=stream)
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
        help="Which lesson to render.",
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
