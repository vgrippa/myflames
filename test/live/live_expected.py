"""Live suite: read-only lab reports match their recorded expected JSON.

For each recorded report under test/live/expected/ (written by
`scripts/live-tests.sh --record`), reads the same report from the lab,
normalizes both with _expected.normalize (UUIDs, timestamps, ports, durations,
connection ids and counters masked) and fails on drift; a version without a
recorded file skips. This is mtr's .result comparison, for JSON.

Only the labs of the run's selection are compared (MYFLAMES_LIVE_LABS_ONLY,
set by scripts/live-tests.sh): a run that selected galera alone skips the ic
and proxysql cases instead of checking labs the run never brought up.

Run: scripts/live-tests.sh all   (recorded once with --record)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _expected import compare  # noqa: E402
from _lab import check_lab, lane_labs, make_home, require_live  # noqa: E402

DEFAULT_LABS = ["ic", "galera", "proxysql"]


def setUpModule():
    require_live()


class ExpectedReportsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.labs = lane_labs() or DEFAULT_LABS
        check_lab(*cls.labs)
        make_home("expected")
        cls.outcomes = compare(cls.labs)

    def expected_outcome(self, name, lab):
        # Not _outcome: unittest.TestCase keeps its own _outcome instance
        # attribute, which would shadow this helper.
        if lab not in self.labs:
            self.skipTest("lab %s not selected for this run" % lab)
        for report_name, outcome in self.outcomes:
            if report_name == name:
                return outcome
        raise AssertionError("report %s was not compared" % name)

    def test_cluster_ic(self):
        outcome = self.expected_outcome("cluster-ic", "ic")
        if outcome is None:
            self.skipTest("no expected file for this server version")
        self.assertTrue(outcome is True, "drift written to %s" % outcome)

    def test_cluster_galera(self):
        outcome = self.expected_outcome("cluster-galera", "galera")
        if outcome is None:
            self.skipTest("no expected file for this server version")
        self.assertTrue(outcome is True, "drift written to %s" % outcome)

    def test_replication_ic(self):
        outcome = self.expected_outcome("replication-ic", "ic")
        if outcome is None:
            self.skipTest("no expected file for this server version")
        self.assertTrue(outcome is True, "drift written to %s" % outcome)

    def test_proxysql(self):
        outcome = self.expected_outcome("proxysql", "proxysql")
        if outcome is None:
            self.skipTest("no expected file for this server version")
        self.assertTrue(outcome is True, "drift written to %s" % outcome)


if __name__ == "__main__":
    unittest.main()
