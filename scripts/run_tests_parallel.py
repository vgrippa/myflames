#!/usr/bin/env python3
"""Run unittest modules in isolated processes, using only the standard library.

JOBS=1 keeps the original single-process discovery command, with no inventory
comparison, for debugging test order. Parallel runs use an independent
HOME/USERPROFILE and vault per module, and compare each module's inventory
count against what its own worker discovers and runs.
"""

import argparse
import ast
import collections
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest


PATTERN = "test_*.py"
STATUS_INTERVAL = 30.0
# These start real Docker containers. Keep their complete module lifetimes in
# one lane, including setup/teardown. Reviewed all socket/server constructors:
# the other modules use ephemeral ports (WorkspaceServer defaults to port 0),
# or mock their fixed addresses. Fixture-generator/GA tests stub Docker.
SERIAL_MODULES = frozenset((
    "test_connector_integration",
    "test_schema_versions_live",
))

RAN = re.compile(r"^Ran (\d+) tests? in [0-9.]+s$", re.MULTILINE)
RESULT = re.compile(r"^(OK|FAILED)(?: \(([^\n]*)\))?$", re.MULTILINE)
TEST_ID = re.compile(r"^\s*(\w+) \(([^)]+)\)")
SKIPPED = re.compile(r" \.\.\. skipped (.+)$")


def fix_sys_path():
    """Resolve imports like `python -m unittest` run from the working directory.

    Executed as a script, sys.path[0] is this file's folder, so test modules
    cannot import packages that live only at the repository root. Put the
    working directory first and take the script's own folder out.
    """
    cwd = os.path.abspath(os.getcwd())
    script_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [entry for entry in sys.path
                   if os.path.abspath(entry or cwd) != script_dir]
    if not sys.path or os.path.abspath(sys.path[0] or cwd) != cwd:
        sys.path.insert(0, cwd)


def iter_cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            for case in iter_cases(item):
                yield case
        else:
            yield item


def fixture_maps(suite):
    """Index the discovered cases per class and per module.

    Must run before the suite does: TestSuite.run replaces each entry with
    None to release memory, so a post-run walk sees nothing.
    """
    by_class = collections.Counter()
    by_module = collections.Counter()
    for case in iter_cases(suite):
        cls = type(case)
        keys = {"%s.%s" % (cls.__module__, cls.__name__)}
        keys.add("%s.%s" % (cls.__module__, getattr(cls, "__qualname__", cls.__name__)))
        for key in keys:
            by_class[key] += 1
        by_module[cls.__module__] += 1
    return by_class, by_module


def fixture_skip_count(by_class, by_module, result):
    """Count test methods suppressed by setUpClass/setUpModule skips.

    unittest records one skip per skipped fixture and excludes that class's or
    module's test methods from "Ran N", so the parent needs this count to
    explain the gap between discovery and execution. The skip is reported
    against a placeholder whose description names the fixture.
    """
    suppressed = 0
    for skipped, _reason in result.skipped:
        if isinstance(skipped, type) and issubclass(skipped, unittest.TestCase):
            keys = ["%s.%s" % (skipped.__module__, skipped.__name__),
                    "%s.%s" % (skipped.__module__, getattr(skipped, "__qualname__", skipped.__name__))]
            suppressed += max(by_class.get(key, 0) for key in keys)
            continue
        if isinstance(skipped, types.ModuleType):
            suppressed += by_module.get(skipped.__name__, 0)
            continue
        description = getattr(skipped, "description", "") or ""
        match = re.match(r"^setUpClass \((.+)\)$", description)
        if match:
            suppressed += by_class.get(match.group(1), 0)
            continue
        match = re.match(r"^setUpModule \((.+)\)$", description)
        if match:
            suppressed += by_module.get(match.group(1), 0)
    return suppressed


def result_test_id(test):
    """A stable id for any object unittest reports a result against.

    Real cases and subtests answer id(); discovery failures are _ErrorHolder
    placeholders whose id() is their description; class and module fixture
    skips arrive as the class or module itself, named after their holder.
    """
    if isinstance(test, type) and issubclass(test, unittest.TestCase):
        return "setUpClass (%s.%s)" % (
            test.__module__, getattr(test, "__qualname__", test.__name__))
    if isinstance(test, types.ModuleType):
        return "setUpModule (%s)" % test.__name__
    ident = getattr(test, "id", None)
    if callable(ident):
        try:
            return str(ident())
        except Exception:
            pass
    return str(test)


