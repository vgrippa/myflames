"""Regressions from CLI live testing; deterministic inputs, no live server."""
import contextlib
import datetime
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from myflames import accounts_cli, dbops, editor_session, mcp_server, replication, server_checks, sql_cli
from myflames import variables as V
from myflames.perf_common import Server
from myflames.workspace_store import WorkspaceStore
from test_variables import mysql_variable, mariadb_variable


class LiveCLIRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patch = mock.patch.dict(os.environ, {'HOME': self.temp.name, 'USERPROFILE': self.temp.name,
                                            'MYFLAMES_SECRET_BACKEND': 'vault'})
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)

    def variables(self):
        return (mysql_variable('long_query_time', global_value='0.200000', session_value='0.200000'),
                mariadb_variable('long_query_time', type='DOUBLE', min='0', max='31536000',
                                 scope='GLOBAL & SESSION', global_value='0.200000', session_value='0.200000'))

    def test_double_literals_are_plain_exact_decimals(self):
        for variable in self.variables():
            for raw, expected in (('10', '10'), ('10.00', '10'), ('0.00000001', '0.00000001'),
                                  ('0.123456789012345678901234567890', '0.12345678901234567890123456789')):
                with self.subTest(engine=variable['engine'], value=raw):
                    result = V.normalize_value(variable, raw)
                    self.assertEqual(result['problems'], [])
                    self.assertEqual(result['literal'], expected)
                    plan = V.build_plan(variable, raw, 'global')
                    self.assertEqual(plan['statement'], 'SET GLOBAL long_query_time = ' + expected)
                    self.assertEqual(plan['config_line'], 'long_query_time = ' + expected)

    def test_setting_current_value_is_successful_unchanged(self):
        for variable in self.variables():
            with self.subTest(engine=variable['engine']):
                after = {'engine': variable['engine'], 'server_version': variable['server_version'],
                         'server_identity': variable['server_identity'], 'global': '0.200000', 'global_read': True}
                conn = mock.Mock()
                conn.run.return_value = ""
                store = WorkspaceStore(Path(self.temp.name) / (variable['engine'] + '.sqlite3'))
                audit = dbops.AuditTrail(store, 'set_global', 'cli')
                with mock.patch.object(V, 'read_variable', return_value=variable), \
                        mock.patch.object(V, '_after', return_value=after):
                    code, result = V.change_cli(conn, 'long_query_time', '0.2', 'global',
                                                assume_yes=True, quiet=True, audit=audit)
                self.assertEqual(result['status'], 'unchanged')
                self.assertEqual(code, 0)
                self.assertIn('unchanged', result['message'])
                self.assertEqual(store.audit_records()[0]['status'], 'ok')
                self.assertEqual(store.audit_records()[0]['outcome'], 'unchanged')

    def test_plain_number_is_sent_and_audited_on_both_engines(self):
        for variable in self.variables():
            with self.subTest(engine=variable['engine']):
                after = {'engine': variable['engine'], 'server_version': variable['server_version'],
                         'server_identity': variable['server_identity'], 'global': '10.000000'}
                conn = mock.Mock()
                conn.run.return_value = ''
                store = WorkspaceStore(Path(self.temp.name) / (variable['engine'] + '.sqlite3'))
                audit = dbops.AuditTrail(store, 'set_global', 'cli')
                with mock.patch.object(V, 'read_variable', return_value=variable), \
                        mock.patch.object(V, '_after', return_value=after):
                    code, result = V.change_cli(conn, 'long_query_time', '10', 'global',
                                                assume_yes=True, quiet=True, audit=audit)
                self.assertEqual((code, result['status']), (0, 'changed'))
                statement = V.TAG + 'SET GLOBAL long_query_time = 10'
                self.assertIn(mock.call(statement, extra_flags=list(dbops.WRITE_CLIENT_FLAGS)), conn.run.call_args_list)
                self.assertEqual(store.audit_records()[0]['statements'], [statement])
                self.assertEqual(result['plan']['config_line'], 'long_query_time = 10')

    def test_persist_scope_is_not_unchanged_when_only_running_value_matches(self):
        for present, wanted_status in ((False, 'changed'), (True, 'unchanged')):
            variable = mysql_variable('long_query_time', global_value='0.200000',
                                      persisted={'present': present, 'value': '0.2' if present else None, 'read': True})
            after = {'engine': variable['engine'], 'server_version': variable['server_version'],
                     'server_identity': variable['server_identity'], 'global': '0.200000',
                     'persisted': '0.2', 'persisted_state': {'present': True, 'value': '0.2', 'read': True}}
            conn = mock.Mock()
            conn.run.return_value = ''
            with mock.patch.object(V, 'read_variable', return_value=variable), \
                    mock.patch.object(V, '_after', return_value=after):
                code, result = V.change_cli(conn, 'long_query_time', '0.2', 'persist', assume_yes=True, quiet=True)
            self.assertEqual((code, result['status']), (0, wanted_status))

    def test_mariadb_persist_and_plan_use_cli_terms(self):
        variable = self.variables()[1]
        plan = V.build_plan(variable, '10', 'persist')
        text = V.format_change(dict(V._result(plan, 'refused', 'Not sent.'), plan=plan))
        self.assertIn('MariaDB has no SET PERSIST', text)
        self.assertIn('myflames config set long_query_time 10', text)
        self.assertNotIn('Now only', text)
        ready = V.build_plan(variable, '10', 'global')
        self.assertIn('SET GLOBAL', V.describe_plan(ready)[0])
        self.assertNotIn('Now only', '\n'.join(V.describe_plan(ready)))

    def test_typed_numeric_sql_formats_preserve_text_and_precision(self):
        result = {'type': 'rows', 'statement': 0,
                  'columns': [{'name': 'n', 'type': 'INT'}, {'name': 's', 'type': 'VARCHAR(20)'},
                              {'name': 'big', 'type': 'BIGINT UNSIGNED'}, {'name': 'd', 'type': 'DECIMAL(40,20)'},
                              {'name': 'f', 'type': 'DOUBLE'}, {'name': 'unknown'}],
                  'rows': [['10', '10', '18446744073709551615', '0.12345678901234567890', '1.25', '10']]}
        body = sql_cli._envelope('complete', {'results': [result]})
        for fmt in ('table', 'insert', 'csv', 'json'):
            with self.subTest(format=fmt):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    sql_cli._emit(body, fmt, True, None)
                text = out.getvalue()
                if fmt == 'table':
                    self.assertIn('10  "10"', text)
                    self.assertNotIn('"18446744073709551615"', text)
                    self.assertIn('"10"', text)
                elif fmt == 'json':
                    self.assertEqual(json.loads(text)['results'][0]['rows'][0],
                                     [10, '10', '18446744073709551615', '0.12345678901234567890', 1.25, '10'])
                elif fmt == 'insert':
                    self.assertIn("(10, '10', 18446744073709551615, 0.12345678901234567890, 1.25, '10')", text)
                else:
                    self.assertIn('\r\n10,10,18446744073709551615,0.12345678901234567890,1.25,10\r\n', text)
        self.assertEqual(body['results'][0]['rows'][0][0], '10', 'formatting must not mutate the result')

    def test_numeric_json_keeps_unsafe_values_and_null(self):
        result = {'type': 'rows', 'statement': 0,
                  'columns': [{'name': 'n', 'type': 'INTEGER'}, {'name': 'f', 'type': 'DOUBLE'},
                              {'name': 'text', 'type': 'VARCHAR'}],
                  'rows': [['9007199254740992', '1e999', '123'], [None, '1e-999', 'NULL'],
                           ['12x', None, ''], ['-10', '-1.25', '10']]}
        body = sql_cli._envelope('complete', {'results': [result]})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            sql_cli._emit(body, 'json', True, None)
        self.assertEqual(json.loads(out.getvalue())['results'][0]['rows'],
                         [['9007199254740992', '1e999', '123'], [None, '1e-999', 'NULL'],
                          ['12x', None, ''], [-10, -1.25, '10']])

    def test_mcp_accepts_capture_and_preserves_engine(self):
        for filename in ('mysql-explain-hash-join.json', 'fixtures/mariadb-11.4-003-table-scan-products.json'):
            path = Path(__file__).parent / filename
            raw = path.read_text()
            capture = json.dumps({'schema_version': 'capture-1.0', 'raw': raw})
            with self.subTest(file=path.name):
                bare = mcp_server.analyze_plan_tool(raw)
                wrapped = mcp_server.analyze_plan_tool(capture)
                self.assertEqual(wrapped['plan_summary'], bare['plan_summary'])
                self.assertEqual(wrapped['source']['engine'], bare['source']['engine'])
                self.assertEqual(mcp_server.digest_plan_tool(capture), mcp_server.digest_plan_tool(raw))
        for bad in ({'raw': ''}, {'raw': 'not JSON'}, {'raw': 10}, {'mode': 'invalid', 'raw': raw}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                mcp_server.analyze_plan_tool(json.dumps(dict(bad, schema_version='capture-1.0')))

    def test_unknown_version_support_uses_plain_wording(self):
        for version in ('12.3.1-MariaDB', '13.0.2-MariaDB', '26.7.0'):
            check = server_checks._version_support(Server({'version': version}, {}), datetime.date(2026, 10, 19))
            self.assertEqual(check['status'], 'unknown')
            self.assertIn('not been verified', check['why'])
            for internal in ('gamma', 'fixture', 'reviewed tree', 'tags', 'MYSQL_VERSION'):
                self.assertNotIn(internal, check['why'])

    def test_replication_text_includes_received_gtids(self):
        for engine, key, label, value in (('mysql', 'retrieved_gtid_set', 'Retrieved_Gtid_Set', 'uuid:1-200'),
                                          ('mariadb', 'gtid_io_pos', 'Gtid_IO_Pos', '0-1-200')):
            report = {'available': True, 'replica': True, 'engine': engine, 'channels': [
                {'name': 'source', 'status': 'problem', 'io': {'running': 'Yes'}, 'sql': {'running': 'No'},
                 'lag_s': None, 'status_why': [], 'lag_notes': [], 'position': {key: value}}]}
            self.assertIn(label + ': ' + value, replication.format_text(report))
            report['channels'][0]['position'] = {}
            self.assertNotIn(label, replication.format_text(report))
