"""
CLI contract tests — the command-line surface invariants owned by the cli-ux
skill. These run ``myflames`` as a subprocess and assert the *contract*, not the
content: exit codes (0/1/2), --output writes a file, --json is valid JSON, and
machine output lands on stdout while diagnostics land on stderr.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
FIXTURE_DIR = os.path.join(TEST_DIR, "fixtures")
FULL_SCAN = os.path.join(FIXTURE_DIR, "explain-001-table-scan-users-no-filter.json")


def run_cli(*args, **kwargs):
    """Invoke `python -m myflames <args>` against the local package."""
    stdin = kwargs.get("stdin")
    proc = subprocess.run(
        [sys.executable, "-m", "myflames"] + list(args),
        cwd=REPO_ROOT,
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    return proc


class TestExitCodes(unittest.TestCase):

    def test_success_is_zero(self):
        # check with a non-matching trigger = clean = 0
        self.assertEqual(run_cli("check", FULL_SCAN, "--fail-on", "filesort").returncode, 0)

    def test_gate_tripped_is_one(self):
        # check matches full_scan = a finding tripped = 1
        self.assertEqual(run_cli("check", FULL_SCAN, "--fail-on", "full_scan").returncode, 1)

    def test_missing_file_is_two(self):
        for cmd in (["digest"], ["advise"], ["check"]):
            proc = run_cli(*(cmd + [os.path.join(FIXTURE_DIR, "does-not-exist.json")]))
            self.assertEqual(proc.returncode, 2, "%s on missing file should exit 2" % cmd[0])

    def test_unparseable_render_is_two(self):
        # Default render path on bad JSON: exit 2 (bad input), not 1.
        proc = run_cli("-", stdin="{ not valid json")
        self.assertEqual(proc.returncode, 2)


class TestOutputFlag(unittest.TestCase):

    def test_digest_output_writes_file(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tf:
            path = tf.name
        try:
            proc = run_cli("digest", FULL_SCAN, "-o", path)
            self.assertEqual(proc.returncode, 0)
            self.assertTrue(os.path.getsize(path) > 0)
            # Payload went to the file, not stdout.
            self.assertEqual(proc.stdout.strip(), "")
            # The "Written to" announcement is a diagnostic → stderr.
            self.assertIn(path, proc.stderr)
        finally:
            os.remove(path)

    def test_advise_json_output_writes_valid_json_file(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
            path = tf.name
        try:
            proc = run_cli("advise", FULL_SCAN, "--json", "-o", path)
            self.assertEqual(proc.returncode, 0)
            with open(path) as f:
                data = json.load(f)  # raises if invalid
            self.assertIsInstance(data, list)
        finally:
            os.remove(path)


class TestStreamDiscipline(unittest.TestCase):

    def test_advise_json_is_valid_on_stdout(self):
        proc = run_cli("advise", FULL_SCAN, "--json")
        self.assertEqual(proc.returncode, 0)
        json.loads(proc.stdout)  # stdout is pure JSON, no diagnostics mixed in

    def test_digest_default_emits_text_on_stdout(self):
        # Bare `digest` now emits the digest text itself (not the savings report).
        proc = run_cli("digest", FULL_SCAN)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("myflames digest", proc.stdout)

    def test_digest_cost_json_is_valid_on_stdout(self):
        proc = run_cli("digest", FULL_SCAN, "--json")
        self.assertEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertIn("tokens_saved", payload)

    def test_check_clean_keeps_stdout_empty(self):
        # 'check' has no data payload — its status is a diagnostic on stderr.
        proc = run_cli("check", FULL_SCAN, "--fail-on", "filesort")
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIn("OK", proc.stderr)

    def test_check_quiet_is_silent(self):
        proc = run_cli("check", FULL_SCAN, "--fail-on", "filesort", "-q")
        self.assertEqual(proc.stdout.strip(), "")
        self.assertEqual(proc.stderr.strip(), "")


class TestDeprecatedAliases(unittest.TestCase):
    """`tokens` and `findings` are kept as deprecated aliases: they must still
    work, warn on stderr (never stdout), and keep stdout clean."""

    def test_tokens_alias_warns_and_keeps_old_default(self):
        # Old bare `tokens` defaulted to the savings report — the alias preserves
        # that by injecting --cost, and warns on stderr only.
        proc = run_cli("tokens", FULL_SCAN)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("deprecated", proc.stderr)
        self.assertIn("digest", proc.stderr)
        self.assertIn("Token cost", proc.stdout)
        self.assertNotIn("deprecated", proc.stdout)

    def test_tokens_alias_json_stdout_stays_pure(self):
        proc = run_cli("tokens", FULL_SCAN, "--json")
        self.assertEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)  # warning must not leak into stdout
        self.assertIn("tokens_saved", payload)

    def test_findings_alias_warns_and_works(self):
        proc = run_cli("findings", FULL_SCAN, "--json")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("deprecated", proc.stderr)
        self.assertIn("advise", proc.stderr)
        json.loads(proc.stdout)  # stdout stays pure JSON


if __name__ == "__main__":
    unittest.main()
