"""Live suite: read-only lab reports match their recorded expected JSON.

For each recorded report under test/live/expected/ (written by
`scripts/live-tests.sh --record`), reads the same report from the lab,
normalizes both with _expected.normalize (UUIDs, timestamps, ports, durations,
connection ids and counters masked) and fails on drift; a version without a
recorded file skips. This is mtr's .result comparison, for JSON.

Run: scripts/live-tests.sh all   (recorded once with --record)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _expected import compare  # noqa: E402
from _lab import check_lab, make_home, require_live  # noqa: E402


def setUpModule():
    require_live()


class ExpectedReportsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        check_lab("ic", "galera", "proxysql")
        make_home("expected")
        cls.outcomes = compare(["ic", "galera", "proxysql"])

    def expected_outcome(self, name):
        # Not _outcome: unittest.TestCase keeps its own _outcome instance
        # attribute, which would shadow this helper.
        for report_name, outcome in self.outcomes:
            if report_name == name:
                return outcome
        raise AssertionError("report %s was not compared" % name)

    def test_cluster_ic(self):
        outcome = self.expected_outcome("cluster-ic")
        if outcome is None:
            self.skipTest("no expected file for this server version")
        self.assertTrue(outcome is True, "drift written to %s" % outcome)

    def test_cluster_galera(self):
        outcome = self.expected_outcome("cluster-galera")
        if outcome is None:
            self.skipTest("no expected file for this server version")
        self.assertTrue(outcome is True, "drift written to %s" % outcome)

    def test_replication_ic(self):
        outcome = self.expected_outcome("replication-ic")
        if outcome is None:
            self.skipTest("no expected file for this server version")
        self.assertTrue(outcome is True, "drift written to %s" % outcome)

    def test_proxysql(self):
        outcome = self.expected_outcome("proxysql")
        if outcome is None:
            self.skipTest("no expected file for this server version")
        self.assertTrue(outcome is True, "drift written to %s" % outcome)


if __name__ == "__main__":
    unittest.main()
