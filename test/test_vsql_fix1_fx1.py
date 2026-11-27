"""FX1 regressions: secret review binding, result privacy and KNN sharing.

Transport answers below are synthetic. Real classification, policy gates,
target guards and audit persistence run unchanged; no server is contacted.
"""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _reviewed_replay import ReplayConn, recorded_body
from myflames import dbops, variables as V
from myflames.chart_data import build_chart_data
from myflames.connector import ConnectorError
from myflames.parser import hide_parsed_plan_literals, parse_explain
from myflames.plan_privacy import hide_analysis_literals, hide_plan_literals
from myflames.workspace_store import WorkspaceStore


NAME = 'vsql_rest.jwt_secret'
OLD = '271314157126'
NEW = '391314157126'
OTHER = '481314157126'


def secret_info(name=NAME, global_value=OLD, session_value=OLD,
                persisted_value=OLD, numeric=False):
    """Raw variable read, with explicit synthetic metadata for all modes."""
    return {
        'name': name, 'engine': 'mysql', 'flavor': 'villagesql',
        'server_product': 'VillageSQL', 'server_base': 'mysql',
        'server_version': '9.7.0-VillageSQL', 'version_tuple': (9, 7, 0),
        'server_identity': {'server_uuid': '11111111-2222-3333-4444-555555555555'},
        'platform': 'Linux', 'in_global': True, 'in_session': True,
        'global_value': global_value, 'session_value': session_value,
        'persisted_globals_load': 'ON', 'related': {}, 'catalog': None,
        'info': {'source': 'DYNAMIC', 'path': '', 'min': '0', 'max': '999999999999',
                 'set_time': None, 'set_user': None, 'set_host': None},
        'metadata': {'scope': 'SESSION', 'type': 'Integer' if numeric else 'String',
                     'min': '0', 'max': '999999999999', 'documentation': ''},
        'persisted': {'present': True, 'value': persisted_value, 'read': True},
        'notes': [],
    }


def after_state(info, mode, value=NEW):
    persisted = copy.deepcopy(info['persisted'])
    if mode in ('persist', 'persist_only'):
        persisted['value'] = value
    elif mode == 'reset_persist':
        persisted.update(present=False, value=None)
    return {
        'global': value if mode in ('global', 'persist') else info['global_value'],
        'global_read': True, 'session': value if mode == 'session' else None,
        'session_read': mode == 'session', 'persisted': persisted['value'],
        'persisted_state': V._persisted_state(persisted, True),
        'engine': info['engine'], 'server_version': info['server_version'],
        'server_identity': info['server_identity'],
    }


class SecretTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = mock.patch.dict(os.environ, {
            'HOME': self.temp.name, 'USERPROFILE': self.temp.name,
            'MYFLAMES_SECRET_BACKEND': 'vault',
        })
        env.start()
        self.addCleanup(env.stop)
        self.store = WorkspaceStore(Path(self.temp.name) / 'workspace.sqlite3')

    def apply(self, info, mode='global', live=None, after=None, expect_edit=None,
              requested=NEW, write_error=None, after_error=None, **kwargs):
        plan = V.build_plan(V.describe(info), requested, mode)
        self.assertEqual(plan['status'], 'ready', plan['problems'])
        expect = json.loads(json.dumps(plan['expect']))
        if expect_edit:
            expect_edit(expect)
        # Session mode also reads back in the protected write packet.
        body = V.TAG + plan['statement']
        if mode == 'session':
            body += "; " + V.TAG + "SELECT '%safter'" % V._MARK
            body += "; " + V.TAG + "SHOW SESSION VARIABLES WHERE Variable_name = '%s'" % info['name']
        conn = ReplayConn([[body, None, write_error] if write_error else [body, '']])
        audit = dbops.AuditTrail(self.store, V.OPERATIONS[mode], 'ui')
        with mock.patch.object(V, 'read_variable', side_effect=lambda *_: V.describe(live or info)), \
                mock.patch.object(V, '_after', return_value=after or after_state(info, mode),
                                  side_effect=after_error):
            result = V.apply_change(conn, plan, expect, confirmed=True, audit=audit, **kwargs)
        return result, conn, plan

    def records(self):
        return dbops.audit_report(self.store)['records']

    def assert_private(self, result):
        text = json.dumps(result)
        for secret in (OLD, NEW, OTHER, OLD.encode().hex(), NEW.encode().hex(), OTHER.encode().hex()):
            self.assertNotIn(secret, text)
        self.assertIn(dbops.REDACTED, text)


