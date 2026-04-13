# Teach Algorithm Roadmap (MySQL + MariaDB)

This roadmap tracks high-value operator lessons that are still missing.
It is grouped by teach family so implementation can stay consistent with
the new family folder layout.

## Join Family

- `bka_join` (MySQL + MariaDB): Batched Key Access with MRR-enabled probes.
- `semijoin_duplicate_weedout` (MySQL + MariaDB): temporary table dedup flow.
- `semijoin_firstmatch` (MySQL + MariaDB): early-out behavior on first match.
- `semijoin_loosescan` (MySQL + MariaDB): index-driven semijoin skipping.
- `semijoin_materialization` (MySQL + MariaDB): subquery materialization path.

## Index Family

- `skip_scan` (MySQL): range access without leading index column equality.
- `mrr` / `mrr_cost_based` (MySQL + MariaDB): rowid batching and clustered read ordering.
- `covering_index_scan` (MySQL + MariaDB): index-only retrieval without table-row fetch.
- `rowid_filter` (MariaDB): rowid pre-filter before table access.
- `loose_index_scan` (MySQL + MariaDB): grouped access for MIN/MAX and grouped scans.

## Scan / Sort / Temp Family

- `derived_table_materialization` (MySQL + MariaDB): build temporary result then consume.
- `derived_merge` (MySQL + MariaDB): merged derived query block path.
- `derived_condition_pushdown` (MySQL + MariaDB): predicate pushdown into derived tables.
- `window_function_pipeline` (MySQL + MariaDB): partition sort + frame evaluation stages.
- `union_distinct` (MySQL + MariaDB): duplicate elimination versus `UNION ALL`.

## Cache / Memory Family

- `read_buffer` (MySQL + MariaDB): sequential scan buffering.
- `read_rnd_buffer` (MySQL + MariaDB): random row reads after filesort.
- `adaptive_hash_index` (MySQL): hot lookup shortcut above B+tree pages.
- `join_buffer_memory_budget` (MySQL + MariaDB): concurrent joins and memory pressure.
- `tmp_table_memory_budget` (MySQL + MariaDB): per-session temp memory pressure dynamics.

## Suggested build order

1. `bka_join`
2. `skip_scan`
3. `rowid_filter`
4. `semijoin_duplicate_weedout`
5. `derived_table_materialization`
