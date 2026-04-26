#!/usr/bin/env python3
"""Compare regenerated fixtures against committed fixtures, ignoring noisy
timing fields.

`EXPLAIN ANALYZE` reports wall-clock measurements (`actual_last_row_ms`,
`actual_first_row_ms`, and the MariaDB `r_*_time_ms` family) that vary every
run. The `Fixtures drift` workflow is meant to catch *structural* drift
between pinned MySQL 8.4 / MariaDB 11.4 output and the committed fixtures —
not wall-clock noise — so this script strips those fields from both sides
before diffing.

Exit 0 when only timing-noise differs; exit 1 (with a diff dump) when any
other field changes.
"""
from __future__ import annotations

import difflib
import json
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

NOISY_KEYS = frozenset({
    "actual_last_row_ms",
    "actual_first_row_ms",
    "r_total_time_ms",
    "r_table_time_ms",
    "r_other_time_ms",
    "r_buffer_size",
    "r_filesort_pass_count",
})

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "test" / "fixtures"


def strip_noise(node):
    if isinstance(node, dict):
        return {k: strip_noise(v) for k, v in node.items() if k not in NOISY_KEYS}
    if isinstance(node, list):
        return [strip_noise(v) for v in node]
    return node


def head_content(rel_path: str) -> Optional[str]:
    try:
        return subprocess.check_output(
            ["git", "show", f"HEAD:{rel_path}"],
            cwd=str(REPO_ROOT),
            stderr=subprocess.DEVNULL,
        ).decode("utf-8")
    except subprocess.CalledProcessError:
        return None


def main() -> int:
    drift_files: List[str] = []
    for path in sorted(FIXTURES_DIR.rglob("*.json")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        committed = head_content(rel)
        if committed is None:
            print(f"::error file={rel}::new fixture not present in HEAD")
            drift_files.append(rel)
            continue
        try:
            current = json.loads(path.read_text())
            previous = json.loads(committed)
        except json.JSONDecodeError as e:
            print(f"::error file={rel}::invalid JSON: {e}")
            drift_files.append(rel)
            continue
        if strip_noise(current) != strip_noise(previous):
            drift_files.append(rel)
            print(f"::error file={rel}::structural drift (non-timing fields changed)")
            previous_norm = json.dumps(strip_noise(previous), indent=2, sort_keys=True)
            current_norm = json.dumps(strip_noise(current), indent=2, sort_keys=True)
            for line in difflib.unified_diff(
                previous_norm.splitlines(),
                current_norm.splitlines(),
                fromfile=f"HEAD:{rel}",
                tofile=f"regenerated:{rel}",
                lineterm="",
            ):
                print(line)

    if drift_files:
        print(f"\n{len(drift_files)} fixture(s) drifted structurally.")
        return 1
    print("No structural drift detected (timing-only changes ignored).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
