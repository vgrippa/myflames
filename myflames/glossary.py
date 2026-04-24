"""
myflames glossary + executive summary generator.

Single source of truth for every EXPLAIN / optimizer term myflames surfaces,
and for the one-line plain-English summary that lands at the top of every
HTML report and in every sidecar's ``executive_summary`` field.

Each glossary entry has THREE tiers of explanation so the HTML template can
pick the right one per audience:

    * ``short``     — 6–10 words, fits in a tooltip.
    * ``technical`` — cost-model grade, cites the actual signal MySQL emits
                      and the tunable that controls it. This is what a
                      senior DBA wants to read. Version-gated where needed.
    * ``newcomer``  — plain English, no jargon. Assumes zero prior EXPLAIN
                      knowledge. Uses analogies only when they don't distort.

The glossary is deliberately scoped to **terms myflames actually emits** —
operator names, optimizer_switch flags, tunables that advisor rules reference,
and the concepts (ICP, MRR, ROWS_EXAMINED) that appear in warnings/suggestions.
Adding speculative entries is a no — if a term can't appear in a myflames
output, it shouldn't be here.

Version-gating policy: when behaviour differs between engines or versions,
the ``technical`` text MUST state the version constraint explicitly
("MySQL 8.0.20+", "MariaDB 11.4 default OFF") rather than making a blanket
claim.
"""
from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# Glossary
# ---------------------------------------------------------------------------

