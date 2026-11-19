"""Pure normalization checks; these never connect to or start a live lab."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location("live_expected_normalizer", str(
    Path(__file__).parent / "live" / "_expected.py"))
expected = importlib.util.module_from_spec(spec)
spec.loader.exec_module(expected)


class LiveExpectedTests(unittest.TestCase):
    def test_pool_counters_are_volatile_but_pool_configuration_is_compared(self):
        counters = {"conn_free": 1, "conn_ok": 2, "conn_used": 3, "max_conn_used": 4,
                    "conn_err": 5, "queries": 6, "bytes_sent": 7, "bytes_recv": 8,
                    "latency_us": 9}
        report = {"schema_version": "proxysql-1.0",
                  "hostgroups": [dict(counters, hostgroup_id=10, status="ONLINE")],
                  "servers": [{"pool": dict(counters, status="ONLINE"),
                               "settings": {"max_connections": 200, "weight": 100}}]}
        changed = copy.deepcopy(report)
        for row in (changed["hostgroups"][0], changed["servers"][0]["pool"]):
            row.update({key: value + 100 for key, value in counters.items()})
        normalized = expected.normalize(report)
        self.assertEqual(normalized, expected.normalize(changed))
        for row in (normalized["hostgroups"][0], normalized["servers"][0]["pool"]):
            for key in counters:
                self.assertEqual(row[key], "<volatile>", key)
            self.assertEqual(row["status"], "ONLINE")
        self.assertEqual(normalized["servers"][0]["settings"], {"max_connections": 200, "weight": 100})
        self.assertEqual(report["servers"][0]["pool"], dict(counters, status="ONLINE"))
        changed["servers"][0]["settings"]["max_connections"] = 300
        self.assertNotEqual(normalized, expected.normalize(changed))

    def test_every_saved_report_is_already_normalized(self):
        paths = sorted((Path(__file__).parent / "live" / "expected").glob("*.json"))
        self.assertTrue(paths, "No expected reports were checked")
        for path in paths:
            with self.subTest(report=path.name):
                report = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(report, expected.normalize(report))


if __name__ == "__main__":
    unittest.main()
