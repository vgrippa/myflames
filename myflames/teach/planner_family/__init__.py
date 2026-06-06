"""Planner-family teach lessons.

Where the *executor* families (scan / index / join / cache) explain how
a chosen plan **runs**, the planner family explains how the plan gets
**chosen** in the first place — the join-order search that decides the
order of nested loops, which side builds a hash table, etc.
"""
from __future__ import annotations

from . import join_order

LESSONS = {
    "join_order": {
        "title": "Join-order search — why query planning is factorial",
        "summary": (
            "MySQL's greedy_search picks the join order one table at a time; "
            "with deep lookahead and pruning off, the search space is O(N!)."
        ),
        "render": join_order.render,
    },
}
