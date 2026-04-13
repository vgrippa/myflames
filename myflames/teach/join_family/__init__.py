"""Join-family teach lessons."""
from __future__ import annotations

from . import bka_join, bnl, hash, join_compare, nested_loop, semijoin_weedout

LESSONS = {
    "bka_join": {
        "title": "Batched Key Access (BKA) join — batch, sort, MRR",
        "summary": "Batch outer keys, sort by rowid, and sweep the inner index sequentially via Multi-Range Read.",
        "render": bka_join.render,
    },
    "bnl": {
        "title": "Block Nested Loop join — MariaDB's default for non-indexed joins",
        "summary": "Watch `join_buffer_size` decide how many times the inner table is rescanned.",
        "render": bnl.render,
    },
    "hash": {
        "title": "Hash join — build, probe, and grace-hash spill",
        "summary": "MySQL 8.4's default for non-indexed equi-joins; animated build + probe phases.",
        "render": hash.render,
    },
    "join": {
        "title": "BNL vs hash join — side by side",
        "summary": "MariaDB BNL (default) vs MySQL 8.4 hash join; move the sliders and feel the asymptotic difference.",
        "render": join_compare.render,
    },
    "nested_loop": {
        "title": "Nested Loop Join — dedicated operator view",
        "summary": "Single-operator view of the outer-driver/inner-probe loop shape from EXPLAIN.",
        "render": nested_loop.render,
    },
    "semijoin_weedout": {
        "title": "Semijoin Duplicate Weedout — dedup via temp table",
        "summary": "IN/EXISTS rewritten as inner join; a temp table keyed on outer rowid removes duplicates.",
        "render": semijoin_weedout.render,
    },
}
