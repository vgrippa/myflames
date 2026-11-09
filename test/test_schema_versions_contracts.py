"""The published JSON Schemas of schema versions match what the code emits.

docs/schemas/schema-snapshot-1.0.json, schema-compare-1.0.json and
schema-version-list-1.0.json are contracts for external tools and agents:
every recorded snapshot (MySQL 8.4 and MariaDB 11.4, test/fixtures/
schema_versions), comparisons of them (with and without differences, with an
account that could not read routines) and the store's list items must
validate, and an unknown field must not.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _schema_replay as R  # noqa: E402
from test_chart_data import validate  # noqa: E402
from myflames import schema_versions  # noqa: E402
from myflames.workspace_store import WorkspaceStore  # noqa: E402

DOCS = Path(__file__).resolve().parent.parent / 'docs' / 'schemas'
SNAPSHOT = json.loads((DOCS / 'schema-snapshot-1.0.json').read_text(encoding='utf-8'))
COMPARE = json.loads((DOCS / 'schema-compare-1.0.json').read_text(encoding='utf-8'))
LISTING = json.loads((DOCS / 'schema-version-list-1.0.json').read_text(encoding='utf-8'))


class SchemaVersionContracts(unittest.TestCase):
    def test_every_recorded_snapshot_validates(self):
        for server in R.SERVERS:
            for which in R.WHICH:
                with self.subTest(server=server, which=which):
                    validate(R.snapshot(server, which), SNAPSHOT)

    def test_comparisons_validate_with_and_without_differences(self):
        for server in R.SERVERS:
            target, source, limited = (R.snapshot(server, w) for w in R.WHICH)
            for left, right, options in ((target, source, {}), (target, target, {}), (source, limited, {}),
                                         (source, target, {'ignore_definers': True})):
                with self.subTest(server=server, left=left['database'], right=right['database']):
                    report = schema_versions.compare(right, left, {'kind': 'live'}, {'kind': 'version', 'id': 'x', 'number': 1,
                                                                   'name': ''}, **options)
                    validate(report, COMPARE)
                    report['markdown'] = schema_versions.format_markdown(report)
                    validate(report, COMPARE)

    def test_unknown_fields_do_not_validate(self):
        snapshot = R.snapshot('mysql-8.4', 'before')
        with self.assertRaises(AssertionError):
            validate(dict(snapshot, extra=1), SNAPSHOT)
        report = schema_versions.compare(snapshot, snapshot)
        with self.assertRaises(AssertionError):
            validate(dict(report, extra=1), COMPARE)

    def test_selection_blockers_round_trip_and_validate(self):
        for server in R.SERVERS:
            with self.subTest(server=server):
                source, target = R.retained_child_pair(server)
                report = schema_versions.compare(source, target)
                reread = json.loads(json.dumps(report))
                validate(reread, COMPARE)
                blocked = next(e for e in reread['objects'] if e['name'] == 'parent2')
                self.assertEqual(blocked['blocked_by'], ['relation:child2'])
                blocked['blocked_by'] = [1]
                with self.assertRaises(AssertionError):
                    validate(reread, COMPARE)

    def test_a_read_through_ssh_validates_everywhere(self):
        # The SSH target is an optional, additive part of every server record.
        through = R.snapshot('mysql-8.4', 'before')
        through['server'].update(ssh_host='bastion', ssh_user='deploy', ssh_port=2222)
        through = schema_versions.validate_snapshot(through)
        validate(through, SNAPSHOT)
        report = schema_versions.compare(R.snapshot('mysql-8.4', 'after'), through, {'kind': 'live'}, {'kind': 'live'})
        self.assertEqual(report['target']['server']['ssh_host'], 'bastion')
        validate(report, COMPARE)
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {'HOME': home, 'USERPROFILE': home, 'MYFLAMES_SECRET_BACKEND': 'vault'}):
            os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
            store = WorkspaceStore()
            store.schema_version_save(through)
            validate(store.schema_version_list(), LISTING)
        bad = dict(through, server=dict(through['server'], ssh_port=0))
        with self.assertRaises(AssertionError):
            validate(bad, SNAPSHOT)

    def test_store_list_items_validate(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {'HOME': home, 'USERPROFILE': home, 'MYFLAMES_SECRET_BACKEND': 'vault'}):
            os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
            store = WorkspaceStore()
            store.schema_version_save(R.snapshot('mysql-8.4', 'before'), 'first', '')
            saved = store.schema_version_save(R.snapshot('mysql-8.4', 'before'), '', 'again')
            self.assertEqual(saved['same_as']['number'], 1)
            validate(store.schema_version_list(), LISTING)
            validate({'schema_version': 'schema-version-list-1.0', 'items': [saved]}, LISTING)


if __name__ == '__main__':
    unittest.main()
