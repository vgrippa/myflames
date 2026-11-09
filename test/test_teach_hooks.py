import os
import sys
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_DIR)

from myflames.parser import parse_explain
from myflames.teach_hooks import build_teach_hooks, build_teach_index_maps


def _load(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestTeachHooks(unittest.TestCase):
    HASH = os.path.join(TEST_DIR, "mysql-explain-hash-join.json")
    BNL = os.path.join(TEST_DIR, "mysql-explain-bnl.json")
    MDB_BNL = os.path.join(TEST_DIR, "mariadb-explain-block-nl-join.json")
    FULL_SCAN = os.path.join(TEST_DIR, "fixtures", "explain-001-table-scan-users-no-filter.json")
    SAMPLE = os.path.join(TEST_DIR, "mysql-explain-json-sample.json")

    @unittest.skipUnless(os.path.exists(HASH), "fixture missing")
    def test_hash_join_maps_to_hash_lesson(self):
        hooks = build_teach_hooks(parse_explain(_load(self.HASH)), query_sql="SELECT 1")
        lessons = [h.get("lesson") for h in hooks]
        self.assertIn("hash", lessons)
        hook = [h for h in hooks if h.get("lesson") == "hash"][0]
        self.assertIn("build_rows", hook.get("controls", {}))
        self.assertIn("probe_rows", hook.get("controls", {}))
        self.assertEqual(hook.get("query_sql"), "SELECT 1")

    @unittest.skipUnless(os.path.exists(BNL), "fixture missing")
    def test_bnl_fixture_maps_to_bnl(self):
        hooks = build_teach_hooks(parse_explain(_load(self.BNL)))
        lessons = [h.get("lesson") for h in hooks]
        self.assertIn("bnl", lessons)
        hook = [h for h in hooks if h.get("lesson") == "bnl"][0]
        self.assertIn("outer_rows", hook.get("controls", {}))
        self.assertIn("inner_rows", hook.get("controls", {}))

    @unittest.skipUnless(os.path.exists(MDB_BNL), "fixture missing")
    def test_mariadb_block_nl_maps_to_bnl(self):
        hooks = build_teach_hooks(parse_explain(_load(self.MDB_BNL)))
        self.assertTrue(any(h.get("lesson") == "bnl" for h in hooks))

    @unittest.skipUnless(os.path.exists(HASH), "fixture missing")
    def test_index_map_contains_folded_labels(self):
        hooks = build_teach_hooks(parse_explain(_load(self.HASH)))
        maps = build_teach_index_maps(hooks)
        self.assertIn("by_folded_label", maps)
        self.assertIsInstance(maps["by_folded_label"], dict)
        self.assertGreater(len(maps["by_folded_label"]), 0)

    @unittest.skipUnless(os.path.exists(FULL_SCAN), "fixture missing")
    def test_full_scan_fixture_maps_to_full_scan_lesson(self):
        hooks = build_teach_hooks(parse_explain(_load(self.FULL_SCAN)))
        full_scan_hooks = [h for h in hooks if h.get("lesson") == "full_scan"]
        self.assertTrue(full_scan_hooks)
        self.assertIn("rows", full_scan_hooks[0].get("controls", {}))
        self.assertIn("selectivity", full_scan_hooks[0].get("controls", {}))

    @unittest.skipUnless(os.path.exists(SAMPLE), "fixture missing")
    def test_sample_fixture_maps_index_range_scan_to_non_unique_lookup(self):
        hooks = build_teach_hooks(parse_explain(_load(self.SAMPLE)))
        non_unique = [h for h in hooks if h.get("lesson") == "non_unique_lookup"]
        self.assertTrue(non_unique)

    @unittest.skipUnless(os.path.exists(os.path.join(TEST_DIR, "mysql-explain-complex-join.json")), "fixture missing")
    def test_complex_fixture_maps_filter_unique_lookup_and_nested_loop(self):
        hooks = build_teach_hooks(parse_explain(_load(os.path.join(TEST_DIR, "mysql-explain-complex-join.json"))))
        lessons = [h.get("lesson") for h in hooks]
        self.assertIn("filter", lessons)
        self.assertIn("unique_lookup", lessons)
        self.assertIn("nested_loop", lessons)


class TestTmpLessonEngineHook(unittest.TestCase):
    """The tmp lesson's limit depends on the in-memory engine, so a hook
    built from a real server must open the lesson on that server's engine."""

    @staticmethod
    def _tmp_controls(variables):
        node = {
            "details": {"operation": "Temporary table with deduplication"},
            "folded_label": "Temporary table",
            "rows": 5000, "loops": 1, "children": [],
        }
        hooks = build_teach_hooks(node, variables=variables)
        tmp = [h for h in hooks if h.get("lesson") == "tmp"]
        assert tmp, hooks
        return tmp[0]["controls"]

    def test_mysql_temptable_engine(self):
        c = self._tmp_controls({"version": "8.4.3",
                                "internal_tmp_mem_storage_engine": "TempTable",
                                "tmp_table_size": "67108864",
                                "max_heap_table_size": "16777216"})
        self.assertEqual(c["engine"], "temptable")
        self.assertEqual(c["tmp_size"], 64 * 1024 * 1024)
        self.assertEqual(c["max_heap"], 16 * 1024 * 1024)

    def test_mysql_memory_engine(self):
        c = self._tmp_controls({"version": "8.4.3",
                                "internal_tmp_mem_storage_engine": "MEMORY"})
        self.assertEqual(c["engine"], "memory")

    def test_mariadb_version_wins(self):
        c = self._tmp_controls({"version": "11.4.4-MariaDB-log"})
        self.assertEqual(c["engine"], "mariadb")

    def test_unknown_engine_leaves_lesson_default(self):
        self.assertNotIn("engine", self._tmp_controls({"version": "8.4.3"}))
        self.assertNotIn("engine", self._tmp_controls(None))


if __name__ == "__main__":
    unittest.main()
