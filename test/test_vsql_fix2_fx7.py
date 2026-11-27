"""FX7 variable-write regressions. Synthetic transport, real write-safety gates."""
import copy
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _perf_replay import FakeFactory, fixture
from _reviewed_replay import ReplayConn, recorded_body
from test_vsql_fix1_fx1 import NAME, OLD, NEW, OTHER, secret_info, after_state
from test_variables import mysql_variable
from myflames import dbops, parameters, ui, variables as V
from myflames.connector import ConnectorError
from myflames.review_tokens import ReviewTokens
from myflames.workspace_store import WorkspaceStore


class VariableCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = mock.patch.dict(os.environ, {
            'HOME': self.temp.name, 'USERPROFILE': self.temp.name,
            'MYFLAMES_SECRET_BACKEND': 'vault',
        })
        env.start()
        self.addCleanup(env.stop)
        self.workspace = types.SimpleNamespace(store=WorkspaceStore(Path(self.temp.name) / 'w.sqlite3'))
        self.connection = {'host': '127.0.0.1', 'port': 34811, 'user': 'root'}

    def call(self, operation, payload, conn):
        with mock.patch('myflames.connector.MySQLConnection', FakeFactory(conn)):
            return ui._variable_response('/api/live/variable/' + operation,
                                         dict(payload, connection=self.connection), self.workspace)

    def records(self):
        return dbops.audit_report(self.workspace.store)['records']

    def execute(self, info, value=NEW, mode='global', after=None, error=None, cli=False):
        plan = V.build_plan(V.describe(info), value, mode)
        self.assertEqual(plan['status'], 'ready', plan['problems'])
        body = V.TAG + plan['statement']
        if mode == 'session':
            body += "; " + V.TAG + "SELECT '%safter'" % V._MARK
            body += "; " + V.TAG + "SHOW SESSION VARIABLES WHERE Variable_name = '%s'" % info['name']
        conn = ReplayConn([[V.TAG + 'SHOW GRANTS', ''], [body, None, error] if error else [body, '']])
        audit = dbops.AuditTrail(self.workspace.store, V.OPERATIONS[mode], 'cli' if cli else 'ui')
        with mock.patch.object(V, '_read', return_value=info), \
                mock.patch.object(V, '_after', return_value=after if after is not None else after_state(info, mode, value)):
            if cli:
                result = V.change_cli(conn, info['name'], value, mode, assume_yes=True, quiet=True,
                                      stderr=io.StringIO(), audit=audit)
            else:
                result = V.apply_change(conn, plan, plan['expect'], confirmed=True, audit=audit)
        return result, conn


