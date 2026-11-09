"""Run the actual frontend statement-thread contracts from the stdlib suite.

The helper owns only frontend projections, so testing a Python reimplementation
would miss regressions. Node bundles and executes the production TypeScript.
Python-only checkouts without workspace development dependencies skip explicitly;
the workspace's Node test remains the required frontend gate.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "apps" / "workspace"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node.js is needed to execute frontend statement-thread contracts")
@unittest.skipUnless((WORKSPACE / "node_modules" / "esbuild").is_dir(),
                     "Workspace npm dependencies are needed to bundle the actual TypeScript helper")
class TestStatementThread(unittest.TestCase):
    def check_contract(self, group):
        with tempfile.TemporaryDirectory(prefix="myflames-thread-test-") as home:
            env = os.environ.copy()
            env.update(HOME=home, USERPROFILE=home, MYFLAMES_SECRET_BACKEND="vault")
            env.pop("MYFLAMES_VAULT_PASSPHRASE", None)
            result = subprocess.run(
                [NODE, "test/statement-thread-unit.mjs", group], cwd=str(WORKSPACE), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                encoding="utf-8", timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("statement thread {}: ok".format(group), result.stdout)

    def test_mysql_mariadb_and_slow_log_inspector_values(self):
        self.check_contract("details")

    def test_suggestion_keeps_statement_schema_before_values_and_target(self):
        self.check_contract("origin")

    def test_only_the_completed_exact_single_ddl_marks_index_applied(self):
        self.check_contract("applied")

    def test_measurement_uses_last_event_and_preserves_unknown_and_zero(self):
        self.check_contract("measurement")

    def test_compare_identity_requires_same_statement_connection_and_database(self):
        self.check_contract("identity")

    def test_real_shared_inspector_props_and_measurement_text(self):
        self.check_contract("render")
