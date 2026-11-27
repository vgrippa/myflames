# Synthetic VillageSQL KNN plans

These two fixtures are source-derived JSON format-version-2 ANALYZE trees,
not recorded server output. Names, cardinalities, costs and timings are
illustrative. Recorded lab output replaces these files when available.
The lab's versioned `explain-knn-8.4-*` and `explain-knn-9.7-*` files remain
separate recordings.

Source read: VillageSQL main, 0.0.9-dev, in `villagesql-server`. Line numbers
below refer to that checkout, inspected 2026-11-27.

- `sql/join_optimizer/explain_access_path.cc:219–265` writes index access
  metadata, including `index_access_type`, `index_name`, `lookup_condition`
  and `covering`. Lines 165–195 write table/schema identity and used columns.
- Lines 1158–1175 emit `index_knn_scan`, the distance expression with `<->`,
  the `Index KNN scan` operation and integer `limit`. Lines 1022–1047 write
  per-loop `actual_*` timings and rows plus `actual_loops`.
- Lines 1383–1392 emit the nested loop with outer input first; lines
  1200–1208 emit the single-row PK lookup. Lines 1612–1641 emit Limit and
  `limit_offset`. No extra format-version envelope is emitted here.
- `villagesql/sql/optimizer.cc:148–205` requires a LIMIT, one ascending
  deterministic distance call and a KNN-capable custom index, then replaces
  filesort ordering with KNN. Lines 104–107 mark the scan's limit exact only
  for an unfiltered single-table read. Lines 213–228 describe join/filter
  and approximate-index limits, so the fixtures assert no universal
  complexity or exact-search guarantee.
- `sql/sql_optimizer.cc:2372–2378` skips sorting after successful KNN
  selection. `sql/sql_executor.cc:3842–3849` constructs its access path;
  lines 3430–3436 still wrap the result in Limit. The simple fixture has no
  WHERE or HAVING, so needs no Filter. Residual predicates can add Filter
  (`sql/sql_executor.cc:808–837`, 2860), but are absent from these queries.
- `villagesql/sql/knn_iterator.cc:53–54` passes K and `limit_is_exact` to
  the custom index. Lines 68–80 read candidates through the handler.
  `sql/sql_opt_exec_shared.h:44–49` defines those arguments.
- `sql/opt_explain.cc:121`, 1169–1178 name traditional `knn_scan` and its
  KNN-limit Extra field. These fixtures cover JSON v2, not traditional output.
- The source's own expected output in
  `mysql-test/suite/villagesql/extension/vsql-knn-index-test/r/knn_query_shapes.result:18–23`
  confirms Limit → KNN without Sort. Lines 121–126 show Limit → Filter → KNN.
  Its `vsql_knn_index_test.kvec_l2_distance` call supplies the exemplar
  distance function and four-component constant; it is a test extension.

Shape exemplars from this repository: `../explain-026-sort-limit-top10-expensive-products.json`
for ANALYZE counters and Limit, and `../explain-035-join-2t-users-orders.json`
for nested-loop input ordering and repeated lookup timing. The join fixture
models a non-null `vectors.user_id` with a matching `users` primary key and
an outer KNN path; its estimates and exact optimizer selection still need
live confirmation. It has Limit → Nested loop → [KNN, PK lookup].
