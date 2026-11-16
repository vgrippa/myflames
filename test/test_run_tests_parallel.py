"""Exercise the real subprocess runner against disposable unittest modules."""

import ast
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_tests_parallel.py"
spec = importlib.util.spec_from_file_location("parallel_test_runner", str(SCRIPT))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def string_literal(node):
    # ast.Str was removed in Python 3.14; Python 3.7's parser still emits it.
    value = getattr(node, "value", getattr(node, "s", None))
    return value if isinstance(value, str) else None


def ps_scan_available():
    """The detached-descendant sweep needs a working `ps -A -o pid=,ppid=`."""
    if os.name != "posix" or not shutil.which("ps"):
        return False
    try:
        subprocess.check_output(["ps", "-A", "-o", "pid=,ppid="],
                                stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


PS_SCAN_AVAILABLE = ps_scan_available()


class ParallelRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="runner-contract-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.tests = self.root / "cases"
        self.tests.mkdir()

    def module(self, name, source):
        path = self.tests / (name + ".py")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")
        return path

    def run_runner(self, *arguments, **environment):
        env = os.environ.copy()
        env.update(JOBS="2", MYFLAMES_SKIP_BUDGET="0", RUNNER_SENTINEL="passed through")
        cwd = environment.pop("cwd", str(ROOT))
        env.update(environment)
        # Isolate even the runner parent, including tests of the JOBS=1 path.
        with tempfile.TemporaryDirectory(dir=str(self.root), prefix="parent-home-") as home:
            env.update(HOME=home, USERPROFILE=home, MYFLAMES_SECRET_BACKEND="vault")
            process = subprocess.Popen(
                [sys.executable, str(SCRIPT), "--start-dir", str(self.tests)] + list(arguments),
                cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                output, _ = process.communicate(timeout=20)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        return process.returncode, output.decode("utf-8", errors="replace")

    def passing_module(self, name="test_pass"):
        return self.module(name, '''
            import unittest
            class Cases(unittest.TestCase):
                def test_pass(self):
                    print('PASS OUTPUT MUST STAY CAPTURED')
                    self.assertEqual(2 + 2, 4)
        ''')

    def test_passes_capture_output_and_write_fresh_durations(self):
        self.passing_module()
        durations = self.root / "durations.json"
        code, output = self.run_runner("--durations-out", str(durations))
        self.assertEqual(code, 0, output)
        self.assertIn("Inventory: discovered 1 tests in 1 modules; ran 1 tests", output)
        self.assertRegex(output, r"Ran 1 tests in [0-9.]+s\n\nOK\n$")
        self.assertNotIn("PASS OUTPUT MUST STAY CAPTURED", output)
        measured = json.loads(durations.read_text(encoding="utf-8"))
        self.assertEqual(set(measured), {"test_pass"})
        self.assertGreater(measured["test_pass"], 0)

    def test_failures_and_import_errors_show_output_and_keep_inventory(self):
        self.passing_module()
        self.module("test_fail", '''
            import unittest
            class Cases(unittest.TestCase):
                def test_fail(self):
                    self.fail('intentional failure evidence')
        ''')
        self.module("test_import", "raise RuntimeError('intentional import error evidence')\n")
        code, output = self.run_runner()
        self.assertEqual(code, 1, output)
        self.assertIn("intentional failure evidence", output)
        self.assertIn("intentional import error evidence", output)
        self.assertIn("Inventory: discovered 3 tests in 3 modules; ran 3 tests", output)
        self.assertIn("FAILED (failures=1, errors=1)", output)
        self.assertNotIn("PASS OUTPUT MUST STAY CAPTURED", output)

    def test_crash_does_not_silently_lose_a_test(self):
        self.module("test_crash", '''
            import os
            import unittest
            class Cases(unittest.TestCase):
                def test_crash(self):
                    os._exit(17)
        ''')
        code, output = self.run_runner()
        self.assertEqual(code, 1, output)
        self.assertIn("test_crash", output)
        self.assertIn("exit 17", output)
        self.assertIn("crashes=1", output)
        self.assertIn("INVENTORY MISMATCH", output)

    def test_inventory_mismatch_is_a_failure_even_when_workers_pass(self):
        # The inventory discovers with the suite pattern and the worker with
        # the module's file name; load_tests answers them differently.
        self.module("test_inventory", '''
            import unittest
            class Cases(unittest.TestCase):
                def test_one(self): pass
                def test_two(self): pass
            def load_tests(loader, tests, pattern):
                if pattern == 'test_*.py':
                    return tests
                return unittest.TestSuite([Cases('test_one')])
        ''')
        code, output = self.run_runner()
        self.assertEqual(code, 1, output)
        self.assertIn("test_inventory: inventory discovered 2, worker discovered 1", output)
        self.assertIn("INVENTORY MISMATCH", output)
        self.assertIn("FAILED (inventory mismatches=1)", output)

    def test_class_fixture_skip_explains_tests_that_never_ran(self):
        self.module("test_fixture_skip", '''
            import unittest
            class Unavailable(unittest.TestCase):
                @classmethod
                def setUpClass(cls):
                    raise unittest.SkipTest('class fixture unavailable')
                def test_one(self): pass
                def test_two(self): pass
            class Runs(unittest.TestCase):
                def test_three(self): pass
        ''')
        code, output = self.run_runner()
        self.assertEqual(code, 0, output)
        self.assertIn("test_fixture_skip: discovered 3; ran 1; "
                      "2 not run because a class or module fixture skipped", output)
        self.assertIn("Inventory: discovered 3 tests in 1 modules; ran 1 tests; "
                      "2 not run because a class or module fixture skipped", output)
        self.assertNotIn("INVENTORY MISMATCH", output)
        self.assertIn("OK (skipped=1)", output)

    def test_inventory_imports_like_python_dash_m_unittest(self):
        # rootonlypkg exists only at the temporary repository root. Run as a
        # script, sys.path[0] is scripts/ instead of the working directory, so
        # the inventory used to fail this import and undercount the module.
        package = self.root / "rootonlypkg"
        package.mkdir()
        (package / "__init__.py").write_text("VALUE = 42\n", encoding="utf-8")
        self.module("test_root_import", '''
            import unittest
            import rootonlypkg
            class Cases(unittest.TestCase):
                def test_one(self):
                    self.assertEqual(rootonlypkg.VALUE, 42)
                def test_two(self): pass
        ''')
        code, output = self.run_runner(cwd=str(self.root))
        self.assertEqual(code, 0, output)
        self.assertIn("Inventory: discovered 2 tests in 1 modules; ran 2 tests", output)
        self.assertNotIn("INVENTORY MISMATCH", output)

    def test_skip_budget_off_reports_all_reasons_and_unexpected_identity(self):
        self.module("test_skips", '''
            import unittest
            class Cases(unittest.TestCase):
                @unittest.skip('allowed reason')
                def test_allowed(self): pass
                @unittest.skip('unexpected reason')
                def test_unexpected(self): pass
        ''')
        allowlist = self.root / "allowlist.txt"
        allowlist.write_text("# one full-match expression\n\nallowed reason\n", encoding="utf-8")
        code, output = self.run_runner("--skip-allowlist", str(allowlist))
        self.assertEqual(code, 0, output)
        self.assertIn('"allowed reason" -> 1', output)
        self.assertIn('"unexpected reason" -> 1', output)
        self.assertIn('test_skips: test_skips.Cases.test_unexpected: "unexpected reason"', output)
        self.assertNotIn('test_skips: test_skips.Cases.test_allowed:', output)
        self.assertIn("OK (skipped=2)", output)
        code, output = self.run_runner("--skip-allowlist", str(allowlist), MYFLAMES_SKIP_BUDGET="1")
        self.assertEqual(code, 1, output)
        self.assertIn("FAILED (skipped=2, unexpected skips=1)", output)
        allowlist.write_text("allowed reason\nunexpected reason\n", encoding="utf-8")
        code, output = self.run_runner("--skip-allowlist", str(allowlist), MYFLAMES_SKIP_BUDGET="1")
        self.assertEqual(code, 0, output)
        self.assertIn("OK (skipped=2)", output)

    def test_multiline_descriptions_subtests_and_escaped_reasons_are_counted(self):
        self.module("test_details", '''
            import unittest
            class Cases(unittest.TestCase):
                def test_doc(self):
                    """A description on its own output line."""
                    self.skipTest("quotes ' and a newline\\nnext")
                def test_subtests(self):
                    for value in (1, 2):
                        with self.subTest(value=value):
                            self.skipTest('subtest reason')
        ''')
        code, output = self.run_runner()
        self.assertEqual(code, 0, output)
        self.assertIn('"quotes \' and a newline\\nnext" -> 1', output)
        self.assertIn('"subtest reason" -> 2', output)
        self.assertIn("test_details.Cases.test_doc", output)
        self.assertIn("test_details.Cases.test_subtests", output)
        self.assertIn("OK (skipped=3)", output)

    def test_child_reports_structured_results_and_no_marker_lines(self):
        self.module("test_mixed", '''
            import unittest
            class Cases(unittest.TestCase):
                @unittest.skip('structured reason')
                def test_skip(self): pass
                def test_pass(self): pass
        ''')
        result_file = self.root / "child-result.json"
        process = subprocess.run(
            [sys.executable, str(SCRIPT), "--_child", str(self.tests), "test_mixed.py",
             "--_child-result", str(result_file)],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
        self.assertEqual(process.returncode, 0, process.stdout.decode("utf-8", errors="replace"))
        data = json.loads(result_file.read_text(encoding="utf-8"))
        self.assertEqual(data, {
            "discovered": 2,
            "testsRun": 2,
            "failures": [],
            "errors": [],
            "skipped": [["test_mixed.Cases.test_skip", "structured reason"]],
            "expectedFailures": 0,
            "unexpectedSuccesses": [],
            "fixtureSkips": 0,
        })
        self.assertNotIn(b"__myflames_child__", process.stdout)
        code, output = self.run_runner()
        self.assertEqual(code, 0, output)
        self.assertNotIn("__myflames_child__", output)

    def test_home_is_unique_removed_and_other_environment_survives(self):
        for name in ("test_first", "test_second"):
            destination = str(self.root / (name + ".json"))
            self.module(name, '''
                import json
                import os
                from pathlib import Path
                import unittest
                class Cases(unittest.TestCase):
                    def test_environment(self):
                        self.assertEqual(os.environ['USERPROFILE'], os.environ['HOME'])
                        self.assertEqual(os.environ['MYFLAMES_SECRET_BACKEND'], 'vault')
                        self.assertEqual(os.environ['RUNNER_SENTINEL'], 'passed through')
                        home = Path(os.environ['HOME'])
                        self.assertFalse((home / 'claimed').exists())
                        (home / 'claimed').write_text('owned')
                        Path(%r).write_text(json.dumps({'home': str(home), 'pid': os.getpid()}))
            ''' % destination)
        code, output = self.run_runner()
        self.assertEqual(code, 0, output)
        records = [json.loads((self.root / (name + ".json")).read_text())
                   for name in ("test_first", "test_second")]
        self.assertNotEqual(records[0]["home"], records[1]["home"])
        self.assertNotEqual(records[0]["pid"], records[1]["pid"])
        self.assertTrue(all(not Path(record["home"]).exists() for record in records))

    def test_jobs_one_keeps_one_process_and_old_discovery_order(self):
        records = self.root / "serial.jsonl"
        for name in ("test_second", "test_first"):
            self.module(name, '''
                import json
                import os
                import unittest
                class Cases(unittest.TestCase):
                    def test_serial(self):
                        with open(%r, 'a') as handle:
                            handle.write(json.dumps([__name__, os.getpid(), os.environ['HOME']]) + '\\n')
            ''' % str(records))
        code, output = self.run_runner(JOBS="1")
        self.assertEqual(code, 0, output)
        rows = [json.loads(line) for line in records.read_text().splitlines()]
        self.assertEqual([row[0] for row in rows], ["test_first", "test_second"])
        self.assertEqual(rows[0][1:], rows[1][1:])
        self.assertFalse(Path(rows[0][2]).exists())
        self.assertNotIn("Inventory", output)  # JOBS=1 has no inventory comparison.
        self.assertIn("__serial__", output)
        self.assertEqual(runner.test_command("test"), [sys.executable, "-m", "unittest", "discover",
                                                     "-s", "test", "-p", "test_*.py", "-v"])
        # Explicit --jobs takes precedence over JOBS.
        records.unlink()
        code, output = self.run_runner("--jobs", "2", JOBS="1")
        self.assertEqual(code, 0, output)
        rows = [json.loads(line) for line in records.read_text().splitlines()]
        self.assertNotEqual(rows[0][1], rows[1][1])

    def test_timeout_is_a_failure_and_does_not_hang(self):
        self.module("test_sleep", '''
            import time
            import unittest
            class Cases(unittest.TestCase):
                def test_sleep(self):
                    time.sleep(10)
        ''')
        start = time.monotonic()
        code, output = self.run_runner("--timeout", "0.5")
        self.assertEqual(code, 1, output)
        self.assertLess(time.monotonic() - start, 5)
        self.assertIn("test_sleep (TIMED OUT)", output)
        self.assertIn("timeouts=1", output)
        self.assertIn("INVENTORY MISMATCH", output)

    @unittest.skipUnless(os.name == "posix", "runner descendant signal check requires POSIX")
    def test_timeout_kills_the_whole_process_group(self):
        marker = self.root / "survived"
        pidfile = self.root / "descendant.pid"
        child = "import time; from pathlib import Path; time.sleep(1.5); Path(%r).touch()" % str(marker)
        self.module("test_descendant", '''
            import subprocess
            import sys
            import time
            from pathlib import Path
            import unittest
            class Cases(unittest.TestCase):
                def test_descendant(self):
                    child = subprocess.Popen([sys.executable, '-c', %r])
                    Path(%r).write_text(str(child.pid))
                    time.sleep(10)
        ''' % (child, str(pidfile)))
        try:
            code, output = self.run_runner("--timeout", "0.7")
            self.assertEqual(code, 1, output)
            self.assertIn("timeouts=1", output)
            self.assertTrue(pidfile.exists(), output)
            time.sleep(1.6)
            self.assertFalse(marker.exists(), "the timed-out worker left its descendant alive")
        finally:
            if pidfile.exists():
                try:
                    os.kill(int(pidfile.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def stop(self, process):
        if process.poll() is None:
            process.kill()
        process.wait()

    @unittest.skipUnless(os.name == "posix", "descendant sweep requires POSIX")
    def test_kill_descendants_walks_the_ps_table_and_spares_bystanders(self):
        # A fake `ps` on PATH reports a synthetic pid/ppid table, so the walk
        # is exercised the same way with or without a real ps.
        bin_dir = self.root / "fakebin"
        bin_dir.mkdir()
        table = self.root / "ps-table"
        fake = bin_dir / "ps"
        fake.write_text("#!/bin/sh\ncat \"%s\"\n" % table, encoding="utf-8")
        fake.chmod(0o755)
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                  start_new_session=True)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 start_new_session=True)
        bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                     start_new_session=True)
        for process in (worker, child, bystander):
            self.addCleanup(self.stop, process)
        table.write_text("%d %d\n" % (child.pid, worker.pid), encoding="utf-8")
        path = os.environ["PATH"]
        os.environ["PATH"] = str(bin_dir) + os.pathsep + path
        try:
            runner.kill_descendants(worker)
        finally:
            os.environ["PATH"] = path
        self.assertEqual(child.wait(timeout=5), -signal.SIGKILL)
        self.assertIsNone(bystander.poll(),
                          "the sweep signalled a pid outside the worker's tree")
        self.assertIsNone(worker.poll(), "the sweep signalled the leader itself")

    @unittest.skipUnless(os.name == "posix", "descendant sweep requires POSIX")
    def test_kill_descendants_without_ps_degrades_quietly(self):
        empty = self.root / "emptybin"
        empty.mkdir()
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                  start_new_session=True)
        self.addCleanup(self.stop, worker)
        path = os.environ["PATH"]
        os.environ["PATH"] = str(empty)
        try:
            runner.kill_descendants(worker)
        finally:
            os.environ["PATH"] = path
        self.assertIsNone(worker.poll())

    @unittest.skipUnless(PS_SCAN_AVAILABLE,
                         "detached descendant sweep requires a working ps")
    def test_timeout_kills_detached_descendants_and_spares_bystanders(self):
        pidfile = self.root / "detached.pid"
        self.module("test_detached", '''
            import subprocess
            import sys
            import time
            from pathlib import Path
            import unittest
            class Cases(unittest.TestCase):
                def test_detached(self):
                    child = subprocess.Popen(
                        [sys.executable, '-c', 'import time; time.sleep(30)'],
                        start_new_session=True)
                    Path(%r).write_text(str(child.pid))
                    time.sleep(30)
        ''' % str(pidfile))
        bystander = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True)
        self.addCleanup(self.stop, bystander)
        try:
            code, output = self.run_runner("--timeout", "0.7")
            self.assertEqual(code, 1, output)
            self.assertIn("timeouts=1", output)
            self.assertTrue(pidfile.exists(), output)
            detached = int(pidfile.read_text())
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(detached, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail("the timed-out worker left its detached descendant alive")
            self.assertIsNone(bystander.poll(),
                              "the sweep killed a process outside the worker's tree")
        finally:
            if pidfile.exists():
                try:
                    os.kill(int(pidfile.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_module_with_a_finished_detached_child_passes(self):
        self.module("test_child_ok", '''
            import subprocess
            import sys
            import unittest
            class Cases(unittest.TestCase):
                def test_child_ok(self):
                    child = subprocess.Popen([sys.executable, '-c', 'pass'],
                                             start_new_session=True)
                    self.assertEqual(child.wait(timeout=10), 0)
        ''')
        code, output = self.run_runner()
        self.assertEqual(code, 0, output)
        self.assertIn("Inventory: discovered 1 tests in 1 modules; ran 1 tests", output)

    def test_container_modules_share_one_lane_but_allow_other_workers(self):
        lock = self.root / "lane.lock"
        for name in sorted(runner.SERIAL_MODULES):
            self.module(name, '''
                import os
                import time
                import unittest
                class Cases(unittest.TestCase):
                    def test_lane(self):
                        fd = os.open(%r, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                        try:
                            time.sleep(0.25)
                        finally:
                            os.close(fd)
                            os.unlink(%r)
            ''' % (str(lock), str(lock)))
        self.passing_module()
        code, output = self.run_runner("--jobs", "3")
        self.assertEqual(code, 0, output)
        self.assertIn("Inventory: discovered 3 tests in 3 modules; ran 3 tests", output)

    def test_docker_starters_are_in_the_serial_lane(self):
        starters = set()
        for path in (ROOT / "test").rglob("test_*.py"):
            # AST string literals avoid comments, fake shell scripts and this
            # test's generated sources. _docker('run', ...) is the helper in
            # the live tests; ['docker', 'run', ...] catches direct launchers.
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call):
                    continue
                helper = isinstance(node.func, ast.Name) and node.func.id == "_docker"
                if helper and node.args and string_literal(node.args[0]) == "run":
                    starters.add(path.stem)
                for argument in node.args:
                    if isinstance(argument, (ast.List, ast.Tuple)):
                        words = [string_literal(item) for item in argument.elts]
                        if len(words) >= 2 and words[:2] == ["docker", "run"]:
                            starters.add(path.stem)
        self.assertTrue(starters, "Docker starter detection must exercise real sources")
        self.assertFalse(starters - runner.SERIAL_MODULES,
                         "Add container starters to SERIAL_MODULES: %s" % sorted(starters - runner.SERIAL_MODULES))

    def test_discovery_preserves_packages_and_duplicate_basenames(self):
        self.module("one/__init__", "")
        self.module("two/__init__", "")
        self.module("ignored/test_not_importable", "raise RuntimeError('not a unittest package')")
        for package in ("one", "two"):
            self.passing_module(package + "/test_nested")
        code, output = self.run_runner()
        self.assertEqual(code, 0, output)
        self.assertIn("Inventory: discovered 2 tests in 2 modules; ran 2 tests", output)
        self.assertIn("one.test_nested", output)
        self.assertIn("two.test_nested", output)
        self.assertNotIn("not a unittest package", output)

    def test_scheduling_uses_recorded_durations_and_size_fallback(self):
        small = self.passing_module("test_small")
        large = self.module("test_large", "# large file\n" * 100)
        modules = [{"name": path.stem, "path": str(path)} for path in (small, large)]
        durations = self.root / "durations.json"
        self.assertEqual(runner.schedule(modules, str(durations))[0]["name"], "test_large")
        durations.write_text(json.dumps({"test_small": 100, "test_large": 1}))
        self.assertEqual(runner.schedule(modules, str(durations))[0]["name"], "test_small")

    def test_recorded_durations_never_live_among_plan_fixtures(self):
        # test_misestimate_marks and parity.mjs read every top-level test/*.json as a plan.
        self.assertEqual(runner.DURATIONS.parent, ROOT / "scripts")
        self.assertFalse((ROOT / "test" / "test_durations.json").exists())

    def test_invalid_controls_and_allowlist_fail_clearly(self):
        self.passing_module()
        for arguments in (("--jobs", "0"), ("--timeout", "0"), ("--timeout", "nan")):
            with self.subTest(arguments=arguments):
                code, output = self.run_runner(*arguments)
                self.assertNotEqual(code, 0, output)
        path = self.root / "bad-allowlist.txt"
        path.write_text("# invalid expression follows\n[\n", encoding="utf-8")
        code, output = self.run_runner("--skip-allowlist", str(path))
        self.assertEqual(code, 1, output)
        self.assertIn("bad-allowlist.txt:2", output)

    def test_allowlist_covers_only_reviewed_linux_exceptions(self):
        patterns = runner.load_allowlist(str(ROOT / "test" / "skip_allowlist.txt"))
        for reason in ("Docker not available", "Docker or mysql client not available",
                       "the fake client is a POSIX script", "POSIX permissions", "fixture missing"):
            self.assertFalse(any(pattern.fullmatch(reason) for pattern in patterns), reason)
        for reason in ("Windows Credential Manager exists only on Windows", "job objects are Windows",
                       "live Docker tests run in the integration workflow",
                       "Workspace npm dependencies are needed to bundle the actual TypeScript helper"):
            self.assertTrue(any(pattern.fullmatch(reason) for pattern in patterns), reason)


if __name__ == "__main__":
    unittest.main()
