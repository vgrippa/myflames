"""Index-access-family teach lessons."""
from __future__ import annotations

from . import btree, non_unique_lookup, unique_lookup, icp, index_merge, skip_scan, rowid_filter

LESSONS = {
    "btree": {
        "title": "B+tree lookup — how InnoDB finds a row",
        "summary": "Clustered primary key vs secondary-to-clustered; 16 KiB page fan-out.",
        "render": btree.render,
    },
    "non_unique_lookup": {
        "title": "Non-Unique Key Lookup — index hits that return many rows",
        "summary": "Understand Index lookup / Index range scan and non-covering row fetch cost.",
        "render": non_unique_lookup.render,
    },
    "unique_lookup": {
        "title": "Unique Key Lookup — single-row index lookup",
        "summary": "Exact-key lookup path and why covering indexes can skip table-row fetch.",
        "render": unique_lookup.render,
    },
    "icp": {
        "title": "Index Condition Pushdown — filtering inside InnoDB",
        "summary": "See how ICP checks trailing index columns before fetching the row.",
        "render": icp.render,
    },
    "index_merge": {
        "title": "Index Merge — combining two index scans",
        "summary": "Union, intersection, sort-union: two indexes are better than a full table scan.",
        "render": index_merge.render,
    },
    "skip_scan": {
        "title": "Skip Scan — range access without the leading index column",
        "summary": "Low-NDV leading column lets MySQL do N small range scans instead of a full table scan.",
        "render": skip_scan.render,
    },
    "rowid_filter": {
        "title": "Rowid Filter — bitmap pre-filter before table access",
        "summary": "MariaDB scans a filtering index to build a rowid bitmap, skipping table fetches for non-matching rows.",
        "render": rowid_filter.render,
    },
}