GLOSSARY = {
    # ---- Access paths ----------------------------------------------------
    "full_table_scan": {
        "short": "Reads every row of a table.",
        "technical": (
            "Access type ``ALL``: the storage engine returns every row of the "
            "table, applying the WHERE clause in the server layer. Cost is "
            "roughly (rows × row_evaluate_cost) + pages_read × io_block_read_cost. "
            "Signal: ``type=ALL`` in EXPLAIN, ``Handler_read_rnd_next`` climbs "
            "by the table row count. Almost always a sign of a missing index "
            "on the WHERE or join column."
        ),
        "newcomer": (
            "MySQL is reading every single row in this table, one by one, to "
            "check if it matches your WHERE clause. This is slow on big tables "
            "— an index on the filter column lets MySQL jump straight to the "
            "matching rows instead."
        ),
        "aliases": ["full table scan", "full scan", "table scan", "ALL scan"],
    },
    "index_scan": {
        "short": "Reads every entry of an index (no table row fetch).",
        "technical": (
            "Access type ``index``: full scan of a secondary index in key order. "
            "Cheaper than a table scan because the index is smaller, but still "
            "O(n). Usually a sign that the query could be satisfied by a "
            "covering index but isn't being filtered early."
        ),
        "newcomer": (
            "MySQL reads the whole index from start to finish. Better than "
            "reading the whole table, but still touches every row."
        ),
        "aliases": ["index scan", "full index scan"],
    },
    "index_range_scan": {
        "short": "Reads a contiguous slice of an index.",
        "technical": (
            "Access type ``range``: reads a bounded key range of a secondary "
            "index (e.g. ``WHERE created_at BETWEEN a AND b``). Fast when the "
            "range is narrow; degrades to a full scan when the range covers "
            "most of the index. With ``mrr=on`` the row-ID fetches can be "
            "reordered to turn random I/O into sequential."
        ),
        "newcomer": (
            "MySQL is reading a chunk of the index that matches your range "
            "filter (like a date range) instead of the whole thing. The "
            "narrower the range, the faster."
        ),
        "aliases": ["index range scan", "range scan"],
    },
    "index_lookup": {
        "short": "Direct index probe for matching rows.",
        "technical": (
            "Access type ``ref`` or ``ref_or_null``: the optimizer uses a "
            "secondary index to fetch all rows matching an equality predicate "
            "on the indexed column. Cost is O(matching_rows). Ideal access "
            "path when the filter column is indexed."
        ),
        "newcomer": (
            "MySQL uses the index to jump straight to the rows you asked for. "
            "This is the fast path — what you want for most queries."
        ),
        "aliases": ["index lookup", "ref lookup"],
    },
    "single_row_lookup": {
        "short": "Primary/unique key lookup returning one row.",
        "technical": (
            "Access type ``const`` or ``eq_ref``: MySQL uses a PRIMARY or "
            "UNIQUE key to fetch exactly one row. Near-zero cost; this is the "
            "floor of what the optimizer can do for a join."
        ),
        "newcomer": (
            "MySQL went straight to the exact row you asked for using its "
            "primary key. About as fast as MySQL can get."
        ),
        "aliases": ["single row lookup", "const lookup", "eq_ref", "primary key lookup"],
    },
    "covering_index": {
        "short": "Index contains every column the query needs — no table fetch.",
        "technical": (
            "The optimizer finds every column referenced by the query "
            "(SELECT list, WHERE, ORDER BY) in the secondary index itself, so "
            "no row fetch from the clustered index is needed. Reported in "
            "MySQL with ``Using index`` in the Extra column. In InnoDB, a "
            "secondary index always includes the primary key columns, which "
            "is why ``use_index_extensions=on`` enables more covering reads."
        ),
        "newcomer": (
            "The index has all the columns the query needs, so MySQL never "
            "has to open the main table at all. This is the fastest form of "
            "SELECT on a large table."
        ),
        "aliases": ["covering index", "Using index"],
    },
    "index_merge": {
        "short": "Combines two or more index range scans on the same table.",
        "technical": (
            "When no single composite index satisfies a multi-column predicate, "
            "the optimizer can scan each single-column index separately and "
            "combine the row IDs — union for OR, intersection for AND. The "
            "cost of each scan is paid independently plus a merge step. A "
            "composite index ``(a, b)`` is almost always faster."
        ),
        "newcomer": (
            "MySQL is using two indexes on the same table and merging their "
            "results. It works, but one index that covers both columns together "
            "would be faster."
        ),
        "aliases": ["index merge", "rowid_union", "rowid_intersection",
                    "Deduplicate rows sorted by row ID",
                    "Intersect rows sorted by row ID"],
    },
    "skip_scan": {
        "short": "Uses a composite index even without filtering its leading column.",
        "technical": (
            "Loose index scan variant where MySQL probes each distinct value "
            "of the leading key column, then range-scans the rest. Only pays "
            "off when the leading column has very low cardinality (few "
            "distinct values). Controlled by ``skip_scan`` optimizer_switch."
        ),
        "newcomer": (
            "Even though your query doesn't filter on the first column of the "
            "index, MySQL is clever enough to jump through all its distinct "
            "values one by one."
        ),
        "aliases": ["skip scan", "Covering index skip scan", "index_skip_scan"],
    },

    # ---- Join strategies -------------------------------------------------
    "nested_loop_join": {
        "short": "For each outer row, look up matches in the inner table.",
        "technical": (
            "The canonical join algorithm: MySQL reads the outer table row "
            "by row, and for each row probes the inner table (ideally via an "
            "index). Cost is O(outer × inner_probe). With an index on the "
            "join column, inner_probe is O(log n); without one, the optimizer "
            "falls back to Block Nested-Loop or hash join."
        ),
        "newcomer": (
            "For every row in the first table, MySQL looks up the matching "
            "rows in the second table. Fast when the join column is indexed."
        ),
        "aliases": ["nested loop", "nested loop join"],
    },
    "hash_join": {
        "short": "Build a hash table on one side, probe from the other.",
        "technical": (
            "MySQL 8.0.18+ (default in 8.0.20+). Reads the smaller input, "
            "builds an in-memory hash table keyed on the join column, then "
            "scans the larger input and probes the hash. Single pass over "
            "each side (O(m + n)). Uses ``join_buffer_size``; if the build "
            "side doesn't fit, MySQL spills to tmpdir and does a multi-pass "
            "(Grace) hash join, re-reading the probe side each pass."
        ),
        "newcomer": (
            "MySQL builds a lookup table in memory for the smaller of the "
            "two tables, then reads the bigger one once and uses the lookup "
            "table to find matches. Much faster than the alternative (Block "
            "Nested-Loop) when neither side has a usable index."
        ),
        "aliases": ["hash join", "Inner hash join", "Outer hash join"],
    },
    "block_nested_loop": {
        "short": "Buffers outer rows in batches, scans the inner once per batch.",
        "technical": (
            "BNL: signalled by ``Using join buffer (Block Nested Loop)`` in "
            "the Extra column. MySQL fills ``join_buffer_size`` with outer "
            "rows, then scans the inner table (type=ALL/index/range) once "
            "for the whole batch. Cost grows with the outer size: "
            "O(outer/batch × inner). On MySQL 8.0.20+ the executor "
            "**rewrites this BNL to a hash join at runtime** "
            "(sql/sql_executor.cc), so the label is slightly misleading — "
            "you are usually already running hash. The ``hash_join`` "
            "optimizer_switch is defined but never checked by the planner "
            "and is effectively a no-op; setting ``block_nested_loop=off`` "
            "would kill the BNL→hash rewrite, not enable it. The real fix "
            "is an index on the join column (converts the join into "
            "eq_ref / ref) or a bigger ``join_buffer_size`` so the hash "
            "build side fits. MariaDB uses ``join_cache_hashed`` + "
            "``join_cache_level >= 3`` to select a hashed join buffer."
        ),
        "newcomer": (
            "MySQL has no index to look up matching rows, so it reads a "
            "chunk of the first table into memory and scans the second "
            "table once per chunk. This is slow on big tables. The real "
            "fix is almost always to add an index on the join column — "
            "that converts the join into a direct lookup and the chunking "
            "goes away."
        ),
        "aliases": ["Block Nested-Loop", "BNL", "Using join buffer (Block Nested Loop)"],
    },
    "batched_key_access": {
        "short": "Batches outer keys, reads inner rows in index order.",
        "technical": (
            "BKA: collects keys from the outer into the join buffer, sorts "
            "them, then uses Multi-Range Read on the inner side to fetch "
            "rows in (mostly) clustered-index order. Cuts random I/O on "
            "large indexed joins. BKA is **off by default** on MySQL 8.0 / "
            "8.4 (verified in sql/sys_vars.cc — OPTIMIZER_SWITCH_BKA is "
            "not in the OPTIMIZER_SWITCH_DEFAULT mask) and depends on MRR "
            "being enabled; enable both via "
            "``optimizer_switch='batched_key_access=on,mrr_cost_based=off'``. "
            "myflames only flags BKA when you've explicitly enabled it and "
            "the plan actually picked it."
        ),
        "newcomer": (
            "BKA is an optimization that can reorder inner-table fetches "
            "to reduce random disk I/O — but it is **off by default** in "
            "MySQL 8, and it relies on MRR also being enabled. If you see "
            "this term in myflames output, it means you (or a DBA) turned "
            "it on explicitly and the optimizer picked it for this plan."
        ),
        "aliases": ["Batched key access", "BKA", "batch_key_access"],
    },

    # ---- Semijoin strategies --------------------------------------------
    "semijoin": {
        "short": "Rewrites IN/EXISTS subqueries as joins with deduplication.",
        "technical": (
            "The optimizer transforms ``WHERE col IN (SELECT …)`` or EXISTS "
            "subqueries into a join against the outer query, then picks one "
            "of four strategies (FirstMatch, LooseScan, DuplicateWeedout, "
            "Materialization) to make sure each outer row appears at most "
            "once. Controlled by ``semijoin`` + per-strategy switches."
        ),
        "newcomer": (
            "Instead of running the subquery once per outer row, MySQL "
            "turned your IN/EXISTS clause into a join, then de-duplicated "
            "the results so each outer row appears once."
        ),
        "aliases": ["semijoin", "semi join", "semi-join"],
    },
    "firstmatch": {
        "short": "Semijoin: stop at the first inner match per outer row.",
        "technical": (
            "Semijoin strategy: as soon as a match is found for the current "
            "outer row, skip to the next one. Cheapest semijoin when the "
            "inner predicate has a selective index."
        ),
        "newcomer": (
            "MySQL stops looking as soon as it finds one matching row for "
            "your IN/EXISTS clause. Efficient for 'does any match exist?' "
            "style queries."
        ),
        "aliases": ["First Match", "FirstMatch", "first match"],
    },
    "loosescan": {
        "short": "Semijoin: scan the inner index once, skipping duplicate groups.",
        "technical": (
            "Semijoin strategy: scans an index on the inner table exactly "
            "once, using index key boundaries to skip over duplicate values "
            "instead of emitting them. Relies on the inner side having an "
            "index with the semijoin column as a leading key."
        ),
        "newcomer": (
            "MySQL walks the inner table's index once, cleverly skipping "
            "past duplicates."
        ),
        "aliases": ["Loose Scan", "LooseScan", "loose scan"],
    },
    "duplicate_weedout": {
        "short": "Semijoin: run as an inner join, then drop duplicates.",
        "technical": (
            "Semijoin strategy: the subquery is run as a plain inner join, "
            "and a temporary table keyed on the outer row IDs removes "
            "duplicates at the end. The fallback strategy when FirstMatch "
            "and LooseScan can't be used."
        ),
        "newcomer": (
            "MySQL ran the join normally, then used a temporary table to "
            "throw away duplicate rows. Not the fastest semijoin strategy, "
            "but the most general."
        ),
        "aliases": ["weedout", "DuplicateWeedout", "Duplicate Weedout",
                    "Remove duplicates"],
    },
    "materialization": {
        "short": "Subquery/derived table is computed once, stored in a temp table.",
        "technical": (
            "The subquery result is materialized into an internal temp table "
            "(MEMORY or on-disk InnoDB depending on size). The outer query "
            "reads from the temp table. Controlled by ``tmp_table_size`` and "
            "``max_heap_table_size`` — MySQL always uses the smaller of the "
            "two. If the temp table exceeds that limit, MySQL converts to "
            "an on-disk temp table and performance drops sharply."
        ),
        "newcomer": (
            "MySQL ran your subquery or derived table once, stored the "
            "result in a temporary table, and then used that temp table "
            "instead of re-running the subquery for every outer row."
        ),
        "aliases": ["Materialize", "Materialization", "Materialize with deduplication"],
    },

    # ---- Operations ------------------------------------------------------
    "filesort": {
        "short": "Sort that can't use an index.",
        "technical": (
            "MySQL sorts the result in the server layer using ``sort_buffer_size``. "
            "If the sort set fits, it stays in memory (fast quicksort). If "
            "not, MySQL writes sorted runs to tmpdir and k-way merges them — "
            "disk I/O plus CPU for the merge. An index in the ``ORDER BY`` "
            "order eliminates the filesort entirely."
        ),
        "newcomer": (
            "MySQL has to sort the rows by hand because the data isn't "
            "already in the order you asked for. Small sorts stay in "
            "memory; big ones use disk and get slow."
        ),
        "aliases": ["filesort", "Using filesort", "sort operation"],
    },
    "temp_table": {
        "short": "MySQL built an internal temporary table.",
        "technical": (
            "Used for materialized subqueries, GROUP BY with no suitable "
            "index, DISTINCT, and some ORDER BY cases. MEMORY/TempTable "
            "engine while it fits in ``min(tmp_table_size, max_heap_table_size)``, "
            "converted to on-disk InnoDB when it doesn't. The on-disk "
            "version is typically 10–100× slower."
        ),
        "newcomer": (
            "MySQL built a scratch table in memory to help process your "
            "query. If the scratch table gets too big, it moves to disk "
            "and slows down a lot."
        ),
        "aliases": ["temp table", "tmp table", "temporary table"],
    },
    "derived_table": {
        "short": "A subquery in the FROM clause, treated as its own table.",
        "technical": (
            "``SELECT … FROM (SELECT …) AS t``: the inner SELECT is a "
            "derived table. With ``derived_merge=on`` (default) the "
            "optimizer folds it into the outer query when possible; "
            "otherwise it's materialized. With "
            "``derived_condition_pushdown=on``, outer predicates can be "
            "pushed inside the derived query block to reduce its row count."
        ),
        "newcomer": (
            "A query-inside-a-query that MySQL treats like a temporary "
            "table. MySQL often rewrites it to be part of the outer query "
            "for speed, but sometimes it can't and has to materialize it."
        ),
        "aliases": ["derived table", "derived"],
    },

    # ---- Concepts --------------------------------------------------------
    "icp": {
        "short": "Push WHERE predicates into the storage engine.",
        "technical": (
            "Index Condition Pushdown: when an index has the WHERE columns "
            "but is not covering, the server pushes the remaining predicate "
            "down to the storage engine so it can reject non-matching rows "
            "BEFORE fetching the full row from the clustered index. Reduces "
            "row fetches dramatically for non-selective indexes."
        ),
        "newcomer": (
            "MySQL checks more of your WHERE clause inside the index itself, "
            "without having to open up each row first. Reduces wasted work."
        ),
        "aliases": ["ICP", "index condition pushdown", "pushed index condition"],
    },
    "sargable": {
        "short": "A predicate an index can satisfy directly.",
        "technical": (
            "Short for *Search ARGument-able*: a WHERE/ON predicate is "
            "sargable if it compares the bare column to a value or another "
            "bare column (``a.id = b.user_id``, ``created_at > '2024-01-01'``). "
            "Wrapping the column in a function (``CONCAT(a.id)``, "
            "``DATE(created_at)``, ``LOWER(email)``) breaks sargability "
            "because the optimizer would need a functional index on that "
            "exact expression. Non-sargable predicates force a per-row "
            "evaluation in the server layer and defeat every regular index."
        ),
        "newcomer": (
            "A filter or join condition MySQL can answer using an index. "
            "Comparing a plain column to a value is sargable. Wrapping the "
            "column in a function (like CONCAT or DATE) makes it "
            "non-sargable, which means MySQL has to evaluate the function "
            "on every row — no index can help."
        ),
        "aliases": ["sargable", "non-sargable", "nonsargable", "non sargable",
                    "SARG"],
    },
    "mrr": {
        "short": "Sort row IDs before fetching rows to make I/O sequential.",
        "technical": (
            "Multi-Range Read: the optimizer collects all row IDs from a "
            "secondary-index range scan, sorts them by primary key, and "
            "then fetches rows in (mostly) physical order. This converts "
            "random I/O on the clustered index into sequential I/O, which "
            "matters most on spinning disks but still helps SSDs via "
            "better cache locality."
        ),
        "newcomer": (
            "Instead of fetching rows one at a time in index order (which "
            "causes random jumps on disk), MySQL collects the row locations "
            "first, sorts them, and fetches them in the order they're "
            "physically stored. Much faster on disk-bound queries."
        ),
        "aliases": ["MRR", "multi-range read", "Multi-range index lookup", "multi_range_read"],
    },
    "rowid_filter": {
        "short": "MariaDB: pre-filter rows using a row-ID bitmap before index lookup.",
        "technical": (
            "MariaDB-only optimization: builds a sorted row-id filter from "
            "a secondary index first, then applies it during the main index "
            "lookup to avoid dereferencing rows that would fail the filter. "
            "Controlled by ``rowid_filter`` optimizer_switch (MariaDB 10.5+)."
        ),
        "newcomer": (
            "MariaDB builds a small list of row locations that match a "
            "filter, and uses it to skip rows that won't qualify, avoiding "
            "the cost of fully loading them."
        ),
        "aliases": ["rowid_filter", "rowid filter"],
    },

    # ---- Tunables --------------------------------------------------------
    "innodb_buffer_pool_size": {
        "short": "The RAM InnoDB uses to cache table and index pages.",
        "technical": (
            "The central caching tunable for InnoDB. Pages are loaded into "
            "the buffer pool on demand and evicted by LRU. Every page miss "
            "is a physical disk read. Rule of thumb: size it to ~70–80% of "
            "system RAM on a dedicated database server, or at least to the "
            "working set of your hottest tables. Requires restart."
        ),
        "newcomer": (
            "The amount of RAM InnoDB uses to cache your data. Bigger = "
            "more of your data stays in memory = faster repeat queries. "
            "If this is smaller than your working set, every query hits "
            "the disk."
        ),
        "aliases": ["innodb_buffer_pool_size", "buffer pool"],
    },
    "sort_buffer_size": {
        "short": "Per-connection buffer for in-memory sorts.",
        "technical": (
            "Allocated lazily per connection, freed after the sort. When a "
            "filesort's data exceeds this buffer, MySQL writes sorted runs "
            "to tmpdir and k-way merges them back — disk I/O. Because it's "
            "per-connection, setting this high globally multiplies by "
            "max_connections; prefer ``SET SESSION sort_buffer_size = …`` "
            "for individual big queries."
        ),
        "newcomer": (
            "The memory MySQL gets per connection to sort rows. If your "
            "sort is bigger than this, MySQL falls back to using disk, "
            "which is slower."
        ),
        "aliases": ["sort_buffer_size", "sort buffer"],
    },
    "join_buffer_size": {
        "short": "Per-join buffer for hash joins and Block Nested-Loop.",
        "technical": (
            "Per-join allocation, freed after the join completes. Feeds "
            "the build side of a hash join or the outer-row batches of a "
            "BNL. If the build side of a hash join doesn't fit, MySQL "
            "spills to a multi-pass (Grace) hash join. An index on the "
            "join column is almost always a bigger win than raising this."
        ),
        "newcomer": (
            "The memory MySQL uses when it has to join tables without a "
            "handy index. If this is too small for a big join, MySQL "
            "spills to disk and slows down."
        ),
        "aliases": ["join_buffer_size", "join buffer"],
    },
    "tmp_table_size": {
        "short": "Maximum in-memory temp table size (half of the pair with max_heap_table_size).",
        "technical": (
            "The effective limit is ``min(tmp_table_size, max_heap_table_size)``. "
            "Internal temp tables (from materialized subqueries, GROUP BY, "
            "DISTINCT, etc.) are MEMORY-engine until they exceed this, at "
            "which point MySQL converts them to on-disk InnoDB temp tables "
            "(10–100× slower for scans). Both variables must be raised "
            "together — raising only one has no effect."
        ),
        "newcomer": (
            "How big MySQL's scratch tables can get before they move from "
            "RAM to disk. If your query builds bigger scratch tables than "
            "this, expect a big slowdown — and you have to raise "
            "``max_heap_table_size`` at the same time, otherwise it does "
            "nothing."
        ),
        "aliases": ["tmp_table_size", "max_heap_table_size", "tmp table size"],
    },
    "optimizer_switch": {
        "short": "Comma-separated list of optimizer feature flags.",
        "technical": (
            "Server/session variable that enables or disables individual "
            "optimizer transformations (hash_join, index_merge, skip_scan, "
            "semijoin strategies, etc.). Session-tunable: "
            "``SET SESSION optimizer_switch='hash_join=on'``. The default "
            "differs between MySQL and MariaDB and between minor versions."
        ),
        "newcomer": (
            "A list of on/off switches for specific optimizer tricks. "
            "Most are on by default; turning the wrong ones off can hurt "
            "performance a lot."
        ),
        "aliases": ["optimizer_switch", "optimizer switch"],
    },

    # ---- Metrics ---------------------------------------------------------
    "rows_examined": {
        "short": "How many rows the storage engine actually touched.",
        "technical": (
            "The total number of rows read by the storage engine to answer "
            "the query, summed across all access paths and all join iterations. "
            "Much larger than ``ROWS_SENT`` when the query does full scans, "
            "joins without indexes, or filters out most rows in the server "
            "layer. Visible in ``performance_schema.events_statements_history.ROWS_EXAMINED``."
        ),
        "newcomer": (
            "The number of rows MySQL actually had to read to answer your "
            "query — including all the rows it read and then threw away. "
            "Can be hundreds of times bigger than the number of rows "
            "returned."
        ),
        "aliases": ["ROWS_EXAMINED", "rows examined"],
    },
    "rows_sent": {
        "short": "How many rows the client ultimately received.",
        "technical": (
            "The number of rows in the final result set, after all filters, "
            "joins, aggregations, and LIMIT clauses. Compare to ``ROWS_EXAMINED`` "
            "to measure selectivity — a ratio of 1:1 is ideal, 1:1000+ is a "
            "sign of a missing index or a poorly selective filter."
        ),
        "newcomer": (
            "The number of rows your query actually returned."
        ),
        "aliases": ["ROWS_SENT", "rows sent"],
    },
}


