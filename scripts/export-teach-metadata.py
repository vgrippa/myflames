#!/usr/bin/env python3
"""Export myflames teach LESSONS metadata to algorithms.json.

The Python LESSONS dict stores `title`/`summary` but not Big-O metadata,
so this script enriches the export with hand-curated complexity info
keyed by lesson name. The complexity entries here are the canonical
source of truth for the Compare view's log-log curves.

Run from the repo root:
    python3 scripts/export-teach-metadata.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from myflames.teach import (  # noqa: E402
    CURRICULUM,
    FAMILY_DIRS,
    LESSON_FAMILIES,
    LESSONS,
)

OUT_PATH = REPO_ROOT / "apps" / "teach-explorer" / "src" / "data" / "algorithms.json"

FAMILY_LABEL = {
    "scan_family": "Scan / Sort / Temp",
    "index_family": "Index Access",
    "join_family": "Join",
    "cache_family": "Cache / Memory",
    "planner_family": "Planner / Optimizer",
}

FAMILY_ACCENT = {
    "scan_family": "#ef4444",
    "index_family": "#2563eb",
    "join_family": "#a855f7",
    "cache_family": "#f59e0b",
    "planner_family": "#ec4899",
}

# Complexity + curve metadata per lesson. `curveKind` maps to a function
# in src/data/complexity.ts. Add new entries here when a lesson lands.
COMPLEXITY = {
    "full_scan": {
        "best": "O(n)", "avg": "O(n)", "worst": "O(n)",
        "curveKind": "linear",
        "tags": ["scan", "sequential-io"],
    },
    "filter": {
        "best": "O(n)", "avg": "O(n)", "worst": "O(n)",
        "curveKind": "linear",
        "tags": ["scan", "predicate"],
    },
    "filesort": {
        "best": "O(n)", "avg": "O(n log n)", "worst": "O(n log n)",
        "curveKind": "nlogn",
        "tags": ["sort", "memory-bound"],
    },
    "tmp": {
        "best": "O(n)", "avg": "O(n)", "worst": "O(n log n)",
        "curveKind": "linear",
        "tags": ["materialization", "memory-bound"],
    },
    "covering_index": {
        "best": "O(log n)", "avg": "O(log n + k)", "worst": "O(log n + k)",
        "curveKind": "log",
        "tags": ["index", "covered"],
    },
    "derived_table": {
        "best": "O(n)", "avg": "O(n + m)", "worst": "O(n*m)",
        "curveKind": "linear_sum",
        "tags": ["materialization", "subquery"],
    },
    "btree": {
        "best": "O(log n)", "avg": "O(log n)", "worst": "O(log n)",
        "curveKind": "log",
        "tags": ["index", "btree", "primary-key"],
    },
    "unique_lookup": {
        "best": "O(1)", "avg": "O(log n)", "worst": "O(log n)",
        "curveKind": "log",
        "tags": ["index", "unique", "eq_ref"],
    },
    "non_unique_lookup": {
        "best": "O(log n)", "avg": "O(log n + k)", "worst": "O(n)",
        "curveKind": "two_log",
        "tags": ["index", "range"],
    },
    "icp": {
        "best": "O(log n)", "avg": "O(log n + k)", "worst": "O(n)",
        "curveKind": "log",
        "tags": ["index", "pushdown"],
    },
    "index_merge": {
        "best": "O(log n)", "avg": "O(k1 + k2)", "worst": "O(n)",
        "curveKind": "linear_sum",
        "tags": ["index", "merge"],
    },
    "skip_scan": {
        "best": "O(d * log n)", "avg": "O(d * log n)", "worst": "O(n)",
        "curveKind": "sqrt_n",
        "tags": ["index", "low-cardinality"],
    },
    "rowid_filter": {
        "best": "O(k)", "avg": "O(k + f*log n)", "worst": "O(n)",
        "curveKind": "linear",
        "tags": ["mariadb", "filter", "bitmap"],
    },
    "nested_loop": {
        "best": "O(n * log m)", "avg": "O(n * log m)", "worst": "O(n * m)",
        "curveKind": "nested_indexed",
        "tags": ["join", "indexed"],
    },
    "bnl": {
        "best": "O(n + m)", "avg": "O(n * m / buf)", "worst": "O(n * m)",
        "curveKind": "block_nl",
        "tags": ["join", "mariadb", "no-index"],
    },
    "hash": {
        "best": "O(n + m)", "avg": "O(n + m)", "worst": "O(n * m)",
        "curveKind": "linear_sum",
        "tags": ["join", "mysql-8.0.18+", "equi-join"],
    },
    "bka_join": {
        "best": "O(n * log m)", "avg": "O(n + m)", "worst": "O(n * m)",
        "curveKind": "linear_sum",
        "tags": ["join", "mrr", "batched"],
    },
    "join": {
        "best": "O(n + m)", "avg": "O(n * m / buf)", "worst": "O(n * m)",
        "curveKind": "block_nl",
        "tags": ["join", "comparison"],
    },
    "semijoin_weedout": {
        "best": "O(n + m)", "avg": "O(n * m)", "worst": "O(n * m)",
        "curveKind": "quadratic",
        "tags": ["join", "subquery", "dedup"],
    },
    "lru": {
        "best": "O(1)", "avg": "O(1)", "worst": "O(1)",
        "curveKind": "constant",
        "tags": ["cache", "buffer-pool"],
    },
    "buffer_pool_warmup": {
        "best": "O(1)", "avg": "O(p)", "worst": "O(p)",
        "curveKind": "linear",
        "tags": ["cache", "cold-start"],
    },
    "join_order": {
        "best": "O(N²)", "avg": "O(N·N^d/d)", "worst": "O(N!)",
        "curveKind": "factorial",
        "tags": ["planner", "greedy_search", "optimizer_search_depth"],
    },
}

MYSQL_VERSION = {
    "hash": "8.0.18+",
    "bnl": "removed in 8.0.20",
    "skip_scan": "8.0.13+",
    "rowid_filter": "MariaDB only",
    "join_order": "all versions (greedy_search)",
}


def main() -> int:
    out = []
    for name, lesson in LESSONS.items():
        family = lesson.get("family", "")
        complexity_meta = COMPLEXITY.get(name, {
            "best": "?", "avg": "?", "worst": "?",
            "curveKind": "linear", "tags": [],
        })
        family_dir = FAMILY_DIRS.get(family, "")
        out.append({
            "name": name,
            "family": family,
            "familyLabel": FAMILY_LABEL.get(family, family),
            "familyAccent": FAMILY_ACCENT.get(family, "#64748b"),
            "title": lesson.get("title", name),
            "summary": lesson.get("summary", ""),
            "complexity": {
                "best": complexity_meta["best"],
                "avg": complexity_meta["avg"],
                "worst": complexity_meta["worst"],
            },
            "curveKind": complexity_meta["curveKind"],
            "tags": complexity_meta["tags"],
            "mysqlVersion": MYSQL_VERSION.get(name, ""),
            "lessonHref": f"../teach/{family_dir}/{name}.html" if family_dir else "",
            "curriculumStep": (CURRICULUM.index(name) + 1) if name in CURRICULUM else None,
        })

    # Stable order: by curriculum, then alphabetical within family.
    def sort_key(item):
        step = item["curriculumStep"]
        return (
            0 if step is not None else 1,
            step or 9999,
            item["family"],
            item["name"],
        )
    out.sort(key=sort_key)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps({"lessons": out, "curriculum": CURRICULUM}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(out)} lessons -> {OUT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
