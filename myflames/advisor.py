"""
Environment-aware advisor: takes collected schema / stats / session variables
and returns additional warnings and suggestions that the existing
:func:`myflames.parser.analyze_plan` can't produce from the plan alone.

Design
------
Rules are split into small, independently-testable functions
(``_rule_buffer_pool_vs_data_size``, ``_rule_sort_buffer_vs_filesort``, ...).
Each rule:

* takes a bag of context (``analysis``, ``schema``, ``stats``, ``variables``),
* returns ``(warning_or_none, suggestion_or_none)``.

:func:`advise` runs every rule, filters ``None`` results and extends the
``analysis`` dict with two new keys (``environment_warnings``,
``environment_suggestions``). It also populates ``collected_variables`` so
the renderer can show the tunables that were inspected.

The advisor never mutates the plan tree; it only augments the analysis dict
produced by :func:`analyze_plan`.
"""


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------

def _to_int(value, default=0):
    """Best-effort integer conversion; returns *default* on failure.

    ``SHOW VARIABLES`` always returns values as strings, including for
    byte-sized tunables like ``innodb_buffer_pool_size``. The server already
    resolves M/G suffixes at startup, so we never have to parse those — but
    we still guard against empty strings from MariaDB dictionary quirks.
    """
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if not s or s.upper() == "NULL":
        return default
    try:
        return int(s)
    except ValueError:
        try:
            return int(float(s))
        except ValueError:
            return default


def _human_bytes(n):
    """Format *n* bytes as a short human string (``1.2 GB``)."""
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            if unit == "B":
                return "{:.0f} {}".format(n, unit)
            return "{:.1f} {}".format(n, unit)
        n /= 1024
    return "{:.1f} TB".format(n)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

def _rule_buffer_pool_vs_data_size(analysis, schema, stats, variables):
    """Warn when ``innodb_buffer_pool_size`` is much smaller than the
    working set of the tables the query touches.

    Why this matters: every page miss on a cold buffer pool becomes a disk
    read. For queries that touch more data than fits in memory, tuning the
    plan will only get you so far — you have to size the pool.
    """
    if not variables or not stats:
        return (None, None)
    bp = _to_int(variables.get("innodb_buffer_pool_size"))
    if bp <= 0:
        return (None, None)
    total = sum(
        _to_int(s.get("data_length")) + _to_int(s.get("index_length"))
        for s in stats.values()
    )
    if total <= 0:
        return (None, None)
    ratio = bp / total
    if ratio >= 1.0:
        return (None, None)  # already fits
    if ratio < 0.25:
        severity = "much smaller than"
    elif ratio < 0.5:
        severity = "smaller than"
    else:
        return (None, None)
    warn = (
        "innodb_buffer_pool_size ({}) is {} the working set of the tables "
        "referenced by this query ({}). Cold reads will hit disk."
    ).format(_human_bytes(bp), severity, _human_bytes(total))
    sug = (
        "Raise innodb_buffer_pool_size to at least {} (≈ working set). "
        "Why: every page miss on a cold buffer pool is a physical read from "
        "disk — once the hot pages fit in RAM, repeat queries stay entirely "
        "in memory and typically run 10–100× faster. Size it to roughly the "
        "combined data_length + index_length of the tables you actually hit."
    ).format(_human_bytes(total))
    return (warn, sug)


def _rule_sort_buffer_vs_filesort(analysis, schema, stats, variables):
    """Warn when the plan has a filesort but ``sort_buffer_size`` is tiny.

    Tiny = the default 256 KB. Anything under 512 KB will often spill to
    disk for realistic result sets. Above 8 MB the buffer is per-connection
    and becomes wasteful — we deliberately do not advise raising it past
    that blindly.
    """
    if not analysis or not variables:
        return (None, None)
    if not analysis.get("filesorts"):
        return (None, None)
    sb = _to_int(variables.get("sort_buffer_size"))
    if sb <= 0 or sb >= 2 * 1024 * 1024:
        return (None, None)
    warn = (
        "Filesort detected but sort_buffer_size is only {} — the sort will "
        "likely spill to disk."
    ).format(_human_bytes(sb))
    sug = (
        "Raise sort_buffer_size to 2M–8M for this session "
        "(SET SESSION sort_buffer_size = 8*1024*1024). "
        "Why: when the sort set does not fit in sort_buffer_size, MySQL "
        "writes multiple sorted runs to tmpdir and merges them back — that "
        "is real disk I/O plus a k-way merge. A buffer big enough to hold "
        "the result stays in memory and uses the faster in-RAM sort. "
        "Caveat: sort_buffer_size is allocated per-connection, so set it "
        "per-session rather than globally to avoid multiplying RAM cost "
        "across all threads."
    )
    return (warn, sug)


