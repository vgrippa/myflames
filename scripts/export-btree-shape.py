#!/usr/bin/env python3
"""Write the B+tree shape fixture the 3D B+tree view is checked against.

The 3D view (apps/teach-3d) mirrors ``innodb_index_shapes`` and
``leaf_range_pages`` from myflames/teach/_cost_model.py in TypeScript. This
script evaluates the Python model for every value the view's controls offer
and writes apps/teach-3d/test/fixtures/btree-shape.json; the node test
compares the TypeScript against it, and test/test_teach.py fails when the
committed fixture no longer matches the Python model.

Usage: python3 scripts/export-btree-shape.py [--check]
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from myflames.teach import _cost_model as cm  # noqa: E402

FIXTURE = os.path.join(ROOT, "apps", "teach-3d", "test", "fixtures", "btree-shape.json")

#: The values the 3D view's controls offer (apps/teach-3d/src/model.ts OPTIONS).
OPTIONS = {
    "rows": [100, 1000, 10000, 100000, 1000000, 10000000, 100000000, 1000000000],
    "row_bytes": [50, 100, 200, 500, 1000],
    "email_bytes": [16, 24, 32, 64],
    "page_kib": [4, 8, 16, 32, 64],
}

#: The range scan chapter reads ``id BETWEEN 42 AND 4000`` (capped at the row count).
RANGE = (42, 4000)


def build():
    cases = []
    for rows in OPTIONS["rows"]:
        for row_bytes in OPTIONS["row_bytes"]:
            for email_bytes in OPTIONS["email_bytes"]:
                for page_kib in OPTIONS["page_kib"]:
                    page_size = page_kib * 1024
                    shapes = cm.innodb_index_shapes(rows, row_bytes, email_bytes, page_size)
                    last = min(RANGE[1], rows)
                    cases.append({
                        "rows": rows, "row_bytes": row_bytes, "email_bytes": email_bytes,
                        "page_size": page_size,
                        "clustered": shapes["clustered"], "secondary": shapes["secondary"],
                        "range_leaves": cm.leaf_range_pages(
                            RANGE[0], last, shapes["clustered"]["leaf_capacity"]),
                    })
    return {
        "generated_by": "scripts/export-btree-shape.py",
        "constants": {
            "page_overhead_bytes": cm.INNODB_PAGE_OVERHEAD_BYTES,
            "child_pointer_bytes": cm.INNODB_CHILD_POINTER_BYTES,
            "record_header_bytes": cm.INNODB_RECORD_HEADER_BYTES,
            "clustered_system_bytes": cm.INNODB_CLUSTERED_SYSTEM_BYTES,
            "pk_bytes": cm.PK_BIGINT_BYTES,
            "clustered_leaf_reserve_divisor": cm.INNODB_CLUSTERED_LEAF_RESERVE_DIVISOR,
        },
        "options": OPTIONS,
        "range": list(RANGE),
        "cases": cases,
    }


def render(data):
    """One case per line keeps diffs readable."""
    head = {k: v for k, v in data.items() if k != "cases"}
    lines = [json.dumps(case, sort_keys=True, separators=(",", ":")) for case in data["cases"]]
    text = json.dumps(head, sort_keys=True, indent=2)[:-2]
    return text + ',\n  "cases": [\n    ' + ",\n    ".join(lines) + "\n  ]\n}\n"


def main(argv):
    text = render(build())
    if "--check" in argv:
        with open(FIXTURE, encoding="utf-8") as handle:
            if handle.read() != text:
                print("btree-shape.json is stale: run python3 scripts/export-btree-shape.py",
                      file=sys.stderr)
                return 1
        return 0
    with open(FIXTURE, "w", encoding="utf-8") as handle:
        handle.write(text)
    print("wrote %s (%d cases)" % (os.path.relpath(FIXTURE, ROOT), len(json.loads(text)["cases"])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
