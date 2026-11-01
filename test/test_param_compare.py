"""Typed comparisons and local snapshots, without a live database or real HOME."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from myflames import param_compare as P, compare_cli
from myflames.connector import ConnectorError
from myflames.workspace_store import WorkspaceStore
from test_chart_data import validate


def snapshot(items=(), engine='mysql', host='db1', complete=True):
    return {'schema_version': P.SNAPSHOT_SCHEMA, 'server': {'host': host, 'port': 3306, 'engine': engine,
            'flavor': engine, 'server_product': 'MariaDB' if engine == 'mariadb' else 'MySQL', 'server_version': 'test'},
            'captured_at': '2026-10-27T12:00:00+00:00', 'label': '', 'complete': complete, 'notes': [], 'items': list(items)}


def item(name, value, kind='integer', default=None, **kw):
    return dict(name=name, value=value, type=kind, default=default, **kw)


class NormalizeTest(unittest.TestCase):
    def test_boolean_spellings(self):
        for truth in (('ON', '1', 'TRUE', 'true ', True), ('OFF', '0', 'FALSE', 'false ', False)):
            self.assertEqual(len({P.normalize(v, 'boolean') for v in truth}), 1)
        self.assertNotEqual(P.normalize('ON', 'boolean'), P.normalize('OFF', 'boolean'))

    def test_sizes_exact_and_suffixes(self):
        for raw in ('1M', '1 MB', '1MiB', '1024k', '1048576', '1.0m '):
            self.assertEqual(P.normalize(raw, 'bytes'), P.normalize('1048576', 'bytes'), raw)
        self.assertNotEqual(P.normalize('1m', 'integer'), P.normalize('1048576', 'integer'))
        self.assertEqual(P.normalize(str(2**64 - 1), 'integer')[1], 2**64 - 1)
        self.assertEqual(P.normalize('001', 'integer'), P.normalize('1', 'integer'))

    def test_enum_case_trailing_spaces_and_sensitive_strings(self):
        self.assertEqual(P.normalize('ON '), P.normalize('ON'))
        self.assertEqual(P.normalize('FULL  ', 'enum'), P.normalize('full', 'enum'))
        self.assertNotEqual(P.normalize('x  ', 'string'), P.normalize('x', 'string'))
        self.assertNotEqual(P.normalize('/Data', 'string'), P.normalize('/data', 'string'))
        self.assertNotEqual(P.normalize(' leading', 'string'), P.normalize('leading', 'string'))
        self.assertNotEqual(P.normalize(None), P.normalize('NULL'))

    def test_string_paths_keep_exact_whitespace_in_status_and_defaults(self):
        name = 'caching_sha2_password_private_key_path'
        source = snapshot([item(name, 'private_key.pem', 'string', 'private_key.pem ')])
        target = snapshot([item(name, 'private_key.pem ', 'string', 'private_key.pem ')])
        row = P.compare(source, target)['items'][0]
        self.assertEqual(row['status'], 'different')
        self.assertFalse(row['source_at_default'])
        self.assertTrue(row['target_at_default'])
        for value in (' x', 'x ', 'x\t', 'x\n'):
            self.assertNotEqual(P.normalize(value, 'string'), P.normalize('x', 'string'))

    def test_every_status_defaults_group_and_identity_folding(self):
        a = snapshot([item('same', '1', default='01'), item('diff', '1'), item('source_only', '1'),
                      item('server_id', '1'), item('size', '1M', 'bytes', '1048576', group='buffers'),
                      item('wsrep_sst_auth', 'super-secret')])
        b = snapshot([item('same', '01', default='2'), item('diff', '2'), item('target_only', '1'),
                      item('server_id', '2'), item('size', '1048576', 'bytes', '2M'), item('wsrep_sst_auth', 'new-secret')])
        report = P.compare(a, b)
        rows = {r['name']: r for r in report['items']}
        self.assertEqual(set(r['status'] for r in rows.values()), set(P.STATUS_TEXT))
        self.assertTrue(rows['same']['source_at_default'])
        self.assertFalse(rows['same']['target_at_default'])
        self.assertIsNone(rows['diff']['source_at_default'])
        self.assertEqual(rows['size']['group'], 'buffers')
        self.assertTrue(rows['server_id']['identity'])
        self.assertNotIn('server_id', compare_cli.format_compare(report))
        self.assertNotIn('same ', compare_cli.format_compare(report))
        self.assertIn('server_id', compare_cli.format_compare(report, True))
        self.assertIn('same', compare_cli.format_compare(report, True))
        self.assertNotIn('super-secret', json.dumps(report))
        self.assertEqual(rows['size']['source']['value'], '1M')
        self.assertEqual(json.loads(json.dumps(report)), report)

    def test_engine_mismatch_and_partial_reads(self):
        a = snapshot([item('only', '1')])
        report = P.compare(a, snapshot([], 'mariadb', complete=False))
        self.assertEqual(report['items'][0]['status'], 'not_compared')
        self.assertTrue(any('different engines' in n for n in report['notes']))
        report = P.compare(P.failed_side('Access denied'), a)
        self.assertEqual(report['source']['error'], 'Access denied')
        self.assertEqual(report['items'][0]['target']['value'], '1')
        self.assertEqual(report['items'][0]['status'], 'not_compared')
        self.assertFalse(report['identical'])
        self.assertFalse(P.compare(P.failed_side('error'), P.failed_side('error'))['identical'])
        self.assertTrue(P.compare(a, a)['identical'])

    def test_truncated_values_not_equal_even_when_same_prefix(self):
        a = snapshot([item('long', 'prefix', 'string', truncated=True)])
        self.assertEqual(P.compare(a, a)['items'][0]['status'], 'not_compared')

    def test_file_validation_and_scrubbing(self):
        a = snapshot([item('wsrep_sst_auth', 'root:secret', 'string', 'other'),
                      item('init_connect', "SET PASSWORD = 'not-this-form'", 'string')])
        a['password'] = 'secret'
        a['server']['password'] = 'secret'
        clean = P.validate_snapshot(a)
        self.assertNotIn('root:secret', json.dumps(clean))
        self.assertNotIn('password', clean['server'])
        self.assertEqual(clean['items'][0]['default'], '[redacted]')
        for changed in ({'schema_version': 'wrong'}, {'items': a['items'] * 2}, {'items': [item('x', '1', source={'password':'leak'})]}, {'items':[{'name':'missing_value'}]}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                P.validate_snapshot(dict(a, **changed))

    def test_capture_keeps_server_identity_and_dynamic_metadata(self):
        catalog = {'complete': True, 'items': [item('max_connections', '151', apply_type='dynamic', checks=['max'])]}
        report = {'engine': 'mysql', 'flavor': 'percona', 'server_product': 'Percona Server',
                  'server_version': '8.4.11-11', 'parameters': catalog, 'notes': [],
                  'checks': [{'key': 'max', 'group': 'connections'}]}
        with mock.patch('myflames.config_review.config_report', return_value=report):
            result = P.capture(mock.Mock(host='db1', port=3306), {'host':'db1','port':3306})
        self.assertEqual(result['server']['server_version'], '8.4.11-11')
        self.assertEqual(result['server']['flavor'], 'percona')
        self.assertTrue(result['items'][0]['dynamic'])
        self.assertEqual(result['items'][0]['group'], 'connections')

    def test_capture_notes_come_from_parameters_not_config_checks(self):
        notes = ['Could not read performance_schema.variables_info: Access denied',
                 'Some parameter values were truncated.']
        for engine in ('mysql', 'mariadb'):
            with self.subTest(engine=engine):
                catalog = {'complete': False, 'notes': notes, 'items': [item('max_connections', '151')]}
                report = {'engine': engine, 'server_version': 'test', 'parameters': catalog,
                          'notes': ['Counters run from server start. The connection peak restarts with FLUSH STATUS.',
                                    'Statuses are rules of thumb, not verdicts.',
                                    'Could not read unrelated health counters.'] + notes}
                with mock.patch('myflames.config_review.config_report', return_value=report):
                    result = P.capture(mock.Mock(host='db1', port=3306))
                self.assertEqual(result['notes'], notes)
                self.assertFalse(result['complete'])
                comparison = P.compare(result, result)
                self.assertEqual(comparison['notes'], ['Source: ' + n for n in notes] +
                                 ['Target: ' + n for n in notes])

    def test_legacy_config_boilerplate_removed_but_read_warnings_kept(self):
        value = snapshot()
        relevant = ['Could not read global parameters: Access denied',
                    'The parameter inventory is incomplete.', 'A parameter value was truncated.']
        value['notes'] = ['Counters run from server start. The connection peak restarts with FLUSH STATUS.',
                          'Statuses are rules of thumb, not verdicts.',
                          'The server started 1 s ago, so the counters since startup are still small.'] + relevant
        self.assertEqual(P.validate_snapshot(value)['notes'], relevant)
        self.assertEqual(P.compare(value, value)['notes'], ['Source: ' + n for n in relevant] +
                         ['Target: ' + n for n in relevant])

    def test_total_read_budget(self):
        conn = mock.Mock(query_timeout=30)
        with mock.patch.object(P.time, 'monotonic', side_effect=[0, 110, 121]):
            bound = P.BoundedConnection(conn)
            bound.run('SHOW GLOBAL VARIABLES')
            self.assertEqual(conn.run.call_args[1]['timeout'], 10)
            with self.assertRaises(ConnectorError):
                bound.run('SHOW GLOBAL VARIABLES')
        self.assertEqual(conn.run.call_count, 1)


class StoreTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.dict(os.environ, {'HOME': tmp.name, 'USERPROFILE': tmp.name, 'MYFLAMES_SECRET_BACKEND':'vault'})
        patch.start(); self.addCleanup(patch.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        self.store = WorkspaceStore()

    def test_save_list_show_delete_and_per_server_numbers(self):
        self.assertEqual(self.store.parameter_snapshot_list()['items'], [])
        a = self.store.parameter_snapshot_save(snapshot([item('x', '1')]), 'first')
        b = self.store.parameter_snapshot_save(snapshot([item('x', '2')]), 'second')
        c = self.store.parameter_snapshot_save(snapshot([], host='db2'), 'another server')
        self.assertEqual([a['number'], b['number'], c['number']], [1,2,1])
        self.assertEqual(self.store.parameter_snapshot_get(a['id'])['snapshot']['items'][0]['value'], '1')
        self.assertEqual(self.store.parameter_snapshot_find('latest', a['source_key'])['id'], b['id'])
        self.assertEqual(len(self.store.parameter_snapshot_list(a['source_key'])['items']), 2)
        self.assertTrue(self.store.parameter_snapshot_delete(a['id'])['deleted'])
        self.assertIsNone(self.store.parameter_snapshot_get(a['id']))
        with self.assertRaises(ValueError):
            self.store.parameter_snapshot_find(a['id'])
        with self.assertRaises(ValueError):
            self.store.parameter_snapshot_delete(a['id'])

    def test_invalid_save_does_not_write_and_secrets_not_persisted(self):
        with self.assertRaises(ValueError):
            self.store.parameter_snapshot_save(snapshot([item('bad name', '1')]))
        self.assertFalse(self.store.path.exists())
        saved = self.store.parameter_snapshot_save(snapshot([item('wsrep_sst_auth', 'needle-password', 'string')]))
        self.assertNotIn('needle-password', json.dumps(self.store.parameter_snapshot_get(saved['id'])))
        self.assertNotIn(b'needle-password', self.store.path.read_bytes())

    def test_schema_validation_roundtrip(self):
        docs = Path(__file__).resolve().parent.parent / 'docs' / 'schemas'
        value = P.validate_snapshot(snapshot([item('log_bin','ON','boolean','OFF')]))
        report = P.compare(value, value)
        saved = self.store.parameter_snapshot_save(value)
        for name, data in [('parameter-snapshot-1.0',value), ('parameter-compare-1.0',report),
                           ('parameter-snapshot-list-1.0',self.store.parameter_snapshot_list())]:
            schema = json.loads((docs / (name+'.json')).read_text())
            validate(data, schema)
            with self.assertRaises(AssertionError):
                validate(dict(data, unexpected='field'), schema)
        self.assertEqual(self.store.parameter_snapshot_get(saved['id'])['snapshot'],value)

class ValidationEdgeTest(unittest.TestCase):
    def test_password_plugin_settings_compared_and_sst_auth_stays_redacted(self):
        settings = [item('caching_sha2_password_auto_generate_rsa_keys', 'ON', 'boolean'),
                    item('caching_sha2_password_digest_rounds', '5000'),
                    item('caching_sha2_password_private_key_path', 'private_key.pem', 'string'),
                    item('caching_sha2_password_public_key_path', '/etc/mysql/public_key.pem', 'string')]
        a = snapshot(settings + [item('wsrep_sst_auth', 'root:secret', 'string')])
        b = copy.deepcopy(a)
        for row, value in zip(b['items'], ('OFF', '6000', '/different/private_key.pem', 'public_key.pem', 'other:secret')):
            row['value'] = value
        rows = {r['name']: r for r in P.compare(a, b)['items']}
        for setting in settings:
            row = rows[setting['name']]
            self.assertEqual(row['status'], 'different')
            self.assertEqual(row['source']['value'], setting['value'])
            self.assertFalse(row['source'].get('redacted', False))
        self.assertEqual(rows['wsrep_sst_auth']['status'], 'not_compared')
        self.assertEqual(rows['wsrep_sst_auth']['source']['value'], '[redacted]')
        for engine in ('mysql', 'mariadb'):
            masked = snapshot([item('wsrep_sst_auth', '********', 'string')], engine)
            self.assertTrue(P.validate_snapshot(masked)['items'][0]['redacted'])

    def test_credential_values_redacted_without_name_guessing(self):
        for name, value in (('wsrep_sst_auth', 'root:secret'), ('report_password', 'secret'),
                            ('s3_secret_key', 'secret'), ('keyring_encrypted_file_password', 'secret'),
                            ('custom_setting', 'alice:secret'),
                            ('custom_setting', 'https://alice:secret@example.com/path')):
            with self.subTest(name=name, value=value):
                clean = P.validate_snapshot(snapshot([item(name, value, 'string', value)]))
                self.assertTrue(clean['items'][0]['redacted'])
                self.assertNotIn('secret', json.dumps(clean['items'][0].get('values')))
                self.assertEqual(clean['items'][0]['value'], '[redacted]')
                self.assertEqual(clean['items'][0]['default'], '[redacted]')
                self.assertEqual(P.compare(clean, clean)['items'][0]['status'], 'not_compared')
        for name, value in (('password_history', '5'), ('custom_password_policy', 'ON'),
                            ('custom_token_size', '100'), ('ssl_key', '/private/key.pem'),
                            ('custom_setting', r'C:\\mysql\\key.pem'),
                            ('custom_setting', 'https://example.com/path'),
                            ('custom_setting', '127.0.0.1:3306')):
            with self.subTest(name=name, value=value):
                clean = P.validate_snapshot(snapshot([item(name, value, 'string')]))
                self.assertEqual(clean['items'][0]['value'], value)
                self.assertFalse(clean['items'][0].get('redacted', False))

    def test_secret_defaults_and_allowed_values_cannot_leak(self):
        for setting in (item('wsrep_sst_auth', 'masked', 'string', 'numeric-password', values=['allowed-secret']),
                        item('custom_setting', 'ordinary', 'string', 'alice:default-secret'),
                        item('custom_setting', 'ordinary', 'string', values=['https://u:allowed-secret@db/path'])):
            with self.subTest(setting=setting):
                clean = P.validate_snapshot(snapshot([setting]))
                encoded = json.dumps(clean)
                for secret in ('numeric-password', 'allowed-secret', 'default-secret'):
                    self.assertNotIn(secret, encoded)
                self.assertTrue(clean['items'][0]['redacted'])
                self.assertEqual(P.compare(clean, clean)['items'][0]['status'], 'not_compared')


    def test_types_times_sensitive_defaults_and_credentials_inside_sql(self):
        for changes in ({'notes':'not a list'}, {'complete':'true'}, {'captured_at':'2026-10-27T12:00:00'},
                        {'server':{'host':'db1','port':False,'engine':'mysql'}}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                P.validate_snapshot(dict(snapshot(),**changes))
        a=P.validate_snapshot(snapshot([item('init_connect',"SET PASSWORD = 'needle-secret'",'string')]))
        self.assertNotIn('needle-secret',json.dumps(a))
        self.assertTrue(a['items'][0]['redacted'])
        self.assertEqual(P.compare(a,a)['items'][0]['status'],'not_compared')
        self.assertFalse(P.compare(snapshot(complete=False),snapshot(complete=False))['identical'])

    def test_security_policy_numbers_and_enum_words_are_not_secrets(self):
        value=P.validate_snapshot(snapshot([item('password_history','5','integer'),
            item('validate_password.policy','MEDIUM','enum'),item('audit_log_encryption_password','secret','string')]))
        rows={i['name']:i for i in value['items']}
        self.assertEqual(rows['password_history']['value'],'5')
        self.assertEqual(rows['validate_password.policy']['value'],'MEDIUM')
        self.assertNotIn('redacted',rows['password_history'])
        self.assertTrue(rows['audit_log_encryption_password']['redacted'])