# ---------------------------------------------------------------------------
# Lookup API
# ---------------------------------------------------------------------------

def _normalize(term):
    """Strip punctuation + lowercase so aliases match reliably."""
    return re.sub(r"[^a-z0-9_ ]+", "", (term or "").lower()).strip()


_ALIAS_INDEX = None


def _build_alias_index():
    """Build ``{normalized_alias: canonical_key}`` once on first lookup."""
    idx = {}
    for canonical, entry in GLOSSARY.items():
        idx[_normalize(canonical)] = canonical
        for alias in entry.get("aliases", []):
            idx[_normalize(alias)] = canonical
    return idx


def lookup(term):
    """Return the glossary entry for *term*, or None if unknown.

    Lookup matches the canonical key AND every declared alias, ignoring
    case and punctuation. Intended for HTML template integration where we
    scan the output text for jargon and wrap each hit in an ``<abbr>``.
    """
    global _ALIAS_INDEX
    if _ALIAS_INDEX is None:
        _ALIAS_INDEX = _build_alias_index()
    key = _ALIAS_INDEX.get(_normalize(term))
    if key is None:
        return None
    entry = GLOSSARY[key]
    # Return a copy with the canonical key attached so the caller can use
    # it as an anchor ID in HTML.
    out = dict(entry)
    out["key"] = key
    return out