class RecordingTextResult(unittest.TextTestResult):
    """TextTestResult that also records structured, version-stable results.

    The verbose human-readable stream is written unchanged; the parent reads
    these lists from the child's JSON file instead of parsing that stream.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failure_ids = []
        self.error_ids = []
        self.skip_pairs = []
        self.unexpected_success_ids = []
        self.expected_failure_count = 0

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.failure_ids.append(result_test_id(test))

    def addError(self, test, err):
        super().addError(test, err)
        self.error_ids.append(result_test_id(test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.skip_pairs.append([result_test_id(test), str(reason)])

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.expected_failure_count += 1

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.unexpected_success_ids.append(result_test_id(test))


def child(start_dir, pattern, top_level, result_path):
    """Discover and run one module exactly like `python -m unittest discover`.

    Human-readable verbose output goes to the captured stream unchanged. Every
    number the parent needs (discovered count, testsRun, failures, errors,
    skips, expected failures, unexpected successes, fixture-skipped tests)
    travels through the JSON result file, written atomically once the run ends.
    """
    fix_sys_path()
    suite = unittest.TestLoader().discover(start_dir, pattern, top_level)
    discovered = suite.countTestCases()
    by_class, by_module = fixture_maps(suite)
    result = unittest.TextTestRunner(
        verbosity=2, resultclass=RecordingTextResult).run(suite)
    record = {
        "discovered": discovered,
        "testsRun": result.testsRun,
        "failures": result.failure_ids,
        "errors": result.error_ids,
        "skipped": result.skip_pairs,
        "expectedFailures": result.expected_failure_count,
        "unexpectedSuccesses": result.unexpected_success_ids,
        "fixtureSkips": fixture_skip_count(by_class, by_module, result),
    }
    temporary = result_path + ".part"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, result_path)
    return 0 if result.wasSuccessful() else 1


def inventory(start_dir, destination):
    """Ask unittest itself which files it discovers, without importing in the parent.

    Recording _find_test_path preserves unittest's package traversal and
    load_tests behavior, including failed imports. Its returned suite, rather
    than a count of methods in source, supplies the independent inventory.
    """
    loader = unittest.TestLoader()
    find_path = loader._find_test_path
    modules = []
    unsupported = []
    root = Path(start_dir).resolve()

    def record(full_path, pattern, *args, **kwargs):
        tests, recurse = find_path(full_path, pattern, *args, **kwargs)
        if tests is not None:
            path = Path(full_path)
            if path.is_file():
                relative = path.resolve().relative_to(root)
                modules.append({"name": ".".join(relative.with_suffix("").parts),
                                "path": str(path.resolve()),
                                "count": tests.countTestCases()})
            elif tests.countTestCases():
                # File-pattern discovery cannot isolate tests supplied by a
                # package's __init__/load_tests hook. Fail visibly, never omit
                # them or duplicate them in every child file's discovery.
                unsupported.append(str(path))
        return tests, recurse

    loader._find_test_path = record
    suite = loader.discover(str(root), PATTERN)
    with open(destination, "w", encoding="utf-8") as handle:
        json.dump({"count": suite.countTestCases(), "modules": modules,
                   "unsupported": unsupported}, handle)


def isolated_environment(home):
    env = os.environ.copy()
    env.update(HOME=home, USERPROFILE=home, MYFLAMES_SECRET_BACKEND="vault")
    return env


def kill_descendants(process):
    """SIGKILL every descendant of the worker, including detached sessions.

    A child started with start_new_session leaves the worker's process
    group, so the group kill alone misses it; only a parent-pid walk finds
    those. `ps -A -o pid=,ppid=` is repeated until a scan adds no new pid
    while the leader still runs, so children forked mid-sweep are caught
    too. Without a usable `ps` the caller's group kill remains the only
    sweep. Nothing outside the worker's tree is signalled.
    """
    seen = set()
    while process.poll() is None:
        try:
            output = subprocess.check_output(
                ["ps", "-A", "-o", "pid=,ppid="], stderr=subprocess.DEVNULL)
        except (OSError, subprocess.CalledProcessError):
            return
        children = {}
        for line in output.decode("utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            try:
                pid, ppid = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            children.setdefault(ppid, []).append(pid)
        found = set()
        frontier = [process.pid]
        while frontier:
            parent = frontier.pop()
            for kid in children.get(parent, ()):
                if kid not in found:
                    found.add(kid)
                    frontier.append(kid)
        new = found - seen
        if not new:
            return
        for pid in sorted(new):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        seen |= found


def kill_group(process):
    """Terminate the whole tree, even when the leader has already exited."""
    if os.name == "posix":
        kill_descendants(process)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        # Windows' standard OS utility terminates the process tree; kill() on
        # its own would leave mysql clients or other children behind.
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if process.poll() is None:
            process.kill()
    process.wait()


class Job(object):
    def __init__(self, name, command, structured=False):
        self.name = name
        self.home = tempfile.TemporaryDirectory(prefix="myflames-test-")
        self.result_path = None
        if structured:
            # The child writes its JSON results here, inside its own folder.
            self.result_path = os.path.join(self.home.name, "child-result.json")
            command = command + ["--_child-result", self.result_path]
        self.command = command
        self.output = tempfile.TemporaryFile()
        self.started = time.monotonic()
        self.timed_out = False
        options = {"start_new_session": True} if os.name == "posix" else {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        try:
            self.process = subprocess.Popen(
                command, env=isolated_environment(self.home.name),
                stdout=self.output, stderr=subprocess.STDOUT, **options)
        except BaseException:
            self.output.close()
            self.home.cleanup()
            raise

    def finish(self):
        try:
            kill_group(self.process)
            self.output.seek(0)
            output = self.output.read().decode("utf-8", errors="replace")
            record = {"name": self.name, "output": output,
                      "returncode": self.process.returncode,
                      "seconds": time.monotonic() - self.started,
                      "timed_out": self.timed_out,
                      "structured": self.result_path is not None,
                      "result_json": None}
            if self.result_path is not None:
                try:
                    with open(self.result_path, encoding="utf-8") as handle:
                        record["result_json"] = json.load(handle)
                except (OSError, ValueError):
                    pass  # Missing or unreadable JSON means the module crashed.
            return record
        finally:
            self.output.close()
            self.home.cleanup()


def run_jobs(tasks, jobs, timeout, started, total=None):
    """Bound concurrency and reserve at most one slot for the serial lane."""
    pending = list(tasks)
    active = []
    finished = []
    total = len(tasks) if total is None else total
    next_status = time.monotonic() + STATUS_INTERVAL
    try:
        while pending or active:
            while pending and len(active) < jobs:
                serial_busy = any(job.name in SERIAL_MODULES for job in active)
                index = next((i for i, task in enumerate(pending)
                              if not serial_busy or task[0] not in SERIAL_MODULES), None)
                if index is None:
                    break
                name, command, structured = pending.pop(index)
                try:
                    active.append(Job(name, command, structured))
                except OSError as exc:
                    finished.append({"name": name, "output": str(exc), "returncode": -1,
                                     "seconds": 0.0, "timed_out": False,
                                     "structured": structured, "result_json": None})
            now = time.monotonic()
            for job in active[:]:
                if job.process.poll() is None and now - job.started >= timeout:
                    job.timed_out = True
                    kill_group(job.process)
                if job.process.poll() is not None:
                    finished.append(job.finish())
                    active.remove(job)
            if now >= next_status:
                print("[%d/%d done; %.1fs elapsed] running: %s" % (
                    len(finished), total, now - started,
                    ", ".join(job.name for job in active) or "none"), file=sys.stderr, flush=True)
                next_status = now + STATUS_INTERVAL
            if active:
                time.sleep(0.05)
    finally:
        # Also clean up on Ctrl-C or an exception in reporting/scheduling.
        for job in active:
            job.finish()
    return finished


CHILD_RESULT_SPEC = (
    ("discovered", int), ("testsRun", int), ("expectedFailures", int),
    ("fixtureSkips", int), ("failures", list), ("errors", list),
    ("unexpectedSuccesses", list), ("skipped", list),
)


def child_record_valid(data):
    """The child JSON is trusted only when every field has the expected shape."""
    if not isinstance(data, dict):
        return False
    for key, kind in CHILD_RESULT_SPEC:
        value = data.get(key)
        if kind is int:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return False
        elif not isinstance(value, list):
            return False
    for key in ("failures", "errors", "unexpectedSuccesses"):
        if not all(isinstance(item, str) for item in data[key]):
            return False
    for entry in data["skipped"]:
        if (not isinstance(entry, list) or len(entry) != 2
                or not all(isinstance(part, str) for part in entry)):
            return False
    return True


def parse_child_result(result):
    """Take every count from the child's JSON, never from unittest's text.

    A missing, unreadable or invalid result file means the module crashed
    mid-run; the caller reports the captured output, as for any crash.
    """
    data = result["result_json"]
    valid = child_record_valid(data)
    counts = collections.Counter()
    skips = []
    tests = 0
    if valid:
        tests = data["testsRun"]
        counts.update({
            "failures": len(data["failures"]),
            "errors": len(data["errors"]),
            "skipped": len(data["skipped"]),
            "expected failures": data["expectedFailures"],
            "unexpected successes": len(data["unexpectedSuccesses"]),
        })
        skips = [(result["name"], entry[0], entry[1]) for entry in data["skipped"]]
    crashed = not result["timed_out"] and (not valid or (
        result["returncode"] != 0 and not any(
            counts[key] for key in ("failures", "errors", "unexpected successes"))))
    result.update(tests=tests, counts=counts, skips=skips, crashed=crashed,
                  malformed=not valid,
                  discovered=data["discovered"] if valid else None,
                  fixture_skips=data["fixtureSkips"] if valid else 0)
    return result


def parse_text_result(result):
    """JOBS=1 lane only: the plain `python -m unittest` command has no JSON file."""
    output = result["output"]
    ran = RAN.findall(output)
    summaries = RESULT.findall(output)
    counts = collections.Counter()
    if summaries:
        for key, value in re.findall(r"([a-z ]+)=(\d+)", summaries[-1][1]):
            counts[key] += int(value)
    skips = []
    current_id = "<unknown test>"
    for line in output.splitlines():
        match = TEST_ID.match(line)
        if match:
            method, current_id = match.groups()
            if method.startswith("test") and not current_id.endswith("." + method):
                current_id += "." + method  # Python 3.7 prints only module.Class.
        match = SKIPPED.search(line)
        if match:
            try:
                reason = ast.literal_eval(match.group(1))
            except (ValueError, SyntaxError):
                reason = match.group(1)
            name = result["name"]
            if name == "__serial__" and current_id != "<unknown test>":
                name = current_id.rsplit(".", 2)[0]
            skips.append((name, current_id, str(reason)))
    malformed = not ran or not summaries or counts["skipped"] != len(skips)
    crashed = not result["timed_out"] and (
        malformed or (result["returncode"] != 0 and (not summaries or summaries[-1][0] != "FAILED")))
    # A FAILED result without an explained failure is still a failure.
    if summaries and summaries[-1][0] == "FAILED" and not any(
            counts[key] for key in ("failures", "errors", "unexpected successes")):
        crashed = True
    result.update(tests=int(ran[-1]) if ran else 0, counts=counts, skips=skips,
                  crashed=crashed, malformed=malformed)
    return result


def parse_result(result):
    """Structured JSON for module workers; text only for the JOBS=1 lane."""
    if result["structured"]:
        return parse_child_result(result)
    return parse_text_result(result)


def load_allowlist(path):
    patterns = []
    if path:
        with open(path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                line = line.strip()
                if line and not line.startswith("#"):
                    try:
                        patterns.append(re.compile(line))
                    except re.error as exc:
                        raise ValueError("%s:%d: %s" % (path, number, exc))
    return patterns


# Recorded module seconds live beside this script, not in test/: every
# top-level test/*.json is read as an EXPLAIN plan fixture.
DURATIONS = Path(__file__).resolve().with_name("test_durations.json")


def schedule(modules, durations_path=DURATIONS):
    path = Path(durations_path)
    durations = {}
    if path.exists():
        with path.open(encoding="utf-8") as handle:
            durations = json.load(handle)
        if not isinstance(durations, dict):
            raise ValueError("%s must contain a module-to-seconds object" % path)
    def weight(module):
        value = durations.get(module["name"])
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError("invalid duration for %s" % module["name"])
            return value
        return Path(module["path"]).stat().st_size
    return sorted(modules, key=lambda module: (-weight(module), module["name"]))


def test_command(start_dir, module=None):
    command = [sys.executable, "-m", "unittest", "discover", "-s", start_dir,
               "-p", Path(module["path"]).name if module else PATTERN, "-v"]
    if module and Path(module["path"]).parent != Path(start_dir).resolve():
        # Preserve package-qualified IDs and isolate identically named files
        # in different packages. Flat test/ modules use the original command.
        command[5] = str(Path(module["path"]).parent)
        command.extend(["-t", str(Path(start_dir).resolve())])
    return command


def child_command(start_dir, module):
    """One worker per module, discovered and run by this same script."""
    command = [sys.executable, str(Path(__file__).resolve()), "--_child",
               start_dir, Path(module["path"]).name]
    if Path(module["path"]).parent != Path(start_dir).resolve():
        # Preserve package-qualified IDs and isolate identically named files
        # in different packages. Flat test/ modules use the original command.
        command[3] = str(Path(module["path"]).parent)
        command.extend(["--_child-top", str(Path(start_dir).resolve())])
    return command


def inventory_report(modules, results):
    """Compare each module's inventory count with what its worker discovered and ran."""
    by_name = {}
    for result in results:
        by_name.setdefault(result["name"], []).append(result)
    problems = []
    notes = []
    discovered = ran = fixture_skips = 0
    for module in modules:
        name = module["name"]
        expected = module["count"]
        discovered += expected
        runs = by_name.pop(name, [])
        if len(runs) != 1:
            problems.append("%s ran %d times (expected exactly once)" % (name, len(runs)))
            continue
        result = runs[0]
        child_count = result.get("discovered")
        fixture = result.get("fixture_skips") or 0
        ran += result["tests"]
        if child_count is None:
            problems.append("%s did not report a discovery count" % name)
            continue
        if child_count != expected:
            problems.append("%s: inventory discovered %d, worker discovered %d" % (
                name, expected, child_count))
            continue
        fixture_skips += fixture
        missing = child_count - result["tests"] - fixture
        if missing > 0:
            problems.append("%s: %d discovered tests never ran, and no class or module "
                            "fixture skip explains them" % (name, missing))
        elif fixture:
            notes.append("  %s: discovered %d; ran %d; %d not run because a class or "
                         "module fixture skipped" % (name, child_count, result["tests"], fixture))
    for name in sorted(by_name):
        problems.append("%s ran but is not in the inventory" % name)
    if notes:
        print("\nInventory per module:")
        for line in notes:
            print(line)
    print("\nInventory: discovered %d tests in %d modules; ran %d tests%s" % (
        discovered, len(modules), ran,
        "; %d not run because a class or module fixture skipped" % fixture_skips if fixture_skips else ""))
    if problems:
        print("INVENTORY MISMATCH:")
        for problem in problems:
            print("  " + problem)
    return bool(problems)


