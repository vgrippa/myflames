"""Regression coverage for malformed reports, unit conversion and CLI errors."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

from myflames.output_bargraph import render_bargraph
from myflames.output_html_report import _sanitize_for_jsonld, render_html_report
from myflames.parser import parse_explain
from myflames.render import render_explain

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAN = json.dumps({
    "operation": "Filter: (t.id > 0)",
    "actual_last_row_ms": 0.5, "actual_rows": 1, "actual_loops": 1,
    "inputs": [{"operation": "Table scan on t", "actual_last_row_ms": 0.2,
                "actual_rows": 1, "actual_loops": 1}],
})


class TestReportRegressions(unittest.TestCase):
    def test_jsonld_roundtrips_html_delimiters(self):
        payload = {"query": "SELECT '<!-- --> </script><script>alert(1)</script>'"}
        encoded = _sanitize_for_jsonld(payload)
        self.assertEqual(json.loads(encoded), payload)
        self.assertNotIn("<", encoded)
        self.assertNotIn(">", encoded)

    def test_bargraph_truncates_before_escaping(self):
        root = parse_explain(PLAN)
        for char in ['&', '<', '"', "'", '\n']:
            with self.subTest(char=char):
                message = 'x' * 399 + char + 'tail'
                svg = render_bargraph(root, analysis={"node_highlights": [
                    {"short_label": root["short_label"], "message": message}]})
                doc = ET.fromstring(svg)
                annotations = [n.get('data-analysis-msg') for n in doc.iter()
                               if n.get('data-analysis-msg')]
                self.assertEqual(annotations, [message[:400]])

    def test_bargraph_microseconds_preserve_tree_and_scale_totals(self):
        root = parse_explain(PLAN)
        before = copy.deepcopy(root)
        svg = render_bargraph(root, unit_display='µs', total_time=root['total_time'])
        self.assertEqual(root, before)
        doc = ET.fromstring(svg)
        tooltip = next(n.get('data-info') for n in doc.iter()
                       if (n.get('data-info') or '').startswith('Filter:'))
        self.assertIn('Self: 300 µs', tooltip)
        self.assertIn('Total: 500 µs', tooltip)

    def test_microsecond_bargraph_matches_html_and_programmatic_paths(self):
        for output in [render_explain(PLAN, 'bargraph'),
                       render_html_report(PLAN, view_type='bargraph')]:
            with self.subTest(html=output.startswith('<!DOCTYPE')):
                self.assertIn('Self: 300 µs', output)
                self.assertIn('Total: 500 µs', output)


class TestCliInputRegressions(unittest.TestCase):
    def run_cli(self, *args, **kwargs):
        return subprocess.run([sys.executable, '-m', 'myflames'] + list(args),
                              cwd=REPO, input=kwargs.get('stdin'),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True)

    def assert_bad_input(self, result):
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertNotIn('Traceback', result.stderr)

    def test_render_missing_file(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assert_bad_input(self.run_cli(os.path.join(folder, 'missing.json')))

    def test_html_invalid_input_does_not_write_report(self):
        with tempfile.TemporaryDirectory() as folder:
            target = os.path.join(folder, 'report.html')
            self.assert_bad_input(self.run_cli('-o', target, '-', stdin='{bad json'))
            self.assertFalse(os.path.exists(target))

    def test_compare_missing_and_malformed_files_all_formats(self):
        with tempfile.TemporaryDirectory() as folder:
            good = os.path.join(folder, 'good.json')
            bad = os.path.join(folder, 'bad.json')
            missing = os.path.join(folder, 'missing.json')
            with open(good, 'w') as f:
                f.write(PLAN)
            with open(bad, 'w') as f:
                f.write('{bad json')
            for cmd in ['compare', 'diff']:
                for flags in [[], ['--json'], ['--digest']]:
                    for invalid in [bad, missing]:
                        for pair in [(good, invalid), (invalid, good)]:
                            with self.subTest(cmd=cmd, flags=flags, pair=pair):
                                self.assert_bad_input(self.run_cli(cmd, *pair, *flags))

    def test_unwritable_output_is_usage_error(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assert_bad_input(self.run_cli('digest', '-', '-o', folder, stdin=PLAN))

    def test_cli_microseconds(self):
        result = self.run_cli('--type', 'bargraph', '-', stdin=PLAN)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Self: 300 µs', result.stdout)
        self.assertIn('Total: 500 µs', result.stdout)


class TestCompareRegressions(unittest.TestCase):
    def test_repeated_operators_remain_in_html_and_sidecar(self):
        from myflames.output_compare import render_compare
        from myflames.output_compare_sidecar import build_compare_sidecar
        scan = {"operation": "Table scan on t", "table_name": "t", "actual_last_row_ms": 1,
                "actual_rows": 1, "actual_loops": 1}
        plan = {"operation": "Nested loop inner join", "actual_last_row_ms": 5,
                "actual_rows": 1, "actual_loops": 1,
                "inputs": [dict(scan), dict(scan)]}
        after = copy.deepcopy(plan)
        after['inputs'][1]['actual_last_row_ms'] = 2
        after['actual_last_row_ms'] = 6
        before_json, after_json = json.dumps(plan), json.dumps(after)
        sidecar = build_compare_sidecar(before_json, after_json)
        self.assertEqual(len(sidecar['deltas']), 3)
        self.assertEqual(sidecar['summary']['regressions'], 1)
        repeated = [d for d in sidecar['deltas'] if d['short_label'] == 'Table scan [t]']
        self.assertEqual(len(repeated), 2)
        self.assertEqual([d['self_time_ms']['after'] for d in repeated], [1, 2])
        html = render_compare(before_json, after_json)
        self.assertEqual(html.count('<td>Table scan [t] '), 2)

    def test_removed_duplicate_is_kept(self):
        from myflames.output_compare import _match_nodes
        before = [{'short_label': 'Scan', 'id': i} for i in range(2)]
        after = [dict(before[0])]
        rows = _match_nodes(before, after)
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[1][2])
        self.assertEqual(rows[1][1]['id'], 1)


if __name__ == '__main__':
    unittest.main()
