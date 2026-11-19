"""Schema versions hands generated sync SQL to the guarded editor apply path."""
import unittest

import _schema_replay as schema_replay
import test_ui_group_consent as consent
from myflames import schema_versions


class SchemaScriptConsentTests(unittest.TestCase):
    def check_script(self, mode):
        case = consent.RemainingWriteConsentTests(methodName='runTest')
        case.setUp()
        self.addCleanup(case.doCleanups)
        # A generated table creation takes the same route as Open in Query
        # editor, rather than inventing a Schema versions apply endpoint.
        source = schema_replay.snapshot('mysql-8.4', 'after')
        target = schema_replay.snapshot('mysql-8.4', 'before')
        report = schema_versions.compare(source, target)
        sql = next(item['sql'] for item in report['sync']['statements']
                   if item['sql'].startswith('CREATE TABLE'))
        case.editor_apply_race(sql, mode)

    def test_sync_member_removed_after_review_sends_nothing(self):
        self.check_script('removed')

    def test_sync_member_read_only_after_review_sends_nothing(self):
        self.check_script('read_only')
