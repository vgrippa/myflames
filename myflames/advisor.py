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

from .server_info import detect_flavor


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

def buffer_pool_coverage(buffer_pool_bytes, data_bytes):
    """How the buffer pool compares with *data_bytes*: ``"much smaller than"``
    below a quarter, ``"smaller than"`` below half, else None (or when either
    size is unknown). Shared with config_review's server-wide check.
    """
    if buffer_pool_bytes <= 0 or data_bytes <= 0:
        return None
    ratio = float(buffer_pool_bytes) / data_bytes
    if ratio < 0.25:
        return "much smaller than"
    if ratio < 0.5:
        return "smaller than"
    return None


def _rule_buffer_pool_vs_data_size(analysis, schema, stats, variables):
    """Compare the buffer pool with table allocation, not measured hot pages."""
    if not variables or not stats:
        return (None, None)
    bp = _to_int(variables.get("innodb_buffer_pool_size"))
    if bp <= 0:
        return (None, None)
    total = sum(
        _to_int(s.get("data_length")) + _to_int(s.get("index_length"))
        for s in stats.values()
    )
    severity = buffer_pool_coverage(bp, total)
    if severity is None:
        return (None, None)
    warn = (
        "innodb_buffer_pool_size ({}) is {} the combined data and index "
        "size of the referenced tables ({}). The query's working set may "
        "be much smaller."
    ).format(_human_bytes(bp), severity, _human_bytes(total))
    sug = (
        "Measure buffer-pool reads and the workload's hot pages before "
        "changing innodb_buffer_pool_size. Why: data_length + index_length "
        "describes table allocation, while an indexed query may read only "
        "a few pages. A larger pool can reduce reads when hot pages are "
        "being evicted; size it within the server's available memory."
    )
    return (warn, sug)


def _captured_sort_merge_passes(context):
    """Sort merge passes a capture measured for the statement, or None:
    ``statistics.sort_merge_passes`` (events_statements_history), else the
    ``Sort_merge_passes`` session status delta; the largest over its runs."""
    capture = (context or {}).get("capture") or {}
    # An estimated capture (EXPLAIN) never runs the statement: its status
    # deltas are the EXPLAIN's own, so they prove nothing about the sorts.
    if capture.get("mode") != "analyze":
        return None
    measured = []
    for run in capture.get("runs") or []:
        if not isinstance(run, dict):
            continue
        value = (run.get("statistics") or {}).get("sort_merge_passes")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            value = (run.get("session_status_delta") or {}).get("Sort_merge_passes")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            measured.append(value)
    return max(measured) if measured else None


def _sort_merge_passes(analysis, context):
    """Merge passes measured for this query's sorts, or None when unknown.

    A sort that does not fit in sort_buffer_size writes sorted chunks to a
    temporary file and merges them; every merge pass increments
    Sort_merge_passes (MySQL filesort.cc merge_buffers; MariaDB
    filesort.cc), and a sort that fits in memory makes none (MySQL
    filesort.cc: "The whole set is in memory"). Evidence, in order: the
    capture's per-statement Performance Schema counter
    (``statistics.sort_merge_passes``), its session status delta, then
    MariaDB ANALYZE ``r_sort_passes`` kept on the plan's sorts.
    """
    captured = _captured_sort_merge_passes(context)
    if captured is not None:
        return captured
    plan = [f.get("merge_passes") for f in (analysis or {}).get("filesorts") or []]
    if plan and all(isinstance(p, (int, float)) for p in plan):
        return sum(plan)
    return None


