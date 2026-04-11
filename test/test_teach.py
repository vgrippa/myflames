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
import subprocess
import sys
import tempfile
import unittest

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from myflames.teach._cost_model import (
    INNODB_PAGE_SIZE_DEFAULT,
    INNODB_PAGE_OVERHEAD_BYTES,
    INNODB_CHILD_POINTER_BYTES,
    JOIN_BUFFER_SIZE_DEFAULT,
    MARIADB_JOIN_CACHE_LEVEL_DEFAULT,
    MYSQL_BNL_REMOVED_IN,
    INNODB_OLD_BLOCKS_PCT_DEFAULT,
    INNODB_OLD_BLOCKS_TIME_DEFAULT_MS,
    innodb_fanout,
    innodb_tree_height,
    btree_lookup_cost,
    bnl_blocks,
    bnl_cost,
    hash_join_memory,
    hash_join_cost,
    simulate_midpoint_lru,
    simulate_classic_lru,
)
from myflames.teach import LESSONS, render_lesson


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

    def test_all_five_lessons_render_via_python_api(self):
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


if __name__ == "__main__":
    unittest.main()
