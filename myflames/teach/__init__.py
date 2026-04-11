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

from . import btree_lookup, bnl, hash_join, join_compare, lru

Lesson = Dict[str, object]

LESSONS: Dict[str, Lesson] = {
    "btree": {
        "title": "B+tree lookup — how InnoDB finds a row",
        "summary": "Clustered primary key vs secondary-to-clustered; 16 KiB page fan-out.",
        "render": btree_lookup.render,
    },
    "bnl": {
        "title": "Block Nested Loop join — MariaDB's default for non-indexed joins",
        "summary": "Watch `join_buffer_size` decide how many times the inner table is rescanned.",
        "render": bnl.render,
    },
    "hash": {
        "title": "Hash join — build, probe, and grace-hash spill",
        "summary": "MySQL 8.4's default for non-indexed equi-joins; animated build + probe phases.",
        "render": hash_join.render,
    },
    "join": {
        "title": "BNL vs hash join — side by side",
        "summary": "MariaDB BNL (default) vs MySQL 8.4 hash join; move the sliders and feel the asymptotic difference.",
        "render": join_compare.render,
    },
    "lru": {
        "title": "InnoDB buffer pool — midpoint-insertion LRU",
        "summary": "Why MySQL's LRU is scan-resistant — young/old sublists, innodb_old_blocks_time.",
        "render": lru.render,
    },
}


def render_lesson(name: str) -> str:
    """Render lesson *name* to a complete HTML document string."""
    if name not in LESSONS:
        raise KeyError(
            f"unknown lesson {name!r}; available: {', '.join(sorted(LESSONS))}"
        )
    render_fn = LESSONS[name]["render"]
    assert callable(render_fn)
    return render_fn()


def _print_catalog(stream=sys.stdout) -> None:
    """Print the list of available lessons (used by `myflames teach` bare)."""
    print("myflames teach — interactive database algorithm lessons\n", file=stream)
    print("Usage: myflames teach <lesson> [-o out.html]\n", file=stream)
    print("Lessons:", file=stream)
    for name, lesson in LESSONS.items():
        print(f"  {name:<8} {lesson['summary']}", file=stream)
    print(
        "\nEvery lesson is a self-contained HTML page with in-page sliders —\n"
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
