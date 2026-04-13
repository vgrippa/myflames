"""Scan/filter/sort/temp teach lessons."""
from __future__ import annotations

from . import derived_table, filesort, tmp, full_scan, filter

LESSONS = {
    "filesort": {
        "title": "Filesort — how MySQL sorts without an index",
        "summary": "sort_buffer_size, sorted runs, k-way merge. Why ORDER BY is slow without an index.",
        "render": filesort.render,
    },
    "tmp": {
        "title": "Temporary tables — MEMORY to on-disk conversion",
        "summary": "Watch GROUP BY hit the tmp_table_size limit and convert to on-disk InnoDB.",
        "render": tmp.render,
    },
    "full_scan": {
        "title": "Full table scan — why every row gets read",
        "summary": "See O(n) row reads in action and compare against indexed access O(log n + k).",
        "render": full_scan.render,
    },
    "filter": {
        "title": "Filter operator — WHERE predicate row-by-row",
        "summary": "Rows entering filter are all evaluated; only matching rows continue.",
        "render": filter.render,
    },
    "derived_table": {
        "title": "Derived Table Materialization",
        "summary": "FROM-clause subquery materialized into temp table, auto-indexed, then probed.",
        "render": derived_table.render,
    },
}