class TestSGF1(VariableCase):
    def test_inventory_marker_is_refused_in_every_mode(self):
        for name in sorted(dbops._SECRET_VARIABLES):
            for mode in V.MODES:
                for value in (dbops.REDACTED, 'prefix' + dbops.REDACTED + 'suffix'):
                    with self.subTest(name=name, mode=mode, value=value):
                        variable = V.describe(secret_info(name=name))
                        plan = V.build_plan(variable, value, mode)
                        self.assertEqual(plan['status'], 'refused')
                        self.assertIsNone(plan['statement'])
                        self.assertIsNone(plan['config_line'])
                        self.assertIn('redacted_value', [p['code'] for p in plan['problems']])

    def test_plan_endpoint_refuses_every_inventory_marker_and_mode(self):
        for name in sorted(dbops._SECRET_VARIABLES):
            for mode in V.MODES:
                with self.subTest(name=name, mode=mode):
                    conn = ReplayConn([[V.TAG + 'SHOW GRANTS', '']])
                    with mock.patch.object(V, '_read', return_value=secret_info(name=name)):
                        plan = self.call('plan', dict(name=name, mode=mode, value=dbops.REDACTED), conn)
                    self.assertEqual(plan['status'], 'refused')
                    self.assertIsNone(plan['statement'])
                    self.assertIn('redacted_value', [p['code'] for p in plan['problems']])
                    self.assertTrue(all('SHOW GRANTS' in sql for sql, _ in conn.calls))

    def test_forged_marker_expect_sends_nothing_and_audits_refusal(self):
        for name in sorted(dbops._SECRET_VARIABLES):
            for mode in V.MODES:
                if mode == 'reset_persist':
                    continue  # RESET has no replacement value to execute.
                with self.subTest(name=name, mode=mode):
                    variable = V.describe(secret_info(name=name, numeric=mode == 'persist_only'))
                    plan = V.build_plan(variable, NEW, mode)
                    if plan['status'] != 'ready':
                        # Inventory variables can have an intrinsically unavailable scope.
                        variable['modes'][mode] = {'available': True, 'reason': None, 'recommended': False}
                        variable['non_persistent'] = False
                        plan = V.build_plan(variable, NEW, mode)
                    expect = copy.deepcopy(plan['expect'])
                    self.assertIsNotNone(expect)
                    expect['statement'] = V.statement_for(name, mode, V._text_literal(dbops.REDACTED))
                    safety, _ = ui._variable_policy(name, mode, self.connection, self.workspace.store)
                    expect['connection_identity'] = dbops.connection_identity(self.connection, safety)
                    conn = ReplayConn([])
                    with mock.patch.object(V, 'read_variable', return_value=variable):
                        result = self.call('apply', dict(name=name, mode=mode, value=dbops.REDACTED,
                                                        expect=expect, confirmed=True), conn)
                    self.assertEqual(result['status'], 'refused')
                    self.assertEqual(conn.calls, [])
                    record = next(row for row in self.records() if row['id'] == result['audit_id'])
                    self.assertEqual(record['status'], 'refused')

    def test_sensitive_flag_is_explicit_on_both_read_shapes(self):
        names = sorted(dbops._SECRET_VARIABLES) + ['max_connections']
        report = parameters.compute_parameters('mysql', (8, 4, 11), rows=[[n, OLD] for n in names])
        for item in report['items']:
            self.assertIs(item.get('sensitive'), dbops.sensitive(item['name']))
        for name in names:
            self.assertIs(V.describe(secret_info(name=name)).get('sensitive'), dbops.sensitive(name))

    def test_schemas_require_the_sensitive_flag(self):
        schemas = Path(__file__).resolve().parents[1] / 'docs' / 'schemas'
        variable = json.loads((schemas / 'variable-1.0.json').read_text(encoding='utf-8'))
        parameters_schema = json.loads((schemas / 'parameters-1.0.json').read_text(encoding='utf-8'))
        for shape in (variable, parameters_schema['$defs']['parameter']):
            self.assertIn('sensitive', shape['required'])
            self.assertEqual(shape['properties']['sensitive']['type'], 'boolean')

    def test_direct_apply_rechecks_markers_without_relying_on_endpoint_tokens(self):
        exercised = set()
        for name in sorted(dbops._SECRET_VARIABLES):
            for mode in ('global', 'session', 'persist', 'persist_only'):
                with self.subTest(name=name, mode=mode):
                    variable = V.describe(secret_info(name=name, numeric=mode == 'persist_only'))
                    plan = V.build_plan(variable, NEW, mode)
                    if plan['status'] != 'ready':
                        continue  # Only scopes this synthetic server actually offers.
                    exercised.add(mode)
                    expect = copy.deepcopy(plan['expect'])
                    expect['statement'] = V.statement_for(name, mode, V._text_literal(dbops.REDACTED))
                    request = dict(plan, requested=dbops.REDACTED)
                    conn = ReplayConn([])
                    audit = dbops.AuditTrail(self.workspace.store, V.OPERATIONS[mode], 'cli')
                    with mock.patch.object(V, 'read_variable', return_value=variable):
                        result = V.apply_change(conn, request, expect, confirmed=True, audit=audit)
                    self.assertEqual((result['status'], result['outcome']), ('refused', 'invalid'))
                    self.assertEqual(conn.calls, [])
                    record = next(row for row in self.records() if row['id'] == result['audit_id'])
                    self.assertEqual((record['status'], record['outcome']), ('refused', 'invalid'))
        self.assertEqual(exercised, {'global', 'session', 'persist', 'persist_only'})

    def test_non_sensitive_marker_remains_literal_text(self):
        plan = V.build_plan(mysql_variable('init_connect'), dbops.REDACTED, 'global')
        self.assertEqual(plan['status'], 'ready')
        self.assertEqual(plan['statement'], "SET GLOBAL init_connect = CONVERT(X'5b72656461637465645d' USING utf8mb4)")


