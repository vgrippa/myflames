"""Build teach-lesson hooks from parsed EXPLAIN trees.

The hook payload is intentionally compact and machine-readable so it can be
embedded in the sidecar and consumed by report UIs without parsing tooltip
strings.
"""
from __future__ import annotations

from .parser import flatten_nodes


SUPPORTED_LESSONS = frozenset({
    "bka_join",
    "btree",
    "bnl",
    "derived_table",
    "filter",
    "filesort",
    "full_scan",
    "hash",
    "icp",
    "index_merge",
    "join",
    "nested_loop",
    "non_unique_lookup",
    "rowid_filter",
    "semijoin_weedout",
    "skip_scan",
    "tmp",
    "unique_lookup",
})


def _as_int(value):
    if value is None:
        return None
    try:
        s = str(value).strip()
        if not s:
            return None
        if s.lower().endswith("kb"):
            return int(float(s[:-2].strip()) * 1024)
        if s.lower().endswith("mb"):
            return int(float(s[:-2].strip()) * 1024 * 1024)
        if s.lower().endswith("gb"):
            return int(float(s[:-2].strip()) * 1024 * 1024 * 1024)
        return int(float(s))
    except Exception:
        return None


def _eff_rows(node):
    rows = float(node.get("rows") or 0)
    loops = float(node.get("loops") or 1)
    v = int(round(rows * loops))
    return v if v > 0 else 1


