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
    SORT_BUFFER_SIZE_DEFAULT,
    TMP_TABLE_SIZE_DEFAULT,
    MAX_HEAP_TABLE_SIZE_DEFAULT,
    innodb_fanout,
    innodb_tree_height,
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

    def test_effective_limit_is_min(self):
        """Effective limit is min(tmp_table_size, max_heap_table_size)."""
        c = tmp_table_cost(rows=100, row_size=200,
                           tmp_table_size=8 * 1024 * 1024,
                           max_heap_table_size=16 * 1024 * 1024)
        self.assertEqual(c.effective_limit, 8 * 1024 * 1024)


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
        chart_lessons = ["btree", "bnl", "hash", "join", "nested_loop", "filesort", "icp", "index_merge", "full_scan", "non_unique_lookup", "unique_lookup", "filter"]
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
            "lru": ["events"],
            "filesort": ["orders"],
            "tmp": ["employees"],
            "icp": ["employees"],
            "index_merge": ["products"],
            "full_scan": ["users"],
            "non_unique_lookup": ["users"],
            "unique_lookup": ["users"],
            "filter": ["orders"],
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
        chart_lessons = ["btree", "bnl", "hash", "join", "nested_loop", "filesort", "icp", "index_merge", "full_scan", "non_unique_lookup", "unique_lookup", "filter"]
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


if __name__ == "__main__":
    unittest.main()