_TERM_REGEX_CACHE = None


def _build_term_regexes(min_len=3):
    """Build ``[(length, compiled_regex, canonical)]`` for every glossary
    surface form. Regexes use flexible separators (``[-_\\s]+``) so
    ``block nested-loop``, ``Block Nested Loop``, and ``block_nested_loop``
    all match the same entry. Sorted by length descending so the longest
    phrase wins when two keys could overlap on the same span.
    """
    pairs = []
    for canonical, entry in GLOSSARY.items():
        # Treat the canonical key's space-ified form as a surface form too,
        # so ``block_nested_loop`` finds ``Block Nested Loop`` without needing
        # the author to list it in aliases.
        forms = {canonical.replace("_", " ")}
        forms.update(entry.get("aliases", []))
        for form in forms:
            form = (form or "").strip()
            if len(form) < min_len:
                continue
            words = [w for w in re.split(r"[-_\s]+", form) if w]
            if not words:
                continue
            # (?:^|(?<=\W)) / (?:(?=\W)|$) emulate word boundaries but also
            # fire on hyphen/punctuation, which \b won't.
            pattern = (
                r"(?<![A-Za-z0-9_])"
                + r"[-_\s]+".join(re.escape(w) for w in words)
                + r"(?![A-Za-z0-9_])"
            )
            try:
                compiled = re.compile(pattern, re.IGNORECASE)
            except re.error:
                continue
            pairs.append((len(form), compiled, canonical))
    pairs.sort(key=lambda p: -p[0])
    return pairs


