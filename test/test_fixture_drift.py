"""Tests for scripts/check_fixture_drift.py.

The script is loaded from disk and its git access (head_content,
head_fixture_files) is replaced with an in-memory "committed" tree, so no
repository and no Docker is needed.
"""
import contextlib
import fnmatch
import importlib.util
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path

SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "check_fixture_drift.py",
)


def _load_script():
    spec = importlib.util.spec_from_file_location("check_fixture_drift", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DriftCase(unittest.TestCase):
    def setUp(self):
        self.mod = _load_script()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.fixtures = root / "test" / "fixtures"
        self.fixtures.mkdir(parents=True)
        self.mod.REPO_ROOT = root
        self.mod.FIXTURES_DIR = self.fixtures
        self.committed = {}
        self.mod.head_content = lambda rel: self.committed.get(rel)
        self.mod.head_fixture_files = lambda: sorted(self.committed)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, obj):
        path = self.fixtures / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj))

    def commit(self, name, obj):
        self.committed["test/fixtures/" + name] = json.dumps(obj)

    def run_main(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.mod.main()
        return code, out.getvalue()


class TestContentComparison(DriftCase):
    def test_noise_only_difference_passes(self):
        name = "explain-001-table-scan.json"
        self.commit(name, {"operation": "Table scan", "actual_last_row_ms": 1.0,
                           "estimated_rows": 100})
        self.write(name, {"operation": "Table scan", "actual_last_row_ms": 9.9,
                          "estimated_rows": 250})
        code, out = self.run_main()
        self.assertEqual(0, code, out)
        self.assertIn("No structural drift", out)

    def test_structural_change_fails(self):
        name = "explain-001-table-scan.json"
        self.commit(name, {"operation": "Table scan"})
        self.write(name, {"operation": "Index lookup"})
        code, out = self.run_main()
        self.assertEqual(1, code)
        self.assertIn("structural drift", out)
        self.assertIn("test/fixtures/" + name, out)

    def test_invalid_json_fails(self):
        name = "explain-001-broken.json"
        self.committed["test/fixtures/" + name] = "{not json"
        (self.fixtures / name).write_text("{also not json")
        code, out = self.run_main()
        self.assertEqual(1, code)
        self.assertIn("invalid JSON", out)


class TestSetComparison(DriftCase):
    def test_managed_fixture_no_longer_produced_fails(self):
        self.commit("mariadb-11.4-001-join.json", {"operation": "Join"})
        code, out = self.run_main()
        self.assertEqual(1, code)
        self.assertIn("no longer produced", out)
        self.assertIn("mariadb-11.4-001-join.json", out)

    def test_managed_mysql_fixture_no_longer_produced_fails(self):
        self.commit("explain-007-gone.json", {"operation": "Join"})
        code, out = self.run_main()
        self.assertEqual(1, code)
        self.assertIn("no longer produced", out)
        self.assertIn("explain-007-gone.json", out)

    def test_produced_fixture_not_committed_fails(self):
        self.write("explain-042-new-plan.json", {"operation": "Table scan"})
        code, out = self.run_main()
        self.assertEqual(1, code)
        self.assertIn("not committed", out)
        self.assertIn("explain-042-new-plan.json", out)

    def test_unmanaged_committed_fixture_missing_is_not_drift(self):
        # Hand-captured fixtures are not regenerated, so the generators not
        # producing them says nothing.
        self.commit("mysql-5.7-union.json", {"operation": "Union"})
        code, out = self.run_main()
        self.assertEqual(0, code, out)

    def test_new_unmanaged_file_is_flagged(self):
        self.write("captures/hand-made.json", {"operation": "Anything"})
        code, out = self.run_main()
        self.assertEqual(1, code)
        self.assertIn("new fixture not present in HEAD", out)

    def test_matching_sets_pass(self):
        for name in ("explain-001-a.json", "mariadb-10.11-002-b.json",
                     "mariadb-11.4-003-c.json", "mysql-5.7-union.json"):
            self.commit(name, {"operation": "X"})
            self.write(name, {"operation": "X"})
        code, out = self.run_main()
        self.assertEqual(0, code, out)


class TestHelpers(unittest.TestCase):
    def test_strip_noise_removes_known_keys_recursively(self):
        mod = _load_script()
        node = {"a": 1, "actual_first_row_ms": 2.0,
                "inputs": [{"r_total_time_ms": 3, "rows": 5, "keep": True}]}
        self.assertEqual(
            {"a": 1, "inputs": [{"keep": True}]},
            mod.strip_noise(node),
        )

    def test_is_managed(self):
        mod = _load_script()
        for rel in ("test/fixtures/explain-001-x.json",
                    "test/fixtures/mariadb-11.4-012-join.json",
                    "test/fixtures/mariadb-10.11-030-y.json"):
            self.assertTrue(mod.is_managed(rel), rel)
        for rel in ("test/fixtures/mysql-5.7-union.json",
                    "test/fixtures/mysql-9.7-hypergraph-on-cte-window-rank.json",
                    "test/fixtures/mariadb-11.8-intersect.json",
                    "test/fixtures/mariadb-11.4-explain-cte.json",
                    "test/fixtures/captures/explain-001-x.json",
                    "test/fixtures/explain-001-x.txt"):
            self.assertFalse(mod.is_managed(rel), rel)

    def test_hypergraph_fixtures_are_unmanaged(self):
        """Hand-recorded hypergraph fixtures must never match MANAGED_PATTERNS.

        The generator scripts delete and recreate numbered fixtures, so
        hand-recorded fixtures must carry unnumbered names to avoid being
        wiped during regeneration or flagged as drift.
        """
        mod = _load_script()
        fixtures_dir = Path(__file__).resolve().parent / "fixtures"
        files = {
            f"test/fixtures/{p.name}"
            for p in fixtures_dir.glob("*.json")
        }
        files.update(mod.head_fixture_files())
        hg_files = [rel for rel in sorted(files) if "hypergraph" in rel]
        self.assertTrue(hg_files, "Expected to find hypergraph fixtures")
        for rel in hg_files:
            self.assertFalse(
                mod.is_managed(rel),
                f"{rel} matches MANAGED_PATTERNS: hand-recorded fixtures must not use numbered generator names",
            )


class TestGeneratorRemovesStaleFixtures(unittest.TestCase):
    """scripts/generate-fixtures.sh must delete its owned numbered fixtures
    before regenerating, or a removed run_explain call leaves a stale file the
    drift set comparison cannot see. Static: the generator needs Docker."""

    GENERATOR = Path(SCRIPT).with_name("generate-fixtures.sh")

    @classmethod
    def setUpClass(cls):
        cls.script = cls.GENERATOR.read_text(encoding="utf-8")

    def removal_globs(self):
        match = re.search(r"^rm -f (.*)$", self.script, re.MULTILINE)
        self.assertIsNotNone(
            match, "generate-fixtures.sh never removes its numbered fixtures")
        globs = re.findall(r'"\$OUTPUT_DIR"/(\S+)', match.group(1))
        self.assertTrue(globs, "the removal must be scoped to $OUTPUT_DIR globs")
        return globs

    def test_removal_runs_before_the_first_fixture_is_written(self):
        removal = self.script.index("rm -f ")
        first_write = self.script.index("run_explain \"")
        self.assertLess(removal, first_write)

    def test_removal_waits_for_preflight_and_a_ready_server(self):
        # A refused run (container already there, server never ready) must
        # leave the existing corpus alone.
        removal = self.script.index("rm -f ")
        self.assertLess(self.script.index("docker ps -a"), removal)
        self.assertLess(self.script.index('log "Schema and data ready."'), removal)

    def test_removal_matches_exactly_the_owned_numbered_fixtures(self):
        globs = self.removal_globs()

        def removed(name):
            return any(fnmatch.fnmatchcase(name, glob) for glob in globs)

        for name in ("explain-001-table-scan-users-no-filter.json",
                     "explain-042-new-plan.json", "explain-999-x.json"):
            self.assertTrue(removed(name), name)
        for name in ("mariadb-11.4-001-join.json", "mariadb-10.11-030-y.json",
                     "mysql-5.7-union.json", "explain-001-x.txt",
                     "explain-01-two-digits.json", "explain-1000-four-digits.json",
                     "server-version.txt", "unrelated.json"):
            self.assertFalse(removed(name), name)


if __name__ == "__main__":
    unittest.main()