class TestSecretReviewBinding(SecretTestCase):
    def test_changed_global_secret_refuses_before_write(self):
        info = secret_info()
        live = dict(info, global_value=OTHER)
        result, conn, _ = self.apply(info, live=live)
        self.assertEqual((result['status'], result['outcome']), ('refused', 'changed_variable'))
        self.assertEqual(conn.calls, [])
        self.assertEqual(self.records()[0]['status'], 'refused')

    def test_changed_persisted_secret_refuses_in_every_mode(self):
        for mode in V.MODES:
            with self.subTest(mode=mode):
                info = secret_info(numeric=mode == 'persist_only')
                live = dict(info, persisted=dict(info['persisted'], value=OTHER))
                result, conn, _ = self.apply(info, mode, live=live)
                self.assertEqual((result['status'], result['outcome']), ('refused', 'changed_variable'))
                self.assertEqual(conn.calls, [])

    def test_changed_session_secret_refuses_session_change(self):
        info = secret_info()
        result, conn, _ = self.apply(info, 'session', live=dict(info, session_value=OTHER))
        self.assertEqual((result['status'], result['outcome']), ('refused', 'changed_variable'))
        self.assertEqual(conn.calls, [])

    def test_unchanged_secret_proceeds_with_exact_reviewed_sql(self):
        for mode in V.MODES:
            with self.subTest(mode=mode):
                info = secret_info(numeric=mode == 'persist_only')
                result, conn, plan = self.apply(info, mode)
                self.assertEqual(result['status'], 'changed')
                self.assertEqual(len(conn.calls), 1)
                self.assertTrue(recorded_body(conn.calls[0][0]).startswith(V.TAG + plan['statement']))

    def test_binding_is_not_a_plain_value_hash_and_distinguishes_empty_and_null(self):
        import hashlib
        descriptions = [V.describe(secret_info(global_value=value)) for value in (OLD, '', None)]
        bindings = [v['value_bindings']['global_value'] for v in descriptions]
        self.assertEqual(len(set(bindings)), 3)
        self.assertNotEqual(bindings[0], hashlib.sha256(OLD.encode()).hexdigest())
        self.assertNotIn(OLD, json.dumps(descriptions[0]))

    def test_missing_or_altered_binding_cannot_authorize_a_write(self):
        for edit in (lambda e: e.pop('value_bindings', None),
                     lambda e: e.update(value_bindings={'global_value': '0' * 64})):
            with self.subTest(edit=edit):
                with self.assertRaisesRegex(ValueError, 'Review'):
                    self.apply(secret_info(), expect_edit=edit)

    def test_forged_or_cross_field_binding_refuses_without_leaking(self):
        for edit in (
                lambda e: e['value_bindings'].update(global_value='0' * 64),
                lambda e: e['value_bindings'].update(global_value=e['value_bindings']['session_value'])):
            with self.subTest(edit=edit):
                result, conn, _ = self.apply(secret_info(), expect_edit=edit)
                self.assertEqual((result['status'], result['outcome']), ('refused', 'changed_variable'))
                self.assertEqual(conn.calls, [])
                self.assert_private(result)
                self.assert_private(self.records())

    def test_changed_to_empty_null_or_whitespace_is_stale(self):
        info = secret_info()
        for current in ('', None, OLD + ' '):
            with self.subTest(current=current):
                result, conn, _ = self.apply(info, live=dict(info, global_value=current))
                self.assertEqual((result['status'], result['outcome']), ('refused', 'changed_variable'))
                self.assertEqual(conn.calls, [])

    def test_a_process_key_change_invalidates_existing_review(self):
        info = secret_info()
        plan = V.build_plan(V.describe(info), NEW, 'global')
        conn = ReplayConn([])
        with mock.patch.object(V, '_VALUE_BINDING_KEY', b'x' * 32), \
                mock.patch.object(V, 'read_variable', side_effect=lambda *_: V.describe(info)):
            result = V.apply_change(conn, plan, plan['expect'], confirmed=True)
        self.assertEqual((result['status'], result['outcome']), ('refused', 'changed_variable'))
        self.assertEqual(conn.calls, [])
        self.assert_private(result)


