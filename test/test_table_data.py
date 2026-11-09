"""Table grid safeguards, exact values and all-or-nothing reviewed batches."""
import copy
import json
import os
import tempfile
import threading
import types
import unittest
from unittest import mock

from myflames import table_data as td, privileges


def column(name, data_type='varchar', nullable='NO', extra='', charset='utf8mb4'):
    return {'COLUMN_NAME': name, 'DATA_TYPE': data_type, 'COLUMN_TYPE': data_type,
            'IS_NULLABLE': nullable, 'COLUMN_DEFAULT': None, 'EXTRA': extra,
            'CHARACTER_SET_NAME': charset if data_type == 'varchar' else None, 'COLLATION_NAME': None}


def model(engine='mysql'):
    return privileges.parse_grants(["GRANT ALL PRIVILEGES ON *.* TO 'operator'@'%'"], engine, '8.4.11' if engine == 'mysql' else '11.4.13')


def meta():
    return {'table': {'TABLE_TYPE': 'BASE TABLE', 'ENGINE': 'InnoDB'},
            'columns': [column('id', 'bigint'), column('note', nullable='YES'), column('blob', 'blob', 'YES')],
            'indexes': [], 'triggers': [], 'foreign_keys': [], 'create': {'Create Table': 'example'}, 'key': ['id']}


class Session:
    def __init__(self):
        self.engine, self.server_version = 'mysql', '8.4.11'
        self._conn = types.SimpleNamespace(password='secret-value')
        self.run_lock = threading.Lock()
        self.ended = False
        self.end = mock.Mock()
        self._verify_identity = mock.Mock()


def snapshot(engine='mysql'):
    session = Session(); session.engine = engine
    return td.Snapshot('identity', {'type': 'development', 'read_only': False}, session, 'db', 'table', meta(),
                       [['31', '6F6C64', '00FF'], ['32', None, '']], model(engine), 0, 50)


class Audit:
    def __init__(self, fail=False):
        self.id = None; self.fail = fail; self.events = []
    def attempt(self, statements, **kwargs):
        if self.fail: raise OSError('audit unavailable')
        self.id = 'audit'; self.events.append(('attempt', statements)); return self.id
    def complete(self, status, **kwargs): self.events.append((status, kwargs))
    def outcome(self, statements, status, **kwargs): self.events.append((status, kwargs)); return 'audit'


class TableDataTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        env = dict(os.environ, HOME=self.home.name, USERPROFILE=self.home.name, MYFLAMES_SECRET_BACKEND='vault')
        env.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        self.environment = mock.patch.dict(os.environ, env, clear=True); self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_cell_exact_null_empty_bigint_binary_and_controls(self):
        self.assertEqual(td.cell(column('x'), None), {'kind': 'null', 'value': None})
        self.assertEqual(td.cell(column('x'), ''), {'kind': 'text', 'value': ''})
        self.assertEqual(td.cell(column('x'), '61000A62'), {'kind': 'text', 'value': 'a\x00\nb'})
        self.assertEqual(td.cell(column('x', 'blob'), '00FF'), {'kind': 'hex', 'value': '00FF'})
        big = '9223372036854775807'
        self.assertEqual(td.value_sql(column('id', 'bigint'), {'kind': 'text', 'value': big}), big)
        self.assertEqual(td.value_sql(column('x'), {'kind': 'text', 'value': "';\\! rm -rf /\n"}), "CONVERT(X'273B5C2120726D202D7266202F0A' USING utf8mb4)")

    def test_values_reject_invalid_null_binary_range_and_expressions(self):
        for col, value in [(column('x'), {'kind': 'null', 'value': None}),
                           (column('x', 'blob'), {'kind': 'hex', 'value': '0'}),
                           (column('x', 'bigint'), {'kind': 'text', 'value': '9223372036854775808'}),
                           (column('x', 'int'), {'kind': 'text', 'value': '1 OR 1=1'}),
                           (column('x', 'decimal'), {'kind': 'text', 'value': 'NaN'}),
                           (column('x'), {'kind': 'text', 'value': 'a' * (td.CELL_LIMIT + 1)})]:
            with self.assertRaises(ValueError): td.value_sql(col, value)

    def test_plan_uses_private_baseline_and_exact_transaction_boundaries(self):
        value = snapshot()
        plan = td.build_plan(value, [{'kind': 'update', 'row_id': value.row_ids[0], 'values': {'note': {'kind': 'text', 'value': 'new'}}},
                                    {'kind': 'delete', 'row_id': value.row_ids[1]},
                                    {'kind': 'insert', 'values': {'id': {'kind': 'text', 'value': '3'}, 'note': {'kind': 'null', 'value': None}}}])
        self.assertIn('WHERE `id` = 1 LIMIT 1', plan['writes'][0])
        self.assertEqual(plan['checks'][0]['before'], value.rows[0])
        self.assertIn('LOCK TABLES `db`.`table` WRITE', plan['statements']); self.assertEqual(plan['statements'][-2:], ['COMMIT', 'UNLOCK TABLES']); self.assertNotIn('START TRANSACTION', plan['statements'])
        self.assertIn('NULL', plan['writes'][2])
        self.assertEqual(td.build_plan(value, [{'kind': 'insert', 'values': {}}])['writes'], ['INSERT INTO `db`.`table` () VALUES ()'])

    def test_plan_rejects_forged_rows_duplicate_edits_and_unknown_fields(self):
        value = snapshot()
        cases = [[{'kind': 'delete', 'row_id': 'forged'}], [{'kind': 'delete', 'row_id': value.row_ids[0]}] * 2,
                 [{'kind': 'update', 'row_id': value.row_ids[0], 'values': {'notacolumn': {'kind': 'text', 'value': 'x'}}}],
                 [{'kind': 'delete', 'row_id': value.row_ids[0], 'where': '1=1'}], [{'kind': 'insert', 'values': {}}] * 21]
        for changes in cases:
            with self.assertRaises(ValueError): td.build_plan(value, changes)

    def test_unsupported_metadata_and_missing_authority_are_read_only_both_engines(self):
        for engine in ('mysql', 'mariadb'):
            self.assertEqual(td.edit_reason(meta(), model(engine), 'db', 'table'), '')
            for mutation, match in [(lambda m: m.update(key=[]), 'UNIQUE'),
                                    (lambda m: m['table'].update(ENGINE='MyISAM'), 'InnoDB'),
                                    (lambda m: m.update(triggers=[{'TRIGGER_NAME': 'trigger'}]), 'triggers'),
                                    (lambda m: m.update(foreign_keys=[{'name': 'foreign'}]), 'foreign keys'),
                                    (lambda m: m['columns'][1].update(DATA_TYPE='float'), 'unsupported')]:
                changed = meta(); mutation(changed)
                self.assertIn(match, td.edit_reason(changed, model(engine), 'db', 'table'))
            limited = privileges.parse_grants(["GRANT SELECT, INSERT, UPDATE, DELETE, TRIGGER, LOCK TABLES ON `db`.* TO 'operator'@'%'"], engine, '8.4.11')
            self.assertIn('global SELECT', td.edit_reason(meta(), limited, 'db', 'table'))
            no_trigger = privileges.parse_grants(["GRANT SELECT, LOCK TABLES ON *.* TO 'operator'@'%'"], engine, '8.4.11')
            self.assertIn('TRIGGER', td.edit_reason(meta(), no_trigger, 'db', 'table'))

    def test_expressions_and_hidden_column_semantics_are_not_editable(self):
        for engine in ('mysql', 'mariadb'):
            for extra in ('VIRTUAL GENERATED', 'STORED GENERATED', 'PERSISTENT', 'INVISIBLE', 'DEFAULT_GENERATED', 'on update current_timestamp()'):
                m = meta(); m['columns'][1]['EXTRA'] = extra
                self.assertIn('unsupported', td.edit_reason(m, model(engine), 'db', 'table'))
            m = meta(); m['checks'] = [{'CONSTRAINT_NAME':'check1'}]
            self.assertIn('CHECK', td.edit_reason(m, model(engine), 'db', 'table'))
        for value in ('nextval(`sequence`)', 'uuid()', "udf('x')", "concat('a','b')", 'current_timestamp()'):
            m = meta(); m['columns'][1]['COLUMN_DEFAULT'] = value
            self.assertIn('default expressions', td.edit_reason(m, model('mariadb'), 'db', 'table'))
        for value in (None, 'NULL', '0.00', "'literal (not a function)'", "'quote''literal'", "x'00FF'"):
            self.assertTrue(td.literal_default(value))

    def test_read_rows_checks_length_and_null_consistency(self):
        m = meta(); m['columns'] = [column('x')]
        for rows in [[{'l0': '1', 'v0': None}], [{'l0': None, 'v0': ''}], [{'l0': '3', 'v0': '61'}], [{'l0': str(td.CELL_LIMIT + 1), 'v0': None}], [{'l0': '1', 'v0': 'GG'}]]:
            with mock.patch.object(td, 'query', return_value=rows):
                with self.assertRaises(ValueError): td.read_rows(Session(), m, 'db', 't', ' LIMIT 1')
        with mock.patch.object(td, 'query', return_value=[{'l0': '0', 'v0': ''}, {'l0': None, 'v0': None}]):
            self.assertEqual(td.read_rows(Session(), m, 'db', 't', ' LIMIT 2'), [[''], [None]])
        self.assertIn('HEX(CAST(`x` AS BINARY))', td.select_sql(m, 'db', 't'))

    def test_length_probe_bounds_hex_response_before_retaining_values(self):
        m = meta(); m['columns'] = [column('a'), column('b')]
        lengths = [{'l0': str(td.CELL_LIMIT), 'l1': str(td.CELL_LIMIT)}] * 17
        with mock.patch.object(td, 'query', return_value=lengths) as query:
            with self.assertRaisesRegex(ValueError, 'snapshot limit'):
                td.read_rows(Session(), m, 'db', 't', ' LIMIT 50')
            self.assertEqual(query.call_count, 1)
            self.assertNotIn('HEX(', query.call_args[0][1])
        with mock.patch.object(td, 'query', side_effect=[[{'l0':'1','l1':None}], [{'l0':'2','v0':'6162','l1':None,'v1':None}]]):
            with self.assertRaisesRegex(ValueError, 'changed while reading'):
                td.read_rows(Session(), m, 'db', 't', ' LIMIT 1')

    def test_protocol_errors_warnings_and_truncation_refuse_snapshots(self):
        session = Session()
        for entry in [{'type': 'rows', 'truncated': True}, {'type': 'ok', 'warnings': 1}]:
            session._helper = mock.Mock(return_value=types.SimpleNamespace(marker=True, real_errors=lambda: [], entries=[entry]))
            with self.assertRaises(ValueError): td.query(session, 'SELECT 1')
        session._helper = mock.Mock(return_value=types.SimpleNamespace(marker=False, real_errors=lambda: [], entries=[]))
        with self.assertRaises(td.ConnectorError): td.query(session, 'SELECT 1')

    def run_batch(self, fault=None, policy=None, audit=None):
        value = snapshot(); value.plan = td.build_plan(value, [
            {'kind': 'update', 'row_id': value.row_ids[0], 'values': {'note': {'kind': 'text', 'value': 'new'}}},
            {'kind': 'update', 'row_id': value.row_ids[1], 'values': {'note': {'kind': 'text', 'value': 'two'}}}])
        value.state = 'running'; calls = []; audit = audit or Audit()
        def execute(session, sql, rows=True):
            calls.append(sql)
            if fault == 'second_write' and sql.startswith('UPDATE') and sum(s.startswith('UPDATE') for s in calls) == 2:
                raise ValueError('second write refused')
            if fault == 'rollback' and (sql.startswith('UPDATE') or sql == 'ROLLBACK'): raise td.ConnectorError('lost rollback reply')
            if fault == 'commit' and sql == 'COMMIT': raise td.ConnectorError('lost commit reply')
            return [] if rows else 1 if sql.startswith('UPDATE') else 0
        current_meta = copy.deepcopy(value.meta)
        if fault == 'metadata': current_meta['create'] = {'Create Table': 'changed'}
        baseline = [[value.rows[0]], [value.rows[1]]]
        if fault == 'conflict': baseline[0] = [['31', '646966666572656E74', '00FF']]
        with mock.patch.object(td, 'query', side_effect=execute), mock.patch.object(td, 'metadata', return_value=current_meta), \
                mock.patch.object(td, 'read_rows', side_effect=baseline), mock.patch.object(td, '_actor', return_value=model()), \
                mock.patch.object(td, '_policy', side_effect=(lambda *args: policy(calls)) if policy else None):
            td._apply(value, {}, types.SimpleNamespace(store=None), audit)
        return value, calls, audit

    def test_success_locks_before_writes_and_commits_once(self):
        value, calls, audit = self.run_batch()
        self.assertEqual(value.state, 'complete'); self.assertEqual(value.affected, 2)
        self.assertLess(calls.index('LOCK TABLES `db`.`table` WRITE'), next(i for i, sql in enumerate(calls) if sql.startswith('UPDATE')))
        self.assertEqual(calls.count('COMMIT'), 1); self.assertNotIn('ROLLBACK', calls)
        self.assertEqual(audit.events[0][0], 'attempt')

    def test_conflict_and_metadata_drift_rollback_before_any_write(self):
        for fault in ('conflict', 'metadata'):
            value, calls, audit = self.run_batch(fault)
            self.assertEqual(value.state, 'error'); self.assertIn('ROLLBACK', calls)
            self.assertFalse(any(sql.startswith('UPDATE') for sql in calls)); self.assertNotIn('COMMIT', calls)
            self.assertEqual(value.affected, 0)

    def test_later_error_rolls_back_whole_batch_and_zeroes_committed_count(self):
        value, calls, audit = self.run_batch('second_write')
        self.assertIn('ROLLBACK', calls); self.assertNotIn('COMMIT', calls)
        self.assertEqual(value.affected, 0); self.assertEqual(value.state, 'error')

    def test_policy_drift_after_first_write_prevents_commit_and_rolls_back(self):
        def drift(calls):
            if any(sql.startswith('UPDATE') for sql in calls): raise ValueError('policy changed')
        value, calls, audit = self.run_batch(policy=drift)
        self.assertEqual(value.state, 'error'); self.assertIn('ROLLBACK', calls); self.assertNotIn('COMMIT', calls)

    def test_audit_failure_sends_nothing(self):
        value, calls, audit = self.run_batch(audit=Audit(fail=True))
        self.assertEqual(calls, []); self.assertEqual(value.state, 'error')

    def test_lost_commit_reply_is_unknown_and_never_retried(self):
        value, calls, audit = self.run_batch('commit')
        self.assertEqual(value.state, 'unknown'); self.assertEqual(calls.count('COMMIT'), 1)
        self.assertNotIn('ROLLBACK', calls); self.assertNotIn('UNLOCK TABLES', calls)

    def test_failed_rollback_disconnects_without_implicit_commit_from_unlock(self):
        value, calls, audit = self.run_batch('rollback')
        self.assertEqual(value.state, 'unknown')
        self.assertNotIn('UNLOCK TABLES', calls)
        self.assertEqual(calls[-1], 'ROLLBACK')
        value.session.end.assert_called_once()

    def test_open_holds_lock_before_metadata_and_rejects_views_without_data(self):
        for view, fault in ((False, None), (True, None), (False, 'generated'), (False, 'metadata'), (False, 'rows'), (False, 'unlock')):
            registry = td.TableData()
            session = Session(); session.start = mock.Mock()
            events = []
            current = meta()
            if view: current['table']['TABLE_TYPE'] = 'VIEW'
            if fault == 'generated': current['columns'][1]['EXTRA'] = 'VIRTUAL GENERATED'
            def execute(_session, sql, rows=True):
                events.append(sql)
                if fault == 'unlock' and sql == 'UNLOCK TABLES': raise ValueError('unlock failed')
                return [] if rows else 0
            def metadata(*args):
                events.append('metadata')
                if fault == 'metadata': raise ValueError('metadata failed')
                return current
            def rows(*args):
                events.append('rows')
                if fault == 'rows': raise ValueError('rows failed')
                return [['31', '61', None]]
            workspace = types.SimpleNamespace(table_data=registry, store=None)
            facts = ({}, 'identity', {'type':'development', 'read_only':False}, 'test')
            payload = {'connection':{}, 'schema':'db', 'table':'table'}
            with mock.patch.object(td.ed, 'EditorSession', return_value=session), mock.patch.object(td, '_facts', return_value=facts), \
                    mock.patch.object(td, 'connection_options', return_value={}), mock.patch.object(td, '_actor', return_value=model()), \
                    mock.patch.object(td, 'query', side_effect=execute), mock.patch.object(td, 'metadata', side_effect=metadata), \
                    mock.patch.object(td, 'read_rows', side_effect=rows):
                if view or fault:
                    with self.assertRaises(ValueError): td.response('/api/table-data/open', payload, workspace, lambda value:value)
                    session.end.assert_called_once()
                else:
                    result = td.response('/api/table-data/open', payload, workspace, lambda value:value)
                    self.assertTrue(result['editable'])
                self.assertLess(events.index('LOCK TABLES `db`.`table` READ'), events.index('metadata'))
                if view or fault == 'generated': self.assertNotIn('rows', events)
                if 'rows' in events: self.assertLess(events.index('metadata'), events.index('rows'))
                self.assertEqual(registry.opening, 0)
            registry.close()

    def test_open_reservations_bound_concurrent_startups(self):
        registry = td.TableData(); self.addCleanup(registry.close)
        with registry.reserve(), registry.reserve(), registry.reserve(), registry.reserve():
            with self.assertRaises(ValueError):
                with registry.reserve(): pass
        self.assertEqual(registry.opening, 0)

    def test_workspace_close_supports_partial_initialization_and_closes_grid(self):
        from myflames.ui import WorkspaceServer
        for present in (False, True):
            server = object.__new__(WorkspaceServer)
            server.jobs, server.editor, server.lifecycle = mock.Mock(), mock.Mock(), mock.Mock()
            if present: server.table_data = mock.Mock()
            with mock.patch('http.server.ThreadingHTTPServer.server_close'):
                server.server_close()
            if present: server.table_data.close.assert_called_once()
            server.jobs.close.assert_called_once()

    def test_registry_identity_expiry_capacity_and_reuse_tokens(self):
        registry = td.TableData(); self.addCleanup(registry.close)
        value = snapshot(); registry.add(value)
        self.assertIs(registry.get(value.id, value.identity), value)
        with self.assertRaises(ValueError): registry.get(value.id, 'another-owner')
        value.created -= td.TTL + 1
        with self.assertRaises(ValueError): registry.get(value.id, value.identity)
        self.assertTrue(value.session.end.called)
        for _ in range(4): registry.add(snapshot())
        with self.assertRaises(ValueError): registry.add(snapshot())
        token = value.tokens.issue('table_data', value.identity, {'sql': 'one'})
        with self.assertRaises(ValueError): value.tokens.check(token, 'table_data', value.identity, {'sql': 'two'}, True)
        value.tokens.check(token, 'table_data', value.identity, {'sql': 'one'}, True)
        with self.assertRaises(ValueError): value.tokens.check(token, 'table_data', value.identity, {'sql': 'one'}, True)


if __name__ == '__main__':
    unittest.main()
