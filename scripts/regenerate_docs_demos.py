#!/usr/bin/env python3
"""Regenerate docs/demos from a backup of the previous tree.

Typical workflow (run from repository root)::

  cp -a docs/demos /tmp/demos_bak
  rm -f docs/demos/*
  python3 scripts/regenerate_docs_demos.py /tmp/demos_bak

File-backed HTML reports and SVGs are rebuilt with ``python3 -m myflames``
using ``fixture_path`` from each backup ``*.json`` sidecar.

* ``live-*`` demos have no fixture path in the sidecar; those files are
  copied verbatim from the backup (re-capture requires a live server).

* Four short-name analysis SVGs (e.g. ``mysql-query-analysis-icp.svg``) are
  rebuilt from known fixtures (see ``_EXTRA_SVGS``).

* ``mysql-query-compare.html`` is rebuilt via ``myflames compare``.

* ``mysql-query-report.html`` is a flamegraph HTML report for the complex
  join fixture.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

_REPO = Path(__file__).resolve().parents[1]

_SUFFIX_TYPES = (
    ("-flamegraph", "flamegraph"),
    ("-bargraph", "bargraph"),
    ("-treemap", "treemap"),
    ("-diagram", "diagram"),
    ("-tree", "tree"),
)

# Short SVG names that duplicate the *-flamegraph.svg chart for the same plan.
_EXTRA_SVGS = (
    ("mysql-query-analysis-derived-sort", "flamegraph", "test/fixtures/explain-052-derived-table-top-spenders.json"),
    ("mysql-query-analysis-full-scan", "flamegraph", "test/fixtures/explain-001-table-scan-users-no-filter.json"),
    ("mysql-query-analysis-hash-join", "flamegraph", "test/mysql-explain-hash-join.json"),
    ("mysql-query-analysis-icp", "flamegraph", "test/fixtures/explain-008-index-scan-users-by-country.json"),
)

# Standalone SVGs embedded from docs/index.html (object data=…).
_INDEX_COMPLEX_SVGS = (
    ("mysql-query-complex-flamegraph", "flamegraph"),
    ("mysql-query-complex-bargraph", "bargraph"),
    ("mysql-query-complex-treemap", "treemap"),
    ("mysql-query-complex-diagram", "diagram"),
    ("mysql-query-complex-tree", "tree"),
)
_COMPLEX_JOIN_FIXTURE = "test/mysql-explain-complex-join.json"


def _infer_view_type(base: str) -> Optional[str]:
    for suf, typ in _SUFFIX_TYPES:
        if base.endswith(suf):
            return typ
    return None


def _infer_subdir(name: str) -> str:
    """Map a demo filename (stem) to its subdirectory."""
    if name.startswith("live-mariadb-"):
        return "live-mariadb"
    if name.startswith("live-mysql-"):
        return "live-mysql"
    if name.startswith("mariadb-") and "-optsw-" in name:
        return "mariadb-optsw"
    if "-optsw-" in name:
        return "mysql-optsw"
    if name.startswith("mysql-query-analysis-"):
        return "mysql-analysis"
    if name.startswith("mysql-query-complex-"):
        return "mysql-complex"
    return "mysql-basic"


def _run_myflames(view: str, out: Path, fixture: Path) -> None:
    cmd = [
        sys.executable,
        "-m",
        "myflames",
        "--type",
        view,
        "--output",
        str(out),
        str(fixture),
    ]
    r = subprocess.run(cmd, cwd=str(_REPO))
    if r.returncode != 0:
        raise SystemExit("command failed: %s" % " ".join(cmd))


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: regenerate_docs_demos.py BACKUP_DEMOS_DIR", file=sys.stderr)
        raise SystemExit(2)
    bak = Path(sys.argv[1]).resolve()
    if not bak.is_dir():
        print("not a directory: %s" % bak, file=sys.stderr)
        raise SystemExit(2)

    out_dir = _REPO / "docs" / "demos"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Create subdirectories for organized output
    for sub in ("mysql-basic", "mysql-analysis", "mysql-optsw",
                "mysql-complex", "mariadb-optsw", "live-mysql", "live-mariadb"):
        (out_dir / sub).mkdir(parents=True, exist_ok=True)

    for jpath in sorted(bak.glob("**/*.json")):
        base = jpath.stem
        if base == "mysql-query-compare":
            continue
        if base == "mysql-query-report":
            continue
        view = _infer_view_type(base)
        if view is None:
            continue
        try:
            with jpath.open(encoding="utf-8") as f:
                meta = json.load(f)
        except Exception as e:
            print("skip %s: %s" % (jpath.name, e), file=sys.stderr)
            continue
        src = (meta.get("source") or {})
        fp = src.get("fixture_path")
        if not fp:
            continue
        fixture = _REPO / fp
        if not fixture.is_file():
            print("missing fixture %s for %s" % (fp, base), file=sys.stderr)
            raise SystemExit(1)
        suffix = ".html" if (bak / (base + ".html")).is_file() or (bak / _infer_subdir(base) / (base + ".html")).is_file() else ".svg"
        subdir = _infer_subdir(base)
        dest = out_dir / subdir / (base + suffix)
        dest.parent.mkdir(parents=True, exist_ok=True)
        print("regen", base, "->", subdir + "/" + dest.name)
        _run_myflames(view, dest, fixture)

    # Full HTML report (flamegraph) — filename has no view suffix.
    report_json = bak / "mysql-basic" / "mysql-query-report.json"
    if not report_json.is_file():
        report_json = bak / "mysql-query-report.json"
    if report_json.is_file():
        with report_json.open(encoding="utf-8") as f:
            rmeta = json.load(f)
        fp = (rmeta.get("source") or {}).get("fixture_path")
        if fp:
            fixture = _REPO / fp
            dest = out_dir / "mysql-basic" / "mysql-query-report.html"
            print("regen mysql-basic/mysql-query-report.html")
            _run_myflames("flamegraph", dest, fixture)

    # Before/after compare page (no JSON sidecar in backup).
    compare_exists = (bak / "mysql-basic" / "mysql-query-compare.html").is_file() or (bak / "mysql-query-compare.html").is_file()
    if compare_exists:
        print("regen mysql-basic/mysql-query-compare.html")
        simple = _REPO / "test" / "mysql-explain-json-sample.json"
        after = _REPO / "test" / "mysql-explain-hash-join.json"
        cmd = [
            sys.executable,
            "-m",
            "myflames",
            "compare",
            str(simple),
            str(after),
            "--title",
            "Before vs After — Adding an index — myflames",
            "--output",
            str(out_dir / "mysql-basic" / "mysql-query-compare.html"),
        ]
        r = subprocess.run(cmd, cwd=str(_REPO))
        if r.returncode != 0:
            raise SystemExit("compare failed")

    for base, view, rel_fp in _EXTRA_SVGS:
        fixture = _REPO / rel_fp
        if not fixture.is_file():
            print("missing %s for %s" % (rel_fp, base), file=sys.stderr)
            raise SystemExit(1)
        subdir = _infer_subdir(base)
        dest = out_dir / subdir / (base + ".svg")
        dest.parent.mkdir(parents=True, exist_ok=True)
        print("regen", subdir + "/" + dest.name)
        _run_myflames(view, dest, fixture)

    cjf = _REPO / _COMPLEX_JOIN_FIXTURE
    if not cjf.is_file():
        print("missing %s for index SVGs" % _COMPLEX_JOIN_FIXTURE, file=sys.stderr)
        raise SystemExit(1)
    for base, view in _INDEX_COMPLEX_SVGS:
        dest = out_dir / "mysql-complex" / (base + ".svg")
        print("regen mysql-complex/" + dest.name, "(docs/index.html)")
        _run_myflames(view, dest, cjf)

    # Live captures: restore HTML + SVG + JSON from backup.
    for path in sorted(list(bak.glob("live-*")) + list(bak.glob("live-*/*"))):
        if path.is_dir():
            continue
        name = path.name
        subdir = _infer_subdir(name)
        dest = out_dir / subdir / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        print("restore", subdir + "/" + name)

    print("done.")


if __name__ == "__main__":
    main()