class TestSecretResults(SecretTestCase):
    def test_success_results_and_audits_are_private_in_every_mode(self):
        for mode in V.MODES:
            with self.subTest(mode=mode):
                info = secret_info(numeric=mode == 'persist_only')
                result, _, plan = self.apply(info, mode)
                self.assertEqual(result['status'], 'changed')
                self.assert_private(result)
                self.assert_private(self.records())
                self.assertEqual(result['after']['global'], dbops.REDACTED)
                self.assertIsInstance(result['sent'], list)
                self.assertIsInstance(result['plan']['problems'], list)
                # Privacy is copy-only; it must never redact the executable plan.
                if mode != 'reset_persist':
                    self.assertIn(NEW, plan['statement'])

    def test_unchanged_and_warning_only_refusal_keep_correct_status(self):
        for mode in ('global', 'persist', 'persist_only', 'session'):
            for already_set in (False, True):
                with self.subTest(mode=mode, already_set=already_set):
                    value = NEW if already_set else OLD
                    info = secret_info(global_value=value, session_value=value,
                                       persisted_value=value, numeric=mode == 'persist_only')
                    after = after_state(info, mode, value=value)
                    result, _, _ = self.apply(info, mode, after=after)
                    expected = 'applied_unverified' if mode == 'persist_only' and not already_set else 'unchanged'
                    self.assertEqual(result['status'], expected)
                    self.assert_private(result)
                    self.assert_private(self.records())

    def test_adjusted_and_unverified_results_do_not_echo_secret(self):
        for mode in V.MODES:
            with self.subTest(mode=mode):
                info = secret_info(numeric=mode == 'persist_only')
                result, _, _ = self.apply(info, mode, after=after_state(info, mode, value=OTHER))
                self.assert_private(result)
                result, _, _ = self.apply(info, mode, after_error=ConnectorError('ERROR 1142: ' + OLD))
                self.assertEqual(result['status'], 'applied_unverified')
                self.assert_private(result)
                self.assert_private(self.records())

    def test_unsent_stale_and_policy_results_are_private(self):
        info = secret_info()
        result, _, _ = self.apply(info, live=dict(info, global_value=OTHER))
        self.assertEqual(result['status'], 'refused')
        self.assert_private(result)
        decision = dbops.decide(dbops.classify(V.representative_statement(NAME, 'global')), 'development', True)
        result, conn, _ = self.apply(info, decision=decision)
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(conn.calls, [])
        self.assert_private(result)
        self.assert_private(self.records())

    def test_server_errors_and_transport_exceptions_do_not_echo_values(self):
        for code, status in ((1227, 'denied'), (1231, 'refused_by_server'),
                             (1317, 'interrupted'), (1105, 'failed'), (2013, None)):
            with self.subTest(code=code):
                error = 'ERROR %d (HY000) at line 1: arbitrary diagnostic %s %s' % (code, OLD, NEW)
                if status:
                    result, _, _ = self.apply(secret_info(), write_error=error)
                    self.assertEqual(result['status'], status)
                    self.assert_private(result)
                else:
                    with self.assertRaises(ValueError) as caught:
                        self.apply(secret_info(), write_error=error)
                    self.assert_private(str(caught.exception))
                self.assert_private(self.records())

    def test_unsent_helper_redacts_every_inventory_secret_and_preserves_public_variables(self):
        for name in dbops._SECRET_VARIABLES:
            with self.subTest(name=name):
                result = V.unsent_result(name, 'persist_only', NEW, 'refused', 'Cannot use ' + NEW)
                self.assert_private(result)
        public = V.unsent_result('max_connections', 'global', NEW, 'refused', 'Cannot use ' + NEW)
        self.assertEqual(public['requested'], NEW)
        self.assertEqual(public['message'], 'Cannot use ' + NEW)

    def test_real_string_persist_only_refusal_and_unconfirmed_change_are_private(self):
        info = secret_info()
        v = V.describe(info)
        plan = V.build_plan(v, NEW, 'global')
        conn = ReplayConn([])
        unconfirmed = V.apply_change(conn, plan, plan['expect'])
        self.assertEqual(unconfirmed['status'], 'needs_confirmation')
        self.assert_private(unconfirmed)
        self.assertEqual(conn.calls, [])
        request = dict(plan, mode='persist_only')
        expect = dict(plan['expect'], mode='persist_only')
        with mock.patch.object(V, 'read_variable', return_value=v):
            refused = V.apply_change(conn, request, expect, confirmed=True)
        self.assertEqual((refused['status'], refused['outcome']), ('refused', 'invalid'))
        self.assert_private(refused)
        self.assertEqual(conn.calls, [])

    def test_read_and_audit_failures_never_expose_free_form_diagnostics(self):
        plan = V.build_plan(V.describe(secret_info()), NEW, 'global')
        conn = ReplayConn([])
        with mock.patch.object(V, 'read_variable', side_effect=ConnectorError('ERROR 2013: ' + OLD)):
            with self.assertRaises(ConnectorError) as caught:
                V.apply_change(conn, plan, plan['expect'], confirmed=True)
        self.assert_private(str(caught.exception))
        broken_store = mock.Mock()
        broken_store.audit_insert.side_effect = RuntimeError('disk failure ' + NEW)
        audit = dbops.AuditTrail(broken_store, 'set_global', 'ui')
        with mock.patch.object(V, 'read_variable', return_value=V.describe(secret_info())):
            with self.assertRaises(ValueError) as caught:
                V.apply_change(conn, plan, plan['expect'], confirmed=True, audit=audit)
        self.assert_private(str(caught.exception))
        self.assertEqual(conn.calls, [])

    def test_change_message_helper_redacts_values(self):
        for mode in V.MODES:
            for matched in (True, False):
                with self.subTest(mode=mode, matched=matched):
                    info = secret_info(numeric=mode == 'persist_only')
                    v = V.describe(info)
                    plan = V.build_plan(v, NEW, mode)
                    message = V._change_message(v, plan, after_state(info, mode, OTHER), matched)
                    if mode != 'reset_persist' or matched:
                        self.assert_private(message)


