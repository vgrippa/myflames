"""Bounded EXPLAIN input shared by capture, discovery, storage and HTTP.

The canonical parser still owns JSON decoding and tree construction.
"""
import math

from .parser import load_explain_json, parse_explain, _is_mariadb_format

MAX_PLAN = 5 * 1024 * 1024


def _plan(payload, key="plan"):
    text = payload.get(key)
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Choose or paste an EXPLAIN JSON plan.")
    if len(text.encode("utf-8")) > MAX_PLAN:
        raise ValueError("Each plan must be at most 5 MiB.")
    data = load_explain_json(text)
    if not isinstance(data, dict):
        raise ValueError("Expected an EXPLAIN JSON object.")
    plan = data.get("query_plan", data)
    if not isinstance(plan, dict) or not ("operation" in plan or _is_mariadb_format(plan)):
        raise ValueError("Expected a MySQL or MariaDB EXPLAIN ANALYZE JSON plan.")
    # Bound work before the recursive parser and renderers run. Validate the
    # entire input, including metadata, without introducing a second parser.
    stack = [(data, 0)]
    containers = 0
    while stack:
        item, depth = stack.pop()
        if depth > 80:
            raise ValueError("This plan is too deeply nested (maximum 80 levels).")
        if isinstance(item, (dict, list)):
            containers += 1
            if containers > 10000:
                raise ValueError("This plan is too large to inspect in the UI.")
            values = item.values() if isinstance(item, dict) else item
            stack.extend((value, depth + 1) for value in values)
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError("Plan numbers must be finite.")
    root = parse_explain(text)
    query = data.get("query") or plan.get("query") or ""
    if not isinstance(query, str):
        query = ""
    return text, root, query, root.get("engine") or ("mariadb" if _is_mariadb_format(plan) else "mysql")

