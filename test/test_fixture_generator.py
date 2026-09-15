"""Exercise container ownership and cleanup without starting Docker."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(shutil.which('bash'), 'Fixture generator requires bash')
class TestFixtureGenerator(unittest.TestCase):
    def _run(self, existing):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docker = root / 'docker'
            docker.write_text('#!' + sys.executable + '\n' + '''
import json, os, sys
args = sys.argv[1:]
with open(os.environ['CALL_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\\n')
if args[0] == 'ps':
    if os.environ['EXISTING'] == '1':
        print(os.environ['CONTAINER_NAME'])
elif args[0] == 'exec':
    if '-e' not in args:
        sys.stdin.read()
        sys.exit(42)  # Simulate a failure while seeding schema.
    print('1')
''')
            docker.chmod(0o700)
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'],
                       CALL_LOG=str(root / 'calls'), EXISTING='1' if existing else '0',
                       CONTAINER_NAME='myflames-unit-fixture', MYSQL_IMAGE='mysql:test-version',
                       KEEP_CONTAINER='0', OUTPUT_DIR=str(root / 'plans'))
            script = Path(__file__).resolve().parents[1] / 'scripts/generate-fixtures.sh'
            result = subprocess.run(['bash', str(script)], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    universal_newlines=True, timeout=15)
            calls = [json.loads(line) for line in (root / 'calls').read_text().splitlines()]
            return result, calls

    def test_existing_container_is_never_removed(self):
        result, calls = self._run(True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('already exists', result.stdout)
        self.assertFalse(any(call[0] in ('rm', 'run') for call in calls))

    def test_failure_cleans_up_only_created_container(self):
        result, calls = self._run(False)
        self.assertEqual(result.returncode, 42, result.stderr)
        run = next(call for call in calls if call[0] == 'run')
        self.assertEqual(run[-1], 'mysql:test-version')
        self.assertEqual(calls[-1], ['rm', '-fv', 'myflames-unit-fixture'])
