"""Tests for `myflames teach` — interactive algorithm lessons.

Three concentric rings:

1. Cost-model unit tests: exact equality on known-good numbers so a
   version upgrade that changes an InnoDB or `join_buffer_size` default
   breaks loudly.
2. Version-accuracy string checks on rendered HTML.
3. CLI + HTML scaffolding checks.
"""
from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from myflames.teach._cost_model import (
    INNODB_PAGE_SIZE_DEFAULT,
    INNODB_PAGE_OVERHEAD_BYTES,
    INNODB_CHILD_POINTER_BYTES,
    INNODB_RECORD_HEADER_BYTES,
    INNODB_CLUSTERED_SYSTEM_BYTES,
    INNODB_CLUSTERED_LEAF_RESERVE_DIVISOR,
    PK_BIGINT_BYTES,
    JOIN_BUFFER_SIZE_DEFAULT,
    MARIADB_JOIN_CACHE_LEVEL_DEFAULT,
    MYSQL_BNL_REMOVED_IN,
    INNODB_OLD_BLOCKS_PCT_DEFAULT,
    INNODB_OLD_BLOCKS_TIME_DEFAULT_MS,
    SORT_BUFFER_SIZE_DEFAULT,
    TMP_TABLE_SIZE_DEFAULT,
    MAX_HEAP_TABLE_SIZE_DEFAULT,
    TMP_ENGINE_MARIADB,
    TMP_ENGINE_MEMORY,
    TMP_ENGINE_TEMPTABLE,
    TMP_TABLE_ENGINES,
    innodb_fanout,
    innodb_tree_height,
    innodb_leaf_capacity,
    innodb_btree_shape,
    innodb_index_shapes,
    leaf_range_pages,
    btree_lookup_cost,
    bnl_blocks,
    bnl_cost,
    hash_join_memory,
    hash_join_cost,
    simulate_midpoint_lru,
    simulate_classic_lru,
    filesort_cost,
    tmp_table_cost,
    icp_cost,
    index_merge_cost,
)
from myflames.teach import LESSONS, render_lesson


class TestExplainerFormatting(unittest.TestCase):
    def test_code_spans_render_without_allowing_arbitrary_markup(self):
        from myflames.teach._html import explainer
        rendered = explainer('Example', [
            'Read <code>ib_buffer_pool</code> and <script>alert(1)</script>',
            '<code onclick="alert(1)">unsafe</code>',
        ])
        self.assertIn('<code>ib_buffer_pool</code>', rendered)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', rendered)
        self.assertNotIn('<code onclick=', rendered)


# ---------------------------------------------------------------------------
# Ring 1 — cost-model invariants
# ---------------------------------------------------------------------------

class TestCostModelConstants(unittest.TestCase):
    """Guard the MySQL 8.4 / MariaDB 11.4 defaults: upgrades must break tests."""

    def test_innodb_page_size_default_is_16kib(self):
        self.assertEqual(INNODB_PAGE_SIZE_DEFAULT, 16 * 1024)

    def test_join_buffer_size_default_is_256kib(self):
        """MySQL 8.4 and MariaDB 11.4 both default to 262144 bytes."""
        self.assertEqual(JOIN_BUFFER_SIZE_DEFAULT, 262144)

    def test_mariadb_default_join_cache_level_is_2(self):
        """join_cache_level=2 = plain BNL without hashing."""
        self.assertEqual(MARIADB_JOIN_CACHE_LEVEL_DEFAULT, 2)

    def test_mysql_bnl_removed_version(self):
        """BNL was removed in MySQL 8.0.20."""
        self.assertEqual(MYSQL_BNL_REMOVED_IN, "8.0.20")

    def test_innodb_old_blocks_pct_default(self):
        self.assertEqual(INNODB_OLD_BLOCKS_PCT_DEFAULT, 37)

    def test_innodb_old_blocks_time_default(self):
        self.assertEqual(INNODB_OLD_BLOCKS_TIME_DEFAULT_MS, 1000)


class TestInnoDBBTree(unittest.TestCase):
    def test_fanout_with_8_byte_pk(self):
        """16 KiB page, 8 B BIGINT PK → fan-out around 1200."""
        fan_out = innodb_fanout(key_size=8)
        expected = (INNODB_PAGE_SIZE_DEFAULT - INNODB_PAGE_OVERHEAD_BYTES) // (
            8 + INNODB_CHILD_POINTER_BYTES
        )
        self.assertEqual(fan_out, expected)
        # Sanity: in the same ballpark as the real InnoDB fan-out (~1200)
        self.assertGreater(fan_out, 800)
        self.assertLess(fan_out, 1500)

    def test_fanout_minimum_is_2(self):
        """An absurdly large key still leaves room for at least 2 children."""
        self.assertGreaterEqual(innodb_fanout(key_size=1000), 2)

    def test_tree_height_floor_is_2(self):
        """Even an empty or tiny table has height 2 (root + leaves)."""
        self.assertEqual(innodb_tree_height(rows=0, fan_out=1000), 2)
        self.assertEqual(innodb_tree_height(rows=10, fan_out=1000), 2)

    def test_tree_height_1m_rows(self):
        """1M rows with ~1200 fan-out → 3 levels."""
        fo = innodb_fanout(key_size=8)
        h = innodb_tree_height(rows=1_000_000, fan_out=fo)
        self.assertEqual(h, 3)

    def test_tree_height_1b_rows(self):
        """1B rows with ~1200 fan-out → 4 levels."""
        fo = innodb_fanout(key_size=8)
        h = innodb_tree_height(rows=1_000_000_000, fan_out=fo)
        self.assertEqual(h, 4)

    def test_noncovering_lookup_doubles_pages(self):
        """Non-covering secondary lookup = 2 tree traversals."""
        cost = btree_lookup_cost(
            rows=1_000_000, key_size=8, key_type="secondary_noncovering"
        )
        self.assertEqual(cost.traversals, 2)
        self.assertEqual(cost.pages_touched, 2 * cost.height)

    def test_covering_lookup_is_one_traversal(self):
        cost = btree_lookup_cost(
            rows=1_000_000, key_size=8, key_type="secondary_covering"
        )
        self.assertEqual(cost.traversals, 1)
        self.assertEqual(cost.pages_touched, cost.height)


def _ceil_div(numerator, denominator):
    return (numerator + denominator - 1) // denominator


