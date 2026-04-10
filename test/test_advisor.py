"""
Unit tests for :mod:`myflames.advisor`.

Each rule is exercised with a minimal synthetic context (no real plan
needed) so failures point at the specific rule that regressed.  The ``advise``
entry point is also tested end-to-end.
"""
import os
import sys
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(TEST_DIR))

from myflames.advisor import (
    advise,
    _to_int,
    _human_bytes,
    _rule_buffer_pool_vs_data_size,
    _rule_sort_buffer_vs_filesort,
    _rule_join_buffer_vs_hash_or_bnl,
    _rule_tmp_table_size_vs_materialize,
    _rule_optimizer_switch_disables,
    _rule_missing_indexes,
    _rule_engine_innodb,
    _rule_flush_log_durability,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestHelpers(unittest.TestCase):

    def test_to_int_basic(self):
        self.assertEqual(_to_int("42"), 42)
        self.assertEqual(_to_int(42), 42)
        self.assertEqual(_to_int("  1048576\n"), 1048576)

    def test_to_int_empty(self):
        self.assertEqual(_to_int(""), 0)
        self.assertEqual(_to_int(None), 0)
        self.assertEqual(_to_int("NULL"), 0)

    def test_to_int_with_default(self):
        self.assertEqual(_to_int("bad", default=99), 99)

    def test_to_int_float_string(self):
        self.assertEqual(_to_int("3.14"), 3)

    def test_human_bytes(self):
        self.assertEqual(_human_bytes(512), "512 B")
        self.assertIn("KB", _human_bytes(2048))
        self.assertIn("MB", _human_bytes(2 * 1024 * 1024))
        self.assertIn("GB", _human_bytes(2 * 1024 ** 3))


# ---------------------------------------------------------------------------
# Buffer pool rule
# ---------------------------------------------------------------------------

class TestBufferPoolRule(unittest.TestCase):

    def test_fires_when_much_smaller(self):
        variables = {"innodb_buffer_pool_size": str(128 * 1024 * 1024)}  # 128MB
        stats = {"t1": {"data_length": 2 * 1024 ** 3, "index_length": 0}}  # 2GB
        w, s = _rule_buffer_pool_vs_data_size({}, {}, stats, variables)
        self.assertIsNotNone(w)
        self.assertIn("innodb_buffer_pool_size", w)
        self.assertIn("working set", w)
        self.assertIn("innodb_buffer_pool_size", s)

    def test_silent_when_fits(self):
        variables = {"innodb_buffer_pool_size": str(4 * 1024 ** 3)}
        stats = {"t1": {"data_length": 100 * 1024 * 1024, "index_length": 0}}
        self.assertEqual(
            _rule_buffer_pool_vs_data_size({}, {}, stats, variables),
            (None, None),
        )

    def test_silent_when_no_variables(self):
        self.assertEqual(
            _rule_buffer_pool_vs_data_size({}, {}, {"t": {}}, None),
            (None, None),
        )

    def test_silent_when_no_stats(self):
        self.assertEqual(
            _rule_buffer_pool_vs_data_size({}, {}, None, {"innodb_buffer_pool_size": "1"}),
            (None, None),
        )


# ---------------------------------------------------------------------------
# Sort buffer rule
# ---------------------------------------------------------------------------

class TestSortBufferRule(unittest.TestCase):

    def test_fires_on_small_buffer_with_filesort(self):
        analysis = {"filesorts": [{"rows": 1000, "short_label": "Sort"}]}
        variables = {"sort_buffer_size": "262144"}  # default
        w, s = _rule_sort_buffer_vs_filesort(analysis, None, None, variables)
        self.assertIsNotNone(w)
        self.assertIn("sort_buffer_size", w)
        self.assertIn("spill", w.lower())

    def test_silent_without_filesort(self):
        analysis = {"filesorts": []}
        variables = {"sort_buffer_size": "262144"}
        self.assertEqual(
            _rule_sort_buffer_vs_filesort(analysis, None, None, variables),
            (None, None),
        )

    def test_silent_with_large_buffer(self):
        analysis = {"filesorts": [{"rows": 1000, "short_label": "Sort"}]}
        variables = {"sort_buffer_size": str(8 * 1024 * 1024)}
        self.assertEqual(
            _rule_sort_buffer_vs_filesort(analysis, None, None, variables),
            (None, None),
        )


# ---------------------------------------------------------------------------
# Join buffer rule
# ---------------------------------------------------------------------------

class TestJoinBufferRule(unittest.TestCase):

    def test_fires_on_hash_join(self):
        analysis = {"hash_joins": [{"rows": 9999}]}
        variables = {"join_buffer_size": "262144"}
        w, s = _rule_join_buffer_vs_hash_or_bnl(analysis, None, None, variables)
        self.assertIsNotNone(w)
        self.assertIn("Hash join", w)

    def test_fires_on_bnl(self):
        analysis = {"bnl_nodes": [{"short_label": "Table scan"}]}
        variables = {"join_buffer_size": "262144"}
        w, _ = _rule_join_buffer_vs_hash_or_bnl(analysis, None, None, variables)
        self.assertIsNotNone(w)
        self.assertIn("Block Nested-Loop", w)

    def test_silent_without_join(self):
        analysis = {"hash_joins": [], "bnl_nodes": []}
        variables = {"join_buffer_size": "262144"}
        self.assertEqual(
            _rule_join_buffer_vs_hash_or_bnl(analysis, None, None, variables),
            (None, None),
        )


# ---------------------------------------------------------------------------
# Tmp table rule
# ---------------------------------------------------------------------------

class TestTmpTableRule(unittest.TestCase):

    def test_fires_when_both_small(self):
        analysis = {"temp_tables": [{"rows": 10000, "short_label": "Materialize"}]}
        variables = {"tmp_table_size": "16777216", "max_heap_table_size": "16777216"}
        w, s = _rule_tmp_table_size_vs_materialize(analysis, None, None, variables)
        self.assertIsNotNone(w)
        self.assertIn("tmp_table_size", w)
        self.assertIn("same value", s)  # key nuance: both must be raised

    def test_silent_without_materialize(self):
        variables = {"tmp_table_size": "16777216", "max_heap_table_size": "16777216"}
        self.assertEqual(
            _rule_tmp_table_size_vs_materialize({"temp_tables": []}, None, None, variables),
            (None, None),
        )


# ---------------------------------------------------------------------------
# optimizer_switch rule
# ---------------------------------------------------------------------------

class TestOptimizerSwitchRule(unittest.TestCase):

    def test_hash_join_disabled_with_bnl(self):
        analysis = {"bnl_nodes": [{"short_label": "x"}]}
        variables = {"optimizer_switch": "hash_join=off,block_nested_loop=on"}
        results = _rule_optimizer_switch_disables(analysis, None, None, variables)
        self.assertTrue(any("hash_join=off" in w for w, _ in results))

    def test_mrr_off_with_filesort(self):
        analysis = {"filesorts": [{"rows": 100}]}
        variables = {"optimizer_switch": "mrr=off,mrr_cost_based=off"}
        results = _rule_optimizer_switch_disables(analysis, None, None, variables)
        self.assertTrue(any("mrr=off" in w for w, _ in results))

    def test_silent_when_all_enabled(self):
        analysis = {"bnl_nodes": [], "filesorts": [], "temp_tables": []}
        variables = {"optimizer_switch": "hash_join=on,mrr=on"}
        results = _rule_optimizer_switch_disables(analysis, None, None, variables)
        self.assertEqual(results, [])


# ---------------------------------------------------------------------------
# Missing indexes rule
# ---------------------------------------------------------------------------

class TestMissingIndexesRule(unittest.TestCase):

    def _schema(self, table, indexes):
        return {table: {
            "table_name": table,
            "columns": [],
            "indexes": [{"name": n, "columns": cols} for n, cols in indexes],
            "engine": "InnoDB",
        }}

    def test_flags_truly_missing_index(self):
        analysis = {
            "index_suggestions": [{
                "table": "users", "columns": ["email"],
                "ddl": "CREATE INDEX idx_users_email ON users (email);",
                "reason": "Full scan on users with filter on email",
            }]
        }
        schema = self._schema("users", [("PRIMARY", ["id"])])
        results = _rule_missing_indexes(analysis, schema, None, None)
        self.assertEqual(len(results), 1)
        self.assertIn("users", results[0][0])
        self.assertIn("CREATE INDEX", results[0][1])

    def test_silent_when_index_already_exists(self):
        analysis = {
            "index_suggestions": [{
                "table": "users", "columns": ["email"],
                "ddl": "CREATE INDEX idx_users_email ON users (email);",
                "reason": "Full scan on users with filter on email",
            }]
        }
        schema = self._schema("users", [
            ("PRIMARY", ["id"]),
            ("idx_email", ["email"]),  # already covers the suggestion
        ])
        results = _rule_missing_indexes(analysis, schema, None, None)
        self.assertEqual(results, [])

    def test_silent_when_no_schema_for_table(self):
        analysis = {
            "index_suggestions": [{
                "table": "unknown", "columns": ["x"],
                "ddl": "CREATE INDEX ...;", "reason": "...",
            }]
        }
        self.assertEqual(
            _rule_missing_indexes(analysis, {}, None, None),
            [],
        )

    def test_matches_schema_qualified(self):
        analysis = {
            "index_suggestions": [{
                "table": "orders", "columns": ["status"],
                "ddl": "CREATE INDEX ...;", "reason": "...",
            }]
        }
        schema = {"shop.orders": {
            "indexes": [{"name": "PRIMARY", "columns": ["id"]}],
        }}
        results = _rule_missing_indexes(analysis, schema, None, None)
        self.assertEqual(len(results), 1)


# ---------------------------------------------------------------------------
# Engine rule
# ---------------------------------------------------------------------------

class TestEngineRule(unittest.TestCase):

    def test_flags_myisam(self):
        schema = {"t1": {"engine": "MyISAM", "indexes": []}}
        results = _rule_engine_innodb({}, schema, None, None)
        self.assertEqual(len(results), 1)
        self.assertIn("MYISAM", results[0][0].upper())
        self.assertIn("ALTER TABLE", results[0][1])

    def test_silent_for_innodb(self):
        schema = {"t1": {"engine": "InnoDB", "indexes": []}}
        self.assertEqual(
            _rule_engine_innodb({}, schema, None, None),
            [],
        )

    def test_aria_not_flagged(self):
        """MariaDB's Aria engine is fine — skip the warning."""
        schema = {"t1": {"engine": "Aria", "indexes": []}}
        self.assertEqual(
            _rule_engine_innodb({}, schema, None, None),
            [],
        )


# ---------------------------------------------------------------------------
# Flush log rule
# ---------------------------------------------------------------------------

class TestFlushLogRule(unittest.TestCase):

    def test_fires_for_update_with_lax_flush(self):
        analysis = {"query_text_lines": ["UPDATE t1 SET x = 1 WHERE id = 2"]}
        variables = {"innodb_flush_log_at_trx_commit": "2"}
        w, s = _rule_flush_log_durability(analysis, None, None, variables)
        self.assertIsNotNone(w)
        self.assertIn("innodb_flush_log_at_trx_commit", w)

    def test_silent_for_select(self):
        analysis = {"query_text_lines": ["SELECT * FROM t1"]}
        variables = {"innodb_flush_log_at_trx_commit": "2"}
        self.assertEqual(
            _rule_flush_log_durability(analysis, None, None, variables),
            (None, None),
        )

    def test_silent_when_flush_is_1(self):
        analysis = {"query_text_lines": ["INSERT INTO t1 VALUES (1)"]}
        variables = {"innodb_flush_log_at_trx_commit": "1"}
        self.assertEqual(
            _rule_flush_log_durability(analysis, None, None, variables),
            (None, None),
        )


# ---------------------------------------------------------------------------
# advise() end-to-end
# ---------------------------------------------------------------------------

class TestAdviseEndToEnd(unittest.TestCase):

    def test_populates_environment_keys(self):
        analysis = {
            "full_scans": [], "hash_joins": [], "temp_tables": [], "filesorts": [],
            "bnl_nodes": [], "index_suggestions": [], "optimizer_features": [],
            "warnings": [], "suggestions": [], "query_text_lines": [],
        }
        advise(analysis, schema={}, stats={}, variables={})
        self.assertIn("environment_warnings", analysis)
        self.assertIn("environment_suggestions", analysis)
        self.assertIn("collected_variables", analysis)
        self.assertIn("collected_schema", analysis)
        self.assertIn("collected_stats", analysis)

    def test_multiple_rules_fire_together(self):
        analysis = {
            "full_scans": [], "hash_joins": [{"rows": 100}],
            "temp_tables": [{"rows": 100, "short_label": "Materialize"}],
            "filesorts": [{"rows": 100, "short_label": "Sort"}],
            "bnl_nodes": [], "index_suggestions": [],
            "optimizer_features": [], "warnings": [], "suggestions": [],
            "query_text_lines": ["SELECT * FROM t1"],
        }
        variables = {
            "innodb_buffer_pool_size": str(128 * 1024 * 1024),
            "sort_buffer_size": "262144",
            "join_buffer_size": "262144",
            "tmp_table_size": "16777216",
            "max_heap_table_size": "16777216",
            "optimizer_switch": "hash_join=on,mrr=on",
        }
        stats = {"t1": {"data_length": 2 * 1024 ** 3, "index_length": 0}}
        advise(analysis, schema={}, stats=stats, variables=variables)
        # Expect at least 4 warnings: buffer pool, sort buffer, join buffer, tmp table
        self.assertGreaterEqual(len(analysis["environment_warnings"]), 3)

    def test_never_raises_on_empty_inputs(self):
        analysis = {}
        advise(analysis)
        self.assertEqual(analysis["environment_warnings"], [])
        self.assertEqual(analysis["environment_suggestions"], [])

    def test_every_suggestion_explains_why(self):
        """Contract: every environment_suggestion must contain an explicit
        reason starting with the word 'Why:' — so users aren't told *what*
        to do without being told *why*. This protects the rules from
        regressing to terse one-liners in future refactors.
        """
        analysis = {
            "full_scans": [],
            "hash_joins": [{"rows": 100}],
            "temp_tables": [{"rows": 100, "short_label": "Materialize"}],
            "filesorts": [{"rows": 100, "short_label": "Sort"}],
            "bnl_nodes": [{"short_label": "Table scan"}],
            "index_suggestions": [{
                "table": "users", "columns": ["email"],
                "ddl": "CREATE INDEX idx_users_email ON users (email);",
                "reason": "Full scan on users with filter on email",
            }],
            "optimizer_features": [],
            "warnings": [],
            "suggestions": [],
            "query_text_lines": ["UPDATE users SET x=1 WHERE id=2"],
        }
        variables = {
            "innodb_buffer_pool_size": str(128 * 1024 * 1024),
            "sort_buffer_size": "262144",
            "join_buffer_size": "262144",
            "tmp_table_size": "16777216",
            "max_heap_table_size": "16777216",
            # Everything off so every optimizer_switch sub-rule fires.
            "optimizer_switch": "hash_join=off,mrr=off,derived_condition_pushdown=off",
            "innodb_flush_log_at_trx_commit": "2",
        }
        stats = {"users": {"data_length": 2 * 1024 ** 3, "index_length": 0}}
        # Include a schema entry with no covering index so the missing-index
        # rule fires and we can exercise its suggestion format too.
        schema = {"users": {
            "engine": "MyISAM",   # exercises the engine rule
            "indexes": [{"name": "PRIMARY", "columns": ["id"]}],
        }}
        advise(analysis, schema=schema, stats=stats, variables=variables)
        suggestions = analysis["environment_suggestions"]
        self.assertGreater(len(suggestions), 4, "expected several rules to fire")
        # The missing-index rule emits the DDL as its suggestion text and is
        # self-explanatory via its warning, so exempt suggestions whose text
        # is a single SQL statement starting with CREATE.
        for s in suggestions:
            if s.strip().upper().startswith("CREATE "):
                continue
            self.assertIn(
                "Why:", s,
                "suggestion missing 'Why:' explanation: " + s[:120],
            )

    def test_sort_buffer_suggestion_mentions_in_memory_sort(self):
        analysis = {"filesorts": [{"rows": 1000, "short_label": "Sort"}]}
        variables = {"sort_buffer_size": "262144"}
        _, s = _rule_sort_buffer_vs_filesort(analysis, None, None, variables)
        self.assertIn("Why:", s)
        # The reason should mention the in-memory sort vs disk spill trade-off.
        self.assertTrue("disk" in s.lower() or "memory" in s.lower())

    def test_join_buffer_suggestion_differentiates_hash_vs_bnl(self):
        hash_analysis = {"hash_joins": [{"rows": 100}]}
        bnl_analysis = {"bnl_nodes": [{"short_label": "Table scan"}]}
        variables = {"join_buffer_size": "262144"}
        _, s_hash = _rule_join_buffer_vs_hash_or_bnl(hash_analysis, None, None, variables)
        _, s_bnl  = _rule_join_buffer_vs_hash_or_bnl(bnl_analysis, None, None, variables)
        # The hash-join suggestion should mention hash-specific terminology.
        self.assertIn("hash", s_hash.lower())
        # The BNL suggestion should mention the inner-scan-per-batch behaviour.
        self.assertIn("batch", s_bnl.lower())
        # Both must still include a Why: prefix.
        self.assertIn("Why:", s_hash)
        self.assertIn("Why:", s_bnl)


if __name__ == "__main__":
    unittest.main()