def find_terms_in_text(text, min_len=3):
    """Return a list of glossary hits found in *text*.

    Each hit is ``{"term": "<surface text>", "key": "<canonical>", "start": N,
    "end": N}``. Matches are non-overlapping and prefer longer terms, so
    ``Block Nested-Loop`` wins over ``nested loop`` on the same span.

    Used by the HTML template to wrap jargon with tooltips without touching
    the glossary definitions themselves.
    """
    if not text:
        return []
    global _TERM_REGEX_CACHE
    if _TERM_REGEX_CACHE is None:
        _TERM_REGEX_CACHE = _build_term_regexes(min_len)

    claimed = [False] * len(text)
    hits = []
    for _, pattern, canonical in _TERM_REGEX_CACHE:
        for m in pattern.finditer(text):
            i, j = m.start(), m.end()
            if any(claimed[i:j]):
                continue
            hits.append({
                "term": text[i:j],
                "key": canonical,
                "start": i,
                "end": j,
            })
            for k in range(i, j):
                claimed[k] = True
    hits.sort(key=lambda h: h["start"])
    return hits


# ---------------------------------------------------------------------------
# Executive summary generator
# ---------------------------------------------------------------------------

def _pick_primary_issue(analysis):
    """Pick the one finding most likely to be the actionable lede.

    Priority order (high severity first) is the one a DBA would use when
    glancing at a plan: **non-sargable join** > durability > engine >
    buffer pool > missing index > disk-spilling sort/tmp > hash join / BNL >
    full scan > temp table > filesort > env warnings > plan warnings > none.

    Non-sargable joins come first because they render every other
    optimization irrelevant — no index will help until the predicate is
    rewritten, so the user's primary action is the rewrite, not the
    index / buffer / switch tweak.
    """
    env_warnings = analysis.get("environment_warnings") or []
    env_suggestions = analysis.get("environment_suggestions") or []
    warnings = analysis.get("warnings") or []
    bnl_nodes = analysis.get("bnl_nodes") or []
    hash_joins = analysis.get("hash_joins") or []
    full_scans = analysis.get("full_scans") or []
    temp_tables = analysis.get("temp_tables") or []
    filesorts = analysis.get("filesorts") or []
    index_suggestions = analysis.get("index_suggestions") or []
    nonsargable_joins = analysis.get("nonsargable_joins") or []

    # Non-sargable join predicate — highest priority because nothing else
    # can help until this is rewritten.
    if nonsargable_joins:
        first = nonsargable_joins[0]
        return (
            "nonsargable_join",
            "join predicate wraps the column in {}() — no index can be used".format(
                first.get("function", "a function")
            ),
        )

    # Durability (writes + relaxed flush)
    for s in env_suggestions:
        if "innodb_flush_log_at_trx_commit" in s:
            return ("durability", "writes are not fsync'd on commit — a crash can lose the last second of transactions")

    # Engine mismatch (MyISAM, etc.)
    for s in env_suggestions:
        if "ENGINE=InnoDB" in s:
            return ("engine", "table uses a storage engine without row locks or crash recovery")

    # Buffer pool vs working set
    for w in env_warnings:
        if "innodb_buffer_pool_size" in w:
            return ("buffer_pool", "the buffer pool is smaller than the working set — cold reads will hit disk")

    # Missing indexes (highest-impact query-level issue)
    if index_suggestions:
        hint = index_suggestions[0]
        return ("missing_index", "no index covers ({}) on {}".format(
            ", ".join(hint.get("columns") or []),
            hint.get("table") or "the filter column",
        ))

    # Sort spilling (filesort + small sort_buffer_size env warning)
    for w in env_warnings:
        if "sort_buffer_size" in w:
            return ("sort_spill", "the sort will likely spill to disk because sort_buffer_size is too small")

    # Temp table spilling
    for w in env_warnings:
        if "tmp_table_size" in w:
            return ("tmp_spill", "the temp table will spill to on-disk InnoDB because tmp_table_size is too small")

    # Plan-level findings in priority order
    if bnl_nodes:
        return ("bnl", "Block Nested-Loop join — each batch of outer rows triggers a full scan of the inner table")
    if hash_joins:
        return ("hash_join", "hash join uses join_buffer_size; an index on the join column would be faster")
    if full_scans:
        biggest = max(full_scans, key=lambda s: s.get("rows") or 0)
        rows = int(biggest.get("rows") or 0)
        return ("full_scan", "full scan of {} ({} row{})".format(
            biggest.get("table") or "a table",
            "{:,}".format(rows),
            "" if rows == 1 else "s",
        ))
    if filesorts:
        return ("filesort", "result is sorted without using an index")
    if temp_tables:
        return ("temp_table", "a temp table is materialized")
    if env_warnings:
        return ("env", env_warnings[0])
    if warnings:
        return ("plan", warnings[0])
    return (None, None)


