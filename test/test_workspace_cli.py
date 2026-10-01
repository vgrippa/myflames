"""CLI contracts for shared capture and interactive exploration."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from myflames import workspace_cli
from myflames.parser import parse_explain

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, 'test', 'fixtures', 'explain-044-join-3t-products-category-reviews.json')


def run_cli(*args, **kwargs):
    return subprocess.run([sys.executable, '-m', 'myflames'] + list(args), cwd=ROOT,
                          input=kwargs.get('stdin'), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True)


class TestCaptureCLI(unittest.TestCase):
    def setUp(self):
        self.out, self.err = io.StringIO(), io.StringIO()
        self.patch = mock.patch.object(workspace_cli, 'LiveJob')
        self.job_class = self.patch.start()
        self.addCleanup(self.patch.stop)
        self.job = self.job_class.return_value
        self.job.state = 'complete'
        self.job.result = {'raw': '{"operation":"Rows fetched before execution"}', 'mode': 'explain', 'runs': []}

    def call(self, *args):
        with contextlib.redirect_stdout(self.out), contextlib.redirect_stderr(self.err):
            workspace_cli.cmd_capture(list(args))

    def test_default_is_estimate_and_stdout_is_only_versioned_json(self):
        self.call('-e', 'SELECT 1')
        result = json.loads(self.out.getvalue())
        self.assertEqual(result['schema_version'], 'capture-1.0')
        self.assertEqual(self.err.getvalue(), '')
        payload = self.job_class.call_args[0][0]
        self.assertEqual(payload['mode'], 'explain')
        self.assertEqual(payload['connection']['host'], '127.0.0.1')
        self.assertEqual(payload['repeat'], 1)
        self.job.run.assert_called_once_with()

    def test_flags_reach_shared_engine_and_password_is_prompted(self):
        with mock.patch.object(workspace_cli.getpass, 'getpass', return_value='secret') as prompt:
            self.call('-h', 'db.example', '-P', '3307', '-u', 'reader', '-p', '-D', 'shop',
                      '-e', 'SELECT * FROM t WHERE id=:id', '--mode', 'analyze', '--repeat', '3',
                      '--timeout', '12', '--trace', '--parameters', '{"id": 42}', '--conditions', 'warm cache',
                      '--ssl-mode', 'VERIFY_IDENTITY', '--ssl-ca', '/tmp/ca.pem')
        prompt.assert_called_once()
        payload = self.job_class.call_args[0][0]
        self.assertEqual(payload['parameters'], {'id': 42})
        self.assertEqual(payload['connection']['password'], 'secret')
        self.assertEqual(payload['connection']['port'], 3307)
        self.assertEqual((payload['mode'], payload['repeat'], payload['timeout'], payload['trace']), ('analyze', 3, 12, True))
        self.assertEqual(payload['conditions'], 'warm cache')
        self.assertNotIn('secret', self.out.getvalue() + self.err.getvalue())

    def test_output_file_and_inline_password_compatibility(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, 'capture.json')
            self.call('-e', 'SELECT 1', '-psecret', '-o', output)
            with open(output) as handle:
                self.assertEqual(json.load(handle)['schema_version'], 'capture-1.0')
            self.assertEqual(self.out.getvalue(), '')
            self.assertIn('Written to', self.err.getvalue())
        self.assertEqual(self.job_class.call_args[0][0]['connection']['password'], 'secret')

    def test_failed_capture_is_exit_two_without_secret_or_traceback(self):
        self.job.state = 'failed'
        self.job.error = 'Failed for password secret\nTry another connection'
        with self.assertRaises(SystemExit) as error:
            self.call('-e', 'SELECT 1', '-psecret')
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(self.out.getvalue(), '')
        self.assertNotIn('secret', self.err.getvalue())
        self.assertNotIn('Traceback', self.err.getvalue())
        self.assertIn('[redacted]', self.err.getvalue())

    def test_keyboard_interrupt_cancels_owned_job(self):
        self.job.run.side_effect = KeyboardInterrupt
        with self.assertRaises(SystemExit) as error:
            self.call('-e', 'SELECT 1', '--mode', 'analyze')
        self.job.cancel.assert_called_once_with()
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(self.out.getvalue(), '')
        self.assertIn('cancelled', self.err.getvalue())

    def test_invalid_input_is_rejected_before_a_connection(self):
        for args in ([], ['-e', 'SELECT 1', '--repeat', '11'], ['-e', 'SELECT 1', '--timeout', '0'],
                     ['-e', 'SELECT 1', '--parameters', '[]'], ['-e', 'SELECT 1', '--parameters', '{broken'],
                     ['-e', 'SELECT 1', '--parameters', '{"x":NaN}'], ['-e', 'SELECT 1', '--parameters', '{"x":1e309}'], ['-e', 'SELECT 1', '-P', '99999']):
            with self.subTest(args=args), self.assertRaises(SystemExit) as error:
                self.call(*args)
            self.assertEqual(error.exception.code, 2)
        self.job_class.assert_not_called()

    def test_engine_validation_failure_is_one_line(self):
        self.job_class.side_effect = ValueError('Use a read-only SELECT.')
        with self.assertRaises(SystemExit) as error:
            self.call('-e', 'DELETE FROM t')
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(self.err.getvalue(), 'myflames: Use a read-only SELECT.\n')


class TestExploreCLI(unittest.TestCase):
    def test_real_fixture_html_and_selected_id(self):
        with open(FIXTURE) as handle:
            node_id = parse_explain(handle.read())['node_id']
        result = run_cli('explore', FIXTURE, '--selected', node_id, '--focus', node_id, '--metric', 'rows')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('<!doctype html>', result.stdout.lower())
        self.assertIn(node_id, result.stdout)
        self.assertIn('rows per loop', result.stdout)
        self.assertEqual(result.stderr, '')

    def test_repeated_collapse_flags_hide_selected_branches(self):
        with open(FIXTURE) as handle:
            root = parse_explain(handle.read())
        child_id = root['children'][0]['node_id']
        result = run_cli('explore', FIXTURE, '--collapse', root['node_id'], '--collapse', child_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('hidden)', result.stdout)
        self.assertIn('Collapsed descendants are excluded', result.stdout)
        self.assertNotIn('data-node-id="' + child_id + '"', result.stdout)

    def test_stdin_and_output_file(self):
        with open(FIXTURE) as handle:
            raw = handle.read()
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, 'exploration.html')
            result = run_cli('explore', '-', '--type', 'tree', '-o', output, stdin=raw)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, '')
            with open(output) as handle:
                self.assertIn('myflames-select', handle.read())

    def test_bad_input_unknown_operator_and_missing_file_exit_two(self):
        for args in (['explore', '/does-not-exist.json'], ['explore', FIXTURE, '--focus', 'n:unknown'],
                     ['explore', FIXTURE, '--selected', 'n:unknown'], ['explore', FIXTURE, '--collapse', 'n:unknown'],
                     ['explore', FIXTURE, '--metric', 'nonsense']):
            with self.subTest(args=args):
                result = run_cli(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertNotIn('Traceback', result.stderr)
        result = run_cli('explore', '-', stdin='{broken')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')

    def test_help_discovers_both_commands_and_host_keeps_short_flag(self):
        result = run_cli('--help')
        self.assertEqual(result.returncode, 0)
        self.assertIn('capture', result.stdout)
        self.assertIn('explore', result.stdout)
        result = run_cli('capture', '--help')
        self.assertEqual(result.returncode, 0)
        self.assertIn('-h HOST', result.stdout)
        self.assertIn('--mode analyze executes', result.stdout)


if __name__ == '__main__':
    unittest.main()
