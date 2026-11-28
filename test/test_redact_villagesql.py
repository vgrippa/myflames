"""VillageSQL vsql_rest.jwt_secret redaction across the shared inventory and all surfaces.

Covers:
1. Shared inventory membership, prefilter/anchor/full-regex equivalence and near-misses.
2. extensions.py using dbops.sensitive(name) with no local secret list.
3. Every variable-displaying and SQL-persisting surface (Parameters parameters-1.0,
   single-variable variable-1.0 read and plan, parameter snapshots, parameter compare
   and CLI/text exports, topology compare/rolling refusal, Extensions report,
   editor history, saved queries, session restore, audit log and audit JSON/CSV/text exports).
4. Abuse cases (50 KB values, values with redaction markers/quotes/newlines,
   double assignments, and mentions inside comments/strings).
"""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))

from _perf_replay import FakeFactory, ReplayConn as PerfReplayConn, batch  # noqa: E402
from _reviewed_replay import (  # noqa: E402
    ReplayConn as ReviewedReplayConn,
    evidence_body,
    evidence_call,
    evidence_options,
)
from myflames import (  # noqa: E402
    cli,
    compare_api,
    compare_cli,
    config_review,
    dbops,
    editor_session,
    extensions,
    param_compare,
    parameters,
    topology_change,
    ui,
    variables,
    workspace_cli,
    workspace_store,
)
from myflames.perf_common import Reader, Server  # noqa: E402
from myflames.workspace_store import WorkspaceStore  # noqa: E402
from test_extensions import Connection as ExtensionsConnection, SCHEMA as EXT_SCHEMA, validate as validate_ext  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
VSQL_VERSION = '8.4.11-villagesql-0.0.8'
VSQL_CODEBASE = 'mysql-8.4_0.0.8'
JWT_SECRET = 'vsql-hs256-super-secret-value'


def _all_regex_spans(pattern, text):
    """Collect every sequential (start, secret_start, end) from pattern.search."""
    found = []
    cursor = 0
    while cursor < len(text):
        match = pattern.search(text, cursor)
        if match is None:
            break
        found.append((match.start(), match.start('secret'), match.end()))
        cursor = match.start('secret') + 1
    return found


def _all_prefilter_spans(pattern, text):
    """Collect every sequential (start, secret_start, end) via the ASCII prefilters."""
    folded = text.lower()
    if not dbops._may_have_secret_variable_literals(folded):
        return []
    search = dbops._make_inventory_literal_search(pattern, text, folded)
    if search is None:
        return []
    found = []
    cursor = 0
    while cursor < len(text):
        match = search(text, cursor)
        if match is None:
            break
        found.append((match.start(), match.start('secret'), match.end()))
        cursor = match.start('secret') + 1
    return found


def _villagesql_snapshot(items, host='vsql-db1', port=3306):
    return {
        'schema_version': param_compare.SNAPSHOT_SCHEMA,
        'server': {
            'host': host,
            'port': port,
            'engine': 'mysql',
            'flavor': 'villagesql',
            'server_product': 'VillageSQL',
            'server_version': VSQL_VERSION,
        },
        'captured_at': '2026-11-27T10:00:00+00:00',
        'label': 'villagesql',
        'complete': True,
        'notes': [],
        'items': items,
    }


def _param_item(name, value, kind='string', default=None, **extra):
    row = {'name': name, 'value': value, 'default': default, 'type': kind}
    row.update(extra)
    return row


class TempHomeTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.dict(os.environ, {
            'HOME': self.temp.name,
            'USERPROFILE': self.temp.name,
            'MYFLAMES_SECRET_BACKEND': 'vault',
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        self.store = WorkspaceStore()
        self.workspace = types.SimpleNamespace(store=self.store)


class TestInventoryAndPrefilters(unittest.TestCase):
    def test_inventory_membership_and_live_fixture_variables(self):
        self.assertIn('vsql_rest.jwt_secret', dbops._SECRET_VARIABLES)
        self.assertIs(param_compare._SECRET_VARIABLES, dbops._SECRET_VARIABLES)
        self.assertIs(param_compare.sensitive, dbops.sensitive)
        self.assertTrue(dbops.sensitive('vsql_rest.jwt_secret'))
        self.assertTrue(dbops.sensitive('VSQL_REST.JWT_SECRET'))
        self.assertTrue(dbops.sensitive('VsQl_ReSt.JwT_sEcReT'))

        # Verify against the real recorded VillageSQL extension variable lists:
        # only vsql_rest.jwt_secret is secret; public keys and cert/key file paths are not.
        fixture_files = sorted((REPO / 'test' / 'fixtures' / 'villagesql')
                               .glob('live-extension-variables-*.txt'))
        self.assertTrue(fixture_files)
        observed = set()
        for path in fixture_files:
            for line in path.read_text(encoding='utf-8').splitlines():
                if '\t' in line:
                    observed.add(line.split('\t', 1)[0].strip().lower())
        self.assertIn('vsql_rest.jwt_secret', observed)
        for non_secret in ('vsql_rest.jwt_public_key', 'vsql_rest.ssl_key',
                           'vsql_rest.ssl_cert', 'vsql_oauth2.public_key'):
            self.assertIn(non_secret, observed)
            self.assertFalse(dbops.sensitive(non_secret), non_secret)
        self.assertEqual({name for name in observed if dbops.sensitive(name)},
                         {'vsql_rest.jwt_secret'})
        for near_miss in ('vsql_rest.jwt_secretx', 'xvsql_rest.jwt_secret', 'jwt_secret'):
            self.assertFalse(dbops.sensitive(near_miss), near_miss)

    def test_prefilter_and_full_regex_agree_on_spelling_table(self):
        secret = JWT_SECRET
        positive_cases = (
            ("plain", "vsql_rest.jwt_secret = '%s'" % secret),
            ("colon_equals", "vsql_rest.jwt_secret := '%s'" % secret),
            ("double_quoted_value", 'vsql_rest.jwt_secret = "%s"' % secret),
            ("backtick_parts", "`vsql_rest`.`jwt_secret` = '%s'" % secret),
            ("backtick_whole", "`vsql_rest.jwt_secret` = '%s'" % secret),
            ("dquote_parts", '"vsql_rest"."jwt_secret" = \'%s\'' % secret),
            ("dquote_whole", '"vsql_rest.jwt_secret" = \'%s\'' % secret),
            ("backtick_left_only", "`vsql_rest`.jwt_secret = '%s'" % secret),
            ("backtick_right_only", "vsql_rest.`jwt_secret` = '%s'" % secret),
            ("spaces_around_dot", "vsql_rest . jwt_secret = '%s'" % secret),
            ("newlines_around_dot", "vsql_rest\n.\njwt_secret = '%s'" % secret),
            ("backtick_and_spaces", "`vsql_rest` . `jwt_secret` := '%s'" % secret),
            ("dquote_and_spaces", '"vsql_rest" . "jwt_secret" = \'%s\'' % secret),
            ("at_global", "@@GLOBAL.vsql_rest.jwt_secret = '%s'" % secret),
            ("at_global_backticks", "@@GLOBAL.`vsql_rest`.`jwt_secret` = '%s'" % secret),
            ("at_global_dquotes", '@@GLOBAL."vsql_rest"."jwt_secret" = \'%s\'' % secret),
            ("at_global_spaced", "@@ GLOBAL . `vsql_rest` . `jwt_secret` = '%s'" % secret),
            ("at_session", "@@SESSION.vsql_rest.jwt_secret = '%s'" % secret),
            ("at_persist", "@@PERSIST.vsql_rest.jwt_secret = '%s'" % secret),
            ("at_persist_only", "@@PERSIST_ONLY.vsql_rest.jwt_secret = '%s'" % secret),
            ("at_local", "@@LOCAL.vsql_rest.jwt_secret = '%s'" % secret),
            ("at_bare", "@@vsql_rest.jwt_secret = '%s'" % secret),
            ("scope_global", "GLOBAL.vsql_rest.jwt_secret = '%s'" % secret),
            ("scope_persist", "PERSIST.vsql_rest.jwt_secret = '%s'" % secret),
            ("set_persist", "SET PERSIST vsql_rest.jwt_secret = '%s'" % secret),
            ("set_global", "SET GLOBAL vsql_rest.jwt_secret = '%s'" % secret),
            ("set_persist_only", "SET PERSIST_ONLY vsql_rest.jwt_secret = '%s'" % secret),
            ("mixed_case_plain", "SET PERSIST VsQl_ReSt.JwT_sEcReT = '%s'" % secret),
            ("upper_case_backtick", "SET GLOBAL `VSQL_REST` . `JWT_SECRET` := '%s'" % secret),
            ("hex_literal", "vsql_rest.jwt_secret = 0x736563726574"),
            ("charset_introducer", "vsql_rest.jwt_secret = _utf8mb4'%s'" % secret),
            ("national_introducer", "vsql_rest.jwt_secret = N'%s'" % secret),
            ("preceded_by_other_vsql_rest",
             "vsql_rest.port = 3000; vsql_rest.jwt_secret = '%s'" % secret),
            ("preceded_by_jwt_public_key",
             "SET GLOBAL vsql_rest.jwt_public_key = '/pub.pem'; SET GLOBAL vsql_rest.jwt_secret = '%s'" % secret),
            ("telemetry_spaced_regression",
             "@@GLOBAL.`telemetry` . `otel_exporter_otlp_traces_headers` = '%s'" % secret),
        )
        for label, text in positive_cases:
            with self.subTest(positive=label):
                regex_spans = _all_regex_spans(dbops._SECRET_VARIABLE_LITERAL, text)
                self.assertTrue(regex_spans, text)
                self.assertTrue(dbops._may_have_secret_variable_literals(text.lower()), text)
                prefilter_spans = _all_prefilter_spans(dbops._SECRET_VARIABLE_LITERAL, text)
                self.assertEqual(prefilter_spans, regex_spans, text)
                clean = dbops.redact(text)
                self.assertNotIn(secret, clean, text)
                self.assertNotIn('736563726574', clean, text)
                self.assertIn(dbops.REDACTED, clean, text)

        near_misses = (
            ("jwt_public_key", "vsql_rest.jwt_public_key = '/etc/keys/jwt.pub'", False),
            ("set_jwt_public_key", "SET GLOBAL vsql_rest.jwt_public_key = '/etc/keys/jwt.pub'", False),
            ("ssl_key", "vsql_rest.ssl_key = '/etc/ssl/server.key'", False),
            ("set_ssl_key", "SET GLOBAL vsql_rest.ssl_key = '/etc/ssl/server.key'", False),
            ("ssl_cert", "vsql_rest.ssl_cert = '/etc/ssl/server.crt'", False),
            ("oauth2_public_key", "vsql_oauth2.public_key = '-----BEGIN PUBLIC KEY-----'", False),
            ("port", "SET GLOBAL vsql_rest.port = 3000", False),
            ("suffix_char", "vsql_rest.jwt_secretx = 'keep-me'", True),
            ("set_suffix_char", "SET GLOBAL vsql_rest.jwt_secretx = 'keep-me'", True),
            ("prefix_char", "xvsql_rest.jwt_secret = 'keep-me'", True),
            ("set_prefix_char", "SET GLOBAL xvsql_rest.jwt_secret = 'keep-me'", True),
            ("quoted_prefix_char", "`xvsql_rest`.`jwt_secret` = 'keep-me'", True),
            ("unqualified_tail", "jwt_secret = 'keep-me'", True),
        )
        for label, text, prefilter_allowed in near_misses:
            with self.subTest(near_miss=label):
                regex_spans = _all_regex_spans(dbops._SECRET_VARIABLE_LITERAL, text)
                self.assertEqual(regex_spans, [], text)
                self.assertEqual(dbops._may_have_secret_variable_literals(text.lower()), prefilter_allowed, text)
                prefilter_spans = _all_prefilter_spans(dbops._SECRET_VARIABLE_LITERAL, text)
                self.assertEqual(prefilter_spans, [], text)
                self.assertEqual(dbops.redact(text), text, text)

    def test_fast_path_skips_inventory_search_for_non_secret_vsql_rest_variables(self):
        sql = ("SET GLOBAL vsql_rest.port = 3000;\n"
               "SET GLOBAL vsql_rest.ssl_key = '/etc/ssl/server.key';\n"
               "SET GLOBAL vsql_rest.jwt_public_key = '/etc/keys/jwt.pub';")
        self.assertFalse(dbops._may_have_secret_variable_literals(sql.lower()))
        with mock.patch.object(dbops, '_make_inventory_literal_search',
                               side_effect=AssertionError('should skip literal search')):
            self.assertEqual(dbops.redact(sql), sql)

    def test_set_expressions_unicode_and_error_1231_redact_jwt_secret(self):
        for raw, expected in (
            ("SET GLOBAL vsql_rest.jwt_secret = CONCAT('hs:', '%s')" % JWT_SECRET,
             "SET GLOBAL vsql_rest.jwt_secret = [redacted]"),
            ("SET PERSIST vsql_rest /* ext */ . /* key */ jwt_secret = '%s'" % JWT_SECRET,
             "SET PERSIST vsql_rest /* ext */ . /* key */ jwt_secret = '[redacted]'"),
            ("SET GLOBAL vsql_rest.jwt_secret = @jwt_token",
             "SET GLOBAL vsql_rest.jwt_secret = [redacted]"),
            ("vsql_re\u017ft.jwt_secret = '%s'" % JWT_SECRET,
             "vsql_re\u017ft.jwt_secret = '[redacted]'"),
            ("ERROR 1231 (42000): Variable 'vsql_rest.jwt_secret' can't be set to the value of '%s'" % JWT_SECRET,
             "ERROR 1231 (42000): Variable 'vsql_rest.jwt_secret' can't be set to the value of '[redacted]'"),
            ("ERROR 1231 (42000): Variable 'vsql_rest.jwt_secret' kann nicht auf '%s' gesetzt werden" % JWT_SECRET,
             "ERROR 1231 (42000): Variable 'vsql_rest.jwt_secret' kann nicht auf '[redacted]' gesetzt werden"),
        ):
            with self.subTest(raw=raw[:60]):
                self.assertEqual(dbops.redact(raw), expected)


class TestExtensionsUsesSharedInventory(unittest.TestCase):
    def test_extensions_has_no_local_secret_list_and_uses_dbops_sensitive(self):
        self.assertFalse(hasattr(extensions, '_EXTENSION_SECRET_VARIABLES'))
        self.assertIs(extensions.sensitive, dbops.sensitive)
        conn = ExtensionsConnection({
            extensions.EXTENSIONS_SQL: [
                ['vsql_rest', '0.0.4', None, None, None, None],
                ['vsql_oauth2', '0.0.4', None, None, None, None],
            ],
            extensions.REGISTRATION_SQL: [
                ['vsql_rest', '5', json.dumps({'sdk_version': '0.0.5', 'funcs': [], 'types': []})],
                ['vsql_oauth2', '5', json.dumps({'sdk_version': '0.0.5', 'funcs': [], 'types': []})],
            ],
            extensions.VARIABLES_SQL: [
                ['veb_dir', '/usr/lib/veb/'],
                ['vsql_allow_preview_extensions', 'OFF'],
                ['villagesql_vef_server_protocol', '6'],
                ['villagesql_schema_version', '2'],
                ['villagesql_server_version', VSQL_CODEBASE],
                ['villagesql_build_info', '{"git_sha":"abc123","is_dirty":false}'],
                ['vsql_rest.jwt_secret', JWT_SECRET],
                ['vsql_rest.jwt_public_key', '/etc/jwt/public.pem'],
                ['vsql_rest.ssl_key', '/etc/ssl/rest.key'],
                ['vsql_rest.ssl_cert', '/etc/ssl/rest.crt'],
                ['vsql_oauth2.public_key', '/etc/oauth/public.pem'],
            ],
            extensions.STATUS_SQL: [
                ['Villagesql_veb_cache_repaired', '0'],
                ['Com_install_extension', '2'],
                ['Com_uninstall_extension', '0'],
            ],
            extensions.COLUMNS_SQL: [],
            extensions.ROUTINES_SQL: [],
        })
        with mock.patch.object(extensions, 'sensitive', wraps=dbops.sensitive) as spy:
            report = extensions.extensions_report(conn)
        validate_ext(report, EXT_SCHEMA)
        self.assertTrue(any(call[0][0] == 'vsql_rest.jwt_secret' for call in spy.call_args_list))
        encoded = json.dumps(report)
        self.assertNotIn(JWT_SECRET, encoded)
        by_ext = {item['name']: {v['name']: v['value'] for v in item['variables']} for item in report['extensions']}
        self.assertEqual(by_ext['vsql_rest']['vsql_rest.jwt_secret'], dbops.REDACTED)
        self.assertEqual(by_ext['vsql_rest']['vsql_rest.jwt_public_key'], '/etc/jwt/public.pem')
        self.assertEqual(by_ext['vsql_rest']['vsql_rest.ssl_key'], '/etc/ssl/rest.key')
        self.assertEqual(by_ext['vsql_rest']['vsql_rest.ssl_cert'], '/etc/ssl/rest.crt')
        self.assertEqual(by_ext['vsql_oauth2']['vsql_oauth2.public_key'], '/etc/oauth/public.pem')
        self.assertIn('Sensitive extension variable values are redacted.', report['notes'])


class TestAllSurfacesRedactJwtSecret(TempHomeTestCase):
    def test_parameters_1_0_and_config_endpoint_redact_jwt_secret(self):
        rows = [
            ['version', VSQL_VERSION],
            ['version_comment', 'Source distribution'],
            ['villagesql_server_version', VSQL_CODEBASE],
            ['vsql_rest.jwt_secret', JWT_SECRET],
            ['vsql_rest.jwt_public_key', '/etc/jwt/public.pem'],
            ['vsql_rest.ssl_key', '/etc/ssl/rest.key'],
            ['vsql_rest.ssl_cert', '/etc/ssl/rest.crt'],
            ['vsql_oauth2.public_key', '/etc/oauth/public.pem'],
            ['wsrep_sst_auth', 'sst:existing-secret'],
            ['telemetry.otel_exporter_otlp_traces_headers', 'Authorization=Bearer otel-secret'],
        ]
        info_rows = [
            ['vsql_rest.jwt_secret', 'DYNAMIC', '', '0', '0', '2026-11-27 10:00:00', 'root', 'localhost'],
        ]
        report = parameters.compute_parameters(
            'mysql', (8, 4, 11), rows=rows, info_rows=info_rows,
            persisted_rows=[['vsql_rest.jwt_secret']], flavor='villagesql', base='mysql')
        encoded = json.dumps(report)
        for secret in (JWT_SECRET, 'sst:existing-secret', 'otel-secret'):
            self.assertNotIn(secret, encoded)
        found = {item['name']: item for item in report['items']}
        self.assertEqual(found['vsql_rest.jwt_secret']['value'], dbops.REDACTED)
        self.assertFalse(found['vsql_rest.jwt_secret']['truncated'])
        self.assertIsNone(found['vsql_rest.jwt_secret']['values'])
        self.assertTrue(found['vsql_rest.jwt_secret']['persisted'])
        self.assertEqual(found['wsrep_sst_auth']['value'], dbops.REDACTED)
        self.assertEqual(found['telemetry.otel_exporter_otlp_traces_headers']['value'], dbops.REDACTED)
        self.assertEqual(found['vsql_rest.jwt_public_key']['value'], '/etc/jwt/public.pem')
        self.assertEqual(found['vsql_rest.ssl_key']['value'], '/etc/ssl/rest.key')
        self.assertEqual(found['vsql_rest.ssl_cert']['value'], '/etc/ssl/rest.crt')
        self.assertEqual(found['vsql_oauth2.public_key']['value'], '/etc/oauth/public.pem')

        # SQL NULL stays None; empty string is still redacted.
        null_report = parameters.compute_parameters(
            'mysql', (8, 4, 11), rows=[['vsql_rest.jwt_secret', None]], info_rows=[], persisted_rows=[])
        self.assertIsNone(null_report['items'][0]['value'])
        empty_report = parameters.compute_parameters(
            'mysql', (8, 4, 11), rows=[['vsql_rest.jwt_secret', '']], info_rows=[], persisted_rows=[])
        self.assertEqual(empty_report['items'][0]['value'], dbops.REDACTED)

        # MariaDB catalog default and enum values for a sensitive variable are redacted too.
        cat = [[
            'VSQL_REST.JWT_SECRET', JWT_SECRET, 'SQL', JWT_SECRET, 'GLOBAL', 'VARCHAR',
            None, None, None, JWT_SECRET, 'NO', None, 'JWT HMAC secret', 'OPTIONAL',
        ]]
        maria_report = parameters.compute_parameters('mariadb', (11, 4, 13), catalog_rows=cat)
        self.assertNotIn(JWT_SECRET, json.dumps(maria_report))
        self.assertEqual(maria_report['items'][0]['value'], dbops.REDACTED)
        self.assertEqual(maria_report['items'][0]['default'], dbops.REDACTED)
        self.assertIsNone(maria_report['items'][0]['values'])

        # Full read_parameters path with a replayed connection.
        tag = config_review.CONFIG_TAG
        conn = PerfReplayConn([
            [tag + 'SHOW GLOBAL VARIABLES', batch(rows)],
            [tag + parameters.MYSQL_INFO_SQL, batch(info_rows)],
            [tag + parameters.MYSQL_PERSISTED_SQL, batch([['vsql_rest.jwt_secret']])],
        ])
        live_params = parameters.read_parameters(
            Reader(conn, tag),
            Server({'version': VSQL_VERSION, 'version_comment': 'Source distribution',
                    'villagesql_server_version': VSQL_CODEBASE}, {}),
            [], [])
        self.assertNotIn(JWT_SECRET, json.dumps(live_params))
        self.assertEqual({i['name']: i['value'] for i in live_params['items']}['vsql_rest.jwt_secret'],
                         dbops.REDACTED)

        # Full /api/live/config endpoint path (config_report(parameters=True)).
        class ConfigEndpointConn(object):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def run(self, sql, extra_flags=None, timeout=None):
                clean = sql[len(tag):] if sql.startswith(tag) else sql
                if clean == 'SHOW GLOBAL VARIABLES':
                    return batch(rows)
                if clean.startswith('SHOW GLOBAL VARIABLES WHERE'):
                    return batch([
                        ['version', VSQL_VERSION],
                        ['version_comment', 'Source distribution'],
                        ['max_connections', '151'],
                        ['performance_schema', 'ON'],
                    ])
                if clean == 'SELECT @@villagesql_server_version':
                    return VSQL_CODEBASE + '\n'
                if clean.startswith('SHOW GLOBAL STATUS'):
                    return batch([['Uptime', '7200']])
                if clean == parameters.MYSQL_INFO_SQL:
                    return batch(info_rows)
                if clean.startswith('SELECT VARIABLE_NAME, VARIABLE_SOURCE, NULL FROM performance_schema.variables_info'):
                    return batch([])
                if clean == parameters.MYSQL_PERSISTED_SQL:
                    return batch([['vsql_rest.jwt_secret']])
                if 'INNODB_TABLESPACES' in clean:
                    return batch([['1048576', '1']])
                raise AssertionError('unexpected SQL: ' + sql)

        with mock.patch('myflames.connector.MySQLConnection', FakeFactory(ConfigEndpointConn())):
            api_cfg = ui.api_response(
                '/api/live/config',
                {'connection': {'host': '127.0.0.1', 'port': 33384, 'user': 'root'}},
                self.workspace)
        self.assertNotIn(JWT_SECRET, json.dumps(api_cfg))
        cfg_items = {i['name']: i for i in api_cfg['parameters']['items']}
        self.assertEqual(cfg_items['vsql_rest.jwt_secret']['value'], dbops.REDACTED)
        self.assertEqual(cfg_items['vsql_rest.jwt_public_key']['value'], '/etc/jwt/public.pem')

    def test_single_variable_read_plan_and_endpoint_redact_jwt_secret(self):
        tag = variables.TAG
        mark = variables._MARK
        secret_hex = JWT_SECRET.encode('utf-8').hex().upper()
        read_names = ', '.join("'%s'" % n for n in sorted((
            'version', 'version_comment', 'persisted_globals_load', 'version_compile_os',
            'server_uuid', 'server_uid', 'server_id', 'hostname', 'port', 'wsrep_node_name', 'wsrep_on',
            'vsql_rest.jwt_secret',
        )))
        read_script = '; '.join(tag + s for s in (
            'SHOW GLOBAL VARIABLES WHERE Variable_name IN (%s)' % read_names,
            "SELECT '%ssession'" % mark,
            "SHOW SESSION VARIABLES WHERE Variable_name = 'vsql_rest.jwt_secret'",
        ))
        detail_script = '; '.join(tag + s for s in (
            "SELECT '%sinfo'" % mark,
            "SELECT VARIABLE_SOURCE, VARIABLE_PATH, MIN_VALUE, MAX_VALUE, SET_TIME, SET_USER, SET_HOST "
            "FROM performance_schema.variables_info WHERE VARIABLE_NAME = 'vsql_rest.jwt_secret'",
            "SELECT '%spersisted'" % mark,
            "SELECT VARIABLE_VALUE IS NULL, HEX(VARIABLE_VALUE) FROM performance_schema.persisted_variables "
            "WHERE VARIABLE_NAME = 'vsql_rest.jwt_secret'",
        ))
        first_result = batch([
            ['hostname', 'vsql-host'],
            ['persisted_globals_load', 'ON'],
            ['port', '3306'],
            ['server_id', '1'],
            ['server_uuid', '11111111-2222-3333-4444-555555555555'],
            ['version', VSQL_VERSION],
            ['version_comment', 'Source distribution'],
            ['version_compile_os', 'Linux'],
            ['vsql_rest.jwt_secret', JWT_SECRET],
            [mark + 'session'],
            ['vsql_rest.jwt_secret', JWT_SECRET],
        ])
        detail_result = batch([
            [mark + 'info'],
            ['PERSISTED', '', '0', '0', '2026-11-27 10:00:00', 'root', 'localhost'],
            [mark + 'persisted'],
            ['0', secret_hex],
        ])
        calls = [
            [read_script, first_result],
            ['/*myflames:target-evidence*/ SELECT @@villagesql_server_version', VSQL_CODEBASE + '\n'],
            [detail_script, detail_result],
        ]
        class VillageEvidenceConn(PerfReplayConn):
            def __init__(self, replay_calls):
                super(VillageEvidenceConn, self).__init__(replay_calls)
                self.evidence_values = {
                    'server_id': '1', 'hostname': 'vsql-host', 'port': '3306',
                    'version': VSQL_VERSION,
                    'server_uuid': '11111111-2222-3333-4444-555555555555',
                }

            @evidence_options
            def run(self, sql, extra_flags=None, timeout=None):
                body, marker = evidence_body(sql)
                def read():
                    return super(VillageEvidenceConn, self).run(
                        body, extra_flags=extra_flags, timeout=timeout)
                return (evidence_call(read, marker, self.evidence_values,
                                      '--xml' in (extra_flags or []))
                        if marker else read())

        conn = VillageEvidenceConn(calls)
        with mock.patch('myflames.connector.MySQLConnection', FakeFactory(conn)):
            read = ui._variable_response(
                '/api/live/variable',
                {'connection': {'host': '127.0.0.1', 'port': 33384, 'user': 'root'},
                 'name': 'vsql_rest.jwt_secret'},
                self.workspace)
        self.assertEqual(read['schema_version'], 'variable-1.0')
        self.assertEqual(read['global_value'], dbops.REDACTED)
        self.assertEqual(read['session_value'], dbops.REDACTED)
        self.assertTrue(read['persisted']['present'])
        self.assertEqual(read['persisted']['value'], dbops.REDACTED)
        self.assertNotIn(JWT_SECRET, json.dumps(read))

        # Planning a change never leaks the old secret in before, variable, undo, or describe_plan.
        plan = variables.build_plan(read, 'new-rotated-value', 'global')
        self.assertEqual(plan['status'], 'ready')
        self.assertEqual(plan['before']['global'], dbops.REDACTED)
        self.assertEqual(plan['before']['persisted'], dbops.REDACTED)
        self.assertEqual(plan['undo'], [])
        self.assertNotIn(JWT_SECRET, json.dumps(plan))
        self.assertNotIn(JWT_SECRET, '\n'.join(variables.describe_plan(plan)))

        # Non-secret VillageSQL variable stays readable.
        pub_info = {
            'name': 'vsql_rest.jwt_public_key', 'engine': 'mysql', 'flavor': 'villagesql',
            'server_product': 'VillageSQL', 'server_base': 'mysql', 'server_version': VSQL_VERSION,
            'version_tuple': (8, 4, 11), 'in_global': True, 'in_session': False,
            'global_value': '/etc/jwt/public.pem', 'session_value': None, 'catalog': None,
            'info': None, 'metadata': None, 'persisted_globals_load': 'ON',
            'persisted': {'present': False, 'value': None, 'read': True}, 'notes': [],
        }
        self.assertEqual(variables.describe(pub_info)['global_value'], '/etc/jwt/public.pem')

        # RESET PERSIST result must not echo the running secret in message or after.
        reset_plan = variables.build_plan(read, None, 'reset_persist')
        self.assertEqual(reset_plan['status'], 'ready')
        self.assertEqual(reset_plan['undo'], [])
        after_reset = {
            'global': JWT_SECRET, 'global_read': True, 'session': None, 'session_read': False,
            'persisted': None, 'persisted_state': {'state': 'absent', 'read': True, 'present': False, 'value': None},
            'engine': read['engine'], 'server_version': read['server_version'],
            'server_identity': read['server_identity'],
        }
        reset_conn = ReviewedReplayConn([[tag + reset_plan['statement'], '']])
        with mock.patch.object(variables, 'read_variable', return_value=read), \
                mock.patch.object(variables, '_after', return_value=after_reset):
            reset_res = variables.apply_change(
                reset_conn, reset_plan, reset_plan['expect'], confirmed=True,
                audit=dbops.AuditTrail(self.store, 'reset_persist', 'ui'))
        self.assertEqual(reset_res['status'], 'changed')
        self.assertNotIn(JWT_SECRET, json.dumps(reset_res))
        self.assertEqual(reset_res['after']['global'], dbops.REDACTED)
        self.assertIn(dbops.REDACTED, reset_res['message'])

    def test_snapshots_compare_cli_exports_and_topology_redact_jwt_secret(self):
        raw_a = _villagesql_snapshot([
            _param_item('max_connections', '151', 'integer', '151'),
            _param_item('vsql_rest.jwt_secret', JWT_SECRET, 'string', JWT_SECRET, values=[JWT_SECRET]),
            _param_item('vsql_rest.jwt_public_key', '/etc/jwt/public.pem', 'string'),
            _param_item('vsql_rest.ssl_key', '/etc/ssl/rest.key', 'string'),
        ], host='vsql-a')
        raw_b = _villagesql_snapshot([
            _param_item('max_connections', '151', 'integer', '151'),
            _param_item('vsql_rest.jwt_secret', 'second-different-jwt-secret', 'string'),
            _param_item('vsql_rest.jwt_public_key', '/etc/jwt/public.pem', 'string'),
            _param_item('vsql_rest.ssl_key', '/etc/ssl/rest.key', 'string'),
        ], host='vsql-b')

        # Capture path from config_report(parameters=True).
        config_payload = {
            'engine': 'mysql', 'flavor': 'villagesql', 'server_product': 'VillageSQL',
            'server_version': VSQL_VERSION, 'notes': [], 'checks': [],
            'parameters': parameters.compute_parameters(
                'mysql', (8, 4, 11),
                rows=[['max_connections', '151'], ['vsql_rest.jwt_secret', JWT_SECRET],
                      ['vsql_rest.jwt_public_key', '/etc/jwt/public.pem']],
                info_rows=[], persisted_rows=[], flavor='villagesql', base='mysql'),
        }
        with mock.patch('myflames.config_review.config_report', return_value=config_payload):
            captured = param_compare.capture(mock.Mock(host='vsql-a', port=3306),
                                             {'host': 'vsql-a', 'port': 3306})
        self.assertNotIn(JWT_SECRET, json.dumps(captured))
        cap_by_name = {i['name']: i for i in captured['items']}
        self.assertEqual(cap_by_name['vsql_rest.jwt_secret']['value'], dbops.REDACTED)
        self.assertTrue(cap_by_name['vsql_rest.jwt_secret']['redacted'])
        self.assertEqual(cap_by_name['vsql_rest.jwt_public_key']['value'], '/etc/jwt/public.pem')

        # Untrusted snapshot validation and persistence in WorkspaceStore.
        saved_a = self.store.parameter_snapshot_save(raw_a, 'vsql-a')
        saved_b = self.store.parameter_snapshot_save(raw_b, 'vsql-b')
        raw_db_bytes = self.store.path.read_bytes()
        self.assertNotIn(JWT_SECRET.encode('utf-8'), raw_db_bytes)
        self.assertNotIn(b'second-different-jwt-secret', raw_db_bytes)
        loaded_a = self.store.parameter_snapshot_get(saved_a['id'])
        self.assertNotIn(JWT_SECRET, json.dumps(loaded_a))
        loaded_row = {i['name']: i for i in loaded_a['snapshot']['items']}['vsql_rest.jwt_secret']
        self.assertEqual((loaded_row['value'], loaded_row['default'], loaded_row['values'], loaded_row['redacted']),
                         (dbops.REDACTED, dbops.REDACTED, None, True))

        # Snapshot API and compare API.
        api_snap = compare_api.response('/api/config/snapshots/get', {'id': saved_a['id']}, self.store)
        self.assertNotIn(JWT_SECRET, json.dumps(api_snap))
        api_cmp = compare_api.response(
            '/api/config/compare',
            {'source': {'snapshot': saved_a['id']}, 'target': {'snapshot': saved_b['id']}},
            self.store)
        self.assertNotIn(JWT_SECRET, json.dumps(api_cmp))
        self.assertNotIn('second-different-jwt-secret', json.dumps(api_cmp))
        cmp_rows = {r['name']: r for r in api_cmp['items']}
        self.assertEqual(cmp_rows['vsql_rest.jwt_secret']['status'], 'not_compared')
        self.assertEqual(cmp_rows['vsql_rest.jwt_secret']['source']['value'], dbops.REDACTED)
        self.assertEqual(cmp_rows['vsql_rest.jwt_secret']['target']['value'], dbops.REDACTED)
        self.assertEqual(cmp_rows['vsql_rest.jwt_public_key']['status'], 'identical')

        # Even identical raw secret values must never compare as identical.
        same_cmp = param_compare.compare(raw_a, raw_a)
        same_row = {r['name']: r for r in same_cmp['items']}['vsql_rest.jwt_secret']
        self.assertEqual(same_row['status'], 'not_compared')
        self.assertNotIn(JWT_SECRET, json.dumps(same_cmp))

        # Text export via compare_cli.format_compare and real CLI snapshot/compare commands.
        text_export = compare_cli.format_compare(api_cmp, show_all=True)
        self.assertNotIn(JWT_SECRET, text_export)
        self.assertNotIn('second-different-jwt-secret', text_export)
        self.assertIn('vsql_rest.jwt_secret', text_export)
        self.assertIn(dbops.REDACTED, text_export)

        # CLI compare exits 0 when an explicit inventory secret is the only not_compared row.
        def run_cli(argv):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(sys, 'argv', ['myflames'] + argv), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    cli.main()
                    code = 0
                except SystemExit as exc:
                    code = exc.code or 0
            return code, out.getvalue(), err.getvalue()

        code, out, err = run_cli(['config', 'snapshot', 'show', saved_a['id']])
        self.assertEqual(code, 0, err)
        self.assertNotIn(JWT_SECRET, out + err)
        self.assertIn(dbops.REDACTED, out)

        code, out, err = run_cli(['config', 'compare', 'snapshot:' + saved_a['id'],
                                  'snapshot:' + saved_b['id'], '--all'])
        self.assertEqual(code, 0, (out, err))
        self.assertNotIn(JWT_SECRET, out + err)
        self.assertNotIn('second-different-jwt-secret', out + err)
        self.assertIn(dbops.REDACTED, out)

        # Group comparison (compare_many) and rolling variable change refusal.
        group_rows = {r['name']: r for r in param_compare.compare_many({'a': raw_a, 'b': raw_b})}
        self.assertEqual(group_rows['vsql_rest.jwt_secret']['status'], 'missing')
        self.assertEqual(group_rows['vsql_rest.jwt_secret']['values'], {'a': dbops.REDACTED, 'b': dbops.REDACTED})
        self.assertNotIn(JWT_SECRET, json.dumps(group_rows))
        untouched_store = mock.Mock()
        with self.assertRaisesRegex(ValueError, 'Sensitive variables') as refused:
            topology_change.plan(untouched_store, 'G', 'a', 'vsql_rest.jwt_secret', JWT_SECRET, 'global', ['a'])
        self.assertNotIn(JWT_SECRET, str(refused.exception))
        self.assertEqual(untouched_store.mock_calls, [])

    def test_editor_history_saved_queries_and_session_restore_redact_jwt_secret(self):
        sql = ("SET GLOBAL vsql_rest.jwt_secret = '%s';\n"
               "SET PERSIST `vsql_rest`.`jwt_secret` = '%s'" % (JWT_SECRET, 'second-jwt-secret'))
        run = types.SimpleNamespace(started_at='2026-11-27T10:00:00Z', sql_text=sql, kind='write', scope='all')
        snap = {'counts': {'statements': 2, 'run': 2, 'rows': 0, 'affected_rows': 0},
                'duration_ms': 1.2, 'state': 'done', 'error': None}
        sess = types.SimpleNamespace(tab_id='t1', profile_id='p1', name='vsql', host='127.0.0.1',
                                     port=33384, engine='mysql')
        entry = editor_session.history_entry(run, snap, sess)
        self.store.history_add(entry, editor_session.HISTORY_MAX_ENTRIES, editor_session.HISTORY_MAX_BYTES)
        history = self.store.history_list()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['sql'].count(dbops.REDACTED), 2)
        self.assertNotIn(JWT_SECRET, json.dumps(history))
        self.assertNotIn('second-jwt-secret', json.dumps(history))

        saved_reply = editor_session.api_response(
            '/api/editor/saved/save',
            {'name': "Rotate vsql_rest.jwt_secret = '%s'" % JWT_SECRET,
             'folder': 'villagesql', 'sql': sql,
             'note': "Do not use vsql_rest.jwt_secret = '%s'" % JWT_SECRET},
            self.workspace, lambda v: v)
        self.assertIs(saved_reply['redacted'], True)
        self.assertNotIn(JWT_SECRET, json.dumps(saved_reply))
        self.assertNotIn('second-jwt-secret', json.dumps(saved_reply))
        self.assertNotIn(JWT_SECRET, json.dumps(self.store.saved_query_list()))

        # Workspace session restore redacts every SQL-bearing field.
        state = {'tabs': [{'sql': sql, 'text': sql, 'query': sql, 'statement': sql, 'sql_text': sql}]}
        self.store.save_session(state, 0)
        restored = WorkspaceStore(self.store.path).load_session()['state']
        self.assertNotIn(JWT_SECRET, json.dumps(restored))
        self.assertNotIn('second-jwt-secret', json.dumps(restored))
        self.assertEqual(restored['tabs'][0]['sql'].count(dbops.REDACTED), 2)
        raw_bytes = self.store.path.read_bytes()
        self.assertNotIn(JWT_SECRET.encode('utf-8'), raw_bytes)
        self.assertNotIn(b'second-jwt-secret', raw_bytes)

    def test_audit_log_and_json_csv_text_exports_redact_jwt_secret(self):
        new_secret = "jwt-new'secret"
        # 1. Direct AuditTrail statement and diagnostic redaction.
        trail = dbops.AuditTrail(
            self.store, 'editor_run', 'ui',
            connection={'host': '127.0.0.1', 'port': 33384, 'user': 'root'},
            safety={'profile_id': 'p1', 'profile_name': 'vsql-lab', 'type': 'development', 'read_only': False})
        trail.server('mysql', VSQL_VERSION)
        trail.outcome(
            ["SET PERSIST vsql_rest.jwt_secret = '%s'" % JWT_SECRET],
            'error', outcome='error',
            message="ERROR 1231 (42000): Variable 'vsql_rest.jwt_secret' can't be set to the value of '%s'" % JWT_SECRET)

        # 2. Variable apply endpoint early refusal / decline / apply via _SensitiveAudit.
        info = {
            'name': 'vsql_rest.jwt_secret', 'engine': 'mysql', 'flavor': 'villagesql',
            'server_product': 'VillageSQL', 'server_base': 'mysql', 'server_version': VSQL_VERSION,
            'version_tuple': (8, 4, 11), 'in_global': True, 'in_session': False,
            'global_value': JWT_SECRET, 'session_value': None, 'catalog': None, 'info': None,
            'metadata': None, 'persisted_globals_load': 'ON',
            'server_identity': {'server_uuid': '11111111-2222-3333-4444-555555555555'},
            'persisted': {'present': False, 'value': None, 'read': True}, 'notes': [],
        }
        v = variables.describe(info)
        plan = variables.build_plan(v, new_secret, 'global')
        connection = {'host': '127.0.0.1', 'port': 33384, 'user': 'root', 'read_only': True}
        safety, _ = ui._variable_policy('vsql_rest.jwt_secret', 'global', connection, self.store)
        expect = dict(plan['expect'], connection_identity=dbops.connection_identity(connection, safety))
        result = ui.api_response('/api/live/variable/apply', {
            'connection': connection, 'name': 'vsql_rest.jwt_secret', 'mode': 'global',
            'value': new_secret, 'expect': expect, 'confirmed': True,
        }, self.workspace)
        self.assertEqual(result['status'], 'refused')

        # Check raw SQLite file, audit_report, JSON export, CSV export, and CLI text output.
        report = dbops.audit_report(self.store)
        self.assertEqual(len(report['records']), 2)
        export_json = dbops.audit_export(self.store, fmt='json')['content']
        export_csv = dbops.audit_export(self.store, fmt='csv')['content']
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            workspace_cli.cmd_audit(['list'])
        cli_text = out.getvalue()

        raw_bytes = self.store.path.read_bytes()
        for secret in (JWT_SECRET, new_secret, new_secret.encode('utf-8').hex(),
                       new_secret.encode('utf-8').hex().upper()):
            self.assertNotIn(secret.encode('utf-8'), raw_bytes)
            for surface_name, text in (('audit_report', json.dumps(report)),
                                       ('audit_json', export_json),
                                       ('audit_csv', export_csv),
                                       ('audit_cli_text', cli_text)):
                self.assertNotIn(secret, text, (surface_name, secret))
        for text in (export_json, export_csv, cli_text):
            self.assertIn(dbops.REDACTED, text)


class TestAbuseCases(unittest.TestCase):
    def test_50kb_value_redacted_across_sql_and_variable_surfaces(self):
        big_secret = 'S' * 100 + ('0123456789abcdef' * 3125) + 'E' * 100
        self.assertGreater(len(big_secret), 50000)
        head, tail = big_secret[:40], big_secret[-40:]

        for sql in (
            "SET GLOBAL vsql_rest.jwt_secret = '%s'; SELECT 1;" % big_secret,
            "@@GLOBAL.vsql_rest.jwt_secret = '%s'; SELECT 1;" % big_secret,
            "`vsql_rest` . `jwt_secret` := '%s'; SELECT 1;" % big_secret,
        ):
            with self.subTest(sql=sql[:50]):
                started = time.monotonic()
                clean = dbops.redact(sql)
                self.assertLess(time.monotonic() - started, 2.0)
                self.assertNotIn(head, clean)
                self.assertNotIn(tail, clean)
                self.assertTrue(clean.endswith("'[redacted]'; SELECT 1;"))

        # Variable surfaces must redact without leaking prefixes.
        params = parameters.compute_parameters(
            'mysql', (8, 4, 11), rows=[['vsql_rest.jwt_secret', big_secret]],
            info_rows=[], persisted_rows=[], flavor='villagesql', base='mysql')
        self.assertEqual(params['items'][0]['value'], dbops.REDACTED)
        self.assertNotIn(head, json.dumps(params))

        single = variables.describe({
            'name': 'vsql_rest.jwt_secret', 'engine': 'mysql', 'flavor': 'villagesql',
            'server_product': 'VillageSQL', 'server_base': 'mysql', 'server_version': VSQL_VERSION,
            'version_tuple': (8, 4, 11), 'in_global': True, 'in_session': True,
            'global_value': big_secret, 'session_value': big_secret, 'catalog': None,
            'info': None, 'metadata': None, 'persisted_globals_load': 'ON',
            'persisted': {'present': True, 'value': big_secret, 'read': True}, 'notes': [],
        })
        self.assertEqual(single['global_value'], dbops.REDACTED)
        self.assertEqual(single['session_value'], dbops.REDACTED)
        self.assertEqual(single['persisted']['value'], dbops.REDACTED)
        self.assertNotIn(head, json.dumps(single))

        ext_conn = ExtensionsConnection({
            extensions.EXTENSIONS_SQL: [['vsql_rest', '0.0.4', None, None, None, None]],
            extensions.REGISTRATION_SQL: [['vsql_rest', '5', json.dumps({'funcs': [], 'types': []})]],
            extensions.VARIABLES_SQL: [
                ['villagesql_server_version', VSQL_CODEBASE],
                ['vsql_rest.jwt_secret', big_secret],
            ],
            extensions.STATUS_SQL: [], extensions.COLUMNS_SQL: [], extensions.ROUTINES_SQL: [],
        })
        ext_report = extensions.extensions_report(ext_conn)
        self.assertEqual(ext_report['extensions'][0]['variables'][0]['value'], dbops.REDACTED)
        self.assertNotIn(head, json.dumps(ext_report))

        snap = param_compare.validate_snapshot(
            _villagesql_snapshot([_param_item('vsql_rest.jwt_secret', big_secret)]))
        self.assertEqual(snap['items'][0]['value'], dbops.REDACTED)
        self.assertNotIn(head, json.dumps(snap))

    def test_value_with_redaction_marker_quotes_and_newlines(self):
        raw_val = 'head [redacted] \'single\' "double" line1\nline2\r\ntail-secret'
        fragments = ('head', 'single', 'double', 'line1', 'line2', 'tail-secret')
        sql_escaped = raw_val.replace("'", "''")
        for text in (
            "SET GLOBAL vsql_rest.jwt_secret = '%s'; SELECT 1;" % sql_escaped,
            "@@GLOBAL.`vsql_rest`.`jwt_secret` = '%s'; SELECT 1;" % sql_escaped,
            "ERROR 1231 (42000): Variable 'vsql_rest.jwt_secret' can't be set to the value of '%s'" % raw_val,
        ):
            with self.subTest(text=text[:40]):
                clean = dbops.redact(text)
                for fragment in fragments:
                    self.assertNotIn(fragment, clean, (fragment, clean))
                self.assertIn(dbops.REDACTED, clean)

        # Variable row surfaces.
        params = parameters.compute_parameters(
            'mysql', (8, 4, 11), rows=[['vsql_rest.jwt_secret', raw_val]],
            info_rows=[], persisted_rows=[])
        self.assertEqual(params['items'][0]['value'], dbops.REDACTED)
        self.assertFalse(any(f in json.dumps(params) for f in fragments))

        snap = param_compare.validate_snapshot(
            _villagesql_snapshot([_param_item('vsql_rest.jwt_secret', raw_val)]))
        self.assertEqual(snap['items'][0]['value'], dbops.REDACTED)
        self.assertFalse(any(f in json.dumps(snap) for f in fragments))

    def test_statement_setting_jwt_secret_twice(self):
        first, second = 'first-jwt-secret-111', 'second-jwt-secret-222'
        cases = (
            ("SET GLOBAL vsql_rest.jwt_secret = '%s', vsql_rest.jwt_secret = '%s'" % (first, second),
             "SET GLOBAL vsql_rest.jwt_secret = '[redacted]', vsql_rest.jwt_secret = '[redacted]'"),
            ("SET GLOBAL vsql_rest.jwt_secret = '%s', vsql_rest.port = 3000, "
             "@@GLOBAL.`vsql_rest`.`jwt_secret` = '%s'" % (first, second),
             "SET GLOBAL vsql_rest.jwt_secret = '[redacted]', vsql_rest.port = 3000, "
             "@@GLOBAL.`vsql_rest`.`jwt_secret` = '[redacted]'"),
            ("SET GLOBAL vsql_rest.jwt_secret = '%s'; SET PERSIST vsql_rest . jwt_secret = '%s'" % (first, second),
             "SET GLOBAL vsql_rest.jwt_secret = '[redacted]'; SET PERSIST vsql_rest . jwt_secret = '[redacted]'"),
            ("vsql_rest.jwt_secret = '%s' and `vsql_rest` . `jwt_secret` := '%s'" % (first, second),
             "vsql_rest.jwt_secret = '[redacted]' and `vsql_rest` . `jwt_secret` := '[redacted]'"),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw[:55]):
                clean = dbops.redact(raw)
                self.assertEqual(clean, expected)
                self.assertNotIn(first, clean)
                self.assertNotIn(second, clean)
                self.assertEqual(clean.count(dbops.REDACTED), 2)

    def test_mentions_inside_comments_and_strings_follow_existing_rules(self):
        # Mentions without a literal assignment remain unchanged.
        unchanged = (
            "/* check vsql_rest.jwt_secret before reload */ SELECT 1",
            "-- vsql_rest.jwt_secret should be rotated\nSELECT 1",
            "# vsql_rest.jwt_secret\nSELECT 1",
            "SELECT 'vsql_rest.jwt_secret'",
            "PREPARE s FROM 'SELECT vsql_rest.jwt_secret'",
            "SET @q = CONCAT('SET GLOBAL vsql_rest.jwt_secret = ', QUOTE(@jwt))",
            'SET @q = CONCAT("SET GLOBAL vsql_rest.jwt_secret = ", QUOTE(@jwt))',
        )
        for raw in unchanged:
            with self.subTest(unchanged=raw):
                self.assertEqual(dbops.redact(raw), raw)

        # Ordinary comments and strings stay inert to the SET lexer (_redact_secret_variables),
        # while the context-free inventory literal rule in dbops.redact hides quoted/encoded
        # assignments inside comments, strings and dynamic SQL echoes (matching existing rules).
        comment_and_string_assignments = (
            ("/* SET GLOBAL vsql_rest.jwt_secret = '%s' */ SELECT 1" % JWT_SECRET,
             "/* SET GLOBAL vsql_rest.jwt_secret = '[redacted]' */ SELECT 1"),
            ("-- vsql_rest.jwt_secret = '%s'\nSELECT 1" % JWT_SECRET,
             "-- vsql_rest.jwt_secret = '[redacted]'\nSELECT 1"),
            ("# `vsql_rest`.`jwt_secret` = '%s'\nSELECT 1" % JWT_SECRET,
             "# `vsql_rest`.`jwt_secret` = '[redacted]'\nSELECT 1"),
            ('EXECUTE IMMEDIATE "SET GLOBAL vsql_rest.jwt_secret = \'%s\'"; SELECT 1' % JWT_SECRET,
             'EXECUTE IMMEDIATE "SET GLOBAL vsql_rest.jwt_secret = \'[redacted]\'"; SELECT 1'),
        )
        for raw, expected in comment_and_string_assignments:
            with self.subTest(assignment_in_comment_or_string=raw[:55]):
                self.assertEqual(dbops._redact_secret_variables(raw), raw)
                self.assertEqual(dbops.redact(raw), expected)

        # Doubled-quote dynamic SQL inside a single-quoted string fails closed inside
        # that string and preserves trailing SQL after it.
        dyn = "PREPARE s FROM 'SET GLOBAL vsql_rest.jwt_secret = ''%s'''; SELECT 1" % JWT_SECRET
        clean_dyn = dbops.redact(dyn)
        self.assertNotIn(JWT_SECRET, clean_dyn)
        self.assertIn(dbops.REDACTED, clean_dyn)
        self.assertTrue(clean_dyn.endswith('; SELECT 1'), clean_dyn)

        # Executable comments are scanned as SQL by both the SET lexer and dbops.redact.
        exec_sql = "/*!80000 SET GLOBAL vsql_rest.jwt_secret = '%s' */; SELECT 1" % JWT_SECRET
        self.assertEqual(dbops._redact_secret_variables(exec_sql),
                         exec_sql.replace(JWT_SECRET, dbops.REDACTED))
        self.assertEqual(dbops.redact(exec_sql),
                         exec_sql.replace(JWT_SECRET, dbops.REDACTED))


if __name__ == '__main__':
    unittest.main()
