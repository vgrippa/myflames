"""Workspace payload size regressions without changing CLI sidecar data."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from myflames.digest import build_digest
from myflames.output_sidecar import build_sidecar
from myflames.parser import analyze_plan, parse_explain
from myflames.teach_hooks import build_teach_hooks
from myflames.ui import api_response


TEST = Path(__file__).resolve().parent


def wide_plan(count):
    """A flat union keeps the test about wire size, not recursion depth."""
    return json.dumps({'operation': 'Union all', 'actual_last_row_ms': count,
                       'actual_rows': 100 * count, 'actual_loops': 1,
                       'inputs': [
                           {'operation': 'Table scan on t%d' % i, 'table_name': 't%d' % i,
                            'access_type': 'table', 'actual_last_row_ms': 1,
                            'actual_rows': 100, 'actual_loops': 1}
                           for i in range(count)]})


class WorkspacePayloadTests(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        env = mock.patch.dict(os.environ, {'HOME': home.name, 'USERPROFILE': home.name,
                                          'MYFLAMES_SECRET_BACKEND': 'vault'})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)

    def test_workspace_hooks_omit_sql_but_keep_all_other_fields_and_root_query(self):
        query = 'SELECT 1 /* keep this SQL exactly */'
        for filename in ('mysql-explain-hash-join.json', 'mariadb-explain-block-nl-join.json'):
            with self.subTest(engine=filename):
                raw = (TEST / filename).read_text(encoding="utf-8")
                root = parse_explain(raw)
                result = api_response('/api/analyze', {'plan': json.dumps(dict(json.loads(raw), query=query))})
                self.assertEqual(result['query'], query)
                self.assertEqual(result['analysis']['query']['raw'], query)
                expected = build_teach_hooks(root)
                self.assertTrue(expected)
                self.assertEqual(result['teach_hooks'], expected)
                self.assertEqual(result['analysis']['teach_hooks'], expected)
                for hook in result['teach_hooks'] + result['analysis']['teach_hooks']:
                    self.assertNotIn('query_sql', hook)
                # CLI and digest retain their full sidecar contract.
                sidecar = build_sidecar(root, analyze_plan(root), query_raw=query)
                self.assertTrue(all(h['query_sql'] == query for h in sidecar['teach_hooks']))
                before = copy.deepcopy(sidecar)
                self.assertTrue(build_digest(sidecar))
                self.assertEqual(sidecar, before)

    def test_workspace_sidecar_keeps_captured_lesson_controls(self):
        raw = wide_plan(2)
        root = parse_explain(raw)
        variables = {'join_buffer_size': '1048576'}
        stats = {'t0': {'table_rows': 10, 'data_length': 12800}}

        def enrich(analysis, **kwargs):
            analysis['collected_variables'] = variables
            analysis['collected_stats'] = stats

        with mock.patch('myflames.live.normalize_capture', return_value={'query': 'SELECT 2'}), \
                mock.patch('myflames.advisor.advise', side_effect=enrich):
            result = api_response('/api/analyze', {'plan': raw})
        expected = build_teach_hooks(root, variables=variables, stats=stats)
        self.assertEqual(result['analysis']['teach_hooks'], expected)
        self.assertEqual(expected[0]['controls']['row_size'], 1280)
        self.assertEqual(result['query'], 'SELECT 2')

    def test_long_sql_response_is_not_multiplied_by_801_operators(self):
        query = 'SELECT 1 /*' + 'x' * (71769 - len('SELECT 1 /**/')) + '*/'
        self.assertEqual(len(query), 71769)
        result = api_response('/api/analyze', {'plan': json.dumps(dict(json.loads(wide_plan(800)), query=query))})
        self.assertEqual(len(result['operators']), 801)
        self.assertEqual(len(result['teach_hooks']), 800)
        serialized = json.dumps(result, ensure_ascii=False, separators=(',', ':'))
        # The root query and the sidecar's raw query preserve the same SQL;
        # neither of the two hook arrays repeats it for every operator.
        self.assertEqual(serialized.count(query), 2)
        self.assertLess(len(serialized.encode('utf-8')), 8 * 1024 * 1024)


if __name__ == '__main__':
    unittest.main()