def _rule_sort_buffer_vs_filesort(analysis, schema, stats, variables, context=None):
    """A small sort buffer is a tuning candidate, not a proven spill.

    With measured merge passes (:func:`_sort_merge_passes`) the rule says
    what happened: passes mean the sort merged runs from disk; none mean it
    fit in memory, so there is nothing to tune. Without them it only notes
    the buffer size.
    """
    if not analysis or not analysis.get("filesorts"):
        return (None, None)
    passes = _sort_merge_passes(analysis, context)
    sb = _to_int((variables or {}).get("sort_buffer_size"))
    if passes is not None and passes > 0:
        warn = (
            "Filesort merged sorted runs from disk: {:,} sort merge pass(es) "
            "for this query{}."
        ).format(int(passes), "; sort_buffer_size is {}".format(_human_bytes(sb)) if sb > 0 else "")
        sug = (
            "Test a larger sort_buffer_size for this session and compare "
            "Sort_merge_passes, or add an index that returns rows in ORDER BY "
            "order. Why: the rows to sort did not fit in the sort buffer, so "
            "the server wrote sorted runs to a temporary file and merged them "
            "back. Account for concurrent sorting sessions before changing the "
            "global default."
        )
        return (warn, sug)
    if passes == 0 or not variables:
        # Measured: the sort fit in memory. Or nothing to compare against.
        return (None, None)
    if sb <= 0 or sb >= 2 * 1024 * 1024:
        return (None, None)
    warn = (
        "Filesort detected with sort_buffer_size={}. Whether it spills "
        "depends on the sort's row count and record size."
    ).format(_human_bytes(sb))
    sug = (
        "Check sort merge passes and query timing before increasing "
        "sort_buffer_size for this session. Why: when sort records exceed "
        "the buffer, MySQL can write sorted runs and merge them. A larger "
        "buffer may avoid that work; small sorts may already fit in memory. Test "
        "session-level changes and account for concurrent sorting sessions "
        "before changing the global default."
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
    if detect_flavor(variables.get("version"), variables.get("version_comment")) == "mariadb":
        # MariaDB BNL and BNLH keep the join buffer in memory: when it is
        # full the inner table is read for the buffered rows and the buffer
        # is refilled (one r_scans per refill, join_matching_records in
        # sql/sql_join_cache.cc). Nothing spills to tmpdir.
        warn = (
            "{} detected with join_buffer_size={}. MariaDB reads the inner "
            "table once per join-buffer refill."
        ).format(kind, _human_bytes(jb))
        sug = (
            "Raise join_buffer_size per-session to 2M–8M "
            "(SET SESSION join_buffer_size = 8*1024*1024) if the inner "
            "table's r_loops shows many refills. Why: MariaDB fills the join "
            "buffer with outer rows (hashed for BNLH) and reads the inner "
            "table once per refill, so a larger buffer means fewer reads of "
            "the inner table. Bigger win: add an index on the join column so "
            "the join becomes an indexed lookup."
        )
        return (warn, sug)
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
    """Report a small configured limit without claiming an observed spill.

    MySQL TempTable uses tmp_table_size, whereas MEMORY (and MariaDB's
    in-memory internal tables) uses min(tmp_table_size, max_heap_table_size).
    """
    if not analysis or not variables or not analysis.get("temp_tables"):
        return (None, None)
    tmp = _to_int(variables.get("tmp_table_size"))
    heap = _to_int(variables.get("max_heap_table_size"))
    engine = (variables.get("internal_tmp_mem_storage_engine") or "").upper()
    is_mariadb = detect_flavor(variables.get("version"), variables.get("version_comment")) == "mariadb"
    uses_memory = engine == "MEMORY" or is_mariadb
    present = [v for v in (tmp, heap) if v > 0] if uses_memory else [tmp]
    limit = min(present) if present else 0
    if limit <= 0 or limit >= 32 * 1024 * 1024:
        return (None, None)
    # MEMORY (and MariaDB's in-memory internal tables) use the smaller of
    # the two limits; MySQL TempTable uses tmp_table_size only.
    setting = ("the smaller of tmp_table_size and max_heap_table_size" if uses_memory
               else "tmp_table_size")
    warn = (
        "Query materializes {} temp table(s); {} is {}. "
        "This limit alone does not show whether a table spilled to disk."
    ).format(len(analysis["temp_tables"]), setting, _human_bytes(limit))
    if uses_memory:
        sug = (
            "Check temporary-table disk usage before increasing tmp_table_size "
            "and max_heap_table_size to the same value for this session. "
            "Why: MEMORY internal tables and MariaDB use the smaller of these "
            "two limits; exceeding it can convert a table to on-disk storage. "
            "Account for concurrent sessions before increasing the limits."
        )
    else:
        sug = (
            "Check the internal temporary-table engine and disk usage before "
            "increasing tmp_table_size for this session. Why: MySQL 8.0.28+ "
            "TempTable uses tmp_table_size as its per-table limit; "
            "max_heap_table_size does not limit TempTable. Shared TempTable "
            "RAM and mmap limits also affect disk usage. If the internal "
            "engine is MEMORY, the smaller of tmp_table_size and "
            "max_heap_table_size applies instead."
        )
    return (warn, sug)


def _rule_optimizer_switch_disables(analysis, schema, stats, variables):
    """Flag optimizer_switch settings whose OFF value matters for *this* plan.

    Engine detection: MariaDB's optimizer_switch includes keys like
    ``join_cache_hashed`` that don't exist on MySQL. MySQL's ``hash_join``
    switch, on the other hand, is defined in sys_vars.cc but never checked
    in the 8.0.20+ planner/executor — it is a no-op kept for compatibility.
    Recommending ``hash_join=on`` on MySQL is therefore meaningless.

    Rules:
      - BNL present on MariaDB with ``join_cache_hashed=off`` → flip it on.
      - BNL present on MySQL → the executor already rewrites BNL to a hash
        join at execution time, so the real fix is an index or more join
        buffer; do not recommend any optimizer_switch change.
      - MRR present on a secondary-index range scan with ``mrr=off`` → advise
        ``mrr=on``: with ``mrr=off`` MRR is globally disabled and
        ``mrr_cost_based`` has no effect, so ``mrr=on`` is the only switch that
        re-enables it. ``mrr_cost_based`` (on by default) then still gates it
        per query, so this re-enables rather than forces MRR.
      - derived_condition_pushdown=off with a materialized temp table.
    """
    if not analysis or not variables:
        return []
    sw = (variables.get("optimizer_switch") or "").lower()
    if not sw:
        return []
    pairs = dict(
        tuple(p.split("=", 1)) for p in sw.split(",") if "=" in p
    )
    is_mariadb = "join_cache_hashed" in pairs
    results = []

    if analysis.get("bnl_nodes"):
        if is_mariadb and pairs.get("join_cache_hashed") == "off":
            results.append((
                "MARIADB_JOIN_CACHE_HASHED_OFF_WITH_BNL",
                "medium",
                "optimizer_switch has join_cache_hashed=off but the plan "
                "uses Block Nested-Loop — enabling join_cache_hashed lets "
                "MariaDB use a hashed join buffer (BNLH) for equi-joins.",
                "SET SESSION optimizer_switch='join_cache_hashed=on'; and "
                "ensure join_cache_level >= 3 (SET SESSION "
                "join_cache_level=4;). Why: join_cache_hashed=on is "
                "required, but it is already on by default — the switch "
                "that actually selects a hashed buffer (BNLH) is "
                "join_cache_level, which defaults to 2 (plain BNL); levels "
                "3 and 4 enable the hash table. With both in place MariaDB "
                "probes a hash table per outer batch in O(1) instead of "
                "re-scanning the inner side. Bigger win: add an index on "
                "the join column."
            ))
        elif not is_mariadb:
            # MySQL 8.0.20+: BNL is rewritten to hash join at execution
            # (sql/sql_executor.cc ~2891). The hash_join switch is a
            # no-op. Never recommend block_nested_loop=off — disabling BNL
            # in the planner also kills the BNL→hash rewrite.
            results.append((
                "MYSQL_BNL_LABEL_MISLEADING",
                "low",
                "Plan reports Block Nested-Loop, but on MySQL 8.0.20+ the "
                "executor rewrites BNL into a hash join at runtime — the "
                "label does not mean you are running a classic BNL.",
                "Do not change optimizer_switch here. "
                "Why: the hash_join switch is defined but never checked "
                "by the 8.0.20+ planner (sql/sql_const.h:221 vs "
                "sql/sql_optimizer.cc — zero usage sites), so turning it "
                "on changes nothing; and setting block_nested_loop=off "
                "disables the planner's join-buffering path, which is "
                "exactly what triggers the BNL→hash rewrite at execution "
                "time (sql/sql_executor.cc:~2891). The real win is an "
                "index on the join column (which converts the join into "
                "eq_ref / ref lookup); if no index is possible, raise "
                "join_buffer_size so the hash build side fits in memory "
                "and avoids a Grace (multi-pass) hash."
            ))

    # MRR: gate on an actual secondary-index range scan in the plan.
    # Without a range scan, MRR has nothing to reorder, so the classic
    # "mrr=off hurts you" advice does not apply. On SSDs the cost-based
    # gate usually decides MRR is not worth it even when enabled — only
    # recommend turning on mrr_cost_based, never force mrr itself.
    range_scans = analysis.get("range_scans") or []
    if range_scans and pairs.get("mrr") == "off":
        results.append((
            "MRR_OFF_WITH_RANGE_SCAN",
            "low",
            "optimizer_switch has mrr=off and the plan has a range scan "
            "on a secondary index — fetching rows in secondary-index "
            "order hits the clustered index randomly.",
            "Consider SET SESSION optimizer_switch='mrr=on'; "
            "Why: Multi-Range Read collects row IDs from a secondary-"
            "index range scan, sorts them by primary key, and fetches "
            "rows in (mostly) physical order. On rotational disks this "
            "converts random I/O into sequential I/O. With mrr=off MRR is "
            "globally disabled and mrr_cost_based has no effect, so the "
            "only switch that re-enables MRR is mrr=on. That is not the "
            "same as forcing it: mrr_cost_based is on by default, so once "
            "mrr=on the cost-based gate still decides per query whether "
            "MRR is worth it. On SSDs the gate often decides it is not."
        ))

    if pairs.get("derived_condition_pushdown") == "off" and analysis.get("temp_tables"):
        results.append((
            "DERIVED_CONDITION_PUSHDOWN_OFF",
            "medium",
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


def flush_log_durability(value):
    """What a non-durable ``innodb_flush_log_at_trx_commit`` loses on a crash,
    or None for 1 and MariaDB's 3, which make each commit durable (MariaDB
    syncs at prepare for both; 3 also syncs at commit when the binary log is
    on). Shared with config_review's durability check.
    """
    # Durability semantics (verified in storage/innobase/handler/ha_innodb.cc
    # around line 5896 and trx/trx0trx.cc): with value 2 the redo log is
    # written to the OS file at each commit but only fsynced once per
    # second, so a mysqld crash keeps the last-second commits (the OS
    # buffer cache still holds them) while an OS crash or power loss
    # drops them. With value 0 the redo log is NOT even written at
    # commit — the background thread writes+fsyncs once per second, so
    # a mysqld crash drops the last ~1s of transactions, too.
    if value == 2:
        return (
            "writes go to the OS file at each COMMIT but are only "
            "fsynced once per second. A mysqld crash is survivable "
            "(the OS buffer cache still holds the last-second "
            "commits), but an OS crash or power loss drops them."
        )
    if value == 0:
        return (
            "writes are neither flushed nor fsynced at COMMIT: the "
            "background thread writes and fsyncs once per second, so ANY "
            "crash, even a plain mysqld crash, drops about the last "
            "second of transactions."
        )
    return None


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
    durability = flush_log_durability(val)
    if durability is None:
        return (None, None)
    rule_id = "FLUSH_LOG_COMMIT_2" if val == 2 else "FLUSH_LOG_COMMIT_0"
    return (
        rule_id,
        "high",
        "innodb_flush_log_at_trx_commit={} — {}".format(val, durability),
        "SET GLOBAL innodb_flush_log_at_trx_commit=1; "
        "Why: with value 1, every COMMIT flushes and fsyncs the redo log, "
        "which is what makes the D (Durability) in ACID actually true — "
        "a successfully committed transaction survives both a mysqld "
        "crash and power loss. Only leave it at {} if the data is "
        "reproducible (caches, metrics, derived rollups) and you are "
        "explicitly OK with the durability loss described above."
        .format(val),
    )


def _lookup(mapping, table):
    """Find *table* (bare or ``schema.table``) in a ``{fq: value}`` map."""
    for key, value in (mapping or {}).items():
        if key == table or key.endswith("." + table):
            return key, value
    return None, None


def _rule_stale_statistics(analysis, schema, stats, variables, context):
    """Add statistics freshness evidence to each misestimated table.

    The plan-level row-estimate warning already suggests ANALYZE TABLE; this
    rule reports when InnoDB persistent statistics were last recalculated
    (``mysql.innodb_table_stats.last_update``) and which columns have
    histograms, so the reader can tell stale statistics from a missing
    histogram. Timestamps are server-local; no age is computed.
    """
    freshness = context.get("freshness") or {}
    if not freshness:
        return []
    out = []
    seen = set()
    for miss in analysis.get("misestimates") or []:
        key, entry = _lookup(freshness, miss.get("table") or "")
        if not entry or key in seen:
            continue
        seen.add(key)
        if not isinstance(entry, dict):
            continue
        facts = []
        if entry.get("last_update"):
            facts.append("InnoDB statistics last updated {}{}".format(
                entry["last_update"],
                " (n_rows {:,})".format(entry["n_rows"]) if isinstance(entry.get("n_rows"), int) else ""))
        histograms = entry.get("histograms") or []
        if not isinstance(histograms, list):
            histograms = []
        facts.append("histograms on " + ", ".join(histograms) if histograms
                     else "no column histograms")
        out.append(("STALE_STATS", "medium",
                     "Statistics for misestimated table {}: {}.".format(key, "; ".join(facts)),
                     ""))
    return out


_DROPPABLE_KINDS = ("KEY", "INDEX")
_BTREE_KINDS = ("KEY", "INDEX", "UNIQUE KEY", "UNIQUE", "UNIQUE INDEX", "PRIMARY KEY")


def _index_signature(index):
    """``((name, length, desc), ...)`` for a plain BTREE index, else None.

    FULLTEXT, SPATIAL, HASH, and functional key parts are excluded, matching
    sys.schema_redundant_indexes (BTREE only). Unlike sys, prefix lengths and
    sort direction are compared, so ``a(10)`` never stands in for ``a``.
    """
    if (index.get("kind") or "").upper() not in _BTREE_KINDS or index.get("using") == "HASH":
        return None
    parts = index.get("parts")
    if parts is None:
        parts = [{"name": c, "length": None, "desc": False, "expression": False}
                 for c in index.get("columns") or []]
    if not parts or any(part.get("expression") for part in parts):
        return None
    return tuple(((part["name"] or "").lower(), part.get("length"), bool(part.get("desc")))
                 for part in parts)


def _ident(name):
    """Backquote an identifier for copy-paste SQL."""
    return "`" + (name or "").replace("`", "``") + "`"


def _qualified(fq):
    return ".".join(_ident(part) for part in fq.split(".", 1))


def _index_text(index):
    name = index.get("name") or ("PRIMARY" if "PRIMARY" in (index.get("kind") or "") else "?")
    cols = []
    for part in index.get("parts") or [{"name": c} for c in index.get("columns") or []]:
        col = part["name"]
        if part.get("length"):
            col += "({})".format(part["length"])
        if part.get("desc"):
            col += " DESC"
        cols.append(col)
    return "{} ({})".format(name, ", ".join(cols))


def find_redundant_indexes(indexes):
    """``[(index, dominant, reason), ...]`` for one table's *indexes*.

    *indexes* use the ``parse_show_create_table`` shape (``kind``, ``name``,
    ``parts`` or ``columns``, ``using``) in table order; the order decides
    which of two identical indexes is reported. *reason* is
    ``"duplicates"`` or ``"is a leftmost prefix of"``. The rules (BTREE key
    parts only; prefix lengths and direction compared; PRIMARY KEY never
    flagged; UNIQUE only as an exact duplicate of another unique index) are
    the ones :func:`_rule_redundant_indexes` and ``myflames indexes`` share.
    """
    out = []
    indexes = [(i, _index_signature(i)) for i in indexes]
    indexes = [(i, sig) for i, sig in indexes if sig]
    for pos, (index, sig) in enumerate(indexes):
        kind = (index.get("kind") or "").upper()
        if "PRIMARY" in kind:
            continue
        unique = kind.startswith("UNIQUE")
        match = None
        # An exact duplicate is the clearer explanation, so look for one
        # before falling back to a longer index that starts the same way.
        for want_duplicate in (True, False):
            for other_pos, (other, other_sig) in enumerate(indexes):
                if other is index:
                    continue
                other_unique = "UNIQUE" in (other.get("kind") or "").upper() \
                    or "PRIMARY" in (other.get("kind") or "").upper()
                if want_duplicate and other_sig == sig and (
                        (other_unique and not unique)
                        or (other_unique == unique and other_pos < pos)):
                    match = (other, "duplicates")
                elif not want_duplicate and not unique and len(other_sig) > len(sig) \
                        and other_sig[:len(sig)] == sig:
                    match = (other, "is a leftmost prefix of")
                if match:
                    break
            if match:
                break
        if match:
            out.append((index, match[0], match[1]))
    return out


def _rule_redundant_indexes(analysis, schema, stats, variables, context):
    """Flag indexes made redundant by another index on the same table.

    A non-unique index is redundant when its key parts are a leftmost prefix
    of another BTREE index, or when another index has the same key parts and
    is unique (or appears first). PRIMARY KEY is never flagged, and a UNIQUE
    index is flagged only as an exact duplicate of another unique index,
    because it also enforces a constraint. Also reports when the advisor's
    own CREATE INDEX suggestion would make an existing index redundant.
    """
    if not schema:
        return []
    out = []
    for fq, parsed in sorted(schema.items()):
        # MySQL prints USING HASH only when it was explicit, so a MEMORY
        # table's default HASH index would look like BTREE here.
        if (parsed.get("engine") or "").upper() in ("MEMORY", "HEAP"):
            continue
        for index, other, reason in find_redundant_indexes(parsed.get("indexes") or []):
            table = _qualified(fq)
            out.append((
                "REDUNDANT_INDEX", "medium",
                "Redundant index on {}: {} {} {}.".format(
                    fq, _index_text(index), reason, _index_text(other)),
                "ALTER TABLE {} DROP INDEX {}. Why: {} serves every lookup "
                "that {} serves, and each extra index slows writes and uses "
                "buffer pool memory. First check for FORCE INDEX or USE INDEX "
                "hints that name it, or test without it by making it "
                "invisible (MySQL 8.0+: ALTER TABLE {} ALTER INDEX {} "
                "INVISIBLE; MariaDB 10.6+: ALTER TABLE {} ALTER INDEX {} "
                "IGNORED).".format(
                    table, _ident(index.get("name")), other.get("name") or "PRIMARY",
                    index.get("name"), table, _ident(index.get("name")), table,
                    _ident(index.get("name"))),
            ))
    for hint in analysis.get("index_suggestions") or []:
        key, parsed = _lookup(schema, hint.get("table") or "")
        cols = tuple(c.lower() for c in hint.get("columns") or [])
        if not parsed or len(cols) < 2:
            continue
        for index, sig in ((i, _index_signature(i)) for i in parsed.get("indexes") or []):
            if not sig or (index.get("kind") or "").upper() not in _DROPPABLE_KINDS:
                continue
            plain = tuple(name for name, length, desc in sig if length is None and not desc)
            if len(plain) == len(sig) and len(sig) < len(cols) and cols[:len(sig)] == plain:
                out.append((
                    "REDUNDANT_INDEX", "low", "",
                    "After you create the suggested index on {} ({}), drop {}: "
                    "ALTER TABLE {} DROP INDEX {}. Why: the new index starts with "
                    "the same columns, so {} would add write cost without serving "
                    "any lookup the new index cannot.".format(
                        hint["table"], ", ".join(hint["columns"]), _index_text(index),
                        _qualified(key), _ident(index.get("name")), index.get("name")),
                ))
    return out


#: Fewest pages run 1 must read from disk before a cold buffer pool is
#: worth mentioning (100 pages = 1.6 MB with 16 KB pages).
COLD_CACHE_MIN_PAGES = 100


def _rule_cold_cache(analysis, schema, stats, variables, context):
    """Report when run 1 of a repeated capture read many pages from disk and
    later runs did not, which is consistent with a cold buffer pool.

    ``Innodb_buffer_pool_reads`` is server-wide (on MariaDB it also counts
    read-ahead pages; on MySQL ``Innodb_buffer_pool_read_ahead`` is added),
    so the wording stays at "consistent with", never proof.
    """
    capture = context.get("capture") or {}
    runs = [r for r in capture.get("runs") or [] if isinstance(r, dict)]
    if capture.get("mode") != "analyze" or len(runs) < 2:
        return []

    mysql = (capture.get("engine") or "mysql") != "mariadb"

    def reads(run):
        delta = run.get("session_status_delta") or {}
        value = delta.get("Innodb_buffer_pool_reads")
        if not isinstance(value, (int, float)):
            return None
        # MySQL counts linear read-ahead pages only in
        # Innodb_buffer_pool_read_ahead (buf0rea.cc), so a cold sequential
        # scan looks almost free without it. MariaDB's reads counter already
        # includes read-ahead, so adding it there would double count.
        ahead = delta.get("Innodb_buffer_pool_read_ahead")
        if mysql and isinstance(ahead, (int, float)):
            value += ahead
        return value

    def median(values):
        values = sorted(values)
        mid = len(values) // 2
        return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2.0

    first_reads = reads(runs[0])
    later_reads = [reads(r) for r in runs[1:]]
    first_ms = runs[0].get("total_time_ms")
    later_ms = [r.get("total_time_ms") for r in runs[1:]]
    if first_reads is None or None in later_reads or first_ms is None \
            or any(not isinstance(ms, (int, float)) for ms in later_ms):
        return []
    warm_reads, warm_ms = median(later_reads), median(later_ms)
    if first_reads < COLD_CACHE_MIN_PAGES or warm_reads > first_reads * 0.1 \
            or first_ms < warm_ms * 1.5:
        return []
    return [("COLD_CACHE", "low",
             "Run 1 read {:,} pages from disk in {:,.1f} ms; runs 2\u2013{} read "
             "about {:,.0f} pages in {:,.1f} ms. This is consistent with a cold "
             "buffer pool on run 1. The counters are server-wide, so other "
             "sessions and background page reads can add to them.".format(
                 int(first_reads), first_ms, len(runs), warm_reads, warm_ms),
             "Judge the plan by the warm runs, or run the query once before "
             "measuring. Why: a run that reads from disk measures storage "
             "latency as much as the plan. If production also starts cold, "
             "check whether the buffer pool holds this working set.")]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

#: Every rule. Functions that return a tuple yield at most one pair;
#: functions that return a list yield zero or more. :func:`advise` handles
#: both shapes.
_RULES = (
    _rule_buffer_pool_vs_data_size,
    _rule_join_buffer_vs_hash_or_bnl,
    _rule_tmp_table_size_vs_materialize,
    _rule_optimizer_switch_disables,
    _rule_missing_indexes,
    _rule_engine_innodb,
    _rule_flush_log_durability,
)

#: Rules that also read live-capture context (``freshness``, ``capture``).
_CONTEXT_RULES = (
    _rule_sort_buffer_vs_filesort,
    _rule_stale_statistics,
    _rule_redundant_indexes,
    _rule_cold_cache,
)

#: Sidecar category for environment findings whose rule has its own entry in
#: the rule catalog (findings.RULES). Other environment rules stay ``env``.
RULE_CATEGORIES = {
    "STALE_STATS": ("misestimate", "warn", "statistics"),
    "REDUNDANT_INDEX": ("redundant_index", "warn", "index"),
    "COLD_CACHE": ("cold_cache", "info", "other"),
}


def _normalize_rule_output(result):
    """Convert any rule return shape into a list of 4-tuples.

    Rules may return:
      * ``None``                                           → 0 results
      * ``(warning, suggestion)``                          → legacy 2-tuple
      * ``(rule_id, severity, warning, suggestion)``       → 4-tuple
      * a ``list`` whose items are any of the tuples above → 0..N results

    Legacy 2-tuples get synthesized ``rule_id = UNCLASSIFIED`` and
    ``severity = medium`` so the digest still has something stable.
    """
    if result is None:
        return []
    if isinstance(result, tuple):
        items = [result]
    elif isinstance(result, list):
        items = result
    else:
        return []
    out = []
    for item in items:
        if not isinstance(item, tuple):
            continue
        if len(item) == 2:
            w, s = item
            out.append(("UNCLASSIFIED", "medium", w, s))
        elif len(item) == 4:
            out.append(item)
    return out


def advise(analysis, schema=None, stats=None, variables=None, freshness=None, capture=None):
    """Run all rules and extend *analysis* in-place.

    Adds:
      * ``analysis["environment_warnings"]`` — list[str]
      * ``analysis["environment_suggestions"]`` — list[str]
      * ``analysis["environment_findings"]`` — list[dict] with
        ``rule_id`` / ``severity`` / ``warning`` / ``suggestion`` per hit.
        This is the presentation-free shape used by the advisor-digest
        golden test (Slice 1 / P2).
      * ``analysis["collected_variables"]`` — the filtered session variables
        (echoed so the renderer can show them in the info panel).

    *freshness* is :func:`myflames.collectors.collect_stats_freshness`
    output; *capture* is a ``capture-1.0`` dict whose repeated runs feed the
    cold-cache rule.
    """
    warnings = []
    suggestions = []
    findings = []
    context = {"freshness": freshness or {}, "capture": capture or {}}
    results = [rule(analysis, schema, stats, variables) for rule in _RULES]
    results += [rule(analysis, schema, stats, variables, context) for rule in _CONTEXT_RULES]
    # Sort merge passes the capture measured restate the plan's sort warning
    # and advice (0: sorted in memory; more: the advisor's advice replaces
    # the plan's).
    from .parser import restate_sorts
    restate_sorts(analysis, _captured_sort_merge_passes(context))
    sort_result = results[len(_RULES) + _CONTEXT_RULES.index(_rule_sort_buffer_vs_filesort)]
    if analysis is not None and isinstance(sort_result, tuple) and len(sort_result) == 2 \
            and sort_result[1] and sort_result[0] and sort_result[0].startswith("Filesort merged"):
        # Measured merge passes: the advisor's sort advice replaces the plan's.
        from .parser import SORT_SUGGESTION
        analysis["suggestions"] = [s for s in analysis.get("suggestions") or []
                                   if s != SORT_SUGGESTION]
    tmp = results[_RULES.index(_rule_tmp_table_size_vs_materialize)]
    if analysis is not None and isinstance(tmp, tuple) and len(tmp) == 2 and tmp[1]:
        # The server-aware temporary-table advice replaces the plan-only one.
        from .parser import TEMP_TABLE_SUGGESTION_MARIADB, TEMP_TABLE_SUGGESTION_MYSQL
        plan_only = (TEMP_TABLE_SUGGESTION_MARIADB, TEMP_TABLE_SUGGESTION_MYSQL)
        analysis["suggestions"] = [s for s in analysis.get("suggestions") or []
                                   if s not in plan_only]
    for result in results:
        for rule_id, severity, w, s in _normalize_rule_output(result):
            if w:
                warnings.append(w)
            if s:
                suggestions.append(s)
            findings.append({
                "rule_id": rule_id,
                "severity": severity,
                "warning": w or "",
                "suggestion": s or "",
            })

    analysis["environment_warnings"] = warnings
    analysis["environment_suggestions"] = suggestions
    analysis["environment_findings"] = findings
    analysis["collected_variables"] = dict(variables or {})
    analysis["collected_schema"] = dict(schema or {})
    analysis["collected_stats"] = dict(stats or {})
    return analysis