def _rule_join_buffer_vs_hash_or_bnl(analysis, schema, stats, variables):
    """Hash join / BNL relies on ``join_buffer_size``. Warn if the plan has
    one and the variable is still at the default 256 KB."""
    if not analysis or not variables:
        return (None, None)
    has_hash = bool(analysis.get("hash_joins"))
    has_bnl = bool(analysis.get("bnl_nodes"))
    if not (has_hash or has_bnl):
        return (None, None)
    jb = _to_int(variables.get("join_buffer_size"))
    if jb <= 0 or jb >= 2 * 1024 * 1024:
        return (None, None)
    kind = "Hash join" if has_hash else "Block Nested-Loop"
    warn = (
        "{} detected but join_buffer_size is only {}. Build/batch phase may "
        "spill to tmpdir."
    ).format(kind, _human_bytes(jb))
    if has_hash:
        why = (
            "Why: hash join builds an in-memory hash table on the smaller "
            "input and probes from the other side. If the build side does "
            "not fit in join_buffer_size, MySQL spills to tmpdir and does a "
            "multi-pass (Grace) hash join — each extra pass re-reads the "
            "probe side, so total I/O roughly multiplies."
        )
    else:
        why = (
            "Why: BNL scans the inner table once per outer batch that fits "
            "in the join buffer. A tiny buffer means more batches, and each "
            "batch triggers another full scan of the inner side."
        )
    sug = (
        "Raise join_buffer_size per-session to 2M–8M "
        "(SET SESSION join_buffer_size = 8*1024*1024). "
        + why + " "
        "Bigger win: add an index on the join column — that removes the "
        "need for the join buffer entirely by turning the join into an "
        "indexed lookup."
    )
    return (warn, sug)


def _rule_tmp_table_size_vs_materialize(analysis, schema, stats, variables):
    """Materialized subqueries / derived tables use ``tmp_table_size`` /
    ``max_heap_table_size``. If both are small, the temp table will spill
    from MEMORY to InnoDB tmp."""
    if not analysis or not variables:
        return (None, None)
    if not analysis.get("temp_tables"):
        return (None, None)
    tmp = _to_int(variables.get("tmp_table_size"))
    heap = _to_int(variables.get("max_heap_table_size"))
    smallest = min(tmp, heap) if tmp and heap else max(tmp, heap)
    if smallest <= 0 or smallest >= 32 * 1024 * 1024:
        return (None, None)
    warn = (
        "Query materializes {} temp table(s) but tmp_table_size/"
        "max_heap_table_size is capped at {} — temp tables will spill to "
        "InnoDB disk tables."
    ).format(len(analysis["temp_tables"]), _human_bytes(smallest))
    sug = (
        "Raise tmp_table_size AND max_heap_table_size together to the "
        "same value (64M–256M). Why: once a materialized temp table "
        "exceeds min(tmp_table_size, max_heap_table_size), MySQL converts "
        "the in-memory MEMORY/TempTable engine to an on-disk InnoDB temp "
        "table — scans over the disk version are typically 10–100× slower "
        "and generate real I/O. Both variables must be raised together "
        "because MySQL always picks the smaller of the two; raising only "
        "one has no effect."
    )
    return (warn, sug)


