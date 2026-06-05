"""
Unit tests for :mod:`myflames.mcp_server`.

Only the *tool logic* is tested here (it's pure stdlib). The MCP transport
wrapper imports the optional ``mcp`` dependency lazily; we assert that
``build_server`` either succeeds (extra installed) or raises a clear ImportError
(extra absent) — never an obscure failure.
"""
import os
import sys
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(TEST_DIR))

from myflames import mcp_server as mcp
from myflames.parser import OPTIMIZER_SWITCH_EXPLANATIONS

FIXTURE_DIR = os.path.join(TEST_DIR, "fixtures")
PLAN = os.path.join(FIXTURE_DIR, "explain-001-table-scan-users-no-filter.json")
PLAN2 = os.path.join(FIXTURE_DIR, "explain-008-index-scan-users-by-country.json")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class TestToolLogic(unittest.TestCase):

    def test_analyze_plan_tool_returns_valid_sidecar(self):
        from myflames.output_sidecar import validate_sidecar
        payload = mcp.analyze_plan_tool(_read(PLAN))
        self.assertTrue(validate_sidecar(payload))
        self.assertIn("plan_summary", payload)
        self.assertEqual(payload["source"]["engine"], "mysql")

    def test_digest_plan_tool_is_compact_text(self):
        digest = mcp.digest_plan_tool(_read(PLAN))
        self.assertIsInstance(digest, str)
        self.assertIn("myflames digest", digest)

    def test_compare_plans_tool_returns_delta_summary(self):
        sidecar = mcp.compare_plans_tool(_read(PLAN), _read(PLAN2))
        self.assertEqual(sidecar["schema_version"], "compare-1.0")
        self.assertIn("summary", sidecar)

    def test_explain_optimizer_switch_known(self):
        result = mcp.explain_optimizer_switch_tool("hash_join")
        self.assertTrue(result["known"])
        self.assertTrue(result["explanation"])

    def test_explain_optimizer_switch_case_insensitive(self):
        self.assertTrue(mcp.explain_optimizer_switch_tool("HASH_JOIN")["known"])

    def test_explain_optimizer_switch_unknown_lists_available(self):
        result = mcp.explain_optimizer_switch_tool("definitely_not_a_switch")
        self.assertFalse(result["known"])
        self.assertEqual(set(result["available"]), set(OPTIMIZER_SWITCH_EXPLANATIONS))


class TestServerWrapper(unittest.TestCase):

    def test_build_server_succeeds_or_raises_clean_importerror(self):
        try:
            server = mcp.build_server()
        except ImportError as e:
            # Extra not installed: the message must point at the fix.
            self.assertIn("myflames[mcp]", str(e))
        else:
            # Extra installed: we got a server object.
            self.assertIsNotNone(server)


if __name__ == "__main__":
    unittest.main()