def _describe_shape(root, analysis, plan_summary):
    """Return a list of verb phrases describing what the query does.

    Reads from the analysis dict rather than re-walking the tree so all
    the classification happens in one place (parser.analyze_plan).
    """
    full_scans = analysis.get("full_scans") or []
    hash_joins = analysis.get("hash_joins") or []
    bnl_nodes = analysis.get("bnl_nodes") or []
    temp_tables = analysis.get("temp_tables") or []
    filesorts = analysis.get("filesorts") or []

    verbs = []
    if full_scans:
        n = len(full_scans)
        verbs.append("scans {} table{}".format(n, "" if n == 1 else "s"))
    if hash_joins:
        n = len(hash_joins)
        verbs.append("hash-joins {} pair{}".format(n, "" if n == 1 else "s"))
    elif bnl_nodes:
        verbs.append("joins via block nested-loop")
    if temp_tables:
        n = len(temp_tables)
        verbs.append("materializes {} temp table{}".format(
            n, "" if n == 1 else "s"
        ))
    if filesorts:
        verbs.append("sorts the result")
    if not verbs:
        verbs.append("runs {} operator{}".format(
            plan_summary.get("operator_count") or 0,
            "" if plan_summary.get("operator_count") == 1 else "s",
        ))
    return verbs