class TestInnoDBBTreeShape(unittest.TestCase):
    """Pages per level of the clustered and secondary index.

    Hand-computed numbers use the 16 KiB default page: 16384 - 120 = 16264
    usable bytes. ``users`` defaults: 100 B of column data, 24 B email.
    """

    ROW_GRID = [10 ** exp for exp in range(2, 10)]          # 100 .. 1e9
    PAGE_GRID = [kib * 1024 for kib in (4, 8, 16, 32, 64)]  # 4 .. 64 KiB

    def test_record_constants(self):
        """REC_N_NEW_EXTRA_BYTES = 5; DB_TRX_ID 6 B + DB_ROLL_PTR 7 B = 13;
        BIGINT = 8. The 9 B child pointer is the 5 B header + 4 B page number."""
        self.assertEqual(INNODB_RECORD_HEADER_BYTES, 5)
        self.assertEqual(INNODB_CLUSTERED_SYSTEM_BYTES, 13)
        self.assertEqual(PK_BIGINT_BYTES, 8)
        self.assertEqual(INNODB_CHILD_POINTER_BYTES, INNODB_RECORD_HEADER_BYTES + 4)
        # dict_index_get_space_reserve() = page size / 16 for clustered leaves.
        self.assertEqual(INNODB_CLUSTERED_LEAF_RESERVE_DIVISOR, 16)

    def test_leaf_capacity_with_the_clustered_reserve(self):
        """16 KiB page keeping 1024 B free: (16264 - 1024) // 118 = 129."""
        self.assertEqual(innodb_leaf_capacity(118, 16 * 1024, 1024), 129)
        self.assertEqual(innodb_leaf_capacity(118, 4 * 1024, 256), 31)
        self.assertEqual(innodb_leaf_capacity(118, 16 * 1024, 0), innodb_leaf_capacity(118))
        with self.assertRaises(ValueError):
            innodb_leaf_capacity(118, 16 * 1024, -1)

    # -- innodb_leaf_capacity ------------------------------------------------

    def test_leaf_capacity_16kib_page(self):
        """16264 // 118 = 137 (137 * 118 = 16166; 138 * 118 = 16284)."""
        self.assertEqual(innodb_leaf_capacity(118), 137)
        self.assertEqual(innodb_leaf_capacity(118, 16 * 1024), 137)
        # Exactly two records fit at half the usable bytes, one byte more is one.
        self.assertEqual(innodb_leaf_capacity(8132), 2)
        self.assertEqual(innodb_leaf_capacity(8133), 1)

    def test_leaf_capacity_floor_is_one_record(self):
        """An entry bigger than the usable page still counts as one per page."""
        usable = INNODB_PAGE_SIZE_DEFAULT - INNODB_PAGE_OVERHEAD_BYTES
        self.assertEqual(innodb_leaf_capacity(usable), 1)
        self.assertEqual(innodb_leaf_capacity(usable + 1), 1)
        self.assertEqual(innodb_leaf_capacity(100_000), 1)
        self.assertEqual(innodb_leaf_capacity(100_000, 4 * 1024), 1)

    def test_leaf_capacity_rejects_non_positive_sizes(self):
        for entry_bytes, page_size in ((0, 16384), (-1, 16384), (118, 0), (118, -16384)):
            with self.subTest(entry_bytes=entry_bytes, page_size=page_size):
                with self.assertRaises(ValueError):
                    innodb_leaf_capacity(entry_bytes, page_size)

    # -- innodb_btree_shape / innodb_index_shapes ----------------------------

    def test_one_million_users_default_shape(self):
        """Clustered: 100 + 5 + 13 = 118 B records; leaves keep 16384 / 16 =
        1024 B free, so (16264 - 1024) // 118 = 129 per leaf;
        ceil(1e6 / 129) = 7752 leaves, fan-out 16264 // 17 = 956, so 9 pages
        above the leaves and one root. Secondary: 24 + 8 + 5 = 37 B records,
        16264 // 37 = 439 per leaf; node pointers hold email + id (32 B), so
        fan-out is 16264 // 41 = 396; ceil(1e6 / 439) = 2278 leaves, 6 above."""
        shapes = innodb_index_shapes(1_000_000, 100, 24)
        self.assertEqual(set(shapes), {"clustered", "secondary"})
        clustered = shapes["clustered"]
        self.assertEqual(clustered["levels"], [1, 9, 7752])
        self.assertEqual(clustered["fan_out"], 956)
        self.assertEqual(clustered["leaf_capacity"], 129)
        self.assertEqual(clustered["leaf_entry_bytes"], 118)
        self.assertEqual(clustered["leaf_reserve_bytes"], 1024)
        self.assertEqual(clustered["pages"], 1 + 9 + 7752)
        secondary = shapes["secondary"]
        self.assertEqual(secondary["fan_out"], 396)
        self.assertEqual(secondary["leaf_entry_bytes"], 37)
        self.assertEqual(secondary["leaf_capacity"], 439)
        self.assertEqual(secondary["leaf_reserve_bytes"], 0, "no update reserve on secondary leaves")
        self.assertEqual(secondary["levels"], [1, 6, 2278])
        self.assertEqual(secondary["pages"], 1 + 6 + 2278)

    def test_small_table_is_one_page_that_is_root_and_leaf(self):
        shapes = innodb_index_shapes(100, 100, 24)
        self.assertEqual(shapes["clustered"]["levels"], [1])
        self.assertEqual(shapes["secondary"]["levels"], [1])
        self.assertEqual(shapes["clustered"]["pages"], 1)
        self.assertEqual(innodb_btree_shape(0, 118, 8), [1])
        self.assertEqual(innodb_btree_shape(1, 118, 8), [1])
        self.assertEqual(innodb_btree_shape(137, 118, 8), [1])
        # One row more than a page holds: a root above two leaves.
        self.assertEqual(innodb_btree_shape(138, 118, 8), [1, 2])
        # 4 KiB pages: 3976 // 118 = 33 per leaf, ceil(100 / 33) = 4 leaves.
        self.assertEqual(innodb_btree_shape(100, 118, 8, 4 * 1024), [1, 4])

    def test_negative_rows_rejected(self):
        with self.assertRaises(ValueError):
            innodb_btree_shape(-1, 118, 8)
        with self.assertRaises(ValueError):
            innodb_index_shapes(-1, 100, 24)

    def _assert_shape(self, levels, rows, capacity, fan_out):
        self.assertGreaterEqual(len(levels), 1)
        self.assertEqual(levels[0], 1, "a B+tree has one root page")
        leaves = levels[-1]
        self.assertGreaterEqual(leaves * capacity, rows, "the leaves hold every row")
        if rows > 0:
            self.assertLess((leaves - 1) * capacity, rows, "no empty leaf page")
        for upper, lower in zip(levels, levels[1:]):
            self.assertEqual(upper, _ceil_div(lower, fan_out),
                             "each level holds ceil(level below / fan-out) pages")
            self.assertLess(upper, lower, "levels shrink strictly toward the root")

    def test_shape_properties_over_rows_and_page_sizes(self):
        for rows in self.ROW_GRID:
            for page_size in self.PAGE_GRID:
                for row_bytes in (50, 100, 1000):
                    for email_bytes in (16, 24, 64):
                        with self.subTest(rows=rows, page_size=page_size,
                                          row_bytes=row_bytes, email_bytes=email_bytes):
                            shapes = innodb_index_shapes(rows, row_bytes, email_bytes, page_size)
                            reserve = page_size // INNODB_CLUSTERED_LEAF_RESERVE_DIVISOR
                            expected = {
                                "clustered": (row_bytes + INNODB_RECORD_HEADER_BYTES
                                              + INNODB_CLUSTERED_SYSTEM_BYTES, PK_BIGINT_BYTES,
                                              reserve),
                                "secondary": (email_bytes + PK_BIGINT_BYTES
                                              + INNODB_RECORD_HEADER_BYTES,
                                              email_bytes + PK_BIGINT_BYTES, 0),
                            }
                            for name, (leaf_bytes, node_key, leaf_reserve) in expected.items():
                                shape = shapes[name]
                                self.assertEqual(shape["leaf_entry_bytes"], leaf_bytes, name)
                                self.assertEqual(shape["leaf_reserve_bytes"], leaf_reserve, name)
                                self.assertEqual(shape["leaf_capacity"], innodb_leaf_capacity(
                                    leaf_bytes, page_size, leaf_reserve), name)
                                self.assertEqual(shape["fan_out"],
                                                 innodb_fanout(node_key, page_size), name)
                                self.assertEqual(shape["levels"], innodb_btree_shape(
                                    rows, leaf_bytes, node_key, page_size, leaf_reserve), name)
                                self.assertEqual(shape["pages"], sum(shape["levels"]), name)
                                self._assert_shape(shape["levels"], rows,
                                                   shape["leaf_capacity"], shape["fan_out"])

    def test_height_agrees_with_innodb_tree_height(self):
        """The lesson's height model and the page-count shape agree on the
        published BIGINT-PK heights: 1M rows -> 3 levels, 1B rows -> 4."""
        fan_out = innodb_fanout(PK_BIGINT_BYTES)
        for rows, height in ((1_000_000, 3), (1_000_000_000, 4)):
            with self.subTest(rows=rows):
                levels = innodb_index_shapes(rows, 100, 24)["clustered"]["levels"]
                self.assertEqual(len(levels), height)
                self.assertEqual(len(levels), innodb_tree_height(rows, fan_out))

    # -- leaf_range_pages ------------------------------------------------------

    def test_leaf_range_pages_known_values(self):
        """id BETWEEN 42 AND 4000 with 137 rows per leaf: leaves 0..29 = 30."""
        self.assertEqual(leaf_range_pages(42, 4000, 137), 30)
        self.assertEqual(leaf_range_pages(42, 42, 137), 1)
        self.assertEqual(leaf_range_pages(1, 137, 137), 1)
        self.assertEqual(leaf_range_pages(137, 138, 137), 2)
        self.assertEqual(leaf_range_pages(1, 10, 1), 10)

    def test_leaf_range_pages_counts_distinct_leaves(self):
        """Leaf i holds ids i*capacity+1 .. (i+1)*capacity; count them directly."""
        for capacity in (1, 2, 7, 137):
            for first in range(1, 40, 3):
                for last in range(first, first + 300, 11):
                    with self.subTest(capacity=capacity, first=first, last=last):
                        leaves = {(row_id - 1) // capacity for row_id in range(first, last + 1)}
                        self.assertEqual(leaf_range_pages(first, last, capacity), len(leaves))

    def test_leaf_range_pages_rejects_bad_ranges(self):
        for args in ((0, 10, 137), (-5, 10, 137), (10, 9, 137), (1, 10, 0), (1, 10, -1)):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    leaf_range_pages(*args)


class TestBNL(unittest.TestCase):
    def test_one_block_when_outer_fits_buffer(self):
        """Outer rows * row_size <= join_buffer_size → 1 block, 1 inner scan."""
        c = bnl_cost(outer_rows=100, inner_rows=10000, row_size=200)
        self.assertEqual(c.blocks, 1)
        self.assertEqual(c.inner_scans, 1)

    def test_multi_block_when_outer_overflows(self):
        """Outer 10x the buffer → 10 blocks → 10 inner scans."""
        jbs = 262144
        row_size = 200
        rpb = jbs // row_size  # 1310
        outer = rpb * 10
        c = bnl_cost(outer_rows=outer, inner_rows=1000, row_size=row_size, join_buffer_size=jbs)
        self.assertEqual(c.blocks, 10)
        self.assertEqual(c.inner_scans, 10)

    def test_bnl_blocks_helper(self):
        """bnl_blocks() matches ceil(outer_rows / rows_per_block)."""
        self.assertEqual(bnl_blocks(1000, 100, 262144), 1)
        self.assertEqual(bnl_blocks(100000, 200, 262144), 77)  # 100k / (262144/200=1310)


class TestHashJoin(unittest.TestCase):
    def test_small_build_fits_in_memory(self):
        c = hash_join_cost(build_rows=100, probe_rows=1_000_000, row_size=200)
        self.assertFalse(c.spilled)
        self.assertTrue(c.fits_in_memory)
        self.assertEqual(c.partitions, 1)
        self.assertEqual(c.phases, 2)

    def test_big_build_spills_to_disk(self):
        """Build side >> join_buffer_size → grace-hash partitioning."""
        c = hash_join_cost(build_rows=1_000_000, probe_rows=10_000_000, row_size=200)
        self.assertTrue(c.spilled)
        self.assertFalse(c.fits_in_memory)
        self.assertGreaterEqual(c.partitions, 2)
        self.assertEqual(c.phases, 4)

    def test_hash_join_memory_includes_overhead(self):
        """In-memory hash table is ~40% bigger than raw build rows."""
        raw = 1000 * 200
        self.assertGreater(hash_join_memory(1000, 200), raw)


class TestMidpointLRU(unittest.TestCase):
    def test_full_scan_does_not_promote(self):
        """One-pass table scan (each page hit exactly once, in order) must
        not promote anything to the young sublist — that is the whole point
        of scan-resistance. The young sublist must still be empty."""
        pool = 100
        # Unique pages, all accessed at t=0..(3*pool-1) ms (well under 1000 ms gap
        # AND each page only seen once, so no hit event ever happens).
        trace = [(i, i * 5) for i in range(pool * 3)]
        state = simulate_midpoint_lru(pool, trace)
        self.assertEqual(state.promotions, 0)
        self.assertEqual(state.young_pages, 0)
        # All pages should be in old (or evicted); young must stay empty.

    def test_repeated_access_after_1000ms_promotes(self):
        """Same page hit twice, second hit ≥ 1000 ms later → promotion."""
        pool = 50
        trace = [(42, 0)]
        # Fill the old sublist with other pages to make sure page 42 is still alive
        for i in range(10):
            trace.append((100 + i, 100 * i))
        # Hit 42 again at t=2000 ms — well past the 1000 ms threshold
        trace.append((42, 2000))
        state = simulate_midpoint_lru(pool, trace)
        self.assertGreaterEqual(state.promotions, 1)
        self.assertGreaterEqual(state.young_pages, 1)

    def test_repeated_access_before_threshold_does_not_promote(self):
        """A re-hit inside old_blocks_time does NOT promote — that is how
        InnoDB survives full-scan pollution."""
        state = simulate_midpoint_lru(
            pool_size=50,
            access_trace=[(42, 0), (42, 500)],  # 500 ms < 1000 ms threshold
        )
        self.assertEqual(state.promotions, 0)

    def test_pool_split_by_old_pct(self):
        """pool_size=100, old_pct=37 → young 63, old 37."""
        state = simulate_midpoint_lru(pool_size=100, access_trace=[])
        self.assertEqual(state.young_capacity, 63)
        self.assertEqual(state.old_capacity, 37)

    def test_classic_lru_evicts_everything_during_scan(self):
        """Plain LRU with a pool of 50 reading 150 unique pages evicts 100 of them.
        This is the pollution InnoDB's midpoint insertion was designed to prevent."""
        pool = 50
        trace = [(i, i) for i in range(pool * 3)]
        result = simulate_classic_lru(pool, trace)
        self.assertEqual(result["evictions"], pool * 2)  # 150 - 50 = 100
        self.assertEqual(result["hits"], 0)


class TestFilesort(unittest.TestCase):
    def test_sort_buffer_size_default_is_256kib(self):
        self.assertEqual(SORT_BUFFER_SIZE_DEFAULT, 262144)

    def test_small_sort_fits_in_memory(self):
        """100 rows × 200 B = 20 KiB → fits in 256 KiB buffer, no spill."""
        c = filesort_cost(rows=100, row_size=200)
        self.assertEqual(c.sorted_runs, 1)
        self.assertEqual(c.merge_passes, 0)

    def test_large_sort_spills(self):
        """100k rows × 200 B = 20 MiB >> 256 KiB → many sorted runs."""
        c = filesort_cost(rows=100_000, row_size=200)
        self.assertGreater(c.sorted_runs, 1)
        self.assertGreater(c.merge_passes, 0)
        self.assertGreater(c.total_io_rows, 100_000)

    def test_rows_per_run_calculation(self):
        """256 KiB / 200 B = 1310 rows per run."""
        c = filesort_cost(rows=10_000, row_size=200)
        self.assertEqual(c.rows_per_run, 262144 // 200)

    def test_bigger_buffer_means_fewer_runs(self):
        """Doubling sort_buffer_size should halve the number of runs."""
        c1 = filesort_cost(rows=100_000, row_size=200, sort_buffer_size=262144)
        c2 = filesort_cost(rows=100_000, row_size=200, sort_buffer_size=262144 * 2)
        self.assertGreater(c1.sorted_runs, c2.sorted_runs)


class TestTmpTable(unittest.TestCase):
    def test_tmp_table_size_default_is_16mib(self):
        self.assertEqual(TMP_TABLE_SIZE_DEFAULT, 16 * 1024 * 1024)

    def test_max_heap_table_size_default_is_16mib(self):
        self.assertEqual(MAX_HEAP_TABLE_SIZE_DEFAULT, 16 * 1024 * 1024)

    def test_small_table_fits_in_memory(self):
        """1000 rows × 200 B = 200 KiB → fits in 16 MiB."""
        c = tmp_table_cost(rows=1000, row_size=200)
        self.assertTrue(c.fits_in_memory)
        self.assertEqual(c.disk_rows, 0)

    def test_large_table_spills_to_disk(self):
        """500k rows × 200 B = 100 MiB >> 16 MiB → disk conversion."""
        c = tmp_table_cost(rows=500_000, row_size=200)
        self.assertFalse(c.fits_in_memory)
        self.assertGreater(c.disk_rows, 0)

    MIB = 1024 * 1024

    def test_temptable_default_uses_tmp_table_size_only(self):
        """MySQL 8.4's default engine (TempTable) caps each table at
        tmp_table_size; max_heap_table_size does not apply
        (sql_tmp_table.cc:2226, temptable handler.cc:185 in mysql-8.4.9)."""
        c = tmp_table_cost(rows=100, row_size=200,
                           tmp_table_size=64 * self.MIB,
                           max_heap_table_size=16 * self.MIB)
        self.assertEqual(c.engine, "temptable")
        self.assertEqual(c.effective_limit, 64 * self.MIB)
        self.assertEqual(c.limit_source, "tmp_table_size")
        self.assertEqual(c.disk_engine, "InnoDB")
        small = tmp_table_cost(rows=100, row_size=200,
                               tmp_table_size=8 * self.MIB,
                               max_heap_table_size=16 * self.MIB)
        self.assertEqual(small.effective_limit, 8 * self.MIB)

    def test_two_size_call_keeps_working_with_temptable_semantics(self):
        """Callers that pass only the two sizes (positionally or by keyword)
        get MySQL's default TempTable rule."""
        positional = tmp_table_cost(100, 200, 64 * self.MIB, 16 * self.MIB)
        explicit = tmp_table_cost(100, 200, 64 * self.MIB, 16 * self.MIB,
                                  engine=TMP_ENGINE_TEMPTABLE)
        self.assertEqual(positional, explicit)
        self.assertEqual(positional.effective_limit, 64 * self.MIB)

    def test_memory_engine_uses_the_smaller_size(self):
        """internal_tmp_mem_storage_engine=MEMORY (heap_hton) uses
        min(tmp_table_size, max_heap_table_size)."""
        heap_wins = tmp_table_cost(rows=100, row_size=200,
                                   tmp_table_size=64 * self.MIB,
                                   max_heap_table_size=16 * self.MIB,
                                   engine=TMP_ENGINE_MEMORY)
        self.assertEqual(heap_wins.effective_limit, 16 * self.MIB)
        self.assertEqual(heap_wins.limit_source, "max_heap_table_size")
        self.assertEqual(heap_wins.disk_engine, "InnoDB")
        tmp_wins = tmp_table_cost(rows=100, row_size=200,
                                  tmp_table_size=8 * self.MIB,
                                  max_heap_table_size=16 * self.MIB,
                                  engine=TMP_ENGINE_MEMORY)
        self.assertEqual(tmp_wins.effective_limit, 8 * self.MIB)
        self.assertEqual(tmp_wins.limit_source, "tmp_table_size")
        equal = tmp_table_cost(rows=100, row_size=200, engine=TMP_ENGINE_MEMORY)
        self.assertEqual(equal.limit_source, "both")

    def test_mariadb_uses_the_smaller_size_and_spills_to_aria(self):
        """MariaDB: MY_MIN(tmp_memory_table_size, max_heap_table_size), then
        an on-disk Aria table (sql_select.cc:22453, 23438 in mariadb-11.4.9)."""
        c = tmp_table_cost(rows=500_000, row_size=200,
                           tmp_table_size=64 * self.MIB,
                           max_heap_table_size=16 * self.MIB,
                           engine=TMP_ENGINE_MARIADB)
        self.assertEqual(c.effective_limit, 16 * self.MIB)
        self.assertEqual(c.disk_engine, "Aria")
        self.assertFalse(c.fits_in_memory)
        self.assertIn("Aria", c.explanation)
        self.assertNotIn("InnoDB", c.explanation)

    def test_same_settings_fit_under_temptable_but_spill_under_memory(self):
        """The case the old min() rule got wrong: a large tmp_table_size with
        a default max_heap_table_size keeps TempTable in memory."""
        kwargs = dict(rows=100_000, row_size=200,
                      tmp_table_size=64 * self.MIB,
                      max_heap_table_size=16 * self.MIB)
        self.assertTrue(tmp_table_cost(**kwargs).fits_in_memory)
        for engine in (TMP_ENGINE_MEMORY, TMP_ENGINE_MARIADB):
            c = tmp_table_cost(engine=engine, **kwargs)
            self.assertFalse(c.fits_in_memory, engine)
            self.assertEqual(c.memory_rows + c.disk_rows, 100_000)

    def test_explanations_name_the_right_knobs(self):
        spill = dict(rows=500_000, row_size=200)
        temptable = tmp_table_cost(**spill).explanation
        self.assertIn("max_heap_table_size changes nothing", temptable)
        fits = tmp_table_cost(rows=10, row_size=200).explanation
        self.assertIn("temptable_max_ram", fits)
        memory = tmp_table_cost(engine=TMP_ENGINE_MEMORY, **spill).explanation
        self.assertIn("tmp_table_size and max_heap_table_size together", memory)
        self.assertNotIn("temptable_max_ram", memory)

    def test_engine_names_are_case_insensitive_and_validated(self):
        self.assertEqual(tmp_table_cost(10, 200, engine="TempTable").engine, "temptable")
        self.assertEqual(tmp_table_cost(10, 200, engine="MEMORY").engine, "memory")
        self.assertEqual(TMP_TABLE_ENGINES, ("temptable", "memory", "mariadb"))
        with self.assertRaises(ValueError):
            tmp_table_cost(10, 200, engine="InnoDB")

    def test_empty_input_reports_the_engine_limit(self):
        c = tmp_table_cost(rows=0, row_size=200,
                           tmp_table_size=64 * self.MIB,
                           max_heap_table_size=16 * self.MIB)
        self.assertEqual(c.effective_limit, 64 * self.MIB)
        self.assertTrue(c.fits_in_memory)


@unittest.skipUnless(shutil.which("node"), "Node.js is needed to execute lesson JavaScript")
class TestTmpLessonJsMatchesCostModel(unittest.TestCase):
    """tmp.js re-implements tmp_table_cost() for the browser; keep them equal."""

    def test_js_mirror_matches_python_for_every_engine(self):
        import json as _json
        js_path = os.path.join(REPO_DIR, "myflames", "teach", "scan_family", "tmp.js")
        with open(js_path, encoding="utf-8") as f:
            src = f.read() % (TMP_TABLE_SIZE_DEFAULT, MAX_HEAP_TABLE_SIZE_DEFAULT)
        start = src.index("var TMP_ENGINES = {")
        end = src.index("\n}\n", src.index("function tmpTableCost(")) + 3
        mib = 1024 * 1024
        cases = []
        for engine in TMP_TABLE_ENGINES:
            for rows in (100, 83_886, 83_887, 1_000_000):
                for tmp, heap in ((16 * mib, 16 * mib), (64 * mib, 16 * mib), (8 * mib, 16 * mib)):
                    cases.append([rows, 200, tmp, heap, engine])
        script = src[start:end] + (
            "\nvar cases = " + _json.dumps(cases) + ";\n"
            "console.log(JSON.stringify(cases.map(function(a) {"
            " return tmpTableCost(a[0], a[1], a[2], a[3], a[4]); })));\n"
        )
        result = subprocess.run(
            [shutil.which("node"), "-e", script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, encoding="utf-8", timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for args, js in zip(cases, _json.loads(result.stdout)):
            py = tmp_table_cost(*args[:4], engine=args[4])
            with self.subTest(args=args):
                self.assertEqual(js["engine"], py.engine)
                self.assertEqual(js["limit"], py.effective_limit)
                self.assertEqual(js["source"], py.limit_source)
                self.assertEqual(js["fits"], py.fits_in_memory)
                self.assertEqual(js["memRows"], py.memory_rows)
                self.assertEqual(js["diskRows"], py.disk_rows)
                self.assertEqual(js["diskEngine"], py.disk_engine)


class TestICP(unittest.TestCase):
    def test_icp_saves_row_fetches(self):
        """With 20% selectivity, ICP should save 80% of row fetches."""
        c = icp_cost(index_rows_scanned=1000, icp_selectivity=0.20)
        self.assertEqual(c.rows_fetched_without_icp, 1000)
        self.assertEqual(c.rows_fetched_with_icp, 200)
        self.assertEqual(c.rows_saved, 800)

    def test_full_selectivity_saves_nothing(self):
        """With 100% selectivity, ICP saves nothing."""
        c = icp_cost(index_rows_scanned=1000, icp_selectivity=1.0)
        self.assertEqual(c.rows_saved, 0)

    def test_low_selectivity_saves_most(self):
        """With 1% selectivity, ICP saves 99%."""
        c = icp_cost(index_rows_scanned=10_000, icp_selectivity=0.01)
        self.assertEqual(c.rows_fetched_with_icp, 100)
        self.assertEqual(c.rows_saved, 9900)


class TestIndexMerge(unittest.TestCase):
    def test_union_deduplicates(self):
        """Union of 1000 + 800 with 10% overlap = 1000 + 800 - 80 = 1720."""
        c = index_merge_cost(index_a_rows=1000, index_b_rows=800,
                             overlap_pct=0.10, variant="union")
        self.assertEqual(c.merged_rows, 1000 + 800 - 80)

    def test_intersection_keeps_overlap(self):
        """Intersection of 1000 + 800 with 10% overlap = 80."""
        c = index_merge_cost(index_a_rows=1000, index_b_rows=800,
                             overlap_pct=0.10, variant="intersection")
        self.assertEqual(c.merged_rows, 80)

    def test_sort_union_same_as_union(self):
        """Sort-union has the same row count as union (just slower to merge)."""
        c = index_merge_cost(index_a_rows=1000, index_b_rows=800,
                             overlap_pct=0.10, variant="sort_union")
        self.assertEqual(c.merged_rows, 1000 + 800 - 80)

    def test_zero_overlap_union(self):
        """With 0% overlap, union = sum of both sets."""
        c = index_merge_cost(index_a_rows=500, index_b_rows=300,
                             overlap_pct=0.0, variant="union")
        self.assertEqual(c.merged_rows, 800)


# ---------------------------------------------------------------------------
# Ring 2 — version-accuracy assertions on rendered HTML
# ---------------------------------------------------------------------------

class TestLessonHTMLContent(unittest.TestCase):

    def test_bnl_lesson_warns_mysql_84_removed(self):
        """The BNL lesson must prominently call out that MySQL 8.4 removed BNL."""
        html = render_lesson("bnl")
        self.assertIn("not used by MySQL 8.4", html)
        self.assertIn("8.0.20", html)

    def test_btree_lesson_mentions_clustered_and_secondary(self):
        html = render_lesson("btree")
        self.assertIn("clustered", html.lower())
        self.assertIn("secondary", html.lower())

    def test_btree_lesson_mentions_16_kib_default(self):
        html = render_lesson("btree")
        self.assertIn("16 KiB", html)

    def test_btree_lesson_cites_innodb_overhead_constant(self):
        """The learn-more section should cite the exact overhead used in the formula."""
        html = render_lesson("btree")
        self.assertIn(str(INNODB_PAGE_OVERHEAD_BYTES), html)

    def test_hash_lesson_mentions_join_buffer_size(self):
        html = render_lesson("hash")
        self.assertIn("join_buffer_size", html)
        self.assertIn("262144", html)  # the constant, in the slider default

    def test_join_compare_has_both_panels(self):
        """The comparison lesson names both algorithms and both engines."""
        html = render_lesson("join")
        self.assertIn("Block Nested Loop", html)
        self.assertIn("hash join", html.lower())
        self.assertIn("MySQL 8.4", html)
        self.assertIn("MariaDB", html)

    def test_join_compare_honest_about_mariadb_hashed_bnl(self):
        """Must not pretend MariaDB's 'hashed BNL' is the same as MySQL's two-phase hash join."""
        html = render_lesson("join")
        self.assertIn("hashed BNL", html)
        self.assertIn("structurally", html)

    def test_lru_lesson_mentions_midpoint_insertion(self):
        html = render_lesson("lru")
        self.assertIn("midpoint", html.lower())
        self.assertIn("innodb_old_blocks_pct", html)
        self.assertIn("innodb_old_blocks_time", html)

    def test_lru_lesson_teaches_scan_resistance(self):
        html = render_lesson("lru")
        self.assertIn("scan-resistant", html.lower().replace("scan resistant", "scan-resistant"))

    def test_filesort_lesson_mentions_sort_buffer_size(self):
        html = render_lesson("filesort")
        self.assertIn("sort_buffer_size", html)
        self.assertIn("262144", html)

    def test_filesort_lesson_mentions_tmpdir(self):
        html = render_lesson("filesort")
        self.assertIn("tmpdir", html)

    def test_filesort_lesson_mentions_merge(self):
        html = render_lesson("filesort")
        self.assertIn("merge", html.lower())

    def test_tmp_lesson_mentions_both_size_limits(self):
        html = render_lesson("tmp")
        self.assertIn("tmp_table_size", html)
        self.assertIn("max_heap_table_size", html)

    def test_tmp_lesson_mentions_memory_to_disk(self):
        html = render_lesson("tmp")
        self.assertIn("MEMORY", html)
        self.assertIn("on-disk", html)

    def test_tmp_lesson_teaches_engine_specific_limit(self):
        """The lesson must not claim MySQL always uses the smaller of the two
        sizes: TempTable (the 8.4 default) uses tmp_table_size alone."""
        html = render_lesson("tmp")
        # The engine's hint is the select's description (its (?) help tip).
        self.assertIn('<select id="engine" name="engine" aria-describedby="hint-engine">', html)
        self.assertIn('<option value="temptable" selected>', html)
        for needle in ("internal_tmp_mem_storage_engine", "temptable_max_ram",
                       "temptable_max_mmap", "Aria", "max_heap_table_size does not apply"):
            self.assertIn(needle, html)
        for stale in ("MySQL uses min(tmp_table_size, max_heap_table_size)",
                      "Capacity is bounded by min(",
                      "temptable_max_ram</code> (default 1 GiB)"):
            self.assertNotIn(stale, html)

    def test_icp_lesson_mentions_index_condition(self):
        html = render_lesson("icp")
        self.assertIn("Index Condition Pushdown", html)
        self.assertIn("pushed condition", html.lower())

    def test_icp_lesson_shows_without_and_with(self):
        html = render_lesson("icp")
        self.assertIn("Without ICP", html)
        self.assertIn("With ICP", html)

    def test_index_merge_lesson_mentions_variants(self):
        html = render_lesson("index_merge")
        self.assertIn("union", html.lower())
        self.assertIn("intersection", html.lower())
        self.assertIn("sort-union", html.lower().replace("sort_union", "sort-union"))


# ---------------------------------------------------------------------------
# Ring 3 — CLI + HTML scaffolding
# ---------------------------------------------------------------------------

class TestTeachCLI(unittest.TestCase):

    def _run(self, *extra):
        return subprocess.run(
            [sys.executable, "-m", "myflames", "teach", *extra],
            cwd=REPO_DIR, capture_output=True, text=True, encoding="utf-8",
        )

    def test_catalog_with_no_arg(self):
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        for name in LESSONS:
            self.assertIn(name, r.stdout)
        self.assertIn("Usage: myflames teach", r.stdout)

    def test_unknown_lesson_exits_nonzero(self):
        r = self._run("bogus")
        self.assertNotEqual(r.returncode, 0)

    def test_btree_writes_html_file(self):
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
            path = f.name
        try:
            r = self._run("btree", "-o", path)
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(path, encoding="utf-8") as f:
                content = f.read()
            self.assertTrue(content.startswith("<!DOCTYPE html>"))
            self.assertIn("<script>", content)
            self.assertIn('<input type="range"', content)
            self.assertIn('<section class="controls"', content)
        finally:
            os.unlink(path)

    def test_all_lessons_render_via_python_api(self):
        """Every lesson in LESSONS renders a well-formed HTML document."""
        for name in LESSONS:
            html = render_lesson(name)
            self.assertTrue(
                html.startswith("<!DOCTYPE html>"),
                f"lesson {name!r} missing doctype",
            )
            self.assertIn("<script>", html, f"{name} missing script")
            self.assertIn("<style>", html, f"{name} missing style")
            self.assertIn('<section class="controls"', html, f"{name} missing controls")
            self.assertIn("</html>", html, f"{name} missing close")

    def test_lessons_are_offline_self_contained(self):
        """No <script src=, no <link href= — everything must be inline.
        Offline-first is part of the project's hard contract."""
        for name in LESSONS:
            html = render_lesson(name)
            self.assertNotIn("<script src=", html, f"{name} has external script")
            self.assertNotIn("<link href=", html, f"{name} has external stylesheet")

    def test_lessons_have_prefers_reduced_motion(self):
        """Accessibility: every lesson must honour prefers-reduced-motion."""
        for name in LESSONS:
            html = render_lesson(name)
            self.assertIn("prefers-reduced-motion", html, f"{name} missing reduced-motion CSS")

    def test_lessons_use_system_font_stack(self):
        for name in LESSONS:
            html = render_lesson(name)
            self.assertIn("-apple-system", html, f"{name} missing system font")
            self.assertIn("BlinkMacSystemFont", html, f"{name} missing system font")

    def test_all_lessons_embed_shared_anim_runtime(self):
        """Every lesson must pull in the shared `anim` runtime from _anim.py.
        This is what guarantees easing math, timing, and reduced-motion
        behaviour stays consistent across lessons — no copy-paste drift.

        Regression guard: the earlier blank-page bug in btree.html came
        from each lesson hand-rolling its own ad-hoc animation loop. The
        shared runtime eliminates that class of bug.
        """
        import re
        # Signature lines from _anim.py that every lesson must have inlined
        shared_markers = [
            "var anim = (function() {",
            "function easeOutCubic(t)",
            "function easeInOutCubic(t)",
            "function easeOutBack(t)",
            "function tween(opts)",
            "function timeline()",
            "reducedMotion",
        ]
        # And every lesson's own JS must actually *use* the runtime. We allow
        # any of the public API calls — some lessons use anim.timeline()
        # which dispatches to anim.tween internally, others call anim.tween
        # directly.
        usage_any_of = [
            "anim.timeline(", "anim.tween(", "anim.pulse(", "anim.path(",
        ]

        for name in LESSONS:
            html = render_lesson(name)
            for marker in shared_markers:
                self.assertIn(
                    marker, html,
                    f"lesson {name!r} is missing shared runtime marker: {marker!r}",
                )
            # anim.svgEl is used by everyone to create SVG elements
            self.assertIn(
                "anim.svgEl(", html,
                f"lesson {name!r} does not use anim.svgEl — did it bypass the shared runtime?",
            )
            # And must use at least one motion primitive
            self.assertTrue(
                any(u in html for u in usage_any_of),
                f"lesson {name!r} does not call any of {usage_any_of!r} — "
                f"animation is not wired through the shared runtime",
            )

    def test_all_lessons_have_play_button(self):
        """Every lesson must expose a Play button — animation without
        user control is disallowed by the animation-expert skill."""
        import re
        for name in LESSONS:
            html = render_lesson(name)
            # Must have an id="btn-play" element (button or similar)
            self.assertRegex(
                html, r'id="btn-play"',
                f"lesson {name!r} missing id=btn-play — every lesson needs a Play button",
            )

    def test_all_lessons_have_speed_dropdown(self):
        """Every lesson must expose a speed dropdown — user requested the
        ability to control playback speed."""
        for name in LESSONS:
            html = render_lesson(name)
            self.assertIn(
                'id="sel-speed"', html,
                f"lesson {name!r} missing speed dropdown (id=sel-speed)",
            )
            # Must offer at least these values
            for val in ["0.25", "0.5", "1", "2", "4"]:
                self.assertIn(
                    f'value="{val}"', html,
                    f"lesson {name!r} missing speed option {val}×",
                )

    def test_all_lessons_have_loop_toggle_disabled_by_default(self):
        """Every lesson must expose loop toggle and it must start disabled."""
        for name in LESSONS:
            html = render_lesson(name)
            self.assertIn(
                'id="btn-loop"', html,
                f"lesson {name!r} missing loop toggle button",
            )
            self.assertIn(
                "Loop: Off", html,
                f"lesson {name!r} loop toggle should default to off",
            )

    def test_anim_runtime_supports_pause_and_speed(self):
        """Runtime guards: the shared _anim.py must expose setPaused,
        isPaused, setSpeed, and getSpeed so lessons can implement the
        pause-and-speed toolbar."""
        for name in LESSONS:
            html = render_lesson(name)
            for api in ["setPaused", "isPaused", "setSpeed", "getSpeed", "animationDone"]:
                self.assertIn(
                    api, html,
                    f"lesson {name!r} missing {api} from shared anim runtime",
                )

    def test_all_lessons_have_query_card(self):
        """Every lesson must show a real SQL query (in About this example,
        right under the animation) so users see meaningful table names
        instead of 'outer table'."""
        for name in LESSONS:
            html = render_lesson(name)
            self.assertIn(
                'class="query-card"', html,
                f"lesson {name!r} missing query-card (real SQL example)",
            )
            self.assertIn(
                "SELECT", html,
                f"lesson {name!r} query-card has no SELECT statement",
            )

    def test_all_lessons_have_explainer(self):
        """Every lesson must have a 'what you'll see' explainer (in About
        this example, linked from the parameters) so first-time viewers
        understand the animation."""
        for name in LESSONS:
            html = render_lesson(name)
            self.assertIn(
                'class="explainer-card"', html,
                f"lesson {name!r} missing explainer card",
            )
            # The apostrophe is HTML-escaped by esc(), so match both forms
            self.assertTrue(
                ("What you&#x27;ll see" in html) or ("What you'll see" in html),
                f"lesson {name!r} explainer missing 'What you\\'ll see' title",
            )

    def test_lessons_with_scaling_have_complexity_chart(self):
        """Lessons that teach an algorithm with O(…) scaling must include a
        live complexity chart. The LRU lesson uses a 3-act hit/miss story
        instead — its punchline is 8/8 vs 0/8 hits, not a log-log curve."""
        chart_lessons = ["btree", "bnl", "hash", "join", "nested_loop", "filesort", "icp", "index_merge", "full_scan", "non_unique_lookup", "unique_lookup", "filter", "join_order", "semijoin_firstmatch"]
        for name in chart_lessons:
            html = render_lesson(name)
            self.assertIn(
                'id="complexity-chart"', html,
                f"lesson {name!r} missing complexity-chart svg",
            )
            self.assertIn(
                "complexityChart", html,
                f"lesson {name!r} does not call anim.complexityChart",
            )

    def test_lessons_use_meaningful_table_names(self):
        """No lesson should refer to 'outer table' / 'inner table' / 't1 ⋈ t2'
        as the primary animation label — the user asked for real table names.
        We allow the terms in explainer bullets (where they explain the concept)
        but the SQL and chart labels must use real names."""
        # Real table names we expect to see somewhere in each lesson
        expected_tables = {
            "btree": ["users"],
            "bnl": ["customers", "orders"],
            "hash": ["departments", "employees"],
            "join": ["customers", "orders"],
            "nested_loop": ["customers", "orders"],
            "semijoin_firstmatch": ["customers", "orders"],
            "lru": ["events"],
            "filesort": ["orders"],
            "tmp": ["orders"],
            "icp": ["employees"],
            "index_merge": ["products"],
            "full_scan": ["users"],
            "non_unique_lookup": ["users"],
            "unique_lookup": ["users"],
            "filter": ["orders"],
            "join_order": [
                "users", "orders", "line_items", "products",
                "suppliers", "categories", "warehouses",
            ],
        }
        for name, names in expected_tables.items():
            html = render_lesson(name)
            for table in names:
                self.assertIn(
                    table, html,
                    f"lesson {name!r} does not mention real table {table!r}",
                )

    def test_all_lessons_have_scrubber(self):
        """Every lesson must expose a YouTube-style scrubber so users can
        rewind or jump to any point in the animation."""
        for name in LESSONS:
            html = render_lesson(name)
            self.assertIn(
                'id="scrubber"', html,
                f"lesson {name!r} missing id=scrubber range input",
            )
            # min/max/step attributes
            self.assertIn(
                'min="0"', html,
                f"lesson {name!r} scrubber missing min=0",
            )
            self.assertIn(
                'max="1000"', html,
                f"lesson {name!r} scrubber missing max=1000",
            )
            # Time labels left and right of the scrubber
            self.assertIn('id="scrubber-time-current"', html)
            self.assertIn('id="scrubber-time-total"', html)

    def test_anim_timeline_supports_fast_forward_to(self):
        """The shared runtime must expose fastForwardTo for scrubber seeking
        and getTotalDuration / getCurrentTime for the scrubber RAF tracker."""
        for name in LESSONS:
            html = render_lesson(name)
            for api in ["fastForwardTo", "getTotalDuration", "getCurrentTime"]:
                self.assertIn(
                    api, html,
                    f"lesson {name!r} missing timeline.{api} API",
                )

    def test_all_lessons_use_build_reset_toolbar_api(self):
        """Every lesson must hand build+reset closures to wireToolbar so
        the toolbar can rebuild the timeline during scrub seeks."""
        for name in LESSONS:
            html = render_lesson(name)
            # The lesson should call wireToolbar with a {build, reset} object
            # literal, not the legacy positional (playFn, resetFn) signature.
            self.assertIn(
                "build:", html,
                f"lesson {name!r} does not pass build: to wireToolbar",
            )
            self.assertIn(
                "reset:", html,
                f"lesson {name!r} does not pass reset: to wireToolbar",
            )

    def test_chart_lessons_are_interactive(self):
        """Lessons with a complexity chart must have hoverable charts with
        xSlider click-to-update binding."""
        chart_lessons = ["btree", "bnl", "hash", "join", "nested_loop", "filesort", "icp", "index_merge", "full_scan", "non_unique_lookup", "unique_lookup", "filter", "join_order", "semijoin_firstmatch"]
        for name in chart_lessons:
            html = render_lesson(name)
            self.assertIn(
                "xSlider:", html,
                f"lesson {name!r} chart does not bind xSlider for click-to-update",
            )
            self.assertIn(
                "createSVGPoint", html,
                f"lesson {name!r} chart missing hover svgPointFromEvent logic",
            )

    def test_join_lesson_clarifies_row_pair_comparisons(self):
        """Regression: the comparison lesson used to say 'rows examined'
        with a giant number that confused users — '10.10B of what?'. Make
        sure we now say 'row-pair comparisons' so the unit is obvious."""
        html = render_lesson("join")
        self.assertIn(
            "row-pair comparison", html,
            "join lesson must label BNL cost as 'row-pair comparisons', not 'rows examined'",
        )

    def test_all_lessons_include_bootstrap_runtime(self):
        """Embedded report mode needs teachRuntime.bootstrapFromObject(ctx)."""
        for name in LESSONS:
            html = render_lesson(name)
            self.assertIn(
                "bootstrapFromObject", html,
                "lesson {!r} missing bootstrap runtime function".format(name),
            )

    def test_full_scan_lesson_explains_every_row_is_read(self):
        html = render_lesson("full_scan")
        self.assertIn("full scan reads every row", html)
        self.assertIn("O(log n + k)", html)
        self.assertIn("country='US'", html)

    def test_non_unique_lookup_lesson_mentions_row_id_fetches(self):
        html = render_lesson("non_unique_lookup")
        self.assertIn("row-id", html)
        self.assertIn("Index range scan", html)
        self.assertIn("non-covering", html)

    def test_unique_lookup_lesson_mentions_single_row(self):
        html = render_lesson("unique_lookup")
        self.assertIn("Single-row", html)
        self.assertIn("covering", html)

    def test_filter_lesson_mentions_predicate_evaluation(self):
        html = render_lesson("filter")
        self.assertIn("Filter operator", html)
        self.assertIn("WHERE", html)
        self.assertIn("incoming row", html)

    def test_semijoin_firstmatch_lesson_teaches_early_out(self):
        """FirstMatch flagship: the punchline is the short-circuit on the
        first matching inner row. Lock down the one-concept framing."""
        html = render_lesson("semijoin_firstmatch")
        self.assertIn("FirstMatch", html)
        self.assertIn("semijoin", html.lower())
        # The IN (subquery) query and its existence framing.
        self.assertIn("IN (", html)
        self.assertIn("does-it-exist", html.lower())
        # The early-out / short-circuit vocabulary is the punchline.
        self.assertIn("short-circuit", html.lower())
        # optimizer_switch flag that controls the strategy.
        self.assertIn("firstmatch=on", html)
        # Both the naive and the FirstMatch curves are named.
        self.assertIn("Naive join", html)


class TestJoinOrderLesson(unittest.TestCase):
    """The planner-family lesson claims a factorial worst case and cites
    real MySQL source-tree facts. Lock those claims down so a future
    edit can't silently rewrite the cost model."""

    def setUp(self):
        self.html = render_lesson("join_order")

    def test_names_the_actual_algorithm(self):
        # The function in sql/sql_planner.cc is greedy_search; the per-step
        # helper is best_extension_by_limited_search.
        self.assertIn("greedy_search", self.html)
        self.assertIn("best_extension_by_limited_search", self.html)

    def test_states_factorial_complexity(self):
        # The big claim the user asked us to surface: planning is O(N!)
        # under exhaustive search.
        self.assertIn("O(N!)", self.html)

    def test_cites_real_session_variables(self):
        self.assertIn("optimizer_search_depth", self.html)
        self.assertIn("optimizer_prune_level", self.html)

    def test_documents_default_search_depth(self):
        # MAX_TABLES+1 = 62; verified in sql/sys_vars.cc Sys_optimizer_search_depth.
        self.assertIn("62", self.html)
        self.assertIn("MAX_TABLES", self.html)

    def test_mentions_hypergraph_alternative(self):
        # MySQL 8.0+ also has the DPhyp set-based optimizer.
        self.assertIn("hypergraph", self.html.lower())
        self.assertIn("optimizer_max_subgraph_pairs", self.html)

    def test_quotes_source_comment(self):
        # The lesson must surface the literal complexity claim from
        # sql/sql_planner.cc so readers can verify it themselves.
        self.assertIn("search_depth", self.html)
        self.assertIn("complexity", self.html.lower())

    def test_lesson_is_in_planner_family(self):
        from myflames.teach import LESSONS
        self.assertEqual(LESSONS["join_order"]["family"], "planner_family")

    def test_in_curriculum(self):
        from myflames.teach import CURRICULUM
        self.assertIn("join_order", CURRICULUM)

    def test_example_uses_7_tables(self):
        # The lesson's example query has 7 tables; the SQL must mention
        # all seven, plus the "7!" or "5,040" hook that motivates the
        # factorial cost.
        self.assertIn("categories", self.html)
        self.assertIn("warehouses", self.html)
        self.assertTrue(
            "7! = 5,040" in self.html or "5,040" in self.html,
            "lesson missing the 7! = 5,040 hook"
        )

    def test_animation_distinguishes_two_algorithms(self):
        # The greedy_search animation walks a search tree of partial
        # orderings; the hypergraph (DPhyp) animation walks a query
        # graph and enumerates connected subgraphs. Both must be
        # implemented in the same lesson JS.
        self.assertIn("buildGreedyStage",      self.html)
        self.assertIn("buildHypergraphStage",  self.html)
        self.assertIn("buildGreedyTimeline",   self.html)
        self.assertIn("buildHypergraphTimeline", self.html)
        # The hypergraph view uses the DPhyp-specific vocabulary.
        self.assertIn("CSG", self.html)          # connected subgraph
        self.assertIn("Moerkotte", self.html)    # original DPhyp paper

    def test_hypergraph_cost_caps_at_max_subgraph_pairs(self):
        # The lesson's hypergraph cost model must respect
        # optimizer_max_subgraph_pairs as a cap.
        self.assertIn("OPTIMIZER_MAX_SUBGRAPH_PAIRS", self.html)
        self.assertIn("100000", self.html)



class TestLessonTitlesSentenceCase(unittest.TestCase):
    """Learn lists the catalog titles and family labels, and every lesson page
    shows its own title: all in sentence case. Product, engine and optimizer
    names keep their spelling (InnoDB, MySQL, FirstMatch, B+tree)."""

    # Words that keep a capital inside a sentence-case title.
    PROPER = {"InnoDB", "MySQL", "MariaDB", "FirstMatch", "B+tree", "EXPLAIN",
              "WHERE", "LRU", "BKA", "MRR", "BNL"}

    def assertSentenceCase(self, text, where):
        # Quoted EXPLAIN output ("Using index") is quoted, not titled.
        unquoted = re.sub(r'"[^"]*"', "", text)
        words = re.findall(r"[A-Za-z][A-Za-z+'-]*", unquoted)
        self.assertTrue(words and words[0][0].isupper(), f"{where}: {text!r} starts with a capital")
        for word in words[1:]:
            bare = word.split("'")[0]
            titled = bare[:1].isupper() and bare[1:].islower() and len(bare) > 1
            self.assertFalse(titled and bare not in self.PROPER,
                             f"{where}: {text!r} capitalizes {bare!r}; use sentence case")

    def test_catalog_titles_and_family_labels(self):
        from myflames.teach import LESSONS, _FAMILY_LABELS
        for key, lesson in LESSONS.items():
            self.assertSentenceCase(lesson["title"], f"catalog title of {key}")
            self.assertNotIn("`", lesson["summary"], f"{key}: the summary is plain text, not Markdown")
        for key, label in _FAMILY_LABELS.items():
            self.assertSentenceCase(label, f"family label {key}")

    def test_lesson_page_titles(self):
        from myflames.teach import LESSONS, render_lesson
        import html as html_lib
        for key in LESSONS:
            page = render_lesson(key)
            title = re.search(r"<title>(.*?)</title>", page, re.S)
            self.assertIsNotNone(title, key)
            self.assertSentenceCase(html_lib.unescape(title.group(1)), f"page title of {key}")
            for chip in re.findall(r'<span class="family-chip">.*?</span>([^<]*)</span>', page):
                self.assertSentenceCase(chip, f"family chip of {key}")


class TestLessonFitAndFullScreen(unittest.TestCase):
    """Every lesson's stage drawing fits the window or frame showing it, and
    the shared chrome carries the full-screen focus layout: the workspace
    switches it by message, a standalone page uses its own button. Browser
    behavior is covered by apps/workspace/test/learn-fullscreen.mjs."""

    @classmethod
    def setUpClass(cls):
        cls.pages = {key: render_lesson(key) for key in LESSONS}

    def test_fit_rule_caps_the_stage_drawing_at_the_viewport(self):
        for key, page in self.pages.items():
            rule = re.search(
                r"\.stage-with-phases svg, section\.stage > svg, \.stage-wrap > svg \{(.*?)\}",
                page, re.S)
            self.assertIsNotNone(rule, f"{key}: no fit rule for stage drawings")
            self.assertIn("max-height: max(160px, calc(100vh - var(--stage-chrome, 96px)))",
                          rule.group(1), key)
            # The room above the drawing is measured, never guessed per lesson.
            self.assertIn('setProperty("--stage-chrome"', page, key)
            # Every stage drawing sits where the fit rule and the focus layout find it.
            stage = re.search(r'<section class="stage".*?</section>', page, re.S)
            self.assertIsNotNone(stage, key)
            self.assertIn('class="stage-with-phases"', stage.group(0), key)

    def test_focus_layout_hook_and_controls(self):
        for key, page in self.pages.items():
            self.assertIn('html[data-focus="true"]', page, key)
            self.assertIn('<main id="lesson-main">', page, key)
            self.assertIn('id="btn-fullscreen" type="button" '
                          'aria-label="Show the lesson animation full screen"', page, key)
            self.assertIn('id="btn-details" type="button" class="focus-only" '
                          'aria-expanded="false" aria-controls="lesson-main"', page, key)
            # Embedded in the workspace, the workspace's own button is the one shown.
            self.assertIn('html[data-embedded="true"] #btn-fullscreen', page, key)
            # A standalone page asks the browser and falls back to the layout alone.
            self.assertIn("root.requestFullscreen()", page, key)
            self.assertIn('addEventListener("fullscreenchange"', page, key)

    def test_focus_message_accepts_only_the_parent_and_a_fixed_type(self):
        for key, page in self.pages.items():
            listener = re.search(
                r'window\.addEventListener\("message", function\(event\) \{(.*?)\n  \}\);',
                page[page.index("var teachFocus"):], re.S)
            self.assertIsNotNone(listener, f"{key}: no focus message listener")
            body = listener.group(1)
            self.assertIn("event.source !== window.parent", body, key)
            self.assertIn("data.type !== MESSAGE", body, key)
            self.assertIn('typeof data.focus !== "boolean"', body, key)
            self.assertIn('var MESSAGE = "myflames-lesson-focus";', page, key)

    def test_escape_leaves_full_screen(self):
        page = self.pages["full_scan"]
        self.assertIn('event.key !== "Escape" || !state.on', page)
        # Asking the workspace to leave carries no lesson data.
        self.assertIn("window.parent.postMessage({ type: MESSAGE, focus: false }", page)


class TestLessonPageOrder(unittest.TestCase):
    """The stage comes first: every lesson opens with its parameters in one
    compact row, then the stage, then About this example (the query card and
    the explainer), then the numbers and Learn more. render_page decides the
    order once for all lessons, so the DOM order is the visual order.
    Measured in the workspace by apps/workspace/test/learn-guide.mjs."""

    @classmethod
    def setUpClass(cls):
        cls.pages = {key: render_lesson(key) for key in LESSONS}

    @staticmethod
    def main_of(page):
        return page[page.index('<main id="lesson-main">'):page.index("</main>")]

    @staticmethod
    def section(main, cls_name):
        found = re.search(r'<section class="%s".*?</section>' % cls_name, main, re.S)
        return found.group(0) if found else ""

    def test_sections_follow_the_stage_first_order(self):
        for key, page in self.pages.items():
            with self.subTest(lesson=key):
                main = self.main_of(page)
                order = [main.find(marker) for marker in (
                    '<section class="controls"', '<section class="stage"',
                    '<section class="about-example"')]
                self.assertTrue(all(at >= 0 for at in order), (key, order))
                self.assertEqual(order, sorted(order), key)
                for later in ('<section class="readout"', '<details class="learn-more"'):
                    if later in main:
                        self.assertGreater(main.index(later), order[2], (key, later))

    def test_about_this_example_holds_the_query_and_the_explainer(self):
        for key, page in self.pages.items():
            with self.subTest(lesson=key):
                main = self.main_of(page)
                about = self.section(main, "about-example")
                self.assertIn('id="about-example"', about)
                self.assertIn('<h2 id="about-example-h">About this example</h2>', about)
                self.assertEqual(about.count('class="query-card"'), 1, key)
                self.assertEqual(about.count('class="explainer-card"'), 1, key)
                # The query comes before the explainer, and nothing else
                # on the page carries either card.
                self.assertLess(about.index("query-card"), about.index("explainer-card"))
                self.assertEqual(main.count('class="query-card"'), 1, key)
                self.assertEqual(main.count('class="explainer-card"'), 1, key)
                stage = self.section(main, "stage")
                self.assertNotIn("query-card", stage, key)
                self.assertNotIn("explainer-card", stage, key)
                # The parameters link to it.
                controls = self.section(main, "controls")
                self.assertIn('<a class="about-link" href="#about-example">About this example', controls)

    def test_parameter_hints_become_help_tips_described_by_their_control(self):
        for key, page in self.pages.items():
            with self.subTest(lesson=key):
                controls = self.section(self.main_of(page), "controls")
                self.assertNotIn('<div class="hint"', controls, key)
                ids = re.findall(r'\bid="([^"]*)"', page[:page.index("<script>")])
                self.assertEqual(len(ids), len(set(ids)), f"{key}: duplicate ids")
                tips = re.findall(r'<span class="help-tip" aria-hidden="true">\?'
                                  r'<span class="tip-text" id="([^"]+)">', controls)
                for tip in tips:
                    described = re.findall(
                        r'<(?:input|select)\b[^>]*\baria-describedby="(?:[^"]* )?%s(?: [^"]*)?"'
                        % re.escape(tip), controls)
                    self.assertEqual(len(described), 1, f"{key}: {tip} describes one control")
                # The (?) sits in the control's head beside its label, never
                # inside the label (a click must not toggle a checkbox).
                for head in re.findall(r'<div class="control-head">(.*?)</div>', controls, re.S):
                    label = re.search(r"<label\b.*?</label>", head, re.S).group(0)
                    self.assertNotIn("help-tip", label, key)
        # Every hint the lessons wrote survives as a tip, lesson ids included.
        self.assertIn('<span class="tip-text" id="max-heap-hint">', self.pages["tmp"])
        self.assertIn('id="max_heap" name="max_heap" min="65536" max="134217728" step="65536" '
                      'value="16777216" aria-describedby="max-heap-hint">', self.pages["tmp"])
        self.assertIn('<input type="range" id="rows" name="rows" min="1" max="9" step="1" value="6" '
                      'aria-describedby="hint-rows">', self.pages["btree"])
        # A control without a hint keeps its label alone (join's row counts).
        self.assertIn('<label for="outer_rows"><code>customers</code> rows: '
                      '<span class="value-pill" data-pill-for="outer_rows">50000</span></label>\n'
                      '      <input type="range" id="outer_rows" name="outer_rows"', self.pages["join"])

    def test_arrange_lesson_moves_cards_and_hints(self):
        from myflames.teach._html import arrange_lesson, explainer, query_card
        controls = (
            '<section class="controls">\n  <h2>Parameters</h2>\n  <div class="control-grid">\n'
            '    <div class="control">\n      <label for="a">A <span class="value-pill">1</span></label>\n'
            '      <input type="range" id="a" name="a" aria-describedby="note">\n'
            '      <div class="hint">Use <code>a</code>.</div>\n    </div>\n'
            '    <div class="control">\n      <label for="b">B</label>\n'
            '      <input type="checkbox" id="b" name="b">\n'
            '      <div class="hint" id="b-hint">Kept id.</div>\n    </div>\n'
            '    <div class="control">\n      <label for="c">C</label>\n'
            '      <input type="range" id="c" name="c">\n    </div>\n'
            '  </div>\n</section>')
        stage = ('<section class="stage">' + query_card("SELECT 1", "note")
                 + explainer("What you'll see", ["x"]) + '<div class="stage-toolbar"></div></section>')
        out_controls, out_stage, about = arrange_lesson(controls, stage)
        self.assertEqual(re.sub(r"\s+", "", out_stage),
                         '<sectionclass="stage"><divclass="stage-toolbar"></div></section>')
        self.assertIn('<div class="query-card">', about)
        self.assertLess(about.index("query-card"), about.index("explainer-card"))
        self.assertIn('<div class="controls-head"><h2>Parameters</h2><a class="about-link" '
                      'href="#about-example">', out_controls)
        # Hint markup is kept; an existing description is extended, not replaced.
        self.assertIn('<span class="tip-text" id="hint-a">Use <code>a</code>.</span>', out_controls)
        self.assertIn('<input type="range" id="a" name="a" aria-describedby="note hint-a">', out_controls)
        # A lesson's own hint id stays (its script may rewrite the text).
        self.assertIn('<span class="tip-text" id="b-hint">Kept id.</span>', out_controls)
        self.assertIn('<input type="checkbox" id="b" name="b" aria-describedby="b-hint">', out_controls)
        # No hint, no tip.
        self.assertIn('<label for="c">C</label>\n      <input type="range" id="c" name="c">', out_controls)
        self.assertNotIn('class="hint"', out_controls)
        # Without cards there is no About section and no link to it.
        bare_controls, _, bare_about = arrange_lesson(controls, "<section class=\"stage\"></section>")
        self.assertEqual(bare_about, "")
        self.assertNotIn("about-link", bare_controls)
        # A generated id never collides with an id already on the page.
        clash = ('<div class="control"><label for="x">X</label><input id="x">'
                 '<div class="hint">h</div></div><p id="hint-x"></p>')
        self.assertIn('id="hint-x-2"', arrange_lesson(clash, "")[0])

    def test_fit_counts_the_parameters_only_while_cheap(self):
        page = self.pages["btree"]
        fit = page[page.index("  function fit() {"):page.index("  var fitFrame = 0;")]
        # What sits above the toolbar counts while it takes at most a quarter
        # of the window and costs the drawing at most 15% of its scale.
        self.assertIn("above <= window.innerHeight / 4", fit)
        self.assertIn(">= 0.85 * kept", fit)
        self.assertIn('root.style.setProperty("--stage-chrome"', fit)

    def test_in_page_links_never_navigate_the_frame(self):
        # In the workspace a lesson is an srcdoc frame whose base address is
        # the workspace's own; "#about-example" must scroll, not navigate.
        for key, page in self.pages.items():
            with self.subTest(lesson=key):
                self.assertIn("closest('a[href^=\"#\"]')", page)
                self.assertIn("event.preventDefault();\n    var heading", page)

    def test_committed_lesson_pages_match_the_renderer(self):
        # docs/teach holds every lesson as rendered; regenerate with
        #   python3 -c "from myflames.teach import LESSONS, FAMILY_DIRS; [print(k, FAMILY_DIRS[v['family']]) for k, v in sorted(LESSONS.items())]" |
        #     while read lesson dir; do python3 -m myflames teach "$lesson" -q -o "docs/teach/$dir/$lesson.html"; done
        from myflames.teach import FAMILY_DIRS
        for key, page in self.pages.items():
            with self.subTest(lesson=key):
                path = os.path.join(REPO_DIR, "docs", "teach",
                                    FAMILY_DIRS[LESSONS[key]["family"]], key + ".html")
                with open(path, encoding="utf-8") as handle:
                    self.assertTrue(handle.read() == page, f"{path} is stale; regenerate it")


class TestLessonGuides(unittest.TestCase):
    """Guided lessons: simplified code that follows the step, Check yourself
    and Explore. Content is declarative (GUIDE in the lesson module) and the
    lesson's timeline marks each step with tl.beat(kind). Browser behavior is
    covered by apps/workspace/test/learn-guide.mjs."""

    FLAGSHIPS = ("btree", "nested_loop", "full_scan")
    GUIDE_RE = re.compile(r'<script type="application/json" id="lesson-guide">(.*?)</script>', re.S)

    @classmethod
    def setUpClass(cls):
        import json as _json
        cls.pages = {key: render_lesson(key) for key in LESSONS}
        cls.guides = {}
        for key, page in cls.pages.items():
            found = cls.GUIDE_RE.search(page)
            if found:
                cls.guides[key] = _json.loads(found.group(1))

    def lesson_js(self, key):
        from myflames.teach import _html
        page = self.pages[key]
        start = page.index("<script>") + len("<script>")
        script = page[start:page.index("</script>", start)]
        # The lesson's own code sits between the shared runtime and the guide UI.
        return script[script.index(_html._FOCUS_JS) + len(_html._FOCUS_JS):script.index(_html._GUIDE_JS)]

    def test_flagship_lessons_declare_a_guide(self):
        for key in self.FLAGSHIPS:
            self.assertIn(key, self.guides, f"{key} has no guide content")

    INDEX_FAMILY = ("unique_lookup", "non_unique_lookup", "icp", "index_merge",
                    "skip_scan", "rowid_filter")

    def test_index_family_lessons_declare_a_guide(self):
        for key in self.INDEX_FAMILY:
            self.assertIn(key, self.guides, f"{key} has no guide content")
            self.assertIn("Ready: press Next step, or Play to run it all", self.pages[key], key)
            # The explainer names Next step and the simplified code.
            self.assertIn("simplified code beside the drawing", self.pages[key], key)

    # Scan family lessons that follow the full_scan flagship.
    SCAN_FAMILY = ("covering_index", "derived_table", "filesort", "filter", "tmp")

    def test_scan_family_lessons_declare_a_guide(self):
        for key in self.SCAN_FAMILY:
            with self.subTest(lesson=key):
                self.assertIn(key, self.guides, f"{key} has no guide content")
                page = self.pages[key]
                self.assertIn("Ready: press Next step, or Play to run it all", page)
                self.assertIn("simplified code beside the drawing", page)

    def test_scan_family_text_matches_the_server_source(self):
        # MySQL has no radix sort in filesort; MariaDB's applies to keys of
        # at most 20 bytes (radixsort_is_applicable in mysys/mf_radix.c).
        filesort = self.pages["filesort"]
        self.assertNotIn("16 B triggers radix", filesort)
        self.assertIn("MariaDB radix sorts", filesort)
        self.assertNotIn('id="radix-svg"', filesort)
        # max_length_for_sort_data has no effect in MySQL 8.4 and 9.x.
        self.assertNotIn("(default 4096 in MySQL 8.4)", filesort)
        # A GROUP BY temp table holds one row per group, not per input row.
        self.assertIn("GROUP BY customer_id", self.pages["tmp"])
        # The derived table's key is in place before rows are written.
        self.assertNotIn("creating auto-index on temp table", self.pages["derived_table"])
        # One drawing for the covering index lesson (labels share one scale).
        covering = self.pages["covering_index"]
        self.assertIn('id="ci-svg"', covering)
        self.assertNotIn('id="svg-index"', covering)
        self.assertEqual(
            self.pages["derived_table"],
            __import__("myflames.teach.scan_family.derived_table", fromlist=["render"]).render())

    # The join family follows its flagship (nested_loop).
    JOIN_FAMILY = ("bka_join", "bnl", "hash", "join", "semijoin_firstmatch", "semijoin_weedout")

    def test_join_family_lessons_declare_a_guide(self):
        for key in self.JOIN_FAMILY:
            self.assertIn(key, self.guides, f"{key} has no guide content")
            page = self.pages[key]
            # Each page names itself, so the workspace can find it (bka_join used "bka").
            self.assertIn(f'<body data-lesson="{key}">', page)
            self.assertIn("Ready: press Next step, or Play to run it all", page)

    # The cache family (lru is its flagship) and the planner family.
    CACHE_AND_PLANNER = ("lru", "buffer_pool_warmup", "join_order")

    def test_cache_and_planner_lessons_declare_a_guide(self):
        for key in self.CACHE_AND_PLANNER:
            self.assertIn(key, self.guides, f"{key} has no guide content")

    def test_guided_lessons_keep_the_toolbar_out_of_the_inputs(self):
        # Explore moves section.controls into the step bar, so the toolbar
        # (and the step bar after it) must not live inside that section.
        controls_re = re.compile(r'<section class="controls".*?</section>', re.S)
        for key in self.guides:
            with self.subTest(lesson=key):
                for block in controls_re.findall(self.pages[key]):
                    self.assertNotIn('id="btn-play"', block)
                    self.assertNotIn('id="scrubber"', block)

    def test_every_declared_guide_is_valid(self):
        from myflames.teach._html import normalize_guide, VERIFIED_SERVER_FUNCTIONS
        for key, guide in self.guides.items():
            with self.subTest(lesson=key):
                # The embedded JSON is already the normalized form.
                self.assertEqual(normalize_guide(guide), guide)
                ids = [line["id"] for line in guide["code"]["lines"]]
                self.assertEqual(len(ids), len(set(ids)))
                for kind, lines in guide["beats"].items():
                    self.assertTrue(set(lines) <= set(ids), kind)
                # Every line is reached by some step.
                used = {line for lines in guide["beats"].values() for line in lines}
                self.assertEqual(used, set(ids), f"{key}: lines no step highlights")
                self.assertTrue(2 <= len(guide["quiz"]) <= 4)
                for question in guide["quiz"]:
                    self.assertTrue(2 <= len(question["choices"]) <= 4)
                    self.assertEqual(sum(c["correct"] for c in question["choices"]), 1)
                    for choice in question["choices"]:
                        self.assertRegex(choice["why"], r"^[^.!?]*(\.\d[^.!?]*)*[.!?]$")
                for line in guide["code"]["lines"]:
                    if line["fn"]:
                        self.assertIn(line["fn"], VERIFIED_SERVER_FUNCTIONS)

    def test_every_step_kind_in_the_timeline_has_lines(self):
        for key, guide in self.guides.items():
            with self.subTest(lesson=key):
                js = self.lesson_js(key)
                self.assertIn("tl.beat(", js)
                literal = set(re.findall(r'tl\.beat\(\s*"([a-z0-9-]+)"', js))
                strings = set(re.findall(r'"([a-z][a-z0-9-]*)"', js))
                self.assertTrue(literal <= set(guide["beats"]), literal - set(guide["beats"]))
                # Kinds built at run time still appear as literals in the source.
                self.assertTrue(set(guide["beats"]) <= strings,
                                f"{key}: guide kinds the timeline never names: "
                                f"{set(guide['beats']) - strings}")

    def test_lessons_without_a_guide_still_render(self):
        # Every shipped lesson has a guide now; a lesson rendered without one
        # (guide=None) must still work, with the guide UI stopping at once.
        from unittest import mock
        from myflames.teach import _html
        real = _html.render_page
        with mock.patch.object(_html, "render_page", lambda **kw: real(**dict(kw, guide=None))):
            from myflames.teach.scan_family import full_scan
            pages = {"full_scan": full_scan.render()}
        for key, page in pages.items():
            self.assertNotIn('id="lesson-guide"', page, key)
            self.assertIn("<!DOCTYPE html>", page)
            self.assertIn('id="btn-play"', page, key)
            # The guide UI is shared but stops at once without content.
            self.assertIn('if (!source || typeof teachRuntime === "undefined"', page, key)

    def test_guide_ui_uses_text_only_and_needs_no_network(self):
        from myflames.teach import _html
        self.assertNotIn("innerHTML", _html._GUIDE_JS)
        self.assertNotIn("insertAdjacentHTML", _html._GUIDE_JS)
        for key in self.guides:
            page = self.pages[key]
            self.assertNotIn("<script src=", page)
            # The quiz is page-only: no storage, no requests.
            for api in ("localStorage", "sessionStorage", "fetch(", "XMLHttpRequest", "sendBeacon"):
                self.assertNotIn(api, _html._GUIDE_JS, api)

    def test_guide_json_cannot_end_its_script_element(self):
        from myflames.teach._html import guide_json_html
        guide = {
            "code": {"title": "t</script><b>", "lines": [
                ("a", 0, "x < y && y > z </script>"), ("b", 1, "b"), ("c", 0, "c")]},
            "beats": {"go": ["a", "b", "c"]},
            "quiz": [{"question": "q?", "choices": [("a", True, "Yes."), ("b", False, "No.")]},
                     {"question": "r?", "choices": [("a", False, "No."), ("b", True, "Yes.")]}],
        }
        html_text = guide_json_html(guide)
        body = html_text[len('<script type="application/json" id="lesson-guide">'):-len("</script>")]
        self.assertNotIn("<", body)
        self.assertNotIn(">", body)
        self.assertNotIn("&", body)
        import json as _json
        self.assertEqual(_json.loads(body)["code"]["lines"][0]["text"], "x < y && y > z </script>")

    def test_normalize_guide_refuses_bad_content(self):
        from myflames.teach._html import normalize_guide
        good = {
            "code": {"title": "Scan", "lines": [("open", 0, "open"), ("read", 1, "read"),
                                                ("end", 0, "end")]},
            "beats": {"start": ["open"], "row": ["read"], "done": ["end"]},
            "quiz": [{"question": "One?", "choices": [("a", True, "Right."), ("b", False, "No.")]},
                     {"question": "Two?", "choices": [("a", False, "No."), ("b", True, "Yes.")]}],
        }
        normalize_guide(good)

        def broken(change):
            import copy
            guide = copy.deepcopy(good)
            change(guide)
            with self.assertRaises(ValueError):
                normalize_guide(guide)

        broken(lambda g: g["beats"].update(row=["missing"]))
        broken(lambda g: g["code"]["lines"].append(("open", 0, "again")))
        broken(lambda g: g["code"]["lines"].__setitem__(0, ("Open", 0, "bad id")))
        broken(lambda g: g["code"]["lines"].__setitem__(0, ("open", 0, "open", "made_up_function")))
        broken(lambda g: g.__setitem__("quiz", g["quiz"][:1]))
        broken(lambda g: g["quiz"][0]["choices"].__setitem__(1, ("b", True, "Also right.")))
        broken(lambda g: g["quiz"][0]["choices"].__setitem__(1, ("b", False, "Two. Sentences.")))
        broken(lambda g: g["quiz"][0]["choices"].__setitem__(1, ("b", False, "No ending")))
        broken(lambda g: g.__setitem__("extra", 1))

    def test_fit_rule_holds_with_the_code_panel_open(self):
        for key in self.guides:
            page = self.pages[key]
            side = re.search(r"\n\.stage-side \{(.*?)\}", page, re.S)
            self.assertIsNotNone(side, key)
            # Beside the drawing, the code column is never taller than the
            # drawing may be, so it cannot push the drawing off screen.
            self.assertIn("max-height: max(160px, calc(100vh - var(--stage-chrome, 96px)))", side.group(1))
            self.assertIn("overflow: auto", side.group(1))
            # In full screen the column is not mistaken for a drawing.
            self.assertIn(".stage-with-phases > :not(.phase-nav):not(.stage-side) {", page)
            # A narrow window puts it under the drawing.
            narrow = re.search(r"@media \(max-width: 900px\) \{\s*html\[data-guided=\"true\"\] "
                               r"\.stage-with-phases \{\s*flex-wrap: wrap;", page)
            self.assertIsNotNone(narrow, key)
            # Full screen moves the step bar with the toolbar and scrubber.
            self.assertIn('move(document.querySelector(".guide-bar"), top);', page)
            # Escape in the quiz closes the quiz, not the lesson's full screen.
            self.assertIn('event.key !== "Escape" || !state.on || event.defaultPrevented', page)


class TestIndexFamilyLessonFacts(unittest.TestCase):
    """What the index-family lessons draw and say, checked against the
    mysql-server and mariadb-server sources (docs/_lane_notes/LEARN-INDEX.md
    names the file and function for each claim). Browser behavior is covered
    by apps/workspace/test/learn-guide.mjs."""

    def lesson_parts(self, key):
        from myflames.teach import _html
        page = render_lesson(key)
        start = page.index("<script>") + len("<script>")
        script = page[start:page.index("</script>", start)]
        js = script[script.index(_html._FOCUS_JS) + len(_html._FOCUS_JS):script.index(_html._GUIDE_JS)]
        return page[:page.index("<script>")], js

    def test_btree_covering_lookup_draws_the_covering_secondary_index(self):
        _, js = self.lesson_parts("btree")
        # The covering option draws the secondary index it walks, not the
        # clustered tree with id=42.
        self.assertIn('keyType === "secondary_covering"', js)
        self.assertIn("Covering secondary B+tree", js)
        self.assertIn('tl.beat(covering ? "covered" : "done")', js)

    def test_unique_lookup_walks_the_clustered_index_from_its_root(self):
        html_text, js = self.lesson_parts("unique_lookup")
        # A secondary record holds the primary key, not a page address, so the
        # non-covering lookup walks the clustered index again (row0sel.cc,
        # Row_sel_get_clust_rec_for_mysql): one page per level, not one fetch.
        for kind in ("clus-root", "clus-internal", "clus-leaf"):
            self.assertIn('"%s"' % kind, js)
        self.assertIn("O(2 log n)", html_text + js)
        self.assertNotIn("O(log n + 1)", html_text + js)
        # A real UNIQUE secondary index, not a second index on the primary key.
        self.assertIn("uq_users_email", html_text)
        self.assertNotIn("uq_users_id", html_text + js)
        # A const table is read while the plan is made (sql_optimizer.cc).
        self.assertIn("Rows fetched before execution", html_text)

    def test_non_unique_lookup_reads_the_covering_choice_as_text(self):
        _, js = self.lesson_parts("non_unique_lookup")
        # "false" is a non-empty string: !!c.covering made every run covering.
        self.assertNotIn("!!c.covering", js)
        self.assertIn('String(teachRuntime.readControls().covering) === "true"', js)
        # The run ends at the first entry with another key (row0sel.cc,
        # ROW_SEL_EXACT), drawn as the next country's entry.
        self.assertIn('var NEXT_KEY = {key: "UY"', js)

    def test_skip_scan_query_is_covered_by_its_index(self):
        html_text, js = self.lesson_parts("skip_scan")
        # MySQL plans a skip scan only on a covering index
        # (index_skip_scan_plan.cc, cause "query_references_nonkey_column").
        self.assertIn("SELECT id, gender, age FROM employees", html_text)
        self.assertNotIn("SELECT * FROM employees\nWHERE", html_text)
        # Entries below 25 and above the first one past 30 are never read.
        self.assertIn('"never read"', js)
        self.assertIn('"next-prefix"', js)

    def test_rowid_filter_is_a_sorted_array_of_primary_keys(self):
        html_text, js = self.lesson_parts("rowid_filter")
        # Rowid_filter_sorted_array in sql/rowid_filter.h; checked by binary
        # search; EXPLAIN shows range|filter and Using rowid filter.
        self.assertNotIn("bitmap", (html_text + js).lower())
        self.assertIn("sorted array", html_text)
        self.assertIn("Using rowid filter", html_text)
        self.assertIn("range|filter", html_text)
        self.assertNotIn("<strong>Rowid-ordered scan</strong> or", html_text)

    def test_index_merge_query_follows_the_variant(self):
        html_text, js = self.lesson_parts("index_merge")
        # Intersection is for AND, sort-union for OR of ranges.
        self.assertIn("  AND  supplier_id = 12;", js)
        self.assertIn("category_id < 5", js)
        self.assertNotIn("A composite index is almost always better", html_text)


class TestLessonSourceCorrections(unittest.TestCase):
    """Corrections from a source-checked review of the Learn lessons. Each
    claim names the server file that decides it; docs/_lane_notes/LESSONFIX.md
    gives the lines."""

    @classmethod
    def setUpClass(cls):
        import json as _json
        keys = ("filesort", "lru", "icp", "bnl", "join_order", "index_merge", "bka_join",
                "skip_scan", "rowid_filter", "semijoin_firstmatch", "semijoin_weedout",
                "unique_lookup", "non_unique_lookup", "covering_index", "tmp", "derived_table",
                "hash", "buffer_pool_warmup")
        cls.pages = {key: render_lesson(key) for key in keys}
        cls.guides = {}
        for key, page in cls.pages.items():
            found = TestLessonGuides.GUIDE_RE.search(page)
            if found:
                cls.guides[key] = _json.loads(found.group(1))

    def line(self, key, line_id):
        for line in self.guides[key]["code"]["lines"]:
            if line["id"] == line_id:
                return line
        self.fail("%s has no line %r" % (key, line_id))

    def filesort_cost_js(self):
        from myflames.teach.scan_family import filesort
        js_path = os.path.join(REPO_DIR, "myflames", "teach", "scan_family", "filesort.js")
        with open(js_path, encoding="utf-8") as f:
            src = f.read() % (filesort.SORT_BUFFER_SIZE_DEFAULT,
                              filesort.MARIADB_SORT_BUFFER_SIZE_DEFAULT)
        end = src.index("\n}\n", src.index("function filesortCost(")) + 3
        return src[src.index("var SORT_BUFFER_SIZE_DEFAULT"):end]

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed to execute lesson JavaScript")
    def test_filesort_radix_follows_the_sort_key_not_the_row(self):
        # MariaDB: radixsort_is_applicable(count, param->sort_length) in
        # sql/filesort_utils.cc and mysys/mf_radix.c. The key is order_date, a
        # DATE (3 bytes, never packed: sql/filesort.cc try_to_pack_sortkeys),
        # so the row size cannot decide the radix path. MySQL has no radix sort.
        import json as _json
        from myflames.teach.scan_family import filesort
        self.assertEqual(filesort.MARIADB_SORT_BUFFER_SIZE_DEFAULT, 2 * 1024 * 1024)
        mysql_default = filesort.SORT_BUFFER_SIZE_DEFAULT
        maria_default = filesort.MARIADB_SORT_BUFFER_SIZE_DEFAULT
        cases = []
        for size in (4, 20, 21, 100, 200):
            # The lesson's default 10,000 rows with each server's default buffer.
            cases.append([10000, size, maria_default, 0, "mariadb"])
            cases.append([10000, size, mysql_default, 0, "mysql"])
            cases.append([10000, size, maria_default, 0, "mysql"])
        cases.append([10000, 200, mysql_default, 0, "mariadb"])  # spills: 1,310 rows per run
        cases.append([500, 200, maria_default, 0, "mariadb"])    # under 1,000 rows
        cases.append([10000, 4096, maria_default, 0, "mariadb"])  # 512 rows per run
        script = self.filesort_cost_js() + (
            "\nvar cases = " + _json.dumps(cases) + ";\n"
            "console.log(JSON.stringify(cases.map(function(a) {"
            " return filesortCost(a[0], a[1], a[2], a[3], a[4]); })));\n"
        )
        result = subprocess.run(
            [shutil.which("node"), "-e", script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, encoding="utf-8", timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        costs = _json.loads(result.stdout)
        got = {tuple(args): cost for args, cost in zip(cases, costs)}
        for size in (4, 20, 21, 100, 200):
            with self.subTest(row_size=size):
                maria = got[(10000, size, maria_default, 0, "mariadb")]
                # The MariaDB default case: all 10,000 rows in one 2 MiB buffer, radix sorted.
                self.assertEqual(maria["sortAlg"], "radix")
                self.assertFalse(maria["spill"])
                self.assertNotEqual(got[(10000, size, mysql_default, 0, "mysql")]["sortAlg"], "radix")
                self.assertEqual(got[(10000, size, maria_default, 0, "mysql")]["sortAlg"], "comparison")
        spilled = got[(10000, 200, mysql_default, 0, "mariadb")]
        self.assertTrue(spilled["spill"])
        self.assertEqual(spilled["rpr"], 1310)
        self.assertEqual(spilled["sortAlg"], "radix", "each full run of 1,310 rows is radix sorted")
        self.assertEqual(got[(500, 200, maria_default, 0, "mariadb")]["sortAlg"], "comparison")
        self.assertEqual(got[(10000, 4096, maria_default, 0, "mariadb")]["sortAlg"], "comparison")

    def test_filesort_text_names_the_sort_key_and_both_defaults(self):
        page = self.pages["filesort"]
        self.assertIn("MariaDB radix sort (sort key of 20 B or less, 1,000 to 99,999 rows in "
                      "the buffer, keys not packed)", page)
        self.assertNotIn("row size 20 B or less", page)
        self.assertNotIn("Sort key + payload", page)
        # sort_buffer_size: 256 KiB in MySQL; DEFAULT(MAX_SORT_MEMORY) = 2 MiB
        # in MariaDB (sql/sys_vars.cc, sql/sql_const.h).
        self.assertIn("Default: 256 KiB on MySQL, 2 MiB on MariaDB.", page)
        self.assertRegex(page, r'<select id="server" name="server" aria-describedby="hint-server">\s*'
                               r'<option value="mysql" selected>')
        self.assertIn("var MARIADB_SORT_BUFFER_SIZE_DEFAULT = 2097152;", page)

    def test_lru_takes_a_free_page_before_it_evicts(self):
        # buf_LRU_get_free_block takes a free page and scans the LRU tail only
        # when none is left (storage/innobase/buf/buf0lru.cc, both servers).
        self.assertEqual(self.line("lru", "full")["text"], "if no free page is left:")
        page = self.pages["lru"]
        self.assertNotIn("old sublist is full", page)
        self.assertIn("buf_LRU_get_free_block", page)
        # A new page goes just behind the first old page (LRU_old).
        self.assertRegex(page, r"just\s+behind the first page of the old sublist")
        self.assertNotIn("(head of old)", page)
        self.assertNotIn("old head (the midpoint)", page)

    def test_icp_names_every_engine_that_supports_it(self):
        # HA_DO_INDEX_COND_PUSHDOWN: InnoDB and MyISAM in MySQL; also Aria and
        # MyRocks in MariaDB.
        page = self.pages["icp"]
        self.assertIn("InnoDB or MyISAM in MySQL; also Aria and\n    MyRocks in MariaDB", page)
        self.assertNotIn("The table is InnoDB or MyISAM", page)

    def test_bnl_names_the_join_cache_switches(self):
        # MariaDB sql/sql_select.cc check_join_cache_usage and the default
        # optimizer_switch in sql/sql_priv.h.
        page = self.pages["bnl"]
        for flag in ("join_cache_incremental", "join_cache_hashed", "join_cache_bka"):
            self.assertIn("<code>%s</code>" % flag, page)
        self.assertNotIn("(0–8), not <code>optimizer_switch</code>", page)

    def test_join_order_counts_eq_ref_groups_and_both_prune_levels(self):
        # N = non-eq_ref tables + eq_ref groups (sql_planner.cc), and MySQL's
        # prune level 1 already groups eq_ref chains.
        page = self.pages["join_order"]
        self.assertIn("N=4 (three\n    tables plus one eq_ref group)", page)
        self.assertNotIn("N=3", page)
        self.assertIn("debug builds by default", page)
        quiz = self.guides["join_order"]["quiz"][1]
        right = [c for c in quiz["choices"] if c["correct"]][0]
        self.assertIn("MySQL's level 1 also adds a chain of eq_ref joins as one group", right["why"])

    def test_index_merge_drops_the_repeated_id_before_its_row_is_read(self):
        quiz = self.guides["index_merge"]["quiz"][0]
        right = [c for c in quiz["choices"] if c["correct"]][0]
        self.assertIn("drops the repeated id before its row is read", right["why"])
        self.assertNotIn("before any row is read", self.pages["index_merge"])

    def test_bka_join_needs_an_index_that_does_not_cover_the_query(self):
        # Both choose_mrr_impl refuse DS-MRR for HA_MRR_INDEX_ONLY (MySQL
        # sql/handler.cc, MariaDB sql/multi_range_read.cc).
        self.assertIn("an index-only\n    read), MySQL and MariaDB both refuse the batched "
                      "Multi-Range Read", self.pages["bka_join"])

    def test_code_lines_name_the_verified_functions(self):
        expected = {
            ("skip_scan", "first"): "IndexSkipScanIterator::Read",
            ("skip_scan", "range"): "IndexSkipScanIterator::Read",
            ("skip_scan", "next"): "index_next_different",
            ("index_merge", "union"): "RowIDUnionIterator::Read",
            ("index_merge", "inter"): "RowIDIntersectionIterator::Read",
            ("index_merge", "sort"): "IndexMergeIterator::Init",
            ("icp", "icp"): "row_search_idx_cond_check",
            # A single-table lookup on a unique key is a const table: read_const,
            # not EQRefIterator (eq_ref in a join).
            ("unique_lookup", "unique"): "read_const",
            ("non_unique_lookup", "next"): "ha_innobase::index_next_same",
            ("covering_index", "stop"): "row_search_mvcc",
            ("hash", "build"): "HashJoinIterator::BuildHashTable",
            ("hash", "probe"): "HashJoinIterator::Read",
            ("bka_join", "fill"): "BKAIterator::ReadOuterRows",
            ("bka_join", "sort"): "DsMrr_impl::dsmrr_fill_buffer",
            ("bka_join", "read"): "DsMrr_impl::dsmrr_next",
            ("semijoin_weedout", "loop"): "WeedoutIterator::Read",
            ("semijoin_weedout", "insert"): "do_sj_dups_weedout",
            ("tmp", "group"): "TemptableAggregateIterator::Init",
            ("tmp", "convert"): "create_ondisk_from_heap",
            ("derived_table", "create"): "Table_ref::generate_keys",
            ("derived_table", "write"): "MaterializeIterator::Init",
            ("derived_table", "spill"): "create_ondisk_from_heap",
            ("filesort", "pq"): "check_if_pq_applicable",
            ("filesort", "sort"): "Filesort_buffer::sort_buffer",
            # The final merge; merge_many_buff only adds passes above 15 runs.
            ("filesort", "merge"): "merge_buffers",
            ("lru", "access"): "buf_page_get_gen",
            ("lru", "old-time"): "buf_page_peek_if_too_old",
            ("lru", "promote"): "buf_LRU_make_block_young",
            ("lru", "evict"): "buf_LRU_free_from_common_LRU_list",
            ("lru", "insert"): "buf_LRU_add_block",
            ("buffer_pool_warmup", "check"): "buf_page_get_gen",
            ("buffer_pool_warmup", "read"): "buf_read_page",
            ("buffer_pool_warmup", "dump"): "buf_dump",
            ("buffer_pool_warmup", "load"): "buf_load",
            ("join_order", "loop"): "Optimize_table_order::greedy_search",
            ("join_order", "each"): "Optimize_table_order::best_extension_by_limited_search",
            ("join_order", "cost"): "Optimize_table_order::best_access_path",
        }
        for (key, line_id), fn in expected.items():
            with self.subTest(lesson=key, line=line_id):
                self.assertEqual(self.line(key, line_id)["fn"], fn)

    def test_every_lesson_in_a_family_shows_its_chip(self):
        for key, label in (("skip_scan", "Index access"), ("rowid_filter", "Index access"),
                           ("bka_join", "Join operator"), ("semijoin_firstmatch", "Join operator"),
                           ("semijoin_weedout", "Join operator")):
            with self.subTest(lesson=key):
                self.assertRegex(self.pages[key],
                                 r'<span class="family-chip">.*?</span>%s</span>' % re.escape(label))


class TestTimelineSteps(unittest.TestCase):
    """The shared timeline's guided-step API, run in node: tl.beat marks a
    step, fastForwardToStep shows its end without animating or pulsing, and
    continueTo plays live up to the next step's start and holds there."""

    def run_node(self, body, reduced):
        from myflames.teach._anim import ANIM_JS
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        prelude = (
            "var raf = 0; var window = { matchMedia: function() { return { matches: %s }; } };\n"
            "function requestAnimationFrame(fn) { raf++; return setTimeout(function() { fn(Date.now()); }, 1); }\n"
            % ("true" if reduced else "false")
        )
        result = subprocess.run(
            [node, "-e", prelude + ANIM_JS + "\n" + body],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        import json as _json
        return _json.loads(result.stdout.strip().splitlines()[-1])

    def test_steps_hold_at_boundaries_and_continue(self):
        out = self.run_node(r"""
var log = [];
var tl = anim.timeline();
tl.beat("one");
tl.call(function() { log.push("a"); });
tl.add({ from: 0, to: 1, duration: 30, onUpdate: function(v) {}, onComplete: function() { log.push("tween"); } });
tl.delay(20);
tl.beat("two");
tl.call(function() { log.push("b"); });
tl.delay(20);
tl.beat("three");
tl.call(function() { log.push("c"); });
var beats = tl.getBeats().map(function(b) { return [b.kind, b.step]; });
tl.fastForwardToStep(tl.getBeats()[1].step);
var held = { log: log.slice(), cursor: tl.getCursor(), isHeld: tl.isHeld() };
tl.continueTo(tl.getBeats()[2].step, function() {
  var second = { log: log.slice(), cursor: tl.getCursor(), isHeld: tl.isHeld() };
  tl.continueTo(-1, null, function() {
    console.log(JSON.stringify({ beats: beats, held: held, second: second, end: log, cursor: tl.getCursor(), steps: tl.getStepCount() }));
  });
});
""", reduced=True)
        self.assertEqual(out["beats"], [["one", 0], ["two", 3], ["three", 5]])
        self.assertEqual(out["held"], {"log": ["a", "tween"], "cursor": 3, "isHeld": True})
        self.assertEqual(out["second"], {"log": ["a", "tween", "b"], "cursor": 5, "isHeld": True})
        self.assertEqual(out["end"], ["a", "tween", "b", "c"])
        self.assertEqual(out["cursor"], out["steps"])

    def test_jumping_to_a_step_skips_pulses(self):
        out = self.run_node(r"""
var el = { attrs: {}, setAttribute: function(k, v) { this.attrs[k] = v; } };
var tl = anim.timeline();
tl.beat("one");
tl.add({ from: 0, to: 1, duration: 10, onComplete: function() { anim.pulse(el, 3, 1, 100); } });
tl.beat("two");
tl.call(function() {});
var before = raf;
tl.fastForwardToStep(1);
var jumped = raf - before;
tl.fastForwardTo(5);
var seeked = raf - before;
anim.pulse(el, 3, 1, 100);
console.log(JSON.stringify({ jumped: jumped, seeked: seeked, live: raf - before }));
process.exit(0);
""", reduced=False)
        self.assertEqual(out["jumped"], 0)
        self.assertEqual(out["seeked"], 0)
        self.assertGreater(out["live"], 0)


if __name__ == "__main__":
    unittest.main()