def positive_int(value):
    number = int(value)
    if number < 1:
        raise ValueError("must be at least 1")
    return number


def positive_seconds(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("must be finite and greater than 0")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-dir", default="test")
    parser.add_argument("--jobs", type=positive_int, default=os.environ.get("JOBS", str(min(os.cpu_count() or 1, 8))),
                        help="worker processes; 1 keeps the original single-process command "
                             "with no inventory comparison (default: JOBS or CPU count, capped at 8)")
    parser.add_argument("--timeout", type=positive_seconds, default=900.0, help="seconds per module (default: 900)")
    parser.add_argument("--durations", default=str(DURATIONS),
                        help="recorded module seconds for longest-first scheduling (default: scripts/test_durations.json)")
    parser.add_argument("--durations-out", help="write measured module seconds as JSON (suite seconds with JOBS=1)")
    parser.add_argument("--skip-allowlist", help="one full-match regular expression per line; # comments allowed")
    parser.add_argument("--_inventory-out", help=argparse.SUPPRESS)
    parser.add_argument("--_child", nargs=2, metavar=("START_DIR", "PATTERN"), help=argparse.SUPPRESS)
    parser.add_argument("--_child-top", help=argparse.SUPPRESS)
    parser.add_argument("--_child-result", metavar="PATH", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._child:
        if not args._child_result:
            print("--_child requires --_child-result PATH", file=sys.stderr)
            return 2
        return child(args._child[0], args._child[1], args._child_top, args._child_result)
    if args._inventory_out:
        fix_sys_path()
        inventory(args.start_dir, args._inventory_out)
        return 0
    started = time.monotonic()
    try:
        allowlist = load_allowlist(args.skip_allowlist)
        discovered = None
        if args.jobs == 1:
            tasks = [("__serial__", test_command(args.start_dir), False)]
        else:
            # Discovery imports test code, so inventory gets the same isolation and
            # process-tree timeout as test workers, plus a private result channel.
            with tempfile.TemporaryDirectory(prefix="myflames-inventory-") as temporary:
                manifest = str(Path(temporary) / "inventory.json")
                command = [sys.executable, str(Path(__file__).resolve()), "--start-dir", args.start_dir,
                           "--_inventory-out", manifest]
                probe = run_jobs([("inventory", command, False)], 1, args.timeout, started)[0]
                if probe["returncode"] != 0 or probe["timed_out"] or not Path(manifest).exists():
                    print("Inventory failed%s:\n%s" % (
                        " (timed out)" if probe["timed_out"] else "", probe["output"]), file=sys.stderr)
                    print("Ran 0 tests in %.3fs\n\nFAILED (inventory errors=1)" % (time.monotonic() - started))
                    return 1
                with open(manifest, encoding="utf-8") as handle:
                    discovered = json.load(handle)
            if discovered["unsupported"]:
                raise ValueError("cannot isolate package-level tests: %s; use JOBS=1" % ", ".join(discovered["unsupported"]))
            tasks = [(module["name"], child_command(args.start_dir, module), True)
                     for module in schedule(discovered["modules"], args.durations)]
        results = [parse_result(result) for result in run_jobs(tasks, args.jobs, args.timeout, started)]
        if args.durations_out:
            with open(args.durations_out, "w", encoding="utf-8") as handle:
                json.dump({result["name"]: round(result["seconds"], 6) for result in results}, handle,
                          indent=2, sort_keys=True)
                handle.write("\n")
    except (OSError, ValueError) as exc:
        print("Test runner error: %s" % exc, file=sys.stderr)
        return 1
    counts = collections.Counter()
    skips = []
    for result in results:
        counts.update(result["counts"])
        skips.extend(result["skips"])
        if result["returncode"] != 0 or result["crashed"] or result["timed_out"] or result["counts"]["failures"] or result["counts"]["errors"]:
            print("\n=== %s%s ===" % (result["name"], " (TIMED OUT)" if result["timed_out"] else ""))
            print(result["output"], end="" if result["output"].endswith("\n") else "\n")
            if result["crashed"]:
                print("Module crashed or produced an incomplete unittest result (exit %s)." % result["returncode"])
            if result["malformed"]:
                if result["structured"]:
                    print("Missing, unreadable or invalid child result file; "
                          "the module did not finish reporting.")
                else:
                    print("Missing summary or skip accounting mismatch; captured skip details are incomplete.")
    print("\nSlowest %d modules:" % min(15, len(results)))
    for result in sorted(results, key=lambda item: (-item["seconds"], item["name"]))[:15]:
        print("  %8.3fs  %s" % (result["seconds"], result["name"]))
    print("\nSkip summary (reason -> count):")
    reasons = collections.Counter(skip[2] for skip in skips)
    for reason, count in sorted(reasons.items()):
        print("  %s -> %d" % (json.dumps(reason, ensure_ascii=True), count))
    if not reasons:
        print("  none")
    unexpected = [skip for skip in skips if not any(pattern.fullmatch(skip[2]) for pattern in allowlist)]
    if unexpected:
        print("\nUnexpected skips (%d; skip budget %s):" % (
            len(unexpected), "enforced" if os.environ.get("MYFLAMES_SKIP_BUDGET") == "1" else "not enforced"))
        for module, test_id, reason in unexpected:
            print("  %s: %s: %s" % (module, test_id, json.dumps(reason, ensure_ascii=True)))
    tests = sum(result["tests"] for result in results)
    mismatch = discovered is not None and inventory_report(discovered["modules"], results)
    extra = collections.Counter({"crashes": sum(result["crashed"] for result in results),
                                 "timeouts": sum(result["timed_out"] for result in results),
                                 "inventory mismatches": int(mismatch),
                                 "unexpected skips": len(unexpected) if os.environ.get("MYFLAMES_SKIP_BUDGET") == "1" else 0})
    failed = any(counts[key] for key in ("failures", "errors", "unexpected successes")) or any(extra.values())
    summary = counts.copy()
    summary.update(extra)
    fields = ["%s=%d" % (key, summary[key]) for key in (
        "failures", "errors", "skipped", "expected failures", "unexpected successes",
        "crashes", "timeouts", "inventory mismatches", "unexpected skips") if summary[key]]
    print("Ran %d tests in %.3fs" % (tests, time.monotonic() - started))
    print("\n%s%s" % ("FAILED" if failed else "OK", " (%s)" % ", ".join(fields) if fields else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Test run interrupted; worker process groups stopped.", file=sys.stderr)
        sys.exit(130)
