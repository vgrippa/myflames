"""Run live suite modules: one PASS/FAIL line per test, plus a JSON summary.

Usage: python3 -m test.live._runner [--json-out PATH] <module> [<module>...]

Modules run in order, in one process (they share the MYFLAMES_LIVE_HOME the
first suite creates). Each test prints `PASS|FAIL|ERROR|SKIP <id> <seconds>s`
as it finishes; the JSON summary (always printed, and written to --json-out)
is what scripts/live-tests.sh reads for the evidence record. Exit status is 0
only when every test passed or skipped.
"""
import argparse
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest

LIVE_DIR = os.path.dirname(os.path.abspath(__file__))
if LIVE_DIR not in sys.path:
    sys.path.insert(0, LIVE_DIR)
import _lab  # noqa: E402 - share the same helper module as the live suites


def load_suite_module(name):
    """Load a live suite by its module name or file, without test/ as a package."""
    path = name if name.endswith(".py") else os.path.join(LIVE_DIR, name + ".py")
    if not os.path.exists(path):
        path = os.path.join(LIVE_DIR, os.path.basename(name).replace(".", os.sep) + ".py")
    module_name = os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class _Result(unittest.TextTestResult):
    def __init__(self):
        super().__init__(stream=open(os.devnull, "w"), descriptions=False, verbosity=0)
        self.records = []
        self._started = {}

    def _record(self, test, outcome):
        try:
            test_id = test.id()
        except Exception:  # module/class fixture outcomes have no id()
            test_id = str(test)
        seconds = max(0.0, time.monotonic() - self._started.get(test_id, time.monotonic()))
        self.records.append({"id": test_id, "outcome": outcome, "seconds": round(seconds, 3)})
        print("%s %s %.1fs" % (outcome.upper(), test_id, seconds), flush=True)

    def startTest(self, test):
        super().startTest(test)
        try:
            self._started[test.id()] = time.monotonic()
        except Exception:
            pass

    def addSuccess(self, test):
        super().addSuccess(test)
        self._record(test, "pass")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._record(test, "fail")

    def addError(self, test, err):
        super().addError(test, err)
        self._record(test, "error")

    def addSkip(self, test, reason):
        reason = _lab.redact(reason)
        super().addSkip(test, reason)
        self._record(test, "skip")
        self.records[-1]["reason"] = reason
        print("  reason: %s" % reason, flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out")
    parser.add_argument("modules", nargs="+")
    args = parser.parse_args(argv)
    # A throwaway lab home before any suite runs; suites share it through
    # _lab.make_home. HOME itself stays the real one until make_home switches
    # it, so _lab can tell the two apart.
    if not os.environ.get("MYFLAMES_LIVE_HOME"):
        os.environ["MYFLAMES_LIVE_HOME"] = tempfile.mkdtemp(prefix="mf-live-base-")
    os.environ.setdefault("MYFLAMES_SECRET_BACKEND", "vault")
    os.environ["MYFLAMES_LIVE_LABS"] = "1"

    loader = unittest.TestLoader()
    modules = {}
    overall = True
    for name in args.modules:
        started = time.monotonic()
        module = load_suite_module(name)
        suite = loader.loadTestsFromModule(module)
        result = _Result()
        suite.run(result)
        ok = result.wasSuccessful()
        overall = overall and ok
        modules[name] = {"ok": ok, "seconds": round(time.monotonic() - started, 3),
                         "tests": result.records}
        for test, err in result.failures + result.errors:
            text = _lab.redact(err)
            print("---- %s ----\n%s" % (test.id(), text[-3000:]), file=sys.stderr)
    summary = {"ok": overall, "modules": modules}
    line = "SUMMARY " + json.dumps(summary, sort_keys=True)
    print(line, flush=True)
    if args.json_out:
        temporary = args.json_out + ".part"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, args.json_out)
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
