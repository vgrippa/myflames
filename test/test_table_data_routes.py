"""API orchestration guards beyond the table planner's value-level tests."""
import types
from unittest import mock

from myflames import dbops, table_data as td
import test_table_data as fixtures


import unittest
class TableDataRoutes(unittest.TestCase):
    setUp = fixtures.TableDataTests.setUp

    def fixture(self):
        registry = td.TableData()
        self.addCleanup(registry.close)
        item = fixtures.snapshot()
        options = {'host': '127.0.0.1', 'port': 3306, 'user': 'operator'}
        item.identity = dbops.connection_identity(options, item.safety)
        registry.add(item)
        workspace = types.SimpleNamespace(table_data=registry, store=None)
        facts = [options, item.identity, dict(item.safety), 'Test server']
        payload = {'connection': options, 'snapshot_id': item.id}
        return item, workspace, facts, payload

    def test_read_only_drift_refuses_before_password_lookup(self):
        item, workspace, facts, payload = self.fixture()
        facts[2]['read_only'] = True
        facts[1] = dbops.connection_identity(facts[0], facts[2])
        fill = mock.Mock()
        with mock.patch.object(td, '_facts', return_value=facts):
            with self.assertRaisesRegex(ValueError, 'write policy changed'):
                td.response('/api/table-data/prepare', payload, workspace, fill)
            self.assertEqual(td.response('/api/table-data/status', payload, workspace, fill)['status'], 'ready')
        fill.assert_not_called()

    def test_foreign_endpoint_cannot_read_or_close_snapshot(self):
        item, workspace, facts, payload = self.fixture()
        facts[1] = dbops.connection_identity(dict(facts[0], host='other.example'), facts[2])
        with mock.patch.object(td, '_facts', return_value=facts):
            for action in ('status', 'close', 'cancel', 'prepare', 'apply'):
                with self.assertRaisesRegex(ValueError, 'another connection'):
                    td.response('/api/table-data/' + action, payload, workspace, mock.Mock())
        self.assertFalse(item.cancel.is_set())
        item.session.end.assert_not_called()

    def test_policy_change_during_password_lookup_refuses_thread_start(self):
        item, workspace, facts, payload = self.fixture()
        item.plan = td.build_plan(item, [{'kind': 'delete', 'row_id': item.row_ids[0]}])
        payload.update(confirmed=True, review_token=item.tokens.issue('table_data', item.identity, item.plan))
        def fill(value):
            facts[2]['read_only'] = True
            facts[1] = dbops.connection_identity(facts[0], facts[2])
            return value
        with mock.patch.object(td, '_facts', side_effect=lambda *args: facts), mock.patch.object(td, '_audit', return_value=fixtures.Audit()), \
                mock.patch.object(td.threading, 'Thread') as thread:
            with self.assertRaisesRegex(ValueError, 'write policy changed'):
                td.response('/api/table-data/apply', payload, workspace, fill)
            thread.assert_not_called()

    def test_confirmation_and_token_cannot_be_replayed(self):
        item, workspace, facts, payload = self.fixture()
        item.plan = td.build_plan(item, [{'kind': 'delete', 'row_id': item.row_ids[0]}])
        payload.update(confirmed=True, review_token=item.tokens.issue('table_data', item.identity, item.plan))
        with mock.patch.object(td, '_facts', return_value=facts), mock.patch.object(td, '_audit', return_value=fixtures.Audit()), \
                mock.patch.object(td.threading, 'Thread') as thread:
            result = td.response('/api/table-data/apply', payload, workspace, lambda value: value)
            self.assertEqual(result['status'], 'running')
            thread.return_value.start.assert_called_once()
            with self.assertRaises(ValueError):
                td.response('/api/table-data/apply', payload, workspace, lambda value: value)
            thread.return_value.start.assert_called_once()
            item.state = 'ready'
            with self.assertRaises(ValueError):
                td.response('/api/table-data/apply', payload, workspace, lambda value: value)
            thread.return_value.start.assert_called_once()

    def test_decline_is_audited_and_has_no_password_lookup_or_thread(self):
        item, workspace, facts, payload = self.fixture()
        item.plan = td.build_plan(item, [{'kind': 'delete', 'row_id': item.row_ids[0]}])
        payload['review_token'] = item.tokens.issue('table_data', item.identity, item.plan)
        audit, fill = fixtures.Audit(), mock.Mock()
        with mock.patch.object(td, '_facts', return_value=facts), mock.patch.object(td, '_audit', return_value=audit), \
                mock.patch.object(td.threading, 'Thread') as thread:
            result = td.response('/api/table-data/decline', payload, workspace, fill)
        self.assertEqual(result['status'], 'ready')
        self.assertIsNone(item.plan)
        self.assertEqual(audit.events[0][0], 'declined')
        fill.assert_not_called(); thread.assert_not_called()