def _rule_optimizer_switch_disables(analysis, schema, stats, variables):
    """If a switch the plan would otherwise benefit from is turned OFF
    in session variables, point it out.

    Two concrete cases:
    - hash_join=off but plan has a BNL → turning on hash_join lets the
      optimizer pick hash join instead on MySQL 8.0.20+.
    - mrr=off but plan has a range scan on a secondary index → enabling
      MRR usually helps on spinning disks / large ranges.
    """
    if not analysis or not variables:
        return []
    sw = (variables.get("optimizer_switch") or "").lower()
    if not sw:
        return []
    pairs = dict(
        tuple(p.split("=", 1)) for p in sw.split(",") if "=" in p
    )
    results = []
    if analysis.get("bnl_nodes") and pairs.get("hash_join") == "off":
        results.append((
            "optimizer_switch has hash_join=off but the plan uses Block "
            "Nested-Loop — enabling hash_join would let the optimizer pick "
            "hash join instead (MySQL 8.0.20+).",
            "SET SESSION optimizer_switch='hash_join=on,block_nested_loop=off'; "
            "Why: Block Nested-Loop scans the inner table once per outer "
            "batch, so its cost grows with the outer side (O(outer/batch × "
            "inner)). Hash join reads each side exactly once (build + "
            "probe), so for joins without a usable index its total I/O is "
            "typically far lower — especially when the inner table is big."
        ))
    if analysis.get("filesorts") and pairs.get("mrr") == "off":
        # MRR only helps ranges but filesorts often follow them; advise
        # conservatively with a "consider" suggestion.
        results.append((
            "optimizer_switch has mrr=off; range scans followed by filesort "
            "may read rows in random order.",
            "Consider SET SESSION optimizer_switch='mrr=on,mrr_cost_based=on'; "
            "Why: Multi-Range Read collects row IDs from a secondary-index "
            "range scan, sorts them by primary key, and then fetches rows "
            "in (mostly) physical order. Without MRR, InnoDB has to "
            "dereference each secondary-index row in secondary-index order, "
            "which translates to random I/O on the clustered index and "
            "hurts both disk and buffer-pool cache locality."
        ))
    if pairs.get("derived_condition_pushdown") == "off" and analysis.get("temp_tables"):
        results.append((
            "optimizer_switch has derived_condition_pushdown=off but the "
            "plan materializes derived tables — pushing predicates could "
            "reduce the materialized row count.",
            "SET SESSION optimizer_switch='derived_condition_pushdown=on'; "
            "Why: with pushdown enabled, outer-query predicates are "
            "evaluated inside the derived/CTE materialization so fewer "
            "rows are ever stored in the temp table. That can turn a "
            "'materialize-everything-then-filter' plan into a much smaller "
            "'filter-then-materialize' plan."
        ))
    return results


def _rule_missing_indexes(analysis, schema, stats, variables):
    """Cross-check the parser's heuristic index_suggestions against the
    actual indexes present in the collected schema. Only emit an
    environment-level suggestion if the suggested columns are NOT already
    the leading key of an existing index.
    """
    if not analysis or not schema:
        return []
    suggestions = analysis.get("index_suggestions") or []
    out = []
    for hint in suggestions:
        table = hint.get("table") or ""
        cols = tuple((hint.get("columns") or [])[:3])
        if not table or not cols:
            continue
        # schema keys are either ``table`` or ``schema.table`` — match both.
        parsed = None
        for key, p in schema.items():
            if key == table or key.endswith("." + table):
                parsed = p
                break
        if not parsed:
            continue
        already_covered = False
        for idx in parsed.get("indexes", []):
            idx_cols = tuple(c.lower() for c in (idx.get("columns") or []))
            if idx_cols[:len(cols)] == tuple(c.lower() for c in cols):
                already_covered = True
                break
        if already_covered:
            continue  # heuristic already satisfied — don't nag
        out.append((
            "Table {} has no index covering ({}).".format(table, ", ".join(cols)),
            hint.get("ddl") or "",
        ))
    return out


