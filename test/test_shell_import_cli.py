"""CLI wiring for MySQL Shell diagnostics bundles: ``import-shell`` and zip
input accepted by every plan-reading command."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(TEST_DIR)
sys.path.insert(0, TEST_DIR)

import test_shell_import as bundles  # noqa: E402


class TestShellBundleCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.zip = os.path.join(cls.tmp, "bundle.zip")
        with open(cls.zip, "wb") as f:
            f.write(bundles.make_zip(bundles.bundle_members(
                analyze=bundles.read_fixture(), estimated=bundles.ESTIMATED_V1)))
        members = bundles.bundle_members(analyze=bundles.read_fixture())
        members.update(bundles.bundle_members(
            prefix="mysql-diagnostics-20240502-090000/", query="SELECT 2",
            estimated=bundles.ESTIMATED_V1))
        cls.multi = os.path.join(cls.tmp, "multi.zip")
        with open(cls.multi, "wb") as f:
            f.write(bundles.make_zip(members))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _run(self, *args):
        return subprocess.run([sys.executable, "-m", "myflames"] + list(args),
                              cwd=REPO_DIR, capture_output=True, text=True)

    def test_import_shell_writes_capture_json(self):
        r = self._run("import-shell", self.zip, "-q")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        data = json.loads(r.stdout)
        self.assertEqual(data["schema_version"], "capture-1.0")
        self.assertEqual(data["mode"], "analyze")
        self.assertEqual(data["source"], "mysqlsh-diagnostics")
        self.assertIn("testdb.reviews", data["schema"])
        self.assertEqual(data["query"], bundles.QUERY)

    def test_import_shell_list_and_select(self):
        r = self._run("import-shell", self.multi, "--list")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(r.stdout.strip().splitlines()), 2)
        r = self._run("import-shell", self.multi, "--query", "1", "-q")
        self.assertEqual(json.loads(r.stdout)["query"], "SELECT 2")
        r = self._run("import-shell", self.multi, "--query", "5")
        self.assertEqual(r.returncode, 2)
        self.assertIn("out of range", r.stderr)

    def test_import_shell_rejects_other_files(self):
        r = self._run("import-shell", os.path.join(TEST_DIR, "mysql-explain-hash-join.json"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("not a MySQL Shell diagnostics zip", r.stderr)

    def test_plan_commands_accept_the_zip(self):
        for args in (("advise", self.zip), ("check", self.zip, "--fail-on", "error"),
                     ("digest", self.zip)):
            r = self._run(*args)
            self.assertEqual(r.returncode, 0, (args, r.stderr))
            self.assertTrue(r.stdout or "OK" in r.stderr)
        r = self._run("--type", "tree", self.zip)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("<svg", r.stdout)

    def test_html_report_uses_bundle_query_and_schema(self):
        out = os.path.join(self.tmp, "report.html")
        r = self._run(self.zip, "-o", out, "--no-teach-bundle")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.tmp, "report.json"), encoding="utf-8") as f:
            sidecar = json.load(f)
        self.assertIn("reviews", json.dumps(sidecar.get("query") or {}))

    def test_broken_bundle_exits_2(self):
        broken = os.path.join(self.tmp, "broken.zip")
        members = bundles.bundle_members()
        with open(broken, "wb") as f:
            f.write(bundles.make_zip(members))
        r = self._run("advise", broken)
        self.assertEqual(r.returncode, 2)
        self.assertIn("Cannot import", r.stderr)

    def test_quiet_silences_import_notes(self):
        for args in (("check", self.zip, "--fail-on", "error", "-q"), ("digest", self.zip, "-q"),
                     ("advise", self.multi, "-q")):
            r = self._run(*args)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("Note:", r.stderr)

    def test_advise_and_check_use_bundle_ddl(self):
        members = bundles.bundle_members(analyze=bundles.read_fixture())
        key = [k for k in members if "referenced_table" in k][0]
        self.assertIn("KEY `idx_rating` (`rating`)", members[key])
        members[key] = members[key].replace(
            "KEY `idx_rating` (`rating`)",
            "KEY `idx_rating` (`rating`),\n    KEY `idx_rating_product` (`rating`,`product_id`)")
        path = os.path.join(self.tmp, "redundant.zip")
        with open(path, "wb") as f:
            f.write(bundles.make_zip(members))
        r = self._run("check", path, "--fail-on", "redundant_index", "-q")
        self.assertEqual(r.returncode, 1, r.stderr)
        data = json.loads(self._run("advise", path, "--json", "-q").stdout)
        self.assertIn("redundant_index", {f["category"] for f in data})


if __name__ == "__main__":
    unittest.main()
