"""Contract tests for scripts/check_fast.sh, the fast pre-handback check.

Each violation case names the patch defect it catches: code that breaks the
project's Python 3.7 promise (walrus, positional-only parameters,
str.removeprefix, self-documenting f-strings) or a broken shell file. The
offending snippets are built by string concatenation so this file itself
stays clean for the script's line-based heuristics when
``scripts/check_fast.sh scripts/check_fast.sh test/test_check_fast.py``
runs.
"""

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_fast.sh"


class CheckFastTests(unittest.TestCase):
    def run_check(self, *args, **env):
        merged = dict(os.environ)
        merged.update(env)
        return subprocess.run(
            ["/bin/sh", str(SCRIPT)] + list(args),
            cwd=str(ROOT), env=merged,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, timeout=180)

    def temp_script_file(self, name, content):
        # Under scripts/ so step (a) checks it and step (b) does not run it.
        path = ROOT / "scripts" / name
        path.write_text(content, encoding="utf-8")

        def cleanup():
            try:
                path.unlink()
            except OSError:
                pass
        self.addCleanup(cleanup)
        return "scripts/" + name

    def assert_violation(self, name, content, line, needle):
        rel = self.temp_script_file(name, content)
        result = self.run_check(rel)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("FAIL", result.stdout)
        lineno = r"\d+" if line is None else str(line)
        self.assertRegex(
            result.stdout, r"FAIL python-3\.7-syntax %s:%s: [^\n]*%s" %
            (re.escape(rel), lineno, re.escape(needle)))

    def test_clean_python_file_passes(self):
        rel = self.temp_script_file("checkfast_case_clean.py", "value = 1\n")
        result = self.run_check(rel)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("PASS python-3.7-syntax", result.stdout)
        self.assertIn("check_fast: PASS", result.stdout)

    def test_walrus_fails_with_file_and_line(self):
        # SyntaxError on 3.7; the AST guard catches permissive newer parsers.
        self.assert_violation(
            "checkfast_case_walrus.py", "if (n := 1):\n    pass\n", 1, "syntax")

    def test_positional_only_parameter_fails_with_file_and_line(self):
        # SyntaxError on 3.7; the AST guard catches permissive newer parsers.
        self.assert_violation(
            "checkfast_case_posonly.py", "def f(a, /):\n    pass\n", 1, "syntax")

    def test_match_fails_with_file_and_line(self):
        # The host grammar can point at the start or end of a match statement.
        self.assert_violation(
            "checkfast_case_match.py", "match value:\n    case 1:\n        pass\n",
            None, "syntax")

    def test_removeprefix_fails_with_file_and_line(self):
        # Catches a patch calling a str method added in Python 3.9.
        call = "text.remove" + "prefix(\"a\")"
        self.assert_violation(
            "checkfast_case_removeprefix.py",
            "text = \"abc\"\n" + call + "\n", 2, "remove" + "prefix")

    def test_fstring_equals_fails_with_file_and_line(self):
        # Catches a patch using a self-documenting f-string, added in 3.8.
        snippet = "print(" + "f" + "\"{value" + "=" + "}\")"
        # Python 3.7 rejects it before the regex, sometimes at line 1.
        line, needle = (2, "f-string") if sys.version_info >= (3, 8) else (None, "syntax")
        self.assert_violation(
            "checkfast_case_fstring.py",
            "value = 1\n" + snippet + "\n", line, needle)

    def test_parenthesized_with_fails_with_file_and_line(self):
        # Catches a patch using with (a, b), added in Python 3.10.
        snippet = "with " + "(open(\"a\") as a, open(\"b\") as b):"
        # The PEG parser accepts this from 3.9; 3.7/3.8 raise SyntaxError first.
        line, needle = (1, "with (") if sys.version_info >= (3, 9) else (None, "syntax")
        self.assert_violation(
            "checkfast_case_with.py", snippet + "\n    pass\n", line, needle)

    def test_parenthesized_expression_in_with_passes(self):
        # A parenthesized expression before .open() is valid on Python 3.7.
        content = "from pathlib import Path\nwith (Path(\".\") / \"a\").open(\"a\") as log:\n    pass\n"
        rel = self.temp_script_file("checkfast_case_with_expression.py", content)
        result = self.run_check(rel)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("PASS python-3.7-syntax", result.stdout)

    def test_broken_shell_file_fails(self):
        # Catches a patch shipping a shell file sh -n rejects.
        rel = self.temp_script_file("checkfast_case_broken.sh", "if true; then\n")
        result = self.run_check(rel)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("FAIL shell " + rel, result.stdout)

    def test_no_args_no_git_exits_2(self):
        # With no arguments and no git on PATH the script prints usage.
        with tempfile.TemporaryDirectory() as empty:
            result = subprocess.run(
                ["/bin/sh", str(SCRIPT)],
                cwd=str(ROOT), env={"PATH": empty},
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                universal_newlines=True, timeout=60)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("usage", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()