class TestRRS5(VariableCase):
    def test_already_set_and_silently_kept_secrets_have_distinct_safe_messages(self):
        info = secret_info()
        already, _ = self.execute(info, value=OLD)
        kept, _ = self.execute(info, value=NEW, after=after_state(info, 'global', OLD))
        self.assertEqual((already['status'], kept['status']), ('unchanged', 'unchanged'))
        self.assertNotEqual(already['message'], kept['message'])
        self.assertIn('already set', already['message'])
        self.assertIn('did not take', kept['message'])
        self.assertIs(already['value_matched'], True)
        self.assertIs(kept['value_matched'], False)
        for result in (already, kept):
            for secret in (OLD, NEW, OLD.encode().hex(), NEW.encode().hex()):
                self.assertNotIn(secret, json.dumps(result))


class TestRRS4(VariableCase):
    def test_cli_already_matching_secret_succeeds_in_every_set_mode(self):
        for mode in ('global', 'session', 'persist', 'persist_only'):
            with self.subTest(mode=mode):
                info = secret_info(numeric=mode == 'persist_only')
                (code, result), _ = self.execute(info, value=OLD, mode=mode, cli=True)
                self.assertEqual(result['status'], 'unchanged')
                self.assertEqual(code, 0)
                self.assertIs(result['value_matched'], True)
                self.assertNotIn(OLD, json.dumps(result))

    def test_cli_kept_secret_fails_and_changed_secret_succeeds(self):
        info = secret_info()
        (code, kept), _ = self.execute(info, after=after_state(info, 'global', OLD), cli=True)
        self.assertEqual((code, kept['status']), (1, 'unchanged'))
        self.assertIs(kept['value_matched'], False)
        (code, changed), _ = self.execute(info, cli=True)
        self.assertEqual((code, changed['status']), (0, 'changed'))
        self.assertIs(changed['value_matched'], True)


class TestRRS3(VariableCase):
    def test_redacted_write_errors_preserve_unknown_outcome(self):
        for error in ('mysql client timed out after 30s: ' + OLD,
                      'ERROR 2013: Lost connection to server ' + OLD,
                      'ERROR 2006: server went away ' + OLD):
            with self.subTest(error=error):
                with self.assertRaises(ValueError) as caught:
                    self.execute(secret_info(), error=error)
                self.assertTrue(dbops.may_have_run(caught.exception))
                self.assertTrue(getattr(caught.exception, 'guarded_unknown', False))
                self.assertIn('may have run', str(caught.exception))
                self.assertNotIn(OLD, str(caught.exception))
                self.assertEqual(self.records()[0]['status'], 'unknown')

    def test_known_connection_failure_stays_known(self):
        # An initial connect failure has no guard acknowledgement. The write
        # replay adapter deliberately models failures after the guard ran.
        conn = ReplayConn([], error=ConnectorError('ERROR 2003: Cannot connect ' + OLD))
        with self.assertRaises(ConnectorError) as caught:
            V.read_variable(conn, NAME)
        self.assertFalse(dbops.may_have_run(caught.exception))
        self.assertNotIn('may have run', str(caught.exception))
        self.assertNotIn(OLD, str(caught.exception))
        self.assertEqual(len(conn.calls), 1)

    def test_endpoint_returns_unknown_result_without_server_diagnostic(self):
        info = secret_info()
        statement = V.build_plan(V.describe(info), NEW, 'global')['statement']
        conn = ReplayConn([[V.TAG + 'SHOW GRANTS', ''],
                           [V.TAG + statement, None, 'mysql client timed out: ' + OLD]])
        with mock.patch.object(V, '_read', return_value=info):
            plan = self.call('plan', dict(name=NAME, mode='global', value=NEW), conn)
            result = self.call('apply', dict(name=NAME, mode='global', value=NEW,
                                            expect=plan['expect'], confirmed=True), conn)
        self.assertEqual(result['status'], 'unknown')
        self.assertIn('may have run', result['message'])
        self.assertNotIn(OLD, json.dumps(result))
        self.assertNotIn(NEW, json.dumps(result))
        self.assertEqual(self.records()[0]['status'], 'unknown')


