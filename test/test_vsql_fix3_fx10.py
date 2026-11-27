"""FX10 abuse cases for literal hiding in shared teach-hook notes."""
import copy
import json
import unittest

from myflames import plan_privacy, ui
from myflames.parser import parse_explain


class SharedTeachHookNotes(unittest.TestCase):
    def assert_private_export(self, source, secrets):
        raw = json.dumps(source)
        analysis = ui.api_response('/api/analyze', {'plan': raw})['analysis']
        before = copy.deepcopy(analysis)
        self.assertTrue(analysis.get('teach_hooks'), 'Exercise a real generated hook')
        notes = json.dumps(analysis['teach_hooks'])
        for secret in secrets:
            self.assertIn(secret, notes)
        hidden = ui.api_response('/api/report', {
            'plan': raw, 'analysis': analysis, 'format': 'json', 'hide_literals': True,
        })['analysis']
        # The CLI uses this same mapper for its sidecar after collection.
        mapped = plan_privacy.hide_analysis_literals(
            analysis, plan_privacy.plan_literal_labels(parse_explain(raw)))
        self.assertEqual(hidden, mapped)
        for secret in secrets:
            self.assertNotIn(secret, json.dumps(hidden))
        self.assertEqual(hidden['plan_summary'], analysis['plan_summary'])
        self.assertEqual(hidden['teach_hooks'][0]['controls'], analysis['teach_hooks'][0]['controls'])
        self.assertEqual(analysis, before, 'Sharing must leave the collected evidence intact')
        return hidden

    def test_sort_arithmetic_constant_is_private_in_hook_note(self):
        for table in ('', 'users'):
            with self.subTest(table=table):
                hidden = self.assert_private_export({
                    'operation': 'Sort: (users.score * 91827303) DESC',
                    'access_type': 'sort', 'table_name': table,
                    'estimated_rows': 42, 'actual_rows': 42,
                    'actual_loops': 1, 'actual_last_row_ms': 12.5,
                }, ('91827303',))
                self.assertIn('users.score * ?', hidden['teach_hooks'][0]['note'])

    def test_literal_after_knn_preview_cut_inside_quote_is_private(self):
        point = "CONCAT('left', '" + 'M' * 160 + "', 'tail')"
        lookup = 'embedding <-> ' + point
        hidden = self.assert_private_export({
            'operation': "Index KNN scan on vectors using idx_embedding (%s), tag = 'SENTSTRZ', limit 5" % lookup,
            'access_type': 'index', 'index_access_type': 'index_knn_scan',
            'table_name': 'vectors', 'index_name': 'idx_embedding',
            'lookup_condition': lookup, 'limit': 5,
            'estimated_rows': 42, 'actual_rows': 5,
            'actual_loops': 1, 'actual_last_row_ms': 12.5,
        }, ('SENTSTRZ', 'M' * 20))
        note = hidden['teach_hooks'][0]['note']
        self.assertIn('embedding <-> ?', note)
        self.assertNotIn('characters)', note)

    def test_unmapped_prose_note_keeps_measurements(self):
        self.assertEqual(plan_privacy.hide_analysis_literals({
            'note': 'Scans 42 rows in 12.5 ms; predicate id = 91827303',
        }), {'note': 'Scans 42 rows in 12.5 ms; predicate id = ?'})


if __name__ == '__main__':
    unittest.main()