def _rule_engine_innodb(analysis, schema, stats, variables):
    """Warn if a touched table is MyISAM/MEMORY etc. Modern tuning advice
    assumes InnoDB — using MyISAM on MySQL 8+ is almost always a mistake."""
    if not schema:
        return []
    out = []
    for name, parsed in schema.items():
        engine = (parsed.get("engine") or "").upper()
        if engine and engine not in ("INNODB", "ARIA"):
            out.append((
                "Table {} uses storage engine {} — most tuning advice "
                "(buffer pool, row locking, crash safety) assumes InnoDB."
                .format(name, engine),
                "ALTER TABLE {} ENGINE=InnoDB; Why: {} predates modern "
                "MySQL assumptions — no row-level locking (only table "
                "locks, so writers block readers), no transactions or "
                "crash recovery (a crash mid-write corrupts the table), "
                "no foreign keys, and no clustered index. InnoDB gives "
                "you all of these plus the buffer pool tunables every "
                "other recommendation assumes."
                .format(name, engine),
            ))
    return out


def _rule_flush_log_durability(analysis, schema, stats, variables):
    """Not directly query-related but commonly misconfigured: warn if
    ``innodb_flush_log_at_trx_commit`` is not 1 — the query plan may look
    fast but writes are not durable on crash.

    We only surface this for UPDATE/DELETE/INSERT plans. SELECT-only plans
    don't care.
    """
    if not analysis or not variables:
        return (None, None)
    features = " ".join(analysis.get("optimizer_features") or []).lower()
    # The analyze_plan output doesn't say "this is an UPDATE" explicitly,
    # but the SQL text (query_text_lines) usually does.
    qlines = " ".join(analysis.get("query_text_lines") or []).upper()
    is_write = any(kw in qlines for kw in ("UPDATE ", "DELETE ", "INSERT "))
    if not is_write:
        return (None, None)
    val = _to_int(variables.get("innodb_flush_log_at_trx_commit"), default=1)
    if val == 1:
        return (None, None)
    return (
        "innodb_flush_log_at_trx_commit={} — writes are not fsynced on "
        "COMMIT; a crash can lose the last second of transactions.".format(val),
        "SET GLOBAL innodb_flush_log_at_trx_commit=1; "
        "Why: with value 1, every COMMIT flushes and fsyncs the redo log, "
        "which is what makes the D (Durability) in ACID actually true — "
        "a successfully committed transaction survives power loss. "
        "Values 2 and 0 batch fsyncs to save I/O but can lose up to the "
        "last ~1s of transactions on OS crash or power loss. Only leave "
        "it at {} if the data is reproducible (caches, metrics, derived "
        "rollups) and you are explicitly OK with losing recent commits."
        .format(val),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

#: Every rule. Functions that return a tuple yield at most one pair;
#: functions that return a list yield zero or more. :func:`advise` handles
#: both shapes.
_RULES = (
    _rule_buffer_pool_vs_data_size,
    _rule_sort_buffer_vs_filesort,
    _rule_join_buffer_vs_hash_or_bnl,
    _rule_tmp_table_size_vs_materialize,
    _rule_optimizer_switch_disables,
    _rule_missing_indexes,
    _rule_engine_innodb,
    _rule_flush_log_durability,
)


def advise(analysis, schema=None, stats=None, variables=None):
    """Run all rules and extend *analysis* in-place.

    Adds:
      * ``analysis["environment_warnings"]`` — list[str]
      * ``analysis["environment_suggestions"]`` — list[str]
      * ``analysis["collected_variables"]`` — the filtered session variables
        (echoed so the renderer can show them in the info panel).
    """
    warnings = []
    suggestions = []
    for rule in _RULES:
        result = rule(analysis, schema, stats, variables)
        if result is None:
            continue
        if isinstance(result, tuple) and len(result) == 2:
            results = [result]
        elif isinstance(result, list):
            results = result
        else:
            continue
        for w, s in results:
            if w:
                warnings.append(w)
            if s:
                suggestions.append(s)

    analysis["environment_warnings"] = warnings
    analysis["environment_suggestions"] = suggestions
    analysis["collected_variables"] = dict(variables or {})
    analysis["collected_schema"] = dict(schema or {})
    analysis["collected_stats"] = dict(stats or {})
    return analysis