class TestKnnSharing(unittest.TestCase):
    @staticmethod
    def raw(point, access='index', subtype='index_knn_scan'):
        return {
            'operation': 'Index KNN scan on vectors using idx_embedding (embedding <-> %s), limit 5' % point,
            'access_type': access, 'index_access_type': subtype,
            'table_name': 'vectors', 'index_name': 'idx_embedding',
            'lookup_condition': 'embedding <-> ' + point, 'limit': 5,
            'estimated_rows': 5.0, 'actual_rows': 5.0, 'actual_loops': 1,
            'actual_first_row_ms': 0.4, 'actual_last_row_ms': 1.0,
        }

    def test_analysis_redacts_knn_chart_strings_in_both_schemas(self):
        for point in ("'[817263,928374,739485]'", '[817263,928374,739485]'):
            for compact in (False, True):
                with self.subTest(point=point, compact=compact):
                    chart = build_chart_data(parse_explain(json.dumps(self.raw(point))), compact=compact)
                    original = copy.deepcopy(chart)
                    hidden = hide_analysis_literals(chart)
                    for literal in ('817263', '928374', '739485'):
                        self.assertNotIn(literal, json.dumps(hidden))
                    self.assertEqual(chart, original)
                    row, before = hidden['nodes'][0], chart['nodes'][0]
                    for key in ('table', 'index', 'actual_rows', 'estimated_rows', 'limit', 'self_ms', 'metrics'):
                        self.assertEqual(row[key], before[key])

    def test_previews_hide_whole_point_for_released_and_main_access_forms(self):
        # The exponent is cut in the middle of a token. SQL lexing alone
        # mistakes its surviving suffix for an identifier.
        point = '7' * 109 + 'e' + '2' * 90
        for access, subtype in (('index', 'index_knn_scan'), ('index', 'INDEX_KNN_SCAN'),
                                ('index_distance_scan', ''), ('INDEX_DISTANCE', '')):
            with self.subTest(access=access, subtype=subtype):
                root = parse_explain(json.dumps(self.raw(point, access, subtype)))
                original = copy.deepcopy(root)
                hidden = hide_parsed_plan_literals(root)
                self.assertEqual(hidden['details']['lookup_condition'], 'embedding <-> ?')
                self.assertNotIn('777777', json.dumps(hidden))
                self.assertNotIn('characters)', json.dumps(hidden))
                self.assertEqual(root, original)

    def test_analysis_previews_remove_all_repeated_copies(self):
        point = '7' * 109 + 'e' + '2' * 90
        for access, subtype in (('index', 'index_knn_scan'), ('index_distance_scan', '')):
            for compact in (False, True):
                with self.subTest(access=access, compact=compact):
                    chart = build_chart_data(parse_explain(json.dumps(self.raw(point, access, subtype))), compact=compact)
                    hidden = hide_analysis_literals(chart)
                    self.assertEqual(hidden['nodes'][0]['lookup_condition'], 'embedding <-> ?')
                    for text in ('777777', 'characters)', 'e\u2026'):
                        self.assertNotIn(text, json.dumps(hidden, ensure_ascii=False))

    def test_preview_redaction_does_not_depend_on_subtype_metadata(self):
        # Sidecars can omit the access metadata, including a newer server's
        # KNN spelling. The bounded preview itself is sufficient to hide it.
        preview = 'embedding <-> 777777e\u2026 (200 characters)'
        for access in ('knn_scan', 'KNN_SCAN', None):
            raw = {'access_type': access, 'lookup_condition': preview, 'limit': 5}
            hidden = hide_plan_literals(raw)
            self.assertEqual(hidden['lookup_condition'], 'embedding <-> ?')
            self.assertEqual(hidden['limit'], 5)


if __name__ == '__main__':
    unittest.main()