class TestRRS2(VariableCase):
    def test_failure_before_identity_prefix_is_private_at_every_public_boundary(self):
        private = 'private-before-identity-prefix'
        class FailingTransport(object):
            def run(self, *args, **kwargs):
                raise ConnectorError('ERROR 1045: ' + private)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        for surface in ('direct_read', 'direct_plan', 'endpoint_read', 'endpoint_plan'):
            with self.subTest(surface=surface):
                conn = FailingTransport()
                with self.assertRaises(ConnectorError) as caught:
                    if surface == 'direct_read':
                        V.read_variable(conn, NAME)
                    elif surface == 'direct_plan':
                        V.plan_change(conn, NAME, NEW, 'global')
                    else:
                        path = '/api/live/variable' + ('/plan' if surface == 'endpoint_plan' else '')
                        with mock.patch('myflames.connector.MySQLConnection', FakeFactory(conn)):
                            ui._variable_response(path, dict(connection=self.connection, name=NAME,
                                                            mode='global', value=NEW), self.workspace)
                self.assertNotIn(private, str(caught.exception))
                self.assertIn('1045', str(caught.exception))

    def transport(self, failure):
        calls = fixture('variable', 'mysql-8.4-global')['calls']
        # Recorded read shape, synthetic secret and server diagnostic.
        calls = [[part.replace('max_connections', 'wsrep_sst_auth').replace('151', OLD)
                  if isinstance(part, str) else part for part in call] for call in calls]
        def edit(sql, text, count):
            if failure in sql:
                raise ConnectorError('ERROR 1142 (42000): echoed-private-diagnostic-' + OLD)
            return text
        return ReplayConn(calls, edit=edit)

    def test_optional_metadata_and_grants_diagnostics_are_private(self):
        for failure in ('variables_info', 'SHOW GRANTS'):
            with self.subTest(failure=failure):
                plan = V.plan_change(self.transport(failure), 'wsrep_sst_auth', NEW, 'global')
                self.assertNotIn(OLD, json.dumps(plan))
                self.assertNotIn('echoed-private-diagnostic', json.dumps(plan))
                self.assertTrue(plan['variable']['notes'])
                self.assertIn('1142', ' '.join(plan['variable']['notes']))

    def test_initial_read_errors_are_private_for_read_and_plan_endpoints(self):
        for operation in ('read', 'plan'):
            for name in sorted(dbops._SECRET_VARIABLES):
                with self.subTest(operation=operation, name=name):
                    conn = ReplayConn([], error=ConnectorError('ERROR 1045: echoed-private-diagnostic-' + OLD))
                    path = '/api/live/variable' + ('/plan' if operation == 'plan' else '')
                    with mock.patch('myflames.connector.MySQLConnection', FakeFactory(conn)):
                        with self.assertRaises(ConnectorError) as caught:
                            ui._variable_response(path, dict(connection=self.connection, name=name,
                                                            mode='global', value=NEW), self.workspace)
                    self.assertNotIn(OLD, str(caught.exception))
                    self.assertNotIn('echoed-private-diagnostic', str(caught.exception))
                    self.assertIn('1045', str(caught.exception))


