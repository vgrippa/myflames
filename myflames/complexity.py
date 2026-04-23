"""
Big O complexity annotations for MySQL / MariaDB plan operators.

Single source of truth. Called once from ``parser.parse_node`` so every node's
``details`` dict carries a vetted ``complexity`` entry that the four renderers
and the JSON sidecar consume unchanged. People study this — correctness is
non-negotiable, and we prefer silence (``None``) to a lie when a required
signal is missing.

A complexity dict looks like::

    {
        "big_o":     "O(n log m)",       # the formula, exact
        "short":     "n log m",          # chip text when space is tight
        "severity":  "good"|"medium"|"bad",
        "rationale": "Indexed nested loop: each outer row probes the inner index.",
        "confidence":"exact"|"typical"|"worst_case",
        "learn_more":"nested_loop_join", # glossary key, optional
    }

For ``access_type == "materialize"`` we emit two keys — ``build_complexity``
and ``scan_complexity`` — because it is a two-phase operator; collapsing
them into one ``big_o`` string misleads the reader about how the cost
compounds over ``loops``.

Parent context
--------------
Nested-loop and semijoin complexities depend on the inner child's access
path. The public ``compute_complexity(node, parent=None)`` accepts a
``parent`` argument so join logic can inspect children; callers that do not
pass a parent get the node-local answer. The parser attaches this field
once, passing the parent node so join decisions are one-shot.

Severity → color (reuses the bargraph "hot palette" at
``output_bargraph.py:98–101``; no new CSS):

    * good   → rgb(100,180,180)  (cyan)
    * medium → rgb(255,200,50)   (amber)
    * bad    → rgb(255,90,90)    (red)
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# Palette (re-exported so renderers don't hard-code hex values)
# ---------------------------------------------------------------------------

SEVERITY_COLORS = {
    "good":   "rgb(100,180,180)",
    "medium": "rgb(255,200,50)",
    "bad":    "rgb(255,90,90)",
}

# Darker variant for chip borders / text, derived once to keep the palette
# a single fact. Swatch colors are intentionally gentle so a chip never
# out-shouts the operator label it annotates.
SEVERITY_BORDERS = {
    "good":   "rgb(40,120,120)",
    "medium": "rgb(180,140,20)",
    "bad":    "rgb(180,50,50)",
}


def _c(big_o, short, severity, rationale, confidence="exact", learn_more=None):
    d = {
        "big_o": big_o,
        "short": short,
        "severity": severity,
        "rationale": rationale,
        "confidence": confidence,
    }
    if learn_more:
        d["learn_more"] = learn_more
    return d


# ---------------------------------------------------------------------------
# Helpers — inspect a child's access path
# ---------------------------------------------------------------------------

_INDEXED_INNER_KINDS = frozenset({"ref", "eq_ref", "range", "fulltext"})
_SCAN_INNER_KINDS = frozenset({"table", "index"})


def _extra_text(node):
    """Lower-cased ``full_label`` — the string MySQL emits in the operation.

    We use ``full_label`` rather than ``extra`` because parser.py doesn't
    populate a separate "Extra" field; the verbiage ("Using index for order
    by", "Using index for skip scan", ...) lives in the operation string.
    """
    return ((node or {}).get("full_label") or "").lower()


def _normalize_kind(node):
    """Collapse the parser's coarse ``access_type`` + fine ``index_access_type``
    + operation text into a single canonical key that dispatch can use.

    MySQL 8 JSON plans set ``access_type == "index"`` for every index-based
    access and put the real kind (range / lookup / single-row / covering-
    variants) in ``index_access_type`` — or, for older plans, in the
    operation string. We reduce all of that to one of the keys in
    ``_DISPATCH`` (see bottom of file) so handlers see a stable input.
    """
    if not isinstance(node, dict):
        return ""
    details = node.get("details") or {}
    access = (details.get("access_type") or "").strip().lower()
    idx_access = (details.get("index_access_type") or "").strip().lower()
    op = _extra_text(node)

    # Joins and operations pass through directly.
    if access in (
        "join", "sort", "group", "union", "weedout", "semijoin", "materialize",
        "rowid_union", "rowid_intersection", "rowid_sort_union",
        "rowid_sort_intersection",
    ):
        return access

    # MySQL's JSON vocabulary uses "aggregate" for GROUP BY operations;
    # we collapse it onto our "group" key.
    if access == "aggregate":
        return "group"

    if access in ("table", "all"):
        return "table"
    if access == "fulltext":
        return "fulltext"
    if access == "range":
        return "range"
    if access in ("ref", "ref_or_null"):
        return "ref"
    if access in ("eq_ref", "const", "system"):
        return "eq_ref"

    # MySQL JSON "index" bucket — disambiguate via index_access_type or op text.
    if access == "index":
        if "range" in idx_access or "range scan" in op:
            return "range"
        if "single_row" in idx_access or "single-row" in op:
            return "eq_ref"
        if "index_lookup" in idx_access or "index lookup" in op:
            return "ref"
        if "covering_index_lookup" in idx_access:
            return "ref"
        if "covering_index_range_scan" in idx_access:
            return "range"
        # Plain / covering index scan → full index walk
        return "index"

    # Unknown access → let handler return None via dispatch miss.
    return access


def _first_child_kind(node):
    """Normalized kind of the right-most (inner) child, for join logic.

    Joins are modeled as a parent with ``children = [outer, inner]``. The
    inner side decides whether the join is indexed nested-loop or a scan.
    """
    kids = (node or {}).get("children") or []
    if not kids:
        return ""
    return _normalize_kind(kids[-1])


# ---------------------------------------------------------------------------
# Per-access-type handlers. Each returns a complexity dict OR None to opt out.
# Parent is consulted for cases where the join frame decides.
# ---------------------------------------------------------------------------


def _for_table_scan(node, _parent):
    return _c(
        "O(n)",
        "n",
        "medium",
        "Full table scan: the storage engine returns every row; cost scales "
        "with the table size.",
        "exact",
        "full_table_scan",
    )


def _for_index_scan(node, _parent):
    covering = bool((node.get("details") or {}).get("covering"))
    if covering:
        return _c(
            "O(n)",
            "n",
            "good",
            "Covering index scan: walks the whole index in key order but "
            "never touches the clustered tree — all selected columns fit on "
            "the index leaves.",
            "exact",
            "covering_index",
        )
    return _c(
        "O(n)",
        "n",
        "medium",
        "Index scan: reads every entry of the index in key order. Cheaper "
        "than a table scan because the index is smaller, but still O(n).",
        "exact",
        "index_scan",
    )


def _for_ref_lookup(node, _parent):
    # Re-normalize because the raw access_type field is "index" for MySQL
    # JSON plans and only the normalized key distinguishes ref vs eq_ref.
    kind = _normalize_kind(node)
    if kind == "eq_ref":
        return _c(
            "O(log n)",
            "log n",
            "good",
            "Unique-key lookup (eq_ref): one B+tree descent into the inner "
            "table per outer row — as fast as MySQL can get.",
            "exact",
            "single_row_lookup",
        )
    return _c(
        "O(log n + k)",
        "log n + k",
        "good",
        "Index lookup (ref): one B+tree descent then a sequential walk over "
        "k matching index entries. k = matching rows for the indexed "
        "predicate.",
        "exact",
        "index_lookup",
    )


def _for_range(node, _parent):
    extra = _extra_text(node)
    if "skip scan" in extra:
        return _c(
            "O(d · log n)",
            "d · log n",
            "medium",
            "Skip scan: probes each of d distinct values of the leading "
            "index column, then range-scans the rest. d is not exposed by "
            "EXPLAIN; pays off only when the leading column has very low "
            "cardinality.",
            "typical",
            "skip_scan",
        )
    return _c(
        "O(log n + k)",
        "log n + k",
        "good",
        "Index range scan: one B+tree descent to the range start, then a "
        "sequential walk over k matching index entries.",
        "exact",
        "index_range_scan",
    )


def _for_fulltext(node, _parent):
    return _c(
        "O(k)",
        "k",
        "good",
        "Fulltext index lookup via inverted index: k = matching documents. "
        "Does not scan the whole table.",
        "exact",
    )


def _for_sort(node, _parent):
    extra = _extra_text(node)
    if "using index for order by" in extra or "using index for group-by" in extra:
        return _c(
            "O(n)",
            "n",
            "good",
            "Index-backed sort: MySQL walks an index already in the required "
            "order, so the ORDER BY / GROUP BY adds no sort cost.",
            "exact",
            "filesort",
        )
    return _c(
        "O(n log n)",
        "n log n",
        "medium",
        "Filesort: MySQL sorts the output in the server layer. Priority-"
        "queue filesort (LIMIT) is not detected today — we report the "
        "upper bound.",
        "worst_case",
        "filesort",
    )


def _for_group(node, _parent):
    extra = _extra_text(node)
    if "using index" in extra:
        return _c(
            "O(n)",
            "n",
            "good",
            "Grouped aggregation backed by a sorted index: one pass, no sort.",
            "exact",
        )
    return _c(
        "O(n log n)",
        "n log n",
        "medium",
        "Sort-based aggregation: MySQL sorts by the grouping key before "
        "collapsing duplicates.",
        "typical",
    )


def _for_union(node, _parent):
    extra = _extra_text(node)
    if "union all" in extra:
        return _c(
            "O(n + m)",
            "n + m",
            "good",
            "UNION ALL: the inputs are concatenated — no dedupe pass.",
            "exact",
        )
    return _c(
        "O((n + m) · log(n + m))",
        "(n+m) log(n+m)",
        "medium",
        "UNION: results are concatenated then sorted/deduplicated. Use "
        "UNION ALL when duplicates are impossible to skip this cost.",
        "typical",
    )


def _for_rowid_union(node, _parent):
    return _c(
        "O(Σ kᵢ)",
        "Σ kᵢ",
        "medium",
        "Index merge (union): each index scan returns kᵢ row IDs; merging "
        "them through a sorted-list union costs a pass over the total.",
        "typical",
        "index_merge",
    )


def _for_rowid_intersection(node, _parent):
    return _c(
        "O(Σ kᵢ)",
        "Σ kᵢ",
        "medium",
        "Index merge (intersection): scans each index separately and "
        "intersects the sorted row-ID streams.",
        "typical",
        "index_merge",
    )


def _for_rowid_sort_union(node, _parent):
    return _c(
        "O(n log n)",
        "n log n",
        "medium",
        "Index merge (sort-union): each input is scanned then sorted before "
        "the merge — slower than plain rowid_union because of the sort.",
        "typical",
        "index_merge",
    )


def _for_weedout(node, _parent):
    return _c(
        "O(n log n)",
        "n log n",
        "medium",
        "DuplicateWeedout semijoin: runs as a plain inner join, then drops "
        "duplicates using a temp-table index on the outer row IDs.",
        "typical",
        "duplicate_weedout",
    )


def _for_semijoin(node, _parent):
    """Semijoin strategy varies; we make the best call from the node text."""
    extra = _extra_text(node)
    if "firstmatch" in extra:
        return _c(
            "O(n · log m)",
            "n · log m",
            "good",
            "FirstMatch semijoin: stops at the first inner match per outer "
            "row. Assumes the inner side has a usable index.",
            "typical",
            "firstmatch",
        )
    if "loosescan" in extra or "loose scan" in extra:
        return _c(
            "O(n)",
            "n",
            "good",
            "LooseScan semijoin: walks the inner index exactly once, using "
            "key boundaries to skip over duplicate values.",
            "typical",
            "loosescan",
        )
    if "materializ" in extra:
        return _c(
            "O(m) + O(n · log m)",
            "m + n log m",
            "medium",
            "Materialization semijoin: the inner side is materialized once "
            "into a temp table (O(m)); each outer row probes it (O(log m)).",
            "typical",
            "materialization",
        )
    # Fallback — semijoin is present but strategy not determinable.
    return _c(
        "O(n · log m)",
        "n · log m",
        "medium",
        "Semijoin: deduplication strategy (FirstMatch / LooseScan / Weedout "
        "/ Materialization) is not explicit in the plan; this is the "
        "typical cost when the inner side is indexed.",
        "typical",
        "semijoin",
    )


def _for_materialize(node, _parent):
    """Two-phase: build cost is the child's complexity; scan cost is O(rows).

    We emit BOTH under separate keys rather than collapsing into one big_o
    string — pretending materialize is single-cost would mislead a student
    about how ``actual_loops`` compounds with the build.
    """
    kids = (node or {}).get("children") or []
    child_complexity = None
    if kids:
        child_details = (kids[0].get("details") or {})
        child_complexity = child_details.get("complexity")
    build = child_complexity or _c(
        "O(m)",
        "m",
        "medium",
        "Materialize build: cost = cost of the source subquery over m rows.",
        "typical",
        "materialization",
    )
    scan = _c(
        "O(1) per probe, O(rows) to fetch all",
        "O(1)/probe",
        "good",
        "Materialize scan: reads the pre-computed temp table. Each probe is "
        "constant time once the table is in memory.",
        "typical",
        "materialization",
    )
    return {
        "big_o": "build: " + build["big_o"] + "  •  scan: " + scan["big_o"],
        "short": "build+scan",
        "severity": build["severity"],
        "rationale": (
            "Materialize is a two-phase operator. The build pays the inner "
            "subquery's cost ONCE; each scan of the temp table is effectively "
            "constant time. Don't compound build cost by loops."
        ),
        "confidence": build.get("confidence", "typical"),
        "learn_more": "materialization",
        "build_complexity": build,
        "scan_complexity": scan,
    }


# ---------------------------------------------------------------------------
# Join handling — decided by details.join_algorithm + inner child access_type
# + using_join_buffer flag. Chip lives on the join node, not on the inner
# child (avoids double-display).
# ---------------------------------------------------------------------------


def _for_join(node, _parent):
    details = node.get("details") or {}
    algo = (details.get("join_algorithm") or "").lower()
    buffer = (details.get("using_join_buffer") or "").lower()

    # Hash join (MySQL 8.0.18+). Detect via algorithm or the Extra clue.
    if algo == "hash" or "hash" in buffer:
        return _c(
            "O(n + m)",
            "n + m",
            "good",
            "Hash join: builds a hash on one side (O(m)), streams the other "
            "past it (O(n)). Disk-spill does NOT change the asymptotic "
            "class — only the constants.",
            "exact",
            "hash_join",
        )

    # Batched Key Access — pedagogically distinct from plain nested loop.
    if "batched key access" in buffer or algo in ("batched_key_access", "batch_key_access"):
        return _c(
            "O(n · log m)",
            "n · log m",
            "medium",
            "Batched Key Access: outer keys are buffered and sorted, then "
            "the inner side is fetched via Multi-Range Read in clustered "
            "order. Cuts random I/O but keeps the index-descent cost per "
            "outer row.",
            "typical",
            "batched_key_access",
        )

    # Block Nested Loop — the classic blow-up case.
    if "block nested loop" in buffer:
        return _c(
            "O(n · m)",
            "n · m",
            "bad",
            "Block Nested Loop: MySQL buffers a block of outer rows then "
            "scans the entire inner side per block. Add an index on the "
            "join column OR enable hash_join to escape this class.",
            "exact",
            "block_nested_loop",
        )

    inner_kind = _first_child_kind(node)

    # Indexed-inner nested loop (ref / eq_ref / range on the inner side).
    if inner_kind in _INDEXED_INNER_KINDS:
        return _c(
            "O(n · log m)",
            "n · log m",
            "medium",
            "Indexed nested loop: each outer row probes the inner table "
            "via an index descent.",
            "exact",
            "nested_loop_join",
        )

    # Unindexed inner with no buffer → quadratic.
    if inner_kind in _SCAN_INNER_KINDS:
        return _c(
            "O(n · m)",
            "n · m",
            "bad",
            "Unindexed nested loop: every outer row drives a full scan of "
            "the inner side. This is the classic O(n²)-class blow-up.",
            "exact",
            "nested_loop_join",
        )

    # Can't determine — opt out rather than lie.
    return None


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

# Keyed on the normalized kind produced by ``_normalize_kind``.
_DISPATCH = {
    "table":                    _for_table_scan,
    "index":                    _for_index_scan,
    "ref":                      _for_ref_lookup,
    "eq_ref":                   _for_ref_lookup,
    "range":                    _for_range,
    "fulltext":                 _for_fulltext,
    "sort":                     _for_sort,
    "group":                    _for_group,
    "union":                    _for_union,
    "rowid_union":              _for_rowid_union,
    "rowid_intersection":       _for_rowid_intersection,
    "rowid_sort_union":         _for_rowid_sort_union,
    "rowid_sort_intersection":  _for_rowid_sort_union,  # same class
    "weedout":                  _for_weedout,
    "semijoin":                 _for_semijoin,
    "materialize":              _for_materialize,
    "join":                     _for_join,
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def compute_complexity(node, parent=None):
    """Return a complexity dict for *node*, or ``None`` if the operator is
    unknown / the required signals are missing.

    Parameters
    ----------
    node : dict
        A parsed node (the shape returned by ``parser.parse_node``).
    parent : dict, optional
        The parsed parent node. Currently unused — join logic inspects the
        node's own ``children`` — but accepted for future extensions
        (e.g. scoping filesort O(n) when an outer LIMIT is known).

    Returns
    -------
    dict or None
        The complexity dict, or ``None`` to signal "no chip; don't lie".
    """
    if not isinstance(node, dict):
        return None
    kind = _normalize_kind(node)
    if not kind:
        # Probably a structural node; check for an implicit join via join_algorithm.
        details = node.get("details") or {}
        if (details.get("join_algorithm") or "").strip():
            return _for_join(node, parent)
        return None
    handler = _DISPATCH.get(kind)
    if handler is None:
        return None
    return handler(node, parent)


__all__ = [
    "compute_complexity",
    "SEVERITY_COLORS",
    "SEVERITY_BORDERS",
]
