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
# A clean plan (primary-key lookup) — emits no full_scan warning, so a
# `--fail-on full_scan` gate passes (exit 0) on it.
CLEAN_PLAN = os.path.join(FIXTURE_DIR, "explain-004-pk-lookup-user.json")


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

    def test_clean_plan_fail_on_full_scan_is_zero(self):
        # A PK-lookup plan has no full_scan warning, so the gate passes.
        self.assertEqual(
            run_cli("check", CLEAN_PLAN, "--fail-on", "full_scan").returncode, 0)

    def test_full_scan_plan_fail_on_full_scan_is_one(self):
        # The full-scan plan trips the full_scan gate.
        self.assertEqual(
            run_cli("check", FULL_SCAN, "--fail-on", "full_scan").returncode, 1)

    def test_unknown_fail_on_trigger_is_two(self):
        # A misspelled / unknown trigger is bad input (exit 2), distinct from a
        # valid-but-unmatched trigger (exit 0) and a tripped gate (exit 1).
        proc = run_cli("check", FULL_SCAN, "--fail-on", "bogustrigger")
        self.assertEqual(proc.returncode, 2)
        # Diagnostic names the offending trigger on stderr, stdout stays clean.
        self.assertIn("bogustrigger", proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_missing_file_is_two(self):
        for cmd in (["digest"], ["advise"], ["check"]):
            proc = run_cli(*(cmd + [os.path.join(FIXTURE_DIR, "does-not-exist.json")]))
            self.assertEqual(proc.returncode, 2, "%s on missing file should exit 2" % cmd[0])

    def test_unparseable_render_is_two(self):
        # Default render path on bad JSON: exit 2 (bad input), not 1.
        proc = run_cli("-", stdin="{ not valid json")
        self.assertEqual(proc.returncode, 2)


class TestOutputFlag(unittest.TestCase):

    def test_workbench_svg_and_html_exports(self):
        import xml.etree.ElementTree as ET
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            for extension in ('svg', 'html'):
                output = Path(directory) / ('plan.' + extension)
                result = run_cli('--type', 'workbench', FULL_SCAN, '-o', str(output))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, '')
                content = output.read_text()
                self.assertIn('class="wb-plan"', content)
                self.assertIn('data-node-id=', content)
                if extension == 'svg':
                    ET.fromstring(content)
                else:
                    self.assertIn('application/ld+json', content)
                    sidecar = json.loads(output.with_suffix('.json').read_text())
                    self.assertIn('plan_tree', sidecar)

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


class TestQuietFlagUniform(unittest.TestCase):
    """--quiet/-q is the same contract on every subcommand that emits stderr
    diagnostics: it silences the chatter (the "Written to <path>" line, digest's
    tokenizer "Note:") and never touches the stdout data. It used to exist only
    on `check`."""

    def _write_and_assert_silent(self, *cli_args):
        with tempfile.NamedTemporaryFile(suffix=".out", delete=False) as tf:
            path = tf.name
        try:
            proc = run_cli(*(cli_args + ("-o", path, "-q")))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            # The file was still written...
            self.assertTrue(os.path.getsize(path) > 0)
            # ...but -q silenced the "Written to <path>" diagnostic on stderr.
            self.assertEqual(proc.stderr.strip(), "")
            self.assertEqual(proc.stdout.strip(), "")
        finally:
            os.remove(path)

    def test_digest_quiet_suppresses_written_to(self):
        self._write_and_assert_silent("digest", FULL_SCAN)

    def test_advise_quiet_suppresses_written_to(self):
        self._write_and_assert_silent("advise", FULL_SCAN)

    def test_compare_quiet_suppresses_written_to(self):
        self._write_and_assert_silent("compare", FULL_SCAN, CLEAN_PLAN, "--digest")

    def test_quiet_does_not_swallow_stdout_data(self):
        # -q only silences stderr; the digest text still flows to stdout.
        proc = run_cli("digest", FULL_SCAN, "-q")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("myflames digest", proc.stdout)
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


class TestWorkspaceCommand(unittest.TestCase):
    def test_ui_is_discoverable_and_has_own_help(self):
        self.assertIn('myflames ui', run_cli('--help').stdout)
        result = run_cli('ui', '--help')
        self.assertEqual(result.returncode, 0)
        self.assertIn('--no-browser', result.stdout)
        self.assertIn('--port', result.stdout)
        self.assertEqual(result.stderr, '')

    def test_invalid_ui_port_is_usage_error(self):
        for port in ('-1', '65536', 'not-a-number'):
            result = run_cli('ui', '--port', port, '--no-browser')
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, '')
            self.assertNotIn('Traceback', result.stderr)

    def test_occupied_port_is_actionable_error(self):
        import socket
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            result = run_cli('ui', '--port', str(listener.getsockname()[1]), '--no-browser')
            self.assertEqual(result.returncode, 2)
            self.assertIn('Try --port 0', result.stderr)
            self.assertEqual(result.stdout, '')


if __name__ == "__main__":
    unittest.main()
