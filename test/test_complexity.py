"""Unit tests for :mod:`myflames.complexity`.

One test case per row of the decision table so regressions surface
immediately. Each fixture is a minimal parsed-node dict, NOT a real EXPLAIN
JSON — the function under test consumes the ``details`` dict shape produced
by :func:`myflames.parser.parse_node`.

Every complexity class people will use to study MySQL plans lives here.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from myflames.complexity import (  # noqa: E402
    compute_complexity,
    SEVERITY_COLORS,
    SEVERITY_BORDERS,
)


def node(access_type="", index_access_type="", op="", covering=None,
         join_algorithm="", using_join_buffer="", children=None):
    """Build a minimal parsed-node dict for the complexity computation."""
    return {
        "full_label": op,
        "short_label": op[:40] or access_type,
        "folded_label": (access_type or "op").upper(),
        "details": {
            "access_type": access_type,
            "index_access_type": index_access_type,
            "covering": covering,
            "join_algorithm": join_algorithm,
            "using_join_buffer": using_join_buffer,
        },
        "children": children or [],
    }


class DecisionTableTests(unittest.TestCase):
    # ---- single-table access paths -----------------------------------------

    def test_table_scan_is_linear_medium(self):
        c = compute_complexity(node(access_type="table", op="Table scan on users"))
        self.assertEqual(c["big_o"], "O(n)")
        self.assertEqual(c["severity"], "medium")
        self.assertEqual(c["confidence"], "exact")
        self.assertEqual(c["learn_more"], "full_table_scan")

    def test_mariadb_all_normalises_to_table_scan(self):
        c = compute_complexity(node(access_type="ALL"))
        self.assertEqual(c["big_o"], "O(n)")
        self.assertEqual(c["severity"], "medium")

    def test_index_scan_noncovering_is_medium(self):
        c = compute_complexity(node(
            access_type="index", op="Index scan on users using idx_name",
            covering=False,
        ))
        self.assertEqual(c["big_o"], "O(n)")
        self.assertEqual(c["severity"], "medium")
        self.assertEqual(c["learn_more"], "index_scan")

    def test_index_scan_covering_is_good(self):
        c = compute_complexity(node(
            access_type="index", op="Covering index scan on users",
            covering=True,
        ))
        self.assertEqual(c["big_o"], "O(n)")
        self.assertEqual(c["severity"], "good")
        self.assertEqual(c["learn_more"], "covering_index")

    def test_range_scan_is_log_n_plus_k(self):
        c = compute_complexity(node(
            access_type="index", index_access_type="index_range_scan",
            op="Index range scan on users using idx_created",
        ))
        self.assertEqual(c["big_o"], "O(log n + k)")
        self.assertEqual(c["severity"], "good")
        self.assertEqual(c["learn_more"], "index_range_scan")

    def test_skip_scan_variant_of_range(self):
        c = compute_complexity(node(
            access_type="index", index_access_type="index_range_scan",
            op="Index range scan on events using idx_type (Using index for skip scan)",
        ))
        self.assertEqual(c["big_o"], "O(d · log n)")
        self.assertEqual(c["severity"], "medium")
        self.assertEqual(c["learn_more"], "skip_scan")
        # Confidence is typical — we can't see d from EXPLAIN.
        self.assertEqual(c["confidence"], "typical")

    def test_index_lookup_ref_is_good(self):
        c = compute_complexity(node(
            access_type="index", index_access_type="index_lookup",
            op="Index lookup on orders using idx_user (user_id=42)",
        ))
        self.assertEqual(c["big_o"], "O(log n + k)")
        self.assertEqual(c["severity"], "good")
        self.assertEqual(c["learn_more"], "index_lookup")

    def test_mariadb_ref_is_good(self):
        c = compute_complexity(node(access_type="ref"))
        self.assertEqual(c["big_o"], "O(log n + k)")
        self.assertEqual(c["severity"], "good")

    def test_single_row_lookup_is_log_n(self):
        c = compute_complexity(node(
            access_type="index", index_access_type="index_lookup",
            op="Single-row index lookup on users using PRIMARY",
        ))
        self.assertEqual(c["big_o"], "O(log n)")
        self.assertEqual(c["severity"], "good")
        self.assertEqual(c["learn_more"], "single_row_lookup")

    def test_mariadb_eq_ref_is_log_n(self):
        c = compute_complexity(node(access_type="eq_ref"))
        self.assertEqual(c["big_o"], "O(log n)")

    def test_mariadb_const_is_log_n(self):
        c = compute_complexity(node(access_type="const"))
        self.assertEqual(c["big_o"], "O(log n)")

    def test_fulltext_is_k(self):
        c = compute_complexity(node(access_type="fulltext"))
        self.assertEqual(c["big_o"], "O(k)")
        self.assertEqual(c["severity"], "good")

    # ---- sort / group / union ---------------------------------------------

    def test_sort_default_is_n_log_n_worst_case(self):
        c = compute_complexity(node(access_type="sort", op="Sort: created_at DESC"))
        self.assertEqual(c["big_o"], "O(n log n)")
        self.assertEqual(c["severity"], "medium")
        self.assertEqual(c["confidence"], "worst_case")

    def test_sort_using_index_is_linear_good(self):
        c = compute_complexity(node(
            access_type="sort",
            op="Sort: c.created_at (Using index for order by)",
        ))
        self.assertEqual(c["big_o"], "O(n)")
        self.assertEqual(c["severity"], "good")

    def test_aggregate_maps_to_group_n_log_n(self):
        c = compute_complexity(node(access_type="aggregate", op="Group aggregate: count(*)"))
        self.assertEqual(c["big_o"], "O(n log n)")
        self.assertEqual(c["severity"], "medium")

    def test_group_with_index_is_linear(self):
        c = compute_complexity(node(
            access_type="group",
            op="Group by status (Using index for group-by)",
        ))
        self.assertEqual(c["big_o"], "O(n)")
        self.assertEqual(c["severity"], "good")

    def test_union_all_is_linear(self):
        c = compute_complexity(node(access_type="union", op="Union all"))
        self.assertEqual(c["big_o"], "O(n + m)")
        self.assertEqual(c["severity"], "good")

    def test_union_dedupe_is_n_log_n(self):
        c = compute_complexity(node(access_type="union", op="Union (distinct)"))
        self.assertEqual(c["severity"], "medium")

    # ---- index merge ------------------------------------------------------

    def test_rowid_union(self):
        c = compute_complexity(node(access_type="rowid_union"))
        self.assertEqual(c["big_o"], "O(Σ kᵢ)")
        self.assertEqual(c["learn_more"], "index_merge")

    def test_rowid_intersection(self):
        c = compute_complexity(node(access_type="rowid_intersection"))
        self.assertEqual(c["big_o"], "O(Σ kᵢ)")

    def test_rowid_sort_union_is_n_log_n(self):
        c = compute_complexity(node(access_type="rowid_sort_union"))
        self.assertEqual(c["big_o"], "O(n log n)")

    def test_rowid_sort_intersection_is_n_log_n(self):
        c = compute_complexity(node(access_type="rowid_sort_intersection"))
        self.assertEqual(c["big_o"], "O(n log n)")

    # ---- semijoin variants ------------------------------------------------

    def test_semijoin_firstmatch(self):
        c = compute_complexity(node(access_type="semijoin", op="Semijoin (FirstMatch)"))
        self.assertEqual(c["big_o"], "O(n · log m)")
        self.assertEqual(c["learn_more"], "firstmatch")

    def test_semijoin_loosescan(self):
        c = compute_complexity(node(access_type="semijoin", op="Semijoin (LooseScan)"))
        self.assertEqual(c["big_o"], "O(n)")
        self.assertEqual(c["learn_more"], "loosescan")

    def test_semijoin_materialization(self):
        c = compute_complexity(node(access_type="semijoin", op="Semijoin (Materialization)"))
        self.assertEqual(c["big_o"], "O(m) + O(n · log m)")
        self.assertEqual(c["learn_more"], "materialization")

    def test_weedout(self):
        c = compute_complexity(node(access_type="weedout"))
        self.assertEqual(c["big_o"], "O(n log n)")
        self.assertEqual(c["learn_more"], "duplicate_weedout")

    # ---- materialize: two-phase ------------------------------------------

    def test_materialize_emits_both_build_and_scan(self):
        # Inner child: a table scan (n rows); build should inherit its class.
        inner = node(access_type="table")
        inner["details"]["complexity"] = compute_complexity(inner)
        mat = node(access_type="materialize", op="Materialize with deduplication",
                   children=[inner])
        c = compute_complexity(mat)
        self.assertIn("build_complexity", c)
        self.assertIn("scan_complexity", c)
        self.assertEqual(c["build_complexity"]["big_o"], "O(n)")
        self.assertEqual(c["scan_complexity"]["severity"], "good")
        self.assertEqual(c["learn_more"], "materialization")

    # ---- joins: decided by inner-child access_type + join flags ----------

    def test_indexed_nested_loop_from_ref_inner(self):
        outer = node(access_type="table")
        inner = node(access_type="ref")
        join = node(access_type="join", op="Nested loop inner join",
                    children=[outer, inner])
        c = compute_complexity(join)
        self.assertEqual(c["big_o"], "O(n · log m)")
        self.assertEqual(c["severity"], "medium")
        self.assertEqual(c["learn_more"], "nested_loop_join")

    def test_indexed_nested_loop_from_mysql_index_inner(self):
        # MySQL 8 JSON plan: inner has access_type="index" but really is a range.
        outer = node(access_type="table")
        inner = node(access_type="index", index_access_type="index_lookup",
                     op="Index lookup on orders using idx_user (user_id=x)")
        join = node(access_type="join", op="Nested loop inner join",
                    children=[outer, inner])
        c = compute_complexity(join)
        self.assertEqual(c["big_o"], "O(n · log m)")

    def test_bnl_nested_loop_is_bad(self):
        outer = node(access_type="table")
        inner = node(access_type="table")
        join = node(access_type="join", op="Nested loop inner join",
                    children=[outer, inner],
                    using_join_buffer="Block Nested Loop")
        c = compute_complexity(join)
        self.assertEqual(c["big_o"], "O(n · m)")
        self.assertEqual(c["severity"], "bad")
        self.assertEqual(c["learn_more"], "block_nested_loop")

    def test_unindexed_inner_scan_is_bad(self):
        outer = node(access_type="table")
        inner = node(access_type="table")
        join = node(access_type="join", op="Nested loop inner join",
                    children=[outer, inner])
        c = compute_complexity(join)
        self.assertEqual(c["big_o"], "O(n · m)")
        self.assertEqual(c["severity"], "bad")

    def test_hash_join_by_algorithm(self):
        outer = node(access_type="table")
        inner = node(access_type="table")
        join = node(access_type="join", op="Inner hash join",
                    join_algorithm="hash",
                    children=[outer, inner])
        c = compute_complexity(join)
        self.assertEqual(c["big_o"], "O(n + m)")
        self.assertEqual(c["severity"], "good")
        self.assertEqual(c["learn_more"], "hash_join")

    def test_hash_join_by_using_join_buffer(self):
        outer = node(access_type="table")
        inner = node(access_type="table")
        join = node(access_type="join",
                    using_join_buffer="hash",
                    children=[outer, inner])
        c = compute_complexity(join)
        self.assertEqual(c["big_o"], "O(n + m)")

    def test_batched_key_access_distinct_class(self):
        outer = node(access_type="table")
        inner = node(access_type="ref")
        join = node(access_type="join",
                    using_join_buffer="Batched Key Access",
                    children=[outer, inner])
        c = compute_complexity(join)
        self.assertEqual(c["big_o"], "O(n · log m)")
        self.assertEqual(c["learn_more"], "batched_key_access")

    # ---- opt-out contract -------------------------------------------------

    def test_unknown_access_type_returns_none(self):
        self.assertIsNone(compute_complexity(node(access_type="unknown_operator_type")))

    def test_empty_access_with_no_join_algo_returns_none(self):
        self.assertIsNone(compute_complexity(node()))

    def test_none_node_returns_none(self):
        self.assertIsNone(compute_complexity(None))

    def test_pre_executed_rows_node_skipped(self):
        # MySQL emits this as a non-access pseudo-node; we never claim complexity.
        n = node(access_type="rows_fetched_before_execution",
                 op="Rows fetched before execution")
        self.assertIsNone(compute_complexity(n))


class SeverityPaletteTests(unittest.TestCase):
    """Colors are a public contract because renderers hard-code nothing."""

    def test_palette_has_all_three_severities(self):
        for sev in ("good", "medium", "bad"):
            self.assertIn(sev, SEVERITY_COLORS)
            self.assertIn(sev, SEVERITY_BORDERS)

    def test_severity_colors_are_rgb_strings(self):
        for sev, colour in SEVERITY_COLORS.items():
            self.assertRegex(colour, r"^rgb\(\d+,\d+,\d+\)$")


class ContractTests(unittest.TestCase):
    """Every returned dict must satisfy the sidecar schema (stringly-typed fields)."""

    def _assert_shape(self, c):
        self.assertIsNotNone(c)
        for k in ("big_o", "short", "severity", "rationale", "confidence"):
            self.assertIn(k, c, f"missing {k}")
            self.assertIsInstance(c[k], str, f"{k} not str: {type(c[k])}")
            self.assertTrue(c[k], f"{k} empty")
        self.assertIn(c["severity"], {"good", "medium", "bad"})
        self.assertIn(c["confidence"], {"exact", "typical", "worst_case"})

    def test_every_positive_case_returns_valid_shape(self):
        cases = [
            node(access_type="table"),
            node(access_type="index", covering=True, op="Covering index scan"),
            node(access_type="index", index_access_type="index_lookup",
                 op="Index lookup"),
            node(access_type="sort", op="Sort"),
            node(access_type="sort", op="Sort (Using index for order by)"),
            node(access_type="rowid_union"),
            node(access_type="semijoin", op="Semijoin (FirstMatch)"),
            node(access_type="join", join_algorithm="hash",
                 children=[node(access_type="table"), node(access_type="table")]),
        ]
        for n in cases:
            c = compute_complexity(n)
            with self.subTest(access=n["details"]["access_type"]):
                self._assert_shape(c)


if __name__ == "__main__":
    unittest.main()