def _format_size_time(plan_summary):
    """Format '<examined/sent rows> in <time>' using human-friendly units."""
    time_ms = plan_summary.get("total_time_ms") or 0
    rows_sent = plan_summary.get("rows_sent") or 0
    rows_examined = plan_summary.get("rows_examined_estimate") or 0

    if time_ms >= 10:
        time_str = "{:.0f} ms".format(time_ms)
    elif time_ms >= 1:
        time_str = "{:.1f} ms".format(time_ms)
    elif time_ms > 0:
        time_str = "{:.2f} ms".format(time_ms)
    else:
        time_str = None

    # Call out the examined:sent ratio when it's wasteful (10×+).
    if rows_examined > max(rows_sent, 1) * 10 and rows_examined >= 100:
        size_str = "examines ~{:,} rows to return {:,}".format(
            rows_examined, rows_sent,
        )
    else:
        size_str = "returns {:,} row{}".format(
            rows_sent, "" if rows_sent == 1 else "s"
        )

    if time_str:
        return size_str + " in " + time_str
    return size_str


def generate_executive_summary(root, analysis, plan_summary=None):
    """Return a one-to-two sentence plain-English description of a plan.

    Shape: ``"Query <shape>; <size/time>. Main finding: <issue>."``

    This is the function ``output_sidecar.build_sidecar`` calls to populate
    the ``executive_summary`` field. Deterministic given the same analysis,
    so it's trivially testable.

    Parameters
    ----------
    root : dict
        The parse_explain() tree. Only used for computing plan_summary if
        one isn't supplied; otherwise not inspected.
    analysis : dict
        Output of parser.analyze_plan, optionally extended by advisor.advise.
    plan_summary : dict, optional
        If already computed by the caller (as output_sidecar does), pass it
        in to avoid re-walking the tree. Same shape as
        output_sidecar._compute_plan_summary.
    """
    if plan_summary is None:
        plan_summary = _compute_plan_summary_local(root)

    verbs = _describe_shape(root, analysis, plan_summary)
    size_time = _format_size_time(plan_summary)

    if len(verbs) == 1:
        shape = verbs[0]
    else:
        shape = ", ".join(verbs[:-1]) + " and " + verbs[-1]

    _, issue_text = _pick_primary_issue(analysis)
    if issue_text:
        return "Query {}; {}. Main finding: {}.".format(shape, size_time, issue_text)
    return "Query {}; {}. No warnings.".format(shape, size_time)


def _compute_plan_summary_local(root):
    """Local duplicate of output_sidecar._compute_plan_summary.

    Kept here (rather than importing) so glossary.py has no dependency on
    output_sidecar.py — avoids a circular import and lets tests import
    just glossary without pulling in the sidecar surface.
    """
    stats = {"op_count": 0, "max_depth": 0, "rows_examined": 0}

    def _walk(node, depth):
        stats["op_count"] += 1
        if depth > stats["max_depth"]:
            stats["max_depth"] = depth
        children = node.get("children") or []
        if not children:
            stats["rows_examined"] += int(node.get("rows") or 0)
        for c in children:
            _walk(c, depth + 1)

    _walk(root, 1)
    return {
        "total_time_ms": round(float(root.get("total_time") or 0), 3),
        "rows_sent": int(root.get("rows") or 0),
        "rows_examined_estimate": stats["rows_examined"],
        "operator_count": stats["op_count"],
        "max_depth": stats["max_depth"],
    }