class TestRRS1(VariableCase):
    def review(self, info, requested=NEW):
        conn = ReplayConn([[V.TAG + 'SHOW GRANTS', '']])
        with mock.patch.object(V, '_read', return_value=info):
            return self.call('plan', dict(name=info['name'], mode='global', value=requested), conn)

    def test_saved_profile_drift_during_fresh_read_refuses_at_send_boundary(self):
        baseline = dict(self.connection, name='send-boundary fixture', type='development', read_only=False)
        profile = self.workspace.store.save('profile', baseline)
        self.connection = dict(self.connection, profile_id=profile['id'])
        for field, value in (('read_only', True), ('type', 'production'), ('host', 'changed.example')):
            with self.subTest(field=field):
                self.workspace.store.save('profile', baseline, key=profile['id'])
                info = secret_info()
                plan = self.review(info)
                conn = ReplayConn([[V.TAG + plan['statement'], '']])

                def read(*args):
                    # Change the real saved profile after apply's initial
                    # policy check, while it refreshes the variable evidence.
                    self.workspace.store.save('profile', dict(baseline, **{field: value}), key=profile['id'])
                    return info

                with mock.patch.object(V, '_read', side_effect=read) as reader, \
                        mock.patch.object(V, '_after', return_value=after_state(info, 'global')) as after:
                    result = self.call('apply', dict(name=NAME, mode='global', value=NEW,
                        expect=plan['expect'], confirmed=True), conn)
                self.assertEqual(reader.call_count, 1, 'Reach the fresh read after the initial policy gate')
                self.assertEqual((result['status'], result['outcome']), ('refused', 'review_changed'))
                self.assertEqual(conn.calls, [], 'Profile drift must prevent every outgoing statement')
                after.assert_not_called()
                record = next(row for row in self.records() if row['id'] == result['audit_id'])
                self.assertEqual((record['status'], record['outcome']), ('refused', 'review_changed'))
                self.assertNotIn(plan['expect']['review_token'], json.dumps(record))

    def test_fresh_bindings_cannot_be_substituted_into_old_review(self):
        info = secret_info()
        plan = self.review(info)
        live = copy.deepcopy(info)
        live['global_value'] = OTHER
        live['info']['set_time'] = '2026-11-27 13:00:00'
        forged = copy.deepcopy(plan['expect'])
        fresh = V.build_plan(V.describe(live), NEW, 'global')['expect']
        for key in ('value_bindings', 'source_metadata'):
            forged[key] = fresh[key]
        conn = ReplayConn([[V.TAG + plan['statement'], '']])
        with mock.patch.object(V, '_read', return_value=live), \
                mock.patch.object(V, '_after', return_value=after_state(live, 'global')):
            result = self.call('apply', dict(name=NAME, mode='global', value=NEW,
                                           expect=forged, confirmed=True), conn)
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(conn.calls, [])
        self.assertEqual(self.records()[0]['status'], 'refused')

    def test_missing_altered_and_replayed_tokens_send_nothing(self):
        info = secret_info()
        plan = self.review(info)
        for change in ('missing', 'altered'):
            with self.subTest(change=change):
                expect = copy.deepcopy(plan['expect'])
                if change == 'missing':
                    expect.pop('review_token', None)
                else:
                    expect['review_token'] = 'not-issued'
                conn = ReplayConn([[V.TAG + plan['statement'], '']])
                with mock.patch.object(V, '_read', return_value=info), \
                        mock.patch.object(V, '_after', return_value=after_state(info, 'global')):
                    result = self.call('apply', dict(name=NAME, mode='global', value=NEW,
                        expect=expect, confirmed=True), conn)
                self.assertEqual(result['status'], 'refused')
                self.assertEqual(conn.calls, [])
        conn = ReplayConn([[V.TAG + plan['statement'], '']])
        with mock.patch.object(V, '_read', return_value=info), \
                mock.patch.object(V, '_after', return_value=after_state(info, 'global')):
            payload = dict(name=NAME, mode='global', value=NEW, expect=plan['expect'], confirmed=True)
            first = self.call('apply', payload, conn)
            self.assertEqual(first['status'], 'changed')
            self.assertEqual(recorded_body(conn.calls[0][0]), V.TAG + plan['statement'])
            count = len(conn.calls)
            again = self.call('apply', payload, conn)
            self.assertEqual(again['status'], 'refused')
            self.assertEqual(len(conn.calls), count)
        self.assertNotIn(plan['expect']['review_token'], json.dumps(self.records()))

    def test_expired_review_refuses_before_connection_and_at_send_boundary(self):
        for when in ('before_connection', 'after_read'):
            with self.subTest(when=when):
                clock = [0]
                self.workspace.review_tokens = ReviewTokens(clock=lambda: clock[0])
                info = secret_info()
                plan = self.review(info)
                if when == 'before_connection':
                    clock[0] = 301
                conn = ReplayConn([[V.TAG + plan['statement'], '']])
                factory = FakeFactory(conn)
                def read(*args):
                    clock[0] = 301
                    return info
                payload = dict(connection=self.connection, name=NAME, mode='global', value=NEW,
                               expect=plan['expect'], confirmed=True)
                with mock.patch('myflames.connector.MySQLConnection', factory), \
                        mock.patch.object(V, '_read', side_effect=read) as reader:
                    result = ui._variable_response('/api/live/variable/apply', payload, self.workspace)
                self.assertEqual(result['status'], 'refused')
                self.assertEqual(conn.calls, [])
                self.assertEqual(reader.call_count, 0 if when == 'before_connection' else 1)
                if when == 'before_connection':
                    self.assertIsNone(factory.options)
                record = next(row for row in self.records() if row['id'] == result['audit_id'])
                self.assertEqual((record['status'], record['outcome']), ('refused', 'review_changed'))
                self.assertNotIn(plan['expect']['review_token'], json.dumps(self.records()))

    def test_requested_value_statement_and_target_cannot_be_changed(self):
        plan = self.review(secret_info())
        for changed in ('value', 'statement', 'server_identity', 'connection'):
            with self.subTest(changed=changed):
                expect = copy.deepcopy(plan['expect'])
                payload = dict(connection=self.connection, name=NAME, mode='global', value=NEW,
                               expect=expect, confirmed=True)
                if changed == 'value':
                    payload['value'] = OTHER
                elif changed == 'statement':
                    expect['statement'] = V.statement_for(NAME, 'global', V._text_literal(OTHER))
                elif changed == 'server_identity':
                    expect['server_identity']['server_uuid'] = '99999999-2222-3333-4444-555555555555'
                else:
                    payload['connection'] = dict(self.connection, host='different-server')
                    safety, _ = ui._variable_policy(NAME, 'global', payload['connection'], self.workspace.store)
                    expect['connection_identity'] = dbops.connection_identity(payload['connection'], safety)
                conn = ReplayConn([])
                factory = FakeFactory(conn)
                with mock.patch('myflames.connector.MySQLConnection', factory):
                    result = ui._variable_response('/api/live/variable/apply', payload, self.workspace)
                self.assertEqual(result['status'], 'refused')
                self.assertIsNone(factory.options)
                self.assertEqual(conn.calls, [])
                record = next(row for row in self.records() if row['id'] == result['audit_id'])
                self.assertEqual((record['status'], record['outcome']), ('refused', 'review_changed'))

    def test_decline_and_policy_refusals_never_persist_review_tokens(self):
        plan = self.review(secret_info())
        for fields, status, outcome in (({'declined': True}, 'declined', 'declined'),
                                        ({'confirmed': False}, 'needs_confirmation', 'needs_confirmation'),
                                        ({'connection': dict(self.connection, read_only=True)}, 'refused', 'read_only')):
            with self.subTest(outcome=outcome):
                payload = dict(connection=self.connection, name=NAME, mode='global', value=NEW,
                               expect=plan['expect'], confirmed=True, reviewed_statement=plan['statement'])
                payload.update(fields)
                conn = ReplayConn([])
                factory = FakeFactory(conn)
                with mock.patch('myflames.connector.MySQLConnection', factory):
                    result = ui._variable_response('/api/live/variable/apply', payload, self.workspace)
                self.assertEqual(result['status'], status)
                self.assertIsNone(factory.options)
                self.assertEqual(conn.calls, [])
                record = next(row for row in self.records() if row['id'] == result['audit_id'])
                self.assertEqual(record['outcome'], outcome)
                for private in (plan['expect']['review_token'], OLD, NEW, OLD.encode().hex(), NEW.encode().hex()):
                    self.assertNotIn(private, json.dumps(record))

    def test_valid_review_initial_connection_exception_stays_private(self):
        # The legacy endpoint regression constructs expect locally, without
        # a token. Use an issued review to reach the same transport failure.
        plan = self.review(secret_info())
        conn = ReplayConn([], error=ConnectorError('ERROR 1045: %s %s' % (OLD, NEW)))
        with self.assertRaises(ConnectorError) as caught:
            self.call('apply', dict(name=NAME, mode='global', value=NEW,
                                   expect=plan['expect'], confirmed=True), conn)
        self.assertNotIn(OLD, str(caught.exception))
        self.assertNotIn(NEW, str(caught.exception))
        self.assertIn('1045', str(caught.exception))
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertEqual((records[0]['status'], records[0]['outcome']), ('error', 'error'))
        for private in (plan['expect']['review_token'], OLD, NEW, OLD.encode().hex(), NEW.encode().hex()):
            self.assertNotIn(private, json.dumps(records))


if __name__ == '__main__':
    unittest.main()
