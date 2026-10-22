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
            universal_newlines=True, timeout=10,
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
        self.assertIn('<select id="engine" name="engine">', html)
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
            cwd=REPO_DIR, capture_output=True, text=True,
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
        """Every lesson must show a real SQL query above the animation so
        users see meaningful table names instead of 'outer table'."""
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
        """Every lesson must start with a 'what you'll see' explainer so
        first-time viewers understand the animation before pressing Play."""
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
            "tmp": ["employees"],
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

if __name__ == "__main__":
    unittest.main()