def _row_size_for_lesson(node, variables):
    """Estimate average row size from EXPLAIN node details.

    EXPLAIN JSON doesn't include physical row sizes directly. We use
    ``data_length / table_rows`` from collected_stats when available,
    otherwise fall back to a conservative 200 B default. The lesson
    sliders let users adjust interactively.
    """
    details = node.get("details") or {}
    table_name = (details.get("table_name") or "").strip()
    stats = (variables or {}).get("_collected_stats") or {}
    if table_name and table_name in stats:
        tbl = stats[table_name]
        tbl_rows = _as_int(tbl.get("table_rows"))
        data_len = _as_int(tbl.get("data_length"))
        if tbl_rows and tbl_rows > 0 and data_len and data_len > 0:
            avg = max(32, min(4096, data_len // tbl_rows))
            return avg
    return 200


def _key_size_for_node(node):
    """Rough key size estimate from index context.

    If the node accesses a PRIMARY key we assume 8 B (BIGINT). For
    secondary indexes we assume 16 B (two-column composite). These are
    intentionally conservative — the lesson sliders can be tuned.
    """
    details = node.get("details") or {}
    index_name = (details.get("index_name") or "").strip().upper()
    if index_name == "PRIMARY" or not index_name:
        return 8
    return 16


def _classify_lesson(node):
    details = node.get("details") or {}
    op = (details.get("operation") or node.get("full_label") or "").lower()
    using_join_buffer = (details.get("using_join_buffer") or "").lower()
    join_alg = (details.get("join_algorithm") or "").lower()
    access_type = (details.get("access_type") or "").lower()

    index_access_type = (details.get("index_access_type") or "").lower()

    if "index merge" in op or "index_merge" in access_type or details.get("mariadb_index_merge"):
        return "index_merge"
    # Semijoin: duplicate weedout
    if "weedout" in op or "remove duplicate" in op:
        return "semijoin_weedout"
    if op.startswith("filter"):
        return "filter"
    if "single-row index lookup" in op:
        return "unique_lookup"
    # Skip scan (MySQL 8+)
    if "skip scan" in op or index_access_type == "skip_scan":
        return "skip_scan"
    # Rowid filter (MariaDB)
    if details.get("using_rowid_filter"):
        return "rowid_filter"
    if "index range scan" in op or "index lookup" in op:
        # BKA: batched key access via MRR
        if details.get("using_mrr") or "multi-range" in op or index_access_type == "multi_range_read":
            return "bka_join"
        return "non_unique_lookup"
    if using_join_buffer or "bnl" in op:
        return "bnl"
    if "table scan" in op or access_type in ("table", "all"):
        return "full_scan"
    if details.get("pushed_index_condition"):
        return "icp"
    if "filesort" in op or op.startswith("sort"):
        return "filesort"
    # Derived table materialization (subquery → temp table)
    if "materialize" in op and ("derived" in op or "subquery" in op):
        return "derived_table"
    if "temporary table" in op or "tmp table" in op or "materialize" in op:
        return "tmp"
    if "hash join" in op or join_alg == "hash":
        return "hash"
    if "nested loop" in op and len(node.get("children") or []) >= 2:
        return "nested_loop"
    if details.get("index_name") and access_type in ("ref", "eq_ref", "range", "index", "const"):
        return "btree"
    return None


def _controls_for_lesson(lesson, node, variables):
    details = node.get("details") or {}
    children = node.get("children") or []
    outer = children[0] if len(children) >= 1 else None
    inner = children[1] if len(children) >= 2 else None
    row_size = _row_size_for_lesson(node, variables)
    join_buffer = _as_int((variables or {}).get("join_buffer_size"))
    sort_buffer = _as_int((variables or {}).get("sort_buffer_size"))
    tmp_size = _as_int((variables or {}).get("tmp_table_size"))
    max_heap = _as_int((variables or {}).get("max_heap_table_size"))
    page_size = _as_int((variables or {}).get("innodb_page_size"))

    controls = {}
    if lesson == "bnl":
        controls["outer_rows"] = _eff_rows(outer or node)
        controls["inner_rows"] = _eff_rows(inner or node)
        controls["row_size"] = row_size
        if join_buffer:
            controls["jbs"] = join_buffer
    elif lesson == "hash":
        left_rows = _eff_rows(outer or node)
        right_rows = _eff_rows(inner or node)
        build_rows = min(left_rows, right_rows)
        probe_rows = max(left_rows, right_rows)
        controls["build_rows"] = build_rows
        controls["probe_rows"] = probe_rows
        controls["row_size"] = row_size
        if join_buffer:
            controls["jbs"] = join_buffer
    elif lesson == "join" or lesson == "nested_loop":
        controls["outer_rows"] = _eff_rows(outer or node)
        controls["inner_rows"] = _eff_rows(inner or node)
        controls["row_size"] = row_size
        if join_buffer:
            controls["jbs"] = join_buffer
    elif lesson == "filesort":
        controls["rows"] = _eff_rows(node)
        controls["row_size"] = row_size
        if sort_buffer:
            controls["sbs"] = sort_buffer
    elif lesson == "tmp":
        controls["rows"] = _eff_rows(node)
        controls["row_size"] = row_size
        if tmp_size:
            controls["tmp_size"] = tmp_size
        if max_heap:
            controls["max_heap"] = max_heap
    elif lesson == "icp":
        controls["index_rows"] = _eff_rows(node)
        controls["selectivity"] = 35
    elif lesson == "index_merge":
        total = _eff_rows(node)
        controls["a_rows"] = max(1, int(round(total * 0.6)))
        controls["b_rows"] = max(1, total - controls["a_rows"])
        controls["overlap"] = 30
        im = details.get("mariadb_index_merge") or {}
        variant = (im.get("kind") or "").lower()
        controls["variant"] = variant if variant in ("union", "intersection", "sort_union") else "union"
    elif lesson == "btree":
        controls["rows"] = _eff_rows(node)
        controls["key_size"] = _key_size_for_node(node)
        controls["page_size"] = page_size or 16384
        controls["key_type"] = "secondary_covering" if details.get("covering") else "secondary_noncovering"
    elif lesson == "full_scan":
        controls["rows"] = _eff_rows(node)
        controls["row_size"] = row_size
        est = details.get("estimated_rows")
        sel = 100.0
        try:
            if est is not None and float(est) > 0:
                sel = (float(node.get("rows") or 0) / float(est)) * 100.0
        except Exception:
            sel = 100.0
        if details.get("condition") and sel > 95:
            sel = 10.0
        sel = max(0.1, min(100.0, sel))
        controls["selectivity"] = round(sel, 1)
    elif lesson == "non_unique_lookup":
        controls["rows"] = _eff_rows(node)
        est = details.get("estimated_rows")
        sel = 5.0
        try:
            if est is not None and float(est) > 0:
                sel = (float(node.get("rows") or 0) / float(est)) * 100.0
        except Exception:
            sel = 5.0
        sel = max(0.1, min(40.0, sel))
        controls["selectivity"] = round(sel, 1)
        controls["covering"] = bool(details.get("covering"))
    elif lesson == "unique_lookup":
        controls["rows"] = _eff_rows(node)
        controls["covering"] = bool(details.get("covering"))
    elif lesson == "filter":
        controls["input_rows"] = _eff_rows(node)
        est = details.get("estimated_rows")
        sel = 50.0
        try:
            if est is not None and float(est) > 0:
                sel = (float(node.get("rows") or 0) / float(est)) * 100.0
        except Exception:
            sel = 50.0
        if details.get("condition") and sel > 95:
            sel = 10.0
        controls["selectivity"] = round(max(0.1, min(100.0, sel)), 1)
    elif lesson == "bka_join":
        controls["outer_rows"] = _eff_rows(outer or node)
        controls["inner_rows"] = _eff_rows(inner or node)
        controls["row_size"] = row_size
        controls["key_size"] = _key_size_for_node(inner or node)
        if join_buffer:
            controls["jbs"] = join_buffer
    elif lesson == "skip_scan":
        controls["table_rows"] = _eff_rows(node)
        controls["ndv_leading"] = 5  # conservative default; EXPLAIN doesn't expose NDV
        controls["selectivity"] = 10
    elif lesson == "rowid_filter":
        controls["main_rows"] = _eff_rows(node)
        controls["filter_selectivity"] = 20
        controls["row_size"] = row_size
    elif lesson == "semijoin_weedout":
        controls["outer_rows"] = _eff_rows(outer or node)
        # Inner matches: estimate from loop count of inner child
        inner_loops = int(inner.get("loops") or 1) if inner else 1
        inner_rows_per = int(round(float(inner.get("rows") or 1))) if inner else 5
        controls["inner_matches"] = max(1, min(100, inner_rows_per))
        controls["row_size"] = row_size
    elif lesson == "derived_table":
        controls["subquery_rows"] = _eff_rows(node)
        controls["row_size"] = row_size
        controls["outer_rows"] = _eff_rows(outer or node) if outer else 1000
        controls["has_index"] = True
    return controls


def _note_for_hook(node):
    details = node.get("details") or {}
    table_name = (details.get("table_name") or "").strip()
    operation = (details.get("operation") or node.get("full_label") or "").strip()
    if table_name:
        return "{} ({})".format(operation, table_name)
    return operation[:180]


def build_teach_hooks(root, query_sql=None, variables=None, stats=None):
    """Return deterministic teach hooks for nodes that map to a lesson.

    *stats* is an optional ``{table_name: {...}}`` dict from
    ``information_schema.tables`` collected by the advisor. When present,
    ``_row_size_for_lesson`` can compute ``data_length / table_rows``
    instead of falling back to the 200 B default.
    """
    # Smuggle stats into the variables dict so _row_size_for_lesson can
    # reach it without changing every intermediate signature.
    if stats:
        variables = dict(variables or {})
        variables["_collected_stats"] = stats
    hooks = []
    seen_folded = set()
    if not root:
        return hooks
    for node in flatten_nodes(root):
        lesson = _classify_lesson(node)
        if lesson not in SUPPORTED_LESSONS:
            continue
        folded = (node.get("folded_label") or "").strip()
        if not folded or folded in seen_folded:
            continue
        seen_folded.add(folded)
        short = (node.get("short_label") or "").strip()
        hook = {
            "lesson": lesson,
            "match": {"folded_label": folded},
            "controls": _controls_for_lesson(lesson, node, variables),
            "note": _note_for_hook(node),
        }
        if short:
            hook["match"]["short_label"] = short
        if query_sql:
            hook["query_sql"] = query_sql
        hooks.append(hook)
    return hooks


def build_teach_index_maps(hooks):
    """Build lookup maps used by SVG renderers."""
    by_folded = {}
    by_short = {}
    for i, hook in enumerate(hooks or []):
        match = hook.get("match") or {}
        folded = (match.get("folded_label") or "").strip()
        short = (match.get("short_label") or "").strip()
        if folded and folded not in by_folded:
            by_folded[folded] = i
        if short and short not in by_short:
            by_short[short] = i
    return {"by_folded_label": by_folded, "by_short_label": by_short}

