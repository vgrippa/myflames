"""Opt-in checks for freshly captured MySQL plans.

MYFLAMES_MYSQL_FIXTURES=/tmp/mysql-plans python3 -m unittest \
    discover -s test -p 'test_mysql_live_corpus.py' -v

Generate the directory with scripts/generate-fixtures.sh. No database connection
or Docker command runs during these tests; they validate the captured output.
"""
import json
import os
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

from myflames.parser import parse_explain, flatten_nodes
from myflames.render import render_explain
from myflames.mcp_server import (
    analyze_plan_tool, compare_plans_tool, digest_plan_tool,
)
from myflames.output_sidecar import validate_sidecar


@unittest.skipUnless(os.environ.get('MYFLAMES_MYSQL_FIXTURES'),
                     'Set MYFLAMES_MYSQL_FIXTURES to a fresh MySQL corpus')
class TestMySQLLiveCorpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = Path(os.environ['MYFLAMES_MYSQL_FIXTURES'])
        cls.plans = sorted(directory.glob('explain-*.json'))
        if not cls.plans:
            raise AssertionError('No explain-*.json plans in ' + str(directory))

    def test_parse_and_all_five_views(self):
        for path in self.plans:
            text = path.read_text(encoding='utf-8')
            with self.subTest(plan=path.name, stage='parse'):
                root = parse_explain(text)
                self.assertGreater(len(list(flatten_nodes(root))), 0)
                self.assertGreaterEqual(root['total_time'], 0)
            for view in ('flamegraph', 'bargraph', 'treemap', 'diagram', 'tree'):
                with self.subTest(plan=path.name, view=view):
                    svg = render_explain(text, view)
                    element = ET.fromstring(svg)
                    self.assertTrue(element.tag.endswith('svg'))

    def test_sidecar_digest_and_identity_comparison(self):
        for path in self.plans:
            with self.subTest(plan=path.name):
                text = path.read_text(encoding='utf-8')
                payload = analyze_plan_tool(text)
                validate_sidecar(payload)
                json.dumps(payload, allow_nan=False)
                self.assertTrue(digest_plan_tool(text).strip())
                comparison = compare_plans_tool(text, text)
                self.assertEqual(comparison['summary']['regressions'], 0)
                self.assertEqual(comparison['summary']['improvements'], 0)
